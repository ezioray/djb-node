# SPDX-License-Identifier: Apache-2.0
"""联调脚本的自检：没部署到位的实例必须被判成「未就绪」，凭据文件读不进来就退出。

不打真网：用 ``check_deploy`` 的纯函数面 + 一个本地 stdlib HTTP 服务喂 openapi.json。
"""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "e2e_nodecloud.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("e2e_nodecloud", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(spec_payload: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                                  # noqa: N802 - stdlib 约定
            if self.path != "/openapi.json":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(spec_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_deploy_check_passes_when_all_routes_present():
    tool = load_tool()
    paths = {method_path: {"get": {}} for method_path in
             ("/api/v1/node/tasks", "/api/v1/node/receipts", "/api/v1/node/earnings",
              "/api/v1/node/profile", "/api/v1/node/receipts/{receipt_id}")}
    server = serve({"paths": paths, "info": {"version": "9.9.9"}})
    try:
        report = tool.check_deploy("http://127.0.0.1:" + str(server.server_port))
    finally:
        server.shutdown()
    assert report["ok"] is True and report["version"] == "9.9.9"
    assert report["missing"] == []


def test_deploy_check_flags_old_build_method_mismatch():
    """线上真出现过：路径在、但只有 POST（旧构建的收据提交口）。"""
    tool = load_tool()
    server = serve({"paths": {"/api/v1/node/receipts": {"post": {}}}, "info": {}})
    try:
        report = tool.check_deploy("http://127.0.0.1:" + str(server.server_port))
    finally:
        server.shutdown()
    assert report["ok"] is False
    reasons = [item["why"] for item in report["missing"]]
    assert any("方法不支持" in reason for reason in reasons)
    assert any("路径不存在" in reason for reason in reasons)


def test_deploy_check_reports_unreachable_instance():
    tool = load_tool()
    report = tool.check_deploy("http://127.0.0.1:1", timeout=1.0)
    assert report["ok"] is False and report["error"]
    assert "hint" in report


def test_key_file_must_be_0600_and_reads_nothing_back(tmp_path):
    tool = load_tool()
    path = tmp_path / "k"
    path.write_text("ck-abc\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(SystemExit) as info:
        tool.read_key(path)
    assert "权限过宽" in str(info.value.code)
    path.chmod(0o600)
    assert tool.read_key(path) == "ck-abc"                # 去掉尾部换行


def test_summarize_never_leaks_row_bodies():
    tool = load_tool()
    summary = tool.summarize("tasks", {"items": [{"task_id": "t-1", "state": "done",
                                                  "prompt": "不要外泄的正文"}], "truncated": False})
    assert summary["rows"] == 1
    assert "prompt" not in json.dumps(summary)
