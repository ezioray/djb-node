# SPDX-License-Identifier: Apache-2.0
"""跨平台 UI 适配层（纯 stdlib；**模块级不 import tkinter**，无图形环境可 import、可单测）。

为什么单独一个模块
==================

桌面端的「平台差异」不是配色问题，而是四类**事件/度量语义**差异：

1. 滚轮事件名与量纲不同（X11 = ``Button-4/5``；Windows = ``MouseWheel`` + ``delta``/120；
   macOS = ``MouseWheel`` + ``delta`` 已经是「行」）。
2. 字号单位不同（Tk 负值 = 像素，正值 = 点；像素→点的换算依赖显示器度量）。
3. 主修饰键不同（Windows/Linux = Control，macOS = Command）。
4. 窗口装饰与屏幕工作区不同（macOS 有应用菜单/Dock，Windows 有任务栏，Tk 8.6 无工作区 API）。

这些差异如果散落在 ``app.py`` 里，就会变成「只在某台机器上对」的隐性分支；集中在本模块后，
**每个函数都能在无窗口的机器上用假对象单测**，真机只需要复核「数值是否合眼」。

证据来源（逐条对应下面的函数）
------------------------------

* 滚轮：Tk 8.6 自带库 ``ttk/utils.tcl`` 的 ``ttk::bindMouseWheel`` 与 ``TtkScrollable`` 类绑定
  —— 原文注释：「On X11, the server typically maps the mouse wheel to Button4 and Button5.
  On OSX, Tk generates sensible values for the %D field in <MouseWheel> events.
  On Windows, %D must be scaled by a factor of 120.」
  同一文件里 X11 分支绑 ``<Button-4>``/``<Button-5>``，非 X11 分支才绑 ``<MouseWheel>``。
  ``listbox.tcl`` 的注释更直接：「The MouseWheel will typically only fire on Windows and Mac OS X.」
  ⇒ **Linux/X11 上 ``<MouseWheel>`` 根本不会触发**，这正是 ``app.py`` 现在的写法。
* 字号：Tk ``font(n)`` 手册：「If size is a positive number, it is interpreted as a size in points.
  If size is a negative number, its absolute value is interpreted as a size in pixels. …
  Sizes should normally be specified in points so the application will remain the same ruler size
  on the screen, even when changing screen resolutions or moving scripts across platforms.
  The mapping between points and pixels is set when the application starts, based on properties of
  the installed monitor, but it can be overridden by calling the ``tk scaling`` command.」
  换算实现在 ``generic/tkFont.c`` 的 ``TkFontGetPixels`` / ``TkFontGetPoints``（按
  ``WidthOfScreen``/``WidthMMOfScreen``，即显示器 DPI）；aqua 最终把字号交给 NSFont 的
  ``pointSize``（``macosx/tkMacOSXFont.c``）。⇒ 像素字号是「绑死某台显示器」的量，
  跨平台要用点值。
* 修饰键：Tk ``generic/tkBind.c`` 的 ``modArray``：``{"Command", Mod1Mask, 0}``、
  ``{"Option", Mod2Mask, 0}``。⇒ ``<Command-Key-1>`` 是合法 Tk 事件，仅在 aqua 上由
  Command 键触发；Windows/Linux 的 Mod1 是 Alt。
* 菜单：``macosx/tkMacOSXMenus.c`` 的 ``_defaultApplicationMenuItems``（About / Preferences… /
  Services / Hide / Hide Others / Show All / Quit）与 ``tkMacOSXMenu.c`` 里
  ``SPECIALMENU(apple, APPLE)``（名为 ``.apple`` 的菜单 = 应用菜单）。
  ``doc/tk_mac.n``：``::tk::mac::Quit`` 未定义时 Tk 直接 ``exit``；
  ``::tk::mac::ShowPreferences`` 未定义时「Preferences」项置灰；
  ``tkAboutDialog`` 定义后接管标准 About 面板。
* 工作区：Tk 8.6 的 ``winfo`` 只有 ``screenwidth/screenheight``（**含**任务栏/Dock），
  没有「可用工作区」API（``winfo workarea`` 不是 8.6 的子命令，Xvfb 实测会报
  ``bad option "workarea"``）。⇒ 只能按边距保守收缩。

红线
====

* 本模块**不改**任何既有接口；``app.py``/``ui_kit.py`` 的接入由 lead 在合并 IA 后统一应用。
* 不引入第三方依赖（只用 ``sys``/``os``/``typing``）。
* macOS 分支在这里只做「按文档应当如此」的防御性实现，**真机未验证**的项在报告里单列。

"""
from __future__ import annotations

import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_MARGIN",
    "IS_LINUX",
    "IS_MACOS",
    "IS_WINDOWS",
    "MACOS_POINTS_PER_PX",
    "bind_page_wheel",
    "bind_wheel",
    "display_available",
    "enable_high_dpi",
    "fit_geometry",
    "fit_minsize",
    "font_size_arg",
    "initial_geometry",
    "install_macos_menu",
    "is_self_scrolling",
    "modifier_bindings",
    "normalize_wheel",
    "primary_modifier",
    "scale_value",
    "screen_size",
    "system_scale",
    "ui_scale",
    "wheel_sequences",
    "windowingsystem",
]

