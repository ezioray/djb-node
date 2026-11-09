#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""T2 最小假网关（RC8.3-C）：客户端启动/心跳/绑定/报价所需的全部端点，只绑 127.0.0.1。

用法：python3 tests/rc8/t2_gateway.py <log-path> [--port 18190]

为什么需要它：T2 要验证 register/heartbeat/policy/bind/quotes 全链路，但**绝不能**
连生产或 RC 栈（会真注册节点）。假网关只回固定 JSON，不落任何凭据。

实测两个坑（改坏了 T2 就会假红/假绿）：
  1) policy 必须是合法 NodePolicy（policy_version / protocol_version / digest / issued_at），
     否则客户端停在 paused(conservative)；
  2) channel/poll 必须等 wait_s（真服务端是长轮询）；立即 204 会让客户端热循环打爆。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUND = [False]
NOW = datetime.now(timezone.utc)


def build_documents() -> tuple[dict, dict]:
    policy = {
        "policy_version": "pol-t2-1",
        "protocol_version": "1.0.0",
        "digest": hashlib.sha256(b"t2-gateway-policy").hexdigest(),
        "issued_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quotas": [], "directives": [], "settings": {},
    }
    capabilities = {
        "protocol_version": "1.0.0",
        "min_supported_version": "1.0.0",
        "server_time": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capabilities": [
            {"id": cid, "version": "1", "status": "stable"}
            for cid in ("chat_completions", "receipts", "policy", "capacity_announcement",
                        "upstream_probe", "protocol_negotiation", "quotes")
        ],
    }
    return policy, capabilities


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_path = "/tmp/t2-gateway.log"

    # ── helpers
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send204(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _log(self, line: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ── routes
    def do_GET(self) -> None:                       # noqa: N802 (http.server API)
        policy, capabilities = build_documents()
        self._log("GET " + self.path)
        if self.path.startswith("/api/v1/protocol/capabilities"):
            self._send(200, capabilities)
        elif self.path.startswith("/api/v1/protocol/deprecations"):
            self._send(200, {"items": []})
        elif self.path.startswith("/api/v1/node/policy"):
            self._send(200, policy)
        elif self.path.startswith("/api/v1/node/binding"):
            self._send(200, {"bound": BOUND[0], "node_id": "node-t2-0001"})
        else:
            self._send(404, {"code": "not_found", "message": "t2-gateway"})

    def do_POST(self) -> None:                      # noqa: N802
        body = self._read()
        self._log("POST " + self.path + " body=" + body.decode("utf-8", "replace")[:160])
        if self.path.startswith("/api/v1/node/register"):
            self._send(201, {"node_id": "node-t2-0001", "state": "pending",
                             "client_key": "djb_ck_t2_0001"})
        elif self.path.startswith("/api/v1/node/binding"):
            BOUND[0] = True
            self._log("BIND accepted")
            self._send(200, {"bound": True, "node_id": "node-t2-0001", "state": "active"})
        elif self.path.startswith("/api/v1/node/heartbeat"):
            self._send(200, {"next_heartbeat_seconds": 60, "policy_updated": False})
        elif self.path.startswith("/api/v1/schedule/quotes"):
            self._send(201, {"quote_id": "q-t2-1", "state": "active", "quote_version": 1,
                             "expires_at": (NOW + timedelta(minutes=2)).strftime(
                                 "%Y-%m-%dT%H:%M:%SZ"),
                             "created": True})
        elif self.path.startswith("/api/v1/node/channel/poll"):
            wait = 2
            try:
                wait = min(int(json.loads(body or b"{}").get("wait_s") or 2), 2)
            except (TypeError, ValueError):
                pass
            time.sleep(max(wait, 1))
            self._send204()
        else:
            self._send(404, {"code": "not_found", "message": "t2-gateway"})

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", nargs="?", default="/tmp/t2-gateway.log")
    parser.add_argument("--port", type=int, default=18190)
    args = parser.parse_args()
    Handler.log_path = args.log
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("t2-gateway listening on 127.0.0.1:%d log=%s" % (args.port, args.log), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
