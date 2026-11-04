# SPDX-License-Identifier: Apache-2.0
"""真实子进程生命周期：djb start -> 回环 /v1 -> djb stop（mock 服务端，无外网）。"""
from __future__ import annotations

import http.client
import json
import os
import socket
import time

import pytest

from djb_node.cli import main
from djb_node.config import Paths
from djb_node.daemon import ControlClient
from djb_node.errors import NotRunningError
from tests import process_tree
from tests.mock_m02 import MockServer


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _call(port, method, path, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": "Bearer " + token} if token else {}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


def _force_stop(paths):
    try:
        ControlClient(paths).call({"cmd": "stop"}, timeout=3.0)
        time.sleep(0.5)
    except Exception:
        pass


def test_cli_start_stop_subprocess(home, windows_control_usable, capsys):
    server = MockServer().start()
    port = _free_port()
    # paths 必须在 `main(["start"])` **之前**构造：start 会真的 spawn daemon，
    # 而它失败（Windows 上 "控制不可用" 超时返回 4）时后面的 `paths = Paths(...)`
    # 根本执行不到 —— 之前的实现就是这样漏出整棵进程树的（task-8）。
    paths = Paths(str(home))
    try:
        assert main(["init", "--no-env", "--gateway", server.base_url,
                     "--port", str(port), "--cpu", "100", "--gpu", "100",
                     "--mem", "99"]) == 0
        capsys.readouterr()
        assert main(["limits", "--no-yield"]) == 0
        capsys.readouterr()
        # 先加一个上游（心跳会携容量公告）
        assert main(["upstream", "add", "--access", "A2",
                     "--base-url", server.upstream_base_url,
                     "--models", "mock-model", "--json"]) == 0
        capsys.readouterr()
        assert main(["start", "--timeout", "30", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["started"] is True
        token = payload["local_token"]
        assert len(token) >= 32
        try:
            # daemon 已在运行：状态 + 只绑回环 + 令牌可用
            response = ControlClient(paths).call({"cmd": "status"}, timeout=5.0)
            status = response["status"]
            assert status["state"] == "online"
            assert status["local_api"]["host"] == "127.0.0.1"
            assert status["local_api"]["port"] == port
            assert status["local_api"]["loopback_only"] is True
            assert status["node"]["bound"] is True
            # /v1/health 免令牌；/v1/models 需要令牌
            code, _ = _call(port, "GET", "/v1/health")
            assert code == 200
            code, _ = _call(port, "GET", "/v1/models")
            assert code == 401
            code, data = _call(port, "GET", "/v1/models", token=token)
            assert code == 200
            assert json.loads(data)["data"][0]["id"] == "mock-model"
            # 令牌不在磁盘上（不落盘）
            for root, _, names in os.walk(paths.home):
                for name in names:
                    try:
                        text = open(os.path.join(root, name), "r", encoding="utf-8",
                                    errors="ignore").read()
                    except OSError:
                        continue
                    assert token not in text
        finally:
            _force_stop(paths)
        assert main(["stop"]) == 0
        time.sleep(0.5)
        with pytest.raises(NotRunningError):
            ControlClient(paths).call({"cmd": "ping"})
    finally:
        # 必然执行：先试优雅停，再按**进程树**整体回收（幂等；已退出则空转）。
        # 不能只依赖 `djb stop`：start 失败时没有 pid 句柄，且内层 daemon 会留在树里。
        try:
            process_tree.reap_daemon_tree(paths.home)
        except Exception:                                  # noqa: BLE001 - teardown 不许抛
            pass
        server.stop()
