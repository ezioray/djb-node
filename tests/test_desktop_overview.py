# SPDX-License-Identifier: Apache-2.0
"""总览页三块投影（主卡 / 贡献节奏 / 右栏）——纯函数，无 Tk。

这些断言盯的是「界面上说了什么话」：缺数据只能是「—」或「暂不可用」，
绝不把没数据画成 0；节奏图的时间窗锚在 ``Snapshot.fetched_at``，不是本机当前时间。
"""
from __future__ import annotations

import copy

import pytest

from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot


def overview(snapshot=None):
    return vw.build_view("overview", snapshot if snapshot is not None else demo_snapshot())


def patched(**sections):
    """在演示快照上按数据域覆写（其余保持真实形状）。"""
    snapshot = demo_snapshot()
    data = dict(snapshot.sections)
    for name, value in sections.items():
        data[name] = value
    return ds.Snapshot(data, fetched_at=snapshot.fetched_at, source=snapshot.source)


def receipts(*rows):
    return {"rows": list(rows), "total": len(rows)}


# ───────────────────────────────────────────────────────────── 主卡
def test_hero_ring_shows_gpu_utilization_from_the_gpu_section():
    hero = overview().hero
    assert hero.gauge is not None
    assert hero.gauge.label == "GPU 利用率"
    assert hero.gauge.ratio == pytest.approx(0.62)
    assert hero.gauge.value == "62%"


def test_hero_ring_draws_no_progress_when_utilization_is_missing():
    gpu = copy.deepcopy(demo_snapshot().sections["gpu"])
    gpu["devices"][0].pop("utilization_pct")
    hero = overview(patched(gpu=gpu)).hero
    # 没有数字 → ratio 是 None（画轨道），中心写「—」，绝不写 0%
    assert hero.gauge.ratio is None
    assert hero.gauge.value == "—"


def test_hero_runtime_is_unavailable_rather_than_invented():
    """本机数据源还没有 uptime 字段：运行时长只能是「—」，不许编一个。"""
    assert overview().hero.runtime == "—"


def test_hero_runtime_uses_uptime_only_when_the_field_exists():
    status = dict(demo_snapshot().sections["status"])
    status["uptime_s"] = 3725
    assert overview(patched(status=status)).hero.runtime == "01:02:05"


def test_hero_metrics_never_turn_missing_values_into_zero():
    gpu = copy.deepcopy(demo_snapshot().sections["gpu"])
    gpu["devices"][0].pop("memory_used_mb")
    gpu["devices"][0].pop("temperature_c")
    hero = overview(patched(gpu=gpu)).hero
    by_label = {metric.label: metric for metric in hero.metrics}
    assert by_label["显存"].value == "—"
    assert by_label["温度"].value == "—"
    assert "0" not in (by_label["显存"].value, by_label["温度"].value)


def test_hero_hint_tracks_the_access_binding():
    base = demo_snapshot()
    bound = overview().hero
    assert bound.hint == "已授权 1 个客户端"
    access = dict(base.sections["access"])
    access["binding"] = {"state": "unbound"}
    assert overview(patched(access=access)).hero.hint == "还没有客户端授权"
    unavailable = ds.Snapshot({name: ds.Unavailable("测试：未接线") for name in ds.SECTIONS})
    assert overview(unavailable).hero.hint == "授权状态暂不可用"


def test_hero_actions_pause_and_resume_are_mutually_exclusive():
    hero = overview().hero
    actions = {action.key: action for action in hero.actions if action.key}
    assert actions["yield.pause"].enabled is True
    assert actions["yield.resume"].enabled is False
    assert actions["yield.resume"].reason                     # 置灰必须写原因
    status = dict(demo_snapshot().sections["status"])
    status["state"] = "paused"
    paused = {action.key: action for action in overview(patched(status=status)).hero.actions
              if action.key}
    assert paused["yield.pause"].enabled is False
    assert paused["yield.resume"].enabled is True


def test_hero_schedule_entry_is_a_page_jump_not_a_backend_call():
    """「调整时段」只是跳到让路页：它不许带动作 key（那会被当成后端请求发出去）。"""
    nav = [action for action in overview().hero.actions if action.nav]
    assert [action.nav for action in nav] == ["yield"]
    assert all(action.key == "" for action in nav)


# ───────────────────────────────────────────────────────────── 贡献节奏
def test_chart_has_one_bucket_per_hour_of_the_last_day():
    chart = overview().chart
    assert len(chart.values) == vw.CHART_HOURS == 24
    assert all(0.0 <= value <= 1.0 for value in chart.values)
    assert max(chart.values) == pytest.approx(1.0)            # 最高的那根永远满格
    assert len(chart.labels) == 5
    assert chart.labels[-1] == "现在"                          # 末端是「现在」，不是重复的刻度


def test_chart_window_is_anchored_on_fetched_at_not_wall_clock():
    """演示快照的 fetched_at 是 2026-11-01T12:00+0800：桶按那一刻算，跑测试的时间无关。"""
    chart = overview().chart
    assert "04:00" in chart.peak                               # 12,288 tokens 的峰值桶
    assert chart.peak.startswith("峰值 12,288 tokens")


def test_chart_sums_tokens_in_the_same_hour():
    rows = ({"receipt_id": "a", "created_at": "2026-11-01T02:10:00Z", "tokens": 100},
            {"receipt_id": "b", "created_at": "2026-11-01T02:50:00Z", "tokens": 200})
    chart = overview(patched(receipts=receipts(*rows))).chart
    assert chart.empty == ""
    assert sum(1 for value in chart.values if value > 0) == 1  # 同一小时只有一根柱
    assert "合计 300 tokens" in chart.foot


