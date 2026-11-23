# SPDX-License-Identifier: Apache-2.0
"""账户页分区 + 「在浏览器确认绑定」反馈（2026-11-19 客户报障）。

客户原话（两条，同一轮）：

* 「绑定页面设置 UI 设置的太拥挤，把绑定节点和 USDT 地址完全隔离开」；
* 「在浏览器确认绑定点击也没反应」。

这两条都不是「再加一句提示」能解决的：前者要**版面分区**（字段与按钮各归各块），
后者要**中间状态就反馈**（点下即刷新 + 到 pending 就开浏览器 + 置灰要给理由）。
本文件把四条边界钉死，防止以后为了省事又合并回一屏：

1. 账户页每个字段/动作都有分区，且 USDT 相关**只**在收款区；
2. 收款区与绑定区不共用同一条按钮栏（动作 group 各自独立）；
3. 设备码处于 ``pending`` 时按钮置灰**必须**带理由（否则客户判成「按钮坏了」）；
4. 设备流程每变一次都刷新页面，``pending`` 时打开一次浏览器（同一链接只开一次）。
"""
from __future__ import annotations

import webbrowser

import pytest

from djb_node.desktop import app as app_module
from djb_node.desktop import views
from djb_node.desktop.controller import ActionResult


def _account_view() -> views.View:
    return views.build_view("account", views.Snapshot({}))


def test_account_fields_are_all_grouped():
    """没有「游离」字段：否则又会挤回同一块，等于没分区。"""
    view = _account_view()
    ungrouped = [field.key for field in view.fields if not field.group]
    assert ungrouped == [], "账户页字段必须全部带分区：" + repr(ungrouped)


def test_payout_fields_live_only_in_payout_group():
    view = _account_view()
    payout_keys = {field.key for field in view.fields if field.group == views.GROUP_PAYOUT}
    assert "usdt_address" in payout_keys
    assert "payout_state" in payout_keys           # 原「USDT 收款地址（云端）」卡降成的行
    # 别的区里不许出现收款字段，绑定区尤其不许
    others = {field.key for field in view.fields if field.group != views.GROUP_PAYOUT}
    assert not (payout_keys & others)


def test_bind_fields_live_only_in_bind_group():
    view = _pending_snapshot()          # 设备码开关开着，才有 device_state 这一行
    bind_keys = {field.key for field in view.fields if field.group == views.GROUP_BIND}
    assert {"web_console", "device_state"} <= bind_keys
    assert "usdt_address" not in bind_keys


def test_payout_action_is_separate_from_bind_actions():
    view = _pending_snapshot()          # 设备码开关开着，才有「在浏览器确认绑定」这颗按钮
    groups = dict((title, [item.key for item in members])
                  for title, members in views.action_groups(view.actions))
    assert "account.submit_payout" in groups[views.GROUP_PAYOUT]
    assert "account.bind_device" in groups[views.GROUP_BIND]
    # 两个区不许互相串：绑定区里没有收款动作，收款区里没有绑定动作
    assert "account.submit_payout" not in groups[views.GROUP_BIND]
    assert "account.bind_device" not in groups[views.GROUP_PAYOUT]


def test_account_cards_keep_payout_out_of_the_identity_row():
    rail = [card.title for card in _account_view().cards]
    assert "USDT 收款地址（云端）" not in rail


def test_section_order_puts_identity_bind_payout_in_that_order():
    view = _account_view()
    order = app_module.DesktopApp._section_order(object(), view)
    assert order == (views.GROUP_IDENTITY, views.GROUP_BIND, views.GROUP_PAYOUT)


def _pending_snapshot() -> views.View:
    """设备码处于 pending 的账户页（云端已发码、还没在网页确认）。"""
    return views.build_view("account", views.Snapshot({
        "account": {"device_flow": {"enabled": True, "state": "pending",
                                    "user_code": "AB12-CD34",
                                    "verify_url": "https://tkbank.ai/device"}},
    }))


def test_pending_bind_button_is_disabled_with_a_reason():
    view = _pending_snapshot()
    button = next(item for item in view.actions if item.key == "account.bind_device")
    assert button.enabled is False
    assert "正在等待网页确认" in button.reason     # 置灰没理由 = 客户以为按钮坏了


def test_pending_bind_button_reason_mentions_the_code():
    view = _pending_snapshot()
    button = next(item for item in view.actions if item.key == "account.bind_device")
    assert "AB12-CD34" in button.reason


class _StubApp:
    """只借 app 的三个方法：不建 Tk 窗口也能验证「点下去之后发生什么」。"""

    _open_verify_url_once = app_module.DesktopApp._open_verify_url_once
    _on_device_state = app_module.DesktopApp._on_device_state
    _apply_device = app_module.DesktopApp._apply_device
    # 2026-11-24 R2.1 ①：_apply_device 现在会调 _resume_after_login（回到原页），替身补齐
    _resume_after_login = app_module.DesktopApp._resume_after_login

    def __init__(self) -> None:
        self.toasts: list[tuple[str, bool]] = []
        self.refreshes: list[tuple[str, ...]] = []

    def _toast(self, message: str, ok: bool = True) -> None:
        self.toasts.append((message, ok))

    def _request_refresh_async(self, sections) -> None:
        self.refreshes.append(tuple(sections))


def test_pending_state_opens_browser_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: calls.append(url) or True)
    stub = _StubApp()
    payload = {"state": "pending", "verify_url": "https://tkbank.ai/device",
               "user_code": "AB12-CD34"}
    stub._on_device_state(dict(payload))
    stub._on_device_state(dict(payload))          # 状态抖动再来一次：不许开第二个窗口
    assert calls == ["https://tkbank.ai/device"]
    assert stub.refreshes, "中间状态必须刷新页面，否则页面一个字都不变"
    assert "已在浏览器打开确认页" in stub.toasts[0][0]


def test_failed_flow_refreshes_instead_of_showing_only_a_toast():
    """旧实现只在成功时刷新 —— 失败时页面毫无变化，客户体感就是「点了没反应」。"""
    stub = _StubApp()
    stub._apply_device(ActionResult("无法连接服务端（tkbank.ai）：网络或服务不可用",
                                    ok=False))
    assert stub.toasts and stub.toasts[0][1] is False
    assert ("account", "overview", "access") in stub.refreshes


def test_open_verify_url_once_ignores_empty_url(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: calls.append(url) or True)
    stub = _StubApp()
    assert stub._open_verify_url_once("") is False
    assert calls == []


def test_approved_state_does_not_reopen_browser(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: calls.append(url) or True)
    stub = _StubApp()
    stub._on_device_state({"state": "approved", "verify_url": "https://tkbank.ai/device",
                           "user_code": "AB12-CD34"})
    assert calls == []
    assert stub.refreshes, "绑定成功后同样要刷新（绑定状态/收款面板立刻反映）"