# ─────────────────────────────────────────────────────────── 平台常量

#: Windows（``sys.platform`` 在这三个平台上互斥）
IS_WINDOWS: bool = sys.platform == "win32"
#: macOS（Tk 的 windowingsystem 叫 ``aqua``）
IS_MACOS: bool = sys.platform == "darwin"
#: Linux/其它 POSIX（Tk 的 windowingsystem 叫 ``x11``；Tk 8.6 没有原生 Wayland 端口）
IS_LINUX: bool = not IS_WINDOWS and not IS_MACOS

#: macOS 上「Windows 的 1px」折算成点：Windows 96dpi 下 1px = 1/96 英寸，
#: macOS/Cocoa 1pt = 1/72 英寸 ⇒ pt = px * 72/96 = px * 0.75。
#: 依据：Tk ``font(n)``「Sizes should normally be specified in points」+ 两平台的
#: 名义 DPI 约定（Windows 96、Cocoa 72）。这是**换算口径**，不是渲染结果；
#: 真机上 Tk 的 px→pt 还要过一遍 ``tk scaling``，见 :func:`font_size_arg`。
MACOS_POINTS_PER_PX: float = 72.0 / 96.0

#: 窗口收缩时的默认边距（px）。Tk 8.6 拿不到系统「工作区」，只能留白：
#: 覆盖 Windows 任务栏（100% 缩放 ~40–48px）、macOS 菜单栏+Dock（~24+）、
#: Linux 面板（24–48px）的典型值，并留一点拖动余量。
DEFAULT_MARGIN: int = 32

#: 自带滚轮语义的控件类名（指针在它们上面时，页面滚动必须让位，
#: 否则一次滚轮会同时滚表格和整页 —— ``app.py`` 现在用 ``bind_all`` 就有这个问题）。
_SELF_SCROLLING_CLASSES: frozenset[str] = frozenset({
    "Treeview", "Text", "Listbox", "TCombobox", "TSpinbox", "TScrollbar",
})

_WHEEL_UP = 1        #: 归一化结果：正数 = 向上（内容向上走 / yview 减小）
_WHEEL_DOWN = -1
_MAX_STEPS = 10      #: 单次事件最多折算 10 格，防止某些驱动一次给 1000+ 把页面甩到底


# ─────────────────────────────────────────────────────────── 平台探测

def windowingsystem(root: Any = None) -> str:
    """返回 Tk 的 ``windowingsystem``：``"x11"`` / ``"aqua"`` / ``"win32"``。

    为什么先问 Tk 再退回 ``sys.platform``：判据应该来自**实际在跑的那个 Tk**，
    而不是 Python 解释器的平台。XWayland、WSLg、Python 里被 mock 的 sys.platform
    都会让两者不一致（本模块的单测就靠 ``windowsystem`` 参数注入来跑三个平台分支）。

    ``root`` 可以是 Tk 根窗口、任意 Tk 控件，或 ``None``（无窗口场景，例如导入期）。
    """
    interp = getattr(root, "tk", None)
    if interp is not None:
        try:
            return str(interp.call("tk", "windowingsystem"))
        except Exception:                     # 解释器已销毁 / 假对象
            pass
    if IS_WINDOWS:
        return "win32"
    if IS_MACOS:
        return "aqua"
    return "x11"


def display_available() -> bool:
    """有没有可用的图形会话（与 ``app.py`` / ``tray/app.py`` 的 ``_has_display`` 同语义）。

    * Windows/macOS：系统层面总有图形会话语义，直接 True。
    * Linux：看 ``DISPLAY``（X11/XWayland）或 ``WAYLAND_DISPLAY``。

    关于 Wayland（审计结论：**保持现状**）：Tk 8.6 只有 X11 端口，纯 Wayland
    会话（只有 ``WAYLAND_DISPLAY``、没有 XWayland）里 ``Tk()`` 必然抛 TclError。
    本函数返回 True 并不是 bug —— 调用方随后会把它转成 ``HeadlessError`` 并返回
    退出码 3，行为是安全的；改成 False 反而会在「XWayland 只在启动后注入 DISPLAY」
    的环境里误判。真正值得改的是**提示语**（见 03-CROSSPLAT-AUDIT.md 的 P2 项）。

    为什么放进本模块：`app.py` 与 `tray/app.py` 各有一份逐字相同的副本，
    平台判定应当只有一个单源。
    """
    return True if (IS_WINDOWS or IS_MACOS) else bool(
        _env("DISPLAY") or _env("WAYLAND_DISPLAY"))


def _env(name: str) -> str:
    import os
    return str(os.environ.get(name) or "")


def screen_size(root: Any) -> tuple[int, int]:
    """``(宽, 高)``：整个屏幕的像素尺寸（**含**任务栏/菜单栏/Dock；Tk 8.6 无工作区 API）。"""
    try:
        return int(root.winfo_screenwidth()), int(root.winfo_screenheight())
    except Exception:
        return 0, 0


# ─────────────────────────────────────────────────────────── 滚轮

