# SPDX-License-Identifier: Apache-2.0
"""桌面主窗口（Tk）：侧栏 12 视图 + 内容区 + 状态栏；逻辑全在 controller。

约束
====

* **纯 stdlib**：只用 ``tkinter`` / ``ttk``；``tkinter`` 延迟到实例化才 import，
  因此没有图形环境的机器上 import 本模块、跑逻辑测试都不受影响。
* **无头语义不变**：窗口打不开 → :class:`~djb_node.gui.window.HeadlessError` →
  :func:`main` 返回 ``EXIT_HEADLESS``(3)，不打印 traceback。
* **颜色只来自主题**：所有样式名在 :func:`djb_node.gui.theme.apply_theme` 里定义，
  本文件不写十六进制。
* **不执行危险动作**：暂停/恢复要二次确认并回读；保存走 controller 的校验+回读；
  云端没接线的按钮置灰并写原因，点了也只回一句「暂不可用」。
* **可截图**：:meth:`DesktopApp.show_view` 只切视图并 ``update_idletasks``，
  外部脚本可用 ``import -window`` 逐视图取证（见 ``scripts/shot-desktop.py``）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Mapping, Sequence

from .access import ACCESS_FILE, RELOAD_DAEMON_DOWN, RELOAD_FAILED, RELOAD_OK, RELOAD_UNSUPPORTED
from .access import AccessService, FileAccessStore
from .controller import DEFAULT_MESSAGE, DesktopController, LocalControlChannel
from .datasource import FakeDataSource, LocalDataSource, ProviderError, Snapshot
from . import theme as dual          # 双主题：色板 + 偏好 + 动效助手
from . import platform_ui
from .views import (NAV_SECTIONS, VIEW_KEYS, VIEW_TITLES, Action, Card, Chart, Field, Hero,
                    Metric, RailCard, Row, Table, View, page_kicker)

__all__ = ["DesktopApp", "EXIT_HEADLESS", "EXIT_OK", "HEADLESS_HINT", "HeadlessError",
           "demo_snapshot", "main", "make_controller"]

#: 正常退出
EXIT_OK = 0
#: 无图形环境（与托盘 / gui.window 同一语义）
EXIT_HEADLESS = 3
HEADLESS_HINT = "桌面客户端需要图形环境（tkinter + 桌面会话），当前无法启动，已跳过"

#: 侧栏图标（13 项一一对应；名字都在 :attr:`ui_kit.Icon.NAMES` 里，缺一个就退化成圆点）
NAV_ICONS = {
    "overview": "grid", "bench": "bolt", "models": "slider", "gpu": "chip",
    "tasks": "queue", "receipts": "doc", "earnings": "coin", "agent": "terminal",
    "token_plan": "external", "access": "key", "yield": "moon", "account": "user",
    "logs": "list",
}

#: 页头眉标：**组名 / 页名** 两级（B3），如 ``RUN / OVERVIEW``。
#:
#: 数据口径在 :func:`djb_node.desktop.views.page_kicker`（组名来自 ``NAV_SECTIONS``），
#: 这里只做「13 个视图各一条」的派生，不再手写第二份表；整串 ASCII，走 mono 不会跳基线。
PAGE_KICKERS = {key: page_kicker(key) for key in VIEW_KEYS}

TOP_NAV_KEYS = VIEW_KEYS

#: 视图切换快捷键（B5）：Ctrl+1…9 直达前 9 项，Ctrl+Shift+1…4 直达后 4 项。
#:
#: Tk 里 Shift+数字 的 keysym 是**移位后的字符**（! @ # $），绑 ``<Control-Shift-Key-1>``
#: 永远不触发（Xvfb 实测），必须绑移位后的 keysym；``Ctrl+PageUp/PageDown`` 前后循环
#: 不依赖键盘布局，作为兜底。macOS 的 Command 版由 xplat 的 platform_ui 提供 modifier 后
#: 复用同一张表（``view_shortcut_bindings(modifier="Command")``），这里只做 Control。
SHIFTED_DIGITS = {1: "exclam", 2: "at", 3: "numbersign", 4: "dollar"}

#: 状态栏提示（与绑定表同源；controller 的默认文案由 ``sync_shortcut_hint`` 替换显示）
VIEW_SHORTCUT_HINT = "Ctrl+1…9 / Ctrl+Shift+1…4 切换视图"


def content_item_width(canvas_width: Any) -> int:
    """内容列宽决策（纯函数，无 Tk）：**任何时刻都不超过画布宽**（B4）。

    单独抽出来是为了无窗口断言：Windows 启动即目标尺寸时，画布先配置、内容后撑宽；
    旧实现只在画布 ``<Configure>`` 时写 item width，内容就会保持自然请求宽（≈1494）。
    """
    try:
        width = int(canvas_width)
    except (TypeError, ValueError):
        return 1
    return max(1, width)


def nav_needs_scroll(content_height: Any, viewport_height: Any) -> bool:
    """导航区是否真的溢出（B2：滚动条只在溢出时出现，与内容区同一策略）。

    显示阈值留 2px 磁滞，避免 1px 请求高摆动把滚动条自己卷进 Configure 回环。
    """
    try:
        need, have = int(content_height), int(viewport_height)
    except (TypeError, ValueError):
        return False
    if have <= 1:                                    # 还没 map：别猜
        return False
    return need > have + 2


def view_shortcut_bindings(keys: Sequence[str] = VIEW_KEYS,
                           modifier: str = "Control") -> tuple[tuple[str, str], ...]:
    """(Tk 事件序列, 视图 key)：13 个视图各覆盖一次（前 9 项 + 后 4 项）。"""
    bindings: list[tuple[str, str]] = []
    for index, key in enumerate(tuple(keys)[:9], start=1):
        bindings.append(("<" + modifier + "-Key-" + str(index) + ">", key))
    for index, key in enumerate(tuple(keys)[9:13], start=1):
        bindings.append(("<" + modifier + "-Shift-" + SHIFTED_DIGITS[index] + ">", key))
    return tuple(bindings)


#: 数字单元格（B7）：去币种/千分位后是纯数字，可带 %/K/M/GB 这类短单位。
_NUMERIC_CELL = re.compile(
    r"^[¥￥$€]?\s*[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|k|K|m|M|x|GB|MB|ms|s)?$")


def looks_numeric(value: Any) -> bool:
    """``"1,234"`` / ``"¥12.00"`` / ``"98%"`` 算数字；``"—"`` / 中文 / 编号不算。"""
    text = str(value).strip()
    if not text or text in ("—", "-", "–"):
        return False
    return bool(_NUMERIC_CELL.match(text))


def numeric_columns(columns: Sequence[str], rows: Sequence[Any]) -> set[str]:
    """表里哪些列是数字列（B7）。

    判据：该列非空单元格里 ≥80% 是数字（``looks_numeric``）。用于把
    「Tokens / 用量 / 金额」这类列连同表头一起右对齐；编号、时间、状态保持左对齐。
    """
    found: set[str] = set()
    for index, column in enumerate(columns):
        values: list[str] = []
        for row in rows:
            cells = getattr(row, "cells", ())
            if index < len(cells):
                value = str(cells[index]).strip()
                if value and value not in ("—", "-", "–"):
                    values.append(value)
        if not values:
            continue
        hits = sum(1 for value in values if looks_numeric(value))
        if hits >= max(1, int(round(len(values) * 0.8))):
            found.add(column)
    return found


def sync_shortcut_hint(text: Any) -> str:
    """状态栏提示与真实绑定同步（B5）：替换 controller 默认文案里的旧说法。

    ``controller.DEFAULT_MESSAGE`` 不在本流的文件所有权内（不改接口/不改文案本体），
    显示层做同源替换；待 controller 维护者方便时再把默认文案本身更新。
    """
    return str(text).replace("Ctrl+1…9 切换视图", VIEW_SHORTCUT_HINT)


class HeadlessError(RuntimeError):
    """无图形环境：``main`` 据此返回 3 而不是抛栈。"""


def _import_tk():
    import tkinter
    return tkinter


def _has_display() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class DesktopApp:
    """主窗口：一个 sidebar + 一个内容区（视图切换只重画内容区，不重建侧栏）。"""

    #: 主题切换的渐变：3 帧 × 40ms = 120ms（只改背景色，不做重绘风暴）
    THEME_FADE_STEPS = 3
    THEME_FADE_MS = 40
    #: 视图切入淡入：3 帧 × 33ms ≈ 100ms
    PAGE_FADE_STEPS = 3
    PAGE_FADE_MS = 33

    def __init__(self, controller: DesktopController, *, root: Any = None,
                 tk: Any = None, title: str = "DJB 联脑 · 桌面控制台",
                 bus: Any | None = None, on_quit: Any = None,
                 prefs_home: Any = None) -> None:
        # DPI：必须在 Tk() 之前开感知（PMv2 → per-monitor → system 三级回退，幂等）
        try:
            platform_ui.enable_high_dpi()
        except Exception:                          # pragma: no cover - 非 Windows 无副作用
            pass
        self._tk = tk if tk is not None else _import_tk()
        try:
            self._root = root if root is not None else self._tk.Tk()
        except Exception as exc:                    # TclError：没有 DISPLAY / 会话不可用
            raise HeadlessError("窗口创建失败：" + type(exc).__name__) from None
        from tkinter import ttk
        self._ttk = ttk
        # 倍率：DJB_UI_SCALE 覆盖（测试/回滚）→ 否则按 Tk 实际 DPI（aqua 恒 1.0）
        try:
            self._scale = float(os.environ.get("DJB_UI_SCALE") or platform_ui.ui_scale(self._root))
        except Exception:                          # pragma: no cover - 无 Tk/非法值
            self._scale = 1.0
        if not (0.5 <= self._scale <= 4.0):
            self._scale = 1.0
        self._px = lambda n: int(round(float(n) * self._scale))
        self._controller = controller
        if getattr(controller, "save_path", None) is None:
            controller.save_path = _make_save_path(lambda: self._root)
        if getattr(controller, "ask_code", None) is None:
            controller.ask_code = self._ask_bind_code
        self._bus = bus
        self._on_quit = on_quit
        self._unsubscribe = None
        self._nav_buttons: dict[str, Any] = {}
        self._fields: dict[str, Any] = {}
        self._row_notes: dict[str, str] = {}
        self._tree: Any = None
        self._detail: Any = None
        self._detail_columns: tuple[str, ...] = ()
        self._vars: dict[str, Any] = {}
        self._root.title(title)
        # 125% 下 1180×780 逻辑 → 1475×975 物理；小屏按屏幕收缩（XPLAT-04），不出界
        try:
            screen_w = int(self._root.winfo_screenwidth())
            screen_h = int(self._root.winfo_screenheight())
        except Exception:                          # pragma: no cover - 无窗口
            screen_w = screen_h = 0
        self._root.geometry(platform_ui.initial_geometry(
            self._root, self._px(1180), self._px(780)))
        self._root.minsize(*platform_ui.fit_minsize(
            self._px(900), self._px(620), screen_w, screen_h))
        # 偏好落在数据源自己的 home（临时 home 的窗口不会写真实 ~/.djb）
        self._prefs_home = prefs_home if prefs_home is not None else getattr(
            getattr(controller, "source", None), "_home", None)
        self._fade_job = None
        self._theme_name = dual.load(self._prefs_home)
        dual.apply(self._root, ttk, self._theme_name)
        self._build_shell()
        self._bind_keys()
        self._root.bind("<Configure>",
                        lambda event: self._apply_layout(getattr(event, "width", None)), add="+")
        if self._bus is not None:
            self._unsubscribe = self._bus.subscribe(self._on_bus)
        self.show_view(controller.view)

    # ── 骨架（设计 01 §1.1：43 应用头 / 208 侧栏 / 68 页头 / 30 状态条）
    #: 三段壳的高度（设计值；窗口总高会多出 Windows 原生标题栏 ~31px，见 R6）
    HEADER_H = 43
    PAGEHEAD_H = 68
    #: 状态条 30（B6 复核）：27 时 11/10px 中文字体行高 22 + 上下留白 8 > 27，
    #: 13 个视图全都报 ``text-clip-h``（标签 19 vs 需要 22）；30 给足行高。
    STATUS_H = 30
    #: 侧栏宽度（设计 208）
    SIDEBAR_W = 208
    #: 两列断点：< 这个宽度右栏落到主栏下面（R5/G-11 只做两档，不做图标塔）
    #: 数值与 :mod:`ui_geom` 同源，单测里会比对（几何计算在那边，这里只是给 Tk 层引用）
    TWO_COL_WIDTH = 1180

    def _build_shell(self) -> None:
        """三段壳：应用头（跨整宽）→ 侧栏 + 主体（页头 + 滚动内容）→ 状态条（跨整宽）。

        侧栏从应用头**下沿**开始，不再横跨顶栏（01 §1.1 / G-03）。
        """
        from . import ui_geom as geom
        from . import ui_kit as kit

        self._geom = geom
        self._kit = kit
        tk = self._tk
        # fonts_for 缓存按 root 键「先到先得」：必须先按 scale 播种，再 apply_kit_styles
        self._fonts = kit.fonts_for(self._root, scale=self._scale)
        kit.apply_kit_styles(self._root, self._ttk)
        if self._scale != 1.0:
            try:                                   # 行高跟着字号一起放大（26 → 32@125%）
                self._ttk.Style().configure("Kit.Treeview", rowheight=self._px(26))
                # 必须从**基常量**派生：原地写 self._px(NavItem.HEIGHT) 会让类属性
                # 每开一个窗口就再乘一次 scale（34→35→36→…），同进程第二个窗口起布局漂移。
                kit.NavItem.HEIGHT = self._px(kit.NAV_ITEM_HEIGHT)
            except Exception:                      # pragma: no cover - 主题缺样式
                pass
        self._root.rowconfigure(1, weight=1)
        self._root.columnconfigure(1, weight=1)
        self._build_header()
        side = tk.Frame(self._root, background=dual.color("surface3"),
                        width=self._px(self.SIDEBAR_W), bd=0, highlightthickness=0)
        side.grid(row=1, column=0, sticky="nsew")
        side.grid_propagate(False)                     # 固定 208：内容再长也不许撑宽
        side.pack_propagate(False)                     # 子控件是 pack 的：这条才是真正生效的那条
        self._side = side
        self._build_sidebar()
        main = tk.Frame(self._root, background=dual.color("bg"), bd=0, highlightthickness=0)
        main.grid(row=1, column=1, sticky="nsew")
        main.rowconfigure(1, weight=1)
        main.columnconfigure(0, weight=1)
        self._main = main
        self._build_pagehead()
        wrapper = tk.Frame(main, background=dual.color("bg"), bd=0, highlightthickness=0)
        wrapper.grid(row=1, column=0, sticky="nsew")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(wrapper, background=dual.color("bg"), highlightthickness=0,
                                borderwidth=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = self._ttk.Scrollbar(wrapper, orient="vertical", style="Kit.Vertical.TScrollbar",
                                        command=self._canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._scrollbar = scrollbar
        self._canvas.configure(yscrollcommand=scrollbar.set)
        # 滚轮直接可用（ttk 的 Scrollbar 只在指针悬停时才滚）；单一派发器：
        # 指针在侧栏导航上滚导航、在内容区滚内容（B2：矮窗要能翻到最后一项）
        # 滚轮按平台绑「真实会到货」的序列：X11 是 Button-4/5，绑 MouseWheel 等于没绑
        platform_ui.bind_wheel(self._root, self._on_mousewheel)
        padding = (self._px(26), self._px(18)) if not self._is_narrow() else (self._px(12), self._px(12))
        self._content = tk.Frame(self._canvas, background=dual.color("bg"), bd=0,
                                 highlightthickness=0)
        self._content_id = self._canvas.create_window((0, 0), window=self._content, anchor="nw")
        self._content_pad = padding
        self._content.bind("<Configure>", self._on_content_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        # B4：首次 map 也要把内容宽钉回画布宽（Windows 启动即目标尺寸时没有额外的 Configure）
        self._canvas.bind("<Map>", lambda _event: self._sync_content_width(), add="+")
        self._build_statusbar()
        # 窄窗导航下拉（<760 时替代侧栏；一个视图都不少）
        self._nav_combo = self._ttk.Combobox(
            self._page_actions, state="readonly", takefocus=True, width=18,
            values=[str(i) + " · " + VIEW_TITLES[key]
                    for i, key in enumerate(VIEW_KEYS, start=1)])
        self._nav_combo.bind("<<ComboboxSelected>>", self._on_nav_combo)
        self._nav_combo.pack(side="right")
        self._nav_combo.pack_forget()

    def _build_header(self) -> None:
        """43px 应用头：品牌 + 状态徽标 … SYNC 时刻 + 跳转 + 主题 + 刷新。

        Windows 原生标题栏保留（既定取舍：不伪造 —/□/×），所以这 43px 只放应用自己的东西。
        """
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        head = tk.Frame(self._root, background=dual.color("surface"), height=self._px(self.HEADER_H),
                        bd=0, highlightthickness=0)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.grid_propagate(False)                     # 固定 43：子控件不改变条高
        head.pack_propagate(False)                     # 子控件用 pack：这条才是真正生效的那条
        self._header = head
        brand = tk.Frame(head, background=dual.color("surface"))
        brand.pack(side="left", padx=(14, 0))
        self._brand = brand
        logo = tk.Canvas(brand, width=self._px(24), height=self._px(24), background=dual.color("surface"),
                         bd=0, highlightthickness=0, takefocus=0)
        logo.pack(side="left", pady=self._px(9))
        logo.create_polygon(self._geom.round_rect_points(
            0.5, 0.5, self._px(24) - 0.5, self._px(24) - 0.5, self._px(6)), smooth=True,
            splinesteps=12, fill=dual.chart("mint_bright"),
            outline=dual.chart("mint_bright"))
        logo.create_text(self._px(12), self._px(12), text="D", fill=dual.color("ink"),
                         font=fonts.sans(kit.PX_CARD, bold=True))
        tk.Label(brand, text="DJB 联脑", font=fonts.sans(kit.PX_CARD, bold=True),
                 foreground=dual.color("ink"), background=dual.color("surface")).pack(
                     side="left", padx=(8, 0))
        self._brand_sub = tk.Label(brand, text="· 节点工作台",
                                   font=fonts.sans(kit.PX_SMALL),
                                   foreground=dual.color("muted_text"),
                                   background=dual.color("surface"))
        self._brand_sub.pack(side="left", padx=(6, 0))
        self._state_pill = kit.Chip(brand, "状态未知", tone="muted", ground=dual.color("surface"))
        self._state_pill.pack(side="left", padx=(10, 0))
        right = tk.Frame(head, background=dual.color("surface"))
        right.pack(side="right", padx=(0, 12))
        self._header_right = right
        self._refresh_button = kit.Button(right, "刷新", self.refresh, kind="ghost",
                                          ground=dual.color("surface"), px=kit.PX_SMALL,
                                          height=self._px(26), pad=self._px(10))
        self._theme_button = kit.Button(right, self._theme_button_text(), self.toggle_theme,
                                        kind="ghost", ground=dual.color("surface"),
                                        px=kit.PX_SMALL, height=self._px(26), pad=self._px(10))
        self._log_button = kit.Button(right, "打开日志", lambda: self.show_view("logs"),
                                      kind="ghost", ground=dual.color("surface"),
                                      px=kit.PX_SMALL, height=self._px(26), pad=self._px(10))
        self._tune_button = kit.Button(right, "调整贡献", lambda: self.show_view("yield"),
                                       kind="primary", ground=dual.color("surface"),
                                       px=kit.PX_SMALL, height=self._px(26), pad=self._px(12))
        self._sync_label = tk.Label(right, text="SYNC —", font=fonts.mono(kit.PX_MICRO),
                                    foreground=dual.color("muted_text"),
                                    background=dual.color("surface"))
        self._pack_header_right(narrow=False)
        # 头下沿一条 1px 分隔线（挂在头里，换肤重建时一起销毁）：无阴影，只靠描边分层
        tk.Frame(head, background=dual.color("line"), height=1).place(
            x=0, y=self._px(self.HEADER_H) - 1, relwidth=1.0)

    def _pack_header_right(self, *, narrow: bool) -> None:
        """右侧控件顺序固定：SYNC · 打开日志 · 调整贡献 · 主题 · 刷新（窄窗只留后两个）。"""
        for widget in (self._refresh_button, self._theme_button, self._log_button,
                       self._tune_button, self._sync_label):
            widget.pack_forget()
        self._refresh_button.pack(side="right", padx=(6, 0))
        self._theme_button.pack(side="right", padx=(6, 0))
        if not narrow:
            self._tune_button.pack(side="right", padx=(6, 0))
            self._log_button.pack(side="right", padx=(10, 0))
            self._sync_label.pack(side="right", padx=(12, 6))

    def _build_sidebar(self) -> None:
        """208px 侧栏：节点身份块 + 图标导航（13 项，一个都不少）+ 底部版本。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        side = self._side
        ground = dual.color("surface3")
        ident = tk.Frame(side, background=ground)
        ident.pack(fill="x", padx=16, pady=(14, 10))
        self._ident = ident
        tk.Label(ident, text="NODE / EDGE", font=fonts.mono(kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=ground, anchor="w").pack(fill="x")
        self._ident_name = tk.Label(ident, text="—", font=fonts.sans(kit.PX_CARD, bold=True),
                                    foreground=dual.color("ink"), background=ground, anchor="w")
        self._ident_name.pack(fill="x", pady=(2, 2))
        line = tk.Frame(ident, background=ground)
        line.pack(fill="x")
        self._ident_dot = tk.Canvas(line, width=10, height=10, background=ground, bd=0,
                                    highlightthickness=0, takefocus=0)
        self._ident_dot.pack(side="left")
        self._ident_dot.create_oval(2, 2, 8, 8, fill=dual.color("mint_text"), outline="")
        self._ident_state = tk.Label(line, text="状态未知", font=fonts.sans(kit.PX_MICRO),
                                     foreground=dual.color("muted_text"), background=ground,
                                     anchor="w")
        self._ident_state.pack(side="left", padx=(4, 0))
        self._ident_meta = tk.Label(ident, text="—", font=fonts.sans(kit.PX_MICRO),
                                    foreground=dual.color("muted_text"), background=ground,
                                    anchor="w", justify="left")
        self._ident_meta.pack(fill="x", pady=(4, 0))
        kit.Divider(side, color=dual.color("line")).pack(fill="x", padx=12)
        # 导航区可滚动（B2）：13 项 + 4 个分组头在 900×620 放不下，滚动条只在溢出时出现；
        # 不创建横向滚动条，内层宽永远钉成画布宽。
        nav_area = tk.Frame(side, background=ground)
        nav_area.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        nav_area.rowconfigure(0, weight=1)
        nav_area.columnconfigure(0, weight=1)
        self._nav_area = nav_area
        self._nav_canvas = tk.Canvas(nav_area, background=ground, highlightthickness=0,
                                     borderwidth=0, takefocus=0)
        self._nav_canvas.grid(row=0, column=0, sticky="nsew")
        self._nav_scrollbar = self._ttk.Scrollbar(
            nav_area, orient="vertical", style="Kit.Vertical.TScrollbar",
            command=self._nav_canvas.yview)
        self._nav_scrollbar.grid(row=0, column=1, sticky="ns")
        self._nav_canvas.configure(yscrollcommand=self._nav_scrollbar.set)
        self._nav_inner = tk.Frame(self._nav_canvas, background=ground)
        self._nav_inner_id = self._nav_canvas.create_window(
            (0, 0), window=self._nav_inner, anchor="nw")
        self._nav_box = self._nav_inner
        self._nav_inner.bind("<Configure>", self._on_nav_inner_resize)
        self._nav_canvas.bind("<Configure>", self._on_nav_canvas_resize)
        self._nav_section_headers: dict[str, Any] = {}
        for index, section in enumerate(NAV_SECTIONS):
            self._render_nav_section(section, first=index == 0)
        kit.Divider(side, color=dual.color("line")).pack(fill="x", padx=12)
        foot = tk.Frame(side, background=ground)
        foot.pack(fill="x", padx=16, pady=(8, 10))
        from ..facts import CLIENT_VERSION
        self._version_label = tk.Label(
            foot, text="v" + CLIENT_VERSION, font=fonts.mono(kit.PX_MICRO),
            foreground=dual.color("muted_text"), background=ground, anchor="w")
        self._version_label.pack(side="left")
        self._demo_chip = None
        if getattr(self._controller.snapshot, "source", "") == "demo":
            self._demo_chip = kit.Chip(foot, "演示数据", tone="amber", ground=ground,
                                       px=kit.PX_MICRO, height=17)
            self._demo_chip.pack(side="right")

    def _font_for(self, text: str, px: int, *, bold: bool = False) -> Any:
        """按文本取字体：纯 ASCII 走 mono、含中文走 sans（分组头 / 页头微标共用）。

        字体流已提供 ``Fonts.for_text(text, px, bold=...)``，这里只做薄封装：
        少一处判断口径，以后回退链/字阶调整只改 ``Fonts``。
        """
        for_text = getattr(self._fonts, "for_text", None)
        if callable(for_text):
            try:
                return for_text(text, px, bold=bold)
            except TypeError:                       # pragma: no cover - 旧签名（无 bold）
                return for_text(text, px)
        if str(text).isascii():
            return self._fonts.mono(px, bold=bold)
        return self._fonts.sans(px, bold=bold)

    def _render_nav_section(self, section: Any, *, first: bool = False) -> None:
        """一个分组：10px 分组头（拉丁微标 + 中文组名）+ 该组导航项。

        只是给既有 :class:`ui_kit.NavItem` 加一层结构；选中态/图标/窄窗下拉都不变。
        """
        kit, tk = self._kit, self._tk
        ground = dual.color("surface3")
        head = tk.Frame(self._nav_inner, background=ground)
        head.pack(fill="x", padx=6, pady=(0 if first else 10, 2))
        tk.Label(head, text=section.micro, font=self._font_for(section.micro, kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=ground,
                 anchor="w").pack(side="left")
        tk.Label(head, text=section.title, font=self._fonts.sans(kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=ground,
                 anchor="w").pack(side="left", padx=(6, 0))
        self._nav_section_headers[section.key] = head
        for key in section.keys:
            item = kit.NavItem(self._nav_inner, text=VIEW_TITLES[key],
                               icon=NAV_ICONS.get(key, "dot"), ground=ground,
                               command=lambda name=key: self.show_view(name))
            item.pack(fill="x", pady=1)
            self._nav_buttons[key] = item

    # ── 侧栏导航滚动（B2：900×620 下 13 项都能点到）
    def _on_nav_inner_resize(self, _event: Any = None) -> None:
        """导航内容变大：更新滚动区、把内层宽钉成画布宽、按需露出滚动条。"""
        canvas = getattr(self, "_nav_canvas", None)
        if canvas is None:
            return
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
            width = int(canvas.winfo_width())
            if width > 1:
                current = canvas.itemcget(self._nav_inner_id, "width")
                if not str(current).strip() or int(float(current)) != width:
                    canvas.itemconfigure(self._nav_inner_id, width=width)
        except Exception:                            # pragma: no cover - 控件已销毁
            return
        self._sync_nav_scrollbar()

    def _on_nav_canvas_resize(self, event: Any) -> None:
        try:
            width = content_item_width(getattr(event, "width", 0))
            current = self._nav_canvas.itemcget(self._nav_inner_id, "width")
            if not str(current).strip() or int(float(current)) != width:
                self._nav_canvas.itemconfigure(self._nav_inner_id, width=width)
        except Exception:                            # pragma: no cover
            pass
        self._sync_nav_scrollbar()

    def _sync_nav_scrollbar(self) -> None:
        """滚动条只在 13 项 + 4 分组头真的放不下时出现（与内容区同一策略）。"""
        canvas = getattr(self, "_nav_canvas", None)
        bar = getattr(self, "_nav_scrollbar", None)
        if canvas is None or bar is None:
            return
        try:
            need = int(self._nav_inner.winfo_reqheight())
            have = int(canvas.winfo_height())
        except Exception:                            # pragma: no cover - 控件已销毁
            return
        if nav_needs_scroll(need, have):
            if not bar.winfo_ismapped():
                bar.grid()
        elif need <= have - 2 and bar.winfo_ismapped():
            bar.grid_remove()

    def _is_inside(self, widget: Any, ancestor: Any) -> bool:
        """控件是否在 ancestor 子树里（滚轮派发用；Canvas 内嵌控件也走 master 链）。"""
        node = widget
        while node is not None and node is not ancestor:
            node = getattr(node, "master", None)
        return node is ancestor

    def _on_mousewheel(self, event: Any) -> str | None:
        """滚轮只有一个入口：指针在侧栏导航上滚导航，其它地方滚内容。

        三个平台的量纲交给 ``platform_ui.normalize_wheel``（X11 无 delta、只有
        Button-4/5；aqua 的 delta 已是行数）。指针在 Treeview/Text/下拉/滚动条上时
        让位给控件自己的类绑定，避免「滚一格，表格和整页各走一遍」。
        """
        steps = platform_ui.normalize_wheel(event)
        if not steps:                                # 不是滚轮事件 / 缺量纲
            return None
        target = None
        try:
            target = self._root.winfo_containing(event.x_root, event.y_root)
        except Exception:                            # pragma: no cover - 无窗口
            target = None
        nav_area = getattr(self, "_nav_area", None)
        if target is not None and nav_area is not None and self._is_inside(target, nav_area):
            self._nav_scroll(-steps)
            return "break"
        if platform_ui.is_self_scrolling(target):
            return "break"                           # 表格/日志框自己滚，页面不动
        self._canvas.yview_scroll(-steps, "units")
        return "break"

    def _nav_scroll(self, units: int) -> None:
        canvas = getattr(self, "_nav_canvas", None)
        if canvas is None:
            return
        try:
            first, last = canvas.yview()
        except Exception:                            # pragma: no cover
            return
        if first <= 0.0 and last >= 1.0:             # 没溢出：滚轮不该动任何东西
            return
        canvas.yview_scroll(int(units), "units")

    def _build_pagehead(self) -> None:
        """68px 页头：左「眉标 + 18px 标题」，右「数据时刻 + 刷新」。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        head = tk.Frame(self._main, background=dual.color("bg"), height=self._px(self.PAGEHEAD_H),
                        bd=0, highlightthickness=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_propagate(False)
        head.columnconfigure(0, weight=1)
        self._pagehead = head
        left = tk.Frame(head, background=dual.color("bg"))
        left.grid(row=0, column=0, sticky="w", padx=(self._px(26), 0), pady=(self._px(12), self._px(12)))
        self._page_kicker = tk.Label(left, text="OVERVIEW", font=fonts.for_text("OVERVIEW", kit.PX_MICRO),
                                     foreground=dual.color("muted_text"),
                                     background=dual.color("bg"), anchor="w")
        self._page_kicker.pack(anchor="w")
        self._view_title = tk.Label(left, text=VIEW_TITLES[self._controller.view],
                                    font=fonts.sans(kit.PX_H1, bold=True),
                                    foreground=dual.color("ink"), background=dual.color("bg"),
                                    anchor="w")
        self._view_title.pack(anchor="w")
        right = tk.Frame(head, background=dual.color("bg"))
        right.grid(row=0, column=1, sticky="e", padx=(0, self._px(26)))
        self._page_actions = right
        self._snapshot_label = tk.Label(right, text="数据截至 —", font=fonts.sans(kit.PX_MICRO),
                                        foreground=dual.color("muted_text"),
                                        background=dual.color("bg"))
        self._snapshot_label.pack(side="left", padx=(0, 10))

    def _build_statusbar(self) -> None:
        """30px 状态条：左「消息」，右「GPU · 显存 · 版本 · 主题」（高度见 ``STATUS_H``）。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        bar = tk.Frame(self._root, background=dual.color("surface3"), height=self._px(self.STATUS_H),
                       bd=0, highlightthickness=0)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.pack_propagate(False)
        self._statusbar = bar
        self._status = tk.Label(bar, text=DEFAULT_MESSAGE, font=fonts.sans(kit.PX_MICRO),
                                foreground=dual.color("muted_text"),
                                background=dual.color("surface3"), anchor="w")
        self._status.pack(side="left", padx=(self._px(14), 0), pady=self._px(4))
        self._status_right = tk.Label(bar, text="—", font=fonts.sans(kit.PX_MICRO),
                                      foreground=dual.color("muted_text"),
                                      background=dual.color("surface3"), anchor="e")
        self._status_right.pack(side="right", padx=(0, self._px(14)), pady=self._px(4))

    def _sync_content_width(self) -> None:
        """把内容列宽钉回画布宽（B4：**任何时刻**内容宽 ≤ 画布宽）。

        旧实现只在画布 ``<Configure>`` 里写 item width；内容在画布配置之后才撑宽时
        （Windows 启动即目标尺寸）内容就保持自然请求宽（≈1494）→ 右侧被窗口裁掉。
        这里在内容自己变大 / 首次 map / 换视图 / 换主题之后都再写一次，且只在
        「当前值与目标值不同」时写，避免和内容的 ``<Configure>`` 形成回环。
        """
        try:
            target = content_item_width(self._canvas.winfo_width())
            current = self._canvas.itemcget(self._content_id, "width")
            current_width = int(float(current)) if str(current).strip() else 0
        except Exception:                            # pragma: no cover - 控件已销毁
            return
        if current_width != target:
            try:
                self._canvas.itemconfigure(self._content_id, width=target)
            except Exception:                        # pragma: no cover
                pass
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except Exception:                            # pragma: no cover
            pass

    def _on_content_resize(self, _event=None) -> None:
        self._sync_content_width()                   # 内容请求宽变了：宽度重新收敛
        self._sync_scrollbar()

    def _on_canvas_resize(self, event) -> None:
        self._sync_content_width()                   # 画布宽变了：内容宽跟着收敛
        self._sync_scrollbar()
        width = int(getattr(event, "width", 0) or 0)
        if abs(width - getattr(self, "_rewrap_width", 0)) > 24:
            self._rewrap_width = width
            self._rewrap()

    def _sync_scrollbar(self) -> None:
        """只在内容真的溢出时才露出滚动条（一屏装得下就没有，G-16）。

        2px 磁滞（P0）：显示要 ``need > have + 2``，收起要 ``need <= have - 2``。
        没有磁滞时，1px 的请求高摆动会让滚动条反复 grid/grid_remove，自己把自己
        卷进 Configure 回环（1180 宽死循环的另一半）。
        """
        bar = getattr(self, "_scrollbar", None)
        if bar is None:
            return
        try:
            need = int(self._content.winfo_reqheight())
            have = int(self._canvas.winfo_height())
        except Exception:                            # pragma: no cover - 控件已销毁
            return
        if have <= 1:                                # 还没 map：别猜
            return
        try:
            if need > have + 2:
                if not bar.winfo_ismapped():
                    bar.grid()
            elif need <= have - 2 and bar.winfo_ismapped():
                bar.grid_remove()
        except Exception:                            # pragma: no cover
            pass

    def _bind_keys(self) -> None:
        self._root.bind("<F5>", lambda _e: self.refresh())
        # B5：13 个视图全部可键盘直达（Ctrl+1…9 / Ctrl+Shift+1…4）+ 前后循环兜底
        for sequence, key in view_shortcut_bindings():
            self._root.bind(sequence, lambda _e, name=key: self.show_view(name))
        self._root.bind("<Control-Prior>", lambda _e: self._step_view(-1))
        self._root.bind("<Control-Next>", lambda _e: self._step_view(1))
        self._root.bind("<Control-r>", lambda _e: self.refresh())
        self._root.bind("<Escape>", lambda _e: self._canvas.yview_moveto(0))

    def _step_view(self, step: int) -> None:
        """Ctrl+PageUp/PageDown：按 IA 顺序前后切（13 个视图循环，不依赖键盘布局）。"""
        try:
            key = self._controller.next_view(int(step)).key
        except Exception:                            # pragma: no cover - 控制器异常
            return
        self.show_view(key)

    # ── 渲染
    def show_view(self, key: str) -> View:
        """切视图并立即完成布局（截图脚本依赖这一点）。"""
        try:
            view = self._controller.show(key)
        except ValueError:
            return self._controller.current()
        self._render(view)
        self._sync_nav(key)
        self._update_status()
        try:
            self._canvas.yview_moveto(0)
            self._root.update_idletasks()
        except Exception:
            pass
        self._sync_content_width()                   # B4：换视图后内容宽仍 ≤ 画布宽
        self._page_fade()
        return view

    def refresh(self) -> View:
        view = self._controller.refresh()
        self._render(view)
        self._update_status()
        return view

    # ── 双主题（day / night）
    def _theme_button_text(self, narrow: bool | None = None) -> str:
        """窄窗只留图标（390 宽时顶栏放不下四个带字的控件）。"""
        if narrow is None:
            narrow = bool(getattr(self, "_narrow", False))
        day = getattr(self, "_theme_name", dual.DEFAULT_THEME) == "day"
        if narrow:
            return "日" if day else "夜"
        return "◐ 日间" if day else "☾ 夜间"

    def toggle_theme(self, *, animate: bool = True, persist: bool = True) -> str:
        """顶栏那个按钮：day ↔ night。返回切换后的主题名。"""
        self._install_theme(dual.toggle(self._theme_name), animate=animate, persist=persist)
        return self._theme_name

    def _install_theme(self, name: str, *, animate: bool = True,
                       persist: bool = True) -> str:
        """换主题：先 120ms 背景渐变，再把样式换过去并重画当前视图。"""
        previous = dual.color("bg")
        target = dual.palette(name)["bg"]

        def finish() -> None:
            self._cancel_fade()
            self._theme_name = dual.apply(self._root, self._ttk, name)
            self._apply_kit_skin()                 # apply_theme 会把字体改回 11pt 点值，这里重新钉住
            # 外壳（头/侧栏/页头/状态条）是普通 tk 控件：换肤直接整块重建
            self._rebuild_chrome()
            self._set_widget_text(self._theme_button, self._theme_button_text())
            view = getattr(self, "_last_view", None)
            if view is not None:
                # 表格 tag 颜色、卡片 tone 都是渲染时取的：重画一次才真正换色
                self._render(view)
                tree = getattr(self, "_tree", None)
                if tree is not None:
                    self._fit_columns(tree)
            self._update_status()
            if persist:
                try:
                    dual.save(self._theme_name, self._prefs_home)
                except OSError as exc:
                    self._toast("主题已切换，但偏好没写进去：" + str(exc), ok=False)

        if not animate or not self._can_animate():
            finish()
            return self._theme_name
        self._fade(previous, target, self.THEME_FADE_STEPS, self.THEME_FADE_MS, finish)
        return self._theme_name

    def _can_animate(self) -> bool:
        return callable(getattr(self._root, "after", None))

    def _paint_backgrounds(self, colour: str | None = None) -> None:
        value = colour or dual.color("bg")
        for widget in (self._root, getattr(self, "_canvas", None)):
            if widget is None:
                continue
            try:
                widget.configure(background=value)
            except Exception:                      # pragma: no cover - 控件已销毁
                pass

    def _cancel_fade(self) -> None:
        """取消还没跑完的旧渐变：否则上一帧会把刚换好的主题底色又盖回去。"""
        job, self._fade_job = getattr(self, "_fade_job", None), None
        if job is None:
            return
        try:
            self._root.after_cancel(job)
        except Exception:                          # pragma: no cover - 已执行/已销毁
            pass

    def _fade(self, start: str, end: str, steps: int, delay_ms: int,
              done: Any) -> None:
        """分帧改背景色（只碰背景，不重排、不重绘子树）。"""
        self._cancel_fade()

        def frame(step: int) -> None:
            self._fade_job = None
            if step >= steps:
                self._paint_backgrounds(end)
                done()
                return
            self._paint_backgrounds(dual.mix_step(start, end, step + 1, steps))
            try:
                self._fade_job = self._root.after(delay_ms, lambda: frame(step + 1))
            except Exception:                      # pragma: no cover - 窗口销毁
                done()

        frame(0)

    def _page_fade(self) -> None:
        """切视图时内容区 100ms 淡入（画布底色由浅到正）。"""
        if not self._can_animate():
            return
        background = dual.color("bg")
        start = dual.blend(dual.color("surface"), background, 0.65)
        self._fade(start, background, self.PAGE_FADE_STEPS, self.PAGE_FADE_MS, lambda: None)

    #: 窄窗阈值：低于这个宽度侧栏让位给内容（E10 口径 390 也算“能看全”）
    NARROW_WIDTH = 760

    def _logical_width(self) -> int:
        """窗口的**逻辑**宽（÷倍率）：断点 760/1180 是设计稿逻辑像素。

        125% 下 1475 物理 = 1180 逻辑；如果拿物理宽直接比断点，1300 物理
        （1040 逻辑）会被误判成双列，窄窗下拉也永远不触发。
        """
        try:
            width = int(self._root.winfo_width())
        except Exception:                            # pragma: no cover - 无窗口
            return 0
        scale = getattr(self, "_scale", 1.0) or 1.0
        return int(round(width / float(scale))) if width > 1 else width

    def _is_narrow(self) -> bool:
        """窄不窄以窗口真实宽度为准（不依赖 _apply_layout 的先后顺序）。"""
        try:
            return self._geom.layout_mode(self._logical_width()) == "narrow"
        except Exception:                            # pragma: no cover - 无窗口
            return False

    def _apply_layout(self, width: int | None = None) -> None:
        """宽度变化只做两件事：窄窗把侧栏换成下拉；跨过 1180 时双列 ↔ 单列重排。

        只看**根窗口**的宽度，且只在宽度真的变化时才动布局——
        子控件的 ``<Configure>`` 也会冒到这里，按事件里的 width 判断会让侧栏反复收起/展开。
        """
        try:
            current = self._logical_width()
        except Exception:                                  # pragma: no cover - 无窗口
            return
        if current <= 1:                                   # 还没 map：没有可用宽度
            return
        if current == getattr(self, "_layout_width", None):
            return
        self._layout_width = current
        mode = self._geom.layout_mode(current)
        narrow, two_col = mode == "narrow", mode == "two"
        flipped = (getattr(self, "_narrow", None) not in (None, narrow)
                   or getattr(self, "_two_col_mode", None) not in (None, two_col))
        self._narrow = narrow
        self._two_col_mode = two_col
        if narrow:
            self._side.grid_remove()
            self._nav_combo.pack(side="right")
            self._snapshot_label.pack_forget()
            self._brand_sub.pack_forget()
            self._state_pill.pack_forget()
            self._pack_header_right(narrow=True)
            self._set_widget_text(self._theme_button,
                                  "日" if self._theme_name == "day" else "夜")
        else:
            self._side.grid()
            self._nav_combo.pack_forget()
            self._snapshot_label.pack(side="left", padx=(0, 10))
            if not self._brand_sub.winfo_ismapped():
                self._brand_sub.pack(side="left", padx=(6, 0))
            if not self._state_pill.winfo_ismapped():
                self._state_pill.pack(side="left", padx=(10, 0))
            self._pack_header_right(narrow=False)
            self._set_widget_text(self._theme_button, self._theme_button_text())
        self._sync_nav(self._controller.view)
        if flipped and hasattr(self, "_status"):
            self._update_status()                    # 状态条文案也分窄/宽两套
        tree = getattr(self, "_tree", None)
        if tree is not None:
            self._apply_narrow_cells(narrow)         # 行文案跟着窄/宽切换
            self._fit_columns(tree)                  # 让位/回位后重排一次列宽
        if flipped and not getattr(self, "_in_render", False):
            # 卡片列数、双列栅格、内距都是渲染时定的：翻转后必须重画一次
            self._relayout_pending = True
            try:
                self._root.after_idle(self._reflow)
            except Exception:                        # pragma: no cover - 无窗口
                pass

    def _reflow(self) -> None:
        """窄/宽刚翻转：按新宽度重画当前视图（只在翻转时发生，不参与每次 resize）。"""
        if not getattr(self, "_relayout_pending", False):
            return
        self._relayout_pending = False
        if getattr(self, "_in_render", False):
            return
        view = getattr(self, "_last_view", None)
        if view is None:                             # 还没画过任何视图
            return
        try:
            # 重画**同一份快照**：resize 不该顺手再拉一次数据（会白等网络，
            # 还可能撞上瞬时失败把「已停止」变成「状态未知」）。
            self._render(view)
            tree = getattr(self, "_tree", None)
            if tree is not None:
                self._fit_columns(tree)
        except Exception:                            # pragma: no cover - 窗口正在销毁
            pass

    def _set_widget_text(self, widget: Any, text: str) -> None:
        """给「Canvas 按钮」和「ttk 控件」都能用的改字助手。"""
        if widget is None:
            return
        setter = getattr(widget, "set_text", None)
        try:
            if callable(setter):
                setter(text)
            else:
                widget.configure(text=text)
        except Exception:                            # pragma: no cover - 控件已销毁
            pass

    def _rebuild_chrome(self) -> None:
        """换肤时整块重建外壳：tk 控件不像 ttk 那样跟样式走，重建比逐个改色可靠。"""
        for child in list(self._root.grid_slaves()):
            child.destroy()
        # 旧内容控件已随外壳销毁：先断引用，否则 _apply_layout/_fit_columns 会摸到
        # 已销毁的 Treeview（换肤时 TclError: invalid command name ...!treeview）
        self._tree = None
        self._table = None
        self._detail = None
        self._row_cells = {}
        self._row_notes = {}
        self._wrap_targets = []
        self._nav_buttons = {}
        self._nav_combo = None
        self._layout_width = None
        self._narrow = None
        self._two_col_mode = None
        self._build_shell()
        self._apply_layout(self._root.winfo_width())
        self._sync_content_width()                   # B4：换主题重建后宽度不变量仍成立

    def _apply_narrow_cells(self, narrow: bool) -> None:
        """窄窗用行级紧凑文案，回宽恢复完整文案（数据两份都在视图里，不丢字段）。"""
        table = getattr(self, "_table", None)
        tree = getattr(self, "_tree", None)
        if table is None or tree is None:
            return
        for row in getattr(table, "rows", ()) or ():
            iid = row.key or ""
            if not iid:
                continue
            compact = getattr(row, "narrow", ())
            values = (compact if narrow and compact and len(compact) == len(row.cells)
                      else row.cells)
            try:
                tree.item(iid, values=values)
            except Exception:                        # pragma: no cover - 行已被重建
                pass

    def _on_nav_combo(self, _event=None) -> None:
        value = str(self._nav_combo.get() or "")
        if "·" in value:
            index = value.split("·", 1)[0].strip()
            for position, key in enumerate(VIEW_KEYS, start=1):
                if str(position) == index:
                    self.show_view(key)
                    return

    def _sync_nav(self, key: str) -> None:
        for name, item in self._nav_buttons.items():
            item.set_active(name == key)
        combo = getattr(self, "_nav_combo", None)
        if combo is not None:
            try:                                           # 窄窗下拉与侧栏保持同一状态
                index = VIEW_KEYS.index(key) + 1
                combo.set(str(index) + " · " + VIEW_TITLES[key])
            except Exception:                              # pragma: no cover - 视图名变了
                pass

    # ── 状态（状态条 / 徽标 / 侧栏身份块 / 页头时刻）
    def _update_status(self) -> None:
        """把快照里的状态一次写进四处：状态条消息、状态徽标、身份块、页头「数据截至」。"""
        snapshot = self._controller.snapshot
        self._status.configure(text=self._status_line())
        pill, tone = self._pill_text()
        try:
            self._state_pill.set_text(pill, tone)
        except Exception:                                  # pragma: no cover - 控件已销毁
            pass
        self._update_identity()
        self._update_clock()
        self._status_right.configure(text=self._status_right_text())

    def _pill_text(self) -> tuple[str, str]:
        """状态徽标文案 + tone（mint/amber/red/muted）；未知就说未知。"""
        status = snapshot_section(self._controller.snapshot, "status")
        state = str(status.get("state") or "")
        if state in ("online", "starting"):
            return "● 运行中", "mint"
        if state in ("paused", "updating"):
            return "◐ 已暂停", "amber"
        if state == "error":
            return "✕ 错误", "red"
        if state == "stopped":
            return "○ 已停止", "muted"
        return "? 状态未知", "muted"

    def _update_identity(self) -> None:
        """侧栏身份块：节点号 / 在线态 / 显卡与显存。全部来自既有数据域，缺就写「—」。"""
        snapshot = self._controller.snapshot
        account = snapshot_section(snapshot, "account")
        profile = account.get("profile") if isinstance(account.get("profile"), Mapping) else {}
        access = snapshot_section(snapshot, "access")
        binding = access.get("binding") if isinstance(access.get("binding"), Mapping) else {}
        node_id = str(profile.get("node_id") or binding.get("node_id") or "—")
        self._ident_name.configure(text=node_id)
        pill, _tone = self._pill_text()
        self._ident_state.configure(text=pill.lstrip("●◐○✕? ").strip() or "状态未知")
        gpu = snapshot_section(snapshot, "gpu")
        devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
        device = devices[0] if devices and isinstance(devices[0], Mapping) else {}
        name = str(device.get("name") or "—")
        if len(name) > 22:
            name = name[:21] + "…"
        total = device.get("memory_total_mb")
        vram = (str(round(float(total) / 1024.0)) + " GB") if is_number(total) else "—"
        self._ident_meta.configure(text=name + " · " + vram if device else "显卡：暂不可用")

    def _update_clock(self) -> None:
        """页头/头上的「数据截至」：只用 ``Snapshot.fetched_at``，没有就写「—」。"""
        stamp = short_clock(self._controller.snapshot.fetched_at)
        self._sync_label.configure(text="SYNC " + stamp)
        self._snapshot_label.configure(text="数据截至 " + stamp)

    def _status_line(self) -> str:
        """状态条左侧消息：窄窗只留前两段（否则会挤到右侧的 GPU/版本上去）。"""
        line = sync_shortcut_hint(self._controller.status_line())
        if not getattr(self, "_narrow", False):
            return line
        parts = [part.strip() for part in line.split("·") if part.strip()]
        return " · ".join(parts[:2])

    def _status_right_text(self) -> str:
        """状态条右侧：GPU 利用率 · 显存 · 版本 · 主题（缺值写「—」，不写 0）。"""
        from ..facts import CLIENT_VERSION
        snapshot = self._controller.snapshot
        gpu = snapshot_section(snapshot, "gpu")
        devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
        device = devices[0] if devices and isinstance(devices[0], Mapping) else {}
        util = device.get("utilization_pct")
        used, total = device.get("memory_used_mb"), device.get("memory_total_mb")
        theme_name = "日间" if self._theme_name == "day" else "夜间"
        if getattr(self, "_narrow", False):
            # 窄窗：GPU/显存留给主卡，状态条只留版本与主题（否则两段文字会叠在一起）
            return "v" + CLIENT_VERSION + "  ·  " + theme_name
        parts = ["GPU " + (str(int(float(util))) + "%" if is_number(util) else "—"),
                 "显存 " + ("%.1f/%.0f GB" % (float(used) / 1024.0, float(total) / 1024.0)
                          if is_number(used) and is_number(total) else "—"),
                 "v" + CLIENT_VERSION,
                 theme_name]
        return "  ·  ".join(parts)

    def _apply_kit_skin(self) -> None:
        """把 ttk 字体与 ``*Font`` 默认字体重新钉回 ui_kit 那套（幂等，换肤后必须调一次）。

        ``gui.theme.apply_theme`` 每次都写 ``option_add("*Font", (family, 11))`` 并重设裸 ttk
        样式字体，而 ``ttk.Style`` / option 数据库都是**解释器级**的：换肤（含二次确认窗）之后
        不重钉，就会退回「11pt 点值 + 13px 像素」两套字体并存（01-FONT-DECISION §1 F6）。
        顺带把 ``Kit.*`` 的颜色刷新到新主题（原来换肤后表格/输入框仍是旧主题的颜色）。
        """
        try:
            self._kit.apply_kit_styles(self._root, self._ttk)
        except Exception:                          # pragma: no cover - 控件已销毁
            pass

    def _set_font(self, label: Any, text: str, px: int, *, bold: bool = False) -> None:
        """给「内容会变」的 Label 连文案带族一起换：ASCII → mono，含中文 → sans。

        只换文字不换字体，正是「中文被塞进 mono」的来源（01-FONT-DECISION §1 F1）：
        这类标签初值是拉丁（如 ``OVERVIEW``），运行时可能被换成中文（如 ``总览``）。
        """
        try:
            label.configure(text=text, font=self._fonts.for_text(text, px, bold=bold))
        except Exception:                              # pragma: no cover - 控件已销毁
            pass

    def _clear(self) -> None:
        for child in self._content.winfo_children():
            child.destroy()
        self._fields.clear()
        self._vars.clear()
        self._row_notes.clear()
        self._tree = None
        self._detail = None
        self._table = None
        self._wrap_targets = []

    # ── 渲染入口
    def _render(self, view: View) -> None:
        """按视图能力分派：带 hero/chart/rail 的（总览）走设计样双列，其余走通用卡片版面。"""
        tk = self._tk
        kit = self._kit
        self._last_view = view
        self._in_render = True
        self._rendered_narrow = self._is_narrow()
        self._rendered_two_col = self._two_col()
        self._clear()
        self._view_title.configure(text=view.title)
        self._set_font(self._page_kicker,
                       PAGE_KICKERS.get(view.key, view.key.upper()), kit.PX_MICRO)
        pad = (12, 6) if self._rendered_narrow else (26, 6)
        self._content_pad = pad
        body = tk.Frame(self._content, background=dual.color("bg"), bd=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        self._body = body
        if view.subtitle:
            self._wrap(tk.Label(body, text=view.subtitle, font=self._fonts.sans(kit.PX_SMALL),
                              foreground=dual.color("muted_text"), background=dual.color("bg"),
                              anchor="w", justify="left", wraplength=self._wrap_hint())).pack(
                                  anchor="w", pady=(0, 4))
        for text in view.unavailable:
            self._wrap(tk.Label(body, text="! " + text, font=self._fonts.sans(kit.PX_SMALL),
                              foreground=dual.color("amber_text"), background=dual.color("bg"),
                              anchor="w", justify="left", wraplength=self._wrap_hint())).pack(
                                  anchor="w", pady=(0, 4))
        if view.hero is not None or view.chart is not None or view.rail:
            self._render_dashboard(view)
        else:
            self._render_generic(view)
        for note in view.notes:
            if not note:
                continue
            self._wrap(tk.Label(body, text="· " + note, font=self._fonts.sans(kit.PX_MICRO),
                              foreground=dual.color("muted_text"), background=dual.color("bg"),
                              anchor="w", justify="left", wraplength=self._wrap_hint())).pack(
                                  anchor="w", pady=(2, 0))
        self._in_render = False
        self._sync_content_width()                   # B4：内容重排后把宽度钉回画布宽

    def _wrap(self, label: Any) -> Any:
        """登记一个「按窗口宽度换行」的文本控件（窗口变宽变窄时统一重算，只改 wraplength）。"""
        try:
            self._wrap_targets.append(label)
        except AttributeError:                       # pragma: no cover - 还没渲染过
            self._wrap_targets = [label]
        return label

    def _wrap_to_column(self, label: Any) -> Any:
        """登记「按自己那一列的实际宽度换行」的文本控件（ROUND3 真缺陷）。

        表单行是两层网格：外层 block 的 column1 只分到「父列宽 − 标签列(104) − 间距」，
        900 宽单列时约 170–180 px。可 `_wrap_hint()` 是按**窗口**估的（~620），
        label 又带着这个大 wraplength 被 grid 压回列宽 ⇒ 尾部被硬切：
        audit 实测 'TRC20（T 开头 34 位）或 EVM（0x + 40 位）' 需要 205 px 只有 171 px、
        'http://127.0.0.1:1717/agent' 需要 193 px 只有 178 px。用户看到的是半句话。
        这里把 wraplength 钳到 label **实际分到**的宽度，并随列宽变化重算。
        """
        self._wrap(label)
        try:
            self._column_wrap_targets.append(label)
        except AttributeError:                       # pragma: no cover - 还没渲染过
            self._column_wrap_targets = [label]
        label.bind("<Configure>", lambda event, target=label: self._fit_wrap(target), add="+")
        return label

    def _fit_wrap(self, label: Any, width: int | None = None) -> None:
        """把单条 label 的换行宽钳到它实际分到的宽度（只减不增，防抖）。"""
        try:
            span = int(label.winfo_width() if width is None else width)
        except Exception:                            # pragma: no cover - 控件已销毁
            return
        span -= 2
        if span < 80:
            return
        try:
            current = int(float(label.cget("wraplength")))
        except Exception:                            # pragma: no cover - 防御
            current = -1
        if current != span:
            try:
                label.configure(wraplength=span)
            except Exception:                        # pragma: no cover - 控件已销毁
                pass

    def _rewrap(self) -> None:
        """宽度变了但没跨断点：只更新换行宽度，不重建控件。"""
        if not getattr(self, "_wrap_targets", None):
            return
        width = self._wrap_hint()
        column_targets = getattr(self, "_column_wrap_targets", None) or []
        for label in self._wrap_targets:
            if label in column_targets:              # 表单行：按自己那一列算，别用窗口估宽
                self._fit_wrap(label)
                continue
            try:
                label.configure(wraplength=width)
            except Exception:                        # pragma: no cover - 控件已销毁
                pass

    def _wrap_hint(self) -> int:
        """文本换行宽度（像素）：按当前窗口估，宽窗留出侧栏与内距。"""
        try:
            width = int(self._root.winfo_width())
        except Exception:                              # pragma: no cover - 无窗口
            return 620
        if width <= 1:
            return 620
        side = 0 if self._is_narrow() else self._px(self.SIDEBAR_W)
        return max(220, width - side - 2 * self._content_pad[0] - 40)

    def _two_col(self) -> bool:
        """是否双列（设计 798/360）。R5/G-11：只做两档，>=1180 双列，其余单列。"""
        try:
            width = self._logical_width()
        except Exception:                              # pragma: no cover - 无窗口
            return True
        if width <= 1:
            return True
        return self._geom.layout_mode(width) == "two"

    # ── 总览：双列仪表盘
    def _render_dashboard(self, view: View) -> None:
        tk = self._tk
        two_col = self._two_col()
        grid = tk.Frame(self._body, background=dual.color("bg"), bd=0, highlightthickness=0)
        grid.pack(fill="both", expand=True)
        if two_col:
            # 设计栅格 798 : 360，右栏固定 340（最小可读宽）。
            # **右栏绝不能写 weight>0 + minsize**：Tk 会在「按权重重分」和「按 reqwidth 撑开」
            # 之间来回切（实测 1180 宽时右栏 330↔368、左栏 596↔558，Bars 宽 567↔568 无限
            # 重绘 → update_idletasks 永不返回）。左栏吃掉剩余宽度即可。
            grid.columnconfigure(0, weight=1, minsize=420)
            grid.columnconfigure(1, weight=0, minsize=340)
        else:
            grid.columnconfigure(0, weight=1)
        left = tk.Frame(grid, background=dual.color("bg"), bd=0, highlightthickness=0)
        left.grid(row=0, column=0, sticky="nsew")
        right = tk.Frame(grid, background=dual.color("bg"), bd=0, highlightthickness=0)
        if two_col:
            right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        else:
            right.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        if view.hero is not None:
            self._render_hero(left, view.hero)
        if view.chart is not None:
            self._render_chart(left, view.chart)
        if view.table is not None:
            # 总览只放「最近几张」：整页要一屏装下（G-16），完整列表在「收据」页
            self._render_table(view.table, parent=left, max_rows=4)
        for card in view.rail:
            self._render_rail_card(right, card)
        if view.actions:
            bar = tk.Frame(left, background=dual.color("bg"))
            bar.pack(fill="x", pady=(2, 0))
            for action in view.actions:
                self._render_action(bar, action)

    def _render_hero(self, parent: Any, hero: Hero) -> None:
        """状态主卡：左「标题 + 说明 + 运行时长 + 动作」，右 125px 仪表环，下面四指标。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        surface = dual.color("surface")
        card = kit.RoundedCard(parent, ground=dual.color("bg"), pad=(14, 10))
        card.pack(fill="x", pady=(0, 10))
        body = card.body
        top = tk.Frame(body, background=surface)
        top.pack(fill="x")
        if hero.gauge is not None:
            gauge = kit.Gauge(top, ground=surface)
            gauge.pack(side="right", padx=(14, 0))
            gauge.set(hero.gauge.ratio, hero.gauge.value, hero.gauge.label)
        text = tk.Frame(top, background=surface)
        text.pack(side="left", fill="both", expand=True)
        tk.Label(text, text=hero.kicker, font=fonts.for_text(hero.kicker, kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(fill="x")
        head = tk.Frame(text, background=surface)
        head.pack(fill="x", pady=(2, 0))
        tk.Label(head, text=hero.title, font=fonts.sans(kit.PX_HERO, bold=True),
                 foreground=dual.color("ink"), background=surface, anchor="w").pack(side="left")
        if hero.chip:
            kit.Chip(head, hero.chip, tone=hero.chip_tone, ground=surface,
                     px=kit.PX_MICRO, height=19).pack(side="left", padx=(10, 0))
        body_label = tk.Label(text, text=hero.body, font=fonts.sans(kit.PX_SMALL),
                              foreground=dual.color("ink2"), background=surface, anchor="w",
                              justify="left")
        body_label.pack(fill="x", pady=(4, 0))
        runtime = tk.Frame(text, background=surface)
        runtime.pack(fill="x", pady=(3, 0))
        tk.Label(runtime, text=hero.runtime_label, font=fonts.sans(kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=surface).pack(side="left")
        tk.Label(runtime, text=hero.runtime,
                 font=fonts.for_text(hero.runtime, kit.PX_BODY, bold=True),
                 foreground=dual.color("ink"), background=surface).pack(side="left", padx=(6, 0))
        # 说明文字按卡片实际宽度换行（窗口变宽/变窄都不截断）
        def _fit(_event: Any = None) -> None:
            try:
                width = max(180, text.winfo_width() - 8)
            except Exception:                          # pragma: no cover - 控件已销毁
                return
            try:
                body_label.configure(wraplength=width)
            except Exception:                          # pragma: no cover
                pass

        card.bind("<Configure>", _fit, add="+")
        if hero.metrics:
            metrics = tk.Frame(body, background=surface)
            metrics.pack(fill="x", pady=(7, 0))
            for index, metric in enumerate(hero.metrics):
                metrics.columnconfigure(index, weight=1, uniform="metric")
                cell = kit.StatCell(metrics, label=metric.label, value=metric.value,
                                    unit=metric.unit, note=metric.note, ground=surface,
                                    value_px=kit.PX_NUM_L, align="w")
                cell.grid(row=0, column=index, sticky="nsew", padx=(0, 10))
        if hero.actions or hero.hint:
            foot = tk.Frame(body, background=surface)
            # B6：底部留 2px，别让 1px 描边贴着卡片下缘/下一行
            foot.pack(fill="x", pady=(7, 2))
            if hero.hint:
                # B6：提示先 pack（side=right）并限宽换行 —— 旧实现里它排在按钮后面，
                # 剩余宽度不够时被内容画布右缘裁掉（「已授权 1 个客户端」就是这样丢字的）。
                tk.Label(foot, text=hero.hint, font=fonts.sans(kit.PX_MICRO),
                         foreground=dual.color("muted_text"), background=surface,
                         anchor="e", justify="right", wraplength=240).pack(
                             side="right", padx=(10, 0))
            for action in hero.actions:
                self._render_action(foot, action, ground=surface)

    def _render_chart(self, parent: Any, chart: Chart) -> None:
        """贡献节奏柱图：没有时序就只写一句实话，不画假柱子。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        surface = dual.color("surface")
        card = kit.RoundedCard(parent, ground=dual.color("bg"), pad=(14, 10))
        card.pack(fill="x", pady=(0, 10))
        body = card.body
        head = tk.Frame(body, background=surface)
        head.pack(fill="x")
        left = tk.Frame(head, background=surface)
        left.pack(side="left")
        tk.Label(left, text=chart.kicker or "CHART",
                 font=fonts.for_text(chart.kicker or "CHART", kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(fill="x")
        tk.Label(left, text=chart.title, font=fonts.sans(kit.PX_CARD, bold=True),
                 foreground=dual.color("ink"), background=surface, anchor="w").pack(fill="x")
        if chart.caption:
            tk.Label(head, text=chart.caption, font=fonts.sans(kit.PX_MICRO),
                     foreground=dual.color("muted_text"), background=surface).pack(
                         side="right", pady=(10, 0))
        if chart.empty:
            tk.Label(body, text=chart.empty, font=fonts.sans(kit.PX_SMALL),
                     foreground=dual.color("amber_text"), background=surface, anchor="w",
                     justify="left", wraplength=520).pack(anchor="w", pady=(12, 4))
            return
        bars = kit.Bars(body, bars=max(1, len(chart.values)), height=78, gap=4, ground=surface)
        bars.pack(fill="x", pady=(8, 0))
        bars.set(chart.values, axis=chart.labels)
        foot = tk.Frame(body, background=surface)
        foot.pack(fill="x", pady=(2, 0))
        tk.Label(foot, text=chart.peak, font=fonts.for_text(chart.peak, kit.PX_MICRO),
                 foreground=dual.color("mint_text"), background=surface).pack(side="left")
        tk.Label(foot, text=chart.foot, font=fonts.sans(kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=surface).pack(side="right")

    def _render_rail_card(self, parent: Any, card: RailCard) -> None:
        """右栏卡：SETUP（只读字段 + 跳转）/ TODAY（四宫格）/ THROTTLE（规则 + 开关）。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        surface = dual.color("surface")
        box = kit.RoundedCard(parent, ground=dual.color("bg"), pad=(14, 10))
        box.pack(fill="x", pady=(0, 10))
        body = box.body
        head = tk.Frame(body, background=surface)
        head.pack(fill="x")
        tk.Label(head, text=card.kicker, font=fonts.for_text(card.kicker, kit.PX_MICRO),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(side="left")
        if card.note:
            tk.Label(head, text=card.note,
                     font=fonts.for_text(card.note, kit.PX_MICRO, bold=True),
                     foreground=dual.color("mint_text"), background=surface).pack(side="right")
        title = tk.Frame(body, background=surface)
        title.pack(fill="x", pady=(2, 0))
        tk.Label(title, text=card.title, font=fonts.sans(kit.PX_CARD, bold=True),
                 foreground=dual.color("ink"), background=surface, anchor="w").pack(side="left")
        if card.badge:
            kit.Chip(title, card.badge, tone=card.badge_tone, ground=surface,
                     px=kit.PX_MICRO, height=18).pack(side="right")
        if card.kind == "stats" and card.stats:
            grid = tk.Frame(body, background=surface)
            grid.pack(fill="x", pady=(6, 0))
            for index, stat in enumerate(card.stats):
                row, column = divmod(index, 2)
                grid.columnconfigure(column, weight=1, uniform="today")
                kit.StatCell(grid, label=stat.label, value=stat.value, unit=stat.unit,
                             note=stat.note, ground=surface, value_px=kit.PX_NUM_L,
                             mono_value=True).grid(row=row, column=column, sticky="nsew",
                                                  padx=(0, 10), pady=(0, 2))
        if card.kind == "setup" and card.fields:
            form = tk.Frame(body, background=surface)
            form.pack(fill="x", pady=(8, 0))
            for field in card.fields:
                line = tk.Frame(form, background=surface)
                line.pack(fill="x", pady=(0, 5))
                tk.Label(line, text=field.label, font=fonts.sans(kit.PX_MICRO),
                         foreground=dual.color("muted_text"), background=surface,
                         anchor="w", width=12).pack(side="left")
                tk.Label(line, text=field.value,
                         font=fonts.for_text(field.value, kit.PX_SMALL),
                         foreground=dual.color("ink"), background=surface,
                         anchor="w").pack(side="left", fill="x", expand=True)
        if card.kind == "throttle":
            box_rows = tk.Frame(body, background=surface)
            box_rows.pack(fill="x", pady=(8, 0))
            for label, value, tone in card.rows:
                line = tk.Frame(box_rows, background=surface)
                line.pack(fill="x", pady=(0, 2))
                tk.Label(line, text=label, font=fonts.sans(kit.PX_MICRO),
                         foreground=dual.color("muted_text"), background=surface,
                         anchor="w").pack(side="left")
                colour = {"mint": "mint_text", "amber": "amber_text", "red": "red_text",
                          "muted": "muted_text"}.get(tone, "ink")
                tk.Label(line, text=value, font=fonts.for_text(value, kit.PX_SMALL),
                         foreground=dual.color(colour), background=surface).pack(side="right")
            if card.toggle is not None:
                line = tk.Frame(box_rows, background=surface)
                line.pack(fill="x", pady=(2, 0))
                tk.Label(line, text=card.toggle[0], font=fonts.sans(kit.PX_MICRO),
                         foreground=dual.color("muted_text"), background=surface).pack(side="left")
                # 开关只读：点它等于「去让路页改」，这里不假装能改（无对应后端动作）
                kit.Switch(line, on=bool(card.toggle[1]), ground=surface,
                           command=lambda _on: self.show_view("yield")).pack(
                               side="right")
        if card.progress is not None:
            # 进度轨（设计 G-05）：一行标签 + 6px 圆角轨，比例直接来自服务端用量
            label, ratio, value_text = card.progress
            head_row = tk.Frame(body, background=surface)
            head_row.pack(fill="x", pady=(4, 0))
            tk.Label(head_row, text=label, font=fonts.sans(kit.PX_MICRO),
                     foreground=dual.color("muted_text"), background=surface).pack(side="left")
            tk.Label(head_row, text=value_text,
                     font=fonts.for_text(value_text, kit.PX_MICRO),
                     foreground=dual.color("muted_text"), background=surface).pack(side="right")
            track = tk.Canvas(body, width=1, height=6, background=surface, bd=0,
                              highlightthickness=0, takefocus=0)
            track.pack(fill="x", pady=(3, 0))

            def _draw_track(_event: Any = None, canvas: Any = track,
                            share: float = float(ratio or 0.0)) -> None:
                width = max(1, int(canvas.winfo_width()))
                canvas.delete("all")
                canvas.create_polygon(
                    self._geom.round_rect_points(0, 1, width, 5, 2), smooth=True,
                    splinesteps=6, fill=dual.chart("ring_track"), outline="")
                filled = max(4, int((width - 2) * max(0.0, min(1.0, share))))
                canvas.create_polygon(
                    self._geom.round_rect_points(1, 1, 1 + filled, 5, 2), smooth=True,
                    splinesteps=6, fill=dual.chart("bar_peak"), outline="")

            track.bind("<Configure>", _draw_track)
        foot = tk.Frame(body, background=surface)
        foot.pack(fill="x", pady=(7, 0))
        for action in card.actions:
            self._render_action(foot, action, ground=surface)
        if card.foot:
            tk.Label(body, text=card.foot, font=fonts.sans(kit.PX_MICRO),
                     foreground=dual.color("muted_text"), background=surface, anchor="w",
                     justify="left", wraplength=300).pack(anchor="w", pady=(5, 0))

    # ── 其余 12 视图：通用卡片 + 表格 + 字段
    def _render_generic(self, view: View) -> None:
        tk = self._tk
        if view.cards:
            grid = tk.Frame(self._body, background=dual.color("bg"), bd=0, highlightthickness=0)
            grid.pack(fill="x", pady=(0, 12))
            per_row = self._cards_per_row(len(view.cards))
            for index, card in enumerate(view.cards):
                self._render_card(grid, card, index, per_row=per_row)
            for column in range(per_row):
                grid.columnconfigure(column, weight=1, uniform="card")
        # 有表单的页：卡片 → 字段 → 动作（保存）→ 表格；没有表单的页：卡片 → 动作 → 表格
        if view.fields:
            self._render_fields(view.fields, view.key)
        if view.actions:
            bar = tk.Frame(self._body, background=dual.color("bg"))
            bar.pack(fill="x", pady=(0, 12))
            for action in view.actions:
                self._render_action(bar, action)
        if view.table is not None:
            self._render_table(view.table)

    def _cards_per_row(self, count: int = 0) -> int:
        """宽窗 4 张、中宽 2 张、窄窗 1 张（设计是四宫格；不许再把第三张挤出窗口）。

        张数不能整除时优先换列数（6 张 → 3 列 2 行），别让最后一行孤零零挂两张。
        """
        try:
            width = self._logical_width()
        except Exception:                              # pragma: no cover - 无窗口
            width = 1440
        try:
            limit = 4 if width <= 1 else self._geom.cards_per_row(width)
            return self._geom.card_columns(count, limit)
        except Exception:                              # pragma: no cover - 还没建壳
            return 4

    def _render_card(self, parent: Any, card: Card, index: int, *, per_row: int = 4) -> None:
        """指标卡：圆角 + 1px 描边 + 白底，取值按 tone 上色，文字按卡片实宽换行。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        surface = dual.color("surface")
        row, column = divmod(index, per_row)
        box = kit.RoundedCard(parent, ground=dual.color("bg"), pad=(14, 11))
        box.grid(row=row, column=column, sticky="nsew", padx=(0, 10), pady=(0, 10))
        body = box.body
        tk.Label(body, text=card.title, font=fonts.sans(kit.PX_CARD, bold=True),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(fill="x")
        tone = {"mint": "mint_text", "amber": "amber_text", "red": "red_text",
                "muted": "muted_text"}.get(card.tone, "ink")
        value = tk.Label(body, text=card.value, font=fonts.sans(kit.PX_NUM_L, bold=True),
                         foreground=dual.color(tone), background=surface, anchor="w",
                         justify="left")
        value.pack(fill="x", pady=(3, 0))
        meta = None
        if card.meta:
            meta = tk.Label(body, text=card.meta, font=fonts.sans(kit.PX_MICRO),
                            foreground=dual.color("muted_text"), background=surface,
                            anchor="w", justify="left")
            meta.pack(fill="x", pady=(3, 0))

        def _fit(_event: Any = None) -> None:
            try:
                width = max(90, box.winfo_width() - 30)
            except Exception:                          # pragma: no cover - 控件已销毁
                return
            for widget in (value, meta):
                if widget is None:
                    continue
                try:
                    widget.configure(wraplength=width)
                except Exception:                  # pragma: no cover
                    pass

        box.bind("<Configure>", _fit, add="+")

    def _ttk_family(self) -> str:
        from ..gui.theme import FONT
        return FONT[0]

    def _render_action(self, parent: Any, action: Action, *,
                       ground: str | None = None) -> None:
        """动作按钮：主/次两档；置灰时旁边写原因（不隐藏，C9 既有行为）。"""
        kit, tk = self._kit, self._tk
        base = ground or dual.color("bg")
        target = (lambda name=action.nav: self.show_view(name)) if action.nav else \
            (lambda key=action.key: self._on_action(key))
        button = kit.Button(parent, action.label, target,
                            kind="primary" if action.primary else "ghost", ground=base,
                            px=kit.PX_SMALL, height=29, pad=12, enabled=action.enabled)
        button.pack(side="left", padx=(0, 8))
        if not action.enabled and action.reason:
            tk.Label(parent, text=action.reason, font=self._fonts.sans(kit.PX_MICRO),
                     foreground=dual.color("muted_text"), background=base).pack(
                         side="left", padx=(0, 10))

    def _render_table(self, table: Table, parent: Any = None,
                      max_rows: int = 12) -> None:
        """表格：保留 ttk.Treeview（``_tree`` / ``_row_cells`` 是既有契约），只换外观。"""
        self._table = table
        narrow = self._is_narrow()
        tk, ttk = self._tk, self._ttk
        kit = self._kit
        target = parent if parent is not None else getattr(self, "_body", self._content)
        card = kit.RoundedCard(target, ground=dual.color("bg"), pad=(14, 11))
        card.pack(fill="both", expand=True, pady=(0, 10))
        body = card.body
        tree = ttk.Treeview(body, columns=list(table.columns), show="headings",
                            style="Kit.Treeview",
                            height=min(max_rows, max(3, len(table.rows) + 1)),
                            selectmode="browse")
        for column in table.columns:
            tree.heading(column, text=column, anchor="w")
            tree.column(column, anchor="w", width=120, stretch=True)
        tree.tag_configure("disabled", foreground=dual.color("muted_text"))
        tag_colors = {"mint": dual.color("mint_text"), "amber": dual.color("amber_text"),
                      "red": dual.color("red_text"), "muted": dual.color("muted_text"),
                      "default": dual.color("ink")}
        for name, color in tag_colors.items():
            tree.tag_configure("tone-" + name, foreground=color)
        self._row_notes = {}
        self._row_cells = {}
        used_iids: set[str] = set()
        for row in table.rows:
            tags = ["tone-" + (row.tone or "default")]
            if row.disabled:
                tags.append("disabled")          # 标灰优先于语义色
            iid = unique_iid(row.key or "", used_iids)
            if iid:
                used_iids.add(iid)
            values = row.cells
            if narrow and getattr(row, "narrow", ()) and len(row.narrow) == len(row.cells):
                values = row.narrow              # 窄窗用紧凑文案，详情抽屉仍看完整字段
            tree.insert("", "end", iid=iid or None, values=values, tags=tuple(tags))
            self._row_cells[iid or str(len(self._row_cells))] = row.cells
            if row.note:
                self._row_notes[iid] = row.note
        tree.pack(fill="both", expand=True)
        # 列宽跟着可视宽度走：窄窗（E10 口径 390）下列不会被挤出可视区，
        # 也不会因为「固定 124px × N」而溢出到看不见的地方（Treeview 没有横向滚动条）。
        self._fit_columns(tree)
        tree.bind("<Configure>", lambda _event, view=tree: self._fit_columns(view), add="+")
        if not table.rows:
            tk.Label(body, text=table.empty, font=self._fonts.sans(kit.PX_SMALL),
                     foreground=dual.color("muted_text"), background=dual.color("surface"),
                     anchor="w").pack(anchor="w", pady=(10, 2))
        # 详情抽屉：选中一行就在表格下面展开字段（键盘 ↑↓ 也生效；本轮不改抽屉形态）
        self._detail = tk.Frame(body, background=dual.color("surface3"), bd=0,
                                highlightthickness=0)
        self._detail_columns = tuple(table.columns)
        self._tree = tree
        if table.rows:
            tree.bind("<<TreeviewSelect>>", self._on_row_select)
            tk.Label(body, text="选中一行看详情（↑↓ 或点击）",
                     font=self._fonts.sans(kit.PX_MICRO),
                     foreground=dual.color("muted_text"),
                     background=dual.color("surface")).pack(anchor="w", pady=(6, 0))
        if table.note:
            self._wrap(tk.Label(body, text=table.note, font=self._fonts.sans(kit.PX_MICRO),
                              foreground=dual.color("muted_text"),
                              background=dual.color("surface"), anchor="w",
                              justify="left", wraplength=self._wrap_hint())).pack(anchor="w", pady=(4, 0))

    def _fit_columns(self, tree: Any) -> None:
        """列宽按可视宽度分配，保证所有可见列都在首屏内（Treeview 没有横向滚动条）。

        权重取「表头长度」与「该列最长单元格（截到 24 字）」的较大者：
        「状态」这种装长文案的列拿到更宽的份额，短列不浪费。
        窄窗下先按 ``Table.narrow_hide`` 让位几列（数据仍在详情抽屉里）。
        """
        table = getattr(self, "_table", None)
        all_columns = list(tree["columns"])
        narrow = self._is_narrow()
        hidden = tuple(getattr(table, "narrow_hide", ())) if narrow else ()
        shown = [column for column in all_columns if column not in hidden]
        if not shown:
            return
        tree.configure(displaycolumns=shown)
        try:
            available = int(tree.winfo_width())
        except Exception:                            # pragma: no cover - 未映射窗口
            return
        if available <= 1:                           # 还没布局：用请求宽度兜底
            available = int(tree.winfo_reqwidth())
        rows = getattr(table, "rows", ()) or ()
        weights: list[float] = []
        for column in shown:
            # 单元格按「原始列序」取，别用 shown 的下标（隐藏列会让下标错位）
            source_index = all_columns.index(column)
            longest = len(column) + 1
            for row in rows:
                if source_index < len(row.cells):
                    longest = max(longest, min(24, len(str(row.cells[source_index]))))
            weights.append(float(max(4, longest)))
        total = sum(weights) or 1.0
        floor = 40
        headers = dict(getattr(table, "narrow_headers", ()) or ())
        # 末列右对齐（设计 G-07）+ 数字列右对齐（B7）：数字/结论靠右，整列一条线
        numeric = numeric_columns(all_columns, rows)
        for index, column in enumerate(shown):
            text = headers.get(column, column) if narrow else column
            anchor = "e" if (index == len(shown) - 1 or column in numeric) else "w"
            tree.heading(column, text=text, anchor=anchor)
        # 按权重分配，舍入误差给最宽的一列（别让末列只剩「剩余」而被挤扁）
        widths: list[int] = [max(floor, int(available * weight / total)) for weight in weights]
        slack = available - sum(widths)
        if slack:
            widest = max(range(len(widths)), key=lambda i: widths[i])
            widths[widest] = max(floor, widths[widest] + slack)
        # 短列也要放得下表头 + 典型值（「校验」列被压到 43px 时只剩「无副」两个字）
        for index, column in enumerate(shown):
            header = headers.get(column, column) if narrow else column
            source_index = all_columns.index(column)
            chars = len(header)
            for row in rows:
                cells = (getattr(row, "narrow", ()) if narrow and
                         len(getattr(row, "narrow", ())) == len(row.cells) else row.cells)
                if source_index < len(cells):
                    chars = max(chars, min(7, len(str(cells[source_index]))))
            need = max(floor, chars * 13 + 12)
            if widths[index] < need:
                gap = need - widths[index]
                donor = max(range(len(widths)), key=lambda i: widths[i])
                if donor != index and widths[donor] - gap >= floor:
                    widths[donor] -= gap
                    widths[index] = need
        for index, (column, width) in enumerate(zip(shown, widths)):
            tree.column(column, width=width, stretch=True,
                        anchor="e" if (index == len(shown) - 1 or column in numeric) else "w")
        self._column_widths = tuple(int(tree.column(column, "width")) for column in shown)
        self._column_viewport = available
        self._hidden_columns = tuple(hidden)

    def _on_row_select(self, _event=None) -> None:
        tk = self._tk                                    # note 行要用；此前漏了，带备注的收据一选就 NameError
        tree = getattr(self, "_tree", None)
        detail = getattr(self, "_detail", None)
        if tree is None or detail is None:
            return
        selection = tree.selection()
        for child in detail.winfo_children():
            child.destroy()
        if not selection:
            detail.pack_forget()
            return
        # 详情抽屉永远用**完整**单元格（窄窗表格里可能是紧凑文案）
        full = getattr(self, "_row_cells", {}) or {}
        values = full.get(str(selection[0])) or tree.item(selection[0], "values")
        try:
            self._controller.select_row(self._controller.view, values[0] if values else "")
        except Exception:
            pass
        detail.pack(fill="x", pady=(8, 2), padx=0)
        surface = dual.color("surface3")
        fonts = self._fonts
        for column, value in zip(self._detail_columns, values):
            line = self._tk.Frame(detail, background=surface)
            line.pack(fill="x", pady=1, padx=10)
            self._tk.Label(line, text=column, font=fonts.sans(self._kit.PX_MICRO),
                           foreground=dual.color("muted_text"), background=surface,
                           width=12, anchor="w").pack(side="left")
            self._tk.Label(line, text=str(value),
                           font=fonts.for_text(str(value), self._kit.PX_SMALL),
                           foreground=dual.color("ink"), background=surface,
                           anchor="w").pack(side="left")
        note = self._row_notes.get(selection[0], "")
        if note:
            self._wrap(tk.Label(detail, text="原因：" + note,
                                    font=fonts.sans(self._kit.PX_MICRO),
                                    foreground=dual.color("amber_text"), background=surface,
                                    anchor="w", justify="left",
                                    wraplength=self._wrap_hint())).pack(anchor="w", padx=10, pady=(4, 6))

    def _render_fields(self, fields: Sequence[Field], view_key: str,
                       parent: Any = None) -> None:
        """字段区：宽窗两列、窄窗一列；可编辑的进输入框，只读的用等宽值。"""
        kit, tk, ttk = self._kit, self._tk, self._ttk
        fonts = self._fonts
        surface = dual.color("surface")
        target = parent if parent is not None else getattr(self, "_body", self._content)
        card = kit.RoundedCard(target, ground=dual.color("bg"), pad=(16, 14))
        card.pack(fill="x", pady=(0, 10))
        body = card.body
        editable = any(field.editable for field in fields)
        tk.Label(body, text="可编辑字段" if editable else "详情",
                 font=fonts.sans(kit.PX_CARD, bold=True), foreground=dual.color("ink"),
                 background=surface, anchor="w").pack(anchor="w", pady=(0, 10))
        per_row = 1 if (self._is_narrow() or len(fields) <= 2) else 2
        grid = tk.Frame(body, background=surface)
        grid.pack(fill="x")
        for column in range(per_row):
            grid.columnconfigure(column, weight=1, uniform="field")
        for index, field in enumerate(fields):
            row, column = divmod(index, per_row)
            block = tk.Frame(grid, background=surface)
            block.grid(row=row, column=column, sticky="nsew", padx=(0, 18), pady=(0, 10))
            # 标签列用像素 minsize（Tk 的 width= 是按「0 字宽」算的，中文标签会被截）
            block.columnconfigure(0, minsize=104)
            block.columnconfigure(1, weight=1)
            tk.Label(block, text=field.label, font=fonts.sans(kit.PX_MICRO),
                     foreground=dual.color("muted_text"), background=surface,
                     anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 10))
            if field.editable:
                variable = tk.StringVar(value="" if field.secret else field.value)
                entry = ttk.Entry(block, textvariable=variable, style="Kit.TEntry",
                                  width=28 if per_row > 1 else 44)
                if field.secret:
                    entry.configure(show="•")
                entry.grid(row=0, column=1, sticky="ew")
                hint = field.hint or ""
                if field.secret:
                    hint = ("当前 " + field.value + "｜" + hint).strip("｜")
                if hint:
                    self._wrap_to_column(tk.Label(
                        block, text=hint, font=fonts.sans(kit.PX_MICRO),
                        foreground=dual.color("muted_text"), background=surface,
                        anchor="w", justify="left",
                        wraplength=self._wrap_hint())).grid(row=1, column=1, sticky="we",
                                                            pady=(3, 0))
                self._fields[field.key] = field
                self._vars[field.key] = variable
            else:
                # 只读值也要能换行：900 宽单列时端点 URL（http://…/agent）比列宽长，
                # 不换行就被切掉尾部（audit text-clip-w）。短值不受影响。
                value_label = self._wrap_to_column(tk.Label(
                    block, text=field.value,
                    font=fonts.for_text(field.value, kit.PX_SMALL),
                    foreground=dual.color("ink"), background=surface,
                    anchor="w", justify="left",
                    wraplength=self._wrap_hint()))
                value_label.grid(row=0, column=1, sticky="we")
                if field.hint:
                    self._wrap_to_column(tk.Label(
                        block, text=field.hint, font=fonts.sans(kit.PX_MICRO),
                        foreground=dual.color("muted_text"), background=surface,
                        anchor="w", justify="left",
                        wraplength=self._wrap_hint())).grid(row=1, column=1, sticky="we",
                                                            pady=(3, 0))

    # ── 动作
    def _on_action(self, key: str) -> None:
        for name, variable in list(self._vars.items()):
            try:
                self._controller.set_draft(name, variable.get())
            except Exception:                            # 变量随旧视图销毁：跳过，不让动作静默不执行
                continue
        result = self._controller.action(key, confirm=self._confirm)
        if result.effect == "copy":
            self._copy(result.payload)
        elif result.effect == "open_dir":
            self._open_dir(result.payload)
        elif result.effect == "open_url":
            self._open_url(result.payload)
        elif result.effect == "goto" and result.payload:
            self.show_view(str(result.payload))       # 例如「共享 Token Plan」→「接入配置」
        elif result.effect in ("refresh", "access", "bench", "control", "reload"):
            self._render(self._controller.current())
        self._update_status()
        if result.effect in ("save", "access", "reload", "control", "export", "verify") and result.message:
            self._toast(result.message, ok=result.ok)

    def _copy(self, text: str) -> None:
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
        except Exception:
            pass

    def _open_dir(self, path: str) -> None:
        """只做「显示路径」：不自动调用系统文件管理器（避免在无桌面环境弹错）。"""
        if path:
            self._toast("日志目录：" + path, ok=True)

    def _ask_bind_code(self) -> str | None:
        """一次性绑定码输入框（模态；Esc/取消返回 None，不执行任何命令）。"""
        from tkinter import simpledialog
        from .. import facts
        message = ("在网页端 → 账户/节点 → 生成绑定码，然后粘贴到这里。\n\n"
                   + facts.WEB_CONSOLE_URL)
        value = simpledialog.askstring("DJB / 绑定网页端", message, parent=self._root)
        return str(value).strip() if value else None

    def _open_url(self, url: str) -> None:
        """用系统浏览器打开网页端；打不开就把地址留在状态栏（不静默失败）。"""
        if not url:
            return
        try:
            import webbrowser
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
        self._toast(("已在浏览器打开：" if opened else "浏览器未能自动打开，请手动访问：") + url,
                    ok=opened)

    def _toast(self, message: str, *, ok: bool = True) -> None:
        self._status.configure(text=("✓ " if ok else "! ") + message)

    def _confirm(self, message: str) -> bool:
        """二次确认（模态，键盘可达：Esc 取消，Enter 确认）。"""
        tk, ttk = self._tk, self._ttk
        window = tk.Toplevel(self._root)
        dual.apply(window, ttk, self._theme_name)
        self._apply_kit_skin()                     # 同上：option 数据库是解释器级的，别被改回 11pt
        window.title("DJB / 确认本机控制")
        window.geometry("560x260")
        choice = [False]
        body = ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="! 影响确认 / 可取消（Esc）", style="Warning.TLabel").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=500).pack(fill="x", pady=18)
        bar = ttk.Frame(body)
        bar.pack(fill="x", side="bottom")

        def accept() -> None:
            choice[0] = True
            window.destroy()

        ttk.Button(bar, text="确认执行并回读", style="Primary.TButton",
                   command=accept).pack(side="right")
        cancel = ttk.Button(bar, text="取消 / Esc", command=window.destroy)
        cancel.pack(side="left")
        cancel.focus_set()
        window.bind("<Escape>", lambda _e: window.destroy())
        window.bind("<Return>", lambda _e: accept())
        window.grab_set()
        self._root.wait_window(window)
        return choice[0]

    def _on_bus(self, snapshot) -> None:
        """托盘那边改了状态：立刻重画（不重新取数，避免抖动）。"""
        try:
            self._update_status()
        except Exception:
            pass

    # ── 生命周期
    def run(self) -> None:
        self._root.mainloop()

    def stop(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        try:
            self._root.destroy()
        except Exception:
            pass
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────── 演示 / 入口
def snapshot_section(snapshot: Any, name: str) -> Mapping[str, Any]:
    """取一个数据域（不是 Mapping 就当空，调用方各自写「暂不可用」）。"""
    value = snapshot.get(name)
    return value if isinstance(value, Mapping) else {}


def is_number(value: Any) -> bool:
    """真·数字（``bool`` 不算；``None``/空串/非数字 → False）。"""
    if value is None or isinstance(value, bool) or value == "":
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def short_clock(value: Any) -> str:
    """「数据截至」用的时刻：解析成功给 ``HH:MM:SS``，解析不了给「—」（绝不编时间）。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).strftime("%H:%M:%S")
    except ValueError:
        return text[11:19] if len(text) >= 19 else "—"


def demo_snapshot() -> Snapshot:
    """确定性的演示数据（截图取证用；不连网络、不读用户目录）。"""
    return FakeDataSource(_DEMO_SECTIONS, source="demo").snapshot()


_DEMO_SECTIONS = {
        "status": {"state": "online", "accepting": True, "daemon_running": True,
                   "balance": "128.40", "today_earnings": "6.12", "reason": None,
                   "inflight": [{"id": "t-8821", "state": "running", "elapsed_s": 12}]},
        "bench": {"cpu_count": 8, "cpu_load_1m": 0.42, "mem_total_bytes": 32 * 1024 ** 3,
                  "mem_available_bytes": 21 * 1024 ** 3, "disk_total_bytes": 2 * 1024 ** 4,
                  "disk_free_bytes": 1_400 * 1024 ** 3, "platform": "Linux", "machine": "x86_64",
                  "python": "3.12.15",
                  "checks": [{"id": "system", "level": "ok", "summary": "系统满足最低要求",
                              "next_step": "无需处理"},
                             {"id": "acceleration", "level": "warn",
                              "summary": "未检测到 GPU 加速",
                              "next_step": "CPU 模式可运行，收益按 CPU 单价计"}]},
        # 演示数据里的 GPU：与 parse_nvidia_smi 的字段形状**完全一致**（不新增字段），
        # 真机没有 GPU 时仍走 present=False 那条真话路径（见 test_gpu_absent_says_so）。
        "gpu": {"present": True,
                "devices": [{"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4070",
                             "memory_total_mb": 12282, "memory_used_mb": 9830,
                             "temperature_c": 62, "utilization_pct": 62}],
                "note": "nvidia-smi"},
        "models": {"catalog": [
            {"id": "qwen2.5-7b-instruct", "context": 32768, "offered": True},
            {"id": "llama-3.1-8b-instruct", "context": 131072, "offered": True},
            {"id": "mixtral-8x7b", "context": 32768, "offered": False,
             "reason": "需要 ≥24GB 显存，本机不满足"}],
            "local": [{"id": "qwen2.5-7b-instruct", "state": "applied"}],
            "online": True, "note": ""},
        "tasks": {"rows": [
            {"id": "t-8821", "state": "running", "elapsed_s": 12, "tokens": 1536},
            {"id": "t-8820", "state": "done", "elapsed_s": 43, "tokens": 8192,
             "receipt_id": "rcpt-77a1"}], "origin": "local", "note": "云端任务列表暂不可用"},
        # 演示收据：跨 24 小时铺开（裁决 R3）——节奏图按 created_at 逐小时聚合，
        # 所以演示数据必须**有时间跨度**，否则实拍里只有一根柱子。
        # 时间窗以快照 fetched_at（2026-11-01T12:00+0800）为锚点往回 24 小时，
        # 故最早一条落在 10-31T05:32Z（=13:32+08），最晚 11-01T04:31Z（=12:31+08）。
        # token 数故意有峰谷，形状才像真实工作量；全部为演示值，窗口上有「演示数据」标注。
        "receipts": {"rows": [
            {"receipt_id": "rcpt-7781", "created_at": "2026-10-31T05:32:00Z", "tokens": 512,
             "state": "accepted"},
            {"receipt_id": "rcpt-7782", "created_at": "2026-10-31T06:10:00Z", "tokens": 1024,
             "state": "accepted"},
            {"receipt_id": "rcpt-7783", "created_at": "2026-10-31T07:41:00Z", "tokens": 2048,
             "state": "accepted"},
            {"receipt_id": "rcpt-7784", "created_at": "2026-10-31T09:05:00Z", "tokens": 4096,
             "state": "accepted"},
            {"receipt_id": "rcpt-7785", "created_at": "2026-10-31T10:22:00Z", "tokens": 8192,
             "state": "accepted"},
            {"receipt_id": "rcpt-7786", "created_at": "2026-10-31T10:58:00Z", "tokens": 1024,
             "state": "accepted"},
            {"receipt_id": "rcpt-7787", "created_at": "2026-10-31T12:33:00Z", "tokens": 2048,
             "state": "accepted"},
            {"receipt_id": "rcpt-7788", "created_at": "2026-10-31T13:47:00Z", "tokens": 6144,
             "state": "rejected", "rejected_code": "46001"},
            {"receipt_id": "rcpt-7789", "created_at": "2026-10-31T14:02:00Z", "tokens": 8192,
             "state": "accepted"},
            {"receipt_id": "rcpt-778a", "created_at": "2026-10-31T15:19:00Z", "tokens": 4096,
             "state": "accepted"},
            {"receipt_id": "rcpt-778b", "created_at": "2026-10-31T16:40:00Z", "tokens": 2048,
             "state": "accepted"},
            {"receipt_id": "rcpt-778c", "created_at": "2026-10-31T17:11:00Z", "tokens": 512,
             "state": "accepted"},
            {"receipt_id": "rcpt-778d", "created_at": "2026-10-31T18:26:00Z", "tokens": 1024,
             "state": "accepted"},
            {"receipt_id": "rcpt-778e", "created_at": "2026-10-31T19:33:00Z", "tokens": 3072,
             "state": "accepted"},
            {"receipt_id": "rcpt-778f", "created_at": "2026-10-31T20:45:00Z", "tokens": 12288,
             "state": "accepted"},
            {"receipt_id": "rcpt-7790", "created_at": "2026-10-31T21:07:00Z", "tokens": 2048,
             "state": "accepted"},
            {"receipt_id": "rcpt-7791", "created_at": "2026-10-31T22:50:00Z", "tokens": 1024,
             "state": "accepted"},
            {"receipt_id": "rcpt-7792", "created_at": "2026-10-31T23:15:00Z", "tokens": 512,
             "state": "accepted"},
            {"receipt_id": "rcpt-7793", "created_at": "2026-11-01T00:38:00Z", "tokens": 2048,
             "state": "accepted"},
            {"receipt_id": "rcpt-7794", "created_at": "2026-11-01T01:52:00Z", "tokens": 4096,
             "state": "accepted"},
            {"receipt_id": "rcpt-77a0", "created_at": "2026-11-01T02:44:00Z", "tokens": 2048,
             "state": "recorded"},
            {"receipt_id": "rcpt-77a1", "created_at": "2026-11-01T03:12:00Z", "tokens": 8192,
             "state": "accepted"},
            {"receipt_id": "rcpt-77a2", "created_at": "2026-11-01T04:31:00Z", "tokens": 4096,
             "state": "accepted"}], "origin": "local", "note": ""},
        "earnings": {"balance": "128.40", "today": "6.12", "currency": "USDT",
                     "pending": "1.80", "settled": "4.32",
                     "payouts": [{"id": "po-31", "amount": "4.32", "currency": "USDT",
                                  "status": "settled", "created_at": "2026-10-31"}],
                     "online": True, "note": ""},
        "agent": {"upstream": {"openai": "http://127.0.0.1:1717/v1",
                               "anthropic": "http://127.0.0.1:1717"},
                  "downlink": {"url": "http://127.0.0.1:1717/agent", "token": "djb_sc_…9f2c"},
                  "note": "下行令牌只读展示；轮换请到服务端控制台。"},
        "access": {"valid": True, "saved": True,
                   "config": {"node_id": "node-7c31", "node_secret": "djb_ns_…4e11",
                              "api_key": "djb_sk_…abcd", "price_mode": "auto",
                              "price_mode_label": "跟随平台建议价",
                              "price_note": "—", "usdt_address": "TQn9Xa…7f3d",
                              "openai_base_url": "http://127.0.0.1:1717/v1",
                              "anthropic_base_url": "http://127.0.0.1:1717",
                              "agent_downlink_url": "http://127.0.0.1:1717/agent",
                              "scoped_token": "djb_sc_…9f2c"},
                   "valid": True, "binding": {"state": "bound", "node_id": "node-7c31"},
                   "note": "", "path": "~/.djb/desktop/access-config.json"},
        # 共享 Token Plan（第 9 项）：演示数据必须给「有上游」的形状，
        # 否则截图里只有空态，客户看不到「我的 Token Plan」长什么样。
        "token_plan": {"upstreams": [
            {"id": "up_moonshot_01", "access_type": "A1", "state": "verified",
             "enabled": True, "upstream_kind": "official", "model_count": 3,
             "verified_models": 2, "max_qps": 1.0, "last_probe_at": "2026-11-01T04:30:00Z"},
            {"id": "up_local_agent", "access_type": "A2", "state": "declared",
             "enabled": True, "upstream_kind": "third_party_relay", "model_count": 1,
             "verified_models": 0, "max_qps": 0.0, "last_probe_at": None}],
            "limits": {"daily_tokens": 200000, "weekly_tokens": 0, "monthly_tokens": 0,
                       "concurrent": 4},
            "usage": [{"name": "daily", "limit": 200000, "used": 41892, "remaining": 158108,
                       "window_seconds": 86400}],
            "preset": "balanced",
            "node": {"node_id": "node-7c31", "bound": True},
            "note": ""},
        "yield": {"state": "online", "reason": None, "reason_label": None, "accepting": True,
                  "mode": "balanced", "yield_enabled": True},
        "account": {"profile": {"email": "node@example.com", "role": "共享方",
                                "twofa_enabled": True},
                    "security": {"node_id": "node-7c31", "storage": "file"},
                    "note": ""},
        "logs": {"rows": [
            {"ts": "2026-11-01T04:31:02Z", "level": "info", "event": "daemon.started"},
            {"ts": "2026-11-01T04:31:09Z", "level": "info", "event": "channel.claimed"},
            {"ts": "2026-11-01T04:32:44Z", "level": "warning",
             "event": "yield.detected"},
            {"ts": "2026-11-01T04:33:10Z", "level": "error", "event": "upstream.probe_failed"}],
            "path": "~/.djb/logs/djb.log", "levels": ["all", "debug", "info", "warning",
                                                      "error"], "note": ""},
}


def _make_save_path(root_getter):
    """导出用保存对话框：返回用户选的路径；取消/无对话框 → None（绝不自己编路径）。"""
    def ask(default_name: str) -> str | None:
        try:
            from tkinter import filedialog

            root = root_getter()
            path = filedialog.asksaveasfilename(
                parent=root, title="导出收益明细", defaultextension=".csv",
                initialfile=default_name,
                filetypes=(("CSV 文件", "*.csv"), ("所有文件", "*.*")))
        except Exception:
            return None
        return str(path) if path else None
    return ask


def unique_iid(key: str, used: set[str]) -> str:
    """Treeview 的行 id 必须唯一。

    撞 key 时 Tk 抛 ``TclError: Item ... already exists``，而它是从
    ``tree.insert()`` 里抛出来的——一旦冒泡，整个窗口直接死（0.2.3 实拍里
    「日志」页就是这么崩的：同一微秒的两行日志用了同一个 ts 当 key）。
    这里统一去重：``a`` / ``a#2`` / ``a#3``……空 key 交给 Tk 自动编号。
    """
    if not key:
        return ""
    candidate = key
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = "%s#%d" % (key, suffix)
    return candidate


def _make_cli_runner(home):
    """本机命令通道：显式带上 ``DJB_HOME``，避免子进程读到别的 home。"""
    def run(argv):
        import subprocess
        import sys

        env = dict(os.environ)
        env["DJB_HOME"] = str(home)
        command = [sys.executable, "-m", "djb_node", *argv]
        if "--json" not in argv:
            command.append("--json")
        try:
            # 与 datasource._cli_runner 同一条规矩：子进程是我们自己的 CLI，它按 UTF-8
            # 写 stdout（cli.main()._enable_utf8_stdio），这里必须显式按 UTF-8 解，
            # 否则中文 Windows 上按 cp936 解会 UnicodeDecodeError → 动作区报错。
            completed = subprocess.run(command, capture_output=True,
                                       encoding="utf-8", errors="replace", timeout=20,
                                       env=env, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            raise ProviderError("本地命令超时（20s）") from None
        except OSError as exc:
            raise ProviderError("本地命令无法执行（" + type(exc).__name__ + "）") from None
        if completed.returncode != 0:
            lines = [line.strip() for line in (completed.stderr or completed.stdout or "").splitlines()
                     if line.strip() and not line.strip().startswith("details=")]
            detail = lines[0].removeprefix("djb: ") if lines else "退出码 " + str(completed.returncode)
            raise ProviderError(detail[:160])
        try:
            return json.loads(completed.stdout or "{}")
        except ValueError:
            raise ProviderError("本地命令输出不是 JSON") from None
    return run


def _make_reloader(home: str | os.PathLike | None):
    """接 daemon 的热重载：与 `djb daemon reload` **同一条控制命令**。

    返回 ``(状态, 说明)``：成功给新配置的变更摘要，失败给 daemon 的原话（可读原因），
    没接上/旧版本 → 对应状态，绝不谎报「已生效」。
    """
    def reload() -> tuple[str, str]:
        try:
            from ..daemon import ControlClient
            from ..config import Paths
            from ..errors import NotRunningError, UsageError
        except ImportError:                       # pragma: no cover - 防御
            return RELOAD_UNSUPPORTED, ""
        paths = Paths(str(home) if home else os.path.expanduser("~/.djb"))
        try:
            response = ControlClient(paths).call({"cmd": "reload"}, timeout=15.0)
        except NotRunningError:
            return RELOAD_DAEMON_DOWN, ""
        except UsageError:
            # 旧 daemon 不认识 reload：这是「没有热重载能力」，不是保存失败
            return RELOAD_UNSUPPORTED, ""
        except Exception as exc:
            return RELOAD_FAILED, type(exc).__name__
        if response.get("ok") is True:
            changed = response.get("changed") or []
            detail = ("已重载：" + ", ".join(str(item) for item in changed)) if changed \
                else "配置无变化"
            return RELOAD_OK, detail
        return RELOAD_FAILED, str(response.get("error") or "原因未知")
    return reload


def demo_source() -> FakeDataSource:
    """演示数据源（``--demo`` / 截图脚本共用，内容确定）。"""
    return FakeDataSource(_DEMO_SECTIONS, source="demo")


def make_controller(*, home: str | os.PathLike | None = None, demo: bool = False,
                    cloud: Any | None = None, bus: Any | None = None) -> DesktopController:
    """装配：数据源 + 接入配置服务 + 控制通道 + 状态总线。"""
    from ..gui.state import ControlBus
    bus = bus if bus is not None else ControlBus()
    if demo:
        source: Any = demo_source()
    else:
        if cloud is None:
            # 没显式注入就按 ~/.djb 自己装：没登录节点时是 None（视图照常显示「暂不可用」）
            from .nodecloud import NodeCloudClient
            cloud = NodeCloudClient.from_home(home)
        source = LocalDataSource(home=home, cloud=cloud)
    store = FileAccessStore(None if home is None else
                            os.path.join(str(home), ACCESS_FILE))
    access = AccessService(store, reloader=_make_reloader(home))
    return DesktopController(source, access=access, control=LocalControlChannel(home), bus=bus,
                             cli=_make_cli_runner(home) if not demo else None)


def _default_home() -> str:
    """本机数据目录：``$DJB_HOME`` 优先，否则 ``~/.djb``（与 CLI 一致）。"""
    from ..config import resolve_home
    return str(resolve_home())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djb desktop", description="DJB 联脑桌面控制台（12 视图）")
    parser.add_argument("--home", help="本机数据目录（默认 $DJB_HOME 或 ~/.djb）")
    parser.add_argument("--view", default="overview", choices=VIEW_KEYS,
                        help="启动时打开的视图")
    parser.add_argument("--demo", action="store_true", help="用确定性演示数据（截图/培训用）")
    parser.add_argument("--shot", metavar="DIR", help="构造后逐视图截图到目录（需要 ImageMagick import）")
    parser.add_argument("--no-auto", action="store_true",
                        help="关闭首启自举（测试/离线环境：不建目录、不起节点）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    boot_line = ""
    if not args.demo and not args.no_auto:
        # 首启自举（2026-11-06 外部客户实测：装完只看到「云端未接通/本机未初始化」）
        from ..bootstrap import ensure_ready, start_daemon, summary_text
        boot = ensure_ready(home=args.home)
        daemon = start_daemon(home=args.home)
        boot_line = summary_text(boot, daemon)
    controller = make_controller(home=args.home, demo=args.demo)
    try:
        # prefs_home 必须显式给：demo 数据源没有 home，不给就会把主题偏好写到真实 ~/.djb
        app = DesktopApp(controller, on_quit=lambda: None,
                         prefs_home=args.home or _default_home())
    except HeadlessError:
        print(HEADLESS_HINT)
        return EXIT_HEADLESS
    except Exception as exc:                        # tkinter 缺失 / 无显示：一律 3
        print(HEADLESS_HINT + "（" + type(exc).__name__ + "）")
        return EXIT_HEADLESS
    app.show_view(args.view)
    if boot_line:
        # 自举结果写进状态栏：用户看得到"已自动初始化/已修正云端地址/节点已启动"
        try:
            app._toast(boot_line)                         # noqa: SLF001 - 同模块私有状态栏
        except Exception:                                # pragma: no cover - 纯展示
            pass
    if args.shot:
        _shoot(app, args.shot)
        app.stop()
        return EXIT_OK
    app.run()
    return EXIT_OK


def _shoot(app: DesktopApp, directory: str) -> list[str]:
    """逐视图截图（调用外部 ``import``；失败只记录，不影响退出码）。"""
    import subprocess
    os.makedirs(directory, exist_ok=True)
    written: list[str] = []
    for key in VIEW_KEYS:
        app.show_view(key)
        try:
            app._root.update()          # noqa: SLF001 - 截图脚本需要一次完整重绘
            app._root.update_idletasks()
        except Exception:
            pass
        shots = [(os.path.join(directory, "view-" + key + ".png"), None)]
        if getattr(app, "_tree", None) is not None:
            shots.append((os.path.join(directory, "view-" + key + "-detail.png"), "select"))
        for target, mode in shots:
            if mode == "select":
                try:
                    children = app._tree.get_children()   # noqa: SLF001 - 取证脚本
                    if not children:
                        continue
                    app._tree.selection_set(children[0])
                    app._tree.focus(children[0])
                    app._root.update()
                except Exception:
                    continue
            try:
                window_id = str(app._root.winfo_id())
                subprocess.run(["import", "-window", window_id, target], check=False, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.exists(target):
                written.append(target)
    return written


if __name__ == "__main__":                          # pragma: no cover
    sys.exit(main())
