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

import sys
from typing import Iterable, Sequence

__all__ = [
    "CJK_NAME_HINTS", "MONO_CHAIN", "MONO_CHAINS", "SANS_CHAIN", "SANS_CHAINS",
    "bar_heights", "bar_points", "bar_slots", "clamp01", "gauge_arc",
    "looks_like_cjk_family", "mono_chain", "pick_family", "pick_mono_family",
    "pick_sans_family", "platform_key", "round_rect_points", "sans_chain",
]

#: 正文/标题字体回退链（01-DESIGN-SPEC §2.1：Windows 统一用雅黑 UI，避免中英混排基线跳）
#: 这是**跨平台兜底**链（历史调用方与既有断言按它写），按平台分派的链见 :data:`SANS_CHAINS`。
SANS_CHAIN: tuple[str, ...] = (
    "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC",
    "Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans",
)

#: 等宽/数字回退链（数字必须等宽，否则指标会抖）
MONO_CHAIN: tuple[str, ...] = (
    "Cascadia Mono", "Cascadia Code", "Consolas", "Menlo", "DejaVu Sans Mono",
)

#: 按平台分派的字族链（R2 裁决 01-FONT-DECISION §2.2，逐字照抄那张表）
#:
#: * 每一链的**前几项必须是含 CJK 字形的族**——中文只能由 CJK 族承载，
#:   不许依赖 Tk 的隐式 fallback（隐式 fallback 会跳基线，见 01-FONT-DECISION §1 F1）。
#: * 链尾的纯拉丁族（``Segoe UI`` / ``Helvetica Neue`` / ``DejaVu Sans``）只作最后兜底：
#:   系统里连一个 CJK 族都没有时，至少还能把拉丁与数字画出来。
SANS_CHAINS: dict[str, tuple[str, ...]] = {
    "windows": ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"),
    "darwin": ("PingFang SC", "Hiragino Sans GB", "Helvetica Neue"),
    "linux": ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "DejaVu Sans"),
}

#: 等宽链（**只许 ASCII**：回执号/时间戳/URL/版本/快捷键；中文一律走 sans）
MONO_CHAINS: dict[str, tuple[str, ...]] = {
    "windows": ("Cascadia Mono", "Consolas"),
    "darwin": ("Menlo", "Monaco"),
    "linux": ("DejaVu Sans Mono", "Noto Sans Mono"),
}

#: CJK 字体名特征：链上一个都没命中时，用它在「系统里实际装了什么」里做**可证伪的探测**
#: （只有 Noto Sans CJK JP / 只有文泉驿 这类机器也要选到含中文的族）。
CJK_NAME_HINTS: tuple[str, ...] = (
    "cjk", "yahei", "雅黑", "pingfang", "hiragino", "heiti", "黑体", "songti", "宋体",
    "simhei", "simsun", "jhenghei", "yenhei", "wenquanyi", "文泉驿", "wqy",
    "source han", "思源", "sarasa", "更纱", "droid sans fallback",
    "noto sans sc", "noto sans tc", "noto sans jp", "noto sans kr",
    "noto serif sc", "noto serif jp", "fandol", "ar pl", "uming", "ukai",
)

#: 名字看起来是等宽/符号/表情的族：不能当正文族（中文会被塞进等宽、数字不对齐）
_MONO_OR_SYMBOL_HINTS: tuple[str, ...] = (
    "mono", "courier", "consolas", "menlo", "monaco", "code", "terminal", "typewriter",
    "emoji", "symbol", "dingbat", "webding", "wingding", "math", "music", "ding",
)

#: 再放宽一档：衬线族也排除（「有没有字可画」的兜底不该落到 Times 上）
_NON_TEXT_HINTS: tuple[str, ...] = _MONO_OR_SYMBOL_HINTS + (
    "serif", "times", "georgia", "book", "school", "ancient", "script", "handwriting",
)

#: CJK 族内部再排序：同一个族装了多地区版本时，中文界面优先简体
_CJK_REGION_ORDER: tuple[str, ...] = ("sc", "cn", "gb", "hans", "tc", "tw", "hk", "jp", "kr", "hant")

