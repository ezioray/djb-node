# SPDX-License-Identifier: Apache-2.0
"""Windows 本地控制通道的开关助手（task-13）。

`client.ipc.windows.enabled`（`djb_node/facts.py` 的 `FEATURE_FLAGS`）自 2026-11-05
首发决策起 **默认开启**（决策人批准；批准记录见
`ops/release/LAUNCH-ENV-CONSTRAINTS-2026-11-05.md` §7）。三字段 owner / expire_at /
rollback_rule 仍在 ⇒ 它是一个真开关：关掉即回到「控制不可用 + 可行动提示」。

因此用例**不再注入开关**（注入只能证明代码能跑，不能证明用户能用）；
本模块只保留一个必需的反向能力：把开关关掉以验证降级路径（kill switch）。
非 Windows 平台一律 no-op —— 其它平台的本地控制走 unix socket，与此开关无关。
"""
from __future__ import annotations

import os

import pytest

from djb_node import ipc_windows

__all__ = ["FLAG", "disable", "enabled", "require_usable"]

FLAG = ipc_windows.FLAG


def enabled(paths) -> bool:
    """开关在本机配置里是否为真（唯一求值入口 `config.feature_flag`）。"""
    from djb_node.config import feature_flag, load_config

    return bool(feature_flag(load_config(paths), FLAG))


def disable(paths, *, save=None) -> bool:
    """把开关写盘关掉（kill switch），返回是否真的关掉了。非 Windows 返回 False。"""
    if os.name != "nt":
        return False
    from djb_node.config import load_config, save_config

    config = load_config(paths)
    extra = dict(getattr(config, "extra", {}) or {})
    flags = dict(extra.get("feature_flags") or {})
    flags[FLAG] = False
    extra["feature_flags"] = flags
    config.extra = extra
    (save or save_config)(paths, config)
    return not enabled(paths)


def _spec_problem() -> str:
    """返回开关「不可用」的具体原因（空字符串 = 可用）。"""
    from datetime import date, datetime, timezone

    from djb_node import facts

    spec = facts.FEATURE_FLAGS.get(FLAG)
    if not isinstance(spec, dict):
        return "开关未登记"
    if spec.get("default") is not True:
        return "default 与发布决策不一致（首发默认开启）"
    if not str(spec.get("owner") or "").strip():
        return "owner 为空"
    if not str(spec.get("rollback_rule") or "").strip():
        return "rollback_rule 为空"
    try:
        expires = date.fromisoformat(str(spec["expire_at"]))
    except (KeyError, ValueError):
        return "expire_at 缺失或非法"
    if datetime.now(timezone.utc).date() > expires:
        return "开关已到期（expire_at=" + str(spec["expire_at"]) + "）"
    return ""


def require_usable() -> None:
    """验证控制通道「本该可用」；不可用时**带可验证理由 skip**，不静默红、不静默绿。

    触发条件都是**确定事实**（未登记 / default 与发布决策不一致 / 到期 / 三字段缺失），
    不是运行时抖动。到期（expire_at=2026-12-31）或按 rollback_rule 回滚时，
    这些用例会变成"环境不允许验证控制通道"的 skip，理由直接指向开关语义用例：
    ``tests/test_windows_ipc.py::test_flag_default_expiry_and_metadata``。
    """
    if os.name != "nt":
        return                       # 其它平台的控制口是 unix socket，与本开关无关
    problem = _spec_problem()
    if problem:
        pytest.skip(
            "Windows 本地控制开关 %r 当前不可用（%s）：本用例无法验证真实控制通道；"
            "开关语义与到期边界见 "
            "tests/test_windows_ipc.py::test_flag_default_expiry_and_metadata" % (FLAG, problem)
        )
