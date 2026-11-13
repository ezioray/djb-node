# SPDX-License-Identifier: Apache-2.0
"""S1/S2/A 面回归：一轮刷新只取一次数（进程数瘦身）+ home 只解析一次。

两条都是真机事故换来的：

* **S1/S2**：冻结包 onefile 冷启动真机实测 ``status --json`` 1.6 s、``model-sync status``
  1.55 s、``powershell`` 0.2-0.5 s/次。旧实现一轮刷新起 6 次 CLI + 2 次 nvidia-smi
  （``status`` 被 5 个域各跑一次、``nvidia-smi`` 被 bench/gpu 各跑一次）。
* **S3**：``make_controller(home=None)`` 把 ``None`` 往下传，各下游各自找默认目录 ——
  数据层 ``resolve_home()``、action CLI ``str(None)``="None"、控制通道 ``~/.djb``。
  同一个窗口里「取数去 A、命令去 B、控制去 C」。
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from djb_node.desktop import datasource as ds


class _Runner:
    """计数用的 CLI 假实现：返回固定 payload，记录每次 argv。"""

    def __init__(self, payload=None, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._payload = payload if payload is not None else {"state": "online", "node": {}}
        self._fail = fail

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if self._fail:
            raise ds.ProviderError("状态回显不完整")
        return dict(self._payload)

    @property
    def status_calls(self) -> int:
        return len([item for item in self.calls if item[:1] == ("status",)])


def _source(tmp_path, **kwargs):
    source = ds.LocalDataSource(home=str(tmp_path), cloud_factory=lambda _home: None,
                                which=lambda _name: None, **kwargs)
    return source


# ─────────────────────────────────────────── 一轮刷新只跑一次 status
def test_status_runs_once_per_snapshot(tmp_path):
    runner = _Runner()
    source = _source(tmp_path, runner=runner)
    snapshot = source.snapshot()

    assert runner.status_calls == 1, runner.calls
    assert snapshot.get("status")["state"] == "online"
    # 依赖 status 的域拿到的是**同一份**数据（不是各跑一次各自解析）
    assert not isinstance(snapshot.get("token_plan"), ds.Unavailable)
    assert not isinstance(snapshot.get("yield"), ds.Unavailable)


def test_status_is_not_cached_outside_snapshot(tmp_path):
    """快照外直调 ``status()`` 永远取新值（否则「刷新」会拿到上一轮的状态）。"""
    runner = _Runner()
    source = _source(tmp_path, runner=runner)

    source.status()
    source.status()

    assert runner.status_calls == 2


def test_status_failure_is_cached_within_one_snapshot(tmp_path):
    """一轮里同一个失败只等一次（不再 5 次超时），但整域仍如实「暂不可用」。"""
    runner = _Runner(fail=True)
    source = _source(tmp_path, runner=runner)
    snapshot = source.snapshot()

    assert runner.status_calls == 1, runner.calls
    assert isinstance(snapshot.get("status"), ds.Unavailable)
    assert "状态回显不完整" in snapshot.get("status").reason


def test_a_new_snapshot_refetches_status(tmp_path):
    runner = _Runner()
    source = _source(tmp_path, runner=runner)
    source.snapshot()
    source.snapshot()
    assert runner.status_calls == 2


# ─────────────────────────────────────────── GPU / 机器画像
def test_gpu_probed_once_per_snapshot(tmp_path, monkeypatch):
    calls = []

    def fake_gpu_devices(*, which=None):
        calls.append(1)
        return {"present": False, "devices": [], "note": "fake"}

    monkeypatch.setattr(ds, "gpu_devices", fake_gpu_devices)
    source = _source(tmp_path)
    snapshot = source.snapshot()

    assert len(calls) == 1, "bench 与 gpu 两个域必须共用一次探测"
    assert snapshot.get("gpu")["note"] == "fake"


def test_machine_profile_collected_once_per_ttl(tmp_path, monkeypatch):
    from djb_node import machine_profile

    calls = []

    def fake_collect(*, home=None, **kwargs):
        calls.append(home)

        class _Doc:
            def to_dict(self_inner):
                return {"cpu": {"model": "fake-cpu"}, "gpu": {"devices": []}}

        return _Doc()

    monkeypatch.setattr(machine_profile, "collect", fake_collect)
    source = _source(tmp_path)
    source.snapshot()
    source.snapshot()

    assert len(calls) == 1, "TTL 内不应重复采集（Windows 上每次采集 = 3 次 powershell）"


def test_machine_profile_ttl_expires(tmp_path, monkeypatch):
    from djb_node import machine_profile

    calls = []
    monkeypatch.setattr(machine_profile, "collect",
                        lambda *, home=None, **kw: (calls.append(1), _ProfileDoc())[1])
    source = _source(tmp_path)
    source.PROFILE_TTL_S = 0.0                 # 立刻过期
    source.snapshot()
    source.snapshot()

    assert len(calls) == 2


class _ProfileDoc:
    def to_dict(self):
        return {"cpu": {"model": "fake-cpu"}, "gpu": {"devices": []}}


# ─────────────────────────────────────────── 进程内 CLI（开关，默认关）
def test_inproc_flag_off_still_spawns_subprocess(tmp_path, monkeypatch):
    monkeypatch.delenv("DJB_INPROC_CLI", raising=False)
    seen = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(cmd)
        return _Done(0, json.dumps({"state": "online"}), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ds.LocalDataSource(home=str(tmp_path), cloud_factory=lambda _h: None,
                                which=lambda _n: None)
    source.snapshot()

    assert any("djb_node" in " ".join(item) for item in seen), seen


def test_inproc_flag_on_needs_no_cli_subprocess(tmp_path, monkeypatch, home_store):
    monkeypatch.setenv("DJB_INPROC_CLI", "1")
    seen = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(cmd)
        raise AssertionError("进程内路径不该再起子进程：" + repr(cmd))

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ds.LocalDataSource(home=str(home_store), cloud_factory=lambda _h: None,
                                which=lambda _n: None)
    snapshot = source.snapshot()

    # 真 CLI 在进程内跑：status 域有真数据（daemon 没起 → stopped）
    status = snapshot.get("status")
    assert not isinstance(status, ds.Unavailable), status
    assert status.get("state") == "stopped"


class _Done:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def home_store(tmp_path):
    """一个**真**初始化过的 DJB_HOME（进程内 CLI 会真的读库）。"""
    from djb_node.config import Paths
    from djb_node.store import Store

    paths = Paths(str(tmp_path / "real"))
    paths.prepare()
    Store.create(paths.db_file, version="0.1.0").close()
    return paths.home


# ─────────────────────────────────────────── S3：home 只解析一次
def test_make_cli_runner_never_passes_none_as_home(tmp_path, monkeypatch):
    """``home=None`` 必须解析成真实目录：旧实现写 ``DJB_HOME=None``（字符串）。

    真机后果：action CLI 把 ``<cwd>/None`` 当数据目录 → 绑定 / 登出一律「本机未初始化」。
    """
    from djb_node.desktop import app as desktop_app

    monkeypatch.setenv("DJB_HOME", str(tmp_path))
    runner = desktop_app._make_cli_runner(None)
    seen = {}

    def fake_run(cmd, *args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return _Done(0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner(["status"])

    assert seen.get("DJB_HOME") == str(tmp_path)
    assert seen.get("DJB_HOME") != "None"


def test_make_controller_resolves_one_home_for_all_channels(tmp_path, monkeypatch):
    """装配层：数据源 / 接入配置 / 控制通道 / action CLI 必须是**同一个** home。"""
    from djb_node.desktop import app as desktop_app
    from djb_node.desktop.access import ACCESS_FILE

    monkeypatch.setenv("DJB_HOME", str(tmp_path))
    controller = desktop_app.make_controller(home=None, demo=False)

    assert str(controller.source.home) == str(tmp_path)
    assert os.fspath(controller.control._home) == str(tmp_path)
    assert str(controller.access._store.path) == os.path.join(str(tmp_path), ACCESS_FILE)

    seen = {}

    def fake_run(cmd, *args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return _Done(0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    controller.cli(["status"])
    assert seen.get("DJB_HOME") == str(tmp_path)


def test_local_control_channel_uses_resolve_home(monkeypatch, tmp_path):
    """控制通道不许写死 ``~/.djb``（Windows 默认 home 是 %APPDATA%\\DJB）。"""
    from djb_node.desktop.controller import LocalControlChannel

    monkeypatch.delenv("DJB_HOME", raising=False)
    channel = LocalControlChannel(None)
    from djb_node.config import resolve_home
    assert channel._home is None            # 构造不变
    # control() 里的 Paths 只在真正调用时才建：用假的 ControlClient 拦下来看 home
    from djb_node import daemon as daemon_mod

    captured = {}

    class FakeClient:
        def __init__(self, paths):
            captured["home"] = paths.home

        def call(self, payload, timeout=5.0):
            return {"ok": True, "status": {"state": "online"}}

    monkeypatch.setattr(daemon_mod, "ControlClient", FakeClient)
    monkeypatch.setattr("djb_node.tray.actions.apply_control",
                        lambda *a, **k: _Ctrl())
    channel.control("pause", lambda _m: True)
    assert captured["home"] == os.path.abspath(resolve_home())


class _Ctrl:
    verified = True
    message = "ok"
    state = "paused"
