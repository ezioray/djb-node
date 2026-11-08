# SPDX-License-Identifier: Apache-2.0
"""``desktop/platform_ui`` 的回归：纯函数全覆盖 + 有 X11 时真开窗验证滚轮。

为什么要这一层
==============

跨平台缺陷（滚轮不响应、窗口出界、字号按像素写死、mac 没有 Command 快捷键）
的共性是「**在没有那个平台时看不出来**」。所以本文件分两半：

1. **纯函数**（不需要 DISPLAY）：用假 root/假 event 把三个平台的分支都走一遍，
   X11 的 ``num=4/5``、Windows 的 ``delta/120``、aqua 的「delta 已经是行」各钉一条。
2. **真窗口**（需要 DISPLAY）：在 Xvfb 下真开 Tk，绑 ``Button-4/5`` 并 ``event_generate``
   验证「页面会滚、表格自己滚时页面不抢」。没有 DISPLAY 时**登记 ENV-MISSING 再跳过**
   （``tests/env_missing``：缺环境的"未验证"不许静默消失，严格模式下直接失败）。

NAS 上跑（Xvfb :93）：

    Xvfb :93 -screen 0 1366x728x24 -nolisten tcp &
    DISPLAY=:93 /tmp/djb-ui-venv/bin/python -m pytest -o addopts="" -q \
        tests/test_desktop_platform_ui.py queries::

macOS 分支只能在这里「按文档」验证调用序列，真机观感未验证（见 03-CROSSPLAT-AUDIT.md）。
"""
from __future__ import annotations

import subprocess
import sys
import warnings
from types import SimpleNamespace

import pytest

from djb_node.desktop import platform_ui
from tests import env_missing


# ─────────────────────────────────────────────────────────── 测试替身

class FakeTcl:
    """假的 Tcl 解释器：只回答 ``tk windowingsystem``，其余调用记账。"""

    def __init__(self, windowingsystem: str = "x11") -> None:
        self.windowingsystem = windowingsystem
        self.calls: list[tuple[object, ...]] = []

    def call(self, *args: object) -> str:
        self.calls.append(tuple(args))
        if tuple(args[:2]) == ("tk", "windowingsystem"):
            return self.windowingsystem
        return ""

    def has_call(self, *prefix: object) -> bool:
        return any(tuple(args[:len(prefix)]) == tuple(prefix) for args in self.calls)


class FakeRoot:
    """假 Tk 根窗口：windowingsystem + bind_all 记账。"""

    def __init__(self, windowingsystem: str = "x11") -> None:
        self.tk = FakeTcl(windowingsystem)
        self.bound: dict[str, tuple[object, str]] = {}
        self.screen = (1366, 728)

    def bind_all(self, sequence: str, handler: object, add: str = "") -> None:
        self.bound[sequence] = (handler, add)

    def winfo_screenwidth(self) -> int:
        return self.screen[0]

    def winfo_screenheight(self) -> int:
        return self.screen[1]


class FakeWidget:
    def __init__(self, widget_class: str = "Frame") -> None:
        self.widget_class = widget_class
        self.bindings: dict[str, tuple[object, str]] = {}

    def bind(self, sequence: str, handler: object, add: str = "") -> None:
        self.bindings[sequence] = (handler, add)

    def winfo_class(self) -> str:
        return self.widget_class


class FakeCanvas:
    def __init__(self) -> None:
        self.scrolls: list[tuple[int, str]] = []
        self.dead = False

    def yview_scroll(self, amount: int, unit: str = "units") -> None:
        if self.dead:
            raise RuntimeError("canvas destroyed")
        self.scrolls.append((amount, unit))


class Exploding:
    """任何属性访问都炸：用来证明「非 macOS 分支一点 Tk 都不碰」。"""

    def __getattr__(self, name: str):        # pragma: no cover - 触发即失败
        raise AssertionError("非 macOS 路径不应访问 root." + name)


# ─────────────────────────────────────────────────────────── 平台常量 / 探测

