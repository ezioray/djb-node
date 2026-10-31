# SPDX-License-Identifier: Apache-2.0
"""`djb start` 必须等到状态机真的 online 才说「已启动」。

修前的行为：控制口 ping 一通就返回 ``started: true``，此时状态可能还是 ``starting``——
用户看到「已启动」但一单不接（真联调时这条把生命周期测试打红了）。
"""
from __future__ import annotations

import json

import pytest

from djb_node import cli as cli_module
from djb_node import facts


class FakeControl:
    """按脚本逐个返回状态的假控制通道。"""

    states: list[str] = []
    calls: list[str] = []

    def __init__(self, _paths):
        pass

    def call(self, payload, timeout=None):        # noqa: ARG002 - 与真接口同名
        cmd = payload.get("cmd")
        FakeControl.calls.append(cmd)
        if cmd == "ping":
            return {"ok": True, "pid": 4242}
        state = FakeControl.states.pop(0) if FakeControl.states else "starting"
        return {"status": {"state": state, "reason": None}}


@pytest.fixture()
def started_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJB_HOME", str(tmp_path))
    monkeypatch.setattr(cli_module, "_spawn_daemon", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "ControlClient", FakeControl)
    FakeControl.states = []
    FakeControl.calls = []
    assert cli_module.main(["init", "--no-env", "--gateway", "http://127.0.0.1:9",
                            "--port", "0"]) == 0
    return tmp_path


def test_start_waits_until_online(started_home, capsys):
    FakeControl.states = ["starting", "starting", "online"]
    assert cli_module.main(["start", "--timeout", "5", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["started"] is True and payload["state"] == "online"
    assert FakeControl.calls.count("status") == 3          # 前两次 starting 没放行


def test_start_fails_readably_when_it_never_accepts(started_home, capsys):
    FakeControl.states = ["starting"] * 100
    assert cli_module.main(["start", "--timeout", "1", "--json"]) == facts.EXIT_NOT_RUNNING
    err = capsys.readouterr().err
    assert "没有进入接单状态" in err and "starting" in err


def test_start_stops_waiting_on_paused_and_says_why(started_home, capsys):
    FakeControl.states = ["paused"]
    assert cli_module.main(["start", "--timeout", "5", "--json"]) == facts.EXIT_NOT_RUNNING
    err = capsys.readouterr().err
    assert "paused" in err and "没有进入接单状态" in err
