# SPDX-License-Identifier: Apache-2.0
"""RC14 D：客户端「模型与探针」——把**已有**的 ``probe_details()`` 摆到台面上。

客户报的缺口是「页面只给聚合数，看不到逐模型结论」（报告 40-UX 的 G5/E7/E8）：

* 探针明细**早就在本机**（``upstream.py`` 的 ``probe_details()``），缺的只是投影；
* 三态不许混：``True`` ✓ / ``False`` ✗ / ``None`` —（``None`` = 没测过，**不是失败**）；
* 失败必须给**人话原因**（``stream_failed`` → 「流式不可用…」），不是英文码；
* 两个新鲜窗口（900 s 公告 / 600 s 报价）的数字只能来自 ``facts``；
* daemon 不在 / 库读不到时说「读不到 + 原因」，**不说 0**。
"""
from __future__ import annotations

import time

import pytest

from djb_node import facts
from djb_node.desktop import app as desktop_app
from djb_node.desktop import views as vw
from djb_node.desktop.controller import ActionResult, DesktopController
from djb_node.desktop.datasource import (FakeDataSource, LocalDataSource, ProviderError,
                                         Snapshot)
from tests.tk_support import needs_tk


def _snapshot(probe: dict, *, catalog: list | None = None) -> Snapshot:
    sections = dict(desktop_app.demo_snapshot().sections)
    sections["models"] = {"catalog": catalog if catalog is not None else [],
                          "local": [], "online": True, "note": "", "probe": probe}
    return Snapshot(sections=sections)


def _row(**overrides) -> dict:
    base = {"upstream_id": "u1", "base_url": "https://relay.example/v1", "model": "m-1",
            "enabled": True, "inference_ok": True, "stream_ok": True, "usage_ok": True,
            "probe_version": 3, "last_probe_at": "2026-11-14T10:00:00+0800",
            "probe_error_class": None, "probe_age_s": 30, "fresh": True,
            "announceable": True}
    base.update(overrides)
    return base


def test_probe_table_has_every_column_the_report_asked_for():
    view = vw.build_view("models", _snapshot({"rows": [_row()]}))
    assert view.table.columns == ("模型", "上游", "最近探针", "L2", "L3", "用量", "状态", "原因")


def test_three_state_mark_never_confuses_none_with_false():
    """``None``（没测过）必须显示「—」，不能显示成 ✗ —— 这是 D 的硬要求。"""
    rows = [_row(model="never", inference_ok=None, stream_ok=None, usage_ok=None,
                 probe_version=None, last_probe_at=None, probe_age_s=None,
                 fresh=False, announceable=False),
            _row(model="failed", inference_ok=False, stream_ok=None, usage_ok=None,
                 probe_error_class="unauthorized", announceable=False)]
    view = vw.build_view("models", _snapshot({"rows": rows}))
    table = {row.cells[0]: row for row in view.table.rows}
    assert table["never"].cells[3:6] == ("—", "—", "—")
    assert table["never"].cells[2] == "从未"
    assert table["never"].cells[6] == "未跑过"
    assert table["failed"].cells[3] == "✕"
    assert table["failed"].cells[4:6] == ("—", "—")


@pytest.mark.parametrize("error_class,needle", [
    ("timeout", "超时"),
    ("unauthorized", "密钥被拒"),
    ("http_5xx", "上游 5xx"),
    ("stream_failed", "流式不可用"),
    ("model_not_listed", "没有这个模型"),
    ("something_new", "未登记的原因"),      # 未登记的原因也要显示，不能吞掉
])
def test_failure_reasons_are_translated_to_plain_chinese(error_class, needle):
    row = _row(probe_error_class=error_class, inference_ok=False, announceable=False,
               stream_ok=None, usage_ok=None)
    view = vw.build_view("models", _snapshot({"rows": [row]}))
    cells = view.table.rows[0].cells
    assert needle in cells[7], error_class
    assert error_class in cells[7], "原值也要留着（排障要能搜到英文码）"


def test_stale_conclusion_is_not_the_same_message_as_failure():
    """过期 ≠ 失败：过期要说「报价已停，公告还剩 N 分钟」（两个窗口的数字来自 facts）。"""
    stale = _row(probe_age_s=facts.PROBE_FRESH_S + 120, fresh=False, announceable=False)
    view = vw.build_view("models", _snapshot({"rows": [stale]}))
    state = view.table.rows[0].cells[6]
    assert state.startswith("结论过期")
    assert "报价已停" in state and "分钟" in state
    assert "失败" not in state


