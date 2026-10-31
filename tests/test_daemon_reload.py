# SPDX-License-Identifier: Apache-2.0
"""F14：配置热重载（`djb daemon reload` / SIGHUP / GUI 保存后调用）。

三条硬要求，逐条都有用例：

1. **幂等**：磁盘内容不变时重载任意多次结果一致（``changed`` 空、指纹相同）；
2. **坏配置回退**：解析/校验失败 → 内存里一个字段都不改，返回可读原因（不静默）；
3. **不中断在途任务**：在途计数、状态机、收据缓冲、监听端口都不因重载而变。

另外覆盖：端口改动只报「需重启」而不是谎报生效、SIGHUP 只置位由主循环执行、
daemon 已停止时拒绝重载、CLI 的三种返回、桌面端 reloader 的四态映射与失败原因透出。
"""
from __future__ import annotations

import json

import pytest

from djb_node import facts
from djb_node.config import ClientConfig, Paths, load_config, save_config
from djb_node.daemon import Daemon
from djb_node.errors import NotRunningError, UsageError
from djb_node.limits import Limiter, Signal, YieldDetector
from djb_node.secure import CredentialStore
from djb_node.store import Store


class FixedProbe:
    def read(self, _now):
        return Signal("fake", "idle", "test")


def make_daemon(paths: Paths, store: Store, credentials: CredentialStore, clock,
                config: ClientConfig | None = None) -> Daemon:
    config = config or ClientConfig()
    save_config(paths, config)
    return Daemon(paths, config=config, store=store, credentials=credentials,
                  local_token="t" * 40,
                  limiter=Limiter(store, detector=YieldDetector(probes=[FixedProbe()]),
                                  now=clock),
                  now=clock)


