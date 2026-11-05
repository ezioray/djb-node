# SPDX-License-Identifier: Apache-2.0
"""上游协议适配：``openai-chat`` ↔ ``anthropic-messages``。

背景
----
平台内部（``NodeCall`` / ``NodeChannelJob`` / ``NodeResult`` / 本机镜像端点）**只认
OpenAI 兼容形态**。但供给方的"请求链接"实际只有两大类：

* ``openai-chat``      —— ``POST {base}/v1/chat/completions``
* ``anthropic-messages`` —— ``POST {base}/v1/messages``（官方 Anthropic；请求体 / 响应 /
  SSE 事件名 / 结束原因 / 用量字段全都不一样）

所以协议转换**必须发生在本机**（客户端侧），平台侧不感知上游协议。本模块是纯函数集合：
不读环境、不发网络、不记录正文，便于单测与审计。

约定
----
* 只做「OpenAI 请求体 → Anthropic 请求体」与「Anthropic 响应 → NodeResult 字段」两向转换。
* 未知字段一律**丢弃**（Anthropic 会 400），但不因未知字段报错。
* 缺 ``max_tokens`` 时用 ``default_max_tokens`` 补齐（Anthropic 必填）。
* ``tool_use`` / ``tool_result`` 双向映射；不支持块类型原样透传为文本（不静默丢内容）。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, Mapping

#: Anthropic 要求的版本头值（``anthropic-version``）
ANTHROPIC_VERSION = "2023-06-01"
#: Anthropic 原生接口路径（相对 base_url，base 通常已含 ``/v1`` 时只拼 ``/messages``）
ANTHROPIC_MESSAGES_PATH = "/v1/messages"
#: 支持的协议标识（与 ``facts.DEFAULT_UPSTREAM_PROTOCOL`` 同一命名风格）
PROTOCOLS: tuple[str, ...] = ("openai-chat", "anthropic-messages")
#: Anthropic 缺省 max_tokens（其 API 必填；OpenAI 侧可省略）
DEFAULT_MAX_TOKENS = 1024

_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


class ProtocolError(ValueError):
    """协议转换失败（请求体不合法 / 无法映射）。调用方应转成本地 400。"""


def normalize_protocol(value: Any) -> str:
    """协议名归一：未知 / 空 → 回落到 ``openai-chat``（tolerant reader）。"""
    text = str(value or "").strip().lower()
    return text if text in PROTOCOLS else PROTOCOLS[0]


def is_anthropic(value: Any) -> bool:
    return normalize_protocol(value) == "anthropic-messages"


def messages_path(protocol: Any, base_url: str = "") -> str:
    """上游 messages 路径：Anthropic 原生用 ``/v1/messages``，OpenAI 兼容用 chat/completions。"""
    if not is_anthropic(protocol):
        return "/v1/chat/completions"
    base = str(base_url or "").rstrip("/")
    return "/messages" if base.endswith("/v1") else ANTHROPIC_MESSAGES_PATH


def auth_headers(protocol: Any, credential: str | None) -> dict[str, str]:
    """按协议给出上游**鉴权头**（探针与请求链接共用同一口径）。

    * ``anthropic-messages``：``x-api-key`` + ``anthropic-version``（Anthropic 官方要求）；
    * ``openai-chat``：``Authorization: Bearer <key>``（旧行为，逐字不变）。

    凭据**只进 header**：不进 URL、不进日志、不进返回值（M17 §10 / H1）。
    无凭据时返回空字典（本地未鉴权端点也要能探）。

    别和 ``relay.auth_headers(profile, credential)`` 混用：那个按中转站
    ``auth_scheme`` / ``header_name`` / ``auth_prefix`` 自定义，答的是「某个上游怎么鉴权」；
    本函数是「某个**协议**的默认鉴权头」，两者不是一回事。
    """
    if not credential:
        return {}
    if is_anthropic(protocol):
        return {"x-api-key": credential, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": "Bearer " + credential}


# --------------------------------------------------------------------------- 请求
def _split_system(messages: Iterable[Mapping[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """OpenAI messages → (system 文本, Anthropic messages)。

    Anthropic 的 ``system`` 是**顶层字段**，且 messages 只允许 user / assistant。
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for item in messages:
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content")
        if role in ("system", "developer"):
            text = _content_to_text(content)
            if text:
                system_parts.append(text)
            continue
        if role not in ("user", "assistant"):
            role = "user"                     # 未知角色不丢内容，降级为 user
        blocks = _openai_content_to_blocks(content)
        if role == "assistant":
            calls = [c for c in (item.get("tool_calls") or [])]
            if calls and not _has_text(blocks):
                blocks = []                       # 纯工具调用的 assistant 轮不塞空 text 块
            for call in calls:
                blocks.append(_openai_tool_call_to_block(call))
        if not blocks:
            blocks = [{"type": "text", "text": ""}]
        converted.append({"role": role, "content": blocks})
    return ("\n\n".join(system_parts) if system_parts else None), _merge_adjacent(converted)


