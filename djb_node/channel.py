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

job（平台 → 节点，字段按需，缺省有兜底）::

    {"job_id": "job-1", "node_id": "node-1", "timeout_s": 120,
     "requests": {"model": "gpt-4o-mini", "messages": [...],
                  "max_tokens": 512, "temperature": 0.7, "stop": ["\\n\\n"]}}

结果（节点 → 平台）::

    {"job_id": "job-1", "node_id": "node-1", "status": "succeeded",
     "content": "...", "finish_reason": "stop",
     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
     "duration_ms": 123, "completed_at": "2026-10-24T00:00:00Z"}
    {"job_id": "job-1", "node_id": "node-1", "status": "failed",
     "error_code": 50903, "error_class": "upstream_error", "duration_ms": 42,
     "completed_at": "..."}
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

__all__ = ["ChannelJob", "ChannelClient", "channel_enabled", "parse_job", "result_for_error"]

#: 本机调用签名：(body, timeout) -> HttpResponse 形状（status/headers/body/json()）
ChatCall = Callable[[dict[str, Any], float], Any]


def channel_enabled(config: ClientConfig | None) -> bool:
    """开关 ``client.channel.enabled``（默认关闭；唯一求值入口 config.feature_flag）。"""
    return bool(config is not None and feature_flag(config, facts.CHANNEL_FLAG))


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
        return body


def _timeout_of(raw: Mapping[str, Any]) -> float:
    value = raw.get("timeout_s")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return float(facts.CHANNEL_JOB_TIMEOUT_DEFAULT_S)
    seconds = float(value)
    return max(float(facts.CHANNEL_JOB_TIMEOUT_MIN_S),
               min(seconds, float(facts.CHANNEL_JOB_TIMEOUT_MAX_S)))


def parse_job(payload: Any) -> ChannelJob | None:
    """平台 job → ChannelJob；缺 job_id / 请求体不合法返回 None（调用方回 failed）。"""
    if not isinstance(payload, Mapping):
        return None
    job_id = str(payload.get("job_id") or "").strip()
    request = payload.get("requests")
    if not isinstance(request, Mapping):
        request = payload.get("request")
    if not job_id or not isinstance(request, Mapping):
        return None
    model = str(request.get("model") or "").strip()
    messages = request.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        return None
    max_tokens = request.get("max_tokens")
    temperature = request.get("temperature")
    stop = request.get("stop")
    node_id = payload.get("node_id")
    return ChannelJob(
        job_id=job_id,
        node_id=str(node_id) if isinstance(node_id, str) and node_id else None,
        model=model,
        messages=messages,
        max_tokens=int(max_tokens) if isinstance(max_tokens, int) and not isinstance(
            max_tokens, bool) else None,
        temperature=float(temperature) if isinstance(temperature, (int, float)) and not
        isinstance(temperature, bool) else None,
        stop=[str(item) for item in stop] if isinstance(stop, list) else None,
        timeout_s=_timeout_of(payload),
    )


# ─────────────────────────────────────────────────────────── 错误映射
_LOCAL_ERROR_BY_STATUS: dict[int, tuple[int, str]] = {
    400: (facts.LOCAL_ERROR_CODES["invalid_request"], "invalid_request"),
    401: (facts.LOCAL_ERROR_CODES["unauthorized"], "unauthorized"),
    402: (facts.LOCAL_ERROR_CODES["quota_exceeded"], "quota_exceeded"),
    429: (facts.LOCAL_ERROR_CODES["rate_limited"], "rate_limited"),
    503: (facts.LOCAL_ERROR_CODES["no_upstream"], "no_upstream"),
    504: (facts.LOCAL_ERROR_CODES["upstream_timeout"], "upstream_timeout"),
}


def result_for_error(job_id: str, status: int, *, node_id: str | None = None,
                     duration_ms: int = 0, error_class: str | None = None) -> dict[str, Any]:
    """本机 4xx/5xx / 网络失败 → failed 结果（只带错误码与错误类，不带上游正文）。"""
    code, klass = _LOCAL_ERROR_BY_STATUS.get(int(status),
                                             (facts.LOCAL_ERROR_CODES["upstream_error"],
                                              "upstream_error"))
    if status >= 500 and int(status) not in _LOCAL_ERROR_BY_STATUS:
        klass = "upstream_error"
    payload: dict[str, Any] = {
        "job_id": job_id, "status": "failed", "error_code": code,
        "error_class": error_class or klass, "duration_ms": int(duration_ms),
        "completed_at": iso(utcnow()),
    }
    if node_id:
        payload["node_id"] = node_id
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
        result["duration_ms"] = duration_ms
        result["completed_at"] = iso(self._now())    # 注入时钟（测试可控）
        self._submit(result)
        if result.get("status") == "succeeded":
            self._jobs_done += 1
            event("channel.job_done", job_id=job.job_id, status="succeeded",
                  duration_ms=duration_ms)
        else:
            self._jobs_failed += 1
            event("channel.job_done", job_id=job.job_id, status="failed",
                  error_class=result.get("error_class"), duration_ms=duration_ms)
        if not stop_event.is_set():
            self._set_state("polling")

    def _execute(self, job: ChannelJob) -> dict[str, Any]:
        """调本机镜像端点；成功映射 content/finish_reason/usage，失败映射 error_code。"""
        body = job.body()
        try:
            response = self.chat_call(body, job.timeout_s)
        except (TimeoutError, OSError):            # 注入替身直接抛原生超时/连接错误
            return result_for_error(job.job_id, 504, node_id=job.node_id,
                                    error_class="upstream_timeout")
        except DjbError as exc:
            status = 504 if getattr(exc, "reason", None) == "timeout" else 502
            return result_for_error(job.job_id, status, node_id=job.node_id)
        status = int(getattr(response, "status", 0) or 0)
        payload = response.json() if callable(getattr(response, "json", None)) else None
        if not (200 <= status < 300) or not isinstance(payload, dict):
            return result_for_error(job.job_id, status or 502, node_id=job.node_id)
        return self._result_from_chat(job, payload)

    def _result_from_chat(self, job: ChannelJob, payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(choice, Mapping):
            choice = {}
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        usage = payload.get("usage")
        clean_usage: dict[str, int] = {}
        if isinstance(usage, Mapping):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    clean_usage[key] = value
        return {
            "job_id": job.job_id,
            "node_id": job.node_id,
            "status": "succeeded",
            "content": content if isinstance(content, str) else "",
            "finish_reason": choice.get("finish_reason"),
            "usage": clean_usage,
        }

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
