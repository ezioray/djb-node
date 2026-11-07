# SPDX-License-Identifier: Apache-2.0
"""``ui_kit`` 的**纯几何**层：不 import tkinter，因此无图形环境也能单测。

为什么单独拆一个模块
====================

图形环境在 NAS/CI 上是没有的（``tests/test_desktop_app.py`` 明确要求 import 整个
``djb_node.desktop.app`` 不得把 ``tkinter`` 拉进 ``sys.modules``）。而「像不像设计样」
最容易做错的地方恰好是**算**的部分：仪表环的弧度、24 根柱子的槽位、圆角矩形的顶点。
把这些算法与 Tk 解耦，就能在没有窗口的机器上用普通断言把错误顶住，
Tk 只负责把算好的坐标画出来。

本模块只做算术与坐标，不取色（颜色一律由调用方从 ``theme`` 传入）。
"""
from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "MONO_CHAIN", "SANS_CHAIN", "bar_heights", "bar_points", "bar_slots", "clamp01",
    "gauge_arc", "pick_family", "round_rect_points",
]

#: 正文/标题字体回退链（01-DESIGN-SPEC §2.1：Windows 统一用雅黑 UI，避免中英混排基线跳）
SANS_CHAIN: tuple[str, ...] = (
    "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC",
    "Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans",
)

#: 等宽/数字回退链（数字必须等宽，否则指标会抖）
MONO_CHAIN: tuple[str, ...] = (
    "Cascadia Mono", "Cascadia Code", "Consolas", "Menlo", "DejaVu Sans Mono",
)


def pick_family(available: Iterable[str] | None, chain: Sequence[str] = SANS_CHAIN) -> str:
    """从系统已装字体里挑第一个命中回退链的族名；都没有就用链首（Tk 会自己再兜一层）。

    ``available`` 传 ``tkinter.font.families()``；传 ``None``/空表示「查不到」。
    比较忽略大小写：Windows 返回的族名大小写与 registry 不完全一致。
    """
    names = {str(item).strip().casefold() for item in (available or ()) if str(item).strip()}
    for family in chain:
        if family.casefold() in names:
            return family
    return chain[0] if chain else "TkDefaultFont"


def clamp01(value: float) -> float:
    """夹到 [0, 1]；``None``/非法值当 0（图表缺数据时画空槽，不画假高度）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:                       # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def gauge_arc(ratio: float, start: float = 225.0, sweep: float = 270.0) -> tuple[float, float]:
    """仪表环的 ``(start, extent)``：从 7:30 位置起**顺时针**扫 ``sweep`` 度。

    Tk 的 ``create_arc`` 角度以 3 点钟为 0°、逆时针为正，所以顺时针要用负 ``extent``。
    起点 225° = 左下（设计样的开口朝下）。``ratio`` 会被夹到 [0,1]——数据缺失时不画假进度。
    """
    return float(start), -float(sweep) * clamp01(ratio)


def _round_corners(x0: float, y0: float, x1: float, y1: float, r: float,
                   corners: str) -> tuple[float, ...]:
    """圆角矩形的顶点序列（配合 ``create_polygon(smooth=True)`` 用）。

    ``smooth=True`` 的多边形会把每个顶点当控制点做二次插值，所以「在角上放三个点」
    （进角点、角点、出角点）就能得到一段圆弧。``corners`` 取 ``"all"`` / ``"top"``。
    """
    radius = max(0.0, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    top_left = top_right = radius
    if corners == "top":                       # 柱状图：只有上面两个角是圆的
        bottom_left = bottom_right = 0.0
    else:
        bottom_left = bottom_right = radius
    points: list[float] = []
    # 上边（左 → 右）
    points += [x0 + top_left, y0, x1 - top_right, y0, x1 - top_right, y0]
    # 右上角
    points += [x1, y0, x1, y0 + top_right]
    # 右边（下 → 上反过来写也行，这里从上到下）
    points += [x1, y1 - bottom_right, x1, y1 - bottom_right]
    # 右下角
    points += [x1, y1, x1 - bottom_right, y1]
    # 下边（右 → 左）
    points += [x0 + bottom_left, y1, x0 + bottom_left, y1]
    # 左下角
    points += [x0, y1, x0, y1 - bottom_left]
    # 左边（下 → 上）
    points += [x0, y0 + top_left, x0, y0 + top_left]
    # 回到左上角
    points += [x0, y0, x0 + top_left, y0]
    return tuple(points)


def round_rect_points(x0: float, y0: float, x1: float, y1: float,
                      r: float = 11.0) -> tuple[float, ...]:
    """整张圆角矩形的顶点序列（卡片 / 按钮 / 胶囊）。"""
    return _round_corners(x0, y0, x1, y1, r, "all")


def bar_points(x0: float, y0: float, x1: float, y1: float,
               r: float = 2.0) -> tuple[float, ...]:
    """柱子的顶点序列：只有上面两个角是圆的（设计样 ``border-radius:2px 2px 0 0``）。"""
    return _round_corners(x0, y0, x1, y1, r, "top")


def bar_slots(count: int, width: float, gap: float = 4.0,
              pad_left: float = 0.0, pad_right: float = 0.0) -> list[tuple[float, float]]:
    """把 ``width`` 宽的区域切成 ``count`` 个等宽槽位，返回每根的 ``(x0, x1)``。

    柱子本身不撑满槽位：``gap`` 是**柱间空隙**，均分到槽位两侧，所以
    ``x1 - x0 < slot``，相邻两根之间正好留 ``gap``。宽度不够时返回 0.1px 宽的退化槽，
    调用方据此跳过绘制（窄窗不画成一条实心块）。
    """
    if count <= 0:
        return []
    usable = max(0.0, float(width) - float(pad_left) - float(pad_right))
    slot = usable / float(count)
    bar = max(0.0, slot - float(gap))
    if bar <= 0.1:
        return [(0.0, 0.0)] * count
    offset = float(pad_left) + (slot - bar) / 2.0
    return [(offset + index * slot, offset + index * slot + bar) for index in range(count)]


def bar_heights(values: Sequence[float], height: float, floor: float = 2.0) -> list[float]:
    """把一组 0..1 的比例换成像素高度（至少 ``floor``，0 值也画一条可见的底槽）。"""
    return [max(float(floor), clamp01(value) * float(height)) for value in values]