def wheel_sequences(widget_or_root: Any = None, *,
                    windowingsystem: str | None = None) -> tuple[str, ...]:
    """本平台**实际会到货**的滚轮事件序列。

    * x11：``('<Button-4>', '<Button-5>')``（Tk ``ttk/utils.tcl`` 的 X11 分支；
      4 = 上、5 = 下 —— 这是 X11 的扩展按钮约定）。
    * Windows/macOS：``('<MouseWheel>',)``（``delta`` 正 = 上）。

    只返回「会触发」的那些：在 X11 上绑 ``<MouseWheel>`` 是死代码
    （``listbox.tcl``：「The MouseWheel will typically only fire on Windows and Mac OS X」），
    在 Windows 上绑 ``<Button-4/5>`` 同样是死代码（除非 ``event generate`` 造事件）。

    两种调用方式等价：传 Tk 控件/根窗口（问 Tk 自己），或显式传
    ``windowingsystem="x11"``（单测/无窗口时）。
    """
    ws = windowingsystem or _ws_of(widget_or_root)
    return ("<Button-4>", "<Button-5>") if ws == "x11" else ("<MouseWheel>",)


def _ws_of(value: Any) -> str:
    """把「控件/根窗口」或「windowingsystem 字符串」统一成窗口系统名。"""
    if isinstance(value, str) and value in ("x11", "aqua", "win32"):
        return value
    return windowingsystem(value)


def normalize_wheel(event: Any, *, windowingsystem: str | None = None) -> int:
    """把任意平台的滚轮事件归一化成「格数」，**正数 = 向上**。

    三个平台的量纲完全不同（这函数存在的全部理由）：

    * X11：``event.num`` 是 4（上）/5（下），**没有 delta**。
    * Windows：``event.delta`` 是 120 的整数倍（一格 = 120）；高分辨率滚轮可能给 60。
    * macOS/aqua：Tk 的 ``%D`` 已经是「行」单位（``ttk/utils.tcl`` 里 aqua 分支直接
      ``expr {-%D}``，Windows 分支才 ``/120``），所以**不能再除 120**，只做限幅。

    返回 0 表示「这事件不该被当成滚轮」（delta 缺失/为 0、num 不是 4/5）。
    调用方应当把 0 当成「不处理」而不是「向上滚一格」。

    参数 ``windowingsystem`` 只为单测注入（``"x11"``/``"aqua"``/``"win32"``）。
    """
    num = getattr(event, "num", None)
    if num == 4:                                    # X11 上滚
        return _WHEEL_UP
    if num == 5:                                    # X11 下滚
        return _WHEEL_DOWN

    try:
        delta = int(getattr(event, "delta", 0) or 0)
    except (TypeError, ValueError):
        return 0
    if delta == 0:
        return 0

    ws = windowingsystem or _windowing_system_from_platform()
    if ws == "aqua":
        # aqua 的 delta 已经是行数：保留量级（触控板一次可能给 3~5），只限幅。
        return _clamp(delta)
    if ws == "x11":
        # 理论上到不了这里（X11 没有 MouseWheel）；若被 event_generate 造出来，
        # 按 Tk 自己的口径当「有方向的一次」处理，不放大。
        return _WHEEL_UP if delta > 0 else _WHEEL_DOWN
    # win32：120 = 一格
    steps = int(delta / 120)
    if steps == 0:
        steps = _WHEEL_UP if delta > 0 else _WHEEL_DOWN
    return _clamp(steps)


def _clamp(value: int) -> int:
    return max(-_MAX_STEPS, min(_MAX_STEPS, int(value)))


def _windowing_system_from_platform() -> str:
    if IS_WINDOWS:
        return "win32"
    if IS_MACOS:
        return "aqua"
    return "x11"


def bind_wheel(widget: Any, handler: Callable[[Any], Any], *, add: str = "+",
               windowingsystem: str | None = None) -> tuple[str, ...]:
    """把 ``handler`` 绑到**本平台真实会到货**的滚轮序列上，返回绑了哪些。

    为什么不是 ``bind_all("<MouseWheel>")``：见 :func:`wheel_sequences`。
    X11 的滚轮是 ``Button-4/5``，绑 ``MouseWheel`` 等于没绑（这就是 Linux 上
    整页滚不动的根因）。

    ``add="+"`` 是默认值：桌面端有「页面滚动」和「控件自带滚动」两套绑定共存，
    用 ``"+"`` 追加、不覆盖别处的绑定（Tk 默认 ``add=""`` 会**替换**同名序列）。

    注意：本函数只负责「绑」，不做「该不该滚」的判断；需要让位给 Treeview/Text 时
    用 :func:`bind_page_wheel`（或自行调用 :func:`is_self_scrolling`）。
    """
    sequences = wheel_sequences(windowingsystem=windowingsystem)
    for sequence in sequences:
        widget.bind(sequence, handler, add=add)
    return sequences


