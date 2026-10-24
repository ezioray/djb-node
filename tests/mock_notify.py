# SPDX-License-Identifier: Apache-2.0
"""C16 测试替身：站内信端点（纯 http.server，无外网）。

    GET  /api/v1/node/notifications?since=&limit=&category=   200 {"items": [...], "unread_count": n}
    GET  /api/v1/node/notifications/unread-count              200 {"unread_count": n}
    POST /api/v1/node/notifications/{id}/read|ack             200 {"ok": true}

可脚本化：list_status / action_status / fail_read_times / fail_ack_times。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote


class MockNotify:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.unread_count = 0
        self.list_status = 200
        self.action_status = 200
        self.fail_read_times = 0
        self.fail_ack_times = 0
        self.list_queries: list[dict[str, str]] = []
        self.list_headers: list[dict[str, str]] = []
        self.actions: list[dict[str, Any]] = []
        self.action_headers: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> "MockNotify":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _headers(self) -> dict[str, str]:
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

            def _error(self, status: int) -> None:
                self._json(status, {"error": {"type": "server_error", "code": 50001,
                                              "message": "mock notify error",
                                              "request_id": "mock"}})

            def do_GET(self) -> None:                          # noqa: N802
                path, _, query = self.path.partition("?")
                if path == "/api/v1/node/notifications":
                    outer.list_queries.append(
                        {key: values[0] for key, values in parse_qs(query).items()})
                    outer.list_headers.append(self._headers())
                    if outer.list_status >= 400:
                        self._error(outer.list_status)
                        return
                    self._json(200, {"items": list(outer.items),
                                     "unread_count": outer.unread_count})
                    return
                if path == "/api/v1/node/notifications/unread-count":
                    self._json(200, {"unread_count": outer.unread_count})
                    return
                self._json(404, {"error": {"type": "not_found", "code": 40401,
                                           "message": "not found", "request_id": "mock"}})

            def do_POST(self) -> None:                         # noqa: N802
                path = self.path.split("?", 1)[0]
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    self.rfile.read(length)
                parts = path.strip("/").split("/")
                # api/v1/node/notifications/<id>/<action>
                if len(parts) == 6 and parts[:4] == ["api", "v1", "node", "notifications"]:
                    notification_id = unquote(parts[4])
                    action = parts[5]
                    outer.actions.append({"id": notification_id, "action": action})
                    outer.action_headers.append(self._headers())
                    if action not in ("read", "ack"):
                        self._json(404, {"error": {
                            "type": "not_found", "code": 40401,
                            "message": "unknown action", "request_id": "mock"}})
                        return
                    remaining = (outer.fail_read_times if action == "read"
                                 else outer.fail_ack_times)
                    if remaining > 0:
                        if action == "read":
                            outer.fail_read_times -= 1
                        else:
                            outer.fail_ack_times -= 1
                        self._error(500)
                        return
                    if outer.action_status >= 400:
                        self._error(outer.action_status)
                        return
                    self._json(200, {"ok": True, "id": notification_id,
                                     "action": action})
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
