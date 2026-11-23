# SPDX-License-Identifier: Apache-2.0
"""品牌资源（LOGO v3）：只用标准库读取，**读不到就返回 None**，绝不抛。

为什么单开一个包
================

* 打包（PyInstaller）要 ``--icon djb_node/assets/tokenbank.ico``，图标文件必须在包内、
  且路径可通过 ``importlib.resources`` 找到（不要写死 ``__file__`` 相对路径：冻结包里会变）。
* 客户端规矩是**永不因为图标启动失败**：任何一个文件缺失/损坏，调用方拿 ``None``，
  回落到原来那个「画出来的色块」。所以这里的函数只返回 ``bytes | None``。

资源口径（来自 ``reports/UX-E2E-20261123/design/logo-v3/``，**不重新缩放**）

======================  ======  ====================================
文件                     尺寸    用途
======================  ======  ====================================
``tokenbank-dark-16-16.png``   16  托盘/标题栏微标（单 T 简化版）
``tokenbank-dark-16-24.png``   24  托盘微标 24（高分屏）
``tokenbank-dark-simple-32.png`` 32  简短版 32
``tokenbank-dark-simple-48.png`` 48  简短版 48
``tokenbank-dark-full-256.png``  256 窗口图标（完整版）
``tokenbank.ico``        16–256   打包图标（Windows 任务栏/资源管理器）
======================  ======  ====================================
"""
from __future__ import annotations

from typing import Any

__all__ = ["ASSETS", "TRAY_ICON", "TRAY_ICON_2X", "WINDOW_ICON", "PACKAGE_ICON",
           "resource_bytes", "resource_path"]

#: 包内资源名（``importlib.resources`` 的相对路径）
ASSETS: dict[str, str] = {
    "tray": "tokenbank-dark-16-16.png",          # 16px 微标（单 T）
    "tray_2x": "tokenbank-dark-16-24.png",       # 24px 微标
    "small": "tokenbank-dark-simple-32.png",     # 32px 简短版
    "medium": "tokenbank-dark-simple-48.png",    # 48px 简短版
    "window": "tokenbank-dark-full-256.png",     # 窗口图标（完整版 256）
    "package": "tokenbank.ico",                  # 打包图标（.ico 多尺寸）
}
TRAY_ICON = ASSETS["tray"]
TRAY_ICON_2X = ASSETS["tray_2x"]
WINDOW_ICON = ASSETS["window"]
PACKAGE_ICON = ASSETS["package"]

_PACKAGE = __name__


def resource_path(name: str) -> Any:
    """资源的真实文件路径（冻结包里可能不存在 → 返回 ``None``）。

    注意：``importlib.resources`` 在 zip 导入下给的是 ``Traversable``，不保证有真实路径；
    拿不到就返回 ``None``，调用方改用 :func:`resource_bytes`。
    """
    try:
        from importlib import resources

        item = resources.files(_PACKAGE).joinpath(str(name))
        return str(item)
    except Exception:                        # 包未装、路径异常、zip 导入……
        return None


def resource_bytes(name: str) -> bytes | None:
    """资源内容（读不到/为空都返回 ``None``，**不抛**）。"""
    try:
        from importlib import resources

        data = resources.files(_PACKAGE).joinpath(str(name)).read_bytes()
    except Exception:
        return None
    return data or None
