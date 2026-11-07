# SPDX-License-Identifier: Apache-2.0
"""纯 Tk 设计组件（ui_kit）：圆角卡片 / 仪表环 / 柱图 / 徽标 / 指标格 / 导航项 / 图标。

设计源：``design/prototypes/desktop-client-v1/index.html``（v2.1 浅色）
规格：``reports/UI-20261107-DESKTOP-PARITY/01-DESIGN-SPEC.md``
裁决：``reports/UI-20261107-DESKTOP-PARITY/00-COLOR-DECISION.md``（沿用 Aurora 色板，不改色）

三条不可破的约束
================

1. **零第三方依赖**：只用 ``tkinter``（Canvas 画形状），不引入图片、字体文件、PIL。
   圆角/弧线/柱状全部是 ``create_polygon(smooth=True)`` 与 ``create_arc``。
2. **色值只来自主题**：本文件不写十六进制，一律 ``theme.color()`` / ``theme.chart()``。
   裁决定的那几个图表 token（``mint_bright`` 等）在 ``theme.CHART_TOKENS`` 里，
   ``PALETTES`` 逐字节不动。
3. **不伪造层级**：卡片 = ``surface`` 底 + 1px 描边（``card_line``），**不加阴影、不加深色底**。
   设计样的层级只靠描边 + 间距 + 字号（01-DESIGN-SPEC §0.3）。

排版（R2 裁决 01-FONT-DECISION）
================================

* 字号一律用**负数字号（像素）**：``-13`` = 13px，字阶只有 :data:`PX_*` 那几档。
* **中文不许进 mono**：要画字符串就调 :meth:`Fonts.for_text`（ASCII → mono，其余 → sans）。
  手写 ``fonts.mono()`` 只允许出现在「确定是 ASCII」的地方（回执号/时间戳/版本/快捷键）。
* 字重只有 normal / bold；用颜色（``muted_text``）做层级，不靠加粗。
* 所有几何计算都在 :mod:`djb_node.desktop.ui_geom`（不依赖 Tk，可无窗口单测）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable, Iterable, Sequence

from . import theme, ui_geom

__all__ = [
    "Bars", "Button", "Chip", "Divider", "Fonts", "Gauge", "Icon", "NavItem", "PX_BODY",
    "PX_BODY_S", "PX_CARD", "PX_CJK_MIN", "PX_DISPLAY", "PX_H1", "PX_HERO", "PX_MICRO",
    "PX_MICRO_CJK", "PX_NAV", "PX_NUM", "PX_NUM_L", "PX_NUM_XL", "PX_SMALL", "PX_TABLE",
    "PX_TITLE", "RoundedCard", "SectionHead", "StatCell", "Switch",
    "apply_kit_styles", "fonts_for",
]

# ── 字阶（px；R2 裁决 01-FONT-DECISION §2.1：只保留 7 档）
#
# 下表是**唯一**的字号来源：渲染层不许再写裸数字字号（``sans(14)`` 那种）。
# 7 档 = 30 / 19 / 18 / 13 / 12 / 10，外加「表格正文 11」这一档**特例**
# （设计 9px 在 CJK 上糊、10px 又比微标还小，见 01-FONT-DECISION §2.1 末段）。
# 字重只两档：标题与关键数字 bold，正文/说明/微标 normal（§2.3）。
PX_DISPLAY = 30     # 仪表环中心大数字                    bold
PX_TITLE = 19       # 页头标题                            bold
PX_HERO = 18        # 主卡标题                            bold
PX_CARD = 13        # 卡片标题 / 关键数值                  bold
PX_BODY = 13        # 正文                                normal
PX_SMALL = 12       # 按钮、辅助说明                       normal
PX_MICRO = 10       # 微标、表头、版本号（含中文也用它，sans）normal / 表头 bold
PX_TABLE = 11       # 表格正文（特例档；行高 26 不变）      normal

#: 含中文时的**最小**字号：中文在 10px 以下会糊成一团（§1 F1 的根因就是 8px 塞中文）
PX_CJK_MIN = PX_MICRO

# ── 兼容别名：调用方（app.py / views.py 投影层）不改签名，值收敛到上面 7 档
PX_H1 = PX_TITLE          # 旧名：页头标题（原 18）
PX_BODY_S = PX_SMALL      # 旧名：按钮/次级正文（原 12）
PX_NAV = PX_SMALL         # 旧名：侧栏导航项（原 12）
PX_NUM = PX_BODY          # 旧名：指标数值（原 13）
PX_NUM_L = PX_TITLE       # 旧名：四宫格数值（原 19）
PX_NUM_XL = PX_DISPLAY    # 旧名：仪表环数字（原 30）
#: 旧名：含中文的微标。**保留为别名**只为不打断调用方签名；
#: 渲染层改用 :meth:`Fonts.for_text`（含中文 → sans 10px），不许再拿它把中文塞进 mono。
PX_MICRO_CJK = PX_MICRO


class Fonts:
    """按窗口解析一次字体族，之后按 px 取 ``tkfont.Font``（缓存，避免每帧新建）。

    两个必须遵守的约定
    ==================

    1. **中文不许进 mono**：取字体用 :meth:`for_text`（纯 ASCII → mono，含非 ASCII → sans），
       而不是手写 ``mono(...)`` 去包一个可能含中文的字符串（01-FONT-DECISION §1 F1）。
    2. **命名字体只有一份、且必须活到进程结束**：:data:`ROLE_FONTS` 里的角色用 Tcl **具名**字体
       建出来，ttk 样式与 ``option_add("*Font")`` 都按名字引用。曾经的 bug 是
       ``apply_kit_styles`` 里临时建了一个 ``Fonts``，函数返回后它被 GC，
       ``tkfont.Font.__del__`` 把 Tcl 字体删掉，样式里只剩一个**悬空名字** ⇒
       Tk 静默退回默认字体（12pt）⇒「表头字号不受控」（§1 F2；复现与证据见 01-FONT-IMPL.md）。
       因此本类只通过 :func:`fonts_for` 取得（模块级缓存 ≈ 窗口生命周期）。

    ``scale`` 是给后续 DPI 适配留的**钩子**：字阶按 px@96dpi 设计，
    ``scale=1.25`` 时 30px → 38px。默认 1.0，本轮不接线（由 lead 合并后注入）。
    """

    #: 角色 → ``(族, px, bold)``；ttk 样式与 option 数据库都引用这些名字
    ROLE_FONTS: dict[str, tuple[str, int, bool]] = {
        "body": ("sans", PX_BODY, False),
        "small": ("sans", PX_SMALL, False),
        "micro": ("sans", PX_MICRO, False),
        "heading": ("sans", PX_MICRO, True),
        "table": ("sans", PX_TABLE, False),
        "mono": ("mono", PX_MICRO, False),
        "nav": ("sans", PX_NAV, False),
        "card": ("sans", PX_CARD, True),
        "title": ("sans", PX_TITLE, True),
        "hero": ("sans", PX_HERO, True),
        "display": ("sans", PX_DISPLAY, True),
    }

    #: 角色的 Tcl 具名字体（``option_add`` / ``style.configure`` 只认名字）
    NAMES: dict[str, str] = {role: "DJB." + role.title() for role in ROLE_FONTS}

    def __init__(self, root: Any, *, scale: float = 1.0) -> None:
        self.root = root
        try:
            self.scale = float(scale)
        except (TypeError, ValueError):             # pragma: no cover - 非法注入
            self.scale = 1.0
        if not self.scale > 0:                      # 0/负数/NaN 一律回到 1.0（不静默缩成 0）
            self.scale = 1.0
        try:
            families: Iterable[str] = tkfont.families(root)
        except Exception:                           # pragma: no cover - 无字库
            families = ()
        self.families = tuple(families or ())
        self.platform = ui_geom.platform_key()
        # 族名探测：平台链 → 系统实际装了什么（可证伪，见 ui_geom.pick_sans_family）
        self.sans_family = ui_geom.pick_sans_family(self.families, self.platform)
        self.mono_family = ui_geom.pick_mono_family(self.families, self.platform)
        self._cache: dict[tuple[str, int, str, str], Any] = {}
        # 角色字体一次建好：改字号只改 ROLE_FONTS 一处
        self.named: dict[str, Any] = {}
        for role, (kind, px, bold) in self.ROLE_FONTS.items():
            self.named[role] = self._build(self.NAMES[role], kind, px, bold)

    # ── 取字体
    def px(self, value: int) -> int:
        """设计 px → 实际 px（乘 ``scale``，至少 1）。"""
        return max(1, int(round(float(value) * self.scale)))

    def _build(self, name: str | None, kind: str, px: int, bold: bool) -> Any:
        family = self.mono_family if kind == "mono" else self.sans_family
        weight = "bold" if bold else "normal"
        size = -self.px(px)
        key = (family, size, weight, name or "")
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        font: Any = None
        if name:
            # 具名字体：已存在就复用（同进程重建窗口/换肤都不会撞名），否则新建
            try:
                font = tkfont.Font(root=self.root, name=name, exists=True)
                font.configure(family=family, size=size, weight=weight)
            except Exception:
                try:
                    font = tkfont.Font(root=self.root, name=name, exists=False,
                                       family=family, size=size, weight=weight)
                except Exception:                   # pragma: no cover - 极端环境
                    font = None
        if font is None:
            try:
                font = tkfont.Font(root=self.root, family=family, size=size, weight=weight)
            except Exception:                       # pragma: no cover - 无字库
                font = (family, size, weight)
        self._cache[key] = font
        return font

    def _get(self, family: str, px: int, weight: str) -> Any:
        size = -abs(self.px(px))
        key = (family, size, weight, "")
        if key not in self._cache:
            try:
                self._cache[key] = tkfont.Font(root=self.root, family=family,
                                               size=size, weight=weight)
            except Exception:                       # pragma: no cover - 极端环境
                self._cache[key] = (family, size, weight)
        return self._cache[key]

    def sans(self, px: int = PX_BODY, *, bold: bool = False) -> Any:
        return self._get(self.sans_family, px, "bold" if bold else "normal")

    def mono(self, px: int = PX_MICRO, *, bold: bool = False) -> Any:
        return self._get(self.mono_family, px, "bold" if bold else "normal")

    def for_text(self, text: Any, px: int = PX_MICRO, *, bold: bool = False) -> Any:
        """按**这段文字**选族：纯 ASCII → mono；含任何非 ASCII → sans（中文最小 10px）。

        为什么需要它：等宽族（Cascadia Mono / Menlo / DejaVu Sans Mono）**没有 CJK 字形**，
        Tk 会静默逐字回退到 CJK 族，但**字号仍是等宽那一档**，于是中文糊成一团、中英基线跳。
        """
        raw = "" if text is None else str(text)
        if raw.isascii():
            return self.mono(px, bold=bold)
        return self.sans(max(int(px), PX_CJK_MIN), bold=bold)

    def named_name(self, role: str) -> Any:
        """该角色可放进 ``option_add`` / ``style.configure`` 的字体值（名字或元组）。"""
        font = self.named.get(role)
        if isinstance(font, tkfont.Font):
            return str(font)
        return font

    def measure(self, text: str, spec: Any) -> int:
        """文本像素宽（给 Canvas 按钮/徽标算宽度用）；失败时按 7px/字粗估。"""
        try:
            return int(spec.measure(text))
        except Exception:                          # pragma: no cover - 非 Font 对象
            return int(len(str(text)) * 7)


_FONTS: dict[str, Fonts] = {}


def fonts_for(widget: Any, *, scale: float | None = None) -> Fonts:
    """取该控件所属窗口的 :class:`Fonts`（按 Tcl 窗口路径缓存，一个窗口只探一次字体）。

    缓存必须活到进程结束：``tkfont.Font`` 被回收时会把 Tcl 字体一起删掉，而 ttk 样式
    与 option 数据库里只存字体**名字**（删掉就成了悬空引用 → 静默退回默认字体）。
    同一进程里重建根窗口（测试会这么干）时按对象身份识别并重建，不会拿到已销毁窗口的字体。
    """
    try:
        root = widget.winfo_toplevel()
    except Exception:                              # pragma: no cover - 未映射控件
        root = widget
    key = str(root)
    cached = _FONTS.get(key)
    if cached is not None and cached.root is not root:
        cached = None                              # 旧根窗口已销毁：重建，不沿用死字体
    if cached is None:
        cached = Fonts(root, scale=1.0 if scale is None else scale)
        _FONTS[key] = cached
    return cached


# ─────────────────────────────────────────────────────────────── 图标（Canvas 线条，15×15）
class Icon:
    """15×15 线条图标：字形之外的唯一图标源（约束 C6：不引图片/字体文件）。

    每个图元都画在传入的 Canvas 上，颜色/位置由调用方给；``size`` 只影响线宽取整。
    """

    SIZE = 15

    @staticmethod
    def draw(canvas: tk.Canvas, name: str, x: float, y: float, color: str,
             size: int = 15, tags: Any = None) -> None:
        """在 ``(x, y)`` 画一个 ``size×size`` 的图标（坐标系原点在左上角）。"""
        width = 1 if size < 18 else 2
        draw = getattr(Icon, "_draw_" + name, None) or Icon._draw_dot
        draw(canvas, x, y, size, color, width, tags)

    #: 图标名 → 语义（侧栏 13 项 + 头部动作）
    NAMES = ("grid", "chip", "queue", "doc", "coin", "slider", "terminal", "gear",
             "refresh", "moon", "sun", "external", "play", "pause", "bolt", "dot",
             "user", "key", "list")

    @staticmethod
    def _line(canvas, *coords, **kwargs) -> None:
        canvas.create_line(*[round(value, 1) for value in coords], **kwargs)

    @staticmethod
    def _draw_grid(canvas, x, y, size, color, width, tags):
        gap, cell = size * 0.14, (size - size * 0.14) / 2
        for row in (0, 1):
            for column in (0, 1):
                x0 = x + column * (cell + gap)
                y0 = y + row * (cell + gap)
                canvas.create_rectangle(round(x0, 1), round(y0, 1),
                                        round(x0 + cell, 1), round(y0 + cell, 1),
                                        outline=color, width=width, tags=tags)

    @staticmethod
    def _draw_chip(canvas, x, y, size, color, width, tags):
        inset = size * 0.26
        canvas.create_rectangle(round(x + inset, 1), round(y + inset, 1),
                                round(x + size - inset, 1), round(y + size - inset, 1),
                                outline=color, width=width, tags=tags)
        for step in (0.34, 0.66):
            Icon._line(canvas, x + size * step, y, x + size * step, y + inset,
                       fill=color, width=width, tags=tags)
            Icon._line(canvas, x + size * step, y + size - inset, x + size * step, y + size,
                       fill=color, width=width, tags=tags)
            Icon._line(canvas, x, y + size * step, x + inset, y + size * step,
                       fill=color, width=width, tags=tags)
            Icon._line(canvas, x + size - inset, y + size * step, x + size, y + size * step,
                       fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_queue(canvas, x, y, size, color, width, tags):
        for index in range(3):
            line_y = y + size * (0.2 + index * 0.3)
            canvas.create_oval(round(x, 1), round(line_y - 1.6, 1), round(x + 3.2, 1),
                               round(line_y + 1.6, 1), outline=color, width=width, tags=tags)
            Icon._line(canvas, x + size * 0.34, line_y, x + size, line_y,
                       fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_doc(canvas, x, y, size, color, width, tags):
        canvas.create_rectangle(round(x + size * 0.16, 1), round(y + size * 0.06, 1),
                                round(x + size * 0.84, 1), round(y + size * 0.94, 1),
                                outline=color, width=width, tags=tags)
        for step in (0.32, 0.52, 0.72):
            Icon._line(canvas, x + size * 0.3, y + size * step, x + size * 0.7,
                       y + size * step, fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_coin(canvas, x, y, size, color, width, tags):
        canvas.create_oval(round(x + size * 0.1, 1), round(y + size * 0.1, 1),
                           round(x + size * 0.9, 1), round(y + size * 0.9, 1),
                           outline=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.5, y + size * 0.3, x + size * 0.5,
                   y + size * 0.7, fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_slider(canvas, x, y, size, color, width, tags):
        for index, knob in enumerate((0.34, 0.68, 0.46)):
            line_y = y + size * (0.18 + index * 0.32)
            Icon._line(canvas, x, line_y, x + size, line_y, fill=color, width=width, tags=tags)
            canvas.create_oval(round(x + size * knob - 1.8, 1), round(line_y - 1.8, 1),
                               round(x + size * knob + 1.8, 1), round(line_y + 1.8, 1),
                               fill=color, outline=color, tags=tags)

    @staticmethod
    def _draw_terminal(canvas, x, y, size, color, width, tags):
        canvas.create_rectangle(round(x + 1, 1), round(y + size * 0.16, 1),
                                round(x + size - 1, 1), round(y + size * 0.84, 1),
                                outline=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.24, y + size * 0.4, x + size * 0.42,
                   y + size * 0.5, fill=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.42, y + size * 0.5, x + size * 0.24,
                   y + size * 0.6, fill=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.52, y + size * 0.62, x + size * 0.76,
                   y + size * 0.62, fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_gear(canvas, x, y, size, color, width, tags):
        center = size / 2
        canvas.create_oval(round(x + center - size * 0.2, 1), round(y + center - size * 0.2, 1),
                           round(x + center + size * 0.2, 1), round(y + center + size * 0.2, 1),
                           outline=color, width=width, tags=tags)
        for angle in (0, 90, 45, 135):
            Icon._spoke(canvas, x, y, size, color, width, angle, tags)

    @staticmethod
    def _spoke(canvas, x, y, size, color, width, angle, tags):
        import math
        center = size / 2
        radius_in, radius_out = size * 0.28, size * 0.48
        radians = math.radians(angle)
        Icon._line(canvas,
                   x + center + math.cos(radians) * radius_in,
                   y + center + math.sin(radians) * radius_in,
                   x + center + math.cos(radians) * radius_out,
                   y + center + math.sin(radians) * radius_out,
                   fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_refresh(canvas, x, y, size, color, width, tags):
        canvas.create_arc(round(x + size * 0.12, 1), round(y + size * 0.12, 1),
                          round(x + size * 0.88, 1), round(y + size * 0.88, 1),
                          start=40, extent=280, style=tk.ARC, outline=color,
                          width=width, tags=tags)
        canvas.create_polygon(round(x + size * 0.74, 1), round(y + size * 0.06, 1),
                              round(x + size * 0.98, 1), round(y + size * 0.26, 1),
                              round(x + size * 0.7, 1), round(y + size * 0.3, 1),
                              fill=color, outline=color, tags=tags)

    @staticmethod
    def _draw_moon(canvas, x, y, size, color, width, tags):
        canvas.create_arc(round(x + size * 0.1, 1), round(y + size * 0.1, 1),
                          round(x + size * 0.9, 1), round(y + size * 0.9, 1),
                          start=60, extent=250, style=tk.ARC, outline=color,
                          width=width, tags=tags)

    @staticmethod
    def _draw_sun(canvas, x, y, size, color, width, tags):
        center = size / 2
        canvas.create_oval(round(x + center - size * 0.18, 1), round(y + center - size * 0.18, 1),
                           round(x + center + size * 0.18, 1), round(y + center + size * 0.18, 1),
                           outline=color, width=width, tags=tags)
        for angle in (0, 45, 90, 135, 180, 225, 270, 315):
            Icon._spoke(canvas, x, y, size * 1.0, color, width, angle, tags)

    @staticmethod
    def _draw_external(canvas, x, y, size, color, width, tags):
        Icon._line(canvas, x + size * 0.16, y + size * 0.84, x + size * 0.84,
                   y + size * 0.16, fill=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.5, y + size * 0.14, x + size * 0.86,
                   y + size * 0.14, fill=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.86, y + size * 0.14, x + size * 0.86,
                   y + size * 0.5, fill=color, width=width, tags=tags)

    @staticmethod
    def _draw_play(canvas, x, y, size, color, width, tags):
        canvas.create_polygon(round(x + size * 0.28, 1), round(y + size * 0.14, 1),
                              round(x + size * 0.84, 1), round(y + size * 0.5, 1),
                              round(x + size * 0.28, 1), round(y + size * 0.86, 1),
                              fill=color, outline=color, tags=tags)

    @staticmethod
    def _draw_pause(canvas, x, y, size, color, width, tags):
        for step in (0.26, 0.58):
            canvas.create_rectangle(round(x + size * step, 1), round(y + size * 0.16, 1),
                                    round(x + size * (step + 0.16), 1),
                                    round(y + size * 0.84, 1), fill=color, outline=color,
                                    tags=tags)

    @staticmethod
    def _draw_bolt(canvas, x, y, size, color, width, tags):
        canvas.create_polygon(round(x + size * 0.56, 1), round(y + size * 0.08, 1),
                              round(x + size * 0.22, 1), round(y + size * 0.56, 1),
                              round(x + size * 0.46, 1), round(y + size * 0.56, 1),
                              round(x + size * 0.4, 1), round(y + size * 0.92, 1),
                              round(x + size * 0.78, 1), round(y + size * 0.44, 1),
                              round(x + size * 0.54, 1), round(y + size * 0.44, 1),
                              fill=color, outline=color, tags=tags)

    @staticmethod
    def _draw_user(canvas, x, y, size, color, width, tags):
        """人像：头 + 肩（账户与安全）。"""
        Icon._line(canvas, x + size * 0.5, y + size * 0.16, x + size * 0.5, y + size * 0.16,
                   fill=color, width=width, capstyle=tk.ROUND, tags=tags)
        canvas.create_oval(round(x + size * 0.32, 1), round(y + size * 0.14, 1),
                           round(x + size * 0.68, 1), round(y + size * 0.5, 1),
                           outline=color, width=width, tags=tags)
        canvas.create_arc(round(x + size * 0.14, 1), round(y + size * 0.52, 1),
                          round(x + size * 0.86, 1), round(y + size * 1.16, 1),
                          start=0, extent=180, style=tk.ARC, outline=color,
                          width=width, tags=tags)

    @staticmethod
    def _draw_key(canvas, x, y, size, color, width, tags):
        """钥匙：环 + 齿（接入配置）。"""
        canvas.create_oval(round(x + size * 0.08, 1), round(y + size * 0.3, 1),
                           round(x + size * 0.5, 1), round(y + size * 0.72, 1),
                           outline=color, width=width, tags=tags)
        Icon._line(canvas, x + size * 0.5, y + size * 0.51, x + size * 0.94, y + size * 0.51,
                   fill=color, width=width, capstyle=tk.ROUND, tags=tags)
        Icon._line(canvas, x + size * 0.72, y + size * 0.51, x + size * 0.72, y + size * 0.72,
                   fill=color, width=width, capstyle=tk.ROUND, tags=tags)

    @staticmethod
    def _draw_list(canvas, x, y, size, color, width, tags):
        """文本行：三行 + 行首点（日志）。"""
        for index in range(3):
            offset = y + size * (0.24 + index * 0.26)
            canvas.create_oval(round(x + size * 0.14, 1), round(offset - 1, 1),
                               round(x + size * 0.2, 1), round(offset + 1, 1),
                               fill=color, outline=color, tags=tags)
            Icon._line(canvas, x + size * 0.36, offset, x + size * 0.88, offset,
                       fill=color, width=width, capstyle=tk.ROUND, tags=tags)

    @staticmethod
    def _draw_dot(canvas, x, y, size, color, width, tags):
        center = size / 2
        canvas.create_oval(round(x + center - 2, 1), round(y + center - 2, 1),
                           round(x + center + 2, 1), round(y + center + 2, 1),
                           fill=color, outline=color, tags=tags)


#: Canvas 上画 1px 描边时的内缩量（B6 的真根因）。
#:
#: Tk 的 ``width=1`` 描边是**以路径为中心**向两侧各铺半个线宽：路径贴着画布边缘（0.5）时，
#: 外侧那半个像素落到画布外被裁掉，剩下的半像素在浅色底上几乎等于背景 ⇒ 底边/右边「看起来
#: 没有边」（浅色 ghost 按钮最明显）。内缩 1.5 让 1px 描边完整落在 1..2 的像素行内，四条边
#: 都看得见；再往里的 focus ring 相应放到 3（不压描边、也不留缝）。
EDGE_INSET = 1.5


# ─────────────────────────────────────────────────────────────── 基元
class Divider(tk.Frame):
    """1px 分隔线（设计里到处是 ``border-top:1px``；不用 Canvas，省一个控件）。"""

    def __init__(self, master: Any, *, color: str | None = None, **kwargs: Any) -> None:
        super().__init__(master, background=color or theme.chart("card_line"),
                         height=1, bd=0, highlightthickness=0, **kwargs)


class RoundedCard(tk.Frame):
    """圆角卡片：``surface`` 底 + 1px 描边 + 11px 圆角（Canvas 画），内容放 :attr:`body`。

    ``ground`` 是**卡片背后那层**的颜色（圆角缺口要露出它），默认页面底色。
    卡片之间只靠描边与间距分层，**没有阴影**（01 §0.3 / 00 §2.3）。
    """

    def __init__(self, master: Any, *, ground: str | None = None, fill: str | None = None,
                 outline: str | None = None, radius: int = 11, border: int = 1,
                 pad: tuple[int, int] = (14, 11), **kwargs: Any) -> None:
        self.ground = ground or theme.color("bg")
        self.fill = fill or theme.color("surface")
        self.outline = outline if outline is not None else theme.chart("card_line")
        self.radius = int(radius)
        self.border = int(border)
        super().__init__(master, background=self.ground, bd=0, highlightthickness=0, **kwargs)
        self._back = tk.Canvas(self, background=self.ground, bd=0, highlightthickness=0,
                               takefocus=0)
        self._back.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self.body = tk.Frame(self, background=self.fill, bd=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        self._shape = None
        self._last_size: tuple[int, int] = (0, 0)
        self.bind("<Configure>", self._on_configure, add="+")

    def _on_configure(self, _event: Any = None) -> None:
        """尺寸没变就不重画（Canvas 的 1px 抖动不许被 ``_redraw`` 放大成重绘风暴）。"""
        size = (self.winfo_width(), self.winfo_height())
        if size == self._last_size:
            return
        self._last_size = size
        self._redraw()

    def _redraw(self, _event: Any = None) -> None:
        try:
            width, height = self.winfo_width(), self.winfo_height()
        except Exception:                          # pragma: no cover - 控件已销毁
            return
        if width <= 2 or height <= 2:
            return
        try:
            self._back.delete("all")
            inset = self.border / 2.0 + (1.0 if self.border else 0.0)   # 见 EDGE_INSET
            self._shape = self._back.create_polygon(
                ui_geom.round_rect_points(inset, inset, width - inset, height - inset,
                                          self.radius),
                smooth=True, splinesteps=12, fill=self.fill,
                outline=self.outline or self.fill,
                width=self.border)
        except Exception:                          # pragma: no cover - 配色非法
            pass


class Chip(tk.Canvas):
    """小徽标（LIVE / TESTNET / 状态 / 计数）：Canvas 圆角 + 居中文字，宽度按文字实测。"""

    #: tone → (底色 token, 文字 token)
    TONES = {
        "mint": ("mint3", "mint_text"), "amber": ("amber_bg", "amber_text"),
        "red": ("red_bg", "red_text"), "violet": ("violet3", "violet_text"),
        "pink": ("pink3", "pink_text"), "muted": ("surface3", "muted_text"),
        "plain": (None, "muted_text"),
    }

    def __init__(self, master: Any, text: str, *, tone: str = "muted",
                 ground: str | None = None, px: int = PX_MICRO_CJK, mono: bool = False,
                 pad: int = 6, height: int = 16, radius: int = 4,
                 outline: str | None = None, **kwargs: Any) -> None:
        self.tone = tone
        self.text = str(text)
        self.ground = ground or theme.color("bg")
        fonts = fonts_for(master)
        # ``mono`` 只为不改调用方签名而保留：现在一律按**内容**选族（ASCII → mono，含中文 → sans）
        self._px = int(px)
        self._font = fonts.for_text(self.text, px)
        self._pad = int(pad)
        self._radius = int(radius)
        width = fonts.measure(self.text, self._font) + self._pad * 2 + 2
        super().__init__(master, width=width, height=int(height), background=self.ground,
                         bd=0, highlightthickness=0, takefocus=0, **kwargs)
        self._outline = outline
        self._redraw()

    def _redraw(self) -> None:
        fill_token, text_token = self.TONES.get(self.tone, self.TONES["muted"])
        fill = theme.color(fill_token) if fill_token else self.ground
        stroke = self._outline if self._outline is not None else (
            theme.color("line2") if fill_token is None else fill)
        self.delete("all")
        height = int(self.cget("height"))
        width = int(self.cget("width"))
        self.create_polygon(ui_geom.round_rect_points(EDGE_INSET, EDGE_INSET,
                                                      width - EDGE_INSET, height - EDGE_INSET,
                                                      self._radius),
                            smooth=True, splinesteps=8, fill=fill, outline=stroke, width=1)
        self.create_text(width / 2.0, height / 2.0, text=self.text,
                         fill=theme.color(text_token), font=self._font, anchor="center")

    def set_text(self, text: str, tone: str | None = None) -> None:
        self.text = str(text)
        if tone:
            self.tone = tone
        fonts = fonts_for(self)
        self._font = fonts.for_text(self.text, self._px)   # 文案变了，族可能也要跟着变
        self.configure(width=fonts.measure(self.text, self._font) + self._pad * 2 + 2)
        self._redraw()


class Button(tk.Canvas):
    """扁圆角按钮（主/次/危险）：Canvas 画底 + 文字，支持 hover / 禁用 / 键盘触发。

    ``kind``：``primary``（mint 实心 + ``on_mint`` 字）、``ghost``（白底 + 控件边界）、
    ``danger``（红底淡染 + 红字）。禁用态按 ``muted_text`` 画并**不隐藏**（C9 保持既有行为：
    置灰 + 旁边写原因）。
    """

    KINDS = {
        "primary": ("mint", "on_mint", "mint_strong"),
        "ghost": ("surface", "ink", None),
        "danger": ("red_bg", "red_text", "red_text"),
        "violet": ("violet", "on_violet", "violet2"),
    }

    def __init__(self, master: Any, text: str, command: Callable[[], None] | None = None, *,
                 kind: str = "ghost", ground: str | None = None, px: int = PX_BODY_S,
                 height: int = 30, pad: int = 11, radius: int = 6,
                 enabled: bool = True, **kwargs: Any) -> None:
        self.kind = kind
        self.text = str(text)
        self.command = command
        self.ground = ground or theme.color("bg")
        fonts = fonts_for(master)
        self._font = fonts.sans(px, bold=(kind in ("primary", "violet")))
        self._pad = int(pad)
        self._radius = int(radius)
        self._enabled = bool(enabled)
        self._hover = False
        width = fonts.measure(self.text, self._font) + self._pad * 2 + 2
        super().__init__(master, width=width, height=int(height), background=self.ground,
                         bd=0, highlightthickness=0, takefocus=1 if enabled else 0, **kwargs)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Return>", self._on_click)
        self.bind("<space>", self._on_click)
        self.bind("<FocusIn>", lambda _e: self._redraw())
        self.bind("<FocusOut>", lambda _e: self._redraw())
        self._redraw()

    # ── 状态
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        try:
            self.configure(takefocus=1 if enabled else 0)
        except Exception:                          # pragma: no cover
            pass
        self._redraw()

    def set_text(self, text: str) -> None:
        self.text = str(text)
        fonts = fonts_for(self)
        self.configure(width=fonts.measure(self.text, self._font) + self._pad * 2 + 2)
        self._redraw()

    # ── 事件
    def _on_click(self, _event: Any = None) -> None:
        if self._enabled and callable(self.command):
            self.command()

    def _on_enter(self, _event: Any = None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event: Any = None) -> None:
        self._hover = False
        self._redraw()

    # ── 绘制
    def _redraw(self) -> None:
        fill_token, text_token, hover_token = self.KINDS.get(self.kind, self.KINDS["ghost"])
        self.delete("all")
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        if not self._enabled:
            fill = theme.color("surface3")
            stroke = theme.color("line")
            text_color = theme.color("muted_text")
        else:
            fill = theme.color(fill_token)
            text_color = theme.color(text_token)
            if self.kind == "ghost":
                stroke = theme.color("border")        # 可点控件边界 ≥3:1（01 §1.3）
            elif hover_token and self._hover:
                fill = theme.color(hover_token)
                stroke = fill
            else:
                stroke = fill
        self.create_polygon(ui_geom.round_rect_points(EDGE_INSET, EDGE_INSET,
                                                      width - EDGE_INSET, height - EDGE_INSET,
                                                      self._radius),
                            smooth=True, splinesteps=8, fill=fill, outline=stroke,
                            width=1)
        if self.focus_get() is self:
            ring = EDGE_INSET + 1.5
            self.create_polygon(ui_geom.round_rect_points(ring, ring, width - ring,
                                                          height - ring,
                                                          max(2, self._radius - ring)),
                                smooth=True, splinesteps=8, fill="", outline=theme.color("focus"),
                                width=1)
        self.create_text(width / 2.0, height / 2.0, text=self.text, fill=text_color,
                         font=self._font, anchor="center")


class Switch(tk.Canvas):
    """28×15 开关（只读展示 + 可点）：ON = ``mint_bright`` 填充，旋钮 ``surface``。"""

    def __init__(self, master: Any, *, on: bool = True, ground: str | None = None,
                 command: Callable[[bool], None] | None = None, **kwargs: Any) -> None:
        self.on = bool(on)
        self.command = command
        self.ground = ground or theme.color("surface")
        super().__init__(master, width=28, height=15, background=self.ground, bd=0,
                         highlightthickness=0, takefocus=1 if command else 0, **kwargs)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<space>", self._toggle)
        self._redraw()

    def _toggle(self, _event: Any = None) -> None:
        if not callable(self.command):
            return
        self.on = not self.on
        self._redraw()
        self.command(self.on)

    def set_on(self, on: bool) -> None:
        self.on = bool(on)
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        track = theme.chart("mint_bright") if self.on else theme.color("line2")
        self.create_polygon(ui_geom.round_rect_points(0.5, 0.5, 27.5, 14.5, 7),
                            smooth=True, splinesteps=8, fill=track, outline=track, width=1)
        center = 14.0 if self.on else 8.0
        knob = theme.color("surface") if self.on else theme.color("surface")
        self.create_oval(center - 5.5, 2.0, center + 5.5, 13.0, fill=knob,
                         outline=theme.color("line"))


# ─────────────────────────────────────────────────────────────── 数据可视化
class Gauge(tk.Canvas):
    """仪表环 125×125：轨道 + 270° 弧 + 中心大数字 + 环下标签（01 §3.4）。

    数值缺失时 ``ratio=None`` → 只画轨道并把中心文字换成 ``value_text``（调用方传「暂不可用」），
    **不画假进度**。
    """

    def __init__(self, master: Any, *, size: int = 125, ring: int = 8,
                 ground: str | None = None, **kwargs: Any) -> None:
        self.size = int(size)
        self.ring = int(ring)
        self.ground = ground or theme.color("surface")
        super().__init__(master, width=self.size, height=self.size, background=self.ground,
                         bd=0, highlightthickness=0, takefocus=0, **kwargs)
        self._ratio: float | None = None
        self._value = ""
        self._label = ""

    def set(self, ratio: float | None, value_text: str, label: str = "") -> None:
        self._ratio = None if ratio is None else ui_geom.clamp01(ratio)
        self._value = str(value_text)
        self._label = str(label)
        self._redraw()

    def _redraw(self) -> None:
        fonts = fonts_for(self)
        self.delete("all")
        inset = self.ring / 2.0 + 1
        box = (inset, inset, self.size - inset, self.size - inset)
        start, extent = ui_geom.gauge_arc(1.0)
        self.create_arc(*box, start=start, extent=extent, style=tk.ARC,
                        outline=theme.chart("ring_track"), width=self.ring)
        if self._ratio:
            start, extent = ui_geom.gauge_arc(self._ratio)
            self.create_arc(*box, start=start, extent=extent, style=tk.ARC,
                            outline=theme.chart("mint_bright"), width=self.ring)
        center = self.size / 2.0
        big = fonts.sans(PX_NUM_XL, bold=True)
        self.create_text(center, center - 4, text=self._value,
                         fill=theme.color("ink"), font=big, anchor="center")
        if self._label:
            self.create_text(center, center + 20, text=self._label,
                             fill=theme.color("muted_text"),
                             font=fonts.for_text(self._label, PX_MICRO), anchor="center")


class Bars(tk.Canvas):
    """柱图：24 根等宽柱 + 底部基线 + 轴标签（01 §3.4 / §4.6）。

    颜色按 ``index % 3`` 轮转（薄荷/紫/粉），柱高驱动颜色深浅（``blend(淡色, 深色, h)``）
    ——设计样是 ``linear-gradient(180deg, 主色, 淡色)``，Tk 不能渐变，
    用「高度→深浅」得到同一观感且**不新增色相**。
    ``values`` 为空 = 没有时序数据：只画等距空槽（轨道色），不画假柱。
    """

    def __init__(self, master: Any, *, bars: int = 24, height: int = 84, gap: int = 4,
                 ground: str | None = None, **kwargs: Any) -> None:
        self.bars = int(bars)
        self.chart_height = int(height)
        self.gap = int(gap)
        self.ground = ground or theme.color("surface")
        self._values: tuple[float, ...] = ()
        self._axis: tuple[str, ...] = ()
        super().__init__(master, height=self.chart_height + 14, background=self.ground,
                         bd=0, highlightthickness=0, takefocus=0, **kwargs)
        self._last_size: tuple[int, int] = (0, 0)
        self.bind("<Configure>", self._on_configure, add="+")

    def _on_configure(self, _event: Any = None) -> None:
        """尺寸没变就不重画（抖动事故防线：1px 抖动 → 重绘 → reqwidth 变 → 再抖动）。"""
        size = (self.winfo_width(), self.winfo_height())
        if size == self._last_size:
            return
        self._last_size = size
        self._redraw()

    def set(self, values: Sequence[float] = (), *, axis: Sequence[str] = ()) -> None:
        self._values = tuple(ui_geom.clamp01(value) for value in values)
        self._axis = tuple(axis)
        self._redraw()

    def _redraw(self) -> None:
        fonts = fonts_for(self)
        self.delete("all")
        try:
            width = int(self.winfo_width())
        except Exception:                          # pragma: no cover
            width = 0
        if width <= 4:
            return
        baseline = self.chart_height
        self.create_line(0, baseline + 0.5, width, baseline + 0.5,
                         fill=theme.chart("card_line"))
        slots = ui_geom.bar_slots(self.bars, width, self.gap)
        pairs = (("bar_mint", "bar_mint_peak"), ("bar_violet", "bar_violet_peak"),
                 ("bar_pink", "bar_pink_peak"))
        heights = ui_geom.bar_heights(self._values, self.chart_height - 2, floor=3.0) \
            if self._values else [3.0] * self.bars
        for index, (x0, x1) in enumerate(slots):
            if x1 - x0 <= 0.1:
                continue
            top = baseline - heights[index]
            if self._values:
                pale, deep = pairs[index % 3]
                ratio = 0.22 + 0.68 * self._values[index]
                fill = theme.blend(theme.chart(pale), theme.chart(deep), ratio)
            else:
                fill = theme.chart("ring_track")
            self.create_polygon(ui_geom.bar_points(x0, top, x1, baseline, 2),
                                smooth=True, splinesteps=6, fill=fill, outline=fill,
                                width=1)
        if self._axis:
            self._draw_axis(fonts, width, baseline)

    def _draw_axis(self, fonts: Any, width: int, baseline: int) -> None:
        """轴标签：首/中/尾按槽位对齐（不硬编码像素）。"""
        labels = self._axis
        if not labels:
            return
        positions = ([0.0] if len(labels) == 1 else
                     [0.0 + i * (width - 1) / (len(labels) - 1) for i in range(len(labels))])
        for index, label in enumerate(labels):
            if index >= len(positions):
                break
            anchor = "w" if index == 0 else ("e" if index == len(labels) - 1 else "center")
            self.create_text(round(positions[index], 1), baseline + 7, text=label,
                             anchor=anchor, fill=theme.color("muted_text"),
                             font=fonts.for_text(label, PX_MICRO))


class StatCell(tk.Frame):
    """指标格：微标 + 数值（+单位）+ 说明；用于状态卡四指标与 TODAY 四宫格。

    基线（01-FONT-DECISION §1 F4）：**值与单位同字号、同 baseline**，
    「62 °C」「23 张」不再一个小两号、也不会被 ``pady`` 顶得上下错位——
    单位靠 ``muted_text`` 降权，不靠缩字号（缩字号就是 F4 那个跳）。
    """

    def __init__(self, master: Any, *, label: str, value: str, unit: str = "",
                 note: str = "", ground: str | None = None, value_px: int = PX_NUM_L,
                 value_tone: str | None = None, mono_value: bool = False,
                 align: str = "w", **kwargs: Any) -> None:
        self.ground = ground or theme.color("surface")
        super().__init__(master, background=self.ground, bd=0, highlightthickness=0, **kwargs)
        fonts = fonts_for(master)
        tk.Label(self, text=label, font=fonts.sans(PX_MICRO),
                 foreground=theme.color("muted_text"), background=self.ground,
                 anchor=align).pack(anchor=align, fill="x")
        line = tk.Frame(self, background=self.ground)
        line.pack(anchor=align, fill="x", pady=(2, 0))
        # 数值：数字/符号走 mono 更齐（ASCII），含单位中文时靠 for_text 换族
        value_font = fonts.for_text(value, value_px, bold=True) if mono_value else \
            fonts.sans(value_px, bold=True)
        value_label = tk.Label(line, text=str(value), font=value_font,
                               foreground=theme.color(value_tone or "ink"), background=self.ground,
                               anchor=align)
        # 单位与值同一个 baseline：右手边（align="e"）时先摆值、再摆单位（单位在值的左侧）
        if align == "w":
            value_label.pack(side="left")
        else:
            value_label.pack(side="right")
        if unit:
            # 同字号 + muted 色（不缩字号、不加 pady：两者都是 F4 基线跳的来源）
            tk.Label(line, text=" " + str(unit), font=fonts.sans(value_px),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor=align).pack(side="left" if align == "w" else "right")
        if note:
            tk.Label(self, text=note, font=fonts.sans(PX_MICRO),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor=align, justify="left").pack(anchor=align, fill="x", pady=(1, 0))


class SectionHead(tk.Frame):
    """卡片头：眉标（微标）+ 标题 + 副标，右侧可挂一个控件。"""

    def __init__(self, master: Any, *, kicker: str = "", title: str = "", subtitle: str = "",
                 ground: str | None = None, right: Callable[[Any], Any] | None = None,
                 title_px: int = PX_CARD, **kwargs: Any) -> None:
        self.ground = ground or theme.color("surface")
        super().__init__(master, background=self.ground, bd=0, highlightthickness=0, **kwargs)
        fonts = fonts_for(master)
        left = tk.Frame(self, background=self.ground)
        left.pack(side="left", fill="x", expand=True)
        if kicker:
            tk.Label(left, text=kicker, font=fonts.for_text(kicker, PX_MICRO),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor="w").pack(anchor="w")
        if title:
            tk.Label(left, text=title, font=fonts.sans(title_px, bold=True),
                     foreground=theme.color("ink"), background=self.ground,
                     anchor="w", justify="left").pack(anchor="w", pady=(3, 0))
        if subtitle:
            tk.Label(left, text=subtitle, font=fonts.sans(PX_MICRO),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor="w").pack(anchor="w", pady=(3, 0))
        if right is not None:
            holder = tk.Frame(self, background=self.ground)
            holder.pack(side="right", anchor="ne", padx=(8, 0))
            right(holder)


class NavItem(tk.Canvas):
    """侧栏导航项：图标 + 标题 + 计数徽标；active = 浅薄荷底 + 2px 薄荷左条。

    整项画在一块 Canvas 上（圆角 7、行高 34），宽度跟着侧栏走（``<Configure>`` 重画）。
    """

    HEIGHT = 34

    def __init__(self, master: Any, *, text: str, icon: str = "dot", count: str = "",
                 active: bool = False, ground: str | None = None,
                 command: Callable[[], None] | None = None, compact: bool = False,
                 **kwargs: Any) -> None:
        self.text = str(text)
        self.icon = icon
        self.count = str(count)
        self.active = bool(active)
        self.compact = bool(compact)
        self.command = command
        self.ground = ground or theme.color("surface3")
        super().__init__(master, width=190, height=self.HEIGHT, background=self.ground,
                         bd=0, highlightthickness=0, takefocus=1, **kwargs)
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<FocusIn>", lambda _e: self._redraw())
        self.bind("<FocusOut>", lambda _e: self._redraw())
        self._hot = False
        self._last_size: tuple[int, int] = (0, 0)

    def _on_configure(self, _event: Any = None) -> None:
        """尺寸没变就不重画（同 RoundedCard：1px 抖动不许放大成重绘风暴）。"""
        size = (self.winfo_width(), self.winfo_height())
        if size == self._last_size:
            return
        self._last_size = size
        self._redraw()

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self._redraw()

    def _click(self, _event: Any = None) -> None:
        if callable(self.command):
            self.command()

    def _hover(self, hot: bool) -> None:
        if self._hot != hot:
            self._hot = hot
            self._redraw()

    def _redraw(self) -> None:
        fonts = fonts_for(self)
        self.delete("all")
        try:
            width = int(self.winfo_width())
        except Exception:                          # pragma: no cover
            width = 190
        if width <= 4:
            return
        height = self.HEIGHT
        if self.active:
            fill = theme.color("mint3")
            outline = theme.color("line2")
        elif self._hot:
            fill = theme.color("surface2")
            outline = ""
        else:
            fill = self.ground
            outline = ""
        if fill != self.ground or outline:
            self.create_polygon(ui_geom.round_rect_points(EDGE_INSET, EDGE_INSET,
                                                          width - EDGE_INSET,
                                                          height - EDGE_INSET, 7),
                                smooth=True, splinesteps=8, fill=fill,
                                outline=outline or fill, width=1)
        if self.active:
            self.create_polygon(ui_geom.round_rect_points(0, 8, 2, height - 8, 1),
                                smooth=True, splinesteps=6,
                                fill=theme.chart("mint_bright"),
                                outline=theme.chart("mint_bright"))
        icon_color = theme.color("mint_text") if self.active else theme.color("muted_text")
        Icon.draw(self, self.icon, 10, (height - Icon.SIZE) / 2.0, icon_color)
        text_color = theme.color("ink") if self.active else theme.color("ink2")
        if not self.compact:
            self.create_text(34, height / 2.0, text=self.text, anchor="w",
                             fill=text_color, font=fonts.sans(PX_NAV))
        if self.count and not self.compact:
            count_font = fonts.mono(PX_MICRO)
            label = str(self.count)
            badge_w = fonts.measure(label, count_font) + 10
            x0 = width - badge_w - 6
            self.create_polygon(ui_geom.round_rect_points(x0, height / 2.0 - 7, x0 + badge_w,
                                                          height / 2.0 + 7, 4),
                                smooth=True, splinesteps=6, fill=theme.color("surface"),
                                outline=theme.color("line2"), width=1)
            self.create_text(x0 + badge_w / 2.0, height / 2.0, text=label, anchor="center",
                             fill=theme.color("muted_text"), font=count_font)
        if self.focus_get() is self:
            ring = EDGE_INSET + 1.5
            self.create_polygon(ui_geom.round_rect_points(ring, ring, width - ring,
                                                          height - ring, 6),
                                smooth=True, splinesteps=8, fill="",
                                outline=theme.color("focus"), width=1)


# ─────────────────────────────────────────────────────────────── ttk 皮肤
#: 原生 ttk 样式名 → :class:`Fonts` 角色。**两套都设**：``Kit.*`` 给本模块用，
#: 裸名（``TEntry``/``Treeview``/…）给没写 ``style=`` 的调用方（窄窗导航下拉就是裸 ``TCombobox``）。
#: 这样「一边 11pt 点值、一边 13px 像素」的第二套字体系统（01-FONT-DECISION §1 F6）就没了。
STYLE_FONT_ROLES: dict[str, str] = {
    # 输入类控件与按钮同档（12）：控件里的字比正文小一档，视觉上才像「可操作的格子」
    "TEntry": "small", "TCombobox": "small", "TMenubutton": "small",
    "TLabel": "body", "TButton": "small", "Toolbutton": "small",
    "TCheckbutton": "small", "TRadiobutton": "small",
    "Treeview": "table", "Heading": "heading", "Treeview.Heading": "heading",
    "TScrollbar": "small",
    "Kit.TEntry": "small", "Kit.TCombobox": "small",
    "Kit.Treeview": "table", "Kit.Treeview.Heading": "heading", "Kit.Heading": "heading",
    "Kit.Vertical.TScrollbar": "small",
}


def apply_kit_styles(root: Any, ttk: Any) -> Any:
    """给 ttk 控件套一层「无边框 + 白底」皮肤，并**统一字体**（01-FONT-DECISION §2.4）。

    两条硬约定：

    1. 字体一律来自 ``fonts_for(root)`` 的**命名字体**（``DJB.Body`` 等），不新建临时
       :class:`Fonts`。临时对象一被 GC，``tkfont.Font.__del__`` 就会删掉对应的 Tcl 字体，
       样式里只剩下悬空名字 ⇒ Tk 静默退回默认字体（12pt）⇒ 表头字号不受控（§1 F2）。
    2. 只改**字体与 Kit 那层皮肤**：颜色/间距的取值仍来自 ``theme``，``gui.theme`` 的
       ``FONT`` / ``TYPE_SCALE`` 与 ``tests/test_desktop_theme.py`` 的 pin 值一个字不动。
    """
    fonts = fonts_for(root)                   # 必须与渲染层共用同一个 Fonts（见上面第 1 条）
    style = ttk.Style(root)

    # 全局默认字体：tk 自带控件（Label/Frame…）与 ttk 的子控件都吃这一条。
    # ``gui.theme.apply_theme`` 先写过 (family, 11) 的点值；这里用命名字体覆盖（同名 pattern 后写生效）。
    for pattern in ("*Font", "*TCombobox*Listbox.font"):
        try:
            root.option_add(pattern, fonts.named_name("body"))
        except Exception:                          # pragma: no cover - 极端环境
            pass

    # 一套 Font 铺满 ttk（表体 11 / 表头 10 bold / 正文 13 / 按钮 12）
    for name, role in STYLE_FONT_ROLES.items():
        try:
            style.configure(name, font=fonts.named_name(role))
        except Exception:                          # pragma: no cover - 主题不支持
            pass

    style.configure("Kit.Treeview", background=theme.color("surface"),
                    fieldbackground=theme.color("surface"),
                    foreground=theme.color("ink2"), font=fonts.named_name("table"),
                    rowheight=26, borderwidth=0, relief="flat")
    style.map("Kit.Treeview",
              background=[("selected", theme.color("violet3"))],
              foreground=[("selected", theme.color("violet_text"))])
    style.configure("Kit.Treeview.Heading", background=theme.color("surface3"),
                    foreground=theme.color("muted_text"), font=fonts.named_name("heading"),
                    relief="flat", borderwidth=0, padding=(6, 5))
    style.map("Kit.Treeview.Heading", background=[("active", theme.color("surface3"))])
    try:
        style.layout("Kit.Treeview", [("Kit.Treeview.treearea", {"sticky": "nswe"})])
    except Exception:                              # pragma: no cover - 主题不支持
        pass
    style.configure("Kit.TEntry", fieldbackground=theme.color("surface"),
                    foreground=theme.color("ink"), bordercolor=theme.color("border"),
                    lightcolor=theme.color("line"), darkcolor=theme.color("line"),
                    insertcolor=theme.color("ink"), padding=(6, 5), relief="flat")
    style.map("Kit.TEntry", bordercolor=[("focus", theme.color("focus"))])
    style.configure("Kit.Vertical.TScrollbar", background=theme.color("surface3"),
                    troughcolor=theme.color("bg"), bordercolor=theme.color("bg"),
                    arrowcolor=theme.color("muted_text"), relief="flat", borderwidth=0)
    return style