def is_self_scrolling(widget: Any, *, skip: Iterable[str] = ()) -> bool:
    """这个控件是不是「自己会滚」的（Treeview / Text / Listbox / 下拉列表 / 滚动条…）。

    为什么需要它：Tk 给 ``TtkScrollable``（Treeview 继承它）在 X11 上绑了
    ``<Button-4/5> → yview scroll ±5 units``。页面的 ``bind_all`` 处理器如果无条件
    再滚一次，指针停在表格上时**表格和整页会一起动**（互相抢）。
    ``skip`` 允许调用方额外排除类名（例如自定义的可滚 Canvas）。

    判断用 ``winfo_class()``（Tk 的类绑定名），不是 Python 类：ttk 控件在 Tk 侧的类名
    才是绑定表里的键。取不到（窗口已销毁/假对象）时返回 False —— 宁可多滚一次页面，
    也不要在正常控件上静默失效。
    """
    if widget is None:
        return False
    try:
        name = str(widget.winfo_class())
    except Exception:
        return False
    return name in _SELF_SCROLLING_CLASSES or name in set(skip)


def bind_page_wheel(root: Any, canvas: Any, *, units: int = 1,
                    skip: Iterable[str] = (),
                    windowingsystem: str | None = None) -> tuple[str, ...]:
    """给「整页滚动」装滚轮：``bind_all`` + 在自带滚动的控件上让位。

    与 ``app.py`` 现有写法（``bind_all("<MouseWheel>")`` 直接滚 canvas）的差别：

    1. 按平台绑对序列 —— X11 用 ``Button-4/5``，否则 Linux 上完全没反应；
    2. 指针在 Treeview/Text/Listbox/下拉/滚动条上时**不接管**（``is_self_scrolling``），
       让控件自己的类绑定干活，避免「滚一格，表格和页面各走一遍」；
    3. 事件被消费时返回 ``"break"``，阻止后续 bindtag 重复处理。

    ``units`` = 一「格」滚多少 canvas 单位（Canvas 未设 ``yscrollincrement`` 时
    1 单位 ≈ 视口高度的 1/10）。默认 1，与 ``app.py`` 现状一致，避免改观感。

    返回已绑定的序列，供测试断言「X11 上确实绑的是 Button-4/5」。
    """
    ws = windowingsystem or _windowing_system_from(root)
    sequences = wheel_sequences(windowingsystem=ws)

    def _on_wheel(event: Any) -> Any:
        if is_self_scrolling(getattr(event, "widget", None), skip=skip):
            return None                       # 让控件自己滚，别抢
        steps = normalize_wheel(event, windowingsystem=ws)
        if steps == 0:
            return None
        try:
            canvas.yview_scroll(-steps * max(1, int(units)), "units")
        except Exception:                     # 画布已销毁（切视图竞态）
            return None
        return "break"

    for sequence in sequences:
        root.bind_all(sequence, _on_wheel, add="+")
    return sequences


def _windowing_system_from(root: Any) -> str | None:
    """从 root 取真实 windowingsystem；取不到返回 None 让下游回落到平台常量。"""
    return windowingsystem(root) if getattr(root, "tk", None) is not None else None


# ─────────────────────────────────────────────────────────── DPI（Windows 为主）

#: ``enable_high_dpi()`` 的结果与选中的模式（``per-monitor-v2`` / ``per-monitor`` /
#: ``system`` / ``already-aware`` / ``None``），供报告与日志引用；进程级，一旦定值不再变。
_DPI_ENABLED: bool = False
_DPI_MODE: str | None = None

#: Windows 的 DPI 常量（见 Microsoft「High DPI desktop application development」）
_DPI_AWARENESS_UNAWARE = 0
_DPI_AWARENESS_SYSTEM = 1
_DPI_AWARENESS_PER_MONITOR = 2
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_LOGPIXELSX = 88


