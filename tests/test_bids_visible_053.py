# SPDX-License-Identifier: Apache-2.0
"""0.5.3：底价区不许「静默为空」。

真机 2026-11-20：`djb bid set/list` 都能读写，GUI 表格 44 行却全显示「未设价」。
根因候选都集中在「异常被吞 / 模型 id 对不上」，两种都必须显示到界面上。
"""
from __future__ import annotations

from djb_node.desktop import views
from djb_node.desktop.datasource import Snapshot


def _plan_snapshot(*, bids: dict, models: list) -> Snapshot:
    return Snapshot(sections={"token_plan": {
        "upstreams": [], "limits": {}, "usage": {}, "preset": "",
        "node": {"node_id": "n_test", "bound": True},
        "models": models, "bids": bids, "protocol": "openai-chat",
        "base_url": "", "secret_seen": "未保存", "discover_note": "",
    }})


def _notes(view) -> str:
    parts = [str(getattr(n, "text", n)) for n in (getattr(view, "notes", ()) or ())]
    return " | ".join(parts)


def test_unavailable_reason_is_visible() -> None:
    snap = _plan_snapshot(bids={"rows": {}, "unavailable": "本地底价读不到（OperationalError）"},
                          models=[{"model": "m1", "upstream_id": "u1"}])
    view = views.build_view("token_plan", snap)
    assert "本地底价读不到" in _notes(view)


def test_ledger_without_matching_models_is_visible() -> None:
    snap = _plan_snapshot(bids={"rows": {"other-model": {"min_price": "0.35"}}},
                          models=[{"model": "m1", "upstream_id": "u1"}])
    view = views.build_view("token_plan", snap)
    assert "没对上当前模型表" in _notes(view)


def test_matched_bid_shows_price_and_no_warning() -> None:
    snap = _plan_snapshot(bids={"rows": {"m1": {"min_price": "0.35"}}},
                          models=[{"model": "m1", "upstream_id": "u1"}])
    view = views.build_view("token_plan", snap)
    text = _notes(view)
    assert "没对上当前模型表" not in text
    assert "本地底价读不到" not in text