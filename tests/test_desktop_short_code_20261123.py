# SPDX-License-Identifier: Apache-2.0
"""R2.1 ②④：短码可读可复制、界面不出现 ``n_`` 长串、复制组件统一。

来源：`CHG-20261123-R2-1-NODE-QUICKWINS` ②（短码）与 ④（统一复制组件）。

口径（owner D1 已裁决）：

* **短码只用于绑定** —— 8 位、一次性、10 分钟；显示成 ``XXXX-XXXX``，绑定完成即失效；
* 节点 ID 是 ``n_`` + 32 位十六进制，**日常界面一个都不出现**：日常只显示「设备名 /
  后 8 位」，完整值折叠进「高级（排障用）」并保留「复制节点 ID」；
* 复制（短码 / 链接 / 地址 / 密钥）走同一个组件：文案统一「复制 + 东西」、禁用必须给原因、
  成功/失败都有 toast（``effect="copy"`` + 非空 message）。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import views as vw
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource
from djb_node.desktop.app import demo_snapshot

FULL_NODE_ID = "n_" + "3f9a2c1b" * 4            # n_ + 32 位十六进制（真机形状）


# ── 1. 两个纯函数（显示口径的唯一实现）
@pytest.mark.parametrize("raw,expected", [
    ("8f3k2p9q", "8F3K-2P9Q"),                  # 小写去分隔符
    ("8F3K-2P9Q", "8F3K-2P9Q"),                 # 已经带横线
    ("8F3K 2P9Q", "8F3K-2P9Q"),
    ("", ""),                                   # 空：不编造
    ("ABC", "ABC"),                             # 不是 8 位：原样（不猜）
    ("8F3K-2P9O", "8F3K-2P9O"),                 # 含易混字符 O：不硬塞横线，原样交给上层判失效
])
def test_format_short_code(raw, expected):
    assert vw.format_short_code(raw) == expected


def test_mask_node_id_never_returns_the_long_form():
    masked = vw.mask_node_id(FULL_NODE_ID)
    assert masked == "…3f9a2c1b" and "n_" not in masked
    assert vw.mask_node_id("nd_old") == "old" or vw.mask_node_id("nd_old") == "…d_old"
    assert vw.mask_node_id("") == "—" and vw.mask_node_id(None) == "—"
    assert vw.mask_node_id("—") == "—"


def test_node_label_prefers_display_name():
    assert vw.node_label(FULL_NODE_ID, "客厅那台 Mac") == "客厅那台 Mac"
    assert vw.node_label(FULL_NODE_ID, "") == "…3f9a2c1b"
    assert vw.node_label("", "客厅那台 Mac") == "客厅那台 Mac"


# ── 2. 13 页首屏（卡片 / 非高级字段 / 眉标 / 副标题）不出现 n_ 长串
def _node_snapshot(*, bound: bool = True):
    sections = dict(demo_snapshot().sections)
    sections["access"] = {
        "config": {"price_mode": "auto", "node_secret": "••••1234", "node_id": FULL_NODE_ID},
        "valid": True, "saved": True,
        "binding": {"state": "bound" if bound else "unbound", "node_id": FULL_NODE_ID},
        "node_id": FULL_NODE_ID, "node_id_source": "credentials",
        "display_name": "客厅那台 Mac", "note": "", "path": "~/.djb/desktop/access-config.json",
    }
    sections["account"] = {
        "profile": {"node_id": FULL_NODE_ID, "display_name": "客厅那台 Mac",
                    "state": "active", "region": "cn-east", "platform": "linux",
                    "last_seen_at": "2026-11-23T10:00:00Z", "bound": bound},
        "security": {"storage": "file", "node_id": FULL_NODE_ID},
        "device_flow": {"enabled": True, "state": "pending", "user_code": "8f3k2p9q",
                        "verify_url": "https://tokenbank.example/device", "attempts": 2},
        "note": "",
    }
    return type(demo_snapshot())(sections=sections, fetched_at="2026-11-23T12:00:00+0800",
                                 source="test")


def test_no_page_shows_the_raw_node_id_in_public_text():
    snapshot = _node_snapshot()
    offenders: list[str] = []
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        public: list[str] = [view.title, view.subtitle or ""]
        if view.hero is not None:
            public += [view.hero.kicker, view.hero.title, view.hero.body or "",
                       view.hero.hint or ""]
        for card in view.cards:
            public += [card.title, card.value, card.subtitle if hasattr(card, "subtitle") else ""]
        for field in view.fields:
            if not field.advanced:                 # 高级区（折叠）里允许留完整值给客服核对
                public += [field.label, field.value, field.hint]
        text = " ".join(str(item) for item in public)
        if FULL_NODE_ID in text:
            offenders.append(key)
    assert offenders == [], "这些页的日常界面还露着完整节点 ID：" + ", ".join(offenders)


def test_access_and_account_show_the_short_label():
    snapshot = _node_snapshot()
    access = vw.build_view("access", snapshot)
    label = next(field for field in access.fields if field.key == "node_label")
    assert label.value == "客厅那台 Mac" and FULL_NODE_ID not in label.value
    full = next(field for field in access.fields if field.key == "node_id")
    assert full.advanced is True and full.value == FULL_NODE_ID     # 折叠区里保留
    account = vw.build_view("account", snapshot)
    identity = next(field for field in account.fields if field.key == "node_label")
    assert identity.value == "客厅那台 Mac"


# ── 3. 短码：显示成 XXXX-XXXX + 可复制
def test_account_shows_formatted_short_code_and_a_copy_action():
    snapshot = _node_snapshot(bound=False)
    view = vw.build_view("account", snapshot)
    field = next(item for item in view.fields if item.key == "device_state")
    assert field.label == "绑定短码" and "8F3K-2P9Q" in field.value
    assert "只用于绑定" in field.hint
    copy = next(item for item in view.actions if item.key == "account.copy_short_code")
    assert copy.label.startswith("复制") and copy.enabled is True


def test_copy_short_code_returns_the_formatted_code():
    from djb_node.device_flow import DeviceFlow

    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections)))
    controller.device_flow = DeviceFlow(user_code="8f3k2p9q", state="pending")
    result = controller.action("account.copy_short_code")
    assert result.ok and result.effect == "copy" and result.payload == "8F3K-2P9Q"
    assert "一次性" in result.message


def test_copy_short_code_refuses_after_binding():
    from djb_node.device_flow import DeviceFlow

    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections)))
    controller.device_flow = DeviceFlow(user_code="8F3K2P9Q", state="approved")
    result = controller.action("account.copy_short_code")
    assert result.ok is False and "已经完成" in result.message


# ── 4. 统一复制组件：文案 / 禁用原因 / 回执
def test_every_copy_action_uses_one_shape():
    snapshot = _node_snapshot(bound=False)          # 待绑定才有短码/确认链接两条复制
    seen: list[str] = []
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        for action in view.actions:
            if "copy" not in action.key:
                continue
            seen.append(action.key)
            assert action.label.startswith("复制"), action.key + " 的文案不统一：" + action.label
            if not action.enabled:
                assert action.reason.strip(), action.key + " 禁用了却没写原因"
    assert {"access.copy_node_id", "account.copy_short_code", "account.copy_verify_url",
            "account.copy_address", "bench.copy", "logs.copy"} <= set(seen)


def test_copy_address_returns_the_local_address(tmp_path):
    from djb_node.desktop import access as ac

    address = "0x" + "ab" * 20
    store = ac.MemoryAccessStore(ac.AccessConfig(usdt_address=address))
    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections)),
                                   access=ac.AccessService(store, reloader=lambda: ac.RELOAD_OK))
    result = controller.action("account.copy_address")
    assert result.ok and result.effect == "copy" and result.payload == address
    assert "0xabab…abab" in result.message            # 回执里是掩码，不是全文


def test_copy_address_without_address_says_the_next_step():
    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections)))
    result = controller.action("account.copy_address")
    assert result.ok is False and "还没有收款地址" in result.message