def _win_api() -> Any:
    """返回 ``ctypes.windll``（只在 Windows 上有），否则 ``None``。

    单测靠 monkeypatch 本函数注入假的 ``windll`` —— 这样「V2 失败→per-monitor→
    system→放弃」这条回退链在 Linux 上也能被测到，不用真 Windows 机器。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        return ctypes.windll
    except Exception:                    # pragma: no cover - 精简 Python 没有 ctypes
        return None


def _windows_dpi_awareness(api: Any) -> int | None:
    """当前进程的 DPI 感知级别：0=unaware / 1=system / 2=per-monitor；查不到返回 None。

    用 ``shcore.GetProcessDpiAwareness(None, &value)``（Win8.1+）：这是文档里的查询接口，
    比 ``GetThreadDpiAwarenessContext`` 少一个「opaque 句柄按指针传递」的坑。
    """
    try:
        import ctypes
        buf = ctypes.c_int(_DPI_AWARENESS_UNAWARE)
        result = int(api.shcore.GetProcessDpiAwareness(None, ctypes.byref(buf)))
        if result == 0:                     # S_OK
            return int(buf.value)
    except Exception:                       # 老系统没有 shcore / 假 api
        pass
    return None


def enable_high_dpi() -> bool:
    """**在创建任何 Tk 窗口之前**声明进程 DPI 感知（Windows）；其它平台 no-op。

    为什么必须有这一步（这是「字体丑」与「右侧被裁」的一部分根因）
    ------------------------------------------------------------

    Windows 上的实际桌面是 2560×1600 @125%（逻辑 2048×1280）。Python/Tk 8.6 默认
    **DPI-unaware**，于是 Windows 的 DWM 把整块窗口按 1.25× **位图拉伸**：

    * 1920 逻辑宽的应用被画成 2400 物理像素再缩回 1920 —— 字发糊、细线变灰；
    * 应用以为自己是 1920 宽，实际可用逻辑宽只有 2048，右侧被裁掉约 275 逻辑 px
      （上一轮实拍里「第三张卡被切」就是这个，不是字体问题）；
    * 截图/坐标全按 96dpi 记账，和真实屏幕对不上。

    置为 Per-Monitor-V2 之后，Tk 拿到真实的 120dpi（``winfo fpixels 1i`` = 120），
    字由 GDI/DirectWrite 按真实像素绘制，不再被拉伸。

    **只开感知不缩放会整体变小 20%** —— 125% 下，设计稿的 1180px 窗口现在真的只有
    1180 物理像素 = 944 逻辑像素；所以必须同时用 :func:`ui_scale` 把几何/字号/间距
    乘上 1.25（见 :func:`scale_value` / :func:`font_size_arg` 的 ``scale`` 参数）。
    接入顺序：``main()`` 第一行调用本函数 → 再建 Tk → 读 :func:`ui_scale` → 缩放。

    回退链（逐级降级，任何一级成功就停）：
    ``SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)``（Win10 1703+）→
    ``shcore.SetProcessDpiAwareness(2)``（Win8.1+）→ ``user32.SetProcessDPIAware()``（Vista+）。

    返回值：进程最终是否处于「至少 system-aware」状态。非 Windows 恒 ``False``
    （macOS 的 Retina 由 Cocoa 自己处理，Linux 由 Tk 的 ``tk scaling`` 处理）。
    幂等：同一进程重复调用只有第一次真的调 Win32。

    为什么必须在 Tk 之前：MSDN 明确「DPI 感知只能在创建任何窗口之前设置」，
    窗口一旦建好再改，已有窗口的 DPI 上下文不会更新 —— 那就是「一半清晰一半糊」。
    """
    global _DPI_ENABLED, _DPI_MODE
    if not IS_WINDOWS:
        return False
    if _DPI_ENABLED:
        return True
    api = _win_api()
    if api is None:
        return False

    aware = _windows_dpi_awareness(api)
    if aware is not None and aware >= _DPI_AWARENESS_SYSTEM:
        _DPI_ENABLED, _DPI_MODE = True, "already-aware"
        return True

    attempts = (
        ("per-monitor-v2", "user32", "SetProcessDpiAwarenessContext",
         (_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,)),
        ("per-monitor", "shcore", "SetProcessDpiAwareness", (_DPI_AWARENESS_PER_MONITOR,)),
        ("system", "user32", "SetProcessDPIAware", (True,)),
    )
    for mode, lib, name, args in attempts:
        try:
            library = getattr(api, lib)
            function = getattr(library, name)
            if args == (True,):
                ok = bool(function())
            elif mode == "per-monitor":
                ok = int(function(*args)) == 0          # S_OK
            else:
                ok = bool(function(*args))
        except Exception:                     # 老系统没有这个函数 / 调用签名不符
            continue
        if ok:
            _DPI_ENABLED, _DPI_MODE = True, mode
            return True
    return False


def dpi_mode() -> str | None:
    """``enable_high_dpi()`` 选中的模式（未调用或失败返回 ``None``）。诊断/日志用。"""
    return _DPI_MODE


def system_scale() -> float:
    """**不需要 Tk** 就能拿到「系统缩放倍数」（1.0 / 1.25 / 1.5 …）。

    按优先级：

    1. ``DJB_UI_SCALE`` 环境变量（运维/截图脚本显式钉住，最高优先级）；
    2. Windows：``user32.GetDpiForSystem()/96``（Win10 1607+），失败退回
       ``gdi32.GetDeviceCaps(GetDC(0), LOGPIXELSX)/96``（Vista+ 都有）；
    3. Linux：``XFT_DPI`` 环境变量 → ``xrdb -query`` 里的 ``Xft.dpi`` → 1.0；
    4. macOS：**返回 1.0 并标注未验证** —— Retina 的 2x 是「物理像素 vs 逻辑点」，
       不是 Windows 那种「逻辑像素 vs 物理像素」的缩放，Tk 又只在建窗后才给
       ``tk scaling``；用 :func:`ui_scale`（有 Tk）才是准的。真机需复核。

    用途：在还没有窗口时（读配置、算初始几何、给 ``Fonts`` 定字号）就能按比例取值。
    """
    override = _float_env("DJB_UI_SCALE")
    if override:
        return _clamp_scale(override)

    if IS_WINDOWS:
        api = _win_api()
        if api is not None:
            dpi = None
            try:
                dpi = int(api.user32.GetDpiForSystem())
            except Exception:
                dpi = None
            if not dpi:
                try:
                    hdc = api.user32.GetDC(0)
                    dpi = int(api.gdi32.GetDeviceCaps(hdc, _LOGPIXELSX))
                    api.user32.ReleaseDC(0, hdc)
                except Exception:
                    dpi = None
            if dpi:
                return _clamp_scale(dpi / 96.0)
        return 1.0

    if IS_MACOS:
        return 1.0                        # 未验证：Retina 需用 ui_scale(root) 实测

    for name in ("XFT_DPI", "QT_SCALE_FACTOR"):
        value = _float_env(name)
        if value:
            # XFT_DPI 是 dpi（120），QT_SCALE_FACTOR 是倍数（1.25）
            return _clamp_scale(value / 96.0 if name == "XFT_DPI" else value)
    value = _float_env("GDK_SCALE")
    if value:
        return _clamp_scale(value)
    dpi = _xrdb_dpi()
    if dpi:
        return _clamp_scale(dpi / 96.0)
    return 1.0


def ui_scale(root: Any) -> float:
    """Tk 建好之后读真实缩放：``winfo fpixels 1i / 96``（= 1.0 / 1.25 / 1.5）。

    为什么用这个而不是 ``tk scaling``：``tk scaling`` 是「像素/点」，还要再乘 72 才是
    DPI；``winfo fpixels 1i`` 直接就是「一英寸是多少像素」，除以 96 就是相对设计稿
    （96dpi）的倍率，少一层换算就少一处错。

    与 :func:`system_scale` 的关系：Tk 能开窗就用这个（最准），否则用那个。
    取不到（无 Tk、假对象）时回落到 :func:`system_scale`。

    **macOS 例外（未验证项）**：aqua 的窗口坐标是「逻辑点」，Retina 的 2× 由 Cocoa
    在合成层处理，不存在 Windows 那种「96dpi 设计稿 → 120dpi 设备像素」的放大；
    硬套 ``fpixels('1i')/96`` 会在 ``tk scaling`` 不是 1.333 的机器上把所有尺寸改错。
    所以 aqua 一律返回 1.0，字体仍走 :func:`font_size_arg` 的点值换算。
    真机复核办法见 03-CROSSPLAT-AUDIT.md（``winfo fpixels 1i`` / ``tk scaling``）。
    """
    try:
        if windowingsystem(root) == "aqua":
            return 1.0
        return _clamp_scale(float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return system_scale()


def scale_value(value: int | float, scale: float) -> int:
    """把设计稿 px 乘上缩放倍数并取整（几何/间距/列宽统一走这里）。"""
    try:
        return int(round(float(value) * max(0.0, float(scale))))
    except (TypeError, ValueError):
        return int(value)                 # type: ignore[arg-type]


def _clamp_scale(value: float) -> float:
    return max(0.5, min(4.0, round(float(value), 3)))


def _float_env(name: str) -> float | None:
    raw = _env(name).strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _xrdb_dpi() -> float | None:
    """从 X 资源里读 ``Xft.dpi``（Linux 上最常见的 DPI 来源）。

    只跑一次 ``xrdb -query``（2 秒超时），失败就返回 None —— 这是**尽力而为**：
    拿不到就按 1.0 处理，和 Tk 自己的默认行为一致（Tk 用 ``WidthMMOfScreen`` 算 DPI）。
    """
    try:
        import subprocess
        result = subprocess.run(["xrdb", "-query"], capture_output=True, text=True,
                                timeout=2, check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "xft.dpi":
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


# ─────────────────────────────────────────────────────────── 字号

def font_size_arg(px: int, *, scale: float = 1.0, dpi: float = 96.0) -> int:
    """把「设计稿里的 px 字号」变成**当前平台该传给 Tk 的 size 参数**。

    * Windows / Linux（X11）：返回**负值**（Tk 的负值 = 像素），保持既有观感不变；
    * macOS（aqua）：返回**正的点值** ``round(px * 72/dpi)``，dpi 默认 96（Windows 口径）。

    为什么 macOS 必须换成点值（依据 Tk ``font(n)`` 手册原文）：

    1. 负值 = 像素，而「点↔像素的映射在程序启动时按显示器属性定死」（同一段手册原文），
       所以像素字号在 Retina/外接屏之间、以及和 Windows 之间都不保证同样的物理大小；
    2. 手册明确建议 「Sizes should normally be specified in points so the application will
       remain the same ruler size on the screen … even when moving scripts across platforms」；
    3. aqua 的字体最终交给 ``NSFont`` 的 ``pointSize``（``macosx/tkMacOSXFont.c``），
       点值才是 Cocoa 的原生单位。

    换算口径：设计稿是 Windows 96dpi 下的 px（设计基线就是 Windows 实拍），
    ``1px = 1/96 in``，``1pt = 1/72 in`` ⇒ ``pt = px * 72/96``。13px → 10pt、19px → 14pt、
    30px → 23pt。**这里给的是「与 Windows 同物理尺寸」的值**；真机上 Tk 还会按
    ``tk scaling`` 再做一次映射，实际观感必须真机复核（报告里标「未验证」）。

    为什么不用 ``tk scaling`` 自己算：那个值要窗口存在才能读，而字号常量在 import 期就要
    定值（``ui_kit.PX_*``）；把「读 scaling」放进渲染路径会让字体缓存失效、切换屏幕后
    不刷新。用它做**一次性校准**是后续工作，不在本轮（见报告 P1 未验证项）。

    ``scale`` 是 DPI 缩放倍数（:func:`ui_scale` / :func:`system_scale`）：Tk 的像素字号
    是**设备像素**，125% 下不乘 1.25 就会比设计稿小 20%。Windows/Linux 走
    ``-(px*scale)``，macOS 走 ``px*scale*72/dpi`` 的点值。

    ``dpi`` 参数只为单测/将来校准注入。
    """
    if int(px) <= 0:
        return 0                          # Tk 的 0 = 平台默认字号，别改成 -1
    effective = float(px) * max(0.0, float(scale))
    if IS_MACOS:
        return max(1, int(effective * 72.0 / max(1.0, float(dpi)) + 0.5))
    return -max(1, int(effective + 0.5))


# ─────────────────────────────────────────────────────────── 窗口几何

def fit_geometry(width: int, height: int, screen_w: int, screen_h: int, *,
                 margin: int = DEFAULT_MARGIN) -> tuple[int, int]:
    """按屏幕尺寸收缩窗口，四周各留 ``margin`` px，返回 ``(w, h)``。

    为什么需要：``app.py`` 现在硬开 ``1180x780``，``minsize(900,620)``。
    1366×768 的笔记本（减掉任务栏后可用高度约 728）上，780 > 728，窗口底部（状态条）
    落到屏幕外，用户既看不到也很难拖回来。这是**实测**的（见
    ``evidence/xplat/geom_probe.log``：``winfo screenheight=728``，请求 780）。

    为什么用「屏幕尺寸 − 2×margin」而不是精确工作区：Tk 8.6 的 ``winfo`` 没有
    ``workarea`` 子命令（Xvfb 实测 ``bad option "workarea"``），拿不到任务栏/Dock 高度；
    只能用一个覆盖三大平台典型值的边距（见 :data:`DEFAULT_MARGIN`）保守收缩。

    约束：
    * 只**缩小**、不放大：大屏（3440×1440）上仍按设计尺寸开窗，不会被撑满；
    * 屏幕本身就比目标尺寸小时以屏幕为准（宁可小窗，不可出界）；
    * 入参非法（0/负数，例如无头环境里 ``winfo_screenwidth`` 返回 1）时原样返回，
      让调用方保持原行为 —— 启动路径上不许炸。
    """
    try:
        width, height = int(width), int(height)
        screen_w, screen_h = int(screen_w), int(screen_h)
        margin = max(0, int(margin))
    except (TypeError, ValueError):
        return (width, height)
    if width <= 0 or height <= 0 or screen_w <= 1 or screen_h <= 1:
        return (width, height)                # 屏幕尺寸不可信：不猜
    usable_w = max(1, screen_w - 2 * margin)
    usable_h = max(1, screen_h - 2 * margin)
    return (max(1, min(width, usable_w)), max(1, min(height, usable_h)))


def fit_minsize(min_width: int, min_height: int, screen_w: int, screen_h: int, *,
                margin: int = DEFAULT_MARGIN) -> tuple[int, int]:
    """:meth:`fit_geometry` 的配套：把 ``minsize`` 也压到屏幕可用范围内。

    为什么必须一起改：只收缩初始几何、却把 ``minsize(900,620)`` 留着，
    在小屏（例如 1024×600 的上网本）上窗口管理器会把窗口**顶回** 620 高，
    于是「收缩」白做，状态条仍然出界。两条约束必须同源，否则修一半等于没修。
    """
    return fit_geometry(min_width, min_height, screen_w, screen_h, margin=margin)


def initial_geometry(root: Any, width: int, height: int, *,
                     margin: int = DEFAULT_MARGIN) -> str:
    """给 ``root.geometry()`` 用的字符串（如 ``"1180x664"``），已按当前屏幕收缩。

    接入点比 :func:`fit_geometry` 少一次转手：窗口必须先 ``Tk()`` 创建，
    ``winfo_screenwidth`` 才有值（``app.py`` 里就是这个顺序）。
    """
    screen_w, screen_h = screen_size(root)
    fitted_w, fitted_h = fit_geometry(width, height, screen_w, screen_h, margin=margin)
    return str(fitted_w) + "x" + str(fitted_h)


# ─────────────────────────────────────────────────────────── 快捷键

def primary_modifier() -> str:
    """本平台的主修饰键名（直接拼进 Tk 事件串）：macOS = ``"Command"``，其余 ``"Control"``。

    依据：Tk ``generic/tkBind.c`` 的 ``modArray`` 里 ``{"Command", Mod1Mask, 0}`` ——
    ``Command`` 是 Tk 认识的事件修饰名；在 aqua 上 Command 键产生的就是 Mod1，
    在 Windows/Linux 上 Mod1 是 Alt，所以 ``<Command-…>`` 在非 aqua 平台上永远不触发
    （不是报错，是静默无效 —— 必须按平台分开绑）。
    """
    return "Command" if IS_MACOS else "Control"


def modifier_bindings(accelerator: str, *, platform: str | None = None) -> tuple[str, ...]:
    """把「不含修饰键的按键」展开成本平台该绑的全部事件串（含 macOS 的 Control 别名）。

    ``accelerator`` 形如 ``"Key-1"``、``"r"``（Tk 的按键名；纯数字必须带 ``Key-`` 前缀，
    否则会被解析成按钮/修饰符）。

    * macOS：``('<Command-Key-1>', '<Control-Key-1>')`` —— Command 是系统预期，
      但 Control 也留着（外接 PC 键盘/习惯 Control 的用户、以及 Tk 默认的
      ``Control-q`` 之类不会冲突：本应用没绑 Control-q）。
    * Windows/Linux：``('<Control-Key-1>',)``。

    为什么不无脑两边都绑：在 Windows 上 ``<Command-…>`` 是死绑定（见
    :func:`primary_modifier`），绑了只会让人误以为「已经适配了 mac」。
    """
    macos = IS_MACOS if platform is None else (platform == "macos")
    primary = "Command" if macos else "Control"
    sequences = ["<" + primary + "-" + accelerator + ">"]
    if macos:
        sequences.append("<Control-" + accelerator + ">")
    return tuple(sequences)


# ─────────────────────────────────────────────────────────── macOS 应用菜单

def install_macos_menu(root: Any, actions: Mapping[str, Any] | None = None, *,
                       assume_macos: bool | None = None) -> bool:
    """装 macOS 应用菜单（About / Preferences / Quit），并把退出接到应用自己的清理上。

    非 macOS **直接 no-op**（返回 False），Windows/Linux 一个控件都不碰。

    为什么需要（依据 ``macosx/tkMacOSXMenus.c`` + ``doc/tk_mac.n``）：

    * Tk 在 aqua 上确实会造一个默认应用菜单（``_defaultApplicationMenuItems``：
      About / Preferences… / Services / Hide / Hide Others / Show All / Quit），所以
      「完全没菜单」不成立；但 Quit/Preferences/About 都是 Tk 的通用实现：
      - 「Quit」调用 ``terminate:`` → 若 Tcl 里没定义 ``::tk::mac::Quit``，Tk **直接 exit**，
        绕过 ``WM_DELETE_WINDOW`` / 控制器 shutdown，可能留下半个写文件或没停的 worker；
      - 「Preferences…」只在 ``::tk::mac::ShowPreferences`` 存在时才可用，否则置灰；
      - 「About」走 ``orderFrontStandardAboutPanel:``，只有定义了 ``tkAboutDialog``
        才会被接管，否则显示 Python/Wish 的通用面板。
    * 菜单名 ``apple`` 是 Tk 的特殊名（``SPECIALMENU(apple, APPLE)``）：把它作为
      menubar 的第一项，Tk 就会把上面那串标准项**追加**进来，我们只补自己的 About。

    参数 ``actions`` 可含（全部可选，缺省就跳过对应项）：
    ``about`` / ``preferences`` / ``quit``（无参 callable）、``app_name``（str）、
    ``hide`` / ``show``（无参 callable，可选）。

    幂等：同一个 root 上重复调用只有第一次生效（返回 False），不会叠出两个菜单。
    所有 Tk 调用都包在 try/except 里 —— 菜单装不上是「少个菜单」，
    绝不能变成「客户端起不来」。

    ``assume_macos`` 只给单测用（在 Linux 上验证 darwin 分支的调用序列）。
    """
    macos = IS_MACOS if assume_macos is None else bool(assume_macos)
    if not macos or root is None:
        return False
    if getattr(root, "_djb_macos_menu", False):
        return False                          # 幂等：已经装过

    actions = dict(actions or {})
    app_name = str(actions.get("app_name") or "DJB 联脑")
    try:
        from tkinter import Menu                # 延迟 import：模块级不碰 tkinter
        menubar = Menu(root)
        apple = Menu(menubar, name="apple")     # 名字必须叫 apple，Tk 才当应用菜单
        menubar.add_cascade(menu=apple)
        about = actions.get("about")
        if callable(about):
            apple.add_command(label="关于 " + app_name, command=about)
        root.configure(menu=menubar)
    except Exception:
        return False                          # 窗口已销毁/无 aqua 支持：静默降级

    interp = getattr(root, "tk", None)
    if interp is not None:
        # ::tk::mac 命名空间在 aqua 下由 Tk 自己的库创建；仍显式保证一次，
        # 免得在精简 Tk 构建里 proc 定义失败。
        _tcl_quietly(interp, "namespace", "eval", "::tk::mac", "")
        for key, proc, handler in (
                ("quit", "::tk::mac::Quit", actions.get("quit")),
                ("preferences", "::tk::mac::ShowPreferences", actions.get("preferences")),
                ("about", "tkAboutDialog", actions.get("about")),
                ("hide", "::tk::mac::OnHide", actions.get("hide")),
                ("show", "::tk::mac::OnShow", actions.get("show")),
        ):
            if not callable(handler):
                continue
            # 先注册一个平命令（命名空间一定存在），再用 proc 转发：
            # 直接把 Tcl 命令建在不存在的命名空间里会报 "invalid command name"。
            name = "_djb_mac_" + key
            try:
                root.createcommand(name, handler)          # type: ignore[attr-defined]
                _tcl_quietly(interp, "proc", proc, "", name)
            except Exception:
                continue
    try:
        root._djb_macos_menu = True          # type: ignore[attr-defined]
    except Exception:                        # 某些假 root 不允许设属性
        pass
    return True


def _tcl_quietly(interp: Any, *args: Any) -> None:
    """执行 Tcl 命令，失败就算了（菜单/钩子装不上不影响启动）。"""
    try:
        interp.call(*args)
    except Exception:
        pass
