# SPDX-License-Identifier: Apache-2.0
"""C3 托盘层（纯数据 + 纯函数；**不含 GUI**）。

子模块
======

* :mod:`djb_node.tray.model` —— 菜单模型、状态投影、消息角标、action intent；
* :mod:`djb_node.tray.notifications` —— 消息中心 → 角标投影（只取计数与标题，不读正文）。

设计边界（C3 脚手架）
====================

* **不写 GUI**：本包不 import tkinter / PyQt / PySide / wx / gi，不创建窗口、不碰系统托盘 API；
  三端壳拿到 :class:`~djb_node.tray.model.TrayModel` 后自行渲染。
* **不执行 action**：暂停 / 恢复 / 退出等只产出 :class:`~djb_node.tray.model.TrayIntent`
  （动作 + 建议 CLI 命令 + 说明），是否执行、如何确认、失败怎么提示由壳决定。
* **纯投影**：只读传入的 status / notifications 映射；余额、今日收益原样展示服务端下发的值，
  本层不做计算；缺字段显示「—」。消息只取计数与标题，**不展开正文 / payload**。
* S2 只有六项菜单（状态 / 暂停恢复 / 消息中心 / 诊断 / 日志 / 退出）；S3 入口不在本层出现。
"""
from __future__ import annotations

from .model import (TrayAction, TrayIntent, TrayMenuItem, TrayModel)
from .notifications import from_store, summarize, title_of

__all__ = [
    "TrayAction",
    "TrayIntent",
    "TrayMenuItem",
    "TrayModel",
    "summarize",
    "from_store",
    "title_of",
]