def test_module_does_not_import_tkinter():
    """无图形环境（CI/服务端）也必须能 import 本模块。"""
    code = ("import sys; import djb_node.desktop.platform_ui;"
            "print('tkinter' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_platform_flags_are_exclusive():
    flags = [platform_ui.IS_WINDOWS, platform_ui.IS_MACOS, platform_ui.IS_LINUX]
    assert sum(1 for flag in flags if flag) == 1
    assert platform_ui.IS_WINDOWS == (sys.platform == "win32")
    assert platform_ui.IS_MACOS == (sys.platform == "darwin")


def test_windowingsystem_prefers_the_running_tk():
    assert platform_ui.windowingsystem(FakeRoot("aqua")) == "aqua"
    assert platform_ui.windowingsystem(FakeRoot("win32")) == "win32"
    # 没有 Tk（导入期 / 假对象）→ 退回解释器平台
    expected = "win32" if platform_ui.IS_WINDOWS else ("aqua" if platform_ui.IS_MACOS else "x11")
    assert platform_ui.windowingsystem(None) == expected


def test_display_available_is_env_driven_on_linux(monkeypatch):
    if platform_ui.IS_WINDOWS or platform_ui.IS_MACOS:
        assert platform_ui.display_available() is True
        return
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert platform_ui.display_available() is False
    monkeypatch.setenv("DISPLAY", ":93")
    assert platform_ui.display_available() is True
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert platform_ui.display_available() is True


# ─────────────────────────────────────────────────────────── 滚轮

def test_wheel_sequences_per_platform():
    assert platform_ui.wheel_sequences("x11") == ("<Button-4>", "<Button-5>")
    assert platform_ui.wheel_sequences("win32") == ("<MouseWheel>",)
    assert platform_ui.wheel_sequences("aqua") == ("<MouseWheel>",)


def test_normalize_wheel_x11_buttons():
    assert platform_ui.normalize_wheel(SimpleNamespace(num=4), windowingsystem="x11") == 1
    assert platform_ui.normalize_wheel(SimpleNamespace(num=5), windowingsystem="x11") == -1
    # tkinter 在非滚轮事件上把 num 填成 '??'：不能当滚轮
    assert platform_ui.normalize_wheel(SimpleNamespace(num="??", delta=0),
                                       windowingsystem="x11") == 0


@pytest.mark.parametrize("delta,expected", [
    (120, 1), (-120, -1), (240, 2), (-240, -2), (60, 1), (-60, -1), (0, 0),
])
def test_normalize_wheel_windows_notches(delta, expected):
    event = SimpleNamespace(num="??", delta=delta)
    assert platform_ui.normalize_wheel(event, windowingsystem="win32") == expected


@pytest.mark.parametrize("delta,expected", [(1, 1), (-1, -1), (3, 3), (-5, -5),
                                            (999, 10), (-999, -10)])
def test_normalize_wheel_macos_keeps_magnitude(delta, expected):
    """aqua 的 delta 已经是「行」：不能再除 120，只限幅。"""
    event = SimpleNamespace(num="??", delta=delta)
    assert platform_ui.normalize_wheel(event, windowingsystem="aqua") == expected


@pytest.mark.parametrize("event", [
    SimpleNamespace(),                                       # 什么都没有
    SimpleNamespace(delta=None),
    SimpleNamespace(delta="not-a-number"),
])
def test_normalize_wheel_returns_zero_for_non_wheel_events(event):
    assert platform_ui.normalize_wheel(event, windowingsystem="win32") == 0


def test_normalize_wheel_defaults_to_current_platform():
    """不传 windowingsystem 时按解释器平台判定（Windows 机器上 120 = 一格）。"""
    event = SimpleNamespace(num="??", delta=120)
    expected = 120 if platform_ui.IS_MACOS else 1
    assert platform_ui.normalize_wheel(event) == expected


def test_bind_wheel_uses_platform_sequences():
    x11_widget = FakeWidget()
    bound = platform_ui.bind_wheel(x11_widget, lambda _e: None, windowingsystem="x11")
    assert bound == ("<Button-4>", "<Button-5>")
    assert set(x11_widget.bindings) == {"<Button-4>", "<Button-5>"}
    assert all(add == "+" for _handler, add in x11_widget.bindings.values())

    mac_widget = FakeWidget()
    bound = platform_ui.bind_wheel(mac_widget, lambda _e: None, windowingsystem="aqua")
    assert bound == ("<MouseWheel>",)
    assert set(mac_widget.bindings) == {"<MouseWheel>"}


def test_bind_page_wheel_scrolls_and_yields_to_self_scrolling_widgets():
    root, canvas = FakeRoot("x11"), FakeCanvas()
    sequences = platform_ui.bind_page_wheel(root, canvas, windowingsystem="x11")
    assert sequences == ("<Button-4>", "<Button-5>")
    assert set(root.bound) == {"<Button-4>", "<Button-5>"}

    handler = root.bound["<Button-5>"][0]
    # 指针在普通控件上 → 页面滚一格，并消费事件
    assert handler(SimpleNamespace(num=5, widget=FakeWidget("Frame"))) == "break"
    assert canvas.scrolls == [(1, "units")]           # 向下 = yview_scroll(+1)
    # 指针在 Treeview 上 → 让控件自己滚，页面不许动
    assert handler(SimpleNamespace(num=5, widget=FakeWidget("Treeview"))) is None
    assert canvas.scrolls == [(1, "units")]


def test_bind_page_wheel_mousewheel_path_for_windows_and_macos():
    for ws, delta in (("win32", -120), ("aqua", -1)):
        root, canvas = FakeRoot(ws), FakeCanvas()
        platform_ui.bind_page_wheel(root, canvas, windowingsystem=ws)
        handler = root.bound["<MouseWheel>"][0]
        assert handler(SimpleNamespace(num="??", delta=delta,
                                       widget=FakeWidget("Frame"))) == "break"
        assert canvas.scrolls == [(1, "units")], ws


def test_bind_page_wheel_survives_destroyed_canvas():
    root, canvas = FakeRoot("x11"), FakeCanvas()
    canvas.dead = True
    platform_ui.bind_page_wheel(root, canvas, windowingsystem="x11")
    handler = root.bound["<Button-4>"][0]
    assert handler(SimpleNamespace(num=4, widget=FakeWidget("Frame"))) is None


def test_is_self_scrolling():
    assert platform_ui.is_self_scrolling(FakeWidget("Treeview")) is True
    assert platform_ui.is_self_scrolling(FakeWidget("Text")) is True
    assert platform_ui.is_self_scrolling(FakeWidget("TCombobox")) is True
    assert platform_ui.is_self_scrolling(FakeWidget("Frame")) is False
    assert platform_ui.is_self_scrolling(None) is False
    assert platform_ui.is_self_scrolling(FakeWidget("KitScroll"), skip=("KitScroll",)) is True
    assert platform_ui.is_self_scrolling(Exploding()) is False   # 取不到类名不要炸


# ─────────────────────────────────────────────────────────── DPI / 缩放

class FakeFunction:
    """记录调用并返回固定值；可配置成抛异常（模拟老系统没有这个函数）。"""

    def __init__(self, result: object = 1, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


class FakeLibrary:
    def __init__(self, **functions: object) -> None:
        self._functions = functions

    def __getattr__(self, name: str) -> object:
        return self._functions[name]              # KeyError = 该 API 不存在


class FakeApi:
    """假的 ``ctypes.windll``：只有被显式给出的 API 才存在。"""

    def __init__(self, **libraries: FakeLibrary) -> None:
        self._libraries = libraries

    def __getattr__(self, name: str) -> FakeLibrary:
        return self._libraries[name]


@pytest.fixture
def dpi_state(monkeypatch):
    """每个用例都从「未设置」开始（模块级 flag 会被跨用例污染）。"""
    monkeypatch.setattr(platform_ui, "_DPI_ENABLED", False)
    monkeypatch.setattr(platform_ui, "_DPI_MODE", None)
    return platform_ui


def test_enable_high_dpi_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: FakeApi())   # 不该被调用
    assert platform_ui.enable_high_dpi() is False


def test_enable_high_dpi_prefers_per_monitor_v2(dpi_state, monkeypatch):
    setter = FakeFunction(1)
    api = FakeApi(user32=FakeLibrary(SetProcessDpiAwarenessContext=setter,
                                     SetProcessDPIAware=FakeFunction(1)),
                  shcore=FakeLibrary(SetProcessDpiAwareness=FakeFunction(0)))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    monkeypatch.setattr(platform_ui, "_windows_dpi_awareness", lambda _api: 0)
    assert platform_ui.enable_high_dpi() is True
    assert platform_ui.dpi_mode() == "per-monitor-v2"
    assert setter.calls == [(-4,)]           # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


def test_enable_high_dpi_falls_back_to_per_monitor_then_system(dpi_state, monkeypatch):
    # V2 不存在（Win8.1/老 10）→ shcore.SetProcessDpiAwareness(2) 成功
    api = FakeApi(user32=FakeLibrary(SetProcessDPIAware=FakeFunction(1)),
                  shcore=FakeLibrary(
                      GetProcessDpiAwareness=FakeFunction(0),
                      SetProcessDpiAwareness=FakeFunction(0)))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    monkeypatch.setattr(platform_ui, "_windows_dpi_awareness", lambda _api: None)
    assert platform_ui.enable_high_dpi() is True
    assert platform_ui.dpi_mode() == "per-monitor"

    # 再降一级：连 shcore 都没有 → user32.SetProcessDPIAware()
    platform_ui._DPI_ENABLED = False
    platform_ui._DPI_MODE = None
    api2 = FakeApi(user32=FakeLibrary(SetProcessDPIAware=FakeFunction(1)))
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api2)
    assert platform_ui.enable_high_dpi() is True
    assert platform_ui.dpi_mode() == "system"


