# SPDX-License-Identifier: Apache-2.0
"""上游协议适配测试（openai-chat ↔ anthropic-messages）。"""
from __future__ import annotations

import pytest

from djb_node import upstream_protocols as up


# --------------------------------------------------------------------------- 归一
@pytest.mark.parametrize("raw,expected", [
    ("anthropic-messages", "anthropic-messages"),
    ("  anthropic-messages  ", "anthropic-messages"),
    ("ANTHROPIC-MESSAGES", "anthropic-messages"),
    ("openai-chat", "openai-chat"),
    ("", "openai-chat"),
    (None, "openai-chat"),
    ("gemini", "openai-chat"),          # 未知 → 回落，不报错
    (123, "openai-chat"),
])
def test_normalize_protocol(raw, expected):
    assert up.normalize_protocol(raw) == expected


def test_messages_path():
    assert up.messages_path("openai-chat") == "/v1/chat/completions"
    assert up.messages_path("anthropic-messages") == "/v1/messages"
    assert up.messages_path("anthropic-messages", "https://api.anthropic.com/v1") == "/messages"
    assert up.messages_path("anthropic-messages", "https://api.anthropic.com/v1/") == "/messages"


# --------------------------------------------------------------------------- 请求
def test_system_is_hoisted_and_messages_merged():
    body = {"model": "claude-3-7-sonnet", "max_tokens": 64,
            "messages": [{"role": "system", "content": "be brief"},
                         {"role": "user", "content": "a"},
                         {"role": "user", "content": "b"}]}
    out = up.to_anthropic_request(body)
    assert out["system"] == "be brief"
    assert out["model"] == "claude-3-7-sonnet"
    assert out["max_tokens"] == 64
    assert len(out["messages"]) == 1                       # 相邻 user 合并
    assert out["messages"][0]["content"] == [{"type": "text", "text": "a"},
                                             {"type": "text", "text": "b"}]


def test_max_tokens_defaults_when_missing():
    out = up.to_anthropic_request({"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert out["max_tokens"] == up.DEFAULT_MAX_TOKENS


def test_max_tokens_override():
    out = up.to_anthropic_request({"model": "m", "messages": [{"role": "user", "content": "x"}]},
                                  default_max_tokens=7)
    assert out["max_tokens"] == 7


def test_sampling_and_stop_mapped():
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
            "temperature": 0.3, "top_p": 0.9, "stop": ["\n\n", "END"], "stream": True}
    out = up.to_anthropic_request(body)
    assert out["temperature"] == 0.3 and out["top_p"] == 0.9
    assert out["stop_sequences"] == ["\n\n", "END"]
    assert out["stream"] is True


def test_tools_mapped_to_input_schema():
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {
                "name": "get_weather", "description": "d",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
            "tool_choice": "auto"}
    out = up.to_anthropic_request(body)
    assert out["tools"] == [{"name": "get_weather", "description": "d",
                             "input_schema": {"type": "object",
                                              "properties": {"city": {"type": "string"}}}}]
    assert out["tool_choice"] == {"type": "auto"}


@pytest.mark.parametrize("choice,expected", [
    ("auto", {"type": "auto"}),
    ("required", {"type": "any"}),
    ("none", None),
    ({"type": "function", "function": {"name": "f"}}, {"type": "tool", "name": "f"}),
])
def test_tool_choice_mapped(choice, expected):
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": choice}
    out = up.to_anthropic_request(body)
    assert out.get("tool_choice") == expected


def test_assistant_tool_calls_become_tool_use_blocks():
    body = {"model": "m", "messages": [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"SH"}'}}]},
        {"role": "user", "content": "thanks"}]}
    out = up.to_anthropic_request(body)
    block = out["messages"][1]["content"][0]
    assert block == {"type": "tool_use", "id": "call_1", "name": "get_weather",
                     "input": {"city": "SH"}}


def test_image_url_becomes_image_block():
    body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "what"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]}
    out = up.to_anthropic_request(body)
    assert out["messages"][0]["content"][1] == {"type": "image",
                                                "source": {"type": "url", "url": "https://x/y.png"}}


def test_data_url_becomes_base64_source():
    url = "data:image/png;base64,AAA"
    body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": url}]}]}
    out = up.to_anthropic_request(body)
    assert out["messages"][0]["content"][0]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "AAA"}


@pytest.mark.parametrize("body", [{}, {"model": "m"}, {"messages": []},
                                  {"model": "m", "messages": []}])
def test_bad_request_raises(body):
    with pytest.raises(up.ProtocolError):
        up.to_anthropic_request(body)


# --------------------------------------------------------------------------- 响应
def test_response_text_and_usage():
    payload = {"type": "message", "model": "claude-3-7-sonnet",
               "content": [{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}],
               "stop_reason": "end_turn", "usage": {"input_tokens": 11, "output_tokens": 5}}
    out = up.from_anthropic_response(payload)
    assert out["content"] == "hi there"
    assert out["finish_reason"] == "stop"
    assert out["tokens_in"] == 11 and out["tokens_out"] == 5
    assert out["usage_source"] == "actual"


def test_response_tool_use_mapped():
    payload = {"content": [{"type": "tool_use", "id": "tu_1", "name": "f",
                            "input": {"a": 1}}],
               "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 2}}
    out = up.from_anthropic_response(payload)
    assert out["finish_reason"] == "tool_calls"
    assert out["tool_calls"][0]["function"]["name"] == "f"
    assert out["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


def test_response_thinking_blocks_not_leaked():
    payload = {"content": [{"type": "thinking", "thinking": "secret"},
                           {"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
    assert up.from_anthropic_response(payload)["content"] == "ok"


def test_response_error_raises():
    with pytest.raises(up.ProtocolError):
        up.from_anthropic_response({"type": "error", "error": {"message": "overloaded"}})


@pytest.mark.parametrize("reason,expected", [
    ("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"),
    ("tool_use", "tool_calls"), (None, "stop"), ("weird", "stop")])
def test_stop_reason_map(reason, expected):
    assert up.map_stop_reason(reason) == expected


# --------------------------------------------------------------------------- SSE
def test_sse_text_stream_maps_to_chunks():
    events = [("message_start", {"type": "message_start", "message": {
        "model": "claude-3-7-sonnet", "usage": {"input_tokens": 3}}}),
        ("content_block_delta", {"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": "he"}}),
        ("content_block_delta", {"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": "llo"}}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"})]
    chunks = list(up.iter_anthropic_sse(events))
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "hello"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[0]["usage"]["prompt_tokens"] == 3
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["completion_tokens"] == 2


def test_sse_tool_use_stream():
    events = [("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "tool_use",
                                                         "id": "tu_1", "name": "f"}}),
              ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "input_json_delta",
                                                 "partial_json": '{"a":'}}),
              ("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": "tool_use"}, "usage": {}})]
    chunks = list(up.iter_anthropic_sse(events))
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "f"
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"a":'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_sse_error_raises():
    with pytest.raises(up.ProtocolError):
        list(up.iter_anthropic_sse([("error", {"type": "error",
                                               "error": {"message": "rate"}})]))


def test_sse_ignores_unknown_events():
    assert list(up.iter_anthropic_sse([("ping", {"type": "ping"}),
                                       ("message_stop", {"type": "message_stop"})])) == []