#: 名字看起来像 CJK 衬线（宋/明朝体）：能兜到就比纯拉丁族好，但排在 CJK 无衬线之后
_CJK_SERIF_HINTS: tuple[str, ...] = ("song", "宋", "ming", "明朝", "mincho", "batang", "serif", "fangsong", "仿宋")


def platform_key(platform: str | None = None) -> str:
    """``windows`` / ``darwin`` / ``linux``（其余一律 ``linux``）。

    用 ``sys.platform`` 判定、不 import tkinter，因此可以在无图形环境里断言平台分派。
    """
    value = (platform if platform is not None else sys.platform) or ""
    value = str(value).lower()
    if value.startswith("win"):
        return "windows"
    if value.startswith("darwin"):
        return "darwin"
    return "linux"


def sans_chain(platform: str | None = None) -> tuple[str, ...]:
    """该平台的正文族回退链（含 CJK 的族在前）。"""
    return SANS_CHAINS[platform_key(platform)]


def mono_chain(platform: str | None = None) -> tuple[str, ...]:
    """该平台的等宽族回退链（只承载 ASCII）。"""
    return MONO_CHAINS[platform_key(platform)]


def looks_like_cjk_family(name: str) -> bool:
    """族名里有没有 CJK 特征（``CJK`` / 雅黑 / 苹方 / 冬青 / 文泉驿 / 思源 …）。"""
    lowered = str(name or "").strip().casefold()
    return any(hint in lowered for hint in CJK_NAME_HINTS)


def _looks_non_text(name: str) -> bool:
    """看着是等宽/衬线/表情/符号族：不适合承载正文（含中文）。"""
    lowered = str(name or "").strip().casefold()
    return any(hint in lowered for hint in _NON_TEXT_HINTS)


def _looks_mono_or_symbol(name: str) -> bool:
    """比 :func:`_looks_non_text` 宽一档：CJK 衬线族在这里**放行**（排在无衬线之后）。"""
    lowered = str(name or "").strip().casefold()
    return any(hint in lowered for hint in _MONO_OR_SYMBOL_HINTS)


def _cjk_sort_key(name: str) -> tuple[int, int, str]:
    """CJK 族的优先序：先简体地区 → 无衬线 → 名字（保证同一批字库上结果稳定）。"""
    lowered = str(name).strip().casefold()
    region = len(_CJK_REGION_ORDER)
    for index, token in enumerate(_CJK_REGION_ORDER):
        if token and token in lowered:
            region = index
            break
    serif = 1 if any(hint in lowered for hint in _CJK_SERIF_HINTS) else 0
    return region, serif, lowered


def _available_map(available: Iterable[str] | None) -> dict[str, str]:
    """``casefold 名字 → 系统返回的原始写法``（Windows 的族名大小写与 registry 不完全一致）。"""
    return {str(item).strip().casefold(): str(item).strip()
            for item in (available or ()) if str(item).strip()}


def _known_sans_chains() -> set[tuple[str, ...]]:
    chains = {tuple(item.casefold() for item in SANS_CHAIN)}
    chains |= {tuple(item.casefold() for item in chain) for chain in SANS_CHAINS.values()}
    return chains