def test_enable_high_dpi_reports_failure_when_everything_fails(dpi_state, monkeypatch):
    api = FakeApi(user32=FakeLibrary(SetProcessDPIAware=FakeFunction(0)))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    monkeypatch.setattr(platform_ui, "_windows_dpi_awareness", lambda _api: None)
    assert platform_ui.enable_high_dpi() is False
    assert platform_ui.dpi_mode() is None


def test_enable_high_dpi_is_idempotent(dpi_state, monkeypatch):
    setter = FakeFunction(1)
    api = FakeApi(user32=FakeLibrary(SetProcessDpiAwarenessContext=setter))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    monkeypatch.setattr(platform_ui, "_windows_dpi_awareness", lambda _api: 0)
    assert platform_ui.enable_high_dpi() is True
    assert platform_ui.enable_high_dpi() is True
    assert len(setter.calls) == 1            # 第二次不再碰 Win32


def test_enable_high_dpi_recognises_already_aware(dpi_state, monkeypatch):
    setter = FakeFunction(1)
    api = FakeApi(user32=FakeLibrary(SetProcessDpiAwarenessContext=setter))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    monkeypatch.setattr(platform_ui, "_windows_dpi_awareness", lambda _api: 2)
    assert platform_ui.enable_high_dpi() is True
    assert platform_ui.dpi_mode() == "already-aware"
    assert setter.calls == []


