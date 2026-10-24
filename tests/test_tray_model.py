# SPDX-License-Identifier: Apache-2.0
"""C3 托盘模型：六项菜单、状态投影、角标、action intent、消息投影（纯函数，无 GUI）。"""
from __future__ import annotations

import ast
import copy
import inspect
import json

import pytest

from djb_node.errors import UsageError
from djb_node.tray import TrayAction, TrayIntent, TrayMenuItem, TrayModel
from djb_node.tray import model as model_mod
from djb_node.tray import notifications as tray_notify
from djb_node.tray.notifications import from_store, summarize, title_of

RUNNING = {"state": "online", "daemon_running": True, "accepting": True,
           "balance": "12.34", "today_earnings": "1.20"}
PAUSED = {"state": "paused", "daemon_running": True, "accepting": False,
          "reason": "user_paused", "reason_label": "用户暂停", "next_step": "djb resume"}
STOPPED = {"state": "stopped", "daemon_running": False, "accepting": False}
ERROR = {"state": "error", "daemon_running": True, "reason": "credential_invalid"}


# ─────────────────────────────────────────────────────────── 菜单结构
def test_menu_has_exactly_six_s2_items():
    tray = TrayModel.build(RUNNING)
    assert len(tray.items) == 6
    assert tray.menu_keys == ("status", "pause", "messages", "diagnose", "logs", "quit")
    assert [item.action for item in tray.items] == [
        TrayAction.OPEN_STATUS, TrayAction.PAUSE, TrayAction.OPEN_MESSAGES,
        TrayAction.DIAGNOSE, TrayAction.OPEN_LOGS, TrayAction.QUIT]


def test_s3_entries_are_absent():
    tray = TrayModel.build(RUNNING, {"unread": 1, "p0_unread": 0})
    text = tray.render_json() + tray.render_text()
    for forbidden in ("上游", "模型同步", "抽检", "收益详情", "设置", "relay", "attest",
                      "model-sync"):
        assert forbidden not in text


def test_paused_state_shows_resume_instead_of_pause():
    tray = TrayModel.build(PAUSED)
    assert tray.item("pause") is None
    resume = tray.item("resume")
    assert resume is not None and resume.enabled is True
    assert resume.label == "恢复接单"
    assert tray.menu_keys[1] == "resume"


@pytest.mark.parametrize("status", [STOPPED, ERROR])
def test_pause_is_disabled_when_not_running(status):
    tray = TrayModel.build(status)
    pause = tray.item("pause")
    assert pause is not None and pause.enabled is False


def test_pause_disabled_when_daemon_not_running():
    tray = TrayModel.build({"state": "online", "daemon_running": False})
    assert tray.item("pause").enabled is False


def test_quit_and_read_only_items_always_enabled():
    tray = TrayModel.build({})
    for key in ("status", "messages", "diagnose", "logs", "quit"):
        assert tray.item(key).enabled is True


# ─────────────────────────────────────────────────────────── 状态投影
@pytest.mark.parametrize("raw, expected_state, expected_label", [
    ("online", "running", "运行中"),
    ("starting", "running", "启动中"),
    ("paused", "paused", "已暂停"),
    ("updating", "paused", "更新中"),
    ("stopped", "stopped", "已停止"),
    ("error", "error", "错误"),
])
def test_state_projection_table(raw, expected_state, expected_label):
    tray = TrayModel.build({"state": raw, "daemon_running": True})
    assert tray.state == expected_state
    assert tray.state_label == expected_label
    assert tray.raw_state == raw


@pytest.mark.parametrize("status", [{}, {"state": ""}, {"state": "meltdown"}])
def test_missing_or_unknown_state_falls_back_to_stopped(status):
    tray = TrayModel.build(status)
    assert tray.state == "stopped"
    assert tray.daemon_running is False


def test_reason_label_falls_back_to_state_table():
    tray = TrayModel.build(ERROR)
    assert tray.reason == "credential_invalid"
    assert tray.reason_label == "凭据失效"
    assert "凭据失效" in tray.status_line()


def test_next_step_is_exposed():
    tray = TrayModel.build(PAUSED)
    assert tray.next_step == "djb resume"
    assert "下一步：djb resume" in tray.render_text()


