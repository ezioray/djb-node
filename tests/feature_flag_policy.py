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
    "client.channel.enabled":
        "ROUND2-20261108 00-PLAN §2.1 P4（客户端出站通道默认开）：默认关闭时云端派单必然"
        " 150 s 超时 → 503 50901，而客户端 status 仍显示「接单=是」，两侧都不说人话。"
        "留痕 reports/E2E-20261107-FULL/TICKETS.md E2E-04 + RELEASE-PREP/PATCHES.md P4；"
        "显式 flags --disable 优先于服务端协商 ⇒ 仍是真开关",
    "client.probe.periodic.enabled":
        "ROUND3-20261109 DECISIONS.md §D-R3-01（Lead 签发）：默认关闭 ⇒ 探针 15 min 过期即掉容量，"
        "供方只能人工 djb upstream probe 续命（Windows 0.2.4 真机 status 原文见"
        " reports/ROUND3-20261109/client/win-probe.json）。三字段：owner=client-seat /"
        " expire_at=2026-12-15T00:00:00+08:00 / rollback_rule=djb flags --disable 或"
        " DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=false 或 revert ⇒ 仍是真开关",
}
