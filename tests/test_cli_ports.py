# SPDX-License-Identifier: Apache-2.0
"""E2E-09：端口占用 / 停止竞态的提示必须是**真话**。

修前现场（TICKETS-ADDENDUM-01 19:26 复现）：`stop` 返回「已停止」但监听 socket 未释放，
紧跟的 `start` 报「daemon 启动超时（控制套接字不存在）」，真实原因
（`[Errno 98] Address already in use`）只躺在 daemon.out 里没人看得到。

本文件钉住三件事：
1. `start` 前置 bind 检查：端口被别人占着时**先说占用 + pid**，且不 spawn；
2. `start` 失败时把**日志尾行**回显出来（真实原因在那里）；
3. `stop` 等端口真正释放；等不到就说「端口仍被 pid=X 占用」，不谎报「已停止」。
"""
from __future__ import annotations

import socket

import pytest

from djb_node import cli as cli_module
from djb_node import facts


class FakeControl:
    """只回答 ping/stop/status，够 cmd_start / cmd_stop 走完。"""

    def __init__(self, _paths):
        pass

    def call(self, payload, timeout=None):      # noqa: ARG002 - 与真接口同名
        cmd = payload.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pid": 4242}
        if cmd == "stop":
            return {"ok": True, "message": "daemon 正在退出"}
        return {"ok": True, "status": {"state": "online", "reason": None}}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJB_HOME", str(tmp_path))
    monkeypatch.setattr(cli_module, "_spawn_daemon", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "ControlClient", FakeControl)
    assert cli_module.main(["init", "--no-env", "--gateway", "http://127.0.0.1:9",
                            "--port", "18555"]) == 0
    return tmp_path


def test_start_timeout_names_the_port_owner_and_shows_log_tail(home, capsys,
                                                               monkeypatch):
    """启动失败 + 端口被占：提示里必须有「pid=X 占用」和日志尾行，不是一句「超时」。"""
    from djb_node.errors import NotRunningError

    class DownControl(FakeControl):
        def call(self, payload, timeout=None):
            if payload.get("cmd") == "ping":
                raise NotRunningError("daemon 未运行（控制套接字不存在）：先 djb start")
            return super().call(payload, timeout=timeout)

    monkeypatch.setattr(cli_module, "ControlClient", DownControl)
    monkeypatch.setattr(cli_module, "_port_busy", lambda _port: True)
    monkeypatch.setattr(cli_module, "_listening_pid", lambda _port: 8123)
    monkeypatch.setattr(cli_module, "_startup_log_hint",
                        lambda _paths, **_k: "端口 18555 绑定失败（[Errno 98] Address already in use）")
    # 退出码语义不变（启动失败仍是 EXIT_NOT_RUNNING），改的只是提示说真话
    assert cli_module.main(["start", "--timeout", "1"]) == facts.EXIT_NOT_RUNNING
    err = capsys.readouterr().err
    assert "18555" in err and "pid=8123" in err
    assert "占用" in err
    assert "Errno 98" in err                      # 日志尾行被回显
    assert "djb stop --force" in err              # 给出可行动路径


def test_start_does_not_mention_port_when_port_is_free(home, capsys, monkeypatch):
    """端口没被占时不许乱报「被占用」（误报会把排障带偏）。"""
    from djb_node.errors import NotRunningError

    class DownControl(FakeControl):
        def call(self, payload, timeout=None):
            if payload.get("cmd") == "ping":
                raise NotRunningError("daemon 未运行（控制套接字不存在）：先 djb start")
            return super().call(payload, timeout=timeout)

    monkeypatch.setattr(cli_module, "ControlClient", DownControl)
    monkeypatch.setattr(cli_module, "_port_busy", lambda _port: False)
    monkeypatch.setattr(cli_module, "_startup_log_hint", lambda _paths, **_k: "")
    assert cli_module.main(["start", "--timeout", "1"]) == facts.EXIT_NOT_RUNNING
    err = capsys.readouterr().err
    assert "启动超时" in err
    assert "占用" not in err


def test_start_echoes_log_tail_when_it_times_out(home, capsys, monkeypatch):
    """启动超时的真实原因在 daemon.out 里，CLI 必须回显尾行（E2E-09 的第 2 条）。"""
    from djb_node.errors import NotRunningError

    class DownControl(FakeControl):
        def call(self, payload, timeout=None):
            if payload.get("cmd") == "ping":
                raise NotRunningError("daemon 未运行（控制套接字不存在）：先 djb start")
            return super().call(payload, timeout=timeout)

    monkeypatch.setattr(cli_module, "ControlClient", DownControl)
    monkeypatch.setattr(cli_module, "_port_busy", lambda _port: False)
    monkeypatch.setattr(cli_module, "_startup_log_hint",
                        lambda _paths, **_k: "端口 18555 绑定失败（[Errno 98] Address already in use）")
    assert cli_module.main(["start", "--timeout", "1"]) == facts.EXIT_NOT_RUNNING
    err = capsys.readouterr().err
    assert "启动超时" in err
    assert "最近的日志" in err and "Errno 98" in err


def test_stop_waits_until_port_is_really_released(home, capsys, monkeypatch):
    """控制口先消失、端口稍后才释放：必须等到端口空了才说「已停止」。"""
    calls = {"n": 0}

    def busy(_port: int) -> bool:
        calls["n"] += 1
        return calls["n"] <= 3          # 前 3 次还在（正在退出），之后释放

    monkeypatch.setattr(cli_module, "_port_busy", busy)
    monkeypatch.setattr(cli_module, "control_socket_file", lambda _paths: "/tmp/no-such.sock")
    assert cli_module.main(["stop", "--timeout", "5"]) == 0
    assert "daemon 已停止" in capsys.readouterr().out
    assert calls["n"] > 3


def test_stop_says_port_busy_instead_of_lying(home, capsys, monkeypatch):
    """端口一直不放：说清楚「端口仍被 pid=X 占用」，不要打一句「已停止」了事。"""
    monkeypatch.setattr(cli_module, "_port_busy", lambda _port: True)
    monkeypatch.setattr(cli_module, "_listening_pid", lambda _port: 8123)
    monkeypatch.setattr(cli_module, "control_socket_file", lambda _paths: "/tmp/no-such.sock")
    monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
    assert cli_module.main(["stop", "--timeout", "0.3"]) == facts.EXIT_CONFLICT
    err = capsys.readouterr().err
    assert "停止超时" in err and "pid=8123" in err and "18555" in err


def test_listening_pid_matches_a_real_listener():
    """`_listening_pid` 在真监听上必须能定位到自己（Linux）；拿不到就返回 None。"""
    if not cli_module.os.path.isdir("/proc"):
        pytest.skip("非 Linux：/proc 不可用，函数按约定返回 None")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert cli_module._port_busy(port) is True
        assert cli_module._listening_pid(port) == cli_module.os.getpid()
    finally:
        listener.close()
    assert cli_module._port_busy(port) is False


def test_fresh_port_is_not_reported_busy():
    """没人的端口不能误报占用（否则正常启动会被拦住）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert cli_module._port_busy(port) is False