def test_l3_failure_shows_degraded_and_keeps_announcing():
    row = _row(stream_ok=False, probe_error_class="stream_failed", announceable=True)
    view = vw.build_view("models", _snapshot({"rows": [row]}))
    cells = view.table.rows[0].cells
    assert cells[3:6] == ("✓", "✕", "✓")
    assert "仍按非流式公告" in cells[7]


def test_disabled_model_is_labelled_not_counted_as_failed():
    row = _row(enabled=False, inference_ok=None, stream_ok=None, usage_ok=None,
               announceable=False, probe_age_s=None, last_probe_at=None)
    view = vw.build_view("models", _snapshot({"rows": [row]}))
    assert view.table.rows[0].cells[6] == "已停用"
    cards = {card.title: card.value for card in view.cards}
    assert cards["可公告"] == "0 个"
    assert cards["探针失败 / 停用"] == "1 个"


def test_windows_note_numbers_come_from_facts():
    probe = {"rows": [_row()], "fresh_window_s": facts.PROBE_FRESH_S,
             "quote_window_s": facts.PROBE_FRESH_AFTER_S}
    view = vw.build_view("models", _snapshot(probe))
    joined = " ".join(view.notes)
    assert str(facts.PROBE_FRESH_S // 60) + " 分钟" in joined
    assert str(facts.PROBE_FRESH_AFTER_S // 60) + " 分钟" in joined


def test_probe_block_unavailable_says_why_instead_of_zero():
    """库读不到 ⇒ 「读不到（原因）」，表格空态与 note 都要说出来，绝不写 0。"""
    view = vw.build_view("models", _snapshot(
        {"rows": [], "unavailable": "探针结论读不到（OperationalError：no such table）"}))
    assert "读不到" in view.table.note
    assert not view.table.rows
    assert "还没有接入任何中转站" in view.table.empty


# ─────────────────────────────────────── datasource：纯投影，不重算门控
def test_probe_rows_only_projects_and_never_recomputes_gating():
    """``_probe_rows`` 只补 ``upstream_id`` / ``base_url``，门控字段原样透传（不二次判定）。"""
    class FakeRecord:
        id = "u1"
        base_url = "https://relay.example/v1"
        protocol = "openai-chat"
        state = "verified"
        enabled = True

    class FakeUpstreams:
        def list(self):
            return [FakeRecord()]

        def probe_details(self, identifier, *, now=None):
            assert identifier == "u1"
            return [{"model": "m-1", "enabled": True, "inference_ok": True,
                     "stream_ok": False, "usage_ok": True, "probe_version": 3,
                     "last_probe_at": "2026-11-14T10:00:00+0800",
                     "probe_error_class": "stream_failed", "probe_age_s": 12,
                     # 故意给「反直觉」的组合：L3 挂但 announceable=True
                     "fresh": True, "announceable": True}]

    rows = LocalDataSource._probe_rows(FakeUpstreams(), None)   # noqa: SLF001
    assert rows == [{"model": "m-1", "enabled": True, "inference_ok": True,
                     "stream_ok": False, "usage_ok": True, "probe_version": 3,
                     "last_probe_at": "2026-11-14T10:00:00+0800",
                     "probe_error_class": "stream_failed", "probe_age_s": 12,
                     "fresh": True, "announceable": True,
                     "upstream_id": "u1", "base_url": "https://relay.example/v1",
                     "protocol": "openai-chat", "upstream_state": "verified",
                     "upstream_enabled": True}]


# ─────────────────────────────────────── controller：探测动作
class FakeCli:
    def __init__(self, *, listing: dict, probes: dict | None = None) -> None:
        self.listing = listing
        self.probes = probes or {}
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        if list(args) == ["upstream", "list"]:
            return self.listing
        identifier = str(args[args.index("--id") + 1])
        value = self.probes.get(identifier)
        if isinstance(value, Exception):
            raise value
        return value if value is not None else {"model_checks": []}


def test_probe_run_reports_per_upstream_pass_and_fail():
    cli = FakeCli(listing={"upstreams": [{"id": "u1"}, {"id": "u2"}]},
                  probes={"u1": {"model_checks": [{"ok": True}, {"ok": False}]},
                          "u2": {"model_checks": [{"ok": False}],
                                 "error_class": "unauthorized"}})
    controller = DesktopController(FakeDataSource({"status": {}}), cli=cli)
    result = controller.run_probe()
    assert result.effect == "probe" and result.payload["state"] == "done"
    assert result.payload["verified"] == 1 and result.payload["failed"] == 1
    assert "1 个上游有模型通过" in result.message
    assert "unauthorized" in result.message        # 原因要能直接照着修
    assert cli.calls[1] == ["upstream", "probe", "--id", "u1"]


def test_probe_run_without_upstreams_points_at_the_right_page():
    controller = DesktopController(FakeDataSource({"status": {}}),
                                   cli=FakeCli(listing={"upstreams": []}))
    result = controller.run_probe()
    assert result.ok is False
    assert "提供 Token Plan" in result.message


def test_probe_run_uses_the_async_channel_when_available():
    cli = FakeCli(listing={"upstreams": [{"id": "u1"}]},
                  probes={"u1": {"model_checks": [{"ok": True}]}})
    controller = DesktopController(FakeDataSource({"status": {}}), cli=cli)
    started: list[int] = []
    controller.probe_via = lambda: bool(started.append(1)) or True
    result = controller.action("probe.run")
    assert started == [1], "探针没有走异步通道（会冻窗口）"
    assert result.payload == {"state": "probing"}
    assert cli.calls == [], "动作里同步跑了探测"


def test_probe_run_falls_back_to_sync_without_a_worker():
    cli = FakeCli(listing={"upstreams": [{"id": "u1"}]},
                  probes={"u1": {"model_checks": [{"ok": True}]}})
    controller = DesktopController(FakeDataSource({"status": {}}), cli=cli)
    result = controller.action("probe.run")
    assert result.payload["state"] == "done" and result.payload["verified"] == 1
    assert result.effect == "probe"


def test_probe_run_never_raises_on_a_broken_cli():
    class Boom(FakeCli):
        def __call__(self, args):
            raise ProviderError("CLI 崩了", hint="看日志")

    controller = DesktopController(FakeDataSource({"status": {}}),
                                   cli=Boom(listing={}))
    result = controller.action("probe.run")
    assert result.ok is False
    assert result.message


# ─────────────────────────────────────── 真窗口：两张表都画出来、探针在最上面
@needs_tk
def test_models_page_renders_two_tables_and_probe_can_be_probed():
    """D 的窗口级验收：探针表是主表（详情抽屉服务它），云端目录是第二张表。"""
    controller = desktop_app.make_controller(home=None, demo=True)
    app = desktop_app.DesktopApp(controller)
    try:
        app.show_view("models")
        app._root.update()
        primary, tree = app._tables[0]
        assert primary.columns[:3] == ("模型", "上游", "最近探针")
        assert len(app._tables) == 2, "云端目录应该是第二张表"
        assert app._table is primary, "主表必须是探针表（详情抽屉/窄窗文案只服务主表）"
        assert tree.winfo_exists()
        # 演示数据里 4 行探针结论都进树
        assert len(tree.get_children()) == 4
        # 「跑一次探针」在演示模式不联网：点下去只播报，不派 worker
        assert app._controller.probe_via is not None
    finally:
        app.stop()


@needs_tk
def test_probe_action_from_the_page_reports_through_the_worker():
    """点「跑一次探针」→ 走 probe_via（不冻窗口）→ 结果回投后重画探针表。"""
    cli = FakeCli(listing={"upstreams": [{"id": "u1"}]},
                  probes={"u1": {"model_checks": [{"ok": True}]}})
    controller = DesktopController(FakeDataSource(dict(desktop_app.demo_snapshot().sections),
                                                 source="local"), cli=cli)
    app = desktop_app.DesktopApp(controller)
    try:
        app.show_view("models")
        began = time.monotonic()
        app._on_action("probe.run")
        assert time.monotonic() - began < 0.2, "点「跑一次探针」被网络阻塞了"
        for _ in range(600):
            app._root.update()
            if not app._probe_worker.busy:
                break
            time.sleep(0.01)
        assert cli.calls[-1] == ["upstream", "probe", "--id", "u1"]
        assert app._probe_worker.busy is False
    finally:
        app.stop()
