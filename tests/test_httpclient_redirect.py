# SPDX-License-Identifier: Apache-2.0
"""重定向护栏（2026-11-15 审计 P1）：跨源不带走凭据、https 不降级、跳数有上限。

现场：`urllib` 默认重定向会把请求头原样带到新 URL —— 节点密钥在 `X-Client-Key`、
上游密钥在 `Authorization`。网关/上游只要回一次 302，密钥就被送到另一台主机。
"""
from __future__ import annotations

import http.server
import threading
import urllib.parse
import urllib.request

import pytest

from djb_node.httpclient import CREDENTIAL_HEADERS, HttpClient, _RedirectGuard


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []
    route = "/"

    def do_GET(self) -> None:                                   # noqa: N802
        type(self).seen.append({"path": self.path, "headers": dict(self.headers)})
        if self.path.startswith("/to-other"):
            target = self.server.target + "/landed"             # type: ignore[attr-defined]
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if self.path.startswith("/to-same"):
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:                        # 静默
        pass


def _serve(*, target: str = "") -> tuple[http.server.HTTPServer, threading.Thread]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.target = target                                      # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def other_server():
    server, thread = _serve()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_cross_origin_redirect_strips_credentials(other_server):
    source, thread = _serve(target="http://127.0.0.1:%d" % other_server.server_port)
    try:
        _Handler.seen.clear()
        response = HttpClient(timeout=5).request(
            "GET", "http://127.0.0.1:%d/to-other" % source.server_port,
            headers={"X-Client-Key": "SYNTHETIC-NODE-KEY",
                     "Authorization": "Bearer SYNTHETIC-UPSTREAM-KEY",
                     "X-Trace": "keep-me"})
        assert response.status == 200
        landed = [item for item in _Handler.seen if item["path"] == "/landed"][-1]
        for name in CREDENTIAL_HEADERS:
            assert name not in {key.lower() for key in landed["headers"]}
        assert landed["headers"].get("X-Trace") == "keep-me"     # 非凭据头保留
        assert response.url.endswith("/landed")                  # 如实给出最终 URL
    finally:
        source.shutdown()
        thread.join(timeout=2)


def test_same_origin_redirect_keeps_credentials():
    server, thread = _serve()
    try:
        _Handler.seen.clear()
        response = HttpClient(timeout=5).request(
            "GET", "http://127.0.0.1:%d/to-same" % server.server_port,
            headers={"X-Client-Key": "SYNTHETIC-NODE-KEY"})
        assert response.status == 200
        landed = [item for item in _Handler.seen if item["path"] == "/landed"][-1]
        assert landed["headers"].get("X-Client-Key") == "SYNTHETIC-NODE-KEY"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_https_to_http_downgrade_is_refused():
    handler = _RedirectGuard()
    request = urllib.request.Request("https://gw.example.invalid/api/v1/node/policy",
                                     headers={"X-Client-Key": "SYNTHETIC"})
    assert handler.redirect_request(request, None, 302, "Found", {},
                                    "http://gw.example.invalid/api/v1/node/policy") is None


def test_redirect_budget_is_bounded():
    assert _RedirectGuard.max_redirections <= 3