def write_config(paths: Paths, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    with open(paths.config_file, "w", encoding="utf-8") as handle:
        handle.write(text)


# ─────────────────────────────────────────────── ① 幂等
def test_reload_without_changes_is_idempotent(paths, store, credentials, clock):
    daemon = make_daemon(paths, store, credentials, clock,
                         ClientConfig(log_level="info", mirror_port=1717))
    first = daemon.reload_config()
    assert first["ok"] is True and first["changed"] == [] and first["unchanged"] is True
    second = daemon.reload_config()
    third = daemon.reload_config()
    assert second["changed"] == [] and third["changed"] == []
    assert second["fingerprint"] == first["fingerprint"] == third["fingerprint"]
    assert [second["reload_count"], third["reload_count"]] == [2, 3]
    assert daemon.config.log_level == "info"


def test_reload_applies_changed_fields_once(paths, store, credentials, clock):
    daemon = make_daemon(paths, store, credentials, clock, ClientConfig(log_level="info"))
    write_config(paths, {"log_level": "debug", "update_channel": "beta",
                         "mirror_port": 1717, "yield_enabled": False})
    result = daemon.reload_config()
    assert result["ok"] is True
    assert set(result["changed"]) == {"log_level", "update_channel", "yield_enabled"}
    assert daemon.config.log_level == "debug"
    assert daemon.config.update_channel == "beta"
    assert daemon.config.yield_enabled is False
    assert daemon.update.channel == "beta"          # 更新管理器同步换值
    # 第二次：磁盘没变 ⇒ 无变更
    again = daemon.reload_config()
    assert again["changed"] == [] and again["fingerprint"] == result["fingerprint"]


# ─────────────────────────────────────────────── ② 坏配置回退
@pytest.mark.parametrize("payload,bad_field", [
    ('{"log_level": "loud"}', "log_level"),
    ('{"mirror_port": 99999}', "mirror_port"),
    ('{"update_channel": "nightly"}', "update_channel"),
    ("{not json", "JSON"),
    ('["list"]', "顶层"),
])
def test_bad_config_keeps_old_values_and_reports(paths, store, credentials, clock,
                                                 payload, bad_field):
    daemon = make_daemon(paths, store, credentials, clock,
                         ClientConfig(log_level="info", mirror_port=1717,
                                      update_channel="stable"))
    write_config(paths, payload)
    result = daemon.reload_config()
    assert result["ok"] is False
    assert result["soft"] is True                    # 不是协议错误，是「结果是 否」
    assert result["reason"] == "invalid_config"
    assert bad_field in result["error"]              # 可读原因，指名道姓
    # 旧值原样保留（内存 + 对外回执都不得出现「已生效」）
    assert daemon.config.log_level == "info"
    assert daemon.config.mirror_port == 1717
    assert daemon.config.update_channel == "stable"
    assert result["kept"]["log_level"] == "info"
    assert "未生效" in result["message"]
    # 磁盘上的坏文件原样保留（用户能看到自己写错了什么，不被静默改写）
    assert open(paths.config_file, encoding="utf-8").read().strip() == payload.strip()
    # daemon 还能继续工作：修好后再重载即恢复
    write_config(paths, {"log_level": "debug", "mirror_port": 1717})
    fixed = daemon.reload_config()
    assert fixed["ok"] is True and fixed["changed"] == ["log_level"]
    assert daemon.config.log_level == "debug"


def test_reload_rejects_when_stopping(paths, store, credentials, clock):
    daemon = make_daemon(paths, store, credentials, clock)
    daemon._stop_event.set()
    result = daemon.reload_config()
    assert result["ok"] is False and result["reason"] == "stopping"


# ─────────────────────────────────────────────── ③ 不中断在途任务
def test_reload_keeps_inflight_state_and_receipts(paths, store, credentials, clock):
    daemon = make_daemon(paths, store, credentials, clock, ClientConfig(log_level="info"))
    daemon.begin_task()
    daemon.begin_task()
    state_before = daemon.machine.snapshot.state
    receipts_before = daemon.buffer.count()
    write_config(paths, {"log_level": "debug", "mirror_port": 1717})
    result = daemon.reload_config()
    assert result["ok"] is True
    assert daemon._inflight == 2 and result["inflight"] == 2
    assert daemon.machine.snapshot.state == state_before
    assert daemon.buffer.count() == receipts_before
    assert daemon.server.port == daemon.port            # 监听端口不动
    daemon.end_task()
    daemon.end_task()
    assert daemon._inflight == 0


def test_port_change_reports_restart_required_without_lying(paths, store, credentials,
                                                             clock):
    daemon = make_daemon(paths, store, credentials, clock,
                         ClientConfig(mirror_port=1717, log_level="info"))
    write_config(paths, {"mirror_port": 1718, "log_level": "debug"})
    result = daemon.reload_config()
    assert result["ok"] is True
    assert result["changed"] == ["log_level"]           # 端口不算「已生效」
    assert result["config"]["mirror_port"] == 1717
    assert daemon.config.mirror_port == 1717
    item = result["restart_required"][0]
    assert item["field"] == "mirror_port" and item["from"] == 1717 and item["to"] == 1718
    assert "重启" in item["reason"]


# ─────────────────────────────────────────────── SIGHUP
def test_sighup_sets_flag_and_reload_runs_in_main_loop(paths, store, credentials, clock):
    daemon = make_daemon(paths, store, credentials, clock, ClientConfig(log_level="info"))
    write_config(paths, {"log_level": "debug", "mirror_port": 1717})
    daemon._on_sighup()                                  # 信号上下文只置位
    assert daemon._reload_requested is True
    assert daemon.config.log_level == "info"             # 置位本身不加载
    result = daemon._consume_reload_flag()
    assert result is not None and result["ok"] is True
    assert daemon._reload_requested is False
    assert daemon.config.log_level == "debug"            # 主循环里真正生效
    assert daemon._consume_reload_flag() is None         # 已消费：不重复加载


def test_reload_signal_install_is_safe(paths, store, credentials, clock):
    daemon = make_daemon(paths, store, credentials, clock)
    installed = daemon._install_reload_signal()
    assert isinstance(installed, bool)                   # Windows/受限环境返回 False
    import signal
    import threading
    if installed:
        assert signal.getsignal(signal.SIGHUP) is not signal.SIG_DFL
        signal.signal(signal.SIGHUP, signal.SIG_DFL)     # 复原，别影响别的用例


# ─────────────────────────────────────────────── CLI
def test_cli_reload_when_daemon_not_running(paths, monkeypatch, capsys):
    from djb_node import cli

    class Down:
        def __init__(self, _paths):
            pass

        def call(self, _command, *, timeout=5.0):
            raise NotRunningError("daemon 未运行（控制套接字不存在）：先 djb start")

    monkeypatch.setattr(cli, "ControlClient", Down)
    code = cli.main(["--home", str(paths.home), "daemon", "reload"])
    captured = capsys.readouterr()
    assert code == facts.EXIT_OK
    assert "下次 djb start 生效" in captured.out
    assert "Traceback" not in captured.err


def test_cli_reload_old_daemon_reports_unsupported(paths, monkeypatch, capsys):
    from djb_node import cli

    class Old:
        def __init__(self, _paths):
            pass

        def call(self, _command, *, timeout=5.0):
            raise UsageError("未知控制命令：reload")

    monkeypatch.setattr(cli, "ControlClient", Old)
    code = cli.main(["--home", str(paths.home), "reload"])
    captured = capsys.readouterr()
    assert code == facts.EXIT_USAGE
    assert "没有 reload 命令" in captured.err


def test_cli_reload_surfaces_bad_config_reason(paths, monkeypatch, capsys):
    from djb_node import cli

    class Soft:
        def __init__(self, _paths):
            pass

        def call(self, _command, *, timeout=5.0):
            return {"ok": False, "soft": True, "reason": "invalid_config",
                    "error": "log_level 必须是 info/debug（当前 'loud'）"}

    monkeypatch.setattr(cli, "ControlClient", Soft)
    code = cli.main(["--home", str(paths.home), "daemon", "reload"])
    captured = capsys.readouterr()
    assert code == facts.EXIT_VERIFY
    assert "log_level 必须是 info/debug" in captured.err   # 原因必须露出来
    assert "已保留旧配置" in captured.err


def test_cli_reload_success_text(paths, monkeypatch, capsys):
    from djb_node import cli

    class Ok:
        def __init__(self, _paths):
            pass

        def call(self, _command, *, timeout=5.0):
            return {"ok": True, "changed": ["log_level"], "applied": ["log_level"],
                    "restart_required": [], "notes": [], "upstreams": 2, "inflight": 1,
                    "fingerprint": "abc123", "reload_count": 1,
                    "message": "配置已重载并生效：log_level"}

    monkeypatch.setattr(cli, "ControlClient", Ok)
    assert cli.main(["--home", str(paths.home), "daemon", "reload"]) == facts.EXIT_OK
    out = capsys.readouterr().out
    assert "配置已重载并生效：log_level" in out
    assert "上游地址：2 条" in out and "在途任务：1" in out


# ─────────────────────────────────────────────── 桌面端（GUI 同一条路径）
def test_desktop_reloader_maps_daemon_outcomes(paths, monkeypatch):
    from djb_node import daemon as daemon_mod
    from djb_node.desktop.access import (RELOAD_DAEMON_DOWN, RELOAD_FAILED, RELOAD_OK,
                                         RELOAD_UNSUPPORTED)
    from djb_node.desktop.app import _make_reloader

    class Client:
        payload: object = None

        def __init__(self, _paths):
            pass

        def call(self, _command, *, timeout=5.0):
            if isinstance(Client.payload, BaseException):
                raise Client.payload
            return dict(Client.payload or {})

    monkeypatch.setattr(daemon_mod, "ControlClient", Client)
    reloader = _make_reloader(paths.home)

    Client.payload = {"ok": True, "changed": ["log_level"], "message": "配置已重载"}
    assert reloader() == (RELOAD_OK, "已重载：log_level")

    Client.payload = {"ok": True, "changed": []}
    assert reloader() == (RELOAD_OK, "配置无变化")

    Client.payload = {"ok": False, "soft": True, "error": "log_level 必须是 info/debug"}
    state, detail = reloader()
    assert state == RELOAD_FAILED and "log_level" in detail

    Client.payload = UsageError("未知控制命令：reload")
    assert reloader() == (RELOAD_UNSUPPORTED, "")

    Client.payload = NotRunningError("daemon 未运行")
    assert reloader() == (RELOAD_DAEMON_DOWN, "")


def test_desktop_save_message_carries_reload_failure_reason(tmp_path):
    from djb_node.desktop.access import (AccessConfig, AccessService, MemoryAccessStore,
                                         RELOAD_FAILED, RELOAD_OK)

    config = AccessConfig(node_id="node-1", node_secret="djb_secret_abcd1234",
                          price_mode="auto", usdt_address="")
    store = MemoryAccessStore()

    service = AccessService(store, reloader=lambda: (RELOAD_FAILED, "配置未生效：log_level 非法"))
    result = service.save(config)
    assert result.saved and result.readback_ok
    assert result.reload == RELOAD_FAILED
    assert result.effective is False                     # 不许说「已生效」
    assert "log_level 非法" in result.message             # 原因露出来，不静默
    assert result.detail == "配置未生效：log_level 非法"

    ok_service = AccessService(MemoryAccessStore(),
                               reloader=lambda: (RELOAD_OK, "已重载：log_level"))
    ok = ok_service.save(config)
    assert ok.effective is True and ok.reload == RELOAD_OK
    assert "配置生效" in ok.message
