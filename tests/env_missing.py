# SPDX-License-Identifier: Apache-2.0
"""「环境缺失」不许静默（task-15）。

背景（真实事故）：`tests/test_f16_contract_parity.py` 缺兄弟仓 `../djb-cloud` 时用
`pytest.mark.skipif` 跳过 6 条跨仓契约用例，结果同一 commit、同一台机器只因 stage 目录
布局不同，一次 `11 skipped`、一次 `17 skipped`。值班侧把 17 当基线，`+6` 被读成
"跳过集与基线一致" —— **delta 建在流沙上**。

判据：缺环境导致的"没验证"必须**可 grep、可拒绝当作基线**；不能让它在结果里以
普通 skip 的形式消失。所以：

1. 任何"环境缺失"都登记到本模块的注册表，由 `pytest_terminal_summary` 统一打
   `ENV-MISSING: …` 一行（黄字，脚本可 grep）；
2. 严格模式（`DJB_CROSS_REPO_STRICT=1`，发布门禁用）下**缺环境直接失败**，因为门禁
   的前提是"该有的都在"；
3. 两者都不是静默：默认非严格是为了不让 QA 分片（没有兄弟仓的 stage 副本）常年挂 6 条
   假红，但那 6 条一定会出现在 ENV-MISSING 里。
"""
from __future__ import annotations

import os

__all__ = ["STRICT_ENV", "record", "records", "format_lines", "strict_enabled",
           "reset"]

STRICT_ENV = "DJB_CROSS_REPO_STRICT"
_MARKER = "ENV-MISSING"

_records: list[str] = []


def strict_enabled() -> bool:
    """严格模式：环境缺失即失败（发布门禁/CI 用；QA 分片默认关闭）。"""
    return os.environ.get(STRICT_ENV, "").strip() in ("1", "true", "on", "yes")


def record(text: str) -> None:
    """登记一条「环境缺失导致未验证」。幂等（同一条只记一次）。"""
    item = str(text).strip()
    if item and item not in _records:
        _records.append(item)


def records() -> list[str]:
    return list(_records)


def reset() -> None:
    _records.clear()


def format_lines() -> list[str]:
    """给 `terminalreporter` 用的告警行（空列表 = 没有缺失）。"""
    return [_MARKER + ": " + item for item in _records]
