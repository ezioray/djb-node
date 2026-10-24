# SPDX-License-Identifier: Apache-2.0
"""C10c 测试替身：平台通道端点 + 本机镜像 chat（纯 http.server，无外网）。

三个端点：

    POST /api/v1/node/channel/poll    204（无 job）/ 200（job）/ 脚本化的 4xx-5xx
    POST /api/v1/node/channel/result  200 accepted / duplicate / 脚本化的 5xx
    POST /v1/chat/completions         本机镜像（可配 status + body）

只在本机回环上随机端口起服务；记录收到的 body / 头，供断言「不发正文、带对头」。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-mock",
    "object": "chat.completion",
    "model": "mock-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
}


class MockChannel:
    """脚本化平台通道 + 本机 chat 的假服务端。"""

    def __init__(self) -> None:
        self.poll_script: list[Any] = []      # None=204，dict=job，int=HTTP 错误码
        self.poll_default: Any = None         # 脚本用尽后的行为（默认 204）
        self.polls: list[dict[str, Any]] = []
        self.poll_headers: list[dict[str, str]] = []
        self.results: list[dict[str, Any]] = []
        self.result_headers: list[dict[str, str]] = []
        self.fail_results_times = 0
        self.chat_requests: list[dict[str, Any]] = []
        self.chat_auth: list[str] = []
        self.chat_status = 200
        self.chat_payload: dict[str, Any] = dict(DEFAULT_COMPLETION)
        self._accepted: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ── 生命周期
    def start(self) -> "MockChannel":
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
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return {}
                return value if isinstance(value, dict) else {}

            def _headers(self) -> dict[str, str]:
                # urllib 会把头名 capitalize；统一小写，断言与服务端解析都不受大小写影响
                return {key.lower(): value for key, value in self.headers.items()}

            def _json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def do_GET(self) -> None:                          # noqa: N802
                self._json(404, {"error": {"type": "not_found", "code": 40401,
                                           "message": "not found",
                                           "request_id": "mock"}})

            def do_POST(self) -> None:                         # noqa: N802
                path = self.path.split("?", 1)[0]
                body = self._body()
                if path == "/api/v1/node/channel/poll":
                    outer.polls.append(body)
                    outer.poll_headers.append(self._headers())
                    item = outer.poll_script.pop(0) if outer.poll_script \
                        else outer.poll_default
                    if isinstance(item, int):
                        self._json(item, {"error": {
                            "type": "authentication_error" if item in (401, 403)
                            else "server_error",
                            "code": 40101 if item in (401, 403) else 50001,
                            "message": "mock channel error", "request_id": "mock"}})
                        return
                    if isinstance(item, dict):
                        self._json(200, item)
                        return
                    self._empty(204)
                    return
                if path == "/api/v1/node/channel/result":
                    outer.results.append(body)
                    outer.result_headers.append(self._headers())
                    if outer.fail_results_times > 0:
                        outer.fail_results_times -= 1
                        self._json(500, {"error": {"type": "server_error", "code": 50001,
                                                   "message": "mock busy",
                                                   "request_id": "mock"}})
                        return
                    job_id = str(body.get("job_id") or "")
                    duplicate = job_id in outer._accepted
                    if not duplicate:
                        outer._accepted.append(job_id)
                    self._json(200, {"accepted": [] if duplicate else [job_id],
                                     "duplicate": duplicate})
                    return
                if path == "/v1/chat/completions":
                    outer.chat_requests.append(body)
                    outer.chat_auth.append(str(self.headers.get("Authorization") or ""))
                    if outer.chat_status != 200:
                        self._json(outer.chat_status, {"error": {
                            "type": "upstream_error", "code": 50903,
                            "message": "mock chat failure", "request_id": "mock"}})
                        return
                    self._json(200, outer.chat_payload)
                    return
                self._json(404, {"error": {"type": "not_found", "code": 40401,
                                           "message": "not found", "request_id": "mock"}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
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

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:" + str(self.port)
