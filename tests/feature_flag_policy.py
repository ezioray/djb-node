# SPDX-License-Identifier: Apache-2.0
"""默认开启的开关必须**逐条批准并留痕**（task-13）。

硬约束是「特性开关默认关闭」。例外只能逐条登记在这里，并写明批准依据（文件/小节），
否则一律按默认关闭校验 —— 想悄悄把某个开关改成默认开，会在
`tests/test_feature_flags.py` / `tests/test_cli_s2.py` / `tests/test_token_compare.py` 三处同时红。

登记进来的开关仍必须是**真开关**：`owner` / `expire_at` / `rollback_rule` 三字段一个都不能少
（少任何一个就回滚不回去，那不叫开关）。
"""
from __future__ import annotations

__all__ = ["DEFAULT_ON_APPROVED"]

DEFAULT_ON_APPROVED: dict[str, str] = {
    "client.ipc.windows.enabled":
        "决策人 2026-11-05 拍板：Windows 本地控制口首发开启。"
        "留痕 ops/release/LAUNCH-ENV-CONSTRAINTS-2026-11-05.md §7（task-13）",
}
