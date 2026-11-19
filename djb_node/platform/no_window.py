# SPDX-License-Identifier: Apache-2.0
"""子进程「不许弹控制台窗口」的唯一封装（S1：Windows 客户端每次刷新闪黑框）。

为什么需要
==========

桌面端打包形态是 PyInstaller **windowed**（``djb-desktop.exe``，无控制台）。Windows 的规则是：

    一个**没有控制台**的进程去创建 **console 子系统**子程序时，系统会为子进程
    **新建一个控制台窗口**（黑框 / CMD 闪窗）。

客户端里 console 子程序不止一个：``powershell.exe``（机器画像 WMI）、``nvidia-smi``
（GPU 探测）、``rocm-smi``、``djb-node.exe``（CLI 数据层）、``import``（截图）、
``taskkill`` 等。任何一处漏传 ``creationflags`` 就是一个黑框——全仓曾有 0 处传过。

修复只有一处是对的
==================

``creationflags=CREATE_NO_WINDOW`` + ``startupinfo(STARTF_USESHOWWINDOW / SW_HIDE)``，
两个都给（只给一个在某些 Windows 版本 / 子程序类型上仍会闪）。把这对常量复制到十几个
调用点迟早漏一个，所以统一走 :func:`subprocess_window_kwargs`：

    subprocess.run(cmd, **subprocess_window_kwargs())

* Windows → ``{"creationflags": 0x08000000, "startupinfo": <SW_HIDE>}``
* 其它平台 → ``{}``（POSIX 没有这个概念；多传参数会 ``TypeError``）

边界
====

* 只返回 kwargs，**不执行**任何子进程；纯函数 + 每次新建 ``STARTUPINFO``（复用同一个对象
  给多个并发 ``CreateProcess`` 调用不需要，且容易被调用方改写）。
* 不改任何调用点的其它参数（stdout/encoding/timeout 语义一个不动）。
* 判定可注入（``WINDOWS`` / ``subprocess`` 都是模块级名字）：单测在 Linux 上也能验
  Windows 分支，不靠宿主 OS。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

__all__ = ["WINDOWS", "CREATE_NO_WINDOW", "SW_HIDE", "STARTF_USESHOWWINDOW",
           "subprocess_window_kwargs"]

#: 当前进程是否跑在 Windows 上（模块级名字：单测可 monkeypatch 验两条分支）
WINDOWS = os.name == "nt"

#: ``CreateProcess`` 的 ``CREATE_NO_WINDOW``（0x08000000）。
#: Python 3.7+ 在 Windows 上提供 ``subprocess.CREATE_NO_WINDOW``；这里给常量兜底，
#: 免得在非 Windows Python 上 ``getattr`` 拿到 None 后把 flag 传成 0（等于没传）。
CREATE_NO_WINDOW = 0x08000000
#: ``STARTUPINFO.dwFlags``：``wShowWindow`` 生效
STARTF_USESHOWWINDOW = 0x00000001
#: ``STARTUPINFO.wShowWindow``：隐藏
SW_HIDE = 0


def _startupinfo() -> Any:
    """建一个「隐藏窗口」的 ``STARTUPINFO``（只在 Windows 上有这个类型）。"""
    info = subprocess.STARTUPINFO()                      # type: ignore[attr-defined]
    info.dwFlags |= STARTF_USESHOWWINDOW
    info.wShowWindow = SW_HIDE
    return info


def subprocess_window_kwargs() -> dict[str, Any]:
    """给 ``subprocess.run`` / ``Popen`` / ``call`` 用的「不弹窗」kwargs。

    非 Windows 返回空 dict（调用方可以无条件 ``**`` 展开，POSIX 行为零变化）。
    每次都新建 dict / STARTUPINFO：调用方改了也不会污染下一个调用点。
    """
    if not WINDOWS:
        return {}
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW) or CREATE_NO_WINDOW)
    return {"creationflags": flags, "startupinfo": _startupinfo()}
