# SPDX-License-Identifier: Apache-2.0
"""客户端壳层特性开关（R2.4 S1/S3/S4）：**默认关闭 + 三字段齐全 + 到期强制关**。

为什么不写进 ``facts.FEATURE_FLAGS``
====================================

``facts.py`` 是全队唯一的开关登记表，但它不在本任务的写域（改了要 lead 批）。
本模块只登记 **桌面壳层自己的** 两个开关，形状与 ``facts.FEATURE_FLAGS`` 逐字一致
（``default / owner / expire_at / rollback_rule / description``），求值走同一个入口
:func:`djb_node.config.feature_flag`（环境变量 > 配置 > 默认值），并在这里补上
``expire_at`` 强制关 —— 因为 ``config.flag_expired`` 只认 ``facts`` 里已登记的开关。

> 待 lead 决定：是否把这两条**照抄**进 ``facts.FEATURE_FLAGS``（推荐抄：那样
> ``tests/test_feature_flags.py`` 的唯一登记表口径也能覆盖到它们）。本模块的
> ``CLIENT_FLAGS`` 就是抄写用的源文本，键名/字段一字不差。

回滚
====

两个开关都遵守「置 false 即回旧行为」：

* ``client.startup_skeleton=false`` → 首屏不再画骨架，直接等第一次快照（0.5.10 行为）；
* ``client.snapshot_cache=false`` → 启动不读缓存、不写缓存，每次启动都实时取数。

环境变量闸门：``DJB_FLAG_CLIENT_STARTUP_SKELETON=false`` / ``DJB_FLAG_CLIENT_SNAPSHOT_CACHE=false``。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = ["ClientFlag", "CLIENT_FLAGS", "SKELETON_FLAG", "CACHE_FLAG",
           "enabled", "describe"]


@dataclass(frozen=True)
class ClientFlag:
    """一个客户端开关的登记项（三字段缺一不可，默认必须是 ``False``）。"""

    name: str
    default: bool
    owner: str
    expire_at: str
    rollback_rule: str
    description: str = ""

    def __post_init__(self) -> None:
        # 默认关闭是**硬约束**（不是约定）：登记错了要在 import/单测时就炸，而不是上线才发现。
        if self.default is not False:
            raise ValueError("开关默认必须关闭：" + self.name)
        if not (self.owner and self.expire_at and self.rollback_rule):
            raise ValueError("开关三字段不齐（owner/expire_at/rollback_rule）：" + self.name)


SKELETON_FLAG = "client.startup_skeleton"
CACHE_FLAG = "client.snapshot_cache"

CLIENT_FLAGS: dict[str, ClientFlag] = {
    SKELETON_FLAG: ClientFlag(
        name=SKELETON_FLAG, default=False, owner="ezio", expire_at="2026-12-31",
        rollback_rule="置 false 即回旧行为：首屏不画骨架，直接等第一次快照（0.5.10 行为）",
        description="启动先出壳+骨架（首屏 ≤1.5s），数据到了再填内容"),
    CACHE_FLAG: ClientFlag(
        name=CACHE_FLAG, default=False, owner="ezio", expire_at="2026-12-31",
        rollback_rule="置 false 即回旧行为：启动不读/不写快照缓存，每次实时取数",
        description="启动用上次快照缓存先画（零网络首屏），随后后台刷新替换"),
}


def _expired(spec: ClientFlag, *, today: date | None = None) -> bool:
    """``expire_at`` 当日仍有效，次日起强制关（与 ``config.flag_expired`` 同口径）。"""
    from datetime import datetime, timezone

    try:
        year, month, day = (int(part) for part in str(spec.expire_at).split("-"))
    except Exception:                     # 元数据坏了 = fail-closed
        return True
    ref = today if isinstance(today, date) and not isinstance(today, datetime) \
        else datetime.now(timezone.utc).date()
    return ref > date(year, month, day)


def enabled(name: str, *, config: Any = None, today: date | None = None) -> bool:
    """开关求值：未登记的名字一律 ``False``（不认识的东西不许悄悄打开）。"""
    spec = CLIENT_FLAGS.get(str(name))
    if spec is None:
        return False
    if _expired(spec, today=today):
        return False
    from ..config import feature_flag

    try:
        return bool(feature_flag(config, spec.name, today=today))
    except Exception:                     # 配置坏了也按关闭处理（fail-closed）
        return False


def describe() -> list[dict[str, Any]]:
    """给取证/排障用的一览（不含任何敏感值）。"""
    return [{"name": item.name, "default": item.default, "owner": item.owner,
             "expire_at": item.expire_at, "rollback_rule": item.rollback_rule,
             "description": item.description}
            for item in CLIENT_FLAGS.values()]
