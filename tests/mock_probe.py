# SPDX-License-Identifier: Apache-2.0
"""C7 探针测试用的最小 OpenAI 兼容上游桩（纯 http.server，无外网、无第三方依赖）。

刻意**不复用** `tests/mock_m02.py`：那个桩服务的是 M02/M05 契约定型用例，
C7 需要「坏 JSON / 无 usage / 不支持 SSE / 流式无 usage」这类畸形上游，
放在独立文件里可以避免与其它 seat 的 mock 改动互相踩。

只用于断言探针行为：记录收到的请求（method/path/headers/body），
不记录也不回显任何用户正文（探针本来就只有合成 prompt）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class UpstreamStub:
    """可配置的 OpenAI 兼容上游。

    非流式 `chat_mode`：``json``（正常）/ ``no_usage`` / ``no_finish`` /
    ``no_choices`` / ``bad_json`` / ``error_envelope``。
    流式 `stream_mode`：``sse``（正常，末段带 usage）/ ``sse_no_usage`` /
    ``json``（不支持 SSE，直接回 JSON）/ ``bad_chunk`` / ``no_done``。
    """

    def __init__(self, models: tuple[str, ...] | list[str] = ("probe-model",)) -> None:
        self.models: list[str] = list(models)
        self.models_status = 200
        self.chat_status = 200
        self.chat_mode = "json"
        self.stream_mode = "sse"
        self.usage: dict[str, int] = {"prompt_tokens": 1, "completion_tokens": 1,
                                      "total_tokens": 2}
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ── 生命周期
    def start(self) -> "UpstreamStub":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or "0")
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return {}

            def _json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _raw(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse_start(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

            def _sse(self, payload: Any) -> None:
                self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False)
                                  + "\n\n").encode("utf-8"))
                self.wfile.flush()

            def do_GET(self) -> None:                      # noqa: N802
                path = self.path.split("?", 1)[0]
                outer.requests.append({"method": "GET", "path": path,
                                       "headers": dict(self.headers)})
                if path == "/v1/models":
                    if outer.models_status != 200:
                        self._json(outer.models_status, {"error": {"code": 50001}})
                        return
                    self._json(200, {"object": "list",
                                     "data": [{"id": model, "object": "model"}
                                              for model in outer.models]})
                    return
                self._json(404, {"error": {"code": 40401, "message": "not found"}})

            def do_POST(self) -> None:                     # noqa: N802
                path = self.path.split("?", 1)[0]
                body = self._body()
                outer.requests.append({"method": "POST", "path": path,
                                       "headers": dict(self.headers), "body": body})
                if path != "/v1/chat/completions":
                    self._json(404, {"error": {"code": 40401, "message": "not found"}})
                    return
                if outer.chat_status != 200:
                    self._json(outer.chat_status,
                               {"error": {"type": "upstream_error", "code": 50903,
                                          "message": "stub upstream"}})
                    return
                if body.get("stream"):
                    self._stream(body)
                    return
                self._completion(body)

            def _completion(self, body: dict[str, Any]) -> None:
                mode = outer.chat_mode
                if mode == "bad_json":
                    self._raw(b"{not json")
                    return
                if mode == "error_envelope":
                    self._json(200, {"error": {"code": 50903, "message": "bad"}})
                    return
                if mode == "no_choices":
                    self._json(200, {"id": "c1", "object": "chat.completion"})
                    return
                choice: dict[str, Any] = {"index": 0,
                                          "message": {"role": "assistant",
                                                      "content": "ok"}}
                if mode != "no_finish":
                    choice["finish_reason"] = "stop"
                payload: dict[str, Any] = {"id": "c1", "object": "chat.completion",
                                           "model": body.get("model"),
                                           "choices": [choice]}
                if mode != "no_usage":
                    payload["usage"] = dict(outer.usage)
                self._json(200, payload)

            def _stream(self, body: dict[str, Any]) -> None:
                mode = outer.stream_mode
                if mode == "json":
                    # 不支持 SSE 的上游：回普通 JSON（探针应判 stream_failed 并降级 L2）
                    self._completion(body)
                    return
                self._sse_start()
                if mode == "bad_chunk":
                    self.wfile.write(b"data: {not json\n\n")
                    self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self._sse({"id": "c1", "object": "chat.completion.chunk",
                           "choices": [{"index": 0, "delta": {"content": "o"}}]})
                self._sse({"id": "c1", "object": "chat.completion.chunk",
                           "choices": [{"index": 0, "delta": {"content": "k"},
                                        "finish_reason": None}]})
                if mode == "sse":
                    self._sse({"id": "c1", "object": "chat.completion.chunk",
                               "choices": [], "usage": dict(outer.usage)})
                if mode != "no_done":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ── 便捷视图
    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:" + str(self.port)

    @property
    def upstream_base_url(self) -> str:
        return self.base_url + "/v1"

    def chat_requests(self) -> list[dict[str, Any]]:
        return [item for item in self.requests
                if item.get("path") == "/v1/chat/completions"]

    def models_requests(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if item.get("path") == "/v1/models"]