def _has_text(blocks: list[dict[str, Any]]) -> bool:
    return any(block.get("type") == "text" and str(block.get("text") or "").strip()
               for block in blocks)


def _merge_adjacent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic 要求 user/assistant 交替；相邻同角色合并成一条。"""
    merged: list[dict[str, Any]] = []
    for item in messages:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["content"].extend(item["content"])
        else:
            merged.append({"role": item["role"], "content": list(item["content"])})
    return merged


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return "" if content is None else str(content)


def _openai_content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI content（字符串或多模态数组）→ Anthropic content blocks。"""
    if content is None:
        return [{"type": "text", "text": ""}]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": str(item.get("text") or "")})
        elif kind == "image_url":
            url = item.get("image_url")
            url = url.get("url") if isinstance(url, Mapping) else url
            blocks.append({"type": "image", "source": _image_source(url)})
        elif kind == "tool_result":
            blocks.append({"type": "tool_result",
                           "tool_use_id": str(item.get("tool_use_id") or ""),
                           "content": _content_to_text(item.get("content"))})
        else:                                  # 未知块 → 不丢，降级为文本
            blocks.append({"type": "text", "text": _content_to_text(item)})
    return blocks or [{"type": "text", "text": ""}]


def _image_source(url: Any) -> dict[str, Any]:
    """``data:`` URL → base64 source；http(s) → url source。"""
    text = str(url or "")
    if text.startswith("data:") and ";base64," in text:
        media, _, data = text.partition(";base64,")
        return {"type": "base64", "media_type": media[len("data:"):] or "image/png",
                "data": data}
    return {"type": "url", "url": text}


def _openai_tool_call_to_block(call: Any) -> dict[str, Any]:
    if not isinstance(call, Mapping):
        return {"type": "text", "text": str(call)}
    fn = call.get("function") or {}
    raw = fn.get("arguments") if isinstance(fn, Mapping) else None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except ValueError:
        parsed = {"_raw": raw}
    return {"type": "tool_use", "id": str(call.get("id") or ""),
            "name": str(fn.get("name") or "") if isinstance(fn, Mapping) else "",
            "input": parsed if isinstance(parsed, Mapping) else {"value": parsed}}


def _tools_to_anthropic(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(tools, Iterable) or isinstance(tools, (str, bytes, Mapping)):
        return out
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        fn = tool.get("function") if tool.get("type") in (None, "function") else None
        source = fn if isinstance(fn, Mapping) else tool
        name = str(source.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name,
                    "description": str(source.get("description") or ""),
                    "input_schema": source.get("parameters")
                    or source.get("input_schema") or {"type": "object", "properties": {}}})
    return out