def test_chart_ignores_receipts_outside_the_window():
    rows = ({"receipt_id": "a", "created_at": "2026-10-01T02:00:00Z", "tokens": 999999},
            {"receipt_id": "b", "created_at": "2026-11-01T02:00:00Z", "tokens": 100})
    chart = overview(patched(receipts=receipts(*rows))).chart
    assert "999,999" not in chart.foot
    assert "1 张收据" in chart.foot


def test_chart_says_unavailable_without_receipts():
    chart = overview(patched(receipts=receipts())).chart
    assert chart.values == ()
    assert chart.empty == "暂不可用（当前没有收据时间戳）"
    assert "暂不可用" in overview(patched(receipts=receipts())).plain_text()


def test_chart_says_unavailable_when_timestamps_are_unreadable():
    rows = ({"receipt_id": "a", "created_at": "不是时间", "tokens": 100},)
    chart = overview(patched(receipts=receipts(*rows))).chart
    assert chart.values == ()
    assert chart.empty                                       # 解析不了就不画柱，也不猜时间


def test_chart_is_unavailable_when_the_receipts_section_is_down():
    unavailable = ds.Snapshot({name: ds.Unavailable("测试：未接线") for name in ds.SECTIONS})
    chart = overview(unavailable).chart
    assert chart.values == () and chart.empty


# ─────────────────────────────────────────────────────────── 右栏三张卡
def test_rail_has_setup_today_and_throttle():
    rail = overview().rail
    assert [card.kind for card in rail] == ["setup", "stats", "throttle"]
    assert all(card.kicker and card.title for card in rail)


def test_setup_card_is_read_only_and_hands_writing_to_the_access_page():
    setup = overview().rail[0]
    assert setup.fields
    assert not any(field.editable for field in setup.fields)  # 同一个配置文件不许两处可写
    assert setup.fields[0].secret is True                     # 凭证只露掩码
    assert [action.nav for action in setup.actions] == ["access"]
    assert setup.note == "3 / 3" and setup.badge == "接入完成"


def test_setup_card_counts_steps_from_the_real_config():
    access = {"config": {"node_secret": "x", "price_mode": "auto"},
              "binding": {"state": "bound"}}
    setup = overview(patched(access=access)).rail[0]
    assert setup.note == "2 / 3"
    assert setup.badge == "待完成" and setup.badge_tone == "amber"


def test_today_card_has_four_cells_and_keeps_the_server_side_ledger():
    today = overview().rail[1]
    labels = [stat.label for stat in today.stats]
    assert labels == ["今日收益", "今日 tokens", "收据数", "验证通过"]
    assert today.stats[0].value == "6.12" and today.stats[0].unit == "USDT"
    assert "服务端记账口径" in today.foot


def test_today_card_says_unavailable_instead_of_zero_when_receipts_are_down():
    snapshot = patched(receipts=ds.Unavailable("测试：收据接口未接线"))
    today = overview(snapshot).rail[1]
    by_label = {stat.label: stat for stat in today.stats}
    assert by_label["收据数"].value == "—"
    assert by_label["验证通过"].value == "—"


def test_throttle_card_shows_limits_and_the_yield_mode():
    throttle = overview().rail[2]
    rows = dict((label, value) for label, value, _tone in throttle.rows)
    assert rows["并发上限"] == "4 路"
    assert rows["每日 tokens 上限"] == "200,000"
    assert rows["让路模式"] == vw.YIELD_MODE_SUMMARY["balanced"]
    assert throttle.toggle == ("自动让路", True)


def test_throttle_card_says_off_when_the_yield_daemon_is_stopped():
    yield_state = dict(demo_snapshot().sections["yield"])
    yield_state["yield_enabled"] = False
    throttle = overview(patched(**{"yield": yield_state})).rail[2]
    assert throttle.toggle == ("自动让路", False)
    assert throttle.badge == "已关闭" and throttle.badge_tone == "muted"


# ───────────────────────────────────────────────────── 证据（plain_text）
def test_plain_text_carries_the_whole_overview_so_screenshots_are_not_the_only_evidence():
    view = overview()
    text = view.plain_text()
    for needle in (view.hero.title, view.hero.gauge.label, view.chart.title,
                   view.chart.peak, view.rail[0].title, view.rail[1].title,
                   view.rail[2].title, "自动让路：开"):
        assert needle in text, needle


def test_overview_only_builds_the_dashboard_for_the_overview_view():
    for key in ("bench", "receipts", "logs"):
        view = vw.build_view(key, demo_snapshot())
        assert view.hero is None and view.chart is None and view.rail == ()


def test_overview_table_lists_the_newest_receipts_first():
    """「最近收据」= 最新几条在前（只改展示顺序，字段与口径不变）。"""
    rows = overview().table.rows
    stamps = [row.cells[1] for row in rows]
    assert 0 < len(stamps) <= 5
    assert stamps == sorted(stamps, reverse=True)


def test_recorded_receipts_read_as_in_flight_not_unknown():
    """``recorded``（服务端已记录、尚未校验）不许显示成「未知状态」。"""
    rows = ({"receipt_id": "a", "state": "recorded", "state_text": "已记录",
             "created_at": "2026-11-01T02:00:00Z", "tokens": 10},)
    row = overview(patched(receipts=receipts(*rows))).table.rows[0]
    assert row.cells[3].startswith(vw.MARK_WAIT)
    assert "未知" not in row.cells[3]
