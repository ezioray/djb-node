# SPDX-License-Identifier: Apache-2.0
"""本机镜像端点的上游协议适配（R4-3 接线测试）。

用一个极简假 Anthropic 上游，验证三件事：
1. ``protocol=anthropic-messages`` 时请求打到 ``/v1/messages``，头是 ``x-api-key`` +
   ``anthropic-version``，body 被转成 Anthropic 形态（system 顶层、max_tokens 必填）；
2. 上游 Anthropic 响应被转回 OpenAI ``chat.completion``（content / usage / finish_reason）；
3. 流式：Anthropic SSE → OpenAI ``chat.completion.chunk`` + ``[DONE]``；
4. ``protocol`` 缺省（openai-chat）时行为与改造前一致（回归）。
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from djb_node.server import Admission, LocalApiServer, RouteTarget

TOKEN = "t" * 40


class _FakeUpstream(BaseHTTPRequestHandler):
    """记录收到的请求；按路径回 OpenAI 或 Anthropic 形态。"""

    received: list[dict] = []
    stream_mode = False

    def log_message(self, *args):            # 静音
        pass

    def do_POST(self):                        # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        type(self).received.append({"path": self.path,
                                    "headers": {k.lower(): v for k, v in self.headers.items()},
                                    "body": body})
        if self.path.endswith("/v1/messages"):
            if type(self).stream_mode:
                self._anthropic_stream()
            else:
                self._anthropic_json()
            return
        self._openai_json()

    def _send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _anthropic_json(self) -> None:
        self._send_json({"id": "msg_1", "type": "message", "model": "claude-3-7-sonnet",
                         "content": [{"type": "text", "text": "hi from anthropic"}],
                         "stop_reason": "end_turn",
                         "usage": {"input_tokens": 9, "output_tokens": 4}})

    def _openai_json(self) -> None:
        self._send_json({"id": "chatcmpl-1", "object": "chat.completion",
                         "model": "mock-model",
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant",
                                                  "content": "hi from openai"}}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                   "total_tokens": 7}})

    def _anthropic_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            ("message_start", {"type": "message_start", "message": {
                "model": "claude-3-7-sonnet", "usage": {"input_tokens": 7}}}),
            ("content_block_delta", {"type": "content_block_delta",
                                     "delta": {"type": "text_delta", "text": "he"}}),
            ("content_block_delta", {"type": "content_block_delta",
                                     "delta": {"type": "text_delta", "text": "llo"}}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn"},
                               "usage": {"output_tokens": 2}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        for name, data in events:
            chunk = ("event: " + name + "\ndata: " + json.dumps(data) + "\n\n").encode()
            self.wfile.write(chunk)
            self.wfile.flush()


class _Runtime:
    """最小 runtime：单上游、可指定协议。"""

    def __init__(self, base_url: str, protocol: str):
        self.base_url = base_url
        self.protocol = protocol
        self.observed: list[dict] = []

    def health(self):
        return {"status": "ok"}

    def models(self):
        return {"object": "list", "data": []}

    def admit(self, model):
        return Admission(True)

    def route(self, model):
        return RouteTarget(base_url=self.base_url, model=model, access="A1",
                           upstream_id="u1", credential="sk-secret",
                           protocol=self.protocol)

    def observe(self, **fields):
        self.observed.append(fields)

    def begin_task(self):
        pass

    def end_task(self):
        pass


@pytest.fixture
def upstream():
    _FakeUpstream.received = []
    _FakeUpstream.stream_mode = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _local(base_url: str, protocol: str):
    runtime = _Runtime(base_url, protocol)
    server = LocalApiServer(runtime, token=TOKEN, port=0)
    server.bind()
    server.start()
    return server, runtime


def _post(port: int, body: dict) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode("utf-8")
    connection.request("POST", "/v1/chat/completions", body=payload,
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + TOKEN})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


_BODY = {"model": "mock-model", "max_tokens": 32, "temperature": 0.2,
         "messages": [{"role": "system", "content": "be brief"},
                      {"role": "user", "content": "hello"}],
         "tools": [{"type": "function",
                    "function": {"name": "f", "parameters": {"type": "object"}}}],
         "tool_choice": "auto"}


def test_anthropic_request_is_translated(upstream):
    url = "http://127.0.0.1:" + str(upstream.server_address[1])
    local, _runtime = _local(url, "anthropic-messages")
    try:
        status, data = _post(local.port, _BODY)
    finally:
        local.shutdown()
    assert status == 200
    sent = _FakeUpstream.received[-1]
    assert sent["path"] == "/v1/messages"
    assert sent["headers"].get("x-api-key") == "sk-secret"
    assert sent["headers"].get("anthropic-version") == "2023-06-01"
    assert "authorization" not in sent["headers"]
    body = sent["body"]
    assert body["system"] == "be brief"                       # system 提升
    assert body["max_tokens"] == 32
    assert body["messages"][0]["role"] == "user"
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert body["tool_choice"] == {"type": "auto"}


def test_anthropic_response_is_converted_to_openai(upstream):
    url = "http://127.0.0.1:" + str(upstream.server_address[1])
    local, runtime = _local(url, "anthropic-messages")
    try:
        status, data = _post(local.port, _BODY)
    finally:
        local.shutdown()
    payload = json.loads(data)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "hi from anthropic"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] == {"prompt_tokens": 9, "completion_tokens": 4,
                                "total_tokens": 13}
    assert runtime.observed and runtime.observed[0]["prompt_tokens"] == 9


def test_anthropic_stream_is_converted(upstream):
    _FakeUpstream.stream_mode = True
    url = "http://127.0.0.1:" + str(upstream.server_address[1])
    local, runtime = _local(url, "anthropic-messages")
    try:
        status, data = _post(local.port, dict(_BODY, stream=True))
    finally:
        local.shutdown()
    assert status == 200
    text = data.decode("utf-8")
    assert text.rstrip().endswith("data: [DONE]")
    chunks = [json.loads(line[len("data: "):]) for line in text.splitlines()
              if line.startswith("data: ") and not line.endswith("[DONE]")]
    merged = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert merged == "hello"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert runtime.observed and runtime.observed[-1]["completion_tokens"] == 2


def test_openai_protocol_unchanged(upstream):
    """缺省 openai-chat：路径/头/响应与改造前一致。"""
    url = "http://127.0.0.1:" + str(upstream.server_address[1])
    local, _runtime = _local(url, "openai-chat")
    try:
        status, data = _post(local.port, _BODY)
    finally:
        local.shutdown()
    sent = _FakeUpstream.received[-1]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"].get("authorization") == "Bearer sk-secret"
    assert "x-api-key" not in sent["headers"]
    assert sent["body"]["messages"][0]["role"] == "system"    # 原样透传
    payload = json.loads(data)
    assert payload["choices"][0]["message"]["content"] == "hi from openai"
