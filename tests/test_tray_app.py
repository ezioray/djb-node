# SPDX-License-Identifier: Apache-2.0
"""C3/C4 托盘壳（``djb_node.tray.app``）：菜单渲染、intent 路由、刷新节奏、无头降级。

**全程不创建 Tk**：GUI 后端是 :class:`FakeBackend`（只记录调用），所以无头 CI 也能跑。
口径以 :mod:`djb_node.tray.model` / :mod:`djb_node.tray.notifications` 为准，本文件只断言
「壳把模型结果原样交给后端」「壳把点击变成正确的 intent」「壳定时刷新且只在 QUIT 停」
「无图形环境返回 3」，不重复断言模型已有的投影用例。
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

import pytest

from djb_node.errors import UsageError
from djb_node.tray import TrayAction, TrayIntent, TrayModel
from djb_node.tray import app as app_mod
from djb_node.tray.app import Backend, HeadlessError, TrayApp, main
from djb_node.tray.model import EMPTY_VALUE, INTENTS, S2_MENU_SIZE
from djb_node.tray.notifications import summarize

#: 六项菜单的稳定顺序（壳不得增删 / 重排）
EXPECTED_KEYS = ("status", "pause", "messages", "diagnose", "logs", "quit")

RUNNING = {"state": "online", "daemon_running": True, "accepting": True,
           "balance": "12.34", "today_earnings": "1.20"}
PAUSED = {"state": "paused", "daemon_running": True, "accepting": False,
          "reason": "user_paused", "reason_label": "用户暂停"}
STOPPED = {"state": "stopped", "daemon_running": False, "accepting": False}
ROWS = [
    {"id": "n-1", "severity": "P0", "title": "配额已用尽",
     "created_at": "2026-10-24T03:00:00Z", "read_at": None},
    {"id": "n-2", "severity": "P1", "title": "有新版本可更新",
     "created_at": "2026-10-24T02:00:00Z", "read_at": None},
    {"id": "n-3", "severity": "P2", "title": "通知",
     "created_at": "2026-10-24T01:00:00Z", "read_at": "2026-10-24T02:30:00Z"},
]


# ─────────────────────────────────────────────────────────── 替身
class FakeBackend:
    """GUI 替身：记录调用序列，不建窗口（测试因此不需要 Tk / DISPLAY）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.handler: Callable[[str], None] | None = None
        self.title: str | None = None
        self.badge: str | None = None
        self.items: tuple[Any, ...] = ()
        self.timers: list[tuple[int, Callable[[], None]]] = []
        self.ran = 0
        self.stopped = 0

    # ── Backend 协议
    def set_activate_handler(self, handler: Callable[[str], None]) -> None:
        self.handler = handler
        self.calls.append(("set_activate_handler",))

    def set_title(self, text: str) -> None:
        self.title = text
        self.calls.append(("set_title", text))

    def set_menu_items(self, items: Sequence[Any]) -> None:
        self.items = tuple(items)
        self.calls.append(("set_menu_items", tuple(item.key for item in items)))

    def set_badge(self, text: str | None) -> None:
        self.badge = text
        self.calls.append(("set_badge", text))

    def after(self, delay_ms: int, callback: Callable[[], None]) -> str:
        self.timers.append((delay_ms, callback))
        self.calls.append(("after", delay_ms))
        return "timer-" + str(len(self.timers))

    def run(self) -> None:
        self.ran += 1
        self.calls.append(("run",))

    def stop(self) -> None:
        self.stopped += 1
        self.calls.append(("stop",))

    # ── 测试辅助
    def click(self, key: str) -> None:
        assert self.handler is not None, "壳没有登记点击回调"
        self.handler(key)

    def fire(self) -> None:
        assert self.timers, "没有待触发的定时器"
        self.timers.pop(0)[1]()

    @property
    def keys(self) -> list[str]:
        return [item.key for item in self.items]


class Sink:
    """intent_sink 替身：只记账，不执行。"""

    def __init__(self) -> None:
        self.intents: list[TrayIntent] = []

    def __call__(self, intent: TrayIntent) -> None:
        self.intents.append(intent)

    @property
    def actions(self) -> list[TrayAction]:
        return [intent.action for intent in self.intents]


