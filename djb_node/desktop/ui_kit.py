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

字号一律用**负数字号（像素）**：``-13`` = 13px。见 :data:`PX_*`。
所有几何计算都在 :mod:`djb_node.desktop.ui_geom`（不依赖 Tk，可无窗口单测）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable, Iterable, Sequence

from . import theme, ui_geom

__all__ = [
    "Bars", "Button", "Chip", "Divider", "Fonts", "Gauge", "Icon", "NavItem", "PX_BODY",
    "PX_CARD", "PX_H1", "PX_HERO", "PX_MICRO", "PX_MICRO_CJK", "PX_NAV", "PX_NUM",
    "PX_NUM_L", "PX_NUM_XL", "PX_TABLE", "RoundedCard", "SectionHead", "StatCell", "Switch",
    "apply_kit_styles", "fonts_for",
]

# ── 字阶（px；01-DESIGN-SPEC §2.2，CJK 下限按该节注 +1~2px，避免 7pt 糊成一团）
PX_H1 = 18          # 页头标题（设计 18px/650）
PX_HERO = 20        # 状态主卡标题（设计 20px/650）
PX_CARD = 14        # 卡片标题（设计 14px/650）
PX_BODY = 13        # 正文（设计 11.5px；CJK 在 12px 以下开始糊，+1.5px）
PX_BODY_S = 12      # 按钮/次级正文（设计 11px）
PX_NAV = 12         # 侧栏导航项（设计 12px）
PX_NUM_XL = 30      # 仪表环中心数字（设计 30px/650）
PX_NUM_L = 19       # TODAY 四宫格数值（设计 19px/650）
PX_NUM = 13         # 指标数值（设计 11.5px mono，+1.5px 便于对齐等宽）
PX_TABLE = 10       # 表格正文（设计 9px mono；CJK 下限 +1px）
PX_MICRO = 8        # 拉丁微标（NODE STATUS / SYNC / 版本号；设计 7–8px mono）
PX_MICRO_CJK = 10   # 含中文的微标（设计 7.5px mono 只放得下拉丁；中文 +2.5px）


class Fonts:
    """按窗口解析一次字体族，之后按 px 取 ``tkfont.Font``（缓存，避免每帧新建）。"""

    def __init__(self, root: Any) -> None:
        self.root = root
        try:
            families: Iterable[str] = tkfont.families(root)
        except Exception:                          # pragma: no cover - 无字库
            families = ()
        self.sans_family = ui_geom.pick_family(families, ui_geom.SANS_CHAIN)
        self.mono_family = ui_geom.pick_family(families, ui_geom.MONO_CHAIN)
        self._cache: dict[tuple[str, int, str], Any] = {}

    def _get(self, family: str, px: int, weight: str) -> Any:
        key = (family, int(px), weight)
        if key not in self._cache:
            try:
                self._cache[key] = tkfont.Font(root=self.root, family=family,
                                               size=-abs(int(px)), weight=weight)
            except Exception:                      # pragma: no cover - 极端环境
                self._cache[key] = (family, -abs(int(px)), weight)
        return self._cache[key]

    def sans(self, px: int = PX_BODY, *, bold: bool = False) -> Any:
        return self._get(self.sans_family, px, "bold" if bold else "normal")

    def mono(self, px: int = PX_MICRO, *, bold: bool = False) -> Any:
        return self._get(self.mono_family, px, "bold" if bold else "normal")

    def measure(self, text: str, spec: Any) -> int:
        """文本像素宽（给 Canvas 按钮/徽标算宽度用）；失败时按 7px/字粗估。"""
        try:
            return int(spec.measure(text))
        except Exception:                          # pragma: no cover - 非 Font 对象
            return int(len(str(text)) * 7)


_FONTS: dict[str, Fonts] = {}


def fonts_for(widget: Any) -> Fonts:
    """取该控件所属窗口的 :class:`Fonts`（按 Tcl 窗口路径缓存，一个窗口只探一次字体）。"""
    try:
        root = widget.winfo_toplevel()
    except Exception:                              # pragma: no cover - 未映射控件
        root = widget
    key = str(root)
    cached = _FONTS.get(key)
    if cached is None:
        cached = Fonts(root)
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
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event: Any = None) -> None:
        try:
            width, height = self.winfo_width(), self.winfo_height()
        except Exception:                          # pragma: no cover - 控件已销毁
            return
        if width <= 2 or height <= 2:
            return
        try:
            self._back.delete("all")
            inset = self.border / 2.0
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
        self._font = fonts.mono(px) if mono else fonts.sans(px)
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
        self.create_polygon(ui_geom.round_rect_points(0.5, 0.5, width - 0.5, height - 0.5,
                                                      self._radius),
                            smooth=True, splinesteps=8, fill=fill, outline=stroke, width=1)
        self.create_text(width / 2.0, height / 2.0, text=self.text,
                         fill=theme.color(text_token), font=self._font, anchor="center")

    def set_text(self, text: str, tone: str | None = None) -> None:
        self.text = str(text)
        if tone:
            self.tone = tone
        fonts = fonts_for(self)
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
        self.create_polygon(ui_geom.round_rect_points(0.5, 0.5, width - 0.5, height - 0.5,
                                                      self._radius),
                            smooth=True, splinesteps=8, fill=fill, outline=stroke,
                            width=1)
        if self.focus_get() is self:
            self.create_polygon(ui_geom.round_rect_points(2, 2, width - 2, height - 2,
                                                          max(2, self._radius - 2)),
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
                             font=fonts.mono(PX_MICRO), anchor="center")


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
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")

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
                             font=fonts.mono(PX_MICRO))


