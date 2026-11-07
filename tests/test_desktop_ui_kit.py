# SPDX-License-Identifier: Apache-2.0
"""``ui_geom``（纯几何）+ 图表 token 的回归：无窗口也能跑。

为什么这些用例不建窗口
======================

NAS/CI 上没有 tkinter（``test_desktop_app.py::test_module_import_does_not_touch_tkinter``
把「import ``djb_node.desktop.app`` 不得拉进 tkinter」写成了硬断言）。而「像不像设计样」最容易
算错的是仪表环弧度、24 根柱子的槽位、圆角顶点——这些都在 :mod:`djb_node.desktop.ui_geom` 里，
只做算术，可以在这类机器上直接断言。Tk 只负责把算好的坐标画出来（Windows 实拍负责那一半）。

本文件同时锁两件与裁决有关的事：
* ``CHART_TOKENS`` **不占** ``PALETTES`` 的键（色板键集不变 ⇒ 双主题的 pin 断言不会红）；
* ``mint_bright`` 只能做填充面（对白卡 < 3:1），**不许当文字色**——这条写成断言，
  以后谁拿它写正文都会在这里挂。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from djb_node.desktop import theme, ui_geom
from djb_node.gui.theme import contrast_ratio

# ────────────────────────────────────────────────────────────── 图表 token（00 §2）
def test_chart_tokens_are_a_separate_namespace():
    """图表 token 不占色板键：两套主题的 PALETTES 键集逐字节不变。"""
    for name in theme.available():
        assert not (set(theme.CHART_TOKENS[name]) & set(theme.palette(name))), name


def test_chart_tokens_follow_the_colour_decision_file():
    """00-COLOR-DECISION §2 的映射：新 token 从既有 token 派生，只有 mint_bright 是字面值。"""
    day = theme.CHART_TOKENS["day"]
    assert day["mint_bright"] == "#6CC0A1"            # 裁决指定的设计强调色
    assert day["ring_track"] == theme.palette("day")["mint3"]
    assert day["bar_mint"] == theme.palette("day")["mint3"]
    assert day["bar_violet"] == theme.palette("day")["violet3"]
    assert day["bar_pink"] == theme.palette("day")["pink3"]
    assert day["bar_peak"] == theme.palette("day")["mint2"]
    assert day["card_line"] == theme.palette("day")["line"]
    assert set(theme.CHART_TOKENS["day"]) == set(theme.CHART_TOKENS["night"])


def test_mint_bright_is_a_fill_never_a_text_colour():
    """``#6CC0A1`` 对白卡只有 1.88:1：只许填充（仪表弧/柱顶/active 左条）。

    夜间 ``mint_bright`` 取的是夜间 ``mint``（``#8FD9BE``），在深底上是亮面，
    对卡片底 11.1:1 —— 所以"不能当文字色"这条只对 day 成立，夜间由 ``mint_text`` 兜底。
    """
    day = theme.CHART_TOKENS["day"]
    assert contrast_ratio(day["mint_bright"], theme.palette("day")["surface"]) < 3.0
    # 需要「薄荷色文字」时一律用 mint_text（两套主题都 ≥4.5:1）
    for name in theme.available():
        palette = theme.palette(name)
        assert contrast_ratio(palette["mint_text"], palette["surface"]) >= 4.5, name


def test_color_resolves_palette_then_chart_tokens():
    theme.set_active("day")
    assert theme.color("bg") == theme.palette("day")["bg"]
    assert theme.color("mint_bright") == "#6CC0A1"
    assert theme.color("no-such-token", "#123456") == "#123456"
    theme.set_active("night")
    try:
        assert theme.color("mint_bright") == theme.palette("night")["mint"]
        assert theme.color("bg") == theme.palette("night")["bg"]
    finally:
        theme.set_active("day")


def test_chart_helper_reads_a_named_theme():
    assert theme.chart("ring_track", "night") == theme.palette("night")["mint3"]
    assert theme.chart("mint_bright", "day") == "#6CC0A1"
    assert theme.chart("no-such-token", "day") == ""


# ────────────────────────────────────────────────────────────── 字体族
def test_pick_family_is_case_insensitive_and_prefers_the_chain():
    available = ["segoe ui", "MICROSOFT YAHEI UI", "Consolas"]
    assert ui_geom.pick_family(available, ui_geom.SANS_CHAIN) == "Microsoft YaHei UI"
    assert ui_geom.pick_family(available, ui_geom.MONO_CHAIN) == "Consolas"


def test_pick_family_falls_back_to_the_head_of_the_chain():
    assert ui_geom.pick_family([], ui_geom.SANS_CHAIN) == ui_geom.SANS_CHAIN[0]
    assert ui_geom.pick_family(None, ui_geom.MONO_CHAIN) == ui_geom.MONO_CHAIN[0]


def test_mono_chain_keeps_windows_fonts_before_generic_ones():
    """等宽回退链必须先把 Windows 自带字体排前面（01 §2.1）。"""
    assert ui_geom.MONO_CHAIN[0] == "Cascadia Mono"
    assert "Consolas" in ui_geom.MONO_CHAIN
    assert ui_geom.SANS_CHAIN[0] == "Microsoft YaHei UI"


# ────────────────────────────────────────────────────────────── 仪表环
def test_gauge_arc_starts_at_bottom_left_and_sweeps_clockwise():
    start, extent = ui_geom.gauge_arc(1.0)
    assert (start, extent) == (225.0, -270.0)
    # 顺时针 = 负 extent（Tk 逆时针为正）
    assert ui_geom.gauge_arc(0.5)[1] == pytest.approx(-135.0)
    assert ui_geom.gauge_arc(0.0)[1] == pytest.approx(0.0)


def test_gauge_arc_clamps_and_treats_missing_as_zero():
    assert ui_geom.gauge_arc(1.8)[1] == pytest.approx(-270.0)
    assert ui_geom.gauge_arc(-0.4)[1] == pytest.approx(0.0)
    assert ui_geom.gauge_arc(None)[1] == pytest.approx(0.0)          # type: ignore[arg-type]
    assert ui_geom.gauge_arc("x")[1] == pytest.approx(0.0)           # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────── 柱图槽位
def test_bar_slots_fill_the_width_and_leave_the_gap():
    slots = ui_geom.bar_slots(24, 480.0, gap=4.0)
    assert len(slots) == 24
    widths = [x1 - x0 for x0, x1 in slots]
    assert all(abs(width - 16.0) < 0.001 for width in widths)        # 480/24 - 4
    # 相邻柱子之间的空隙正好是 gap
    for (_, previous_end), (next_start, _) in zip(slots, slots[1:]):
        assert next_start - previous_end == pytest.approx(4.0)
    assert slots[0][0] == pytest.approx(2.0)                         # gap 均分到两侧
    assert slots[-1][1] == pytest.approx(478.0)


def test_bar_slots_degenerate_width_is_skipped_not_squashed():
    """宽度不够时给退化槽（0 宽），调用方跳过绘制——不许画成一条实心块。"""
    assert ui_geom.bar_slots(24, 10.0, gap=4.0) == [(0.0, 0.0)] * 24
    assert ui_geom.bar_slots(0, 400.0) == []


def test_bar_heights_keep_a_visible_floor_and_clamp():
    heights = ui_geom.bar_heights((0.0, 0.5, 1.0, 2.0), 84.0, floor=3.0)
    assert heights[0] == 3.0
    assert heights[1] == pytest.approx(42.0)
    assert heights[2] == pytest.approx(84.0)
    assert heights[3] == pytest.approx(84.0)                         # 越界夹住


# ────────────────────────────────────────────────────────────── 圆角
def test_round_rect_points_stay_inside_the_box():
    points = ui_geom.round_rect_points(0, 0, 100, 40, 11)
    xs, ys = points[0::2], points[1::2]
    assert min(xs) == 0 and max(xs) == 100
    assert min(ys) == 0 and max(ys) == 40
    assert len(points) % 2 == 0 and len(points) >= 16


def test_round_rect_radius_is_clamped_to_half_the_shorter_side():
    """半径大于半高时不越界（1px 高的小徽标也不会画出负坐标）。"""
    points = ui_geom.round_rect_points(0, 0, 40, 2, 11)
    ys = points[1::2]
    assert min(ys) >= 0 and max(ys) <= 2


def test_bar_points_only_round_the_top():
    """柱状图只有上面两个角是圆的：底部两个角必须是硬角（y1 上不得有内缩）。"""
    points = ui_geom.bar_points(0, 0, 10, 100, 2)
    pairs = list(zip(points[0::2], points[1::2]))
    bottom = [pair for pair in pairs if pair[1] == 100]
    assert (0, 100) in bottom and (10, 100) in bottom


# ────────────────────────────────────────────────────────────── clamp
@pytest.mark.parametrize("value,expected", ((None, 0.0), ("x", 0.0), (float("nan"), 0.0),
                                            (-1, 0.0), (0.25, 0.25), (5, 1.0)))
def test_clamp01(value, expected):
    assert ui_geom.clamp01(value) == expected


# ────────────────────────────────────────────────────────────── import 纪律
def test_ui_kit_is_not_imported_by_the_app_module():
    """组件层带 tkinter：``djb_node.desktop.app`` 的模块级 import 不许把它拉进来。"""
    code = ("import sys; import djb_node.desktop.app;"
            "print('tkinter' in sys.modules,"
            " 'djb_node.desktop.ui_kit' in sys.modules,"
            " 'djb_node.desktop.ui_geom' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split() == ["False", "False", "False"]