def _tool_choice_to_anthropic(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        text = choice.strip().lower()
        if text in ("auto",):
            return {"type": "auto"}
        if text in ("required", "any"):
            return {"type": "any"}
        if text in ("none",):
            return None                          # Anthropic 无 none → 不下发（不表示禁止）
        return {"type": "tool", "name": choice}
    if isinstance(choice, Mapping):
        if choice.get("type") == "function":
            fn = choice.get("function") or {}
            name = fn.get("name") if isinstance(fn, Mapping) else None
            return {"type": "tool", "name": str(name)} if name else {"type": "auto"}
        if choice.get("type") in ("auto", "any", "tool"):
            return dict(choice)
    return None


def to_anthropic_request(body: Mapping[str, Any], *,
                          default_max_tokens: int = DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """OpenAI 兼容请求体 → Anthropic ``/v1/messages`` 请求体。

    不修改入参；不做校验失败的静默修正（缺 model / messages 抛 :class:`ProtocolError`）。
    """
    if not isinstance(body, Mapping):
        raise ProtocolError("请求体必须是对象")
    model = str(body.get("model") or "").strip()
    if not model:
        raise ProtocolError("缺 model")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProtocolError("缺 messages")

    system, converted = _split_system(messages)
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        max_tokens = int(default_max_tokens)

    out: dict[str, Any] = {"model": model, "messages": converted, "max_tokens": max_tokens}
    if system:
        out["system"] = system
    for key in ("temperature", "top_p"):
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    stop = body.get("stop")
    if isinstance(stop, str):
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [str(item) for item in stop]
    if body.get("stream"):
        out["stream"] = True
    tools = _tools_to_anthropic(body.get("tools"))
    if tools:
        out["tools"] = tools
        choice = _tool_choice_to_anthropic(body.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice
    return out


# --------------------------------------------------------------------------- 响应
def _blocks_to_content(blocks: Any) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic content blocks → (文本, OpenAI tool_calls)。"""
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    if not isinstance(blocks, list):
        return "", calls
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            calls.append({"id": str(block.get("id") or ""), "type": "function",
                          "function": {"name": str(block.get("name") or ""),
                                       "arguments": json.dumps(block.get("input") or {},
                                                               ensure_ascii=False)}})
        elif kind == "thinking":
            continue                          # 思考块不外泄到正文（与平台口径一致）
        else:
            texts.append(_content_to_text(block))
    return "".join(texts), calls


def map_stop_reason(reason: Any) -> str:
    return _STOP_REASON_MAP.get(str(reason or "").strip(), "stop")


def from_anthropic_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Anthropic 响应 → ``NodeResult`` 字段字典（content/finish_reason/tokens_*/model）。"""
    if not isinstance(payload, Mapping):
        raise ProtocolError("响应必须是对象")
    if payload.get("type") == "error" or isinstance(payload.get("error"), Mapping):
        error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
        raise ProtocolError(str(error.get("message") or "上游返回 error"))
    content, calls = _blocks_to_content(payload.get("content"))
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    result: dict[str, Any] = {
        "content": content,
        "finish_reason": map_stop_reason(payload.get("stop_reason")),
        "model": payload.get("model"),
        "tokens_in": _as_int(usage.get("input_tokens")),
        "tokens_out": _as_int(usage.get("output_tokens")),
        "usage_source": "actual",
    }
    if calls:
        result["tool_calls"] = calls
    return result


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- SSE
def iter_anthropic_sse(events: Iterable[tuple[str | None, Any]]) -> Iterator[dict[str, Any]]:
    """Anthropic SSE ``(event_name, data)`` 序列 → OpenAI ``chat.completion.chunk`` 字典流。

    ``events`` 由调用方解出（本模块不解析 ``data:`` 行，避免与传输层耦合）。
    """
    for name, data in events:
        if not isinstance(data, Mapping):
            continue
        kind = str(name or data.get("type") or "")
        if kind == "message_start":
            message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), Mapping) else {}
            yield _chunk(message.get("model"), delta_role=True,
                         tokens_in=_as_int(usage.get("input_tokens")))
        elif kind == "content_block_start":
            block = data.get("content_block") if isinstance(data.get("content_block"),
                                                            Mapping) else {}
            if block.get("type") == "tool_use":
                yield _chunk(None, tool_call={"index": _as_int(data.get("index")) or 0,
                                              "id": str(block.get("id") or ""),
                                              "name": str(block.get("name") or ""),
                                              "arguments": ""})
        elif kind == "content_block_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), Mapping) else {}
            if delta.get("type") == "text_delta":
                yield _chunk(None, delta_text=str(delta.get("text") or ""))
            elif delta.get("type") == "input_json_delta":
                yield _chunk(None, tool_call={"index": 0, "arguments":
                                              str(delta.get("partial_json") or "")})
        elif kind == "message_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), Mapping) else {}
            usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
            yield _chunk(None, finish_reason=map_stop_reason(delta.get("stop_reason")),
                         tokens_out=_as_int(usage.get("output_tokens")))
        elif kind == "error":
            error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
            raise ProtocolError(str(error.get("message") or "SSE error"))
        # message_stop / ping / content_block_stop：无需下发


def _chunk(model: Any = None, *, delta_role: bool = False, delta_text: str | None = None,
           tool_call: Mapping[str, Any] | None = None, finish_reason: str | None = None,
           tokens_in: int | None = None, tokens_out: int | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if delta_role:
        delta["role"] = "assistant"
    if delta_text is not None:
        delta["content"] = delta_text
    if tool_call is not None:
        entry: dict[str, Any] = {"index": tool_call.get("index", 0),
                                 "function": {"arguments": tool_call.get("arguments", "")}}
        if tool_call.get("id"):
            entry["id"] = tool_call["id"]
            entry["type"] = "function"
        if tool_call.get("name"):
            entry["function"]["name"] = tool_call["name"]
        delta["tool_calls"] = [entry]
    chunk: dict[str, Any] = {"object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    if model:
        chunk["model"] = model
    usage = {}
    if tokens_in is not None:
        usage["prompt_tokens"] = tokens_in
    if tokens_out is not None:
        usage["completion_tokens"] = tokens_out
    if usage:
        chunk["usage"] = usage
    return chunk


__all__ = ["ANTHROPIC_VERSION", "ANTHROPIC_MESSAGES_PATH", "PROTOCOLS", "DEFAULT_MAX_TOKENS",
           "ProtocolError", "normalize_protocol", "is_anthropic", "messages_path",
           "auth_headers", "to_anthropic_request", "from_anthropic_response",
           "map_stop_reason", "iter_anthropic_sse"]
