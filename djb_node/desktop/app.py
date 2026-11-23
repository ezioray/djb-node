# SPDX-License-Identifier: Apache-2.0
"""桌面主窗口（Tk）：侧栏 13 页（IA 五组，可回滚四组）+ 内容区 + 状态栏；逻辑全在 controller。

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
import time
from datetime import datetime
from typing import Any, Mapping, Sequence

from .access import ACCESS_FILE, RELOAD_DAEMON_DOWN, RELOAD_FAILED, RELOAD_OK, RELOAD_UNSUPPORTED
from .access import AccessService, FileAccessStore
from .controller import DEFAULT_MESSAGE, DesktopController, LocalControlChannel
from .datasource import SECTIONS, FakeDataSource, LocalDataSource, ProviderError, Snapshot
from ..platform.no_window import subprocess_window_kwargs
from . import theme as dual          # 双主题：色板 + 偏好 + 动效助手
from .a11y import install as install_a11y   # 读屏（MSAA）；**静态 import**，别改成 import_module
from . import ia                     # 第三轮：侧栏/快捷键/下拉的 IA 数据源
from .. import facts                 # WEB_CONSOLE_URL（401 横幅的「打开网页端」）
from . import client_flags            # R2.4 S1/S3/S4：壳层开关（默认关，三字段齐全）
from . import platform_ui
from .views import (VIEW_KEYS, VIEW_TITLES, Action, Card, Chart, Field, Hero,
                    Metric, RailCard, Row, Table, View, page_kicker, with_field_errors)

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

#: 页头眉标：**组名 / 页名** 两级（B3），如 ``运行 / 总览``（2026-11-23 起全中文）。
#:
#: 这是 **0.3.4 口径**的派生（组名来自 ``views.NAV_SECTIONS``），保留给旧调用方与截图基线；
#: 窗口实际用的是实例级 ``self._ia_kickers``（跟着 ``DJB_UI_IA`` 走，回滚时与本表逐项相等）。
PAGE_KICKERS = {key: page_kicker(key) for key in VIEW_KEYS}

#: 这些 effect 由**自己的渲染函数**播报结果（打开浏览器 / 打开目录 / 派发收款提交）。
#:
#: 它们在 :meth:`DesktopApp._on_action` 的通用播报里要跳过，否则「已在浏览器打开」会被
#: 动作自带的一句「网页端：https://…」覆盖掉——那正好抹掉了「打不开」这个关键差别。
SELF_REPORTING_EFFECTS = ("open_url", "open_dir", "payout_submit", "update", "probe")

#: RC15-039 #4：自动刷新的两档间隔（秒）与「空闲」阈值。
#:
#: 走查原话：「要么不刷新，要么一直在后台刷」——所以不是开关二选一，而是**按可见性降频**：
#: 页面可见 10 秒；最小化（或超过 ``AUTO_IDLE_S`` 没有任何输入）降到 60 秒。
#: 不做 0 秒轮询：本机数据源一轮要跑一次 ``djb status``，10 秒已经够实时。
AUTO_REFRESH_VISIBLE_S = 10
AUTO_REFRESH_HIDDEN_S = 60
AUTO_IDLE_S = 60

#: 会回投**字段级错误**的 effect：这类动作的结果要覆盖本页红字状态（含「清空」）。
#:
#: 只有表单动作能改这份状态。其它动作（复制 / 打开目录 / 切页）无权把客户刚看到的红字抹掉，
#: 也不该继承上一份错误——所以名单是**白名单**，不是「除黑名单外都清」。
FORM_EFFECTS = ("access", "payout_submit")

#: D4（2026-11-18 客户报障「点 OK 没反应」）：失败提示的存活时间。
#:
#: 旧实现 ``_toast`` 只把文案写进状态条，下一轮刷新（可见时 10s）就用
#: ``_status_line()`` 覆写 ⇒ 客户还没看到就没了。失败文案现在进 ``_sticky_error``，
#: 由 ``_update_status`` 让位，并一直留到「下一次动作 / 换页 / 本秒数超时」。
STICKY_ERROR_SECONDS = 120

TOP_NAV_KEYS = VIEW_KEYS

# ── IA 接线（第三轮）：侧栏 / 快捷键 / 窄窗下拉 / 页头眉标全部由 ``ia.py`` 驱动
#
# 数据源只有一处：:mod:`djb_node.desktop.ia`。``DJB_UI_IA=legacy`` 回滚到 0.3.4 的 4 组 13 页
# （``ia.IA_SECTIONS_LEGACY`` 与 ``views.NAV_SECTIONS`` 逐项相等，测试钉住）；未设/未识别 = 方案 A。
# 本节所有函数都是**纯数据**（不碰 Tk），canonical 断言在 ``tests/test_desktop_ia_wiring.py``。


def ia_state(raw: object = None, *, env: Mapping[str, str] | None = None) -> ia.IaDecision:
    """裁决当前 IA 模式：``DJB_UI_IA=legacy`` 回 0.3.4，其余/未设 = 方案 A（5 组）。"""
    source: Mapping[str, str] = os.environ if env is None else env
    return ia.resolve_ia(source.get("DJB_UI_IA") if raw is None else raw)


def nav_order(decision: ia.IaDecision) -> tuple[str, ...]:
    """侧栏顺序 = 快捷键顺序 = 窄窗下拉顺序（一个数组三处共用）。"""
    return ia.flat_keys(decision.sections)


def nav_kicker(decision: ia.IaDecision, key: str) -> str:
    """页头眉标「组名 / 页名」：组名取**当前生效表**里的所属组（回滚时自然是旧组名）。"""
    spec = ia.page(key)
    section = next((part for part in decision.sections if key in part.keys), None)
    return (section.micro + " / " + spec.kicker) if section is not None else spec.kicker


def nav_plan(decision: ia.IaDecision, collapsed: "set[str] | frozenset[str]" = frozenset(),
             ) -> tuple[tuple[ia.SectionSpec, tuple[tuple[str, str, str, str], ...], bool], ...]:
    """侧栏渲染计划：(分组, ((key, 标题, 图标, 徽标), ...), 是否折叠)——纯数据，可无窗口断言。

    ``collapsed`` 是**已折叠**的组 key 集合；默认空集 = 全部展开（D17 的默认态）。
    """
    plan = []
    folded = set(collapsed or ())
    for section in decision.sections:
        # 徽标只在方案 A 显示：legacy 回滚必须与 0.3.4 **逐项相等**（旧版没有 BETA）
        items = tuple((key, ia.page(key).title, NAV_ICONS.get(key, "dot"),
                       "BETA" if (ia.page(key).beta and decision.mode != "legacy") else "")
                      for key in section.keys)
        plan.append((section, items, section.key in folded))
    return tuple(plan)


def nav_combo_values(decision: ia.IaDecision) -> tuple[str, ...]:
    """窄窗导航下拉的取值（``1 · 总览``）：顺序与侧栏一致，编号即 Ctrl+N。"""
    return tuple(str(index) + " · " + ia.page(key).title
                 for index, key in enumerate(nav_order(decision), start=1))


def route_for(key: str, renderable: Any) -> tuple[str, Any]:
    """视图路由：``("view", key)`` / ``("placeholder", PageSpec)`` / ``("unknown", None)``。

    IA 注册表（``ia.PAGES``）里可以出现**还没有渲染实现**的页：这类页给占位空态，
    不许把窗口打崩；不在注册表里的 key 按老行为回到当前页（深链校验仍走 ia.page）。
    """
    try:
        spec = ia.page(key)
    except KeyError:
        return ("unknown", None)
    if key in renderable:
        return ("view", key)
    return ("placeholder", spec)


def placeholder_view(spec: ia.PageSpec) -> View:
    """占位页的空态：说清「已进 IA、内容待实现」，不是报错也不是「暂无数据」。"""
    subtitle = spec.subtitle or "该页已纳入 IA 基线，内容实现排在后续切片。"
    return View(key=spec.key, title=spec.title, subtitle=subtitle,
                notes=("占位页：导航可达、空态渲染（非故障、非无数据）。",))

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


def _device_error(exc: BaseException) -> Any:
    """后台线程兜底：把异常变成一条**不假成功**的结果（线程里不能抛给 Tk）。"""
    from .controller import ActionResult

    return ActionResult("绑定流程出错（" + type(exc).__name__ + "）：未绑定；"
                        "可以用高级→手输绑定码", ok=False)


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
        #: S2：取数 worker（惰性建，见 _ensure_refresh_worker）
        self._refresh_worker: Any = None
        #: USDT 收款地址提交 worker（惰性建，见 _ensure_payout_worker）；目标地址与提示文案
        self._payout_worker: Any = None
        self._payout_target = ""
        self._payout_note = ""
        if getattr(controller, "save_path", None) is None:
            controller.save_path = _make_save_path(lambda: self._root)
        if getattr(controller, "ask_code", None) is None:
            controller.ask_code = self._ask_bind_code
        self._bus = bus
        self._on_quit = on_quit
        self._unsubscribe = None
        # IA 模式在构造时定一次：DJB_UI_IA=legacy → 0.3.4 四组；其余 → 方案 A 五组。
        # 侧栏 / Ctrl+N / 窄窗下拉 / 页头眉标全部读这里，不在别处再派生第二份顺序。
        self._ia = ia_state()
        self._ia_order = nav_order(self._ia)
        self._ia_kickers = {key: nav_kicker(self._ia, key) for key in self._ia_order}
        self._nav_buttons: dict[str, Any] = {}
        self._fields: dict[str, Any] = {}
        #: 密码框的「显示 / 隐藏」按钮（key → Button）：窗口级测试要点它验证真能切换
        self._reveal_buttons: dict[str, Any] = {}
        #: 可编辑字段的输入框（key → Entry）：窗口级测试要直接读 ``show`` 选项
        self._entries: dict[str, Any] = {}
        self._row_notes: dict[str, str] = {}
        self._tree: Any = None
        self._detail: Any = None
        self._detail_columns: tuple[str, ...] = ()
        self._vars: dict[str, Any] = {}
        #: CR-20261119-CLIENT-TOKENPLAN-NOOP F2：**本页**输入框上次的值（view key → {字段: 文本}）。
        #: 每个动作都会整页重建，``prefill=False``/secret 的框一律从空串起 → 客户刚填的
        #: 请求地址/密钥/价格在点击瞬间消失（「清空页面」）。重画前把本页的值留在这里，重画时带回。
        self._form_keep: dict[str, dict[str, str]] = {}
        #: 本轮动作要求「已经落盘、重画时别再带回」的字段（F5，密钥即用即清）
        self._forget_keys: set[str] = set()
        #: 下一次性渲染不许走「内容没变就跳过」（动作刚落地的结论必须上屏）
        self._force_next_render: bool = False
        #: 正在渲染的 view key（``_render`` 里钉住；``_render_field`` 取本页暂存值用）
        self._render_view_key: str = ""
        #: RC15-039 #1：动作回投的字段级错误（key → 一句话）。切页/保存成功即清空——
        #: 上一页的红字绝不能跟到下一页（走查原话「跨页面残留」）。
        self._field_errors: dict[str, str] = {}
        #: 当前已渲染视图（含贴上去的错误）；证据/测试读它判断「红字是否真的上屏」
        self._last_view: View | None = None
        #: D4：粘住的失败提示（非空时 ``_update_status`` 不许用常规状态行覆盖它）
        self._sticky_error: str = ""
        self._sticky_job: Any = None
        #: D5：刷新被「已有刷新在跑」吃掉时记在这里，等这一轮结束后补一次
        self._refresh_deferred: Any = None
        #: R2.1 ①：401 顶部横幅（构建在 _build_auth_banner；默认隐藏）
        self._auth_banner: Any = None
        #: R2.1 ①：「重新登录」前所在的页 —— 登录成功后回到这里（不是把客户丢到账户页）
        self._return_view: str = ""
        self._root.title(title)
        self._install_brand_icon()                    # R2.1 C4：窗口图标 = TOKENBANK 完整版
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
        #: D17：折叠状态记在客户端偏好（`theme.load_list("nav_collapsed")`），默认全展开。
        #: 只认当前生效表里存在的组 key —— 换了 IA 表/回滚 legacy 时旧 key 不生效。
        known_sections = {section.key for section in self._ia.sections}
        self._collapsed_sections = {
            key for key in dual.load_list("nav_collapsed", self._prefs_home)
            if key in known_sections}
        self._fade_job = None
        self._theme_name = dual.load(self._prefs_home)
        #: RC15-039 #4：自动刷新（本机偏好，默认开）。关掉之后**一个定时器都不留**。
        self._auto_timer: Any = None
        self._refresh_quiet = False          # 自动刷新：不弹 toast、不抢状态栏
        self._last_input = time.monotonic()
        try:
            controller.auto_refresh = dual.load_flag("auto_refresh", self._prefs_home,
                                                     default=bool(controller.auto_refresh))
        except Exception:                    # pragma: no cover - 偏好文件坏了：用默认（开）
            pass
        dual.apply(self._root, ttk, self._theme_name)
        # ── R2.4 S1/S4：开关与缓存必须在 **_build_shell 之前**定下来
        # 外壳本身要读一次 ``controller.snapshot``（侧栏「演示数据」判断），所以缓存
        # 预填必须更早，否则「启动零网络」这条在第一次 build 就已经破了。
        self._startup_ms: dict[str, float] = {}
        self._skeleton_on = bool(self._flag_enabled(client_flags.SKELETON_FLAG))
        self._cache_primed = False
        self._skeleton_shown = False           # 只有真画了骨架才置 True（测试/取证都读它）
        if self._skeleton_on and self._flag_enabled(client_flags.CACHE_FLAG):
            try:
                self._cache_primed = bool(controller.prime_from_cache())
            except Exception:                    # 缓存坏了：退回骨架，绝不挡启动
                self._cache_primed = False
        self._build_shell()
        self._bind_keys()
        self._root.bind("<Configure>",
                        lambda event: self._apply_layout(getattr(event, "width", None)), add="+")
        if self._bus is not None:
            self._unsubscribe = self._bus.subscribe(self._on_bus)
        # G4：更新 worker 在窗口起来时就接上——否则第一次点「检查更新」会因为
        # ``update_via`` 还是 None 而在**主线程**跑网络检查（正是本条要修的卡顿）。
        self._ensure_update_worker()
        self._ensure_probe_worker()
        self._ensure_discover_worker()
        self._ensure_connectivity_worker()
        self._controller.device_via = self._start_device_flow
        self._controller.device_notify = self._device_state_changed
        bind = getattr(self._root, "bind", None)
        if callable(bind):
            try:
                bind("<Escape>", self._on_escape, add="+")
            except Exception:                            # pragma: no cover - 无窗口
                pass
        # ── R2.4 S1：先出壳（骨架/缓存），再填数据
        # 关（默认）= 0.5.10 行为：show_view 里同步等第一次快照，窗口空白到数据回来。
        # 开 = 先把窗口真正画到屏幕上（骨架或上次缓存的快照），首屏 ≤1.5s 有内容。
        if self._skeleton_on:
            self._paint_first_screen(controller)
        self.show_view(controller.view)
        self._a11y = self._install_a11y()    # 读屏：装不上就 None，界面照跑
        self._bind_activity()                # 记录最后一次输入（判「空闲 60 秒」）
        self._bind_window_state()            # 最大化/恢复后立刻按新状态重排（S4）
        self._schedule_auto_refresh()

    # ── RC15-039 #4：自动刷新（可见 10s / 最小化或空闲 60s；可关；不叠请求）
    def _bind_activity(self) -> None:
        """记录最后一次键盘/鼠标动作：只用于「空闲就降频」，不拦截任何事件。"""
        for sequence in ("<KeyPress>", "<ButtonPress>", "<Motion>", "<MouseWheel>"):
            try:
                self._root.bind(sequence, self._note_activity, add="+")
            except Exception:                        # pragma: no cover - 无窗口
                pass

    def _bind_window_state(self) -> None:
        """窗口 `<Map>`/`<Unmap>` 后**立刻**按新的可见状态重排定时器（复核 S4）。

        没有这一条：最小化 → 定时器还在 10s 那档；恢复可见后，最坏要等 60s 才刷一次，
        走查原话「页面不会自己更新」会在这个窗口里复现。``<Unmap>`` 之后 ``state()``
        可能还没更新，所以延到下一轮事件循环再排（``after(0, …)``）。
        """
        def reschedule(_event: Any = None) -> None:
            try:
                self._root.after_idle(self._schedule_auto_refresh)
            except Exception:                    # pragma: no cover - 窗口已销毁
                pass

        for sequence in ("<Map>", "<Unmap>"):
            try:
                self._root.bind(sequence, reschedule, add="+")
            except Exception:                    # pragma: no cover - 无窗口
                pass

    def _note_activity(self, _event: Any = None) -> None:
        self._last_input = time.monotonic()

    def _idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - float(getattr(self, "_last_input", 0.0)))

    #: 「窗口在眼前」的 wm state。**Windows/macOS 最大化是 ``zoomed``**，不是 ``normal``：
    #: 旧实现只认 ``normal``，把最常见的最大化当成不可见，刷新悄悄降到 60s（复核 M2）。
    VISIBLE_WM_STATES = ("normal", "zoomed")

    def _auto_visible(self) -> bool:
        """窗口是否「在眼前」：最小化（iconic/withdrawn）或长时间无输入都算不可见。"""
        try:
            if str(self._root.state()) not in self.VISIBLE_WM_STATES:
                return False
        except Exception:                            # pragma: no cover - 无窗口
            pass
        return self._idle_seconds() < AUTO_IDLE_S

    def _auto_interval_s(self) -> int:
        return AUTO_REFRESH_VISIBLE_S if self._auto_visible() else AUTO_REFRESH_HIDDEN_S

    #: 总览页**真正**用到的域（复核 M3/S11：以前这里只给 `status`，主卡数字永不更新，
    #: 而页头的「数据截至」照走 = 谎报新鲜度）。
    #:
    #: 2026-11-16 复核 MUST-FIX #4：上一版只补了 6 个域，``_hero``/``_rail`` 还在读
    #: ``gpu``/``account``/``token_plan``/``access``——于是出现「今日贡献在涨、GPU 仪表钉在
    #: 62%、页头时间在跑」的半截假新鲜。现在**首屏每一块用到的域都在这里**：
    #: ``_hero``（gpu/account/token_plan/access/yield/receipts）+ ``_rail``（access/receipts）
    #: + 卡片与表格（status/earnings/bench/tasks）。改版面必须同步改这张清单。
    OVERVIEW_SECTIONS = ("status", "earnings", "bench", "yield", "receipts", "tasks",
                         "gpu", "account", "token_plan", "access")

    def _auto_sections(self) -> tuple[str, ...]:
        """自动刷新取的域：**本页真正要用的那几个**（总览有 6 个域，不能只取 status）。

        宁可多取几个已缓存的小域，也不要出现「时间在走、数字不动」的假新鲜。
        """
        key = str(getattr(self._controller, "view", "") or "")
        if key in ("", "overview"):
            return self.OVERVIEW_SECTIONS
        return (key,) if key in SECTIONS else ()

    def _schedule_auto_refresh(self, delay_s: float | None = None) -> None:
        """排下一次自动刷新（单一定时器：``after`` 只留一个，关掉即取消）。"""
        if not bool(getattr(self._controller, "auto_refresh", True)):
            self._cancel_auto_refresh()
            return
        root = self._root
        if not callable(getattr(root, "after", None)):
            return
        self._cancel_auto_refresh()
        delay = int((self._auto_interval_s() if delay_s is None else delay_s) * 1000)
        try:
            self._auto_timer = root.after(max(250, delay), self._auto_refresh_tick)
        except Exception:                            # pragma: no cover - 窗口已销毁
            self._auto_timer = None

    def _cancel_auto_refresh(self) -> None:
        if self._auto_timer is None:
            return
        try:
            self._root.after_cancel(self._auto_timer)
        except Exception:                            # pragma: no cover - 已触发/已销毁
            pass
        self._auto_timer = None

    def _auto_refresh_tick(self) -> None:
        """到点了：能刷就刷一次，然后**无论如何**排下一次（不叠请求、不刷提示）。"""
        self._auto_timer = None
        if not bool(getattr(self._controller, "auto_refresh", True)):
            return
        worker = getattr(self, "_refresh_worker", None)
        busy = bool(getattr(worker, "busy", False))
        # 三个「不刷」：上一次还没回来（不叠请求）/ 正在改表单（别把输入冲掉）/ 页面没画出来
        if not busy and not self._has_unsaved_edits() and getattr(self, "_last_view", None):
            self._start_quiet_refresh(self._auto_sections())
        self._schedule_auto_refresh()

    def _has_unsaved_edits(self) -> bool:
        """本页有没有**改过但没保存**的输入。有 → 自动刷新让路（重画会把输入冲掉）。

        拿「输入框现在的内容」跟「渲染时填进去的内容」比：不碰「有没有焦点」这种不可靠信号，
        也不用 ``edit_modified``（ttk.Entry 没有这个命令）。
        """
        for key, entry in (getattr(self, "_entries", {}) or {}).items():
            try:
                if str(entry.get()) != str(self._entry_initial.get(key, "")):
                    return True
            except Exception:                        # pragma: no cover - 控件已销毁
                continue
        return False

    def _start_quiet_refresh(self, sections: Sequence[str] | None = None) -> None:
        """自动刷新用的**安静**刷新：不 toast、成功不抢状态栏；失败仍然留一句。"""
        self._refresh_quiet = True
        try:
            self._request_refresh_async(sections)
        finally:
            # 同步回落（无事件循环）时不会有回调来清这个标记
            if getattr(self, "_refresh_worker", None) is None:
                self._refresh_quiet = False

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
            try:                                   # 行高跟着字号一起放大（30 → 38@125%）；口径同 ui_kit.table_rowheight
                self._ttk.Style().configure(
                    "Kit.Treeview", rowheight=kit.table_rowheight(self._scale))
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
        # 0 = 401 横幅（R2.1 ①；默认 grid_remove，不占高度）/ 1 = 页头 / 2 = 滚动内容
        main.rowconfigure(2, weight=1)
        main.columnconfigure(0, weight=1)
        self._main = main
        self._build_auth_banner()
        self._build_pagehead()
        wrapper = tk.Frame(main, background=dual.color("bg"), bd=0, highlightthickness=0)
        wrapper.grid(row=2, column=0, sticky="nsew")
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
            values=list(nav_combo_values(self._ia)))
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
                         font=fonts.sans(kit.px("h2"), bold=True))
        tk.Label(brand, text="DJB 联脑", font=fonts.sans(kit.px("h2"), bold=True),
                 foreground=dual.color("ink"), background=dual.color("surface")).pack(
                     side="left", padx=(8, 0))
        self._brand_sub = tk.Label(brand, text="· 节点工作台",
                                   font=fonts.sans(kit.px("small")),
                                   foreground=dual.color("muted_text"),
                                   background=dual.color("surface"))
        self._brand_sub.pack(side="left", padx=(6, 0))
        self._state_pill = kit.Chip(brand, "状态未知", tone="muted", ground=dual.color("surface"))
        self._state_pill.pack(side="left", padx=(10, 0))
        right = tk.Frame(head, background=dual.color("surface"))
        right.pack(side="right", padx=(0, 12))
        self._header_right = right
        self._refresh_button = kit.Button(right, "刷新", self.refresh, kind="ghost",
                                          ground=dual.color("surface"), px=kit.px("small"),
                                          height=self._px(32), pad=self._px(10))
        self._theme_button = kit.Button(right, self._theme_button_text(), self.toggle_theme,
                                        kind="ghost", ground=dual.color("surface"),
                                        px=kit.px("small"), height=self._px(32), pad=self._px(10))
        self._log_button = kit.Button(right, "打开日志", lambda: self.show_view("logs"),
                                      kind="ghost", ground=dual.color("surface"),
                                      px=kit.px("small"), height=self._px(32), pad=self._px(10))
        self._tune_button = kit.Button(right, "调整贡献", lambda: self.show_view("yield"),
                                       kind="primary", ground=dual.color("surface"),
                                       px=kit.px("small"), height=self._px(32), pad=self._px(12))
        # 头里的固定按钮：读屏要念得出「这是什么按钮」（#31①）
        for widget, name in ((self._refresh_button, "刷新本页数据（也可按 F5）"),
                             (self._theme_button, self._theme_button_text()),
                             (self._log_button, "打开日志"),
                             (self._tune_button, "调整贡献（去让路/节能页）")):
            self._a11y_name(widget, name)
        self._sync_label = tk.Label(right, text="同步 —", font=fonts.mono(kit.px("micro")),
                                    foreground=dual.color("muted_text"),
                                    background=dual.color("surface"))
        self._pack_header_right(narrow=False)
        # 头下沿一条 1px 分隔线（挂在头里，换肤重建时一起销毁）：无阴影，只靠描边分层
        tk.Frame(head, background=dual.color("line"), height=1).place(
            x=0, y=self._px(self.HEADER_H) - 1, relwidth=1.0)

    def _pack_header_right(self, *, narrow: bool) -> None:
        """右侧控件顺序固定：同步时刻 · 打开日志 · 调整贡献 · 主题 · 刷新（窄窗只留后两个）。"""
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
        # 身份块第 3 行要按**真实列宽**截字（字阶 +1px 后 900px 窗里会超出 13 px）；
        # 首次布局完成时再算一遍，否则拿到的是 winfo_width()==1 的未映射状态。
        ident.bind("<Configure>", lambda _event: self._update_identity())
        tk.Label(ident, text="本机节点", font=fonts.sans(kit.px("micro")),
                 foreground=dual.color("muted_text"), background=ground, anchor="w").pack(fill="x")
        self._ident_name = tk.Label(ident, text="—", font=fonts.sans(kit.px("h2"), bold=True),
                                    foreground=dual.color("ink"), background=ground, anchor="w")
        self._ident_name.pack(fill="x", pady=(2, 2))
        line = tk.Frame(ident, background=ground)
        line.pack(fill="x")
        self._ident_dot = tk.Canvas(line, width=10, height=10, background=ground, bd=0,
                                    highlightthickness=0, takefocus=0)
        self._ident_dot.pack(side="left")
        self._ident_dot.create_oval(2, 2, 8, 8, fill=dual.color("mint_text"), outline="")
        self._ident_state = tk.Label(line, text="状态未知", font=fonts.sans(kit.px("micro")),
                                     foreground=dual.color("muted_text"), background=ground,
                                     anchor="w")
        self._ident_state.pack(side="left", padx=(4, 0))
        self._ident_meta = tk.Label(ident, text="—", font=fonts.sans(kit.px("micro")),
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
        self._nav_section_bodies: dict[str, Any] = {}
        self._nav_section_chevrons: dict[str, Any] = {}
        for index, (section, items, folded) in enumerate(
                nav_plan(self._ia, self._collapsed_sections)):
            self._render_nav_section(section, items, first=index == 0, collapsed=folded)
        kit.Divider(side, color=dual.color("line")).pack(fill="x", padx=12)
        foot = tk.Frame(side, background=ground)
        foot.pack(fill="x", padx=16, pady=(8, 10))
        from ..facts import CLIENT_VERSION
        self._version_label = tk.Label(
            foot, text="v" + CLIENT_VERSION, font=fonts.mono(kit.px("micro")),
            foreground=dual.color("muted_text"), background=ground, anchor="w")
        self._version_label.pack(side="left")
        self._demo_chip = None
        if getattr(self._controller.snapshot, "source", "") == "demo":
            self._demo_chip = kit.Chip(foot, "演示数据", tone="amber", ground=ground,
                                       px=kit.px("micro"), height=17)
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

    def _render_nav_section(self, section: Any,
                            items: tuple[tuple[str, str, str, str], ...] = (),
                            *, first: bool = False, collapsed: bool = False) -> None:
        """一个分组：micro 档分组头（拉丁微标 + 中文组名）+ 该组导航项。

        分组/顺序/标题/徽标全部来自 :func:`nav_plan`（IA 单一数据源）；本方法只负责画。
        ``BETA`` 徽标走 :class:`ui_kit.NavItem` 自带的右侧计数位（等宽 micro 档），
        与「暂不可用」的置灰是两件事：BETA 可点，只是标明能力滞后。
        """
        kit, tk = self._kit, self._tk
        ground = dual.color("surface3")
        head = tk.Frame(self._nav_inner, background=ground)
        head.pack(fill="x", padx=6, pady=(0 if first else 10, 2))
        # D17：组标题可折叠（默认全展开，状态存客户端偏好）。整行可点，不只箭头——
        # 鼠标目标 14px 字太小，客户会点不中。
        chevron = tk.Label(head, text="▸" if collapsed else "▾",
                           font=self._fonts.sans(kit.px("micro")),
                           foreground=dual.color("muted_text"), background=ground, anchor="w")
        chevron.pack(side="left", padx=(0, 4))
        self._nav_section_chevrons[section.key] = chevron
        for widget in (head, chevron):
            widget.bind("<Button-1>", lambda _event, name=section.key: self._toggle_nav_section(name))
            try:
                widget.configure(cursor="hand2")
            except Exception:                          # pragma: no cover - 平台不支持
                pass
        tk.Label(head, text=section.micro, font=self._font_for(section.micro, kit.px("micro")),
                 foreground=dual.color("muted_text"), background=ground,
                 anchor="w").pack(side="left")
        # R2.1 ⑤ 的副作用修正：微标中文化之后，有的分组 title 与 micro 是同一个词
        # （共享/运行/收益/接入/系统），旧布局会把同一句话并排画两遍（「共享 共享」）。
        # 相同就只画一次；不同（例如未来 title 更具体）仍然两段都画。
        if str(section.title).strip() and str(section.title).strip() != str(section.micro).strip():
            tk.Label(head, text=section.title, font=self._fonts.sans(kit.px("micro")),
                     foreground=dual.color("muted_text"), background=ground,
                     anchor="w").pack(side="left", padx=(6, 0))
        self._nav_section_headers[section.key] = head
        body = tk.Frame(self._nav_inner, background=ground)
        self._nav_section_bodies[section.key] = body
        for key, label, icon, badge in items:
            item = kit.NavItem(body, text=label, icon=icon, count=badge,
                               ground=ground,
                               command=lambda name=key: self.show_view(name))
            self._a11y_name(item, label + ("（当前页）" if key == self._controller.view else ""))
            item.pack(fill="x", pady=1)
            self._nav_buttons[key] = item
        if not collapsed:
            body.pack(fill="x")

    # ── D17：分组折叠（状态持久化在客户端偏好里，默认全展开）
    def _toggle_nav_section(self, key: str) -> None:
        """点组标题：展开 ⇄ 折叠，并把新状态写回偏好（写不进去也照样折叠，不弹错）。"""
        self._set_nav_collapsed(key, key not in self._collapsed_sections)

    def _set_nav_collapsed(self, key: str, collapsed: bool, *, persist: bool = True) -> None:
        """落一次折叠状态：内存 → 控件 → 偏好文件（三步各自独立，任一步坏了不连坐）。"""
        name = str(key or "")
        if collapsed:
            self._collapsed_sections.add(name)
        else:
            self._collapsed_sections.discard(name)
        body = self._nav_section_bodies.get(name)
        head = self._nav_section_headers.get(name)
        if body is not None:
            try:
                if collapsed:
                    body.pack_forget()
                elif head is not None and not body.winfo_ismapped():
                    body.pack(fill="x", after=head)
            except Exception:                          # pragma: no cover - 控件已销毁
                pass
        chevron = self._nav_section_chevrons.get(name)
        if chevron is not None:
            try:
                chevron.configure(text="▸" if collapsed else "▾")
            except Exception:                          # pragma: no cover
                pass
        if persist:
            try:
                dual.save_list("nav_collapsed", sorted(self._collapsed_sections),
                               self._prefs_home)
            except Exception:                          # pragma: no cover - 只读 home
                pass
        on_resize = getattr(self, "_on_nav_inner_resize", None)
        if callable(on_resize):
            try:
                on_resize()                            # 折叠后滚动区要重算
            except Exception:                          # pragma: no cover
                pass

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

    # ── R2.4 S1：骨架 / 缓存首屏（先出壳，再填数据）
    def _flag_enabled(self, name: str) -> bool:
        """读壳层开关（配置读不到一律按关闭 —— 默认关是硬约束）。"""
        try:
            return client_flags.enabled(name, config=_flags_config(self._prefs_home))
        except Exception:                          # 开关模块坏了也不许挡住启动
            return False

    def _paint_first_screen(self, controller: Any) -> None:
        """把**有内容的首屏**画到屏幕上并记时（骨架 or 上次缓存），不等网络。

        顺序很关键：``update_idletasks()`` 让 X11/Win32 真的把这一帧推出去，
        否则「首屏时间」只是 Python 里的一个数字，客户看到的还是空白窗。
        """
        cache_ok = bool(getattr(self, "_cache_primed", False))
        if cache_ok:
            view = controller.current()             # 从缓存算视图，**不取数**
            self._render(view, force=True)
            self._update_status()
        else:
            self._render_skeleton()
        self._startup_ms["first_paint"] = time.monotonic()
        try:
            self._root.update_idletasks()           # 真正把这一帧推给窗口管理器
        except Exception:                           # pragma: no cover - 无窗口
            pass

    def _render_skeleton(self) -> None:
        """骨架：标题 + 三块灰色占位卡 + 一行「正在读取本机数据…」。

        为什么不是转圈圈：客户在真机上第一次看到的是空白窗，最需要的一句话是
        「它在动」——文字比动画在截图/读屏里都留得下证据。
        """
        kit, tk = self._kit, self._tk
        ground = dual.color("bg")
        try:
            # ``_body`` 是 ``_render`` 里按页新建的；骨架先自己起一个，下一次 _render 会整体
            # ``_clear()`` 掉（骨架只是一次性的占位，不参与任何布局契约）。
            for child in list(self._content.winfo_children()):
                child.destroy()
            body = tk.Frame(self._content, background=ground, bd=0, highlightthickness=0)
            body.pack(fill="both", expand=True, padx=(26, 6), pady=(12, 6))
            self._body = body
            tk.Label(self._body, text="正在读取本机数据…", font=self._fonts.sans(kit.px("h2"),
                     bold=True), foreground=dual.color("ink"), background=ground,
                     anchor="w").pack(anchor="w", pady=(2, 2))
            tk.Label(self._body, text="窗口已经起来了：节点状态、收益与接入数据正在读，"
                     "读到就会自动填进来。", font=self._fonts.sans(kit.px("small")),
                     foreground=dual.color("muted_text"), background=ground, anchor="w",
                     justify="left", wraplength=self._wrap_hint()).pack(anchor="w", pady=(0, 10))
            for _card_index in range(3):
                card = kit.RoundedCard(self._body, ground=dual.color("surface"), pad=(14, 11))
                card.pack(fill="x", pady=(0, 8))
                for line, height in ((0, 10), (1, 10), (2, 8)):
                    bar = tk.Frame(card.body, background=dual.color("line"), height=height)
                    bar.pack(fill="x", pady=(0 if line == 0 else 6, 0))
            self._skeleton_shown = True
        except Exception as exc:                       # pragma: no cover - 渲染失败不许挡住启动
            self._skeleton_shown = False
            self._skeleton_error = type(exc).__name__ + ": " + str(exc)

    # ── R2.1 C4：窗口图标（读不到就静默跳过，绝不影响启动）
    def _install_brand_icon(self) -> None:
        """把 ``djb_node/assets/tokenbank-dark-full-256.png`` 设成窗口图标。

        为什么不是 ``iconbitmap``（.ico）：Tk 在 Linux/macOS 只认 PNG/GIF，``iconbitmap``
        还要求真实文件路径（冻结包里没有）。这里统一走 ``PhotoImage + iconphoto``：
        三平台同一份代码、同一份资源，失败就当没设过。
        """
        from .. import assets as assets_mod

        self._brand_icon = None                    # 先置空：读不到时属性也在（测试/取证读它）
        data = assets_mod.resource_bytes(assets_mod.WINDOW_ICON)
        if not data:
            return
        try:
            import base64

            self._brand_icon = self._tk.PhotoImage(
                data=base64.b64encode(data).decode("ascii"))
            self._root.iconphoto(True, self._brand_icon)
        except Exception:                          # Tk 不认 / 窗口已销毁：当作没图标
            self._brand_icon = None

    # ── R2.1 ①：401 顶部横幅（登录过期不是死路）
    def _build_auth_banner(self) -> None:
        """一条**跨内容区**的横幅：说清「什么过期了 + 下一步按钮」，默认隐藏。

        为什么放顶部而不是某一页：一轮取数里 tasks/receipts/earnings/account 会同时 401，
        客户此刻可能停在**任何**一页；只在账户页给入口等于让他自己去猜（报告 P0-1）。
        """
        kit, tk = self._kit, self._tk
        bar = tk.Frame(self._main, background=dual.color("amber_bg"), bd=0,
                       highlightthickness=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self._auth_banner = bar
        self._auth_text = tk.Label(
            bar, text="登录已过期：云端数据（任务 / 收据 / 收益）暂时读不到",
            font=self._fonts.sans(kit.px("small")),
            foreground=dual.color("amber_text"), background=dual.color("amber_bg"),
            anchor="w", justify="left")
        self._auth_text.grid(row=0, column=0, sticky="w",
                             padx=(self._px(26), self._px(10)), pady=(self._px(8), self._px(8)))
        self._auth_button = kit.Button(
            bar, "重新登录", self._on_relogin, kind="primary",
            ground=dual.color("amber_bg"), px=kit.px("small"), height=self._px(32),
            pad=12)
        self._auth_button.grid(row=0, column=1, sticky="e", padx=(0, self._px(8)),
                               pady=(self._px(6), self._px(6)))
        self._auth_open = kit.Button(
            bar, "打开网页端", lambda: self._open_url(facts.WEB_CONSOLE_URL), kind="ghost",
            ground=dual.color("amber_bg"), px=kit.px("small"), height=self._px(32),
            pad=12)
        self._auth_open.grid(row=0, column=2, sticky="e", padx=(0, self._px(26)),
                             pady=(self._px(6), self._px(6)))
        self._a11y_name(self._auth_button, "重新登录（登录已过期）")
        bar.grid_remove()                     # 默认不占位

    def _sync_auth_banner(self) -> None:
        """按**当前快照**决定横幅显隐（每轮渲染/状态更新都调，代价是一次属性读）。"""
        bar = getattr(self, "_auth_banner", None)
        if bar is None:
            return
        try:
            snapshot = self._controller.snapshot
            domain, reason = snapshot.login_required() if hasattr(snapshot, "login_required") \
                else ("", "")
        except Exception:                      # pragma: no cover - 快照形状异常时不挡界面
            domain, reason = "", ""
        if not domain:
            if getattr(self, "_auth_shown", False):
                bar.grid_remove()
                self._auth_shown = False
            return
        text = ("登录已过期（" + reason + "）：云端数据（任务 / 收据 / 收益）暂时读不到；"
                "重新登录后回到本页，本机数据不受影响")
        try:
            self._auth_text.configure(text=text)
        except Exception:                      # pragma: no cover - 控件已销毁
            return
        # 每次重建壳/换肤后都要重新贴回去（横幅是 grid_remove 的，必须显式 grid）
        try:
            bar.grid()
        except Exception:                      # pragma: no cover
            pass
        self._auth_shown = True

    def _on_relogin(self) -> None:
        """横幅按钮：记住**现在这一页**，登录成功后回到这里（R2.1 ① 的回原页口径）。"""
        self._return_view = self._controller.view
        self._on_action("auth.relogin")

    def _resume_after_login(self, ok: bool) -> None:
        """登录有结果后回原页（成功才回；失败留在原地，横幅还在，不会假装已登录）。"""
        target = str(getattr(self, "_return_view", "") or "")
        if not ok or not target:
            return
        self._return_view = ""
        if target != self._controller.view:
            self.show_view(target)
        self._toast("已重新登录，已回到「" + VIEW_TITLES.get(target, target) + "」", ok=True)

    def _build_pagehead(self) -> None:
        """68px 页头：左「眉标 + 18px 标题」，右「数据时刻 + 刷新」。"""
        kit, tk = self._kit, self._tk
        fonts = self._fonts
        head = tk.Frame(self._main, background=dual.color("bg"), height=self._px(self.PAGEHEAD_H),
                        bd=0, highlightthickness=0)
        head.grid(row=1, column=0, sticky="ew")
        head.grid_propagate(False)
        head.columnconfigure(0, weight=1)
        self._pagehead = head
        left = tk.Frame(head, background=dual.color("bg"))
        left.grid(row=0, column=0, sticky="w", padx=(self._px(26), 0), pady=(self._px(12), self._px(12)))
        self._page_kicker = tk.Label(left, text="总览", font=fonts.for_text("总览", kit.px("micro")),
                                     foreground=dual.color("muted_text"),
                                     background=dual.color("bg"), anchor="w")
        self._page_kicker.pack(anchor="w")
        self._view_title = tk.Label(left, text=VIEW_TITLES[self._controller.view],
                                    font=fonts.sans(kit.px("h1"), bold=True),
                                    foreground=dual.color("ink"), background=dual.color("bg"),
                                    anchor="w")
        self._view_title.pack(anchor="w")
        right = tk.Frame(head, background=dual.color("bg"))
        right.grid(row=0, column=1, sticky="e", padx=(0, self._px(26)))
        self._page_actions = right
        self._snapshot_label = tk.Label(right, text="数据截至 —", font=fonts.sans(kit.px("micro")),
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
        self._status = tk.Label(bar, text=DEFAULT_MESSAGE, font=fonts.sans(kit.px("micro")),
                                foreground=dual.color("muted_text"),
                                background=dual.color("surface3"), anchor="w")
        self._status.pack(side="left", padx=(self._px(14), 0), pady=self._px(4))
        self._status_right = tk.Label(bar, text="—", font=fonts.sans(kit.px("micro")),
                                      foreground=dual.color("muted_text"),
                                      background=dual.color("surface3"), anchor="e")
        self._status_right.pack(side="right", padx=(0, self._px(14)), pady=self._px(4))
        # 2026-11-24：字阶整体 +1px 后，900px 宽窗里「GPU · 显存 · 版本 · 主题」四段放不下
        # （需求 273 px / 实得 197 px），而窄窗阈值仍是 760 —— 阈值跟不上字阶。
        # 这里按**实测是否被裁**退一档，而不是再加一个魔法宽度。
        bar.bind("<Configure>", lambda _event: self._fit_status_right())

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
        for sequence, key in view_shortcut_bindings(keys=self._ia_order):
            self._root.bind(sequence, lambda _e, name=key: self.show_view(name))
        self._root.bind("<Control-Prior>", lambda _e: self._step_view(-1))
        self._root.bind("<Control-Next>", lambda _e: self._step_view(1))
        self._root.bind("<Control-r>", lambda _e: self.refresh())
        # RC15-039 #31②：Tab / Shift-Tab 按**视觉顺序**走（Tk 默认的创建顺序在顶部那排
        # 右对齐按钮上是从右往左，客户按第一下 Tab 就发现焦点跑到最右边去了）
        self._root.bind("<Tab>", lambda _e: self._focus_step(1))
        self._root.bind("<Shift-Tab>", lambda _e: self._focus_step(-1))
        # X11 把 Shift+Tab 报成 ISO_Left_Tab；Windows 的 Tcl/Tk **没有这个 keysym**，
        # 直接 bind 会抛 TclError，把整个 __init__ 炸掉（0.3.9 真机验证抓到：Windows 上
        # 客户端根本起不来）。Shift+Tab 已由上一行覆盖，这条纯属可选别名，绑不上就跳过。
        try:
            self._root.bind("<ISO_Left_Tab>", lambda _e: self._focus_step(-1))
        except Exception:
            pass
        self._root.bind("<Escape>", lambda _e: self._canvas.yview_moveto(0))

    def _step_view(self, step: int) -> None:
        """Ctrl+PageUp/PageDown：按 IA 顺序前后切（13 个视图循环，不依赖键盘布局）。"""
        try:
            current = self._ia_order.index(self._controller.view)
            key = self._ia_order[(current + int(step)) % len(self._ia_order)]
        except Exception:                            # pragma: no cover - 控制器异常
            return
        self.show_view(key)

    # ── 渲染
    def show_view(self, key: str) -> View:
        """切视图并立即完成布局（截图脚本依赖这一点）。

        IA 注册表里还没有渲染实现的页 → 占位空态（可点、可截图、不崩）；
        完全未知的 key → 回到当前页（与 0.3.4 的 ``ValueError`` 行为一致）。
        """
        route, payload = route_for(key, VIEW_TITLES)
        if route == "unknown":
            return self._controller.current()
        if route == "placeholder":
            view = placeholder_view(payload)
        else:
            try:
                view = self._controller.show(key)
            except ValueError:                       # pragma: no cover - 与 route 双保险
                return self._controller.current()
        target_section = ia.section_for(key, self._ia.sections)
        first_show = not getattr(self, "_shown_once", False)
        if (not first_show and target_section is not None
                and target_section.key in self._collapsed_sections):
            # D17：**显式**去折叠组里的页 → 先把该组展开（选中项不许藏在收起的组里）。
            # 首屏那次不算：否则启动落在总览就会把用户折叠的「诊断」组又掰开、还写回偏好，
            # 折叠形同虚设（重启一次偏好就被改写）。
            self._set_nav_collapsed(target_section.key, False)
        self._set_field_errors({})          # 切页清空上一页的字段红字（走查原话：跨页残留）
        self._clear_sticky()                # D4：上一页的失败提示也不许跟到下一页
        self._render(view, force=True)      # 切页 = 显式意图：布局/焦点都要重来
        self._sync_nav(key)
        self._update_status()
        try:
            self._canvas.yview_moveto(0)
            self._root.update_idletasks()
        except Exception:
            pass
        self._sync_content_width()                   # B4：换视图后内容宽仍 ≤ 画布宽
        self._page_fade()
        self._shown_once = True
        return view

    def refresh(self) -> View | None:
        """刷新（交互路径：按钮 / F5 / Ctrl-R）。**取数在 worker 线程**，不阻塞主线程。

        返回 ``None`` 表示结果稍后到（:meth:`refresh_now` 是同步版，给 ``--shot`` /
        没有事件循环的场景用）。重复点击不叠加：只会更新一次「正在刷新…」。
        """
        worker = self._ensure_refresh_worker()
        if worker is None:                         # 没有 after()（假 root / 无窗口）
            return self.refresh_now()
        if not worker.start():
            return None
        return None

    def refresh_now(self) -> View:
        """同步刷新（阻塞到取数完成）：截图脚本 / 单测 / 无事件循环环境专用。"""
        view = self._controller.refresh()
        self._render(view, force=True)      # 显式「立即刷新」（截图脚本/单测要确定性重画）
        self._update_status()
        return view

    def _ensure_refresh_worker(self) -> Any:
        """惰性建 worker：``schedule`` 用 ``root.after``，``cancel`` 用 ``after_cancel``。

        ``after`` 是**主线程**的东西：worker 线程只置一个 Event，主线程轮询取结果。
        """
        worker = getattr(self, "_refresh_worker", None)
        if worker is not None:
            return worker
        root = self._root
        if not callable(getattr(root, "after", None)):
            return None
        from .refresh_worker import RefreshWorker

        def schedule(delay: int, fn: Any) -> Any:
            return root.after(int(delay), fn)

        def cancel(job: Any) -> None:
            root.after_cancel(job)

        worker = RefreshWorker(self._controller.source.snapshot,
                               apply=self._apply_refresh,
                               on_start=self._refresh_started,
                               on_busy=self._refresh_busy,
                               on_finish=self._refresh_finished,
                               schedule=schedule, cancel=cancel)
        self._refresh_worker = worker
        # 动作里（登出/绑定/暂停后）的刷新也走异步：否则那几次点击照样冻住窗口
        self._controller.refresh_via = self._request_refresh_async
        return worker

    # ── 刷新回调（全部在主线程执行）
    def _request_refresh_async(self, sections: Any = None) -> None:
        worker = self._ensure_refresh_worker()
        if worker is None:
            self._controller.refresh(sections)     # 没有事件循环：退回同步
            return
        worker.start(sections)

    def _refresh_started(self) -> None:
        """(a) 立即反馈：按钮文案 + 状态栏，在取数**开始之前**就变。"""
        quiet = bool(getattr(self, "_refresh_quiet", False))
        button = getattr(self, "_refresh_button", None)
        if button is not None:
            try:
                button.set_text("刷新中…" if not quiet else button.text)
            except Exception:                      # pragma: no cover - 控件已销毁
                pass
        if not quiet:
            self._toast("正在刷新…（可以继续拖动/切页）", ok=True)

    def _refresh_busy(self) -> None:
        """(c) 并发闸门：重复点击只提示，不再派发第二个取数。"""
        if getattr(self, "_refresh_quiet", False):
            return                                 # 自动刷新不刷「已忽略这次点击」
        self._toast("正在刷新…上一次还没结束，已忽略这次点击", ok=True)

    def _refresh_finished(self) -> None:
        button = getattr(self, "_refresh_button", None)
        if button is not None:
            try:
                button.set_text("刷新")
            except Exception:                      # pragma: no cover
                pass

    def _apply_refresh(self, fresh: Any, error: BaseException | None,
                       sections: Any = None) -> None:
        """(d) 结果落地：合并快照 → 渲染 → 状态栏。异常已经在这里变成「刷新失败」。

        自动刷新（``_refresh_quiet``）走**安静**路径：成功不 toast、不覆盖状态栏最后一条消息
        （那条常常是「未保存：…」这种客户还没读完的话），但**失败照旧留一句**在上面。
        """
        quiet = bool(getattr(self, "_refresh_quiet", False))
        view = self._controller.apply_refresh(fresh, sections=sections, error=error,
                                             quiet=quiet)
        self._consume_deferred_refresh()          # D5：补上被丢弃的那次刷新
        # RC15-039 复核 M1：**派发时判过，落地时还要再判一次**。
        # 派发到落地之间的几百毫秒里客户可能刚开始打字；这时重画会把输入框刷回旧值，
        # 还会顺手把 `_entry_initial` 一起刷新——"有改动"的证据被抹掉，下一轮继续冲。
        # 所以落地发现新输入：数据留在快照里，**本页不重画、页头时间也不动**（时间写的是
        # 屏幕上这份数据的截至时刻，重画没发生就不能往前推，否则又是一种谎报）。
        if quiet and error is None and self._has_unsaved_edits():
            self._refresh_quiet = False
            self._note_skipped_render()
            return
        self._skip_notice_on = False
        self._render(view)
        self._update_status()
        worker = getattr(self, "_refresh_worker", None)
        duration = getattr(worker, "last_duration_s", 0.0) or 0.0
        message = str(self._controller.message or "")
        if quiet:
            self._refresh_quiet = False
            return
        if duration and message.startswith("已刷新"):
            message += "（用时 %.1f s）" % duration
        self._toast(message or "已刷新", ok=bool(message.startswith("已刷新")))

    def _show_payout_dialog(self, text: str, *, ok: bool) -> None:
        """收款地址提交结果的模态提示（失败用 warning，成功用 info）。

        设计取舍：Tk 的 messagebox 在没有显示环境（单测 / 截图脚本 / Xvfb 缺失）会抛
        TclError——那种情况下**只保留状态条**，绝不因为「弹不出来」把结果弄丢，也绝不
        让异常冒到事件循环里把窗口打死。
        """
        root = getattr(self, "_root", None)
        if root is None or not callable(getattr(root, "after", None)):
            return                                   # 无事件循环（测试 / headless）
        try:
            from tkinter import messagebox
        except Exception:                            # pragma: no cover - 极简 Tk
            return
        title = "DJB / 收款地址提交成功" if ok else "DJB / 收款地址没提交成功"
        try:
            if ok:
                messagebox.showinfo(title, text, parent=root)
            else:
                messagebox.showwarning(title, text, parent=root)
        except Exception:                            # pragma: no cover - 无显示环境
            return

    def _consume_deferred_refresh(self) -> None:
        """把「提交成功后没刷成」的那次刷新补上（D5）。

        ``RefreshWorker.start`` 忙时**丢弃**请求，因此不能只靠一次派发：记下 sections，
        这一轮结束后再试；仍在忙就 300ms 后重试（有界：只重试到成功一次为止）。
        """
        sections = getattr(self, "_refresh_deferred", None)
        if sections is None:
            return
        worker = getattr(self, "_refresh_worker", None)
        if worker is not None and getattr(worker, "busy", False):
            root = getattr(self, "_root", None)
            if callable(getattr(root, "after", None)):
                root.after(300, self._consume_deferred_refresh)
            return
        self._refresh_deferred = None
        self._request_refresh_async(sections)

    def _note_skipped_render(self) -> None:
        """落地时发现新输入 → 只留一句话，不重画（同一次跳过只念一遍，别每 10 秒吵一次）。"""
        if bool(getattr(self, "_skip_notice_on", False)):
            return
        self._skip_notice_on = True
        self._announce("有未保存的输入，已跳过自动刷新（这一页没重画，保存后自动恢复）")

    # ── 检查更新 / 安装更新（G4 / RC14 P0）：网络 + 签名校验，绝不在主线程
    def _ensure_update_worker(self) -> Any:
        """惰性建更新 worker（同一个 :class:`RefreshWorker` 模板，``kind`` 区分 check/apply）。"""
        worker = getattr(self, "_update_worker", None)
        if worker is not None:
            return worker
        root = self._root
        if not callable(getattr(root, "after", None)):
            return None
        from .refresh_worker import RefreshWorker

        def schedule(delay: int, fn: Any) -> Any:
            return root.after(int(delay), fn)

        def cancel(job: Any) -> None:
            root.after_cancel(job)

        def fetch(sections: Any = None) -> Any:
            kind = str(getattr(self, "_update_kind", "check"))
            if kind == "apply":
                return self._controller.run_update_apply()
            return self._controller.run_update_check()

        worker = RefreshWorker(fetch, apply=self._apply_update_worker,
                               on_start=self._update_started,
                               on_busy=self._update_busy,
                               schedule=schedule, cancel=cancel)
        self._update_worker = worker
        # 动作入口：controller 侧只认「有没有异步通道」，具体 check/apply 由本层记 kind
        self._controller.update_via = self._start_update
        return worker

    def _start_update(self, kind: str = "check") -> bool:
        """controller 的动作回调进来：派给 worker（返回 True = 已派出）。"""
        self._update_kind = "apply" if str(kind) == "apply" else "check"
        worker = self._ensure_update_worker()
        if worker is None:
            return False                  # 无事件循环 → controller 回落同步
        return bool(worker.start())

    def _update_started(self) -> None:
        self._toast("正在检查更新…" if getattr(self, "_update_kind", "check") == "check"
                    else "正在下载并安装更新…", ok=True)

    def _update_busy(self) -> None:
        self._toast("上一次更新操作还没结束，已忽略这次点击", ok=True)

    def _apply_update_worker(self, result: Any, error: BaseException | None,
                             sections: Any = None) -> None:
        if error is not None:
            self._toast("更新操作失败（" + type(error).__name__ + "）：" + str(error)[:120],
                        ok=False)
            return
        if result is None:
            self._toast("更新操作失败：没有拿到结果", ok=False)
            return
        self._apply_update(getattr(result, "payload", None) or {},
                           ok=bool(getattr(result, "ok", False)),
                           message=str(getattr(result, "message", "") or ""))

    def _apply_update(self, payload: Any, *, ok: bool | None = None,
                      message: str = "") -> None:
        """把一次更新结果讲给客户听（G4-3/G4-4）：发生了什么 + 下一步 + 能点就点。

        ``payload`` 是 ``check()``/``apply()`` 的原始结果（含 ``state``）；``message``
        是 controller 已经翻好的人话（优先用它，避免两层文案打架）。
        """
        from ..update import guidance
        state = str((payload or {}).get("state") or "") if hasattr(payload, "get") else ""
        if state in ("checking", "applying"):
            # 派发那一瞬间已经在 ``_update_started`` 里播报过（同一句话），这里不再重复
            return
        info = guidance(payload if hasattr(payload, "get") else None)
        text = message or (info["title"] + ("（" + info["detail"] + "）" if info["detail"] else "")
                          + " — 下一步：" + info["next"])
        self._toast(text, ok=bool(ok if ok is not None else info["ok"]))
        # 下载类动作直接把客户送到下载页（no_artifact 的「下一步」不能只是一句话）
        url = str(info.get("url") or "")
        if info.get("action") == "download" and url:
            self._open_url(url)
        # 结果落地后重画账户页（「上次结果」那三行要立刻反映新状态）
        self._render(self._controller.current())
        self._update_status()

    # ── USDT 收款地址提交（owner 2026-11-13 方案 B：客户端直接提交云端）
    def _ensure_payout_worker(self) -> Any:
        """惰性建「提交收款地址」worker：复用 W1 的 :class:`RefreshWorker` 模式。

        同一个模式解决同一个问题（主线程不碰网络）：worker 线程只跑
        ``controller.submit_payout``（纯数据层调用，不碰控件），主线程用 ``after`` 轮询取结果。
        """
        worker = getattr(self, "_payout_worker", None)
        if worker is not None:
            return worker
        root = self._root
        if not callable(getattr(root, "after", None)):
            return None
        from .refresh_worker import RefreshWorker

        def schedule(delay: int, fn: Any) -> Any:
            return root.after(int(delay), fn)

        def cancel(job: Any) -> None:
            root.after_cancel(job)

        worker = RefreshWorker(
            lambda sections=None: self._controller.submit_payout(self._payout_target),
            apply=self._apply_payout, on_start=self._payout_started,
            on_busy=self._payout_busy, schedule=schedule, cancel=cancel)
        self._payout_worker = worker
        return worker

    def _start_payout_submit(self, address: str, message: str = "") -> None:
        """派发一次提交；没有事件循环（截图脚本/单测）才退回同步，行为和按钮一致。"""
        self._payout_target = str(address)
        self._payout_note = str(message or "")
        worker = self._ensure_payout_worker()
        if worker is None:
            result = self._controller.submit_payout(self._payout_target)
            self._toast(str(result.message), ok=bool(result.ok))
            self._request_refresh_async(("access",))
            return
        worker.start()                     # 忙时走 on_busy（不叠加第二个 POST）

    def _payout_started(self) -> None:
        self._toast(self._payout_note or "正在提交收款地址到云端…", ok=True)

    def _payout_busy(self) -> None:
        self._toast("正在提交…上一次还没结束，已忽略这次点击", ok=True)

    def _apply_payout(self, result: Any, error: BaseException | None,
                      sections: Any = None) -> None:
        """结果落地（主线程）：先弹窗 + toast 说清成败，再异步重拉云端状态。

        2026-11-18 客户要求：「更换 USDT 提交要提示冷却时间等返回和弹窗」。收款地址是
        钱相关的一次性动作，服务端回什么（待审核 / 冷却到什么时候 / 哪种拒绝）必须让
        客户在**看得见、停得住**的地方读完——所以除了状态条，还给一个模态提示框。
        """
        if error is not None:
            text = "提交失败（" + type(error).__name__ + "）；地址未变更"
            ok = False
        elif result is None:
            text = "提交失败：没有拿到结果；地址未变更"
            ok = False
        else:
            text = str(getattr(result, "message", "") or "提交完成")
            ok = bool(getattr(result, "ok", False))
        self._toast(text, ok=ok)
        self._show_payout_dialog(text, ok=ok)
        # D5（2026-11-18）：POST 成功后必须让卡片追上来。旧实现遇到「已有刷新在跑」
        # 就直接放弃这次刷新（RefreshWorker.start 忙时丢弃请求）⇒ 卡片停在提交前的
        # 「尚未提交」，客户看到的却是「刚提交成功」，于是再点一次（真机 11:00:33 /
        # 11:00:49 两次 POST 就是这么来的）。现在改成「记下来，这一轮结束立刻补」。
        refresh = getattr(self, "_refresh_worker", None)
        if refresh is None or not getattr(refresh, "busy", False):
            self._request_refresh_async(("access",))
        else:
            self._refresh_deferred = ("access",)

    # ── 探针（RC14 D）：「立即探测」也要在 worker 里跑（网络 10 s 级）
    def _ensure_probe_worker(self) -> Any:
        """惰性建探针 worker：与刷新/更新同一个 :class:`RefreshWorker` 模板。"""
        worker = getattr(self, "_probe_worker", None)
        if worker is not None:
            return worker
        root = self._root
        if not callable(getattr(root, "after", None)):
            return None
        from .refresh_worker import RefreshWorker

        def schedule(delay: int, fn: Any) -> Any:
            return root.after(int(delay), fn)

        def cancel(job: Any) -> None:
            root.after_cancel(job)

        worker = RefreshWorker(lambda sections=None: self._controller.run_probe(),
                               apply=self._apply_probe, on_start=self._probe_started,
                               on_busy=self._probe_busy,
                               schedule=schedule, cancel=cancel)
        self._probe_worker = worker
        self._controller.probe_via = self._start_probe
        return worker

    # ── R2.1 ③：联通测试 worker（网络在后台；主线程只写「进行中」+ 播报结果）
    def _ensure_connectivity_worker(self) -> Any:
        """惰性建联通测试 worker：与刷新/探针同一个 RefreshWorker 模板。"""
        worker = getattr(self, "_connectivity_worker", None)
        if worker is not None:
            return worker
        root = self._root
        if not callable(getattr(root, "after", None)):
            return None
        from .refresh_worker import RefreshWorker

        def schedule(delay: int, fn: Any) -> Any:
            return root.after(int(delay), fn)

        def cancel(job: Any) -> None:
            root.after_cancel(job)

        worker = RefreshWorker(
            lambda sections=None: self._controller.run_connectivity(
                getattr(self, "_connectivity_payload", {}) or {}),
            apply=self._apply_connectivity, on_busy=self._connectivity_busy,
            schedule=schedule, cancel=cancel)
        self._connectivity_worker = worker
        self._controller.connectivity_via = self._start_connectivity
        return worker

    def _start_connectivity(self, payload: Mapping[str, Any]) -> bool:
        worker = self._ensure_connectivity_worker()
        if worker is None:
            return False                   # 无事件循环 → controller 回落同步
        self._connectivity_payload = dict(payload or {})
        return bool(worker.start())

    def _connectivity_busy(self) -> None:
        self._toast("上一次测试还没结束：结果出来会自动更新", ok=True)

    def _apply_connectivity(self, result: Any, error: BaseException | None,
                            sections: Any = None) -> None:
        """worker 回来：播报结论并**立刻重画本页**（结果区就是客户要看的那句话）。"""
        if error is not None:
            message = "联通测试中断（" + type(error).__name__ + "）：" + str(error)[:120]
            ok = False
        else:
            message = str(getattr(result, "message", "") or "联通测试完成")
            ok = bool(getattr(result, "ok", False))
        self._toast(message, ok=ok)
        # 状态在 controller 里，不用整轮取数：原地重画本页（不 reset 滚动位置，客户刚在哪还哪）
        self._force_next_render = True
        try:
            self._render(self._controller.current(), force=True)
            self._update_status()
        except Exception:                          # pragma: no cover - 窗口已销毁
            pass

    def _start_probe(self) -> bool:
        worker = self._ensure_probe_worker()
        if worker is None:
            return False                   # 无事件循环 → controller 回落同步
        return bool(worker.start())

    def _probe_started(self) -> None:
        self._toast("正在探测上游（L1 列表 + L2 推理 / L3 流式，通常 10 秒内）…", ok=True)

    def _probe_busy(self) -> None:
        self._toast("上一次探测还没结束，已忽略这次点击", ok=True)

    def _apply_probe(self, result: Any, error: BaseException | None,
                     sections: Any = None) -> None:
        if error is not None:
            self._toast("探测失败（" + type(error).__name__ + "）：" + str(error)[:120], ok=False)
        elif result is None:
            self._toast("探测失败：没有拿到结果", ok=False)
        else:
            self._toast(str(getattr(result, "message", "") or "探测完成"),
                        ok=bool(getattr(result, "ok", False)))
        # 结论落库后再取一次数（探针表/容量卡片要立刻反映新结论）；GET 也不在主线程
        refresh = getattr(self, "_refresh_worker", None)
        if refresh is None or not getattr(refresh, "busy", False):
            self._request_refresh_async(("models", "token_plan"))

    # ── RC14 C 设备码绑定（轮询在后台线程；主线程只播报 + 重画）
    def _ensure_device_runner(self) -> bool:
        """首点建线程；轮询节奏由 ``device_flow`` 决定（5/15/30 秒，10 分钟停）。"""
        if getattr(self, "_device_thread", None) is not None and self._device_thread.is_alive():
            return True
        if not callable(getattr(self._root, "after", None)):
            return False
        import threading

        stop = threading.Event()
        self._device_stop = stop

        def work() -> None:
            try:
                result = self._controller.run_device_flow(stop=stop.is_set)
            except Exception as exc:                     # 后台线程绝不把异常抛给 Tk
                result = _device_error(exc)
            self._ui_call(lambda: self._apply_device(result))

        thread = threading.Thread(target=work, name="djb-device-flow", daemon=True)
        self._device_thread = thread
        thread.start()
        return True

    def _ui_call(self, fn: Any) -> None:
        """把回调排队回主线程（Tk 只能在自己的线程里改界面）。"""
        after = getattr(self._root, "after", None)
        if callable(after):
            try:
                after(0, fn)
                return
            except Exception:                            # pragma: no cover - 窗口已销毁
                pass
        fn()

    def _start_device_flow(self, _flow: Any = None) -> bool:
        if not self._controller.device_flow_enabled():
            return False
        return self._ensure_device_runner()

    def _apply_device(self, result: Any) -> None:
        message = str(getattr(result, "message", "") or "绑定流程结束")
        ok = bool(getattr(result, "ok", False))
        self._toast(message, ok=ok)
        # R2.1 ①：这条流程也可能是顶部横幅发起的「重新登录」→ 成功就回原页
        self._resume_after_login(ok)
        # 2026-11-19 客户报障「在浏览器确认绑定点击没反应」：旧实现只在**成功**时刷新，
        # 失败时页面一个字都不变，只闪一条 toast —— 客户看不到任何状态，就判成按钮坏了。
        self._request_refresh_async(("account", "overview", "access"))

    def _device_state_changed(self, flow: Any) -> None:
        """设备流程状态一变就排队回主线程（这是 worker 线程，不能碰 Tk）。"""
        try:
            data = flow.as_dict() if callable(getattr(flow, "as_dict", None)) else {}
        except Exception:                             # pragma: no cover - 形状异常
            data = {}
        snapshot = dict(data or {})
        self._ui_call(lambda: self._on_device_state(snapshot))

    def _on_device_state(self, data: Mapping[str, Any]) -> None:
        state = str(data.get("state") or "")
        url = str(data.get("verify_url") or "")
        code = str(data.get("user_code") or "")
        if state == "pending" and self._open_verify_url_once(url):
            self._toast("已在浏览器打开确认页" + ("（码 " + code + "）" if code else "")
                        + "：在网页里点「确认」，本机会自动接上", ok=True)
        # 每个中间状态都刷一次：设备码/状态字段要立刻反映（含失败原因）
        self._request_refresh_async(("account", "access"))

    def _open_verify_url_once(self, url: str) -> bool:
        """同一个确认链接只开一次浏览器（状态反复变化不许连开窗口）。"""
        url = str(url or "")
        if not url:
            return False
        seen = getattr(self, "_opened_verify_urls", None)
        if seen is None:
            seen = set()
            self._opened_verify_urls = seen
        if url in seen:
            return False
        seen.add(url)
        try:
            import webbrowser
            return bool(webbrowser.open(url))
        except Exception:                             # pragma: no cover - 无浏览器/无显示
            return False

    # ── RC14 I：读取模型清单（网络在 worker 线程，主线程只播报）
    def _ensure_discover_worker(self) -> Any:
        """惰性建「读取模型清单」worker（与探针/更新同一个 RefreshWorker 模板）。"""
        worker = getattr(self, "_discover_worker", None)
        if worker is not None:
            return worker
        root = self._root
        if not callable(getattr(root, "after", None)):
            return None
        from .refresh_worker import RefreshWorker

        worker = RefreshWorker(lambda sections=None: self._controller.run_discover(
            str(self._controller.drafts.get("base_url") or "").strip(),
            str(self._controller.drafts.get("protocol") or "").strip(),
            str(self._controller.drafts.get("upstream_secret") or "").strip()),
            apply=self._apply_discover, on_start=self._discover_started,
            on_busy=self._discover_busy,
            schedule=lambda delay, fn: root.after(int(delay), fn),
            cancel=lambda job: root.after_cancel(job))
        self._discover_worker = worker
        self._controller.discover_via = self._start_discover
        return worker

    def _start_discover(self, base_url: str, protocol: str, secret: str) -> bool:
        worker = self._ensure_discover_worker()
        if worker is None:
            return False                   # 无事件循环 → controller 回落同步
        return bool(worker.start())

    def _discover_started(self) -> None:
        self._toast("正在读取上游模型清单（GET {base}/models）…", ok=True)

    def _discover_busy(self) -> None:
        self._toast("上一次读取还没结束，已忽略这次点击", ok=True)

    def _apply_discover(self, result: Any, error: BaseException | None,
                        sections: Any = None) -> None:
        if error is not None:
            self._toast("读取失败（" + type(error).__name__ + "）：" + str(error)[:120], ok=False)
        else:
            self._toast(str(getattr(result, "message", "") or "读取完成"),
                        ok=bool(getattr(result, "ok", False)))
            if bool(getattr(result, "ok", False)):
                self._absorb_forget(result)
                self._request_refresh_async(("token_plan",))

    def _absorb_forget(self, result: Any) -> None:
        """动作说「这些字段已经落盘」→ 立刻清框，并让下一次重画把结论画出来（F5/F3）。

        异步通道（worker）的结果不走 ``_on_action``，这里补同一套处理：否则密钥会一直
        留在框里，客户以为没保存。
        """
        payload = getattr(result, "payload", None)
        forget = payload.get("forget") if isinstance(payload, Mapping) else None
        self._forget_keys = {str(name) for name in (forget or [])}
        for name in self._forget_keys:
            variable = self._vars.get(name)
            try:
                if variable is not None:
                    variable.set("")
            except Exception:                      # noqa: BLE001 - 控件已销毁
                continue
        if self._forget_keys:
            self._force_next_render = True

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
                self._render(view, force=True)          # 换肤：颜色要真的重取
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
            self._render(view, force=True)               # 宽度翻转：布局要重排
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
        self._tables = []                    # [(Table, Treeview)]：主表 + 附加表（见 _render_table）
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
        pairs = list(getattr(self, "_tables", ()) or ())
        if not pairs:
            table = getattr(self, "_table", None)
            tree = getattr(self, "_tree", None)
            if table is not None and tree is not None:
                pairs = [(table, tree)]
        for table, tree in pairs:
            for row in getattr(table, "rows", ()) or ():
                iid = row.key or ""
                if not iid:
                    continue
                compact = getattr(row, "narrow", ())
                values = (compact if narrow and compact and len(compact) == len(row.cells)
                          else row.cells)
                try:
                    tree.item(iid, values=values)
                except Exception:                    # pragma: no cover - 行已被重建
                    pass

    def _on_nav_combo(self, _event=None) -> None:
        value = str(self._nav_combo.get() or "")
        if "·" in value:
            index = value.split("·", 1)[0].strip()
            for position, key in enumerate(self._ia_order, start=1):
                if str(position) == index:
                    self.show_view(key)
                    return

    def _sync_nav(self, key: str) -> None:
        for name, item in self._nav_buttons.items():
            item.set_active(name == key)
        combo = getattr(self, "_nav_combo", None)
        if combo is not None:
            try:                                           # 窄窗下拉与侧栏保持同一状态
                index = self._ia_order.index(key) + 1
                combo.set(str(index) + " · " + ia.page(key).title)
            except Exception:                              # pragma: no cover - 视图名变了
                pass

    # ── 状态（状态条 / 徽标 / 侧栏身份块 / 页头时刻）
    def _update_status(self) -> None:
        """把快照里的状态一次写进四处：状态条消息、状态徽标、身份块、页头「数据截至」。"""
        self._sync_auth_banner()                     # R2.1 ①：401 → 顶部横幅
        snapshot = self._controller.snapshot
        # D4：失败提示粘住期间不许被「已连接 · …」这类常规状态行覆盖
        sticky = getattr(self, "_sticky_error", "")
        self._status.configure(
            text=(sticky or self._status_line()),
            foreground=dual.color("red" if sticky else "muted_text"))
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
        meta = (name + " · " + vram) if device else "显卡：暂不可用"
        self._ident_meta.configure(text=self._fit_text(self._ident_meta, meta))

    def _update_clock(self) -> None:
        """页头/头上的「数据截至」：只用 ``Snapshot.fetched_at``，没有就写「—」。"""
        stamp = short_clock(self._controller.snapshot.fetched_at)
        self._sync_label.configure(text="同步 " + stamp)
        self._snapshot_label.configure(text="数据截至 " + stamp)

    def _status_line(self) -> str:
        """状态条左侧消息：窄窗只留前两段（否则会挤到右侧的 GPU/版本上去）。

        ``DJB_UI_IA`` 写了但没识别（``ia.ignored``）时追加一句：不静默当默认。
        """
        line = sync_shortcut_hint(self._controller.status_line())
        decision = getattr(self, "_ia", None)
        if decision is not None and getattr(decision, "ignored", False):
            line = line + " · DJB_UI_IA=" + str(decision.raw) + " 未识别，已按 share 生效"
        if not getattr(self, "_narrow", False):
            return line
        parts = [part.strip() for part in line.split("·") if part.strip()]
        return " · ".join(parts[:2])

    def _status_right_text(self, *, compact: bool | None = None) -> str:
        """状态条右侧：GPU 利用率 · 显存 · 版本 · 主题（缺值写「—」，不写 0）。

        ``compact`` 显式要求两段式（版本 + 主题）；``None`` 时看 ``_narrow`` /
        ``_status_compact``（见 ``_fit_status_right``）。
        """
        from ..facts import CLIENT_VERSION
        snapshot = self._controller.snapshot
        gpu = snapshot_section(snapshot, "gpu")
        devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
        device = devices[0] if devices and isinstance(devices[0], Mapping) else {}
        util = device.get("utilization_pct")
        used, total = device.get("memory_used_mb"), device.get("memory_total_mb")
        theme_name = "日间" if self._theme_name == "day" else "夜间"
        compact = bool(compact if compact is not None else getattr(self, "_status_compact", False))
        if getattr(self, "_narrow", False) or compact:
            # 窄窗 / 完整文案被裁：GPU/显存留给主卡，状态条只留版本与主题
            return "v" + CLIENT_VERSION + "  ·  " + theme_name
        parts = ["GPU " + (str(int(float(util))) + "%" if is_number(util) else "—"),
                 "显存 " + ("%.1f/%.0f GB" % (float(used) / 1024.0, float(total) / 1024.0)
                          if is_number(used) and is_number(total) else "—"),
                 "v" + CLIENT_VERSION,
                 theme_name]
        return "  ·  ".join(parts)

    def _fit_status_right(self) -> None:
        """状态条右侧自适应：**实测**放不下就退到「版本 + 主题」，绝不让文字被硬裁。

        2026-11-24（R2.1 ⑤ 之后字阶 +1px）：900px 宽窗下四段式需求 273 px、实得 197 px，
        旧逻辑只在 <760 才退两段，于是状态条被硬裁。这里按 label 的真实需求宽度判断，
        窗变宽时再自动恢复四段（每次 ``<Configure>`` 重算，不缓存结论）。
        """
        label = getattr(self, "_status_right", None)
        if label is None or getattr(self, "_fitting_status", False):
            return
        self._fitting_status = True
        try:
            label.update_idletasks()
            if not label.winfo_ismapped() or getattr(self, "_narrow", False):
                return
            clipped = label.winfo_reqwidth() > label.winfo_width() + 2
            if clipped and not getattr(self, "_status_compact", False):
                self._status_compact = True
                label.configure(text=self._status_right_text(compact=True))
            elif not clipped and getattr(self, "_status_compact", False):
                full = self._status_right_text(compact=False)
                if self._text_width(label, full) <= label.winfo_width() + 2:
                    self._status_compact = False
                    label.configure(text=full)
        except Exception:                                  # pragma: no cover - 控件已销毁
            return
        finally:
            self._fitting_status = False

    def _text_width(self, label, text: str) -> int:
        """按 label 自己的字体量文字宽度（px）；量不了就退回 reqwidth 估算。"""
        try:
            font = self._tk.font.Font(font=label.cget("font"))
            return int(font.measure(text))
        except Exception:                                  # pragma: no cover - 字体异常
            return 0

    def _fit_text(self, label, text: str) -> str:
        """把文字截到控件**当前**可用宽度内（超出加「…」），不让 Tk 硬裁。

        2026-11-24：字阶 +1px 后 900px 窗里侧栏「显卡名 · 显存」超出列宽 13 px。
        """
        try:
            width = int(label.winfo_width())
            if width <= 1:
                return text
            if self._text_width(label, text) <= width + 2:
                return text
            cut = text
            while cut and self._text_width(label, cut + "…") > width + 2:
                cut = cut[:-1]
            return (cut + "…") if cut else "…"
        except Exception:                                  # pragma: no cover - 控件已销毁
            return text

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

    def _remember_form_values(self) -> None:
        """把**当前页**输入框里的值存进 ``_form_keep``（重画前调用；CR-20261119 F2）。

        只存本页可编辑字段；控件已销毁/值读不到就跳过。``_forget_keys`` 里的字段
        （例如刚保存成功的密钥）**不存**，等于下次重画就清空。空串照存：客户主动清空
        就是清空，不许被配置里的旧值顶回来。
        """
        view_key = getattr(self, "_render_view_key", "") or ""
        entries = getattr(self, "_entries", None) or {}
        if not view_key or not entries:
            return
        skip = getattr(self, "_forget_keys", set()) or set()
        values: dict[str, str] = {}
        for key, entry in entries.items():
            if key in skip:
                continue
            try:
                values[str(key)] = str(entry.get())
            except Exception:                      # noqa: BLE001 - 控件已销毁
                continue
        # 整页覆盖（不是 update）：被 skip 的字段因此从暂存里消失。
        self._form_keep[view_key] = values

    def _clear(self) -> None:
        for child in self._content.winfo_children():
            child.destroy()
        self._fields.clear()
        self._vars.clear()
        self._reveal_buttons.clear()         # 按钮随旧控件销毁，不能留悬空引用
        self._entries.clear()
        #: 渲染时输入框里的初始文本（判「改过但没保存」用；自动刷新要给编辑让路）
        self._entry_initial = {}
        self._filter_widgets = []
        self._switch_widgets = {}
        self._row_notes.clear()
        self._tree = None
        self._detail = None
        self._table = None
        self._tables = []                    # [(Table, Treeview)]：主表 + 附加表（见 _render_table）
        self._wrap_targets = []

    # ── 渲染入口
    def _on_escape(self, event: Any = None) -> None:
        """Esc = 跳过当前引导步骤（设计 §3.4）；引导不在时什么都不做。"""
        try:
            if self._controller.onboarding_visible():
                self._on_action("onboarding.skip")
        except Exception:                                # 状态读不到：不要拦 Esc
            return None
        return None

    def _render_onboarding(self, body: Any) -> None:
        """首启引导面板（应用内、非模态）。渲染失败绝不影响主界面。"""
        try:
            panel = self._controller.onboarding_panel()
        except Exception:                                # 状态文件坏了：不画，别崩窗口
            panel = None
        if panel is None:
            return
        tk = self._tk
        kit = self._kit
        ground = dual.color("surface")
        card = kit.RoundedCard(body, ground=dual.color("bg"), pad=(16, 12))
        card.pack(fill="x", pady=(0, 10))
        inner = card.body
        tk.Label(inner, text=panel.title, font=self._fonts.sans(kit.px("h2"), bold=True),
                 foreground=dual.color("ink"), background=ground, anchor="w").pack(anchor="w")
        self._wrap(tk.Label(inner, text=panel.body, font=self._fonts.sans(kit.px("small")),
                            foreground=dual.color("muted_text"), background=ground,
                            anchor="w", justify="left", wraplength=self._wrap_hint())).pack(
                                anchor="w", pady=(4, 8))
        row = tk.Frame(inner, background=ground)
        row.pack(anchor="w")
        for action in panel.actions:
            button = kit.Button(row, action.label,
                                (lambda key: (lambda: self._on_action(key)))(action.key),
                                kind="primary" if action.primary else "ghost",
                                ground=ground, px=kit.px("small"),
                                height=kit.btn_height("lg" if action.primary else "md",
                                                      scale=self._scale), pad=12,
                                enabled=bool(action.enabled))
            self._a11y_name(button, action.label)      # 引导卡的动作按钮同样要能念出来
            button.pack(side="left", padx=(0, 8))
        self._onboarding_panel = card

    def _should_skip_render(self, view: View) -> bool:
        """这一版视图和屏幕上那版**一模一样**时，不重建控件（客户反馈「经常卡顿」）。

        为什么值得单独一条：``_render`` 先 ``_clear()``（销毁本页全部控件，含 Treeview），
        再逐块重建，最后把整棵树推给读屏（MSAA）。数据没变也照做的话，每 10 秒就有一次
        「全量拆装 + 读屏重建」——在慢机器上就是肉眼可见的停顿。空转的重画只该更新
        状态栏那点文字（那部分在 ``_update_status`` 里，与这里无关）。

        宽度档位翻转（窄/宽、双列/单列）时**不能**跳过：布局本身变了。
        """
        previous = getattr(self, "_last_view", None)
        if previous is None or previous != view:
            return False
        if getattr(self, "_body", None) is None:
            return False                                   # 还没画过，必须画
        try:
            if self._rendered_narrow != self._is_narrow():
                return False
            if self._rendered_two_col != self._two_col():
                return False
        except Exception:                                  # pragma: no cover - 无窗口
            return False
        # 有密码框被点成「显示」时**不许跳过**：跳过就等于没有人把掩码装回去，
        # 上一次点开的明文会一直留在屏幕上（tests/test_desktop_reveal.py 的红用例）。
        for button in getattr(self, "_reveal_buttons", {}).values():
            if str(getattr(button, "text", "")) == "隐藏":
                return False
        return True

    def _render(self, view: View, *, force: bool = False) -> None:
        """按视图能力分派：带 hero/chart/rail 的（总览）走设计样双列，其余走通用卡片版面。

        RC15-039 #1：渲染前把**本页**的字段级错误贴上去（纯函数 ``with_field_errors``），
        渲染后把焦点送到第一个错误字段——客户点完「保存」不用自己找哪一行错了。

        ``force=True``：换肤 / 换布局 / 显式重画（``show_view``）时不许走「内容没变就跳过」。
        """
        tk = self._tk
        kit = self._kit
        patched = with_field_errors(view, self._field_errors)
        if not force and not self._force_next_render and self._should_skip_render(patched):
            return
        self._force_next_render = False
        view = patched
        self._remember_form_values()                 # F2：先把客户已经打好的字留住
        self._last_view = view
        self._render_view_key = view.key
        self._in_render = True
        self._rendered_narrow = self._is_narrow()
        self._rendered_two_col = self._two_col()
        self._clear()
        self._view_title.configure(text=view.title)
        self._set_font(self._page_kicker,
                       self._ia_kickers.get(view.key, view.title), kit.px("micro"))
        pad = (12, 6) if self._rendered_narrow else (26, 6)
        self._content_pad = pad
        body = tk.Frame(self._content, background=dual.color("bg"), bd=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        self._body = body
        # RC14 B：首次引导面板 —— **不是模态**：画在正文最上方，不拦点击、不挡导航；
        # 每步都有「跳过这一步」，第一步还有「跳过设置，直接进入」。开关关 → 不画。
        self._render_onboarding(body)
        if view.subtitle:
            self._wrap(tk.Label(body, text=view.subtitle, font=self._fonts.sans(kit.px("small")),
                              foreground=dual.color("muted_text"), background=dual.color("bg"),
                              anchor="w", justify="left", wraplength=self._wrap_hint())).pack(
                                  anchor="w", pady=(0, 4))
        for text in view.unavailable:
            self._wrap(tk.Label(body, text="! " + text, font=self._fonts.sans(kit.px("small")),
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
            self._wrap(tk.Label(body, text="· " + note, font=self._fonts.sans(kit.px("micro")),
                              foreground=dual.color("muted_text"), background=dual.color("bg"),
                              anchor="w", justify="left", wraplength=self._wrap_hint())).pack(
                                  anchor="w", pady=(2, 0))
        self._in_render = False
        self._sync_content_width()                   # B4：内容重排后把宽度钉回画布宽
        self._sync_auth_banner()                     # R2.1 ①：401 → 顶部横幅
        self._refresh_a11y(view)                     # 读屏树跟着这一版数据走（不发焦点事件）
        if view.focus_field:
            self._focus_field(view.focus_field)

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

    #: 换行宽「变大」的最小步长（px）。同一帧里列宽会因滚动条出现/消失抖 1px，
    #: 若逐像素追着改：wraplength → 行数 → 高度 → 滚动条 → 列宽 形成回环，
    #: ``update_idletasks`` **永不返回**（0.3.6 接入页长提示实测复现）。两个钉子：
    #: tests/test_desktop_payout.py::test_fit_wrap_follows_shrink_but_steps_growth
    #: 与 ::test_real_tk_access_page_folds_advanced_and_never_prefills_mask（修前会挂死）。
    #: 策略：**变窄立刻跟**（保证不裁字），变宽按 24px 步进（抖动吃掉，最多多折一行）。
    WRAP_GROW_STEP = 24

    def _fit_wrap(self, label: Any, width: int | None = None) -> None:
        """把单条 label 的换行宽钳到它实际分到的宽度（变窄立刻跟、变宽按步进）。"""
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
        if current <= 0 or span < current or span - current >= self.WRAP_GROW_STEP:
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
        for extra in view.tables:
            self._render_table(extra, parent=left, max_rows=4, primary=False)
        for card in view.rail:
            self._render_rail_card(right, card)
        if view.actions:
            self._render_action_bar(left, view.actions, pady=(2, 0))

    def _render_action_bar(self, parent: Any, actions: Any, *,
                           pady: tuple[int, int] = (0, 0),
                           titles: bool = True) -> None:
        """动作栏。D6：带 ``group`` 的动作各自成块（小标题 + 独立一行按钮）。

        客户原话「UI 上拆分绑定节点和提交 USDT 地址可以么，隔离两个动作」——原来整页只有
        一条按钮栏，用户填完地址顺手点了「绑定网页端」，会以为地址跟着提交了。现在两组
        之间有小标题和间距，按错的可能性肉眼可见地降低。
        """
        tk = self._tk
        from .views import action_groups

        if not titles:                               # 分区渲染：外层已画过组标题
            bar = tk.Frame(parent, background=dual.color("bg"))
            bar.pack(fill="x", pady=pady)
            for action in actions:
                self._render_action(bar, action)
            return
        groups = action_groups(actions)
        if not any(title for title, _ in groups):
            # 老行为（无分组）：一条栏，逐字节等价
            bar = tk.Frame(parent, background=dual.color("bg"))
            bar.pack(fill="x", pady=pady)
            for action in actions:
                self._render_action(bar, action)
            return
        wrap = tk.Frame(parent, background=dual.color("bg"))
        wrap.pack(fill="x", pady=pady)
        for title, members in groups:
            if title:
                tk.Label(wrap, text=title, font=self._fonts.sans(self._kit.px("micro"), bold=True),
                         foreground=dual.color("muted_text"), background=dual.color("bg"),
                         anchor="w").pack(fill="x", pady=(6, 2))
            row = tk.Frame(wrap, background=dual.color("bg"))
            row.pack(fill="x")
            for action in members:
                self._render_action(row, action)

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
        tk.Label(text, text=hero.kicker, font=fonts.for_text(hero.kicker, kit.px("micro")),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(fill="x")
        head = tk.Frame(text, background=surface)
        head.pack(fill="x", pady=(2, 0))
        tk.Label(head, text=hero.title, font=fonts.sans(kit.px("hero"), bold=True),
                 foreground=dual.color("ink"), background=surface, anchor="w").pack(side="left")
        if hero.chip:
            kit.Chip(head, hero.chip, tone=hero.chip_tone, ground=surface,
                     px=kit.px("micro"), height=19).pack(side="left", padx=(10, 0))
        body_label = tk.Label(text, text=hero.body, font=fonts.sans(kit.px("small")),
                              foreground=dual.color("ink2"), background=surface, anchor="w",
                              justify="left")
        body_label.pack(fill="x", pady=(4, 0))
        runtime = tk.Frame(text, background=surface)
        runtime.pack(fill="x", pady=(3, 0))
        tk.Label(runtime, text=hero.runtime_label, font=fonts.sans(kit.px("micro")),
                 foreground=dual.color("muted_text"), background=surface).pack(side="left")
        tk.Label(runtime, text=hero.runtime,
                 font=fonts.for_text(hero.runtime, kit.px("body"), bold=True),
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
                                    value_px=kit.px("h1"), align="w")
                cell.grid(row=0, column=index, sticky="nsew", padx=(0, 10))
        if hero.actions or hero.hint:
            foot = tk.Frame(body, background=surface)
            # B6：底部留 2px，别让 1px 描边贴着卡片下缘/下一行
            foot.pack(fill="x", pady=(7, 2))
            if hero.hint:
                # B6：提示先 pack（side=right）并限宽换行 —— 旧实现里它排在按钮后面，
                # 剩余宽度不够时被内容画布右缘裁掉（「已授权 1 个客户端」就是这样丢字的）。
                tk.Label(foot, text=hero.hint, font=fonts.sans(kit.px("micro")),
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
                 font=fonts.for_text(chart.kicker or "CHART", kit.px("micro")),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(fill="x")
        tk.Label(left, text=chart.title, font=fonts.sans(kit.px("h2"), bold=True),
                 foreground=dual.color("ink"), background=surface, anchor="w").pack(fill="x")
        if chart.caption:
            tk.Label(head, text=chart.caption, font=fonts.sans(kit.px("micro")),
                     foreground=dual.color("muted_text"), background=surface).pack(
                         side="right", pady=(10, 0))
        if chart.empty:
            tk.Label(body, text=chart.empty, font=fonts.sans(kit.px("small")),
                     foreground=dual.color("amber_text"), background=surface, anchor="w",
                     justify="left", wraplength=520).pack(anchor="w", pady=(12, 4))
            return
        bars = kit.Bars(body, bars=max(1, len(chart.values)), height=78, gap=4, ground=surface)
        bars.pack(fill="x", pady=(8, 0))
        bars.set(chart.values, axis=chart.labels)
        foot = tk.Frame(body, background=surface)
        foot.pack(fill="x", pady=(2, 0))
        tk.Label(foot, text=chart.peak, font=fonts.for_text(chart.peak, kit.px("micro")),
                 foreground=dual.color("mint_text"), background=surface).pack(side="left")
        tk.Label(foot, text=chart.foot, font=fonts.sans(kit.px("micro")),
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
        tk.Label(head, text=card.kicker, font=fonts.for_text(card.kicker, kit.px("micro")),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(side="left")
        if card.note:
            tk.Label(head, text=card.note,
                     font=fonts.for_text(card.note, kit.px("micro"), bold=True),
                     foreground=dual.color("mint_text"), background=surface).pack(side="right")
        title = tk.Frame(body, background=surface)
        title.pack(fill="x", pady=(2, 0))
        tk.Label(title, text=card.title, font=fonts.sans(kit.px("h2"), bold=True),
                 foreground=dual.color("ink"), background=surface, anchor="w").pack(side="left")
        if card.badge:
            kit.Chip(title, card.badge, tone=card.badge_tone, ground=surface,
                     px=kit.px("micro"), height=18).pack(side="right")
        if card.kind == "stats" and card.stats:
            grid = tk.Frame(body, background=surface)
            grid.pack(fill="x", pady=(6, 0))
            for index, stat in enumerate(card.stats):
                row, column = divmod(index, 2)
                grid.columnconfigure(column, weight=1, uniform="today")
                kit.StatCell(grid, label=stat.label, value=stat.value, unit=stat.unit,
                             note=stat.note, ground=surface, value_px=kit.px("h1"),
                             mono_value=True).grid(row=row, column=column, sticky="nsew",
                                                  padx=(0, 10), pady=(0, 2))
        if card.kind == "setup" and card.fields:
            form = tk.Frame(body, background=surface)
            form.pack(fill="x", pady=(8, 0))
            for field in card.fields:
                line = tk.Frame(form, background=surface)
                line.pack(fill="x", pady=(0, 5))
                tk.Label(line, text=field.label, font=fonts.sans(kit.px("micro")),
                         foreground=dual.color("muted_text"), background=surface,
                         anchor="w", width=12).pack(side="left")
                tk.Label(line, text=field.value,
                         font=fonts.for_text(field.value, kit.px("small")),
                         foreground=dual.color("ink"), background=surface,
                         anchor="w").pack(side="left", fill="x", expand=True)
        if card.kind == "throttle":
            box_rows = tk.Frame(body, background=surface)
            box_rows.pack(fill="x", pady=(8, 0))
            for label, value, tone in card.rows:
                line = tk.Frame(box_rows, background=surface)
                line.pack(fill="x", pady=(0, 2))
                tk.Label(line, text=label, font=fonts.sans(kit.px("micro")),
                         foreground=dual.color("muted_text"), background=surface,
                         anchor="w").pack(side="left")
                colour = {"mint": "mint_text", "amber": "amber_text", "red": "red_text",
                          "muted": "muted_text"}.get(tone, "ink")
                tk.Label(line, text=value, font=fonts.for_text(value, kit.px("small")),
                         foreground=dual.color(colour), background=surface).pack(side="right")
            if card.toggle is not None:
                line = tk.Frame(box_rows, background=surface)
                line.pack(fill="x", pady=(2, 0))
                tk.Label(line, text=card.toggle[0], font=fonts.sans(kit.px("micro")),
                         foreground=dual.color("muted_text"), background=surface).pack(side="left")
                # 开关只读：点它等于「去让路页改」，这里不假装能改（无对应后端动作）
                toggle = kit.Switch(line, on=bool(card.toggle[1]), ground=surface,
                                    command=lambda _on: self.show_view("yield"))
                self._a11y_name(toggle, str(card.toggle[0]) + "（只读，点一下去让路页改）")
                toggle.pack(side="right")
        if card.progress is not None:
            # 进度轨（设计 G-05）：一行标签 + 6px 圆角轨，比例直接来自服务端用量
            label, ratio, value_text = card.progress
            head_row = tk.Frame(body, background=surface)
            head_row.pack(fill="x", pady=(4, 0))
            tk.Label(head_row, text=label, font=fonts.sans(kit.px("micro")),
                     foreground=dual.color("muted_text"), background=surface).pack(side="left")
            tk.Label(head_row, text=value_text,
                     font=fonts.for_text(value_text, kit.px("micro")),
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
            tk.Label(body, text=card.foot, font=fonts.sans(kit.px("micro")),
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
        for item in view.switches:                   # RC15-039 #4：本机界面开关
            self._render_switch(item)
        # 有表单的页：卡片 → 字段 → 动作（保存）→ 表格；没有表单的页：卡片 → 动作 → 表格
        sections = self._section_order(view)
        if sections:
            # 2026-11-19 客户报障：账户页「绑定节点」与「USDT 收款地址」必须完全隔离。
            # 有分区的页改成「每组各成一块」：标题 → 该组字段 → 该组按钮，组与组之间
            # 不再共享任何一条按钮栏。没有 group 的页走的还是下面这条老路径（逐字节等价）。
            self._render_sections(view, sections)
            rest = tuple(item for item in view.actions
                         if not str(getattr(item, "group", "") or ""))
            if rest:
                self._render_action_bar(self._body, rest, pady=(0, 12))
        else:
            if view.fields:
                self._render_fields(view.fields, view.key)
            if view.actions:
                self._render_action_bar(self._body, view.actions, pady=(0, 12))
        for item in view.filters:                    # RC15-039 #5：表格上方的下拉筛选
            self._render_filter(item)
        if view.table is not None:
            self._render_table(view.table)
        for extra in view.tables:                     # RC14 D：模型与探针页的第二张表
            self._render_table(extra, primary=False)

    def _section_order(self, view: View) -> tuple[str, ...]:
        """页面里的分区标题顺序（字段在前、动作在后，按首次出现排）。空 = 老行为。"""
        order: list[str] = []
        for item in tuple(view.fields) + tuple(view.actions):
            title = str(getattr(item, "group", "") or "")
            if title and title not in order:
                order.append(title)
        return tuple(order)

    def _render_sections(self, view: View, sections: Sequence[str]) -> None:
        """按分区画：标题 → 该组字段 → 该组按钮。组内按钮**不再**重复小标题。"""
        tk = self._tk
        plain = tuple(item for item in view.fields
                      if not str(getattr(item, "group", "") or ""))
        if plain:                                    # 兜底：没分区的字段照旧先渲染
            self._render_fields(plain, view.key)
        for title in sections:
            block = tk.Frame(self._body, background=dual.color("bg"))
            block.pack(fill="x", pady=(0, 6))
            tk.Label(block, text=title, font=self._fonts.sans(self._kit.px("h2"), bold=True),
                     foreground=dual.color("ink"), background=dual.color("bg"),
                     anchor="w").pack(fill="x", pady=(10, 6))
            fields = tuple(item for item in view.fields
                           if str(getattr(item, "group", "") or "") == title)
            if fields:
                self._render_fields(fields, view.key, parent=block)
            actions = tuple(item for item in view.actions
                            if str(getattr(item, "group", "") or "") == title)
            if actions:
                self._render_action_bar(block, actions, pady=(0, 10), titles=False)

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
        tk.Label(body, text=card.title, font=fonts.sans(kit.px("h2"), bold=True),
                 foreground=dual.color("muted_text"), background=surface, anchor="w").pack(fill="x")
        tone = {"mint": "mint_text", "amber": "amber_text", "red": "red_text",
                "muted": "muted_text"}.get(card.tone, "ink")
        value = tk.Label(body, text=card.value, font=fonts.sans(kit.px("h1"), bold=True),
                         foreground=dual.color(tone), background=surface, anchor="w",
                         justify="left")
        value.pack(fill="x", pady=(3, 0))
        meta = None
        if card.meta:
            meta = tk.Label(body, text=card.meta, font=fonts.sans(kit.px("micro")),
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

    #: 可读名称的属性名（读屏软件读不到 Tk 子控件，但导出/证据/测试能读到；
    #: 真 UIA 命名需要 provider，见 RC15-039 #31 的边界说明）
    A11Y_NAME_ATTR = "djb_accessible_name"

    def _a11y_name(self, widget: Any, text: str) -> Any:
        """给控件挂一个**人类可读的名称**（#31 最小切片的底座，也供本条筛选控件用）。"""
        try:
            setattr(widget, self.A11Y_NAME_ATTR, str(text))
        except Exception:                            # pragma: no cover - 控件已销毁
            pass
        return widget

    def _render_switch(self, item: Any) -> None:
        """一条开关：真 Switch（28×15，Tab 可达、空格/回车可切）+ 标签 + 说明。"""
        tk, kit = self._tk, self._kit
        base = dual.color("bg")
        bar = tk.Frame(self._body, background=base, bd=0, highlightthickness=0)
        bar.pack(fill="x", pady=(0, 8))
        switch = kit.Switch(bar, on=bool(item.value), ground=base,
                            command=lambda on, target=item: self._on_toggle(target, bool(on)))
        switch.pack(side="left", padx=(2, 8))
        tk.Label(bar, text=item.label, font=self._fonts.sans(kit.px("small")),
                 foreground=dual.color("ink"), background=base).pack(side="left")
        if item.hint:
            tk.Label(bar, text=item.hint, font=self._fonts.sans(kit.px("micro")),
                     foreground=dual.color("muted_text"), background=base).pack(
                         side="left", padx=(10, 0))
        self._switch_widgets[item.key] = switch
        self._a11y_name(switch, item.label + "（开关键）")

    def _on_toggle(self, item: Any, on: bool) -> None:
        """开关切换 → 动作层 → 立即落偏好 → 重画（状态以重画后的视图为准，不猜）。"""
        self._controller.set_draft(item.key, "on" if on else "off")
        self._on_action(item.action or ("toggle." + item.key))
        if item.key == "auto_refresh":
            try:
                dual.save_flag("auto_refresh", bool(on), self._prefs_home)
            except Exception as exc:                 # noqa: BLE001 - 偏好写不进去也要能用
                self._toast("自动刷新已改，但偏好没写进 desktop-prefs.json（"
                            + type(exc).__name__ + "）：下次启动会回到默认", ok=False)
            if on:
                self._schedule_auto_refresh()
            else:
                self._cancel_auto_refresh()          # 关掉 = 一个定时器都不留

    def _render_filter(self, item: Any) -> None:
        """下拉筛选：一条标签 + 只读 Combobox（键盘可达：Tab 到、↑↓ 选、回车生效）。"""
        tk, ttk, kit = self._tk, self._ttk, self._kit
        base = dual.color("bg")
        bar = tk.Frame(self._body, background=base, bd=0, highlightthickness=0)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text=item.label, font=self._fonts.sans(kit.px("small")),
                 foreground=dual.color("muted_text"), background=base).pack(
                     side="left", padx=(2, 8))
        values = [text for _value, text in item.options]
        box = ttk.Combobox(bar, state="readonly", values=values, width=16, takefocus=True,
                           font=self._fonts.sans(kit.px("small")))
        box.set(item.current_text())
        box.pack(side="left")
        # 选中即生效（下拉本身就是「确认」动作，不再要一次点击）
        box.bind("<<ComboboxSelected>>",
                 lambda _event, target=item, widget=box: self._on_filter(target, widget))
        if item.hint:
            tk.Label(bar, text=item.hint, font=self._fonts.sans(kit.px("micro")),
                     foreground=dual.color("muted_text"), background=base).pack(
                         side="left", padx=(10, 0))
        try:
            self._filter_widgets.append(box)
        except AttributeError:                     # pragma: no cover - 还没渲染过
            self._filter_widgets = [box]
        self._a11y_name(box, item.label + "筛选")

    def _on_filter(self, item: Any, widget: Any) -> None:
        """把下拉的选择值交给动作层（草稿 → 动作 → 重画 + 播报）。"""
        index = int(widget.current())
        if index < 0 or index >= len(item.options):
            return
        value = item.options[index][0]
        self._controller.set_draft(item.key, value)
        self._on_action(item.action or ("filter." + item.key))

    def _render_action(self, parent: Any, action: Action, *,
                       ground: str | None = None) -> None:
        """动作按钮：主/次两档；置灰时旁边写原因（不隐藏，C9 既有行为）。"""
        kit, tk = self._kit, self._tk
        base = ground or dual.color("bg")
        target = (lambda name=action.nav: self.show_view(name)) if action.nav else \
            (lambda key=action.key: self._on_action(key))
        # R2.1 ⑥：主操作 44、次级 36（下限 32），危险动作画成红按钮且动作层二次确认
        kind = "primary" if action.primary else ("danger" if action.danger else "ghost")
        size = "lg" if action.primary else "md"
        button = kit.Button(parent, action.label, target, kind=kind, ground=base,
                            px=kit.px("small"), height=kit.btn_height(size, scale=self._scale),
                            pad=12, enabled=action.enabled)
        self._a11y_name(button, action.label if action.enabled
                        else action.label + "（不可用：" + (action.reason or "条件不满足") + "）")
        button.pack(side="left", padx=(0, 8))
        if not action.enabled and action.reason:
            # 2026-11-24：置灰原因以前是「自然宽度」，900px 窗里被硬裁成半句
            # （'节点进程未运行：先启动节点（「重新注册」只在节点被云端清' 412 px / 166 px）。
            # 走与表单提示同一套「按实际列宽换行」机制，不再切尾巴。
            self._wrap_to_column(tk.Label(
                parent, text=action.reason, font=self._fonts.sans(kit.px("micro")),
                foreground=dual.color("muted_text"), background=base,
                anchor="w", justify="left",
                wraplength=self._wrap_hint())).pack(side="left", padx=(0, 10))

    def _render_table(self, table: Table, parent: Any = None,
                      max_rows: int = 12, primary: bool = True) -> None:
        """表格：保留 ttk.Treeview（``_tree`` / ``_row_cells`` 是既有契约），只换外观。

        ``primary=False`` 是**附加表**（RC14 D：模型与探针页的云端目录）：照常渲染并登记到
        ``self._tables``，但**不覆盖** ``_table`` / ``_tree`` / ``_row_cells`` / ``_detail``——
        详情抽屉、窄窗紧凑文案都只服务主表，附加表抢走它们会让探针表点开没反应。
        """
        if primary:
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
            # R2.4 C3：表头点一下就按这一列排（再点一次反序）。排序只影响**当前视图**，
            # 不改数据、不落盘 —— 下一次刷新按数据源顺序重画。
            tree.heading(column, text=column, anchor="w",
                         command=lambda col=column, view=tree: self._sort_by_column(view, col))
            tree.column(column, anchor="w", width=120, stretch=True)
        # 每一张表（含附加表）都要有名字：读屏靠它知道「这是哪张表、有哪些列」（#31①）。
        # 附加表**没有**详情抽屉，名字里就不许承诺 Enter 能开详情（#32：提示语跟着真实路径写）
        self._a11y_name(tree, "表格：" + "、".join(table.columns)
                        + ("（↑↓ 选择，Home/End 到首尾，Enter 打开详情）" if primary
                           else "（附加表：↑↓ 可以看行，没有详情抽屉）"))
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
            tk.Label(body, text=table.empty, font=self._fonts.sans(kit.px("small")),
                     foreground=dual.color("muted_text"), background=dual.color("surface"),
                     anchor="w").pack(anchor="w", pady=(10, 2))
        if not primary:
            # 附加表：只登记（列宽/窄窗适配要能一起遍历），不抢主表的契约字段
            self._tables = list(getattr(self, "_tables", ()) or ()) + [(table, tree)]
            if table.note:
                self._wrap(tk.Label(body, text=table.note, font=self._fonts.sans(kit.px("micro")),
                                    foreground=dual.color("muted_text"),
                                    background=dual.color("surface"), anchor="w",
                                    justify="left",
                                    wraplength=self._wrap_hint())).pack(anchor="w", pady=(4, 0))
            return
        # 详情抽屉：选中一行就在表格下面展开字段（键盘 ↑↓ 也生效；本轮不改抽屉形态）
        self._detail = tk.Frame(body, background=dual.color("surface3"), bd=0,
                                highlightthickness=0)
        self._detail_columns = tuple(table.columns)
        self._tree = tree
        # 主表也登记（窄窗紧凑文案要能连附加表一起遍历；见 _apply_narrow_cells）
        self._tables = list(getattr(self, "_tables", ()) or ()) + [(table, tree)]
        # 2026-11-20 真机：44 行的模型表**点不中任何一行**（「当前选中」永远不变），
        # 客户因此没法设价。根因是这一层只在**构建时** ``table.rows`` 非空才绑
        # ``<<TreeviewSelect>>``：空表先建、刷新后才填行的页面（Token Plan 就是）
        # 永远没有这个绑定 —— 点上去没有任何反应，也不报错。
        # 绑定与「有没有行」无关：选中事件由控件发出，行是后来填的也照样生效。
        if True:
            tree.bind("<<TreeviewSelect>>", self._on_row_select)
            for sequence, step in (("<Up>", -1), ("<Down>", 1)):
                tree.bind(sequence, lambda _e, delta=step: self._move_selection(delta))
            tree.bind("<Home>", lambda _e: self._select_row_at(0))
            tree.bind("<End>", lambda _e: self._select_row_at(-1))
            # RC15-039 #31③/#32：↑↓/Home/End 真绑上，提示语才敢这么写
            for sequence, handler in (("<Return>", lambda _e: self._activate_row()),
                                      ("<KP_Enter>", lambda _e: self._activate_row()),
                                      ("<Double-1>", self._on_double_click),
                                      ("<space>", lambda _e: self._activate_row())):
                tree.bind(sequence, handler)
            # R2.4 C3：右键 = 行菜单（复制这一行 / 复制这一格 / 详情）。Linux 用 Button-3，
            # macOS 触控板/鼠标右键在 Tk 里是 Button-2 或 Control-Button-1，三个都绑。
            for sequence in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
                tree.bind(sequence, self._on_table_context_menu)
            hint = ("选中一行看详情（↑↓ 选择，Home/End 到首尾，Enter 打开详情）"
                    if not table.activate else
                    "选中一档再按 Enter（或双击这一行）＝切到它，会先问一次")
            tk.Label(body, text=hint,
                     font=self._fonts.sans(kit.px("micro")),
                     foreground=dual.color("muted_text"),
                     background=dual.color("surface")).pack(anchor="w", pady=(6, 0))
        if table.note:
            self._wrap(tk.Label(body, text=table.note, font=self._fonts.sans(kit.px("micro")),
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
            self._tk.Label(line, text=column, font=fonts.sans(self._kit.px("micro")),
                           foreground=dual.color("muted_text"), background=surface,
                           width=12, anchor="w").pack(side="left")
            self._tk.Label(line, text=str(value),
                           font=fonts.for_text(str(value), self._kit.px("small")),
                           foreground=dual.color("ink"), background=surface,
                           anchor="w").pack(side="left")
        note = self._row_notes.get(selection[0], "")
        if note:
            self._wrap(tk.Label(detail, text="原因：" + note,
                                    font=fonts.sans(self._kit.px("micro")),
                                    foreground=dual.color("amber_text"), background=surface,
                                    anchor="w", justify="left",
                                    wraplength=self._wrap_hint())).pack(anchor="w", padx=10, pady=(4, 6))

    # ── RC15-039 #31②：Tab 顺序 = 视觉顺序（顶部按钮 → 侧栏 → 内容区），焦点环可见
    def _is_focusable(self, widget: Any) -> bool:
        """这个控件进不进 Tab 链（Tk 的 ``takefocus`` 有三种写法：0/1/""/类默认）。"""
        try:
            raw = widget.cget("takefocus")
        except Exception:
            return False
        if raw is None or raw == "":
            # 空值 = 走控件类默认：ttk 输入类默认进链，Canvas 默认不进（我们的 Canvas 都显式写了 1）
            return widget.winfo_class() in ("TEntry", "TCombobox", "Treeview", "Text")
        text = str(raw).strip()
        if text.lstrip("-").isdigit():
            return int(text) != 0
        return text.lower() not in ("false", "no", "0")

    def _collect_focusable(self, root_widget: Any) -> list[Any]:
        """递归收出「看得见且能聚焦」的控件，按（上→下、左→右）排。"""
        found: list[Any] = []
        stack = [root_widget]
        while stack:
            widget = stack.pop()
            try:
                stack.extend(widget.winfo_children())
            except Exception:                            # pragma: no cover - 已销毁
                continue
            if not self._is_focusable(widget):
                continue
            try:
                if not widget.winfo_ismapped():
                    continue
                found.append((widget.winfo_rooty(), widget.winfo_rootx(), widget))
            except Exception:                            # pragma: no cover
                continue
        return [item[2] for item in sorted(found, key=lambda item: (item[0], item[1]))]

    def _tab_order(self) -> list[Any]:
        """整窗的 Tab 链：**顶部按钮**（左→右）→ **侧栏**（上→下）→ **内容区**（上→下、左→右）。

        为什么自己算一次：Tk 默认按**创建顺序**遍历，而顶部那排按钮是右对齐打包的
        （创建顺序 = 刷新/主题/日志/调整贡献 = 从右往左），客户按 Tab 会先从右端跳。
        """
        header = self._collect_focusable(getattr(self, "_header_right", None)) \
            if getattr(self, "_header_right", None) is not None else []
        nav = [self._nav_buttons[key] for key in self._ia_order
               if key in getattr(self, "_nav_buttons", {}) and self._is_focusable(
                   self._nav_buttons[key])]
        content = self._collect_focusable(getattr(self, "_content", None)) \
            if getattr(self, "_content", None) is not None else []
        ordered: list[Any] = []
        for widget in (*(header or ()), *(nav or ()), *(content or ())):
            if widget not in ordered:
                ordered.append(widget)
        return ordered

    def _focus_next(self, delta: int) -> Any:
        """按 :meth:`_tab_order` 前后移动一格（到头绕回）；返回目标控件（没有则 None）。"""
        order = self._tab_order()
        if not order:
            return None
        try:
            current = self._root.focus_get()
        except Exception:                                # pragma: no cover
            current = None
        index = order.index(current) if current in order else (-1 if delta > 0 else 0)
        return order[(index + int(delta)) % len(order)]

    def _focus_step(self, delta: int) -> str:
        target = self._focus_next(delta)
        if target is not None:
            try:
                target.focus_set()
            except Exception:                            # pragma: no cover - 控件已销毁
                pass
            self._notify_a11y_focus(target)
        return "break"                                   # 吃掉按键：不再走 Tk 的默认遍历

    def _notify_a11y_focus(self, widget: Any) -> None:
        """键盘焦点动了 → 给读屏发 ``EVENT_OBJECT_FOCUS``（S3）。

        Tab 顺序（``_tab_order``）与 a11y 树的可聚焦顺序都由同一个 ``View`` 的视觉顺序生成，
        所以按**位置**对应。任何异常都咽掉：读屏事件不许影响键盘路径。
        """
        provider = getattr(self, "_a11y", None)
        if provider is None:
            return
        try:
            order = self._tab_order()
            if widget in order:
                provider.notify_focus_at(order.index(widget))
        except Exception:                                # pragma: no cover - 防御
            pass

    def _notify_a11y_selection(self, index: int) -> None:
        """表格行选中动了 → ``EVENT_OBJECT_SELECTION``。只发事件，不改界面。"""
        provider = getattr(self, "_a11y", None)
        if provider is None:
            return
        try:
            provider.notify_selection_at(index)
        except Exception:                                # pragma: no cover - 防御
            pass

    def _select_row_at(self, index: int) -> str:
        """把选中行移到第 ``index`` 条（``-1`` = 最后一条）；返回 ``"break"`` 吃掉按键。"""
        tree = getattr(self, "_tree", None)
        if tree is None:
            return "break"
        try:
            children = list(tree.get_children())
        except Exception:                                # pragma: no cover - 控件已销毁
            return "break"
        if not children:
            return "break"
        target = children[index] if -len(children) <= index < len(children) else children[-1]
        try:
            tree.selection_set(target)
            tree.focus(target)
            tree.see(target)
        except Exception:                                # pragma: no cover
            pass
        self._notify_a11y_selection(children.index(target))
        return "break"

    def _move_selection(self, delta: int) -> str:
        """↑↓ 逐行移动；到边界就停住（不绕回：绕回会让「最后一条」按不到）。"""
        tree = getattr(self, "_tree", None)
        if tree is None:
            return "break"
        try:
            children = list(tree.get_children())
            current = tree.selection()
            index = children.index(current[0]) if current and current[0] in children else -1
        except Exception:                                # pragma: no cover
            return "break"
        if not children:
            return "break"
        nxt = max(0, min(len(children) - 1, index + int(delta)))
        return self._select_row_at(nxt)

    def _announce(self, text: str) -> None:
        """把一句话放到状态条：读屏用户靠它「听见」焦点与详情的当前值。

        （RC15-039 #31④ 的配套：Tk 没有可用的 UIA 通知，状态条就是唯一的播报面。）
        """
        try:
            self._status.configure(text=str(text or ""))
        except Exception:                            # pragma: no cover - 控件已销毁
            pass

    # ── R2.4 C3：列排序 + 右键行菜单
    def _sort_by_column(self, tree: Any, column: str) -> None:
        """按某列排序（同一列再点反序）：数字列按数值，其它按字符串，空值永远排最后。"""
        state = getattr(self, "_sort_state", {})
        key = str(id(tree))
        ascending = not (state.get(key, {}).get("column") == column
                         and state.get(key, {}).get("ascending", True))
        try:
            columns = list(tree["columns"])
            index = columns.index(column)
            rows = [(tree.item(iid, "values"), iid) for iid in tree.get_children("")]
        except Exception:                              # pragma: no cover - 控件已销毁
            return

        def sort_key(item: Any) -> tuple[int, float, str]:
            raw = item[0][index] if index < len(item[0]) else ""
            text = str(raw).strip()
            if text in ("", "—", "-"):
                return (1, 0.0, "")                    # 空/缺值排最后（正序反序都一样）
            number = self._as_number(text)
            return (0, number, text) if number is not None else (0, float("inf"), text)

        try:
            rows.sort(key=sort_key, reverse=not ascending)
            for position, (_values, iid) in enumerate(rows):
                tree.move(iid, "", position)
            self._sort_state = {**(state or {}), key: {"column": column,
                                                      "ascending": ascending}}
            arrow = "▲" if ascending else "▼"
            for name in columns:
                tree.heading(name, text=(name + " " + arrow) if name == column else name)
            self._toast("已按「" + column + "」" + ("升序" if ascending else "降序")
                        + "排序（只影响这一屏）", ok=True)
        except Exception:                              # pragma: no cover - 控件已销毁
            pass

    @staticmethod
    def _as_number(text: str) -> float | None:
        """把「1,234 / 12.5% / 3 ms / ¥8」这类单元格尽力读成数字；读不出返回 None。"""
        cleaned = str(text or "").replace(",", "").replace("¥", "").replace("$", "")
        for token in ("ms", "毫秒", "%", " 个", " 条", " GB", " MB", "x"):
            cleaned = cleaned.replace(token, "")
        cleaned = cleaned.strip()
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _on_table_context_menu(self, event: Any = None) -> str:
        """右键行菜单：复制整行 / 复制该格 / 打开详情（都在本机完成，不发请求）。"""
        tree = getattr(self, "_tree", None)
        if tree is None:
            return "break"
        try:
            row = str(tree.identify_row(getattr(event, "y", 0)))
            column_id = str(tree.identify_column(getattr(event, "x", 0)))   # "#2" 形式
        except Exception:                              # pragma: no cover - 控件已销毁
            return "break"
        if not row:
            return "break"
        try:
            tree.selection_set(row)
            tree.focus(row)
        except Exception:                              # pragma: no cover
            pass
        columns = list(tree["columns"])
        try:
            index = max(0, int(column_id.lstrip("#") or "1") - 1)
        except ValueError:
            index = 0
        values = [str(item) for item in tree.item(row, "values")]
        cell = values[index] if index < len(values) else ""

        menu = self._tk.Menu(self._root, tearoff=0)
        menu.add_command(label="复制这一整行", command=lambda: self._copy(
            "\t".join(values)))
        if cell:
            menu.add_command(label="复制「" + (columns[index] if index < len(columns) else "这一格")
                             + "」：" + (cell if len(cell) <= 18 else cell[:18] + "…"),
                             command=lambda: self._copy(cell))
        menu.add_separator()
        menu.add_command(label="打开详情", command=self._activate_row)
        try:
            menu.tk_popup(int(getattr(event, "x_root", 0)), int(getattr(event, "y_root", 0)))
        finally:
            try:
                menu.grab_release()
            except Exception:                          # pragma: no cover
                pass
        return "break"

    def _on_double_click(self, event: Any = None) -> None:
        row = ""
        tree = getattr(self, "_tree", None)
        try:
            row = str(tree.identify_row(getattr(event, "y", 0))) if tree is not None else ""
        except Exception:                                # pragma: no cover - 控件已销毁
            row = ""
        if row:
            try:
                tree.selection_set(row)
            except Exception:                            # pragma: no cover
                pass
        self._activate_row()

    def _activate_row(self) -> None:
        """回车 / 双击：表自己声明了 ``activate`` 就派发它，否则**播报**这一行的详情（#31③）。

        复核 M5：旧实现把焦点搬到详情框上，可那个 Frame ``highlightthickness=0``——
        客户看不见焦点在哪，而 ↑↓/Home/End 绑在表格上，焦点一走键位立刻失效（实测选中不再变）。
        现在焦点**留在表格**里，详情用状态条念一句；键盘路径不断，也不需要"看得见的焦点环"。
        """
        table = getattr(self, "_table", None)
        action = str(getattr(table, "activate", "") or "")
        if action:
            self._on_action(action)
            return
        selection = ()
        tree = getattr(self, "_tree", None)
        try:
            selection = tree.selection() if tree is not None else ()
        except Exception:                                # pragma: no cover
            selection = ()
        if not selection:
            self._toast("先用 ↑↓ 选中一行，再按 Enter 打开详情。", ok=False)
            return
        cells = self._row_cells.get(str(selection[0]), ()) or ()
        head = " · ".join(str(item) for item in cells)
        # 只播报，不搬焦点：焦点留在表格上，↑↓/Home/End/Enter 继续可用。
        # 不宣称读屏会念出来——Tk 没有 UIA/MSAA provider，`djb_accessible_name` 只是
        # Python 属性；真读屏要整段文字时用 `--export-text`（票面 #31④ 的兜底）。
        self._announce("详情：" + head)

    def _render_fields(self, fields: Sequence[Field], view_key: str,
                       parent: Any = None) -> None:
        """字段区：宽窗两列、窄窗一列；可编辑的进输入框，只读的用等宽值。

        ``Field.advanced`` 的字段**默认折叠**在「高级（排障用）」按钮后面（接入页的
        节点凭证就是这种：排障要用，但绝不该占首屏——owner 2026-11-13「页面太复杂」）。
        ``Field.prefill=False`` 的可编辑框留空（掩码值只进 hint），避免一点保存就把掩码
        当真实值写回配置。
        """
        kit, tk, ttk = self._kit, self._tk, self._ttk
        fonts = self._fonts
        surface = dual.color("surface")
        target = parent if parent is not None else getattr(self, "_body", self._content)
        card = kit.RoundedCard(target, ground=dual.color("bg"), pad=(16, 14))
        card.pack(fill="x", pady=(0, 10))
        body = card.body
        normal = [field for field in fields if not getattr(field, "advanced", False)]
        advanced = [field for field in fields if getattr(field, "advanced", False)]
        editable = any(field.editable for field in normal)
        tk.Label(body, text="可编辑字段" if editable else "详情",
                 font=fonts.sans(kit.px("h2"), bold=True), foreground=dual.color("ink"),
                 background=surface, anchor="w").pack(anchor="w", pady=(0, 10))
        per_row = 1 if (self._is_narrow() or len(normal) <= 2) else 2

        def build(container: Any, rows: Sequence[Field]) -> None:
            for column in range(per_row):
                container.columnconfigure(column, weight=1, uniform="field")
            for index, field in enumerate(rows):
                row, column = divmod(index, per_row)
                self._render_field(container, row, column, field, per_row)

        grid = tk.Frame(body, background=surface)
        grid.pack(fill="x")
        build(grid, normal)
        if not advanced:
            return
        # 折叠区：默认不 pack（= 不占高度）；按钮文案在「高级」与「收起高级」之间切
        hidden = tk.Frame(body, background=surface)
        toggle = kit.Button(body, "高级（排障用）", lambda: _toggle(),
                            kind="ghost", ground=surface, px=kit.px("small"),
                            height=self._px(32), pad=10)
        self._a11y_name(toggle, "展开高级（排障用）字段")
        toggle.pack(anchor="w", pady=(2, 0))
        # 留给真窗口取证（tests/test_desktop_payout.py）：折叠区/按钮不在首屏，也得能被点开
        self._advanced_panel = hidden
        self._advanced_toggle = toggle
        state = {"open": False}
        if any(getattr(item, "error", "") for item in advanced):
            # 错误落在折叠区里就等于没显示：有错就默认展开（客户改完再自己收起）
            state["open"] = True
            build(hidden, advanced)
            hidden.pack(fill="x", pady=(6, 0), before=toggle)
            toggle.set_text("收起高级")

        def _toggle() -> None:
            state["open"] = not state["open"]
            if state["open"]:
                build(hidden, advanced)
                hidden.pack(fill="x", pady=(6, 0), before=toggle)
                toggle.set_text("收起高级")
                self._a11y_name(toggle, "收起高级（排障用）字段")
            else:
                hidden.pack_forget()
                toggle.set_text("高级（排障用）")
                self._a11y_name(toggle, "展开高级（排障用）字段")

    def _render_field(self, container: Any, row: int, column: int, field: Field,
                      per_row: int) -> None:
        """画一个字段格（从 :meth:`_render_fields` 拆出来：折叠区复用同一套画法）。"""
        kit, tk, ttk = self._kit, self._tk, self._ttk
        fonts = self._fonts
        surface = dual.color("surface")
        block = tk.Frame(container, background=surface)
        block.grid(row=row, column=column, sticky="nsew", padx=(0, 18), pady=(0, 10))
        # 标签列用像素 minsize（Tk 的 width= 是按「0 字宽」算的，中文标签会被截）
        block.columnconfigure(0, minsize=104)
        block.columnconfigure(1, weight=1)
        tk.Label(block, text=field.label, font=fonts.sans(kit.px("micro")),
                 foreground=dual.color("muted_text"), background=surface,
                 anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 10))
        if field.editable:
            # 秘密与「只给掩码」的字段都不预填：输入框里的空串 = 不改（_config_for_save 口径）
            empty = field.secret or not getattr(field, "prefill", True)
            # F2：**本页**刚刚填过的值优先（客户点按钮/等刷新都不该把它抹掉）；
            # 只认本页暂存，不读配置、不跨页串值。
            kept = (self._form_keep.get(self._render_view_key) or {})
            initial = kept[field.key] if field.key in kept else ("" if empty else field.value)
            variable = tk.StringVar(value=initial)
            # RC14 I：协议用**只读下拉**（枚举值不可能非法；也免得客户手打错协议名）。
            if getattr(field, "choices", ()):
                entry = ttk.Combobox(block, textvariable=variable,
                                     values=list(field.choices), state="readonly",
                                     width=26 if per_row > 1 else 42, style="Kit.TCombobox")
            else:
                entry = ttk.Entry(block, textvariable=variable, style="Kit.TEntry",
                                  width=28 if per_row > 1 else 44)
            if field.secret:
                entry.configure(show="•")
            entry.grid(row=0, column=1, sticky="ew")
            # G7（RC14）：秘密字段右侧「显示 / 隐藏」。只切 Entry 的 show 选项——
            # 不复制、不写日志、不进快照；输入框里的内容本来就只是客户刚粘的那串。
            if getattr(field, "can_reveal", lambda: False)():
                reveal = self._render_reveal_button(block, field, entry, surface)
                if reveal is not None:
                    self._reveal_buttons[field.key] = reveal
            hint = field.hint or ""
            if field.secret:
                hint = ("当前 " + field.value + "｜" + hint).strip("｜")
            if hint:
                self._wrap_to_column(tk.Label(
                    block, text=hint, font=fonts.sans(kit.px("micro")),
                    foreground=dual.color("muted_text"), background=surface,
                    anchor="w", justify="left",
                    wraplength=self._wrap_hint())).grid(row=1, column=1, sticky="we",
                                                        pady=(3, 0))
            if field.error:
                # RC15-039 #1：红字就在**这个输入框**下面（不是状态栏、不是页脚）
                self._wrap_to_column(tk.Label(
                    block, text="！" + field.error, font=fonts.sans(kit.px("micro")),
                    foreground=dual.color("red_text"), background=surface,
                    anchor="w", justify="left",
                    wraplength=self._wrap_hint())).grid(row=2, column=1, sticky="we",
                                                        pady=(2, 0))
            self._fields[field.key] = field
            self._vars[field.key] = variable
            self._entries[field.key] = entry
            self._a11y_name(entry, field.label + ("（已掩码，粘贴新值才会覆盖）"
                                                 if field.secret else ""))
            try:                                   # 初始文本（自动刷新判「有没有在改」）
                self._entry_initial[field.key] = str(entry.get())
            except Exception:                      # pragma: no cover - 控件已销毁
                self._entry_initial[field.key] = str(field.value or "")
        else:
            # 只读值也要能换行：900 宽单列时端点 URL（http://…/agent）比列宽长，
            # 不换行就被切掉尾部（audit text-clip-w）。短值不受影响。
            value_label = self._wrap_to_column(tk.Label(
                block, text=field.value,
                font=fonts.for_text(field.value, kit.px("small")),
                foreground=dual.color("ink"), background=surface,
                anchor="w", justify="left",
                wraplength=self._wrap_hint()))
            value_label.grid(row=0, column=1, sticky="we")
            if field.hint:
                self._wrap_to_column(tk.Label(
                    block, text=field.hint, font=fonts.sans(kit.px("micro")),
                    foreground=dual.color("muted_text"), background=surface,
                    anchor="w", justify="left",
                    wraplength=self._wrap_hint())).grid(row=1, column=1, sticky="we",
                                                        pady=(3, 0))
            if field.error:
                self._wrap_to_column(tk.Label(
                    block, text="！" + field.error, font=fonts.sans(kit.px("micro")),
                    foreground=dual.color("red_text"), background=surface,
                    anchor="w", justify="left",
                    wraplength=self._wrap_hint())).grid(row=2, column=1, sticky="we",
                                                        pady=(2, 0))

    def _entry_for(self, key: str) -> Any:
        """取某字段的输入框（没有该字段 → ``None``）；给窗口级测试与「显示密码」用。"""
        return self._entries.get(str(key))

    def _render_reveal_button(self, block: Any, field: Field, entry: Any,
                              surface: str) -> Any:
        """密码框的「显示 / 隐藏」（RC14 G7）：画在输入框**右侧**，点一下才看得见。

        只做一件事：``entry.configure(show=""/"•")`` + 按钮文案切换。
        **不**把值写进日志、不复制到剪贴板、不改 ``_vars``（保存路径读到的还是同一串）。
        """
        kit, tk = self._kit, self._tk
        state = {"shown": False}
        button = kit.Button(block, "显示", lambda: _toggle(), kind="ghost", ground=surface,
                            px=kit.px("micro"), height=self._px(32), pad=8)
        self._a11y_name(button, "显示" + field.label + "的明文")
        button.grid(row=0, column=2, sticky="w", padx=(6, 0))
        del tk                                        # 只用 kit 的按钮，避免两套样式

        def _toggle() -> None:
            state["shown"] = not state["shown"]
            try:
                entry.configure(show="" if state["shown"] else "•")
            except Exception:                          # pragma: no cover - 控件已销毁
                return
            button.set_text("隐藏" if state["shown"] else "显示")
            self._a11y_name(button, ("隐藏" if state["shown"] else "显示") + field.label + "的明文")

        return button

    # ── 动作
    def _on_action(self, key: str) -> None:
        # 2026-11-20：动作级可观测性。真机上「点了没反应」时，日志里连一条动作记录
        # 都没有，只能靠猜。这里在入口/出口各记一条（动作名、耗时、ok、effect），
        # **不记**任何字段值（密钥/地址都不进日志）。
        import time as _time
        _started = _time.monotonic()
        _log = None
        try:
            from ..logging_setup import event as _log
            _log("desktop.action.start", action=key)
        except Exception:                          # pragma: no cover - 日志不可用
            _log = None
        # D4：客户按了新动作 ⇒ 上一条失败提示可以退场（新的失败会重新粘上）
        self._clear_sticky()
        for name, variable in list(self._vars.items()):
            try:
                self._controller.set_draft(name, variable.get())
            except Exception:                            # 变量随旧视图销毁：跳过，不让动作静默不执行
                continue
        result = self._controller.action(key, confirm=self._confirm)
        # CR-20261119 F5：动作说「这些字段已经落盘了」→ 本轮重画就不再带回（密钥框回空）
        payload = getattr(result, "payload", None)
        forget = payload.get("forget") if isinstance(payload, Mapping) else None
        self._forget_keys = {str(name) for name in (forget or [])}
        # 即用即清：密钥已经落盘，框里不再留着（不靠「等下一次重画」——视图没变时
        # ``_render`` 会跳过重画，那样客户会以为没生效）
        for name in self._forget_keys:
            variable = self._vars.get(name)
            try:
                if variable is not None:
                    variable.set("")
            except Exception:                      # noqa: BLE001 - 控件已销毁
                continue
        # RC15-039 #1：动作带回**字段级错误**时，先落到本页再重画（保存成功后为空 = 清残留）。
        # 只有「表单类」动作会改这份状态：复制/打开目录之类的动作不许把红字顺手抹掉。
        fields = dict(getattr(result, "fields", {}) or {})
        if fields or result.effect in FORM_EFFECTS:
            self._set_field_errors(fields)
        copied = True
        if result.effect == "copy":
            copied = self.copy_text(result.payload)
        elif result.effect == "open_dir":
            self._open_dir(result.payload)
        elif result.effect == "open_url":
            self._open_url(result.payload)
        elif result.effect == "connectivity":
            # R2.1 ③：动作已写好「进行中」并派了 worker；同步回落（无窗口）时结果已就绪
            if str((result.payload or {}).get("state") or "") in ("ok", "fail"):
                self._apply_connectivity(result, None)      # 同步回落：结论已就绪
            else:
                # 已派给 worker：先把「进行中」画上去（这就是 200ms 内要看到的那行字）
                self._toast(str(result.message or "进行中…"), ok=True)
                self._force_next_render = True
                try:
                    self._render(self._controller.current(), force=True)
                    self._update_status()
                except Exception:                  # pragma: no cover - 窗口已销毁
                    pass
        elif result.effect == "probe":
            # 「已派发」由 _probe_started 播报；只有同步回落时这里才拿到最终结论，
            # 否则 worker 侧结束时 _apply_probe 播报（两条路都只播一次）
            if str((result.payload or {}).get("state") or "") == "done":
                self._apply_probe(result, None)
        elif result.effect == "update":
            # G4-3：九状态逐个翻译成「发生了什么 + 下一步」，下载类动作顺手打开下载页
            self._apply_update(result.payload, ok=result.ok, message=result.message)
        elif result.effect == "relogin":
            # R2.1 ①：横幅按钮派发到这里 —— 自动绑定走设备码（后台轮询），
            # 否则弹一次性绑定码输入框；两条路完成后都回原页（_resume_after_login）。
            self._return_view = self._return_view or self._controller.view
            if str(result.payload or "") == "device":
                self._on_action("account.bind_device")
            else:
                self._on_action("account.bind")
        elif result.effect == "goto" and result.payload:
            self.show_view(str(result.payload))       # 例如「共享 Token Plan」→「接入配置」
        elif result.effect == "payout_submit":
            # 网络调用不许在主线程：派给 worker，结果由 _apply_payout 回投（toast + 重拉状态）
            self._start_payout_submit(str(result.payload), result.message)
        elif result.effect in ("refresh", "access", "bench", "control", "reload", "filter"):
            # 2026-11-20：「刷新」原来只在表单字段被忘掉时才强制重画；否则内容比对
            # 认为没变化就跳过 —— 客户点了刷新，屏幕一动不动（底价/上游都是后到的
            # 数据）。刷新语义就是「重读 + 重画」，这里一律强制。
            if result.effect == "refresh" and result.ok:
                self._force_next_render = True
            # 动作刚落地（有 forget ⇒ 真的写了盘）时强制重画：视图内容没变也得把新结论
            # （「读到 N 个模型 / 已添加上游 …」）画出来，否则客户还是看到「没反应」。
            self._force_next_render = bool(self._forget_keys)
            self._render(self._controller.current())
        elif fields:
            # 表单错误回来了，但这个 effect 自己不重画（例如收款提交）：补一次，
            # 否则「红字在数据里、屏幕上没有」——那还是 0.3.7 的「点了没反应」。
            self._render(self._controller.current())
        if key in ("account.bind", "account.bind_device") and bool(result.ok):
            self._resume_after_login(True)
        self._update_status()
        self._forget_keys = set()
        # "copy" 漏在名单外 = 复制成功/失败都零反馈（S4：客户说「点了没反应」）
        if result.effect == "copy" and not copied:
            self._toast("复制失败：剪贴板写入被拒绝（可能有别的程序占着剪贴板），请再点一次",
                        ok=False)
        elif result.effect == "copy":
            if result.message:
                self._toast(result.message, ok=result.ok)
        elif result.effect in SELF_REPORTING_EFFECTS:
            # 这几条在自己的渲染函数里已经播报（``_open_url`` / ``_open_dir`` / 收款 worker），
            # 再播一次会让后一条把「已打开 / 未打开」这类关键差别盖掉。
            pass
        # G1-0（RC14 P0）：白名单 → **黑名单**。旧实现只播报
        # ("save","access","reload","control","export","verify","copy") 六种 effect，
        # 于是 effect="" 的 8 处（模型意向上报 / 跑分失败 / 空节点 ID / 收据未选中 /
        # 控制通道未接通 / 登出无节点 / 日志无行 / 日志路径未知）全部**点了没反应**。
        # 现在：**失败必报**，成功只要有话就说；只有「用户自己取消」和「刷新中…」
        # （``_refresh_started`` 已经播过，重复会盖掉忙态提示）两种例外。
        elif (result.effect != "cancelled" and result.message
              and not (result.effect == "refresh" and result.ok)):
            self._toast(result.message, ok=result.ok)
        # 出口：一条动作的最终结局（成功/失败/耗时/分流方式）。与入口成对，
        # 「点了没反应」时能直接看出是没进 action、进了但早退、还是失败被吞。
        if _log is not None:
            try:
                _log("desktop.action.done", action=key, ok=bool(result.ok),
                     effect=str(getattr(result, "effect", "") or ""),
                     ms=int((_time.monotonic() - _started) * 1000))
            except Exception:                      # pragma: no cover - 日志不可用
                pass

    def copy_text(self, text: str) -> bool:
        """把文本放进系统剪贴板；**必须** ``update()`` 一次才算真的写进去。

        真机事故（S4）：Tk 的 ``clipboard_append`` 只是把内容登记给本进程，X11/Win32 的
        剪贴板所有权要等事件循环处理一次才转移；不 ``update()`` 就退出/切窗口，粘贴出来
        是**旧内容或空**。旧实现还 ``except Exception: pass``，失败连日志都没有。
        返回是否成功；失败原因交给调用方 toast（不再静默）。
        """
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(str(text))
            updater = getattr(self._root, "update", None)
            if callable(updater):
                updater()
            return True
        except Exception as exc:                      # noqa: BLE001 - 剪贴板可能被别的程序占着
            try:
                from ..logging_setup import event
                event("desktop.copy_failed", error=type(exc).__name__)
            except Exception:
                pass
            return False

    def _copy(self, text: str) -> None:
        self.copy_text(text)

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
        # 2026-11-20：**显式给空初始值**。之前没传 initialvalue，客户看到输入框里
        # 预填着网页端地址（URL 其实在 message 里），以为「绑定码就是那个地址」，
        # 点 OK 就把 URL 当码提交了（真机复现，报 binding_code_unknown）。
        value = simpledialog.askstring("DJB / 绑定网页端", message, parent=self._root,
                                       initialvalue="")
        return str(value).strip() if value else None

    def _open_url(self, url: str) -> None:
        """用系统浏览器打开网页端；打不开就把地址留在状态栏（不静默失败）。

        G1-2 的附加要求：**先校验协议头**。``DJB_WEB_CONSOLE`` 配错（或动作给错）时，
        ``webbrowser`` 可能把 ``ftp:`` / 空 scheme 交给系统处理器，甚至什么都不发生——
        客户看到「点了没反应」。这里直接报错，并把地址原样留在状态栏供手工复制。
        """
        if not url:
            self._toast("打开失败：链接为空（配置可能缺失）", ok=False)
            return
        lowered = str(url).strip().lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            self._toast("打开失败：不是 http(s) 链接，已拒绝调用浏览器（" + str(url) + "）",
                        ok=False)
            return
        try:
            import webbrowser
            opened = bool(webbrowser.open(str(url)))
        except Exception:
            opened = False
        self._toast(("已在浏览器打开：" if opened else "浏览器未能自动打开，请手动访问：") + str(url),
                    ok=opened)

    # ── 字段级错误（RC15-039 #1：内联红字 + 聚焦首个错误 + 切页清残留）
    def _set_field_errors(self, errors: Mapping[str, str] | None) -> None:
        """换一份字段错误（只在表单动作回来时调用；重画由 ``_on_action`` 统一负责）。

        重画是必须的（错误来自一次动作、视图是数据的投影），但只画**一次**：
        ``access`` 这类 effect 自己就会重画，这里再画一次会让焦点闪一下再被抢走。
        """

        fresh = {str(key): str(value) for key, value in (errors or {}).items() if value}
        if fresh == getattr(self, "_field_errors", None):
            return
        self._field_errors = fresh

    def _focus_field(self, key: str) -> None:
        """把键盘焦点送到某个字段的输入框，并把它滚进可视区（「本页内可见」）。"""
        entry = getattr(self, "_entries", {}).get(str(key))
        if entry is None:
            return
        try:
            entry.focus_set()
        except Exception:                              # pragma: no cover - 控件已销毁
            return
        self._scroll_into_view(entry)

    def _scroll_into_view(self, widget: Any) -> None:
        """把控件滚进内容区可视范围（只改滚动位置，不动布局）。"""
        canvas = getattr(self, "_canvas", None)
        content = getattr(self, "_content", None)
        if canvas is None or content is None:
            return
        try:
            canvas.update_idletasks()
            total = max(1, int(content.winfo_height()))
            offset = int(widget.winfo_rooty()) - int(content.winfo_rooty())
            visible = int(canvas.winfo_height())
            top = int(canvas.canvasy(0))
            if offset < top or offset + int(widget.winfo_height()) > top + visible:
                canvas.yview_moveto(max(0.0, min(1.0, offset / float(total))))
        except Exception:                              # pragma: no cover - 无显示/已销毁
            return

    def _toast(self, message: str, *, ok: bool = True) -> None:
        """状态条播报一句话；**失败要粘住**（D4）。

        为什么不能只 ``configure(text=…)``：``_update_status()`` 每轮刷新都会调，它用
        ``_status_line()`` 写状态条，于是「! 绑定未成功：绑定码已过期…」在 10 秒内就
        被「已连接 · …」盖掉——客户看到的就是「点 OK 没反应」（2026-11-18 真机复现）。
        失败文案进 ``_sticky_error`` 后由 :meth:`_update_status` 让位，并用红色区分。
        """
        text = ("✓ " if ok else "! ") + str(message or "")
        self._sticky_error = "" if ok else text
        try:
            self._status.configure(
                text=text,
                foreground=dual.color("muted_text" if ok else "red"))
        except Exception:                          # pragma: no cover - 控件已销毁
            return
        self._arm_sticky_clear()

    def _arm_sticky_clear(self) -> None:
        """给粘住的失败提示排一个退场；没有事件循环（测试 / 截图 / 假 root）就跳过。"""
        if not self._sticky_error:
            return
        root = getattr(self, "_root", None)        # 假 root（测试 / 无窗口）没有 after
        after = getattr(root, "after", None)
        cancel = getattr(root, "after_cancel", None)
        if not callable(after) or not callable(cancel):
            return
        previous = getattr(self, "_sticky_job", None)
        if previous is not None:
            try:
                cancel(previous)
            except Exception:                      # pragma: no cover - 窗口已销毁
                pass
        try:
            self._sticky_job = after(int(STICKY_ERROR_SECONDS * 1000), self._clear_sticky)
        except Exception:                          # pragma: no cover - 窗口已销毁
            self._sticky_job = None

    def _clear_sticky(self) -> None:
        """撤掉粘住的失败提示，把状态条交还给常规状态行（新动作 / 换页 / 超时）。"""
        has_sticky = bool(getattr(self, "_sticky_error", ""))
        self._sticky_job = None
        self._sticky_error = ""
        if not has_sticky:
            return
        try:
            self._status.configure(text=self._status_line(),
                                   foreground=dual.color("muted_text"))
        except Exception:                          # pragma: no cover - 控件已销毁
            pass

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

    # ── 读屏（RC15-039 #31）：只读镜像，装不上就静默降级
    def _install_a11y(self) -> Any:
        """装 MSAA 提供方；任何失败都返回 ``None``（Linux、无 comtypes、钩窗口失败都走这里）。"""
        try:
            provider = install_a11y(self._root)
        except Exception:                      # pragma: no cover - 防御
            return None
        if provider is not None and getattr(self, "_last_view", None) is not None:
            # 构造时先渲染了一页、后装的 provider：补一次刷新，否则首屏读屏是空的
            try:
                provider.refresh(self._last_view)
            except Exception:                  # pragma: no cover - 防御
                pass
        return provider

    def _refresh_a11y(self, view: View) -> None:
        """把这一版 ``View`` 推给读屏树。**自动刷新也走这里，但 provider 不发焦点事件**。"""
        provider = getattr(self, "_a11y", None)
        if provider is None:
            return
        try:
            provider.refresh(view)
        except Exception:                      # pragma: no cover - 防御
            pass

    # ── 生命周期
    def run(self) -> None:
        self._root.mainloop()

    def stop(self) -> None:
        provider = getattr(self, "_a11y", None)
        if provider is not None:
            try:
                provider.detach()            # 先摘窗口过程钩子，再销毁窗口
            except Exception:
                pass
            self._a11y = None
        self._cancel_auto_refresh()          # 自动刷新不许在关窗后继续排下一次
        self._cancel_fade()                  # 淡入/主题渐变的 after 也要取消：
        # 不然销毁窗口时 Tk 会对着已消失的 lambda 打一行「invalid command name」
        # （只是 stderr 噪音，但会把真实测试输出搞脏——0.3.7 起就有的收场缺口）
        for name in ("_refresh_worker", "_payout_worker", "_update_worker", "_probe_worker",
                     "_discover_worker"):
            worker = getattr(self, name, None)
            if worker is None:
                continue
            try:
                worker.close()
            except Exception:                      # pragma: no cover - 已关闭/线程已退
                pass
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
            # 探针事实（截图基线）：四种状态各来一行，页面才看得出「三态不混」
            "probe": {"rows": [
                {"upstream_id": "u1", "base_url": "https://api.example-relay.com/v1",
                 "model": "qwen2.5-7b-instruct", "enabled": True,
                 "inference_ok": True, "stream_ok": True, "usage_ok": True,
                 "probe_version": 3, "last_probe_at": "2026-11-14T11:58:00+0800",
                 "probe_error_class": None, "probe_age_s": 132, "fresh": True,
                 "announceable": True},
                {"upstream_id": "u1", "base_url": "https://api.example-relay.com/v1",
                 "model": "llama-3.1-8b-instruct", "enabled": True,
                 "inference_ok": True, "stream_ok": False, "usage_ok": True,
                 "probe_version": 3, "last_probe_at": "2026-11-14T11:57:00+0800",
                 "probe_error_class": "stream_failed", "probe_age_s": 192,
                 "fresh": True, "announceable": False},
                {"upstream_id": "u2", "base_url": "https://api.another-relay.net",
                 "model": "claude-3-7-sonnet", "enabled": True,
                 "inference_ok": False, "stream_ok": None, "usage_ok": None,
                 "probe_version": 3, "last_probe_at": "2026-11-14T11:50:00+0800",
                 "probe_error_class": "unauthorized", "probe_age_s": 612,
                 "fresh": False, "announceable": False},
                {"upstream_id": "u2", "base_url": "https://api.another-relay.net",
                 "model": "gpt-4o-mini", "enabled": True,
                 "inference_ok": None, "stream_ok": None, "usage_ok": None,
                 "probe_version": None, "last_probe_at": None,
                 "probe_error_class": None, "probe_age_s": None, "fresh": False,
                 "announceable": False}],
                "periodic_s": 450, "fresh_window_s": 900, "quote_window_s": 600,
                "note": ""},
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
                              "price_note": "—", "usdt_address": "0x3f7c…9a2c",
                              "openai_base_url": "http://127.0.0.1:1717/v1",
                              "anthropic_base_url": "http://127.0.0.1:1717",
                              "agent_downlink_url": "http://127.0.0.1:1717/agent",
                              "scoped_token": "djb_sc_…9f2c"},
                   "valid": True, "binding": {"state": "bound", "node_id": "node-7c31"},
                   # 收款地址云端状态（方案 B）：演示数据给「已生效」，截图才看得到正向态
                   "payout": {"ok": True, "can_submit": True, "reason": "", "hint": "",
                              "data": {"address": "0x3f7c9a1b2d3e4f5a6b7c8d9e0f1a2b3c4d5e9a2c",
                                       "status": "whitelisted", "cooldown_until": None,
                                       "updated_at": "2026-11-01T04:30:00Z",
                                       "label": "默认收款"}},
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
                  "mode": "balanced", "preset": "均衡", "yield_enabled": True},
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
    """本机命令通道：显式带上 ``DJB_HOME``，避免子进程读到别的 home。

    ``home is None`` 必须在这里解析成**真实目录**：旧实现写 ``str(home)`` → 子进程拿到
    ``DJB_HOME=None``（字符串），CLI 会把 ``<cwd>/None`` 当数据目录 → 一律「本机未初始化」。
    """
    if home is None:
        from ..config import resolve_home
        home = resolve_home()
    home = os.fspath(home)

    def run(argv, stdin_text=None):
        """跑一条本机命令。

        ``stdin_text``：需要**安全投喂**的内容（上游密钥走 ``--secret-stdin``）。
        有它就不接 DEVNULL，直接把文本写进子进程 stdin——密钥因此不进 argv、
        不进 shell 历史、不进进程列表。
        """
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
            # 带 stdin 的那条是 ``upstream add``：它还要联网申报，20 s 常常不够。
            timeout = 60 if stdin_text is not None else 20
            completed = subprocess.run(command, capture_output=True,
                                       encoding="utf-8", errors="replace", timeout=timeout,
                                       env=env, input=stdin_text, check=False,
                                       **({} if stdin_text is not None
                                          else {"stdin": subprocess.DEVNULL}),
                                       **subprocess_window_kwargs())
        except subprocess.TimeoutExpired:
            raise ProviderError("本地命令超时（" + str(timeout) + "s）") from None
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
        # 与数据层同一个解析口径（Windows 是 %APPDATA%\DJB，不是 ~/.djb）：
        # 写死 ~/.djb 时「保存并回读」会连到不存在的 socket → 永远「daemon 未运行」。
        from ..config import resolve_home
        paths = Paths(os.fspath(home) if home else resolve_home())
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
                    cloud: Any | None = None, bus: Any | None = None,
                    boot: Mapping[str, Any] | None = None) -> DesktopController:
    """装配：数据源 + 接入配置服务 + 控制通道 + 状态总线。

    ``home`` **只解析一次**（S3 根因，见 REPORT）：``None`` 时各下游曾各自找默认目录——
    数据层用 ``resolve_home()``（Windows = ``%APPDATA%\\DJB``），action CLI 用了
    ``str(None)`` = 字符串 ``"None"``（= 当前工作目录下的 ``None`` 目录），控制通道 / 热重载
    又写死 ``~/.djb``。结果：同一个窗口里「取数去 A、保存去 B、控制去 C」，
    在真机上表现成「节点绑定不可用 / 保存了不生效」。
    """
    from ..gui.state import ControlBus
    bus = bus if bus is not None else ControlBus()
    if home is None and not demo:
        home = _default_home()             # 与 LocalDataSource / bootstrap 同一个解析口径
    home = None if home is None else os.fspath(home)
    if demo:
        source: Any = demo_source()
    else:
        if cloud is None:
            # 没显式注入就按 ~/.djb 自己装：没登录节点时是 None（视图照常显示「暂不可用」）
            from .nodecloud import NodeCloudClient
            cloud = NodeCloudClient.from_home(home)
        source = LocalDataSource(home=home, cloud=cloud, boot=boot)
    store = FileAccessStore(None if home is None else
                            os.path.join(str(home), ACCESS_FILE))
    access = AccessService(store, reloader=_make_reloader(home))
    factory = None
    if not demo:
        def factory() -> Any:              # noqa: ANN401 - 惰性构造（worker 线程里调用）
            from ..update import open_manager
            return open_manager(home)
    controller = DesktopController(source, access=access, control=LocalControlChannel(home),
                                   bus=bus,
                                   cli=_make_cli_runner(home) if not demo else None,
                                   # G4：更新检查要 UpdateManager（本机 paths/store/config），
                                   # 由装配层注入，controller 不自己找 home（S3 教训）
                                   update_factory=factory)
    # R2.4 S3/S4：只有**开关打开**时才接缓存（关 = 完全不读不写，回旧行为）。demo 不接：
    # 演示数据每次都是同一份，接了反而让「缓存」这件事在截图里说不清。
    if not demo and home and client_flags.enabled(client_flags.CACHE_FLAG,
                                                  config=_flags_config(home)):
        controller.cache_home = str(home)
    return controller


