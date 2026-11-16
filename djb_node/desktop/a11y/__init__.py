# SPDX-License-Identifier: Apache-2.0
"""读屏（MSAA / IAccessible）投影层——**平台无关的纯数据**。

分两块，互不牵连：

* :mod:`djb_node.desktop.a11y.tree`：:class:`~djb_node.desktop.views.View` → 节点树（纯函数，
  Linux 上可单测，不 import 任何 Windows 模块）。
* :mod:`djb_node.desktop.a11y.msaa`：把节点树挂到 Tk 窗口上（**只在 Windows 生效**，
  import 时才尝试 ``comtypes``；失败即降级为「没有 a11y」，界面照常）。

对外只暴露节点树这一层；渲染层（``app.py``）只调 :func:`build_tree` + provider 的
``attach`` / ``refresh`` / ``detach``，不碰 COM 细节。
"""
from __future__ import annotations

from typing import Any

from . import msaa as _msaa   # 静态 import：PyInstaller 只能看见这种（别改成 import_module）
from .tree import (
    NODE_STATES,
    ROLE_ACTION,
    ROLE_CARD,
    ROLE_CELL,
    ROLE_COLUMNHEADER,
    ROLE_FILTER,
    ROLE_PAGE,
    ROLE_ROW,
    ROLE_TABLE,
    ROLE_STATIC,
    ROLE_TEXT,
    ROLE_TOGGLE,
    STATE_READONLY,
    TABLE_MAX_ROWS,
    Node,
    build_tree,
    flatten,
)

__all__ = [
    "NODE_STATES",
    "ROLE_ACTION",
    "ROLE_CARD",
    "ROLE_CELL",
    "ROLE_COLUMNHEADER",
    "ROLE_FILTER",
    "ROLE_PAGE",
    "ROLE_ROW",
    "ROLE_TABLE",
    "ROLE_STATIC",
    "ROLE_TEXT",
    "ROLE_TOGGLE",
    "STATE_READONLY",
    "TABLE_MAX_ROWS",
    "Node",
    "build_tree",
    "flatten",
    "install",
]


def install(root: Any) -> "Any | None":
    """装读屏提供方（Windows 生效；其它平台/逃生阀/失败 → ``None``）。"""
    return _msaa.attach(root)
