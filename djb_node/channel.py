# SPDX-License-Identifier: Apache-2.0
"""C10c · 客户端出站长轮询通道（djb-node 侧）。

设计目标：**客户端不需要入站端口**。节点主动向平台长轮询取 job，把 job 转成本机
回环镜像端点（``127.0.0.1:<port>/v1/chat/completions``）的一次调用，再把结果回传平台。

    ┌────────┐  POST /channel/poll（长轮询，X-Client-Key）   ┌────────┐
    │  node  │ ───────────────────────────────────────────▶ │  平台  │
    │        │ ◀─────────────── 200 job / 204 无 job ─────── │        │
    │        │  POST 127.0.0.1:<port>/v1/chat/completions    │        │
    │        │  （Bearer <一次性 local_token>）              │        │
    │        │  POST /channel/result（幂等键 = job_id）        │        │
    └────────┘ ───────────────────────────────────────────▶ └────────┘

硬约束：

* **只出站**：不监听任何新端口；本机调用固定走 ``127.0.0.1``（INV-03）。
* **不落正文**：``event()`` 只出现 job_id / node_id / 状态 / 耗时 / 错误类 / 长度类计数，
  **永不**记录 messages、content、prompt、local_token 或任何凭据；也不记录平台 body。
* **单在途**（S2）：一次只执行一个 job；本地执行时间被 ``job.timeout_s`` 截断
  （上限 ``facts.CHANNEL_JOB_TIMEOUT_MAX_S``），不让单个 job 拖死 poll 主循环。
* **失败不致命**：poll 失败按 ``facts.CHANNEL_BACKOFF_S`` 退避（1/5/30 s）继续；
  只有 401/403（凭据失效 / 被冻结）按 M15 §7 停止通道并留事件。
* **开关默认关闭**：``client.channel.enabled``（见 ``facts.FEATURE_FLAGS``）；关闭时
  daemon 不创建本客户端，行为与无通道版本完全一致（回滚：已取 job 尽力回传后停止）。

job（平台 → 节点；spec 1.2.9 ``NodeChannelPollResponse.job``）::

    {"job": {"job_id": "job-1", "kind": "chat.completions", "request_id": "req-1",
             "model": "gpt-4o-mini", "messages": [...], "max_tokens": 512,
             "temperature": 0.7, "stop": ["\\n\\n"], "timeout_s": 120,
             "lease_expires_at": "2026-10-24T00:05:00Z"},
     "server_time": "2026-10-24T00:00:00Z"}

``parse_job`` 同时兼容请求体嵌套在 ``request`` / ``requests`` 或扁平形状。

结果（节点 → 平台；字段严格按 ``NodeChannelResultRequest``，多余都会被丢弃）::

    {"job_id": "job-1", "status": "completed", "content": "...",
     "finish_reason": "stop", "tokens_in": 7, "tokens_out": 2,
     "usage_source": "actual", "latency_ms": 123}
    {"job_id": "job-1", "status": "failed", "error_code": "50903",
     "error_class": "upstream_error", "latency_ms": 42}

``status`` 只有 ``completed`` / ``failed``；**不发** ``node_id`` / ``usage`` 嵌套 /
``duration_ms`` / ``completed_at``（C10a 契约里没有这些字段）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import facts
from .api import NodeApi
from .config import ClientConfig, Paths, feature_flag
from .errors import (AuthError, DjbError, ForbiddenError, NetworkError, RateLimitError)
from .httpclient import HttpClient
from .logging_setup import event
from .secure import CredentialStore
from .util import iso, utcnow

__all__ = ["ChannelJob", "ChannelClient", "channel_enabled", "explicit_flag_value",
           "parse_job", "result_for_error"]

#: 本机调用签名：(body, timeout) -> HttpResponse 形状（status/headers/body/json()）
ChatCall = Callable[[dict[str, Any], float], Any]


def explicit_flag_value(config: ClientConfig | None, name: str) -> Any:
    """**显式写盘**的开关值（``None`` = 没写过）；与 ``config.feature_flag`` 同一取值优先级。

    为什么要单独读一次：``feature_flag`` 分不清「用户显式关掉」和「没写过」。
    P4 起通道默认开，同时服务端可以用心跳 ``channel_required`` 要求通道；
    这两种来源都不能覆盖用户的**显式**关闭，否则 ``rollback_rule`` 里的
    ``djb flags --disable client.channel.enabled`` 就失效了（回滚不回去 = 没有开关）。
    """
    if config is None:
        return None
    extra = getattr(config, "extra", None)
    if not isinstance(extra, Mapping):
        return None
    nested = extra.get("feature_flags")
    if isinstance(nested, Mapping) and name in nested:
        return nested[name]
    if name in extra:
        return extra[name]
    return None


def channel_enabled(config: ClientConfig | None, *, required: bool = False) -> bool:
    """通道是否应工作：开关 ``client.channel.enabled``（0.2.4 起默认开）+ 服务端协商。

    ``required`` = 心跳响应里的 ``channel_required``（P4 落点 B）。优先级：

    1. 本地**显式**写盘 false → 关（kill switch 优先，回滚路径必须可靠）；
    2. 否则 ``feature_flag`` 真值 or ``required`` → 开。

    第 2 条让老配置（0.2.3 之前默认关闭、且没写过 flags 的机器）在云端要求通道时也能起来，
    不需要用户先跑 ``djb flags --enable``。
    """
    if config is None:
        return False
    if explicit_flag_value(config, facts.CHANNEL_FLAG) is False:
        return False
    return bool(feature_flag(config, facts.CHANNEL_FLAG) or required)


# ─────────────────────────────────────────────────────────── job 解析
@dataclass
class ChannelJob:
    """解析后的 job：只保留执行需要的字段（原 payload 不参与日志）。"""

    job_id: str
    node_id: str | None = None
    model: str = ""
    messages: list[Any] = field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    stop: list[str] | None = None
    timeout_s: float = float(facts.CHANNEL_JOB_TIMEOUT_DEFAULT_S)
    # R2-1 加性透传：None / 空 时 body 与改前逐字节一致
    tools: list[Any] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    top_p: float | None = None
    seed: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None

    def body(self) -> dict[str, Any]:
        """转给本机镜像端点的 body（只带白名单字段；stream 固定 False）。"""
        body: dict[str, Any] = {"model": self.model, "messages": self.messages,
                                "stream": False}
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.stop:
            body["stop"] = list(self.stop)
        if self.tools:
            body["tools"] = list(self.tools)
        if self.tool_choice is not None:
            body["tool_choice"] = self.tool_choice
        if self.response_format is not None:
            body["response_format"] = dict(self.response_format)
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.seed is not None:
            body["seed"] = self.seed
        if self.frequency_penalty is not None:
            body["frequency_penalty"] = self.frequency_penalty
        if self.presence_penalty is not None:
            body["presence_penalty"] = self.presence_penalty
        return body


def _timeout_of(payload: Mapping[str, Any], request: Mapping[str, Any]) -> float:
    """timeout_s：job 顶层优先，其次 request 内（C10a/C10b 两种形状都认）。"""
    value = payload.get("timeout_s")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = request.get("timeout_s")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return float(facts.CHANNEL_JOB_TIMEOUT_DEFAULT_S)
    seconds = float(value)
    return max(float(facts.CHANNEL_JOB_TIMEOUT_MIN_S),
               min(seconds, float(facts.CHANNEL_JOB_TIMEOUT_MAX_S)))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _request_of(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """取「请求体」这一段：``request``（C10a）→ ``requests``（历史）→ 顶层（spec 扁平形状）。"""
    for key in ("request", "requests"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return payload


def parse_job(payload: Any) -> ChannelJob | None:
    """平台 job → ChannelJob；缺 job_id / 请求体不合法返回 None（调用方回 failed）。

    兼容三种形状（只加不改）：

    * 派发信封 ``{"job": {...}, "server_time": ...}``（spec NodeChannelPollResponse）；
    * 请求体嵌套在 ``request``（C10a）或 ``requests``（C10c 早期）里；
    * spec 的扁平 ``NodeChannelJob``（model / messages 与 job_id 同级）。

    ``lease_expires_at`` 只用于平台侧重派，客户端忽略（本地执行按 timeout_s 截断）。
    """
    if not isinstance(payload, Mapping):
        return None
    envelope = payload.get("job")
    if isinstance(envelope, Mapping):
        payload = envelope
    request = _request_of(payload)
    job_id = str(payload.get("job_id") or request.get("job_id") or "").strip()
    if not job_id:
        return None
    model = str(request.get("model") or "").strip()
    messages = request.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        return None
    stop = request.get("stop")
    tools = request.get("tools")
    node_id = payload.get("node_id")
    return ChannelJob(
        job_id=job_id,
        node_id=str(node_id) if isinstance(node_id, str) and node_id else None,
        model=model,
        messages=messages,
        max_tokens=_int_or_none(request.get("max_tokens")),
        temperature=_float_or_none(request.get("temperature")),
        stop=[str(item) for item in stop] if isinstance(stop, list) else None,
        timeout_s=_timeout_of(payload, request),
        tools=tools if isinstance(tools, list) and tools else None,
        tool_choice=request.get("tool_choice"),
        response_format=(dict(fmt) if isinstance(fmt := request.get("response_format"),
                                                 Mapping) else None),
        top_p=_float_or_none(request.get("top_p")),
        seed=_int_or_none(request.get("seed")),
        frequency_penalty=_float_or_none(request.get("frequency_penalty")),
        presence_penalty=_float_or_none(request.get("presence_penalty")),
    )


# ─────────────────────────────────────────────────────────── 结果（wire 契约）
#: spec 1.2.9 ``NodeChannelResultRequest`` 字段（只发这些；多余的会被 M02 丢弃或 40001）
WIRE_FIELDS: tuple[str, ...] = ("job_id", "status", "content", "finish_reason",
                                "tokens_in", "tokens_out", "usage_source", "latency_ms",
                                "error_code", "error_class", "upstream_request_id")
#: ``usage_source`` 取值：C10b ``facts.USAGE_SOURCES`` = (estimated, actual, dual)
USAGE_SOURCE_ACTUAL = "actual"
USAGE_SOURCE_ESTIMATED = "estimated"

_LOCAL_ERROR_BY_STATUS: dict[int, tuple[int, str]] = {
    400: (facts.LOCAL_ERROR_CODES["invalid_request"], "invalid_request"),
    401: (facts.LOCAL_ERROR_CODES["unauthorized"], "unauthorized"),
    402: (facts.LOCAL_ERROR_CODES["quota_exceeded"], "quota_exceeded"),
    429: (facts.LOCAL_ERROR_CODES["rate_limited"], "rate_limited"),
    503: (facts.LOCAL_ERROR_CODES["no_upstream"], "no_upstream"),
    504: (facts.LOCAL_ERROR_CODES["upstream_timeout"], "upstream_timeout"),
}


def result_for_error(job_id: str, status: int, *, error_class: str | None = None,
                     latency_ms: int | None = None) -> dict[str, Any]:
    """本机 4xx/5xx / 网络失败 → ``status=failed`` 结果（wire 字段见 ``WIRE_FIELDS``）。

    * ``error_code`` 按 spec 发**字符串**（``"50903"``），不发整数；
    * 不发 ``node_id`` / ``duration_ms`` / ``completed_at``（契约无此字段）；
    * 不带上游正文（错误只按类别粗粒度描述）。
    """
    code, klass = _LOCAL_ERROR_BY_STATUS.get(int(status),
                                             (facts.LOCAL_ERROR_CODES["upstream_error"],
                                              "upstream_error"))
    if int(status) >= 500 and int(status) not in _LOCAL_ERROR_BY_STATUS:
        klass = "upstream_error"
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": "failed",
        "error_code": str(code),
        "error_class": error_class or klass,
    }
    if latency_ms is not None:
        payload["latency_ms"] = max(0, int(latency_ms))
    return payload


# ─────────────────────────────────────────────────────────── 客户端
class ChannelClient:
    """出站长轮询 + 本机执行 + 结果回传（单在途；可在 daemon 线程里跑）。"""

    def __init__(self, paths: Paths, config: ClientConfig, credentials: CredentialStore, *,
                 port: int, local_token: str, http: HttpClient | None = None,
                 chat_call: ChatCall | None = None, now: Callable[[], Any] = utcnow,
                 sleep: Callable[[float], Any] | None = None,
                 api: NodeApi | None = None) -> None:
        self.paths = paths
        self.config = config
        self.credentials = credentials
        self.port = int(port)
        self.local_token = local_token          # 只进 Authorization 头，绝不进日志
        self.http = http or HttpClient(timeout=float(facts.CHANNEL_POLL_TIMEOUT_S))
        self._now = now or utcnow
        self._sleep = sleep
        self.api = api or NodeApi(config.gateway_url,
                                  client_key=credentials.client_key(), http=self.http)
        self.chat_call: ChatCall = chat_call or self._default_chat_call
        self.local_url = ("http://" + facts.MIRROR_HOST + ":" + str(self.port)
                          + facts.LOCAL_API_PREFIX + "/chat/completions")
        self._state = facts.CHANNEL_STATUS_KINDS[0]
        self._jobs_done = 0
        self._jobs_failed = 0
        self._last_error: str | None = None
        self._last_job_id: str | None = None
        self._last_job_at: str | None = None
        self._lock = threading.Lock()

    # ── 观察面（status / 诊断；不含正文与令牌）
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": channel_enabled(self.config),
                    "state": self._state,
                    "port": self.port,
                    "jobs_done": self._jobs_done,
                    "jobs_failed": self._jobs_failed,
                    "last_job_id": self._last_job_id,
                    "last_job_at": self._last_job_at,
                    "last_error": self._last_error,
                    "wait_s": facts.CHANNEL_POLL_WAIT_S}

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    # ── 默认本机调用
    def _default_chat_call(self, body: dict[str, Any], timeout: float) -> Any:
        return self.http.request(
            "POST", self.local_url,
            headers={"Authorization": "Bearer " + self.local_token,
                     "Content-Type": "application/json"},
            json_body=body, timeout=timeout)

    def _wait(self, seconds: float, stop_event: threading.Event) -> None:
        """退避等待：默认可被 stop_event 唤醒；测试注入 sleep 避免真睡。"""
        if self._sleep is not None:
            self._sleep(seconds)
            return
        stop_event.wait(seconds)

    # ── 主循环
    def run(self, stop_event: threading.Event) -> None:
        """长轮询主循环（阻塞，直到 stop_event 或 401/403）。"""
        self._set_state("polling")
        event("channel.started", port=self.port,
              node_id=self.credentials.node_id(), wait_s=facts.CHANNEL_POLL_WAIT_S)
        backoff_index = 0
        try:
            while not stop_event.is_set():
                try:
                    job_payload = self.api.poll_channel(wait_s=facts.CHANNEL_POLL_WAIT_S)
                except (AuthError, ForbiddenError) as exc:
                    self._last_error = type(exc).__name__
                    self._set_state("stopping")
                    event("channel.auth_failed", level="warning",
                          error=type(exc).__name__, code=exc.code,
                          node_id=self.credentials.node_id())
                    return
                except DjbError as exc:                # 网络 / 限流 / 5xx：退避后继续
                    delay = facts.CHANNEL_BACKOFF_S[min(backoff_index,
                                                        len(facts.CHANNEL_BACKOFF_S) - 1)]
                    backoff_index += 1
                    self._last_error = type(exc).__name__
                    event("channel.poll_failed", level="warning",
                          error=type(exc).__name__, code=exc.code, retry_in_s=delay)
                    self._wait(float(delay), stop_event)
                    continue
                backoff_index = 0
                if job_payload is None:            # 204：无 job，立即再 poll
                    continue
                self._handle(job_payload, stop_event)
        except Exception as exc:                   # 防御：线程异常只留事件，不炸 daemon
            self._last_error = type(exc).__name__
            event("channel.thread_failed", level="warning", error=type(exc).__name__)
        finally:
            self._set_state("stopping")
            event("channel.stopped", port=self.port, jobs_done=self._jobs_done,
                  jobs_failed=self._jobs_failed)

    # ── 单个 job
    def _handle(self, payload: dict[str, Any], stop_event: threading.Event) -> None:
        job = parse_job(payload)
        if job is None:
            raw_id = payload.get("job_id") if isinstance(payload, Mapping) else None
            event("channel.job_invalid", level="warning",
                  job_id=str(raw_id or "")[:64])
            self._submit(result_for_error(str(raw_id or ""), 400,
                                          error_class="invalid_job"))
            self._jobs_failed += 1
            return
        started = time.monotonic()
        self._set_state("executing")
        self._last_job_id = job.job_id
        event("channel.job_received", job_id=job.job_id, node_id=job.node_id,
              model=job.model, timeout_s=job.timeout_s)
        result = self._execute(job)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._last_job_at = iso(self._now())        # 只进 status / 诊断，不上 wire
        self._submit(result)
        if result.get("status") == "completed":
            self._jobs_done += 1
            event("channel.job_done", job_id=job.job_id, status="completed",
                  duration_ms=duration_ms)
        else:
            self._jobs_failed += 1
            event("channel.job_done", job_id=job.job_id, status="failed",
                  error_class=result.get("error_class"), duration_ms=duration_ms)
        if not stop_event.is_set():
            self._set_state("polling")

    def _execute(self, job: ChannelJob) -> dict[str, Any]:
        """调本机镜像端点；成功映射 content/finish_reason/tokens_*，失败映射 error_code。"""
        body = job.body()
        started = time.monotonic()
        try:
            response = self.chat_call(body, job.timeout_s)
        except (TimeoutError, OSError):            # 注入替身直接抛原生超时/连接错误
            return result_for_error(job.job_id, 504, error_class="upstream_timeout",
                                    latency_ms=self._elapsed_ms(started))
        except DjbError as exc:
            status = 504 if getattr(exc, "reason", None) == "timeout" else 502
            return result_for_error(job.job_id, status,
                                    latency_ms=self._elapsed_ms(started))
        latency_ms = self._elapsed_ms(started)
        status = int(getattr(response, "status", 0) or 0)
        payload = response.json() if callable(getattr(response, "json", None)) else None
        if not (200 <= status < 300) or not isinstance(payload, dict):
            return result_for_error(job.job_id, status or 502, latency_ms=latency_ms)
        return self._result_from_chat(job, payload, latency_ms=latency_ms)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def _result_from_chat(self, job: ChannelJob, payload: dict[str, Any], *,
                          latency_ms: int | None = None) -> dict[str, Any]:
        """本机 200 响应 → ``status=completed`` 契约结果（tokens_in/out + usage_source）。

        * 上游给了 usage → ``tokens_in/tokens_out`` 为整数，``usage_source="actual"``；
        * 上游没给 usage → **不发** tokens_in/tokens_out（spec 里是 integer，发 null 会破坏
          类型），``usage_source="estimated"``；平台侧 ``int_or_none`` 取到的同样是空值。
        """
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(choice, Mapping):
            choice = {}
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        usage = payload.get("usage")
        tokens_in = tokens_out = None
        if isinstance(usage, Mapping):
            candidate_in = _int_or_none(usage.get("prompt_tokens"))
            candidate_out = _int_or_none(usage.get("completion_tokens"))
            if candidate_in is not None:
                tokens_in = max(0, candidate_in)
            if candidate_out is not None:
                tokens_out = max(0, candidate_out)
        result: dict[str, Any] = {
            "job_id": job.job_id,
            "status": "completed",
            "content": content if isinstance(content, str) else "",
            "finish_reason": choice.get("finish_reason"),
            "usage_source": (USAGE_SOURCE_ACTUAL if tokens_in is not None
                             or tokens_out is not None else USAGE_SOURCE_ESTIMATED),
        }
        if tokens_in is not None:
            result["tokens_in"] = tokens_in
        if tokens_out is not None:
            result["tokens_out"] = tokens_out
        if latency_ms is not None:
            result["latency_ms"] = max(0, int(latency_ms))
        return result

    # ── 结果回传（幂等键 = job_id；网络类失败重试一次）
    def _submit(self, payload: dict[str, Any]) -> None:
        attempts = max(1, int(facts.CHANNEL_RESULT_ATTEMPTS))
        last_error: DjbError | None = None
        for attempt in range(attempts):
            try:
                response = self.api.submit_channel_result(payload)
            except (NetworkError, RateLimitError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._wait(float(facts.CHANNEL_RESULT_RETRY_S),
                               threading.Event())
                continue
            except DjbError as exc:
                self._last_error = type(exc).__name__
                event("channel.result_failed", level="warning",
                      job_id=payload.get("job_id"),
                      error=type(exc).__name__, code=exc.code)
                return
            event("channel.result_submitted", job_id=payload.get("job_id"),
                  status=payload.get("status"),
                  duplicate=bool(response.get("duplicate")))
            return
        self._last_error = type(last_error).__name__ if last_error else "unknown"
        event("channel.result_failed", level="warning",
              job_id=payload.get("job_id"), error=self._last_error,
              attempts=attempts)
