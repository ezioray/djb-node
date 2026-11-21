# SPDX-License-Identifier: Apache-2.0
"""客户端模型探针 L2 / L3（M17 §4.5 / §5.1 客户端侧，C7）。

分级口径（M17 §4.5）：

* **L1**：`GET /models` —— 可达、延迟、模型是否在清单（实现在
  `upstream.probe_upstream`，本模块不重复）；
* **L2**：`POST /chat/completions` —— 合成 prompt、`max_tokens=1`、非流式，
  校验 HTTP 200 / `choices` / `finish_reason` / `usage` 是否存在；
* **L3**：同请求 + `stream=true` —— 逐行读 SSE，取最后一段 `usage`。

**协议感知**（CR-20261106 客户端 P2）：`probe_model` / `probe_models` 与
`upstream.probe_upstream` 都接一个**可选** `protocol`（缺省 ``openai-chat``，
与改前逐字一致）。传 ``anthropic-messages`` 时端点走 ``/messages``、鉴权走
``x-api-key`` + ``anthropic-version``、请求体与响应判定过
``upstream_protocols`` 的同一套转换 —— 与 ``server.upstream_request`` 口径一致，
避免"链接能通、探针不通"把上游判 rejected 并连累同上游其它模型。
L1（``GET /models``）的 URL 不随协议变化（Anthropic 也有 ``/v1/models``），
变的只有鉴权头。

隐私与安全（M17 §10 / T9 / H1）：

* prompt 是编译期合成常量（`facts.PROBE_PROMPT`），**不含用户正文**；
* 本模块只保留原子结论（布尔 + 整数计数 + 粗粒度 error_class），
  不读、不存、不回传上游响应正文与模型输出；
* 凭据只进 header（``Authorization: Bearer`` 或 ``x-api-key``），
  绝不进 URL / 日志 / 异常文案 / 返回值；
* 网络异常一律归一成「不可达 / 超时」这类粗粒度分类。

探针失败不抛异常：调用方（`UpstreamManager.probe`）按结论决定公告门控。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from . import facts
from . import upstream_protocols as up
from .httpclient import HttpClient, join_url
from .util import utcnow

_EPOCH_DATE = date(1970, 1, 1)


@dataclass
class ProbeOutcome:
    """单个模型的探针结论（**本地**；spec 1.2.7 的 NodeProbeReport 没有这些字段）。"""

    level: str = "L2"
    reachable: bool = False
    auth_ok: bool = False
    inference_ok: bool = False
    stream_ok: bool | None = None
    usage_present: bool = False
    latency_ms: int = 0
    error_class: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """本地存储 / 测试用视图（含 L2/L3 细节，**不上行**）。"""
        return {"level": self.level, "reachable": self.reachable,
                "auth_ok": self.auth_ok, "inference_ok": self.inference_ok,
                "stream_ok": self.stream_ok, "usage_present": self.usage_present,
                "latency_ms": self.latency_ms, "error_class": self.error_class,
                "token_usage": dict(self.token_usage)}


# ─────────────────────────────────────────────────────────── 请求体 / 解析
def chat_body(model: str, *, stream: bool = False) -> dict[str, Any]:
    """最小合成请求：单条合成 prompt + `max_tokens=1`（M17 §4.5）。"""
    return {"model": model,
            "messages": [{"role": "user", "content": facts.PROBE_PROMPT}],
            "max_tokens": facts.PROBE_MAX_TOKENS,
            "temperature": 0,
            "stream": bool(stream)}


def normalize_level(level: Any) -> str:
    """未知 / 缺省级别回落到 L2（tolerant reader：不因调用方脏参数崩）。"""
    text = str(level or "").strip().upper()
    return "L3" if text == "L3" else "L2"


def usage_from_payload(payload: Any) -> dict[str, int]:
    """提取 usage 的整数计数（丢弃非整数 / 负数 / 未知键）。"""
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def usage_from_sse_line(line: bytes) -> dict[str, int]:
    """解析一行 SSE `data:` 里的 usage；不是数据行 / 坏 JSON 返回空。"""
    try:
        text = line.decode("utf-8", errors="replace").strip()
    except (UnicodeDecodeError, AttributeError):             # pragma: no cover - 防御
        return {}
    if not text.startswith("data:"):
        return {}
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return {}
    try:
        return usage_from_payload(json.loads(data))
    except ValueError:
        return {}


def error_class_for(exc: BaseException) -> str:
    """异常 → 粗粒度分类（不含 URL / 凭据 / 上游正文）。"""
    from .errors import NetworkError
    if isinstance(exc, NetworkError):
        return str(exc.reason or "unreachable")
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    return "unreachable"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


# ─────────────────────────────────────────────────────────── 协议感知（M17 §4.5）
def probe_endpoint(protocol: str | None, base_url: str) -> str:
    """探针要打的**推理端点**（协议感知；缺省 = OpenAI 兼容，逐字沿用旧路径）。

    * ``openai-chat`` → ``{base}/v1/chat/completions``（base 以 ``/v1`` 结尾时不重复拼）；
    * ``anthropic-messages`` → ``{base}/messages``（base 以 ``/v1`` 结尾）或
      ``{base}/v1/messages``；口径与 ``upstream_protocols.messages_path`` /
      ``server.upstream_request`` 逐字一致（CR-20261106 客户端 P2）。

    旧行为的端点计算**没有改动**：走 `upstream.chat_endpoint`，保证缺省协议下逐字一致。
    """
    if up.is_anthropic(protocol):
        base = str(base_url or "").rstrip("/")
        return join_url(base, up.messages_path(protocol, base))
    # 延迟导入：upstream 在模块级导入本模块，这里反向导入会形成循环。
    from .upstream import chat_endpoint
    return chat_endpoint(base_url)


def request_body(model: str, *, stream: bool = False,
                 protocol: str | None = None) -> dict[str, Any]:
    """探针请求体：OpenAI 形态原样返回，anthropic 形态过一遍协议转换（纯函数）。"""
    body = chat_body(model, stream=stream)
    if up.is_anthropic(protocol):
        return up.to_anthropic_request(body)
    return body


def probe_model(base_url: str, model: str, *, credential: str | None = None,
                level: str | None = None, timeout: float | None = None,
                http: HttpClient | None = None,
                protocol: str | None = None) -> ProbeOutcome:
    """单个模型的最小推理探针（L2 非流式；L3 = 同请求 + `stream=true`）。

    ``protocol`` **可选**（缺省 = ``openai-chat``，与改前逐字一致）：传
    ``anthropic-messages`` 时端点走 ``/messages``、鉴权走 ``x-api-key`` +
    ``anthropic-version``、请求体与响应判定都过 ``upstream_protocols`` ——
    与 ``server.upstream_request`` 同一套转换，避免「链接能通、探针不通」把
    上游判成 rejected、连累同一上游的其它模型（CR-20261106 客户端 P2）。

    只回结论；不落 prompt、不落响应正文、不回传凭据。
    """
    chosen = normalize_level(level or facts.PROBE_DEFAULT_LEVEL)
    outcome = ProbeOutcome(level=chosen)
    client = http or HttpClient()
    seconds = float(timeout if timeout is not None else facts.PROBE_TIMEOUT_S)
    protocol_name = up.normalize_protocol(protocol)
    headers = up.auth_headers(protocol_name, credential)
    url = probe_endpoint(protocol_name, base_url)
    if chosen == "L3":
        return _probe_stream(client, url, model, headers, seconds, outcome,
                             protocol=protocol_name)
    return _probe_completion(client, url, model, headers, seconds, outcome,
                             protocol=protocol_name)


def _probe_completion(client: HttpClient, url: str, model: str,
                      headers: dict[str, str], timeout: float,
                      outcome: ProbeOutcome, *,
                      protocol: str = facts.DEFAULT_UPSTREAM_PROTOCOL) -> ProbeOutcome:
    started = time.monotonic()
    try:
        response = client.request("POST", url, headers=headers,
                                  json_body=request_body(model, stream=False,
                                                         protocol=protocol),
                                  timeout=timeout)
    except Exception as exc:
        outcome.error_class = error_class_for(exc)
        outcome.latency_ms = _elapsed_ms(started)
        return outcome
    outcome.reachable = True
    outcome.latency_ms = int(response.elapsed_ms or _elapsed_ms(started))
    status = int(response.status or 0)
    if status in (401, 403):
        outcome.error_class = "unauthorized"
        return outcome
    if status >= 400:
        outcome.error_class = "http_" + str(status)
        return outcome
    outcome.auth_ok = True
    payload = response.json()
    if not isinstance(payload, dict):
        outcome.error_class = "bad_json"
        return outcome
    if up.is_anthropic(protocol) and not _is_openai_payload(payload):
        return _judge_anthropic(payload, outcome)
    outcome.token_usage = usage_from_payload(payload)
    outcome.usage_present = bool(outcome.token_usage)
    choice = _first_choice(payload)
    finish_reason = choice.get("finish_reason") if choice else None
    outcome.inference_ok = isinstance(finish_reason, str) and bool(finish_reason)
    if not outcome.inference_ok:
        outcome.error_class = "inference_failed"
    return outcome


def _is_openai_payload(payload: dict[str, Any]) -> bool:
    """兼容垫片：声明 anthropic 但实际回 OpenAI 形态的网关，仍按 OpenAI 口径判定。"""
    return isinstance(payload.get("choices"), list)


def _judge_anthropic(payload: dict[str, Any], outcome: ProbeOutcome) -> ProbeOutcome:
    """Anthropic 响应判定：`stop_reason` 或非空正文有一个就算能推理（不读正文内容）。

    错误包成 200（``{"type":"error"}``）时 `from_anthropic_response` 抛
    :class:`upstream_protocols.ProtocolError` —— 照样只归成 ``inference_failed``，
    **不向上抛**（探针失败不抛异常是模块纪律）。
    """
    try:
        converted = up.from_anthropic_response(payload)
    except up.ProtocolError:
        outcome.error_class = "inference_failed"
        return outcome
    usage: dict[str, int] = {}
    for key, source in (("prompt_tokens", "tokens_in"),
                        ("completion_tokens", "tokens_out")):
        value = converted.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[key] = value
    if usage:
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get(
            "completion_tokens", 0)
    outcome.token_usage = usage
    outcome.usage_present = bool(usage)
    stop_reason = payload.get("stop_reason")
    has_text = bool(str(converted.get("content") or "").strip())
    outcome.inference_ok = (isinstance(stop_reason, str) and bool(stop_reason)) or has_text
    if not outcome.inference_ok:
        outcome.error_class = "inference_failed"
    return outcome


def _probe_stream(client: HttpClient, url: str, model: str,
                  headers: dict[str, str], timeout: float,
                  outcome: ProbeOutcome, *,
                  protocol: str = facts.DEFAULT_UPSTREAM_PROTOCOL) -> ProbeOutcome:
    anthropic = up.is_anthropic(protocol)
    started = time.monotonic()
    try:
        stream = client.open_stream("POST", url, headers=headers,
                                    json_body=request_body(model, stream=True,
                                                           protocol=protocol),
                                    timeout=timeout)
    except Exception as exc:
        outcome.error_class = error_class_for(exc)
        outcome.latency_ms = _elapsed_ms(started)
        return outcome
    outcome.reachable = True
    try:
        status = int(stream.status or 0)
        if status in (401, 403):
            outcome.error_class = "unauthorized"
            return outcome
        if status >= 400:
            outcome.error_class = "http_" + str(status)
            return outcome
        outcome.auth_ok = True
        saw_delta = False
        saw_done = False
        usage: dict[str, int] = {}
        event_name = ""
        try:
            for line in stream.lines():
                text = line.decode("utf-8", errors="replace").strip()
                if not text or text.startswith(":"):
                    continue
                if text.startswith("event:"):
                    # Anthropic 的事件名单独一行（`event: content_block_delta`），
                    # 下一行 `data:` 才是载荷；只记名字，不落盘。
                    event_name = text[6:].strip()
                    continue
                if not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if data == "[DONE]":
                    saw_done = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if anthropic:
                    # 复用同一套 Anthropic → OpenAI chunk 转换来判「有没有增量 + usage」，
                    # 不另写一份解析（事件名缺失时 `iter_anthropic_sse` 会看 data.type）。
                    try:
                        pieces = list(up.iter_anthropic_sse([(event_name, chunk)]))
                    except up.ProtocolError:
                        outcome.error_class = "stream_failed"
                        outcome.latency_ms = _elapsed_ms(started)
                        return outcome
                    for piece in pieces:
                        choice = _first_choice(piece)
                        if choice is not None and choice.get("delta"):
                            saw_delta = True
                        part = usage_from_payload(piece)
                        if part:
                            usage.update(part)
                    if event_name == "message_stop":
                        saw_done = True
                    continue
                if _first_choice(chunk) is not None:
                    saw_delta = True
                usage = usage_from_sse_line(line) or usage
        except Exception as exc:
            outcome.error_class = error_class_for(exc)
            outcome.latency_ms = _elapsed_ms(started)
            return outcome
    finally:
        stream.close()
    outcome.latency_ms = _elapsed_ms(started)
    outcome.token_usage = usage
    outcome.usage_present = bool(usage)
    # 流式可用的判定：收到增量 chunk，并且正常收尾（`[DONE]` / `message_stop`）
    # 或至少拿到了 usage。两个协议同一把尺子，不因协议不同放宽。
    outcome.stream_ok = bool(saw_delta and (saw_done or bool(usage)))
    outcome.inference_ok = outcome.stream_ok
    if not outcome.stream_ok:
        outcome.error_class = "stream_failed"
    return outcome


def _first_choice(payload: dict[str, Any]) -> dict[str, Any] | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    return first if isinstance(first, dict) else None


# ─────────────────────────────────────────────────────────── 批量 / 调度
def probe_models(base_url: str, models: Iterable[str], *, credential: str | None = None,
                 level: str | None = None, timeout: float | None = None,
                 http: HttpClient | None = None, deadline: datetime | None = None,
                 clock: Callable[[], datetime] | None = None,
                 fallback_l2: bool = True, protocol: str | None = None,
                 ) -> dict[str, ProbeOutcome]:
    """逐模型探针；`deadline` 到点即停（剩余模型留给下一轮，不算失败）。

    `fallback_l2`：L3（流式）失败时**降级**再跑一次 L2 —— 流式被中间层缓冲 /
    不支持 SSE 的端点仍然算「能推理」，只是记 `stream_ok=False`（C7 降级不丢验证）。

    `protocol` 可选（缺省 = `openai-chat`）：整批模型用同一协议，L3 降级重试也沿用，
    避免"L3 按 anthropic 发了、L2 又退回 OpenAI"这种半截切换。

    `clock` 可注入（测试用假时钟，不 sleep）。
    """
    now = clock or utcnow
    client = http or HttpClient()
    chosen = normalize_level(level or facts.PROBE_DEFAULT_LEVEL)
    base_timeout = float(timeout if timeout is not None else facts.PROBE_TIMEOUT_S)
    # 0.3.9 / D-RCA P1-a：**单模型预算切片**。0.3.8 只在每个模型开始前看一次 deadline，
    # 却把完整的 ``timeout``(20s) 交给**每个** ``probe_model`` ⇒ 一个慢模型可以吃掉整轮
    # 预算（5 模型最坏 ~100s，超过 30s 心跳上限；慢模型排第一时其余全部 ``not_probed``，
    # 2026-11-16 现场 qwen 排最后没饿死是运气）。现在剩余预算按「剩余模型数」均分，
    # 每个模型至少 ``PROBE_MIN_MODEL_TIMEOUT_S``（2s，L2 健康响应远小于此）；
    # 单个模型确实需要更久时只丢自己这一行，并进入 backlog（``not_probed``）等下一轮。
    # 注意：L3 失败后的 L2 降级重试沿用同一个切片，所以单模型最坏花 2 个切片；
    # 下一轮模型开始前会重新看 deadline，超预算的模型照样留给下一轮，不会无限拖延。
    ordered = [str(model) for model in models]
    outcomes: dict[str, ProbeOutcome] = {}
    for index, name in enumerate(ordered):
        attempt_timeout = base_timeout
        if deadline is not None:
            remaining_s = (deadline - now()).total_seconds()
            if remaining_s <= 0:
                break
            remaining_models = len(ordered) - index
            attempt_timeout = min(base_timeout,
                                  max(facts.PROBE_MIN_MODEL_TIMEOUT_S,
                                      remaining_s / remaining_models))
        outcome = probe_model(base_url, name, credential=credential, level=chosen,
                              timeout=attempt_timeout, http=client, protocol=protocol)
        if fallback_l2 and chosen == "L3" and not outcome.inference_ok:
            retry = probe_model(base_url, name, credential=credential, level="L2",
                                timeout=attempt_timeout, http=client, protocol=protocol)
            if retry.inference_ok:
                retry.stream_ok = outcome.stream_ok is True
                retry.error_class = None
                outcome = retry
        outcomes[name] = outcome
    return outcomes


def periodic_interval_s(model_count: int = 0, failures: int = 0) -> int:
    """到下一次日探针的秒数（CHG-20261121-PROBE-DAILY）。

    **语义已变**：修前是「周期复探间隔」——基础 7.5 min、按模型数缩放、按失败指数退避，
    上界钳到新鲜度一半（450 s）。现在只有「今天」和「明天」两个答案：
    每链接每天 1 轮，所以这里恒返回 :data:`facts.PROBE_DAILY_INTERVAL_S`（86400）。

    为什么把退避整段删掉而不是留个下限：额度是被**周期**烧掉的（450 s × 全模型 ≈ 2 000+
    请求/天，见 `changes/CHG-20261121-PROBE-QUOTA-BURN.md`）。退避在「分钟级」和「小时级」
    之间抖动，仍然是同一个量级；日探针的失败处理只有「当天补一次、然后等次日」
    （``facts.PROBE_DAILY_MAX_ATTEMPTS`` + ``PROBE_DAILY_RETRY_DELAY_S``），由 daemon 的
    调度器负责，不在本函数里做曲线。

    ``model_count`` / ``failures`` 保留在签名里只为了不惊动调用方与状态渲染
    （``djb status`` 会显示 ``failures`` 计数）；两者**不再影响**返回值 —— 票面要求
    「每链接每天 1 次」与「模型越多越稀疏」互斥，后者是旧额度模型下的产物。
    回滚：恢复修前的 ``DEFAULT * scale * 2**shift`` 曲线并把 CAP 改回 half-fresh。
    """
    del model_count, failures                  # 显式声明「入参不再参与调度」
    return int(facts.PROBE_DAILY_INTERVAL_S)


def day_index(moment: datetime) -> int:
    """``moment`` 所在 UTC 日的序号（1970-01-01 起算的天数）。

    日探针的「日期轮换」用它当游标：同一天内重试/重启游标不变 ⇒ 代表模型不变；
    跨天 +1 ⇒ 换下一个模型。用 UTC 日（不是本地日）是因为节点内部时钟全是 UTC
    （``util.utcnow``），时区只在渲染层处理。
    """
    return (moment.date() - _EPOCH_DATE).days


def seconds_of_day(moment: datetime) -> int:
    """``moment`` 在当天的秒数（0–86399）。"""
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def daily_offset_s(material: str, span: int = facts.PROBE_DAILY_JITTER_S) -> int:
    """从稳定材料派生 [0, ``span``) 的稳定偏移（秒）。

    用 ``sha256`` 而不是内置 ``hash()``：后者对 str 加了进程随机盐（``PYTHONHASHSEED``），
    重启就变 —— 而「重启不换时刻」是票面不变量。也不引 ``random``：测试要可复现，
    且同一份配置在两次部署之间应当落到同一个时刻（可预测地分散，而不是每次重抽）。
    """
    limit = max(1, int(span))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % limit


def daily_slot_offset(moment: datetime, material: str) -> int:
    """该链接**固定的每天探针时刻**（当天的秒数）= 首启时刻 + 0–30 min 稳定抖动。

    首启那一刻决定之后每天的时刻（``PROBE_DAILY_JITTER_S`` 只用来把同一批次安装的
    客户端摊开，避免同一把上游 key 在同一分钟被打成尖峰）；落盘后不再重算，
    所以「当天固定、下一天同一时刻」与「重启不换时刻」同时成立。
    """
    jitter = daily_offset_s(material, facts.PROBE_DAILY_JITTER_S)
    return (seconds_of_day(moment) + jitter) % (24 * 3600)


def next_daily_at(moment: datetime, offset_s: int, *,
                  min_gap_s: float | None = None) -> datetime:
    """下一个 ≥ ``moment`` 且满足最小间隔的「当天 ``offset_s`` 秒」时刻。

    * 严格**晚于** ``moment``（等于也不算到点，避免刚探完又立刻重排）；
    * 与 ``moment`` 的间隔不得小于 ``min_gap_s``（默认 ``PROBE_DAILY_MIN_GAP_S`` = 20 h）：
      首启时当天槽位可能只差几分钟，本下限把计划轮推到次日 ⇒ 当日只有一轮；
    * 跨度上限由调用方保证：槽位 = 首启时刻 ± 抖动，所以相邻两轮间隔
      ∈ (24 h − 30 min, 24 h + 30 min) < ``PROBE_FRESH_S``(25 h)。
    """
    gap = facts.PROBE_DAILY_MIN_GAP_S if min_gap_s is None else float(min_gap_s)
    candidate = moment.replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(seconds=int(offset_s) % (24 * 3600))
    step = timedelta(days=1)
    if candidate <= moment:
        candidate += step
    while (candidate - moment).total_seconds() < gap:
        candidate += step
    return candidate


def pick_daily_model(models: Iterable[str], day: int) -> str | None:
    """按日期轮换取**一个**代表模型（票面「每轮每链接只探 1 个模型」）。

    排序后再取模：同一份清单在同一 ``day`` 上永远选出同一个模型（重启/重试不换），
    跨天 +1 换下一个。输入为空返回 ``None``（调用方据此跳过该链接，不算失败）。
    """
    ordered = sorted({str(model) for model in models if str(model)})
    if not ordered:
        return None
    return ordered[int(day) % len(ordered)]