def _flags_config(home: str | os.PathLike | None) -> Any:
    """读开关用的配置（读不到就当空配置：开关按默认值关闭）。"""
    if not home:
        return {}
    try:
        from ..config import Paths, load_config

        return load_config(Paths(os.fspath(home)))
    except Exception:                      # 配置坏了不影响开关求值（默认全关）
        return {}


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
    parser.add_argument("--export-text", action="store_true",
                        help="把当前页的文字版打印出来并复制到剪贴板（读屏/排障兜底）")
    parser.add_argument("--no-auto", action="store_true",
                        help="关闭首启自举（测试/离线环境：不建目录、不起节点）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    boot_line = ""
    daemon: Mapping[str, Any] | None = None
    # 2026-11-19 真机：首窗 6~15 s，客户以为「双击没反应」。
    # onefile 解包本身约 6 s（改不了），自举再叠数秒（可以挪）——
    # 所以**先建窗口、先画出来，再跑自举**，自举结论用 controller.set_boot() 回填后重画。
    # 自举结论（尤其 start_daemon 的 no-cli / timeout）仍要带进数据层：
    # 「接入配置」页的「节点进程」字段靠它说出真因，而不是只说「daemon 未运行」
    controller = make_controller(home=args.home, demo=args.demo, boot=None)
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
    # 先把窗口真正映射到屏幕，再去做可能很慢的首启自举（否则观感是「双击没反应」）
    try:
        app._root.update()                                # noqa: SLF001 - 纯展示
    except Exception:                                     # pragma: no cover - 纯展示
        pass
    if not args.demo and not args.no_auto:
        # 首启自举（2026-11-06 外部客户实测：装完只看到「云端未接通/本机未初始化」）
        try:
            from ..bootstrap import ensure_ready, start_daemon, summary_text
            boot = ensure_ready(home=args.home)
            daemon = start_daemon(home=args.home)
            boot_line = summary_text(boot, daemon)
            controller.set_boot(daemon)
        except Exception as exc:                          # 自举失败不许拖垮启动
            boot_line = "首启自举失败（" + type(exc).__name__ + "）；界面可继续使用"
        if boot_line:
            # 自举结果写进状态栏：用户看得到"已自动初始化/已修正云端地址/节点已启动"
            try:
                app._toast(boot_line)                     # noqa: SLF001 - 纯展示
                app.show_view(args.view)                  # 回填「节点进程」后重画本页
            except Exception:                             # pragma: no cover - 纯展示
                pass
    if args.export_text:
        code = _export_text(app, args.view)
        app.stop()
        return code
    if args.shot:
        _shoot(app, args.shot)
        app.stop()
        return EXIT_OK
    app.run()
    return EXIT_OK


def _print_utf8(text: str) -> None:
    """按 UTF-8 把一行文本写到 stdout（``--export-text`` 专用）。

    CR-20261119-CLIENT-EXPORT-TEXT-GBK：中文 Windows 的 ``sys.stdout`` 编码是 GBK，
    页面文本里带上主题图标 ``◐``(U+25D0) 就会 `UnicodeEncodeError` —— 导出直接崩，
    连取证都拿不到。这里绕过控制台编码：有 ``buffer`` 就写 UTF-8 字节（重定向到文件/
    管道时最干净），没有 buffer 或写失败再退回 ``print``，并把编不出的字符降级成 ``?``。
    **任何情况都不抛**：导出是兜底功能，它自己不能成为新的故障点。
    """
    stream = getattr(sys, "stdout", None)
    if stream is None:                                 # windowed + 无控制台/无重定向
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write((text + "\n").encode("utf-8", "replace"))
            buffer.flush()
            return
        except Exception:                              # noqa: BLE001 - 退回文本写
            pass
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(str(text).encode(encoding, "replace").decode(encoding, "replace"))


def _export_text(app: DesktopApp, view: str = "") -> int:
    """RC15-039 #31④：把当前页的 ``View.plain_text()`` 打印出来并放进剪贴板。

    为什么要有：Tk 没有 UIA provider，读屏软件读不到画布上的字（见票面 #31 的完整方案
    另立评估票）。``--export-text`` 是**兜底**——客户按一次就能把这一页的**全部**文字
    （含被折叠的、被表格截断的）拿到手，粘贴到记事本或让读屏去念。

    只读：只看 ``View``，不碰数据源、不写任何文件、不改配置。
    """
    try:
        page = app._controller.show(view or app._controller.view)   # noqa: SLF001 - 同一模块
    except Exception as exc:                                        # pragma: no cover
        _print_utf8("取这一页失败（" + type(exc).__name__ + "）；没有导出任何内容。")
        return EXIT_HEADLESS
    text = page.plain_text()
    _print_utf8(text)
    copied = app.copy_text(text)
    _print_utf8("")
    _print_utf8(("已复制到剪贴板（" if copied else "剪贴板不可用（")
                + page.title + " · " + str(len(text)) + " 字符）"
                + ("" if copied else "；上面的文字可直接从终端复制"))
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
                subprocess.run(["import", "-window", window_id, target], check=False,
                               timeout=30, **subprocess_window_kwargs())
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.exists(target):
                written.append(target)
    return written


if __name__ == "__main__":                          # pragma: no cover
    sys.exit(main())
