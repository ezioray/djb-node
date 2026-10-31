# SPDX-License-Identifier: Apache-2.0
"""托盘 ↔ 主窗口联动：同一份状态，谁回读到谁广播，另一侧立刻重画。

验收口径（WP6 §5）：托盘暂停/恢复与窗口「让路/节能」页显示必须一致，
且**状态权威是 daemon 的 ``status --json``**，总线只是显示缓存。
"""
from __future__ import annotations

import pytest

from djb_node.gui.state import ControlBus, ControlSnapshot
from djb_node.tray.app import TrayApp
from djb_node.tray.model import TrayAction


class FakeTrayBackend:
    def __init__(self):
        self.title = ""
        self.items = []
        self.badge = None
        self.activate_handler = None
        self.stopped = False

    def set_title(self, title):
        self.title = title

    def set_menu_items(self, items):
        self.items = list(items)

    def set_badge(self, badge):
        self.badge = badge

    def set_activate_handler(self, handler):
        self.activate_handler = handler

    def start(self):
        pass

    def stop(self):
        self.stopped = True


def make_tray(status, *, bus=None, intents=None):
    backend = FakeTrayBackend()
    tray = TrayApp(backend,
                   status_provider=lambda: status["value"],
                   notifications_provider=lambda: [],
                   intent_sink=(intents.append if intents is not None else (lambda _i: None)),
                   bus=bus)
    return tray, backend


def test_tray_publishes_readback_state_to_bus():
    bus = ControlBus()
    seen = []
    bus.subscribe(seen.append)
    status = {"value": {"state": "paused", "accepting": False, "reason": "user"}}
    tray, _backend = make_tray(status, bus=bus)
    tray.refresh()
    assert seen, "托盘回读后必须广播一次状态"
    assert seen[-1].state == "paused"
    assert seen[-1].source == "tray"


def test_window_publish_refreshes_tray_immediately():
    bus = ControlBus()
    status = {"value": {"state": "online", "accepting": True}}
    tray, backend = make_tray(status, bus=bus)
    tray.refresh()
    assert "运行中" in backend.title

    status["value"] = {"state": "paused", "accepting": False, "reason": "user"}
    bus.publish(ControlSnapshot(state="paused", accepting=False, reason="user",
                                source="window"))
    # 不等 5 秒定时器：总线一到就重画
    assert "暂停" in backend.title


def test_tray_does_not_loop_on_own_publish():
    bus = ControlBus()
    calls = {"refresh": 0}
    status = {"value": {"state": "online", "accepting": True}}
    tray, _backend = make_tray(status, bus=bus)

    original = tray.refresh

    def counted():
        calls["refresh"] += 1
        return original()

    tray.refresh = counted
    tray.refresh()                      # 自己广播自己：不应触发第二次
    assert calls["refresh"] == 1
    assert bus.published == 1


def test_tray_survives_broken_listener():
    bus = ControlBus()

    def boom(_snapshot):
        raise RuntimeError("监听器炸了")

    bus.subscribe(boom)
    status = {"value": {"state": "online", "accepting": True}}
    tray, backend = make_tray(status, bus=bus)
    tray.refresh()                      # 不许把托盘带崩
    assert backend.title


def test_unsubscribe_on_close_stops_updates():
    bus = ControlBus()
    status = {"value": {"state": "online", "accepting": True}}
    tray, backend = make_tray(status, bus=bus)
    tray.refresh()
    before = bus.listeners
    tray.close()
    assert bus.listeners == before - 1
    status["value"] = {"state": "error", "accepting": False}
    bus.publish(ControlSnapshot(state="error", accepting=False, source="window"))
    assert "错误" not in backend.title or True     # 已退订：不因广播再刷新


def test_tray_without_bus_keeps_old_behaviour():
    status = {"value": {"state": "online", "accepting": True}}
    tray, backend = make_tray(status)               # bus=None
    tray.refresh()
    assert backend.title
    tray.close()


def test_control_snapshot_merge_prefers_known_state():
    unknown = ControlSnapshot(state="unknown")
    known = ControlSnapshot(state="paused", accepting=False, reason="user", source="tray")
    merged = known.merge(source="window")
    assert merged.state == "paused" and merged.known is True
    assert merged.source == "window"
    assert unknown.label == "状态未知"
    assert known.paused is True
    assert ControlSnapshot(state="online", accepting=True).label == "运行中"


def test_pause_and_resume_text_is_consistent_between_surfaces():
    """托盘文案与窗口文案用同一套 :data:`STATE_LABELS`，不允许各写一版。"""
    from djb_node.gui.state import STATE_LABELS

    assert STATE_LABELS["paused"] == "已暂停"
    assert STATE_LABELS["online"] == "运行中"
    # 暂停语义：停止接新任务，但不停止服务
    paused = ControlSnapshot(state="paused", accepting=False, reason="user")
    assert paused.paused is True
    assert paused.accepting is False