def make_app(status: Any = None, rows: Any = None, *, interval_ms: int = 5000,
             status_provider: Callable[[], Any] | None = None,
             notifications_provider: Callable[[], Any] | None = None
             ) -> tuple[TrayApp, FakeBackend, Sink]:
    """装一个壳 + 替身后端；清掉构造期的调用，让 ``calls`` 只反映刷新行为。"""
    backend = FakeBackend()
    sink = Sink()
    app = TrayApp(backend,
                  status_provider or (lambda: status),
                  notifications_provider or (lambda: rows),
                  sink, interval_ms=interval_ms)
    backend.calls.clear()
    return app, backend, sink


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=["djb"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


# ─────────────────────────────────────────────────────────── 协议 / 构造
def test_fake_backend_satisfies_backend_protocol():
    assert isinstance(FakeBackend(), Backend)


def test_construction_registers_click_handler():
    backend = FakeBackend()
    app = TrayApp(backend, lambda: RUNNING, lambda: ROWS, Sink())
    assert backend.calls == [("set_activate_handler",)]
    assert backend.handler == app.activate          # 绑定方法：同一实例同一函数
    assert app.model is None and app.closed is False


@pytest.mark.parametrize("bad", [0, -1, 1.5, "5000", True])
def test_interval_must_be_positive_int(bad):
    with pytest.raises(UsageError):
        TrayApp(FakeBackend(), lambda: RUNNING, lambda: ROWS, Sink(), interval_ms=bad)


# ─────────────────────────────────────────────────────────── 菜单 / 标题 / 角标
def test_menu_is_six_items_in_model_order():
    app, backend, _ = make_app(RUNNING, ROWS)
    model = app.refresh()
    assert len(backend.items) == S2_MENU_SIZE
    assert backend.keys == list(EXPECTED_KEYS)
    assert backend.items == model.items             # 壳只搬运，不改 label / enabled / badge
    assert tuple(backend.keys) == model.menu_keys


def test_refresh_twice_does_not_duplicate_items():
    app, backend, _ = make_app(RUNNING, ROWS)
    app.refresh()
    app.refresh()
    assert backend.keys == list(EXPECTED_KEYS)      # 整份重绘，不是追加


def test_pause_and_resume_are_exclusive_through_shell():
    paused_app, paused_backend, _ = make_app(PAUSED, ())
    paused_app.refresh()
    assert "resume" in paused_backend.keys
    assert "pause" not in paused_backend.keys

    running_app, running_backend, _ = make_app(RUNNING, ())
    running_app.refresh()
    assert "pause" in running_backend.keys
    assert "resume" not in running_backend.keys


def test_title_is_the_model_status_line():
    app, backend, _ = make_app(RUNNING, ())
    model = app.refresh()
    assert backend.title == model.status_line()
    assert "余额 12.34" in backend.title


def test_missing_status_fields_reach_shell_as_dash():
    app, backend, _ = make_app({}, ())
    app.refresh()
    assert backend.title.count(EMPTY_VALUE) == 2     # 余额 / 今日收益都缺 → 两个「—」
    assert EMPTY_VALUE in backend.title


def test_badge_is_the_model_badge():
    app, backend, _ = make_app(RUNNING, ROWS)
    model = app.refresh()
    assert backend.badge == "!2"                     # 2 条未读里有 1 条 P0（口径来自 summarize）
    assert backend.badge == model.item("messages").badge


def test_badge_cleared_when_nothing_unread():
    app, backend, _ = make_app(RUNNING, [])
    app.refresh()
    assert backend.badge is None


def test_mapping_summary_is_used_as_is():
    # 已经是投影的映射（例如 from_store 的结果）直接交给模型，壳不重新计数
    app, backend, _ = make_app(RUNNING, (), notifications_provider=lambda: {"unread": 4,
                                                                           "p0_unread": 0})
    app.refresh()
    assert backend.badge == "4"


def test_shell_projects_rows_with_summarize():
    app, backend, _ = make_app(RUNNING, ROWS)
    model = app.refresh()
    assert backend.badge == model.item("messages").badge
    assert model.unread == summarize(ROWS)["unread"] == 2       # 3 行里 1 行已读


# ─────────────────────────────────────────────────────────── 点击 → intent
@pytest.mark.parametrize("key, action, command", [
    ("status", TrayAction.OPEN_STATUS, ("djb", "status", "--json")),
    ("pause", TrayAction.PAUSE, ("djb", "pause")),
    ("messages", TrayAction.OPEN_MESSAGES, ("djb", "notify", "list", "--json")),
    ("diagnose", TrayAction.DIAGNOSE, ("djb", "diagnose", "--json")),
    ("logs", TrayAction.OPEN_LOGS, ("djb", "logs", "--tail", "200")),
])
def test_click_sends_intent_to_sink(key, action, command):
    app, backend, sink = make_app(RUNNING, ROWS)
    app.refresh()
    intent = app.activate(key)
    assert sink.actions == [action]
    assert sink.intents[0] == INTENTS[action]
    assert sink.intents[0].command == command
    assert intent is sink.intents[0]                 # 发给 sink 的与返回的是同一个 intent


def test_backend_callback_is_routed_to_activate():
    app, backend, sink = make_app(RUNNING, ROWS)
    app.refresh()
    backend.click("diagnose")                        # 后端回调 = 用户点菜单
    assert sink.actions == [TrayAction.DIAGNOSE]


def test_click_resume_when_paused():
    app, backend, sink = make_app(PAUSED, ())
    app.refresh()
    backend.click("resume")
    assert sink.actions == [TrayAction.RESUME]
    assert sink.intents[0].command == ("djb", "resume")


def test_click_on_disabled_item_sends_nothing():
    app, backend, sink = make_app(STOPPED, ())
    app.refresh()
    backend.click("pause")
    assert sink.intents == []
    assert backend.stopped == 0


def test_click_unknown_key_sends_nothing():
    app, backend, sink = make_app(RUNNING, ())
    app.refresh()
    backend.click("nope")
    assert sink.intents == []


def test_click_before_first_refresh_sends_nothing():
    app, backend, sink = make_app(RUNNING, ROWS)
    backend.click("status")
    assert sink.intents == []


def test_quit_intent_stops_loop_and_executes_nothing(monkeypatch):
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("壳不得执行任何命令")
    monkeypatch.setattr(app_mod.subprocess, "run", forbidden)
    app, backend, sink = make_app(RUNNING, ROWS)
    app.refresh()
    backend.click("quit")
    assert sink.actions == [TrayAction.QUIT]
    assert sink.intents[0].command == ()             # QUIT 没有建议命令
    assert backend.stopped == 1
    assert app.closed is True


def test_other_intents_do_not_stop_loop():
    app, backend, sink = make_app(RUNNING, ROWS)
    app.refresh()
    backend.click("logs")
    assert sink.actions == [TrayAction.OPEN_LOGS]
    assert backend.stopped == 0 and app.closed is False


def test_sink_failure_does_not_break_shell():
    def boom(_intent: TrayIntent) -> None:
        raise RuntimeError("disk full")
    backend = FakeBackend()
    app = TrayApp(backend, lambda: RUNNING, lambda: ROWS, boom)
    app.refresh()
    backend.click("diagnose")                        # 不抛
    assert app.model is not None and app.closed is False


def test_quit_still_closes_when_sink_fails():
    def boom(_intent: TrayIntent) -> None:
        raise RuntimeError("disk full")
    backend = FakeBackend()
    app = TrayApp(backend, lambda: RUNNING, lambda: ROWS, boom)
    app.refresh()
    backend.click("quit")
    assert backend.stopped == 1 and app.closed is True


def test_click_after_close_sends_nothing():
    app, backend, sink = make_app(RUNNING, ROWS)
    app.refresh()
    app.close()
    backend.click("status")
    assert sink.intents == []


# ─────────────────────────────────────────────────────────── 刷新节奏（调用序列快照）
def test_refresh_call_sequence_is_stable():
    app, backend, _ = make_app(RUNNING, ROWS)
    model = app.refresh()
    assert backend.calls == [
        ("set_title", model.status_line()),
        ("set_menu_items", EXPECTED_KEYS),
        ("set_badge", "!2"),
    ]


def test_run_refreshes_then_schedules_then_blocks():
    app, backend, _ = make_app(RUNNING, ROWS, interval_ms=1000)
    app.run()
    assert [call[0] for call in backend.calls] == [
        "set_title", "set_menu_items", "set_badge", "after", "run"]
    assert backend.timers[0][0] == 1000
    assert backend.ran == 1


def test_default_interval_is_5000():
    app, backend, _ = make_app(RUNNING, ROWS)
    assert app.interval_ms == 5000
    app.run()
    assert [delay for delay, _callback in backend.timers] == [5000]


def test_timer_refresh_reschedules_itself():
    app, backend, _ = make_app(RUNNING, ROWS, interval_ms=5000)
    app.run()
    backend.calls.clear()
    backend.fire()
    assert [call[0] for call in backend.calls] == [
        "set_title", "set_menu_items", "set_badge", "after"]
    # fire() 消费掉旧句柄，只留新排的一次；再 fire 一次应再排一次
    assert [delay for delay, _callback in backend.timers] == [5000]
    backend.fire()
    assert [delay for delay, _callback in backend.timers] == [5000]


def test_close_is_idempotent_and_stops_rescheduling():
    app, backend, _ = make_app(RUNNING, ROWS)
    app.run()
    pending = backend.timers[-1][1]
    app.close()
    app.close()
    assert backend.stopped == 1                      # 只 stop 一次
    backend.calls.clear()
    pending()                                        # 已关闭：定时器回调空转
    assert backend.calls == []
    assert backend.timers[-1][0] == 5000             # 也不再新排定时器


# ─────────────────────────────────────────────────────────── provider 容错
def test_status_provider_failure_degrades_to_stopped():
    def boom() -> Any:
        raise RuntimeError("daemon gone")
    backend = FakeBackend()
    app = TrayApp(backend, boom, lambda: (), Sink())
    model = app.refresh()
    assert model.state == "stopped"                  # 模型保守兜底 → 壳照常渲染
    assert backend.title == model.status_line()
    assert backend.keys == list(EXPECTED_KEYS)
    assert backend.stopped == 0


def test_notifications_provider_failure_clears_badge():
    def boom() -> Any:
        raise RuntimeError("db broken")
    app, backend, _ = make_app(RUNNING, (), notifications_provider=boom)
    app.refresh()
    assert backend.badge is None


@pytest.mark.parametrize("value", [None, 42, "boom", [1, None, "s"], (None,)])
def test_notifications_provider_tolerates_odd_values(value):
    app, backend, _ = make_app(RUNNING, (), notifications_provider=lambda: value)
    app.refresh()
    assert backend.badge is None


def test_status_provider_odd_value_falls_back_to_empty():
    app, backend, _ = make_app(status_provider=lambda: "not-a-mapping")
    app.refresh()
    assert backend.title.count(EMPTY_VALUE) == 2


# ─────────────────────────────────────────────────────────── 只读 CLI 采集
def test_cli_status_reads_json_read_only(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        return completed('{"state": "online", "daemon_running": true}')

    monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
    assert app_mod.cli_status() == {"state": "online", "daemon_running": True}
    assert seen["command"] == [sys.executable, "-m", "djb_node", "status", "--json"]
    assert seen["kwargs"]["timeout"] > 0
    assert seen["kwargs"]["check"] is False          # 非零退出自己判，不抛


@pytest.mark.parametrize("failure", ["nonzero", "bad_json", "oserror", "timeout", "empty"])
def test_cli_status_degrades_to_stopped(monkeypatch, failure):
    if failure == "nonzero":
        monkeypatch.setattr(app_mod.subprocess, "run",
                            lambda *a, **k: completed("boom", returncode=2, stderr="x"))
    elif failure == "bad_json":
        monkeypatch.setattr(app_mod.subprocess, "run",
                            lambda *a, **k: completed("not json"))
    elif failure == "oserror":
        def raise_os(*_a, **_k):
            raise OSError("no python")
        monkeypatch.setattr(app_mod.subprocess, "run", raise_os)
    elif failure == "timeout":
        def raise_timeout(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="djb", timeout=1)
        monkeypatch.setattr(app_mod.subprocess, "run", raise_timeout)
    else:
        monkeypatch.setattr(app_mod.subprocess, "run", lambda *a, **k: completed(""))
    assert app_mod.cli_status() == {"state": "stopped"}


def test_cli_notifications_reads_items(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return completed(json.dumps({"items": ROWS, "count": 3, "unread": 2}))

    monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
    rows = app_mod.cli_notifications()
    assert [row["id"] for row in rows] == ["n-1", "n-2", "n-3"]
    assert seen["command"][:5] == [sys.executable, "-m", "djb_node", "notify", "list"]


@pytest.mark.parametrize("stdout, returncode", [("nope", 1), ("[]", 0), ("{}", 0)])
def test_cli_notifications_degrades_to_empty(monkeypatch, stdout, returncode):
    monkeypatch.setattr(app_mod.subprocess, "run",
                        lambda *a, **k: completed(stdout, returncode=returncode))
    assert app_mod.cli_notifications() == ()


def test_end_to_end_main_with_injected_backend(monkeypatch):
    """真实入口的接线：只读采集 → 壳 → 后端；后端替身让 main 立即返回。"""
    backend = FakeBackend()
    monkeypatch.setattr(app_mod, "_make_backend", lambda *, title: backend)
    monkeypatch.setattr(app_mod, "_setup_logging", lambda: None)
    monkeypatch.setattr(app_mod, "cli_status", lambda: RUNNING)
    monkeypatch.setattr(app_mod, "cli_notifications", lambda: ROWS)
    assert main([]) == 0
    assert backend.ran == 1
    assert backend.keys == list(EXPECTED_KEYS) and backend.badge == "!2"
    backend.click("diagnose")
    backend.click("quit")
    assert backend.stopped == 1


# ─────────────────────────────────────────────────────────── 默认 sink（不执行）
class Capture(logging.Handler):
    """抓结构化事件（``logging_setup.event`` 走 "djb" logger）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({"event": getattr(record, "event", None),
                             "fields": getattr(record, "fields", {}) or {},
                             "level": record.levelname})


def test_default_sink_logs_suggested_command_without_executing(monkeypatch, capsys):
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("intent_sink 不得执行命令")
    monkeypatch.setattr(app_mod.subprocess, "run", forbidden)

    logger = logging.getLogger("djb")
    previous = logger.level
    handler = Capture()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        app_mod._default_intent_sink(INTENTS[TrayAction.PAUSE])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert [item["event"] for item in handler.records] == ["tray.intent"]
    assert handler.records[0]["fields"]["command"] == "djb pause"
    assert handler.records[0]["fields"]["action"] == "pause"
    out = capsys.readouterr().out
    assert "djb pause" in out and "不自动执行" in out


# ─────────────────────────────────────────────────────────── 无头降级
def test_main_returns_3_without_tkinter(monkeypatch, capsys):
    monkeypatch.setattr(app_mod, "_has_display", lambda: True)

    def no_tk() -> Any:
        raise ImportError("No module named 'tkinter'")
    monkeypatch.setattr(app_mod, "_import_tk", no_tk)

    assert main([]) == app_mod.EXIT_HEADLESS == 3
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1                            # 一行中文说明
    assert "图形" in lines[0] and "tkinter" in lines[0]
    assert "Traceback" not in captured.out + captured.err


def test_main_returns_3_without_display(monkeypatch, capsys):
    monkeypatch.setattr(app_mod, "_has_display", lambda: False)

    def forbidden() -> Any:
        raise AssertionError("无头环境不应 import tkinter")
    monkeypatch.setattr(app_mod, "_import_tk", forbidden)

    assert main([]) == 3
    out = capsys.readouterr().out.strip()
    assert "DISPLAY" in out and "\n" not in out
    assert "Traceback" not in out


def test_main_returns_3_when_window_cannot_open(monkeypatch, capsys):
    """tkinter 装了但窗口打不开（无 X 会话）：同样降级为 3，不抛栈。"""
    class BrokenTk:
        class TclError(Exception):
            pass

        def Tk(self) -> Any:
            raise RuntimeError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(app_mod, "_has_display", lambda: True)
    monkeypatch.setattr(app_mod, "_import_tk", lambda: BrokenTk())
    with pytest.raises(HeadlessError):
        app_mod._make_backend(title="t")
    assert main([]) == 3
    assert "Traceback" not in capsys.readouterr().out


def test_tk_dependent_imports_stay_lazy():
    """模块导入不碰 GUI：tkinter 只在 _import_tk 里延迟导入，顶层只有标准库。"""
    tree = ast.parse(inspect.getsource(app_mod))
    # 只看模块顶层（函数体内的 import 是刻意的延迟导入）
    imported = {alias.name.split(".")[0] for node in tree.body
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in tree.body
                 if isinstance(node, ast.ImportFrom) and not node.level}
    assert "tkinter" not in imported
    assert not imported - set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert not hasattr(app_mod, "tkinter")            # 延迟导入不进模块命名空间
    for forbidden in ("pystray", "rumps", "PyQt5", "PyQt6", "PySide2", "PySide6"):
        assert forbidden not in imported
