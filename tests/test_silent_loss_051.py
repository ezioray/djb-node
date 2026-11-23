# SPDX-License-Identifier: Apache-2.0
"""0.5.1：把「点了没反应」整类问题钉死（2026-11-20 真机复现）。

真机现象（生产环境、44 模型表）：
  1. 模型表行**点不中** → 客户没法设价（构建时无行就不绑 <<TreeviewSelect>>）；
  2. relay 开关默认关闭时，GUI 对「读取模型清单」**零反馈**；
  3. 底价只有 GUI 一条路，脚本/验收无法复核。
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from djb_node import cli as cli_mod
from djb_node import facts


class _Store:
    def __init__(self):
        self.kv = {}

    def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    def set_kv(self, key, value):
        self.kv[key] = value


def test_row_selection_binding_is_not_gated_by_rows() -> None:
    """表格「有没有行」不许影响 <<TreeviewSelect>> 的绑定。"""
    source = Path(__file__).resolve().parents[1] / "djb_node" / "desktop" / "app.py"
    text = source.read_text(encoding="utf-8")
    assert 'if table.rows:\n            tree.bind("<<TreeviewSelect>>"' not in text
    assert 'tree.bind("<<TreeviewSelect>>", self._on_row_select)' in text


def test_token_plan_offers_connectivity_test_and_relay_switch() -> None:
    from djb_node.desktop import views

    from djb_node.desktop.datasource import Snapshot
    view = views.build_view("token_plan", Snapshot(sections={}))
    found = {action.key for action in getattr(view, "actions", ()) or ()}
    assert "token_plan.test_connectivity" in found
    assert "token_plan.enable_relay" in found



def test_connectivity_action_reports_counts_without_saving() -> None:
    from djb_node.desktop.controller import DesktopController

    class _Src:
        _home = None

        def discover_upstream_models(self, base_url, protocol, secret):
            return {"base_url": base_url, "protocol": protocol, "models": [], "rejected": [],
                    "error_class": None, "http_status": 200, "latency_ms": 137,
                    "mapped": 2, "total": 44, "note": ""}

        def snapshot(self, sections=None):
            raise RuntimeError("本测试不取数")

    controller = DesktopController(_Src())
    controller.set_draft("base_url", "https://api.tkmarket.ai/v1")
    controller.set_draft("protocol", "openai-chat")
    controller.set_draft("upstream_secret", "sk-test")
    result = controller.action("token_plan.test_connectivity")
    assert result.ok, result.message
    assert "44" in result.message and "137" in result.message
    assert "还没保存" in result.message


def test_enable_relay_writes_the_flag(tmp_path) -> None:
    from djb_node.config import Paths, load_config, feature_flag
    from djb_node.desktop.controller import DesktopController

    home = tmp_path / "home"
    home.mkdir()

    class _Src:
        _home = str(home)

        def snapshot(self, sections=None):
            raise RuntimeError("本测试不取数")

    controller = DesktopController(_Src())
    result = controller.action("token_plan.enable_relay")
    assert result.ok, result.message
    config = load_config(Paths(str(home)))
    assert feature_flag(config, facts.RELAY_FLAG) is True


def test_cli_bid_round_trip(monkeypatch) -> None:
    store = _Store()
    monkeypatch.setattr(cli_mod, "_context", lambda args: (None, None, store))

    set_args = argparse.Namespace(bid_action="set", model="deepseek-v4-flash",
                                  min_price="0.35", json=True)
    assert cli_mod.cmd_bid(set_args) == 0
    assert store.kv["rc14/model_bids"]["bids"]["deepseek-v4-flash"]["min_price"] == "0.35"
    assert store.kv["rc14/model_bids"]["bids"]["deepseek-v4-flash"]["mode"] == "auto_best"

    from djb_node.bids import BidBook
    book = BidBook(store)
    assert book.load() == {"deepseek-v4-flash": {"min_price": "0.35"}}

    remove_args = argparse.Namespace(bid_action="remove", model="deepseek-v4-flash", json=True)
    assert cli_mod.cmd_bid(remove_args) == 0
    assert book.load() == {}