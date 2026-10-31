# SPDX-License-Identifier: Apache-2.0
"""桌面客户端（WP6）：12 视图 + 浅色主题 + 可注入数据层。

分层
====

=====================  ==========================================================
模块                    职责
=====================  ==========================================================
``datasource``          数据域协议 + 真实现（CLI 只读 / store / 本机事实 / GPU /
                        日志 / 云端只读）+ 假实现（测试注入）；失败一律「暂不可用」
``views``               12 个视图的纯投影（无 Tk），可做文本快照断言
``controller``          视图状态机 + 动作路由 + 草稿编辑 + 保存回读（无 Tk）
``access``              接入配置模型 / 掩码 / 原子落盘 / 「保存 → reload → 回读一致」
``app``                 Tk 主窗口（侧栏 12 项 + 内容区 + 状态栏）；无图形环境返回 3
=====================  ==========================================================

入口：``python -m djb_node.desktop``（``--demo`` 用确定性演示数据，``--view`` 指定首屏）。
"""
from .access import (ACCESS_FILE, AccessConfig, AccessService, FileAccessStore,
                     MemoryAccessStore, PRICE_MODES, RELOAD_DAEMON_DOWN, RELOAD_FAILED,
                     RELOAD_OK, RELOAD_UNSUPPORTED, SaveResult, mask_address, mask_secret)
from .controller import ActionResult, ControlOutcome, DesktopController, LocalControlChannel
from .datasource import (DataSource, FakeDataSource, LocalDataSource, NullDataSource,
                         ProviderError, RingLog, SECTIONS, Snapshot, UNAVAILABLE, Unavailable)
from .views import VIEW_KEYS, VIEWS, VIEW_TITLES, View, build_view, build_views

__all__ = [
    "ACCESS_FILE", "ActionResult", "AccessConfig", "AccessService", "ControlOutcome",
    "DATA_SECTIONS", "DataSource", "DesktopController", "FakeDataSource", "FileAccessStore",
    "LocalControlChannel", "LocalDataSource", "MemoryAccessStore", "NullDataSource",
    "PRICE_MODES", "ProviderError", "RELOAD_DAEMON_DOWN", "RELOAD_FAILED", "RELOAD_OK",
    "RELOAD_UNSUPPORTED", "RingLog", "SECTIONS", "SaveResult", "Snapshot", "UNAVAILABLE",
    "UNAVAILABLE_ALIAS", "VIEWS", "VIEW_KEYS", "VIEW_TITLES", "View", "Unavailable",
    "build_view", "build_views", "mask_address", "mask_secret",
]

#: 别名（文档里用 DATA_SECTIONS，代码里用 SECTIONS）
DATA_SECTIONS = SECTIONS
#: 别名（旧代码可能写 UNAVAILABLE_ALIAS）
UNAVAILABLE_ALIAS = UNAVAILABLE
