# SPDX-License-Identifier: Apache-2.0
"""mock M02 / M05 / 上游 OpenAI 端点（纯 http.server，无数据库、无外网）。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "1.0.0"
CLIENT_KEY = "ck_" + "test_mock_client_key_0123456789abcdef"
NEW_CLIENT_KEY = "ck_" + "test_rotated_key_0123456789abcdef"
NODE_ID = "node-mock-1"
POLICY_VERSION = "policy-1"
# 固定测试纪元：daemon 用假时钟（2026-10-24T00:00Z），mock 不得用真实时间生成 issued_at，
# 否则每天 UTC 00:05 之后「issued_at 在未来」会假失败（时间炸弹）。
TEST_EPOCH = "2026-10-24T00:00:00Z"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def policy_payload(*, policy_version: str = POLICY_VERSION, quotas=None,
                   issued_at: str | None = None) -> dict[str, Any]:
    return {
        "policy_version": policy_version,
        "protocol_version": PROTOCOL_VERSION,
        "issued_at": issued_at or TEST_EPOCH,
        "effective_at": "2026-10-24T00:00:00Z",
        "digest": "b" * 64,
        "quotas": quotas or [],
        "directives": [],
        # 固定纪元 + 1h TTL 会在真实时钟 01:00Z 后永久过期（子进程用真实时钟）。
        # 统一给 10 年 TTL；过期场景由用例自行覆写 expires_at / policy_override。
        # 显式远期 expires_at：parse_policy 对 policy_ttl_s 有 30 天上限，
        # 超出会回退到默认 1h（真实时钟 01:00Z 后必过期）。过期场景由用例覆写。
        "expires_at": "2036-01-01T00:00:00Z",
        "settings": {"policy_ttl_s": 3600},
    }


class MockServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        # CR-20261106-OFFICIAL-SUPPLY-01 报价：内容指纹命中即复用（created=false，
        # 与云端 quotes.register 一致：不升版本、不延长 expires_at）
        self.quotes: list[dict[str, Any]] = []
        self.quote_keys: dict[tuple, str] = {}
        self.quote_fail_models: set[str] = set()
        self.quote_status = 201
        self.policy_version = POLICY_VERSION
        self.policy_issued_at: str | None = None
        self.policy_updated = True
        self.policy_quotas: list[dict[str, Any]] = []
        self.register_state = "active"
        self.register_returns_key = True
        self.fail_register_count = 0
        self.policy_override: dict[str, Any] | None = None
        self.fail_heartbeat_count = 0
        self.fail_receipts_count = 0
        self.heartbeat_status = 200
        self.receipts_status = 200
        self.policy_status = 200
        self.capabilities_status = 200
        self.capabilities_override: dict[str, Any] | None = None
        self.chat_status = 200
        self.chat_usage = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
        self.extra_headers: dict[str, str] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ── 生命周期
    def start(self) -> "MockServer":
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
                for key, value in outer.extra_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status: int, code: int, message: str) -> None:
                self._json(status, {"error": {"type": "invalid_request_error",
                                              "code": code, "message": message,
                                              "request_id": "mock"}})

            def do_GET(self) -> None:                      # noqa: N802
                path = self.path.split("?", 1)[0]
                outer.requests.append({"method": "GET", "path": path,
                                       "headers": dict(self.headers)})
                if path == "/api/v1/protocol/capabilities":
                    if outer.capabilities_status != 200:
                        self._error(outer.capabilities_status, 42601, "protocol")
                        return
                    if outer.capabilities_override is not None:
                        self._json(200, outer.capabilities_override)
                        return
                    self._json(200, {"protocol_version": PROTOCOL_VERSION,
                                     "min_supported_version": PROTOCOL_VERSION,
                                     "capabilities": [{"id": "chat_completions",
                                                       "version": "1.0.0",
                                                       "status": "stable"},
                                                      {"id": "receipts",
                                                       "version": "1.0.0",
                                                       "status": "stable"}],
                                     "server_time": _iso_now()})
                    return
                if path == "/api/v1/protocol/deprecations":
                    self._json(200, {"items": [], "protocol_version": PROTOCOL_VERSION})
                    return
                if path == "/api/v1/node/policy":
                    if not outer._authed(self):
                        return
                    if outer.policy_status != 200:
                        self._error(outer.policy_status, 40305, "node state")
                        return
                    if_none_match = (self.headers.get("If-None-Match") or "").strip()
                    if if_none_match and if_none_match == outer.policy_version:
                        self.send_response(304)
                        self.send_header("ETag", outer.policy_version)
                        self.end_headers()
                        return
                    if outer.policy_override is not None:
                        self._json(200, outer.policy_override)
                        return
                    self._json(200, policy_payload(
                        policy_version=outer.policy_version,
                        quotas=outer.policy_quotas,
                        issued_at=outer.policy_issued_at))
                    return
                if path == "/api/v1/node/binding":
                    if not outer._authed(self):
                        return
                    self._json(200, {"node_id": NODE_ID, "bound": True,
                                     "account_id": "acct-1"})
                    return
                if path == "/v1/models":
                    self._json(200, {"object": "list", "data": [
                        {"id": "mock-model", "object": "model", "owned_by": "mock"}]})
                    return
                self._error(404, 40401, "not found")

            def do_POST(self) -> None:                     # noqa: N802
                path = self.path.split("?", 1)[0]
                body = self._body()
                outer.requests.append({"method": "POST", "path": path,
                                       "headers": dict(self.headers), "body": body})
                if path == "/api/v1/node/register":
                    if outer.fail_register_count > 0:
                        outer.fail_register_count -= 1
                        self._error(500, 50001, "mock register failure")
                        return
                    payload = {"node_id": NODE_ID, "state": outer.register_state,
                               "policy_version": outer.policy_version,
                               "protocol_version": PROTOCOL_VERSION,
                               "server_time": _iso_now()}
                    if outer.register_returns_key:
                        payload["client_key"] = CLIENT_KEY
                    self._json(201, payload)
                    return
                if path == "/api/v1/node/pair":
                    self._json(201, {"node_id": NODE_ID, "client_key": NEW_CLIENT_KEY,
                                     "key_prefix": "ck_test_",
                                     "previous_key_expires_at": _iso_now()})
                    return
                if path == "/api/v1/node/binding":
                    self._json(200, {"node_id": NODE_ID, "bound": True,
                                     "account_id": "acct-1"})
                    return
                if path == "/api/v1/node/heartbeat":
                    if not outer._authed(self):
                        return
                    outer.heartbeats.append(body)
                    if outer.fail_heartbeat_count > 0:
                        outer.fail_heartbeat_count -= 1
                        self._error(500, 50001, "mock failure")
                        return
                    if outer.heartbeat_status != 200:
                        code = {403: 40305, 429: 42901, 426: 42601}.get(
                            outer.heartbeat_status, 50001)
                        self._error(outer.heartbeat_status, code,
                                    "frozen" if outer.heartbeat_status == 403 else "err")
                        return
                    updated = outer.policy_updated
                    outer.policy_updated = False
                    self._json(200, {"server_time": _iso_now(),
                                     "protocol_version": PROTOCOL_VERSION,
                                     "policy_version": outer.policy_version,
                                     "next_heartbeat_seconds": 30,
                                     "policy_updated": updated,
                                     "announcement": {"seq": len(outer.heartbeats),
                                                      "accepted": True,
                                                      "expires_at": _iso_now()}})
                    return
                if path == "/api/v1/node/upstream":
                    self._json(200, {"upstream_id": "up-1", "state": "declared",
                                     "models_declared": body.get("models") or [],
                                     "probe_hint": "probe_locally"})
                    return
                if path == "/api/v1/node/upstream/probe":
                    self._json(200, {"upstream_id": body.get("upstream_id"),
                                     "state": "verified", "recorded_at": _iso_now()})
                    return
                if path == "/api/v1/schedule/quotes":
                    # 云端契约：X-Client-Key 鉴权；body {node_id?, access, model, unit,
                    # price_min, price_max?, ttl_s, currency?}；201 + 扁平 QuoteAck
                    if not outer._authed(self):
                        return
                    outer.quotes.append(body)
                    model = body.get("model")
                    if model in outer.quote_fail_models:
                        self._error(400, 40001, "mock quote rejected")
                        return
                    if outer.quote_status != 201:
                        self._error(outer.quote_status, 50001, "mock quote failure")
                        return
                    fingerprint = (body.get("node_id") or NODE_ID, body.get("access"),
                                   model, body.get("unit"), body.get("price_min"),
                                   body.get("price_max"), body.get("ttl_s"),
                                   body.get("currency"))
                    created = fingerprint not in outer.quote_keys
                    quote_id = outer.quote_keys.setdefault(
                        fingerprint, "q-" + str(len(outer.quote_keys) + 1))
                    self._json(201, {"quote_id": quote_id, "state": "active",
                                     "quote_version": 1 if created else 1,
                                     "expires_at": _iso_now(), "created": created})
                    return
                if path == "/api/v1/node/receipts":
                    if not outer._authed(self):
                        return
                    if not self.headers.get("X-DJB-Idempotency-Key"):
                        self._error(400, 40001, "idempotency required")
                        return
                    outer.receipts.append(body)
                    if outer.fail_receipts_count > 0:
                        outer.fail_receipts_count -= 1
                        self._error(500, 50001, "mock receipts failure")
                        return
                    if outer.receipts_status != 200:
                        self._error(outer.receipts_status, 40001, "bad receipts")
                        return
                    receipts = body.get("receipts") or []
                    self._json(200, {"accepted": [item.get("receipt_id") for item in receipts],
                                     "rejected": []})
                    return
                if path == "/v1/chat/completions":
                    outer._chat(self, body)
                    return
                self._error(404, 40401, "not found")

            def do_DELETE(self) -> None:                   # noqa: N802
                path = self.path.split("?", 1)[0]
                outer.requests.append({"method": "DELETE", "path": path,
                                       "headers": dict(self.headers)})
                if path == "/api/v1/node/binding":
                    self._json(200, {"node_id": NODE_ID, "bound": False})
                    return
                self._error(404, 40401, "not found")

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

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:" + str(self.port)

    @property
    def upstream_base_url(self) -> str:
        return self.base_url + "/v1"

    # ── 行为
    def _authed(self, handler: BaseHTTPRequestHandler) -> bool:
        key = handler.headers.get("X-Client-Key")
        if key in (CLIENT_KEY, NEW_CLIENT_KEY):
            return True
        handler._json(401, {"error": {"type": "authentication_error", "code": 40101,
                                      "message": "bad key", "request_id": "mock"}})
        return False

    def _chat(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        if self.chat_status != 200:
            handler._json(self.chat_status, {"error": {"type": "upstream_error",
                                                       "code": 50903,
                                                       "message": "mock upstream",
                                                       "request_id": "mock"}})
            return
        if body.get("stream"):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            chunks = [
                {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "你"}}]},
                {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "好"}}]},
                {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                 "choices": [], "usage": dict(self.chat_usage)},
            ]
            for chunk in chunks:
                handler.wfile.write(("data: " + json.dumps(chunk,
                                                           ensure_ascii=False) + "\n\n")
                                    .encode("utf-8"))
                handler.wfile.flush()
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
            handler.close_connection = True
            return
        handler._json(200, {"id": "chatcmpl-1", "object": "chat.completion",
                            "created": 1, "model": body.get("model"),
                            "choices": [{"index": 0,
                                         "message": {"role": "assistant",
                                                     "content": "你好"},
                                         "finish_reason": "stop"}],
                            "usage": dict(self.chat_usage)})
