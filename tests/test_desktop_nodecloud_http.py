# SPDX-License-Identifier: Apache-2.0
"""F16 · 真 HTTP 联调（本地契约服务器）：把 `NodeCloudClient` 放在真 socket 上跑一遍。

与 `test_desktop_nodecloud.py` 的分工：那边用注入 transport 测**解析与口径**，
这边起一个 stdlib HTTP 服务器，验证**真的出站行为**——头名、查询串、游标翻页、
401 / 服务端降级的映射。云端四端点还没上线，所以这台「契约服务器」就是联调替身；
接口上线后同一批断言可以直接指向真网关。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from djb_node.desktop.datasource import ProviderError
from djb_node.desktop.nodecloud import NodeCloudClient
from djb_node.protocol import NODE_KEY_HEADER

PAGES = {
    "tasks": [
        {"items": [{"id": "t1", "state": "running"}], "page": {"next_cursor": "c2"}},
        {"items": [{"id": "t2", "state": "done"}], "page": {"next_cursor": "c3"}},
        {"items": [{"id": "t3", "state": "queued"}], "page": {"next_cursor": None}},
    ],
    "receipts": [
        {"items": [{"receipt_id": "r1", "status": "verified", "tokens": 10}],
         "page": {"next_cursor": None}},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    mode = {"value": "ok"}

    def log_message(self, *args):            # 安静
        pass

    def do_GET(self):                        # noqa: N802 - stdlib 约定
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        Handler.seen.append({"path": parsed.path, "query": query,
                             "key": self.headers.get(NODE_KEY_HEADER),
                             "protocol": self.headers.get("DJB-Protocol-Version")})
        if Handler.mode["value"] == "401":
            return self._send(401, {"error": {"type": "auth_error"}})
        if Handler.mode["value"] == "degraded":
            return self._send(200, {"items": [], "unavailable_reason": "metering 维护中"})
        kind = parsed.path.rsplit("/", 1)[-1]
        if kind == "earnings":
            return self._send(200, {"currency": "USDT", "settled_minor": 1_500_000,
                                    "pending_minor": 1_234_567, "today_minor": 250_000,
                                    "balance_minor": 0, "payouts": []})
        if kind == "profile":
            return self._send(200, {"email": "node@example.test", "role": "node",
                                    "twofa_enabled": True})
        pages = PAGES.get(kind, [])
        cursor = (query.get("cursor") or [None])[0]
        index = 0
        if cursor:
            from djb_node.desktop.nodecloud import _next_cursor
            for position, page in enumerate(pages):
                if _next_cursor(page) == cursor:
                    index = position + 1
                    break
        payload = pages[index] if index < len(pages) else {"items": []}
        self._send(200, payload)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    Handler.seen = []
    Handler.mode = {"value": "ok"}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def make_client(base: str) -> NodeCloudClient:
    return NodeCloudClient(base, "ck-test", timeout=5.0)


def test_real_http_pagination_end_to_end(server):
    client = make_client(server)
    result = client.fetch("tasks")
    assert [row["id"] for row in result["items"]] == ["t1", "t2", "t3"]
    assert result["pages"] == 3 and result["truncated"] is False
    assert [call["query"].get("cursor", [None])[0] for call in Handler.seen] == [None, "c2", "c3"]
    assert all(call["path"] == "/api/v1/node/tasks" for call in Handler.seen)


def test_real_http_sends_node_key_header_not_query(server):
    make_client(server).fetch("receipts")
    call = Handler.seen[0]
    assert call["key"] == "ck-test"
    assert "ck-test" not in json.dumps(call["query"])
    assert call["protocol"]                       # 协议版本头也带上了


def test_real_http_401_maps_to_relogin(server):
    Handler.mode["value"] = "401"
    with pytest.raises(ProviderError) as info:
        make_client(server).fetch("tasks")
    assert "登录已过期" in info.value.reason and "重新登录" in info.value.hint


def test_real_http_unavailable_reason_degrades(server):
    Handler.mode["value"] = "degraded"
    with pytest.raises(ProviderError) as info:
        make_client(server).fetch("receipts")
    assert "服务端已降级" in info.value.reason and "维护中" in info.value.reason


def test_real_http_earnings_and_profile(server):
    client = make_client(server)
    earnings = client.earnings()
    assert earnings["settled"] == "1.50" and earnings["pending"] == "1.234567"
    assert earnings["today"] == "0.25" and earnings["balance"] == "0.00"
    profile = client.profile()
    assert profile["email"] == "node@example.test" and profile["twofa_enabled"] is True


def test_real_http_connection_refused_is_readable():
    client = NodeCloudClient("http://127.0.0.1:9", "ck", timeout=1.0)
    with pytest.raises(ProviderError) as info:
        client.fetch("tasks")
    assert "云端连不上" in info.value.reason
