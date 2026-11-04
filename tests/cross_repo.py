# SPDX-License-Identifier: Apache-2.0
"""跨仓检查的前置：缺兄弟仓时**记录 + 跳过（严格模式失败）**，绝不静默（task-15）。"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from tests import env_missing

__all__ = ["require_file"]


def require_file(path: Path, *, what: str, reason: str = "",
                 module_level: bool = False) -> None:
    """`path` 不存在 ⇒ 登记 ENV-MISSING，然后跳过（严格模式直接失败）。

    跳过是"本环境没这个仓"，不是"检查通过"；所以一定要同时登记告警，
    让值班侧无法把它当成正常基线。
    """
    if Path(path).exists():
        return
    detail = reason or ("缺少 " + str(path))
    env_missing.record(what + "：未验证（" + detail + "）")
    # 两条通道同时留痕：注册表（终端摘要用）+ 警告（一定出现在 warnings summary，
    # 不依赖 conftest 钩子是否被调用；值班侧 grep "ENV-MISSING" 就能拦住）。
    warnings.warn(env_missing.format_lines()[-1], UserWarning, stacklevel=2)
    message = (
        "环境缺失，无法执行" + what + "：" + detail +
        "。这不是通过——缺环境导致的「未验证」会登记成 ENV-MISSING 行"
        "（严格模式 " + env_missing.STRICT_ENV + "=1 下直接失败）。"
    )
    if env_missing.strict_enabled():
        pytest.fail(message, pytrace=False)
    pytest.skip(message, allow_module_level=module_level)
