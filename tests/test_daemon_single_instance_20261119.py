# SPDX-License-Identifier: Apache-2.0
"""单实例护栏（T2，`CR-20261119-CLIENT-TLS-DIAG`）。

客户现场实测：`djb-desktop` 与手动 `djb-node start` 同时跑起两个 daemon（两个 pid），
两边都去注册、互相把 ``client_key`` 转掉。旧实现启动前**完全不读** pid 文件，只写。

边界（本文件钉死）：

1. pid 文件里的进程**验得过**（``pidfile.verify`` 全过）→ 拒绝再起一个，抛 ``ConflictError``；
2. 验不过（PID 复用/旧版记录/文件损坏）→ 放行，宁可多起一次也不许卡死启动；
3. ``DJB_DAEMON_SINGLE_INSTANCE=0`` → 回到旧行为；
4. 控制面还没应答但 pid 文件验得过时，``start_daemon`` 报 ``already-running``，不再 spawn。
"""
from __future__ import annotations

import pytest

from djb_node import bootstrap, daemon, facts, pidfile
from djb_node.errors import ConflictError

LIVE_RECORD = {"schema": pidfile.SCHEMA, "pid": 4242, "start_token": "tok-abc",
               "home": "/tmp/djb-home", "marker": "djb_node.daemon"}


class _Stub:
    """只借 ``paths`` 一个属性 —— 不构造真 Daemon（那要全套依赖）。"""

    def __init__(self, paths=None) -> None:
        self.paths = paths


def _stub_paths():
    return object()          # _refuse_if_another_daemon 只把它透传给 pidfile


def test_single_instance_is_on_by_default():
    assert facts.single_instance_enabled("") is True
    assert facts.single_instance_enabled(None) is True        # 未设 = 开


@pytest.mark.parametrize("raw", ["0", "off", "false", "no", "OFF"])
def test_single_instance_can_be_turned_off(raw):
    assert facts.single_instance_enabled(raw) is False


def test_refuses_when_a_live_daemon_is_verified(monkeypatch):
    monkeypatch.delenv(facts.DAEMON_SINGLE_INSTANCE_ENV, raising=False)
    monkeypatch.setattr(daemon.pidfile, "read", lambda _paths: dict(LIVE_RECORD))
    monkeypatch.setattr(daemon.pidfile, "verify", lambda *_a, **_k: (True, ""))
    with pytest.raises(ConflictError) as caught:
        daemon.Daemon._refuse_if_another_daemon(_Stub(_stub_paths()))
    assert "只允许一个实例" in caught.value.message
    assert caught.value.details["reason"] == "single_instance"


def test_allows_when_verification_fails(monkeypatch):
    """PID 复用 / 旧版记录 / 文件损坏 → 放行（不许把启动卡死）。"""
    monkeypatch.delenv(facts.DAEMON_SINGLE_INSTANCE_ENV, raising=False)
    monkeypatch.setattr(daemon.pidfile, "read", lambda _paths: dict(LIVE_RECORD))
    monkeypatch.setattr(daemon.pidfile, "verify",
                        lambda *_a, **_k: (False, pidfile.REASON.TOKEN_MISMATCH))
    daemon.Daemon._refuse_if_another_daemon(_Stub(_stub_paths()))   # 不抛 = 放行


def test_kill_switch_restores_old_behaviour(monkeypatch):
    monkeypatch.setenv(facts.DAEMON_SINGLE_INSTANCE_ENV, "0")
    monkeypatch.setattr(daemon.pidfile, "read", lambda _paths: dict(LIVE_RECORD))
    monkeypatch.setattr(daemon.pidfile, "verify", lambda *_a, **_k: (True, ""))
    daemon.Daemon._refuse_if_another_daemon(_Stub(_stub_paths()))


def test_start_daemon_reports_already_running_from_pid_file(tmp_path, monkeypatch):
    monkeypatch.delenv(facts.DAEMON_SINGLE_INSTANCE_ENV, raising=False)
    monkeypatch.setattr(bootstrap, "control_ping", lambda _paths: False)
    monkeypatch.setattr(bootstrap.pidfile, "read", lambda _paths: dict(LIVE_RECORD))
    monkeypatch.setattr(bootstrap.pidfile, "verify", lambda *_a, **_k: (True, ""))

    def _boom(*_args, **_kwargs):            # 走到 spawn 就是错
        raise AssertionError("已经在跑时不许再 spawn 一个 daemon")

    monkeypatch.setattr(bootstrap.subprocess, "Popen", _boom)
    result = bootstrap.start_daemon(home=tmp_path)
    assert result["reason"] == "already-running"
    assert result["pid"] == 4242
    assert result["started"] is False


def test_start_daemon_still_starts_when_no_record(tmp_path, monkeypatch):
    monkeypatch.delenv(facts.DAEMON_SINGLE_INSTANCE_ENV, raising=False)
    monkeypatch.setattr(bootstrap, "control_ping", lambda _paths: False)
    monkeypatch.setattr(bootstrap.pidfile, "read", lambda _paths: None)
    monkeypatch.setattr(bootstrap, "_daemon_command", lambda *_a, **_k: None)
    result = bootstrap.start_daemon(home=tmp_path)
    assert result["reason"] == "no-cli"      # 越过了单实例判断，才走到「找不到 CLI」