# ─────────────────────────────────────────────────────────── 余额 / 收益（只展示）
def test_balance_and_earnings_are_server_values_only():
    tray = TrayModel.build(RUNNING)
    assert tray.balance == "12.34"
    assert tray.today_earnings == "1.20"
    assert "余额 12.34" in tray.status_line()
    assert "今日收益 1.20" in tray.status_line()


@pytest.mark.parametrize("raw, expected", [
    (None, "—"),
    ("", "—"),
    ("   ", "—"),
    ("0.001", "0.001"),
    (12, "12"),
    (True, "—"),
])
def test_missing_money_fields_show_dash_without_computation(raw, expected):
    tray = TrayModel.build({"state": "online", "balance": raw, "today_earnings": raw})
    assert tray.balance == expected
    assert tray.today_earnings == expected


def test_money_values_are_never_rounded_or_reformatted():
    tray = TrayModel.build({"state": "online", "balance": "999999999999.000001",
                            "today_earnings": "+0.1"})
    assert tray.balance == "999999999999.000001"
    assert tray.today_earnings == "+0.1"


# ─────────────────────────────────────────────────────────── 角标
def test_badge_without_p0_is_plain_count():
    tray = TrayModel.build(RUNNING, {"unread": 3, "p0_unread": 0})
    assert tray.item("messages").badge == "3"
    assert tray.unread == 3 and tray.p0_unread == 0


def test_badge_with_p0_gets_exclamation_prefix():
    tray = TrayModel.build(RUNNING, {"unread": 2, "p0_unread": 1})
    assert tray.item("messages").badge == "!2"
    assert "[!2]" in tray.render_text()


def test_zero_unread_has_no_badge():
    tray = TrayModel.build(RUNNING, {"unread": 0, "p0_unread": 0})
    assert tray.item("messages").badge is None
    tray_none = TrayModel.build(RUNNING, None)
    assert tray_none.unread == 0 and tray_none.item("messages").badge is None


def test_notification_count_weak_aliases():
    tray = TrayModel.build(RUNNING, {"unread_count": "4", "p0": 2})
    assert tray.unread == 4 and tray.p0_unread == 2
    assert tray.item("messages").badge == "!4"


# ─────────────────────────────────────────────────────────── intent（不执行）
def test_intents_are_plain_data():
    tray = TrayModel.build(RUNNING)
    status = tray.intent_for("status")
    assert status.action is TrayAction.OPEN_STATUS
    assert status.command == ("djb", "status", "--json")
    assert status.requires_daemon is False

    pause = tray.intent_for("pause")
    assert pause.action is TrayAction.PAUSE
    assert pause.command == ("djb", "pause")
    assert pause.requires_daemon is True


def test_quit_intent_does_not_stop_daemon():
    tray = TrayModel.build(RUNNING)
    quit_intent = tray.intent_for("quit")
    assert quit_intent.action is TrayAction.QUIT
    assert quit_intent.command == ()
    assert "不改变 daemon 状态" in quit_intent.description


def test_disabled_and_unknown_items_have_no_intent():
    tray = TrayModel.build(STOPPED)
    assert tray.intent_for("pause") is None
    assert tray.intent_for("nope") is None


def test_intent_accepts_action_enum():
    tray = TrayModel.build(PAUSED)
    assert tray.intent_for(TrayAction.RESUME).command == ("djb", "resume")
    assert tray.intent_for(TrayAction.PAUSE) is None


def test_tray_modules_import_no_gui_or_process_control():
    forbidden = {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "gi",
                 "subprocess", "os"}
    for module in (model_mod, tray_notify):
        tree = ast.parse(inspect.getsource(module))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom)}
        assert not (imported & forbidden), (module.__name__, imported & forbidden)
    assert not hasattr(model_mod, "tkinter")
    assert not hasattr(tray_notify, "tkinter")


def test_menu_item_and_intent_validate_inputs():
    with pytest.raises(UsageError):
        TrayMenuItem("", "label", TrayAction.QUIT)
    with pytest.raises(UsageError):
        TrayMenuItem("key", "  ", TrayAction.QUIT)
    with pytest.raises(UsageError):
        TrayMenuItem("key", "label", "quit")              # type: ignore[arg-type]
    with pytest.raises(UsageError):
        TrayIntent(TrayAction.PAUSE, ("djb", ""))
    with pytest.raises(UsageError):
        TrayIntent("pause")                               # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────── 渲染 / 纯函数