class StatCell(tk.Frame):
    """指标格：微标 + 数值（+单位）+ 说明；用于状态卡四指标与 TODAY 四宫格。"""

    def __init__(self, master: Any, *, label: str, value: str, unit: str = "",
                 note: str = "", ground: str | None = None, value_px: int = PX_NUM_L,
                 value_tone: str | None = None, mono_value: bool = False,
                 align: str = "w", **kwargs: Any) -> None:
        self.ground = ground or theme.color("surface")
        super().__init__(master, background=self.ground, bd=0, highlightthickness=0, **kwargs)
        fonts = fonts_for(master)
        tk.Label(self, text=label, font=fonts.sans(PX_MICRO_CJK),
                 foreground=theme.color("muted_text"), background=self.ground,
                 anchor=align).pack(anchor=align, fill="x")
        line = tk.Frame(self, background=self.ground)
        line.pack(anchor=align, fill="x", pady=(3, 0))
        value_font = fonts.mono(value_px, bold=not mono_value) if mono_value else \
            fonts.sans(value_px, bold=True)
        tk.Label(line, text=str(value), font=value_font,
                 foreground=theme.color(value_tone or "ink"), background=self.ground,
                 anchor=align).pack(side="left" if align == "w" else "right")
        if unit:
            tk.Label(line, text=" " + str(unit), font=fonts.sans(PX_MICRO_CJK),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor=align).pack(side="left", pady=(3, 0))
        if note:
            tk.Label(self, text=note, font=fonts.sans(PX_MICRO_CJK),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor=align, justify="left").pack(anchor=align, fill="x", pady=(2, 0))


class SectionHead(tk.Frame):
    """卡片头：眉标（mono 微标）+ 标题 + 副标，右侧可挂一个控件。"""

    def __init__(self, master: Any, *, kicker: str = "", title: str = "", subtitle: str = "",
                 ground: str | None = None, right: Callable[[Any], Any] | None = None,
                 title_px: int = PX_CARD, **kwargs: Any) -> None:
        self.ground = ground or theme.color("surface")
        super().__init__(master, background=self.ground, bd=0, highlightthickness=0, **kwargs)
        fonts = fonts_for(master)
        left = tk.Frame(self, background=self.ground)
        left.pack(side="left", fill="x", expand=True)
        if kicker:
            tk.Label(left, text=kicker, font=fonts.mono(PX_MICRO),
                     foreground=theme.color("muted_text"), background=self.ground,
                     anchor="w").pack(anchor="w")
        if title:
            tk.Label(left, text=title, font=fonts.sans(title_px, bold=True),
                     foreground=theme.color("ink"), background=self.ground,
                     anchor="w", justify="left").pack(anchor="w", pady=(3, 0))
        if subtitle:
            tk.Label(left, text=subtitle, font=fonts.sans(PX_MICRO_CJK),
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
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<FocusIn>", lambda _e: self._redraw())
        self.bind("<FocusOut>", lambda _e: self._redraw())
        self._hot = False

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
            self.create_polygon(ui_geom.round_rect_points(0.5, 0.5, width - 0.5, height - 0.5, 7),
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
            self.create_polygon(ui_geom.round_rect_points(1.5, 1.5, width - 1.5, height - 1.5, 6),
                                smooth=True, splinesteps=8, fill="",
                                outline=theme.color("focus"), width=1)


# ─────────────────────────────────────────────────────────────── ttk 皮肤
def apply_kit_styles(root: Any, ttk: Any) -> Any:
    """给 ttk 控件套一层「无边框 + 白底」皮肤（只新增 ``Kit.*`` 样式名，不动既有样式）。

    为什么不改 ``Treeview`` / ``TEntry`` 本身：``gui.theme.apply_theme`` 是托盘与
    ``gui`` 共用的一套样式，``tests/test_desktop_theme.py`` 逐项钉死了它的取值；
    这里只新增命名样式，老调用方与老断言都不受影响。
    """
    fonts = Fonts(root)
    style = ttk.Style(root)
    style.configure("Kit.Treeview", background=theme.color("surface"),
                    fieldbackground=theme.color("surface"),
                    foreground=theme.color("ink2"), font=fonts.sans(PX_TABLE),
                    rowheight=26, borderwidth=0, relief="flat")
    style.map("Kit.Treeview",
              background=[("selected", theme.color("violet3"))],
              foreground=[("selected", theme.color("violet_text"))])
    style.configure("Kit.Treeview.Heading", background=theme.color("surface3"),
                    foreground=theme.color("muted_text"), font=fonts.mono(PX_MICRO),
                    relief="flat", borderwidth=0, padding=(6, 5))
    style.map("Kit.Treeview.Heading", background=[("active", theme.color("surface3"))])
    try:
        style.layout("Kit.Treeview", [("Kit.Treeview.treearea", {"sticky": "nswe"})])
    except Exception:                              # pragma: no cover - 主题不支持
        pass
    style.configure("Kit.TEntry", fieldbackground=theme.color("surface"),
                    foreground=theme.color("ink"), bordercolor=theme.color("border"),
                    lightcolor=theme.color("line"), darkcolor=theme.color("line"),
                    insertcolor=theme.color("ink"), padding=(6, 4), relief="flat")
    style.map("Kit.TEntry", bordercolor=[("focus", theme.color("focus"))])
    style.configure("Kit.Vertical.TScrollbar", background=theme.color("surface3"),
                    troughcolor=theme.color("bg"), bordercolor=theme.color("bg"),
                    arrowcolor=theme.color("muted_text"), relief="flat", borderwidth=0)
    return style
