"""真窗口用例的共用跳板（只在「必须开 Tk」的用例里用）。

三个坑，写成一处，别再各写一遍：

1. **skip 判据必须确定性**：看 ``DISPLAY``/``WAYLAND_DISPLAY``（或 Windows 有交互桌面），
   不要用 ``try: tk.Tk() except: skip``——那样环境抖一下就会把真问题吞成 skip。
2. **建根要能重试**：``TclError: Can't find a usable init.tcl`` 这类瞬时失败偶发，
   重试 3 次仍失败就 **fail**（不是 skip）。
3. **读字号要读配置值**：``font actual -size`` 会把像素字号折成点再返回（-13px → 9pt），
   必须走 ``font configure -size``（具名字体）或 ``Font.cget("size")``，否则 10/13/18/30
   会被读小 1px，看起来像「字阶没落地」。
"""
from __future__ import annotations

import os
import time

import pytest


def desktop_available() -> tuple[bool, str]:
    """本机到底有没有图形桌面（确定性判据）。"""
    if os.name == "nt":
        try:
            import ctypes

            if ctypes.windll.user32.GetSystemMetrics(0) > 0 and \
                    ctypes.windll.user32.GetSystemMetrics(1) > 0:
                return True, ""
        except Exception:                          # pragma: no cover - 极端环境
            pass
        return False, "无交互桌面（Windows session 0 / 无显示器）"
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True, ""
    return False, "DISPLAY/WAYLAND_DISPLAY 均未设置（无 X/Wayland 会话）"


_TK_OK, _TK_REASON = desktop_available()
needs_tk = pytest.mark.skipif(not _TK_OK, reason="真窗口用例需要可用图形桌面：" + _TK_REASON)


def new_root():
    """建一个 Tk 根；瞬时初始化失败重试，真失败就 fail（不吞成 skip）。"""
    import tkinter

    last = None
    for _ in range(3):
        try:
            return tkinter.Tk()
        except Exception as exc:                   # pragma: no cover - 瞬时失败
            last = exc
            time.sleep(0.2)
    pytest.fail("Tk 初始化连续 3 次失败（不是 skip，是真问题）：%r" % (last,))


def configured_px(root, spec) -> int:
    """字体**配置值**的像素数（负数=像素的设计值）。

    三种入参分开处理，别让 ``font actual`` 那条有损路径混进来：
    * ``tkfont.Font`` 对象 → 直接读它自己的配置值（最常用：``fonts.sans(px)`` 的返回值）；
    * 具名字符串（``"DJB.Body"`` / ``"font3"``）→ ``exists=True`` 指着它读，**不重建**；
    * 元组等字体描述（``gui.theme`` 写的 ``(family, 11)``）→ 只能拿点值，正数。
    """
    import tkinter.font as tkfont

    if isinstance(spec, tkfont.Font):
        return int(spec.cget("size"))
    if isinstance(spec, str) and spec in tuple(root.tk.call("font", "names")):
        return int(tkfont.Font(root=root, name=spec, exists=True).cget("size"))
    return int(tkfont.Font(root=root, font=spec).cget("size"))


def path_bounds(widget, item) -> tuple[float, float, float, float]:
    """圆角矩形图元的**路径**包围盒 ``(x0, y0, x1, y1)``。

    不用 ``bbox()``：``smooth=True`` 的图元，Tk 的 bbox 是按控制点凸包再外扩算的
    （实测 1.5px 内缩的形状 bbox 会报到 -1 / w+1），没法用来判断描边有没有贴边。
    路径坐标才是可控量：描边以路径为中心各铺 ``width/2``，所以「路径离画布边 ≥1.5」
    等价于「1px 描边完整落在画布内」。
    """
    coords = widget.coords(item)
    if not coords:                                 # pragma: no cover - 空图元
        raise AssertionError("Canvas 图元 %r 没有坐标" % (item,))
    xs, ys = coords[0::2], coords[1::2]
    return (min(xs), min(ys), max(xs), max(ys))
