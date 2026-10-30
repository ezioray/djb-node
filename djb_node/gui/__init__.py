# SPDX-License-Identifier: Apache-2.0
"""S3 完整 GUI 的**共享偏好**最小面（仅本机、只发 intent）。

设计边界
========
* 模型层纯函数：预设 / 三档开关 / 组网独立开关 / 网络达标提示 / 收益预估文案；
  不读服务端、不算钱、不写 store。
* 窗口层只做渲染与收集，点击「保存并生效」把 :class:`ShareSettings` 交给注入的
  ``save_sink``；是否落盘、何时重启 daemon 由调用方决定。
* 无 tkinter 时 :func:`main` 返回 ``EXIT_HEADLESS``(3)，不抛 traceback。
"""
from .share import (PRESETS, ShareOption, ShareSettings, ShareView, build_share_view,
                    validate_settings)
from .window import EXIT_HEADLESS, HeadlessError, ShareWindow, main

__all__ = [
    "PRESETS", "ShareOption", "ShareSettings", "ShareView", "build_share_view",
    "validate_settings", "ShareWindow", "HeadlessError", "EXIT_HEADLESS", "main",
]
