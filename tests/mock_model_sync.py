# SPDX-License-Identifier: Apache-2.0
"""M18b 测试替身：模型目录 + 同步状态端点（纯 http.server，无外网）。

    GET  /api/v1/node/model-catalog?since=<v>   200 bundle / 304 / 脚本化错误
    POST /api/v1/node/model-sync/state          {"accepted": bool, "server_version": n}

只在本机回环随机端口起服务；记录请求头与 body，供断言「带对头、不泄凭据/正文」。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class MockModelSync:
    def __init__(self) -> None:
        self.catalog: dict[str, Any] | None = None
        self.catalog_status = 200          # 200 / 304 / 4xx-5xx
        self.state_status = 200
        self.server_version = 1
        self.catalog_requests: list[str] = []
        self.catalog_headers: list[dict[str, str]] = []
        self.state_requests: list[dict[str, Any]] = []
        self.state_headers: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ── 生命周期
    def start(self) -> "MockModelSync":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _headers(self) -> dict[str, str]:
                return {key.lower(): value for key, value in self.headers.items()}

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or "0")
                if not length:
                    return {}
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return {}
                return value if isinstance(value, dict) else {}

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
                path, _, query = self.path.partition("?")
                if path == "/api/v1/node/model-catalog":
                    outer.catalog_requests.append(query)
                    outer.catalog_headers.append(self._headers())
                    if outer.catalog_status == 304:
                        self._empty(304)
                        return
                    if outer.catalog_status >= 400:
                        self._json(outer.catalog_status, {"error": {
                            "type": "server_error", "code": 50001,
                            "message": "mock catalog error", "request_id": "mock"}})
                        return
                    self._json(200, outer.catalog or {})
                    return
                self._json(404, {"error": {"type": "not_found", "code": 40401,
                                           "message": "not found", "request_id": "mock"}})

            def do_POST(self) -> None:                         # noqa: N802
                path = self.path.split("?", 1)[0]
                body = self._body()
                if path == "/api/v1/node/model-sync/state":
                    outer.state_requests.append(body)
                    outer.state_headers.append(self._headers())
                    if outer.state_status >= 400:
                        self._json(outer.state_status, {"error": {
                            "type": "server_error", "code": 50001,
                            "message": "mock state error", "request_id": "mock"}})
                        return
                    self._json(200, {"accepted": True,
                                     "server_version": outer.server_version})
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