def pick_family(available: Iterable[str] | None, chain: Sequence[str] = SANS_CHAIN, *,
                cjk_fallback: bool | None = None) -> str:
    """从系统已装字体里挑第一个命中回退链的族名；都没有就用链首（Tk 会自己再兜一层）。

    ``available`` 传 ``tkinter.font.families()``；传 ``None``/空表示「查不到」。
    比较忽略大小写：Windows 返回的族名大小写与 registry 不完全一致。

    ``cjk_fallback``（默认对**已知的正文链**自动开启，等宽链自动关闭）新增三层探测，
    解决「链上一个都没装」的真实情况（01-FONT-DECISION §2.2 / R2 任务单要求）：

    1. 链上**含 CJK 的族**按链序命中；
    2. 系统里任何含 CJK 特征的无衬线族（按 简体 → 其它地区 → 无衬线 排序）；
    3. 链上其余族（纯拉丁兜底，如 ``Segoe UI`` / ``DejaVu Sans``）；
    4. 系统里任何非等宽/非衬线的族；最后才是链首。

    为什么把「CJK 探测」插在纯拉丁族**前面**：只有 ``DejaVu Sans`` + ``Noto Sans CJK JP``
    的机器上，选 ``DejaVu Sans`` 会让中文落到 Tk 隐式 fallback（基线跳），
    而 ``Noto Sans CJK JP`` 至少能自洽地画出中文。
    """
    by_case = _available_map(available)
    if cjk_fallback is None:
        cjk_fallback = tuple(item.casefold() for item in chain) in _known_sans_chains()
    if cjk_fallback:
        for family in chain:                    # 1) 链上的 CJK 族
            if looks_like_cjk_family(family) and family.casefold() in by_case:
                return family                       # 回链上写法（Tk 族名匹配不分大小写）
        fuzzy = [name for name in by_case.values()
                 if looks_like_cjk_family(name) and not _looks_mono_or_symbol(name)]
        if fuzzy:                               # 2) 系统里别处的 CJK 族（衬线排最后）
            return min(fuzzy, key=_cjk_sort_key)
    for family in chain:                        # 3) 链上其它族（纯拉丁兜底）
        if family.casefold() in by_case and not (cjk_fallback and looks_like_cjk_family(family)):
            return family
    if cjk_fallback:
        others = [name for name in by_case.values() if not _looks_non_text(name)]
        if others:                              # 4) 至少有字可画的族
            return min(others, key=lambda item: item.casefold())
    return chain[0] if chain else "TkDefaultFont"


def pick_sans_family(available: Iterable[str] | None, platform: str | None = None) -> str:
    """正文族：该平台的链 + CJK 探测（01-FONT-DECISION §2.2）。"""
    return pick_family(available, sans_chain(platform), cjk_fallback=True)


def pick_mono_family(available: Iterable[str] | None, platform: str | None = None) -> str:
    """等宽族：链序命中；一个都没装时退到系统里名字像等宽的族，最后才是链首。

    不做 CJK 探测——等宽族**只承载 ASCII**，中文由 :meth:`ui_kit.Fonts.for_text` 分派给正文族。
    """
    chain = mono_chain(platform)
    by_case = _available_map(available)
    for family in chain:
        if family.casefold() in by_case:
            return family
    fuzzy = [name for name in by_case.values()
             if any(hint in name.casefold() for hint in ("mono", "consolas", "courier", "code"))]
    if fuzzy:
        return min(fuzzy, key=lambda item: item.casefold())
    return chain[0] if chain else "TkFixedFont"


#: 侧栏收到下拉的宽度（01 §4.6：< 760 用下拉导航，不做 62px 图标塔）
NARROW_WIDTH = 760
#: 总览双栏断点（02 §4 / G-11：>= 1180 双栏，其余单栏；只做两档）
TWO_COL_WIDTH = 1180
#: 卡片一行几张的中间档（760..1180 用两列，免得四宫格被挤成四条）
CARD_GRID_WIDTH = 900


def layout_mode(width: int) -> str:
    """宽度 → ``narrow``（<760，下拉导航）/ ``single``（<1180，单列）/ ``two``（双列）。"""
    if width < NARROW_WIDTH:
        return "narrow"
    if width < TWO_COL_WIDTH:
        return "single"
    return "two"


def cards_per_row(width: int) -> int:
    """指标卡一行几张：窄 1 / 中 2 / 宽 4（四宫格是设计值，但绝不把卡片挤出窗口）。"""
    if width < NARROW_WIDTH:
        return 1
    if width < TWO_COL_WIDTH:
        return 2
    return 4


def card_columns(count: int, limit: int) -> int:
    """在「最多 ``limit`` 列」的前提下选列数：优先能整除张数的，别让最后一行只剩一张。

    ``6 张 / 上限 4`` → 3 列（2 行整齐），``5 张`` → 3 列（3+2），``4 张`` → 4 列。
    """
    limit = max(1, int(limit))
    if count <= 0:
        return limit
    if count <= limit:
        return max(1, count)
    for candidate in (limit, 3, 2):
        if candidate <= limit and count % candidate == 0:
            return candidate
    return 3 if limit >= 3 else limit


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
