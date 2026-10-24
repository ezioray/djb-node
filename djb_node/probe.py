# SPDX-License-Identifier: Apache-2.0
"""客户端模型探针 L2 / L3（M17 §4.5 / §5.1 客户端侧，C7）。

分级口径（M17 §4.5）：

* **L1**：`GET /models` —— 可达、延迟、模型是否在清单（实现在
  `upstream.probe_upstream`，本模块不重复）；
* **L2**：`POST /chat/completions` —— 合成 prompt、`max_tokens=1`、非流式，
  校验 HTTP 200 / `choices` / `finish_reason` / `usage` 是否存在；
* **L3**：同请求 + `stream=true` —— 逐行读 SSE，取最后一段 `usage`。

隐私与安全（M17 §10 / T9 / H1）：

* prompt 是编译期合成常量（`facts.PROBE_PROMPT`），**不含用户正文**；
* 本模块只保留原子结论（布尔 + 整数计数 + 粗粒度 error_class），
  不读、不存、不回传上游响应正文与模型输出；
* 凭据只进 `Authorization` 头，绝不进 URL / 日志 / 异常文案 / 返回值；
* 网络异常一律归一成「不可达 / 超时」这类粗粒度分类。

探针失败不抛异常：调用方（`UpstreamManager.probe`）按结论决定公告门控。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from . import facts
from .httpclient import HttpClient
from .util import utcnow


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


# ─────────────────────────────────────────────────────────── L2 / L3
def probe_model(base_url: str, model: str, *, credential: str | None = None,
                level: str | None = None, timeout: float | None = None,
                http: HttpClient | None = None) -> ProbeOutcome:
    """单个模型的最小推理探针（L2 非流式；L3 = 同请求 + `stream=true`）。

    只回结论；不落 prompt、不落响应正文、不回传凭据。
    """
    chosen = normalize_level(level or facts.PROBE_DEFAULT_LEVEL)
    outcome = ProbeOutcome(level=chosen)
    client = http or HttpClient()
    seconds = float(timeout if timeout is not None else facts.PROBE_TIMEOUT_S)
    headers = {"Authorization": "Bearer " + credential} if credential else {}
    # 延迟导入：upstream 在模块级导入本模块，这里反向导入会形成循环。
    from .upstream import chat_endpoint
    url = chat_endpoint(base_url)
    if chosen == "L3":
        return _probe_stream(client, url, model, headers, seconds, outcome)
    return _probe_completion(client, url, model, headers, seconds, outcome)


def _probe_completion(client: HttpClient, url: str, model: str,
                      headers: dict[str, str], timeout: float,
                      outcome: ProbeOutcome) -> ProbeOutcome:
    started = time.monotonic()
    try:
        response = client.request("POST", url, headers=headers,
                                  json_body=chat_body(model, stream=False),
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
    outcome.token_usage = usage_from_payload(payload)
    outcome.usage_present = bool(outcome.token_usage)
    choice = _first_choice(payload)
    finish_reason = choice.get("finish_reason") if choice else None
    outcome.inference_ok = isinstance(finish_reason, str) and bool(finish_reason)
    if not outcome.inference_ok:
        outcome.error_class = "inference_failed"
    return outcome


def _probe_stream(client: HttpClient, url: str, model: str,
                  headers: dict[str, str], timeout: float,
                  outcome: ProbeOutcome) -> ProbeOutcome:
    started = time.monotonic()
    try:
        stream = client.open_stream("POST", url, headers=headers,
                                    json_body=chat_body(model, stream=True),
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
        try:
            for line in stream.lines():
                text = line.decode("utf-8", errors="replace").strip()
                if not text or text.startswith(":"):
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
    # 流式可用的判定：收到增量 chunk，并且正常收尾（[DONE]）或至少拿到了 usage。
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
                 fallback_l2: bool = True) -> dict[str, ProbeOutcome]:
    """逐模型探针；`deadline` 到点即停（剩余模型留给下一轮，不算失败）。

    `fallback_l2`：L3（流式）失败时**降级**再跑一次 L2 —— 流式被中间层缓冲 /
    不支持 SSE 的端点仍然算「能推理」，只是记 `stream_ok=False`（C7 降级不丢验证）。

    `clock` 可注入（测试用假时钟，不 sleep）。
    """
    now = clock or utcnow
    client = http or HttpClient()
    chosen = normalize_level(level or facts.PROBE_DEFAULT_LEVEL)
    outcomes: dict[str, ProbeOutcome] = {}
    for model in models:
        if deadline is not None and now() >= deadline:
            break
        name = str(model)
        outcome = probe_model(base_url, name, credential=credential, level=chosen,
                              timeout=timeout, http=client)
        if fallback_l2 and chosen == "L3" and not outcome.inference_ok:
            retry = probe_model(base_url, name, credential=credential, level="L2",
                                timeout=timeout, http=client)
            if retry.inference_ok:
                retry.stream_ok = outcome.stream_ok is True
                retry.error_class = None
                outcome = retry
        outcomes[name] = outcome
    return outcomes


def periodic_interval_s(model_count: int, failures: int) -> int:
    """周期复探间隔（秒）：默认 10 min；模型越多越稀疏；连续失败指数退避。"""
    count = max(0, int(model_count or 0))
    scale = 1 + count // max(1, int(facts.PROBE_MODELS_PER_STEP))
    shift = min(max(0, int(failures or 0)), int(facts.PROBE_MAX_BACKOFF_SHIFT))
    return int(min(facts.PROBE_PERIODIC_MAX_S,
                   facts.PROBE_PERIODIC_DEFAULT_S * scale * (2 ** shift)))