def test_system_scale_env_override_wins(monkeypatch):
    monkeypatch.setenv("DJB_UI_SCALE", "1.5")
    assert platform_ui.system_scale() == 1.5


def test_system_scale_windows_uses_GetDpiForSystem(dpi_state, monkeypatch):
    api = FakeApi(user32=FakeLibrary(GetDpiForSystem=FakeFunction(120)),
                  gdi32=FakeLibrary(GetDeviceCaps=FakeFunction(96)))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "IS_MACOS", False)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    assert platform_ui.system_scale() == 1.25


def test_system_scale_windows_falls_back_to_device_caps(dpi_state, monkeypatch):
    api = FakeApi(user32=FakeLibrary(GetDpiForSystem=FakeFunction(0, RuntimeError("nope")),
                                     GetDC=FakeFunction(1234), ReleaseDC=FakeFunction(1)),
                  gdi32=FakeLibrary(GetDeviceCaps=FakeFunction(144)))
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_ui, "IS_MACOS", False)
    monkeypatch.setattr(platform_ui, "_win_api", lambda: api)
    assert platform_ui.system_scale() == 1.5


def test_system_scale_linux_reads_xft_dpi(monkeypatch):
    monkeypatch.setattr(platform_ui, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_ui, "IS_MACOS", False)
    for name in ("DJB_UI_SCALE", "XFT_DPI", "QT_SCALE_FACTOR", "GDK_SCALE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(platform_ui, "_xrdb_dpi", lambda: 120.0)
    assert platform_ui.system_scale() == 1.25
    monkeypatch.setenv("XFT_DPI", "144")
    assert platform_ui.system_scale() == 1.5
    monkeypatch.delenv("XFT_DPI")
    monkeypatch.setattr(platform_ui, "_xrdb_dpi", lambda: None)
    assert platform_ui.system_scale() == 1.0


def test_ui_scale_reads_pixels_per_inch():
    class Root:
        def __init__(self, ppi: float, ws: str = "x11") -> None:
            self.ppi = ppi
            self.tk = FakeTcl(ws)

        def winfo_fpixels(self, _what: str) -> float:
            return self.ppi

    assert platform_ui.ui_scale(Root(96)) == 1.0
    assert platform_ui.ui_scale(Root(120)) == 1.25
    assert platform_ui.ui_scale(Root(144)) == 1.5
    # macOS 例外：aqua 走逻辑点，不做 Windows 式缩放（未验证项，见报告）
    assert platform_ui.ui_scale(Root(72, "aqua")) == 1.0
    # 取不到就回落到 system_scale，不许抛
    assert platform_ui.ui_scale(object()) == platform_ui.system_scale()


def test_scale_value_rounds():
    assert platform_ui.scale_value(1180, 1.25) == 1475
    assert platform_ui.scale_value(13, 1.25) == 16
    assert platform_ui.scale_value(13, 1.0) == 13


# ─────────────────────────────────────────────────────────── 字号

def test_font_size_arg_keeps_pixels_on_windows_and_linux(monkeypatch):
    monkeypatch.setattr(platform_ui, "IS_MACOS", False)
    assert platform_ui.font_size_arg(13) == -13
    assert platform_ui.font_size_arg(30) == -30
    assert platform_ui.font_size_arg(0) == 0


def test_font_size_arg_returns_points_on_macos(monkeypatch):
    """13px（Windows/96dpi）≈ 10pt（Cocoa/72dpi）—— 与 Windows 同物理尺寸。"""
    monkeypatch.setattr(platform_ui, "IS_MACOS", True)
    assert platform_ui.font_size_arg(13) == 10
    assert platform_ui.font_size_arg(19) == 14
    assert platform_ui.font_size_arg(30) == 23
    assert platform_ui.font_size_arg(10) == 8
    assert platform_ui.font_size_arg(1) == 1          # 不返回 0（Tk 里 0 = 平台默认）
    assert platform_ui.font_size_arg(13, dpi=72) == 13
    # 125% 下 13px 设计稿 = 16.25 设备像素 ≈ 12.19pt（四舍五入 12）
    assert platform_ui.font_size_arg(13, scale=1.25) == 12


# ─────────────────────────────────────────────────────────── 几何

def test_fit_geometry_shrinks_height_on_1366x728_laptop():
    """1366×768 笔记本（扣掉任务栏 728）：780 高的窗口会出界，必须缩到 664。"""
    assert platform_ui.fit_geometry(1180, 780, 1366, 728) == (1180, 664)


def test_fit_geometry_leaves_big_screens_alone():
    assert platform_ui.fit_geometry(1180, 780, 3440, 1440) == (1180, 780)


def test_fit_geometry_shrinks_both_axes_on_small_screens():
    assert platform_ui.fit_geometry(1180, 780, 1024, 600) == (960, 536)


def test_fit_geometry_edge_cases():
    # 屏幕尺寸不可信（无头环境 winfo 返回 1）→ 原样返回，不在启动路径上猜
    assert platform_ui.fit_geometry(1180, 780, 1, 1) == (1180, 780)
    assert platform_ui.fit_geometry(1180, 780, 0, 0) == (1180, 780)
    # 边距可调：0 边距 = 正好贴屏
    assert platform_ui.fit_geometry(1180, 780, 1366, 728, margin=0) == (1180, 728)


def test_fit_minsize_never_exceeds_screen():
    """minsize 不同步收缩的话，窗口管理器会把窗口顶回去 —— 修一半等于没修。"""
    assert platform_ui.fit_minsize(900, 620, 1366, 728) == (900, 620)
    assert platform_ui.fit_minsize(900, 620, 1024, 600) == (900, 536)


def test_initial_geometry_reads_the_screen():
    root = FakeRoot()
    root.screen = (1366, 728)
    assert platform_ui.initial_geometry(root, 1180, 780) == "1180x664"
    root.screen = (3440, 1440)
    assert platform_ui.initial_geometry(root, 1180, 780) == "1180x780"


# ─────────────────────────────────────────────────────────── 快捷键

def test_primary_modifier():
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(platform_ui, "IS_MACOS", False)
        assert platform_ui.primary_modifier() == "Control"
        monkeypatch.setattr(platform_ui, "IS_MACOS", True)
        assert platform_ui.primary_modifier() == "Command"
    finally:
        monkeypatch.undo()


def test_modifier_bindings_macos_binds_command_and_control():
    assert platform_ui.modifier_bindings("Key-1", platform="macos") == (
        "<Command-Key-1>", "<Control-Key-1>")
    assert platform_ui.modifier_bindings("r", platform="macos") == (
        "<Command-r>", "<Control-r>")


def test_modifier_bindings_windows_linux_only_control():
    for platform in ("windows", "linux"):
        assert platform_ui.modifier_bindings("Key-3", platform=platform) == ("<Control-Key-3>",)


# ─────────────────────────────────────────────────────────── macOS 菜单

def test_install_macos_menu_is_a_noop_off_macos():
    """非 macOS：一个属性都不许碰（Exploding 会立刻炸）。"""
    assert platform_ui.install_macos_menu(Exploding()) is False
    assert platform_ui.install_macos_menu(None) is False
    assert platform_ui.install_macos_menu(Exploding(), {"quit": lambda: None},
                                          assume_macos=False) is False


# ─────────────────────────────────────────────────────────── 真窗口（需要 DISPLAY）

def _tk_root_or_skip(what: str):
    """开一个真 Tk 根；没有图形会话就登记 ENV-MISSING 再跳过（严格模式失败）。"""
    if not platform_ui.display_available():
        detail = "DISPLAY / WAYLAND_DISPLAY 均未设置（Xvfb 下用 DISPLAY=:93 复跑）"
        env_missing.record(what + "：未验证（" + detail + "）")
        warnings.warn(env_missing.format_lines()[-1], UserWarning, stacklevel=2)
        pytest.skip(what + " 需要图形会话：" + detail, allow_module_level=False)
    try:
        import tkinter as tk
        return tk, tk.Tk()
    except Exception as exc:                     # 没有 tkinter / 连不上 display
        detail = type(exc).__name__ + ": " + str(exc)
        env_missing.record(what + "：未验证（" + detail + "）")
        warnings.warn(env_missing.format_lines()[-1], UserWarning, stacklevel=2)
        pytest.skip(what + " 需要可用的 Tk：" + detail, allow_module_level=False)


def test_real_tk_wheel_bindings_and_scrolling():
    """X11 上必须绑 Button-4/5，且真事件能把 Canvas 滚起来；Treeview 上不许抢。"""
    tk, root = _tk_root_or_skip("真窗口滚轮")
    try:
        root.title("platform-ui-test")
        canvas = tk.Canvas(root, width=300, height=200, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(scrollregion=(0, 0, 400, 2000))
        for index in range(20):
            canvas.create_rectangle(10, index * 90 + 5, 390, index * 90 + 80,
                                    fill="#334455", outline="")
        from tkinter import ttk
        tree = ttk.Treeview(root, columns=("a",), show="headings", height=8)
        for index in range(60):
            tree.insert("", "end", values=(str(index),))
        tree.pack(side="right", fill="y")
        root.update()

        ws = platform_ui.windowingsystem(root)
        sequences = platform_ui.bind_page_wheel(root, canvas)
        assert sequences == platform_ui.wheel_sequences(root)

        if ws == "x11":
            # 设计如此：X11 上不绑 MouseWheel（真实滚轮不会产生它）
            assert root.bind_all("<MouseWheel>") == ""
            down = ("<Button-5>", -1)
            up = ("<Button-4>", 1)
        else:                                      # pragma: no cover - 非 Linux 宿主
            down = ("<MouseWheel>", -120)
            up = ("<MouseWheel>", 120)

        before = canvas.yview()[0]
        canvas.event_generate(down[0], x=10, y=10, **({} if ws == "x11" else {"delta": down[1]}))
        root.update()
        assert canvas.yview()[0] > before, "页面应当向下滚动"

        middle = canvas.yview()[0]
        canvas.event_generate(up[0], x=10, y=10, **({} if ws == "x11" else {"delta": up[1]}))
        root.update()
        assert canvas.yview()[0] < middle, "页面应当向上滚回"

        # 指针在表格上：表格自己滚，页面不许动（这就是「互相抢」的回归点）
        canvas_before, tree_before = canvas.yview()[0], tree.yview()[0]
        tree.event_generate(down[0], x=10, y=10, **({} if ws == "x11" else {"delta": down[1]}))
        root.update()
        assert tree.yview()[0] > tree_before, "Treeview 自己应当滚动（TtkScrollable 类绑定）"
        assert canvas.yview()[0] == canvas_before, "页面不许跟着表格一起滚"
    finally:
        root.destroy()


def test_real_tk_install_macos_menu_pipeline():
    """非 darwin 宿主上验证「菜单安装的 Tk 侧管线」：建菜单、注册 Tcl 钩子、幂等。

    注意：这只证明调用序列正确；**aqua 上的真实菜单渲染未验证**（无 Mac 宿主）。
    """
    tk, root = _tk_root_or_skip("macOS 菜单管线")
    try:
        events: list[str] = []
        first = platform_ui.install_macos_menu(root, {
            "app_name": "DJB 联脑",
            "about": lambda: events.append("about"),
            "quit": lambda: events.append("quit"),
            "preferences": lambda: events.append("prefs"),
        }, assume_macos=True)
        assert first is True
        menubar = str(root.cget("menu"))
        assert menubar, "必须真的挂上菜单栏"
        # 名字叫 apple 的子菜单 = Tk 的应用菜单（tkMacOSXMenu.c: SPECIALMENU(apple, APPLE)）
        assert int(root.tk.call("winfo", "exists", menubar + ".apple")) == 1
        # Tcl 钩子都注册上了（aqua 上 Tk 才会调用它们；这里直接调，验证转发）
        for proc in ("::tk::mac::Quit", "::tk::mac::ShowPreferences", "tkAboutDialog"):
            found = root.tk.call("info", "procs", proc)
            names = list(found) if isinstance(found, tuple) else [found]
            assert names == [proc], proc
        root.tk.call("::tk::mac::Quit")
        root.tk.call("tkAboutDialog")
        assert events == ["quit", "about"]
        # 幂等：第二次不重复建菜单
        again = platform_ui.install_macos_menu(root, {"quit": lambda: None},
                                               assume_macos=True)
        assert again is False
    finally:
        root.destroy()