def test_to_dict_is_json_stable_and_matches_render_json():
    tray = TrayModel.build(PAUSED, {"unread": 1, "p0_unread": 1})
    payload = json.loads(tray.render_json())
    assert payload == tray.to_dict()
    assert json.loads(tray.render_json()) == payload                  # 幂等
    assert payload["items"][2]["badge"] == "!1"


def test_render_text_marks_disabled_items():
    running = TrayModel.build(RUNNING).render_text()
    assert "✓ 暂停接单" in running
    stopped = TrayModel.build(STOPPED).render_text()
    assert "✗ 暂停接单" in stopped


def test_build_is_pure_and_does_not_mutate_inputs():
    status = copy.deepcopy(PAUSED)
    notifications = {"unread": 2, "p0_unread": 0, "latest": [{"id": "n-1"}]}
    before_status, before_notifications = copy.deepcopy(status), copy.deepcopy(notifications)
    first = TrayModel.build(status, notifications)
    second = TrayModel.build(status, notifications)
    assert status == before_status
    assert notifications == before_notifications
    assert first.to_dict() == second.to_dict()


# ─────────────────────────────────────────────────────────── 消息投影
class FakeStore:
    def __init__(self, rows, *, unread=None, boom=False):
        self.rows = rows
        self.unread = unread
        self.boom = boom

    def list(self, limit=50):
        if self.boom:
            raise RuntimeError("db broken")
        return list(self.rows)

    def unread_count(self):
        if self.unread is not None:
            return self.unread
        return sum(1 for row in self.rows if not row.get("read_at"))


def test_empty_projection_returns_zero():
    for value in (None, [], ()):
        assert summarize(value) == {"unread": 0, "p0_unread": 0, "latest": []}
    assert from_store(None) == {"unread": 0, "p0_unread": 0, "latest": []}


def test_from_store_counts_and_limits_latest():
    rows = [
        {"id": "n-1", "severity": "P0", "template_key": "quota.exhausted",
         "created_at": "2026-10-24T03:00:00Z", "payload": {"x": 1}, "read_at": None},
        {"id": "n-2", "severity": "P1", "template_key": "update.ready",
         "created_at": "2026-10-24T02:00:00Z", "read_at": None},
        {"id": "n-3", "severity": "P0", "template_key": "quota.near_limit",
         "created_at": "2026-10-24T01:00:00Z", "read_at": "2026-10-24T02:30:00Z"},
    ]
    summary = from_store(FakeStore(rows), limit=2)
    assert summary["unread"] == 2
    assert summary["p0_unread"] == 1
    assert [item["id"] for item in summary["latest"]] == ["n-1", "n-2"]
    assert set(summary["latest"][0]) == {"id", "severity", "title", "created_at"}
    assert summary["latest"][0]["title"] == "配额已用尽"
    assert "payload" not in json.dumps(summary, ensure_ascii=False)


def test_store_unread_count_wins_for_total():
    rows = [{"id": "n-1", "severity": "P1", "read_at": None}]
    summary = from_store(FakeStore(rows, unread=7))
    assert summary["unread"] == 7
    assert summary["p0_unread"] == 0                     # 只有 P1 未读 → 0 条 P0


def test_title_priority_never_reads_body():
    assert title_of({"title": "服务端标题", "template_key": "quota.exhausted"}) == "服务端标题"
    assert title_of({"template_key": "quota.exhausted"}) == "配额已用尽"
    assert title_of({"template_key": "custom.unknown"}) == "custom.unknown"
    assert title_of({"category": "billing"}) == "billing"
    assert title_of({}) == "通知"
    assert title_of(None) == "通知"


def test_projection_drops_payload_and_body():
    rows = [{"id": "n-1", "severity": "P0", "title": "标题", "created_at": "t",
             "payload": {"prompt": "正文", "secret": "sk-xxx"}}]
    text = json.dumps(summarize(rows), ensure_ascii=False)
    for forbidden in ("正文", "prompt", "secret", "sk-xxx"):
        assert forbidden not in text


def test_broken_store_degrades_to_zero():
    summary = from_store(FakeStore([], boom=True), limit=3)
    assert summary == {"unread": 0, "p0_unread": 0, "latest": []}
