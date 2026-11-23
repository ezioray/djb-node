# SPDX-License-Identifier: Apache-2.0
"""R2.1 ⑥：按钮口径 —— 高度 44/36/32、一页一个主按钮、禁用说原因、危险操作二次确认。

来源：`CHG-20261123-R2-1-NODE-QUICKWINS` ⑥（真机：26–30px 的小按钮在 150% 缩放下点不中；
同一页出现两颗「主按钮」等于没主按钮；置灰按钮旁边空白让人猜；解绑/清退类操作点一下就生效）。

三档高度：主操作 **44**、次级 **36**、行内小按钮 **32**（32 是下限，见 ``ui_kit.BTN_HEIGHTS``）。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import ui_kit as kit
from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource
from tests import tk_support


def _snapshots():
    """几套代表性快照：默认 / 全不可用 / 未绑定 / 已清退（危险按钮要可点才谈得上确认）。"""
    demo = demo_snapshot()
    everything_down = type(demo)(sections={}, fetched_at=demo.fetched_at, source="test")
    unbound = dict(demo.sections)
    unbound["access"] = {**dict(unbound.get("access") or {}),
                         "binding": {"state": "unbound", "node_id": ""}}
    retired = dict(demo.sections)
    retired["access"] = {**dict(retired.get("access") or {}),
                         "daemon": {"running": False, "reason_code": "retired",
                                    "reason": "节点已被云端清退"}}
    return {
        "demo": demo,
        "all_unavailable": everything_down,
        "unbound": type(demo)(sections=unbound, fetched_at=demo.fetched_at, source="test"),
        "retired": type(demo)(sections=retired, fetched_at=demo.fetched_at, source="test"),
    }


# ── 1. 三档高度常量本身
def test_button_height_tiers_are_44_36_32():
    assert kit.BTN_HEIGHTS == {"lg": 44, "md": 36, "sm": 32}
    assert kit.btn_height("lg") == 44 and kit.btn_height("md") == 36
    assert kit.btn_height("sm") == 32
    assert kit.btn_height("nope") == 32                     # 非法档位回落下限
    assert kit.btn_height("lg", scale=1.25) == 55           # 125% 跟着放大


# ── 2. 一页一个主按钮
@pytest.mark.parametrize("name", ["demo", "all_unavailable", "unbound", "retired"])
def test_each_page_has_at_most_one_primary_action(name):
    snapshot = _snapshots()[name]
    offenders: dict[str, list[str]] = {}
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        primaries = [action.key for action in view.actions if action.primary]
        if len(primaries) > 1:
            offenders[key] = primaries
    assert offenders == {}, "这些页出现了多个主按钮：" + repr(offenders)


# ── 3. 置灰必须说原因
@pytest.mark.parametrize("name", ["demo", "all_unavailable", "unbound", "retired"])
def test_disabled_actions_always_explain_why(name):
    snapshot = _snapshots()[name]
    silent: list[str] = []
    for key in vw.VIEW_KEYS:
        for action in vw.build_view(key, snapshot).actions:
            if not action.enabled and not action.reason.strip():
                silent.append(key + ":" + action.key)
    assert silent == [], "这些按钮置灰却没有原因：" + ", ".join(silent)


# ── 4. 危险动作：标 danger + 动作层二次确认（确认框说「不」= 什么都不做）
def test_destructive_actions_are_marked_danger():
    snapshot = _snapshots()["retired"]
    marked = {action.key: action.danger
              for key in vw.VIEW_KEYS
              for action in vw.build_view(key, snapshot).actions
              if action.key in ("account.logout", "access.reset_node")}
    assert marked == {"account.logout": True, "access.reset_node": True}


def test_logout_requires_a_second_confirmation():
    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections)))
    asked: list[str] = []

    def deny(message: str) -> bool:
        asked.append(message)
        return False

    result = controller.action("account.logout", confirm=deny)
    assert asked and "确认" in asked[0]              # 真的问了
    assert result.ok is False and result.effect == "cancelled"
    assert "取消" in result.message and "未改动" in result.message


def test_reset_node_requires_a_second_confirmation():
    sections = dict(demo_snapshot().sections)
    sections["access"] = {**dict(sections.get("access") or {}),
                          "daemon": {"running": False, "reason_code": "retired",
                                     "reason": "节点已被云端清退"}}
    controller = DesktopController(FakeDataSource(sections))
    asked: list[str] = []

    def deny(message: str) -> bool:
        asked.append(message)
        return False

    result = controller.action("access.reset_node", confirm=deny)
    assert asked and "不可逆" in asked[0]            # 说清后果再问
    assert result.ok is False and result.effect == "cancelled"
    assert "未改动" in result.message


# ── 5. 真窗口：所有按钮 ≥32（且主按钮 44、次按钮 36、危险按钮是红的）
def _all_buttons(widget) -> list:
    found = []
    try:
        for child in widget.winfo_children():
            if isinstance(child, kit.Button):
                found.append(child)
            found.extend(_all_buttons(child))
    except Exception:                                # pragma: no cover - 已销毁
        pass
    return found


@tk_support.needs_tk
def test_real_window_buttons_are_at_least_32px_and_use_the_tiers():
    from djb_node.desktop.app import DesktopApp

    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections),
                                                  source="test"))
    root = tk_support.new_root()
    app = DesktopApp(controller, root=root)
    try:
        too_short: list[str] = []
        for key in ("overview", "access", "account"):
            app.show_view(key)
            root.update()
            for button in _all_buttons(root):
                if int(button["height"]) < 32:
                    too_short.append(key + ":" + button.text + "=" + str(button["height"]))
        assert too_short == [], "这些按钮低于 32px 下限：" + ", ".join(sorted(set(too_short)))

        app.show_view("account")
        root.update()
        logout = next(button for button in _all_buttons(root)
                      if button.text.startswith("解绑这台电脑"))
        assert logout.kind == "danger"               # 危险操作是红按钮，不是普通次按钮
        assert int(logout["height"]) >= 36
    finally:
        try:
            app.stop()
        except Exception:                            # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                            # pragma: no cover
            pass


@tk_support.needs_tk
def test_primary_action_button_is_44px():
    from djb_node.desktop.app import DesktopApp

    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections),
                                                  source="test"))
    root = tk_support.new_root()
    app = DesktopApp(controller, root=root)
    try:
        app.show_view("access")
        root.update()
        primary = [button for button in _all_buttons(root)
                   if button.kind == "primary" and button.text == "保存并回读"]
        # DPI 倍率会让实际像素略大于逻辑值（Xvfb 上 Tk 报 1.04 左右），所以按同一档位算
        expected = kit.btn_height("lg", scale=app._scale)
        assert primary and int(primary[0]["height"]) == expected
        assert 44 <= int(primary[0]["height"]) <= 46
    finally:
        try:
            app.stop()
        except Exception:                        # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                        # pragma: no cover
            pass
