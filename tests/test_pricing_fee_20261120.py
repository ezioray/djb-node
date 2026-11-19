# SPDX-License-Identifier: Apache-2.0
"""设价前必须看见两件事（2026-11-20 owner）：
  1. 这是**最低价**：低于不成交，高于时按最优价成交（原始设计）；
  2. 平台服务费 15%：客户实付 = 报价 × 1.15，费率以成交快照为准。
"""
from __future__ import annotations

from djb_node import facts
from djb_node import bids as bids_mod


class _Store:
    def __init__(self):
        self.kv = {}

    def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    def set_kv(self, key, value):
        self.kv[key] = value


def test_save_price_dialog_discloses_fee_and_settlement():
    from djb_node.desktop.controller import DesktopController

    seen: list[str] = []

    class _Src:
        _home = None

        def __init__(self):
            self._store = _Store()

        def snapshot(self, sections=None):
            raise RuntimeError("本测试不取数")

    source = _Src()
    controller = DesktopController(source)
    source.bids_store = lambda: source._store   # _bids() 需要的是**工厂**（callable）
    controller.selected["token_plan"] = "gpt-4o-mini"
    controller.set_draft("min_price", "0.35")
    result = controller.action("token_plan.save_price",
                               confirm=lambda text: (seen.append(text), True)[1])
    assert result.ok
    assert seen, "设价必须先弹确认框"
    dialog = seen[0]
    assert "15%" in dialog, dialog
    assert "客户实付" in dialog, dialog
    assert "1.15" in dialog, dialog
    assert "最优价" in dialog, dialog


def test_auto_best_is_the_default_mode():
    store = _Store()
    book = bids_mod.BidBook(store)
    book.set_price("gpt-4o-mini", "0.5")
    assert book.mode_of("gpt-4o-mini") == bids_mod.MODE_AUTO_BEST


def test_old_ledger_without_mode_reads_as_auto_best():
    store = _Store()
    store.kv[bids_mod.BIDS_KV_KEY] = {"version": 1, "bids": {"m1": {"min_price": "0.5"}}}
    book = bids_mod.BidBook(store)
    assert book.mode_of("m1") == bids_mod.MODE_AUTO_BEST
    assert book.load() == {"m1": {"min_price": "0.5"}}      # 公开形状不变


def test_customer_pay_helper():
    assert facts.SERVICE_FEE_BPS == 1500
    assert facts.service_fee_percent() == 15.0
    assert facts.customer_pay_text("0.35") == "0.40"