# SPDX-License-Identifier: Apache-2.0
"""客户端壳层特性开关（R2.4 S1/S3/S4）：**默认关闭 + 三字段齐全 + 到期强制关**。

唯一登记表
==========

开关元数据**只认** ``djb_node.facts.FEATURE_FLAGS``（lead 2026-11-24 批准扩写域后，
本模块原来那两份自建登记已并入 facts，见 :data:`facts.STARTUP_SKELETON_FLAG` /
:data:`facts.SNAPSHOT_CACHE_FLAG`）。本模块只做三件事：

1. 把名字收窄成常量（:data:`SKELETON_FLAG` / :data:`CACHE_FLAG`），桌面端不写字面量；
2. 求值统一走 :func:`djb_node.config.feature_flag`（环境变量 > 配置 > 登记表默认值）；
3. 到期强制关走 :func:`djb_node.config.flag_expired`（只认 facts 已登记的开关）。

一致性由 ``tests/test_feature_flags.py::test_desktop_flags_come_from_the_registry`` 钉住：
本模块读到的每个字段都必须与 facts 逐字相同，且未登记的名字一律 ``False``。

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
from typing import Any, Mapping

from .. import facts

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


SKELETON_FLAG = facts.STARTUP_SKELETON_FLAG
CACHE_FLAG = facts.SNAPSHOT_CACHE_FLAG


def _from_registry(name: str) -> ClientFlag | None:
    """从唯一登记表取一条；没登记/形状不对就 ``None``（调用方按关闭处理）。"""
    spec = facts.FEATURE_FLAGS.get(name)
    if not isinstance(spec, Mapping):
        return None
    try:
        return ClientFlag(name=name, default=spec.get("default"),
                          owner=str(spec.get("owner") or ""),
                          expire_at=str(spec.get("expire_at") or ""),
                          rollback_rule=str(spec.get("rollback_rule") or ""),
                          description=str(spec.get("description") or ""))
    except (TypeError, ValueError):        # 登记表被改坏 → 当作没登记（fail-closed）
        return None


CLIENT_FLAGS: dict[str, ClientFlag] = {
    flag.name: flag
    for flag in (_from_registry(SKELETON_FLAG), _from_registry(CACHE_FLAG))
    if flag is not None
}


def enabled(name: str, *, config: Any = None, today: date | None = None) -> bool:
    """开关求值：未登记的名字一律 ``False``（不认识的东西不许悄悄打开）。"""
    spec = CLIENT_FLAGS.get(str(name))
    if spec is None:
        return False
    from ..config import feature_flag, flag_expired

    try:
        if flag_expired(spec.name, today=today):      # 到期当日仍有效，次日起强制关
            return False
        return bool(feature_flag(config, spec.name, today=today))
    except Exception:                     # 配置坏了也按关闭处理（fail-closed）
        return False


def describe() -> list[dict[str, Any]]:
    """给取证/排障用的一览（不含任何敏感值）。"""
    return [{"name": item.name, "default": item.default, "owner": item.owner,
             "expire_at": item.expire_at, "rollback_rule": item.rollback_rule,
             "description": item.description}
            for item in CLIENT_FLAGS.values()]
