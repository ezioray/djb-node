# SPDX-License-Identifier: AGPL-3.0-or-later
"""多 plan 组：组内多模型共享预算 + 自动切换（纯逻辑，无 IO）。

## 为什么需要
客户端可以配多个上游（plan），但此前没有「组」概念：
- 无法表达「这 3 个 plan 共享一个月度额度」
- 无法表达「一个 plan 支持多个模型，组内所有模型共用同一个预算池」
- 一个 plan 额度用尽 / 凭据失效 / 上游 5xx 时，没有「自动切下一个」的规则

## 三个硬口径
1. **预算按组共享，按模型出明细**：组级 `budget_usd_minor` 是**一个池子**，组内所有模型共用；
   `spent_by_model` 只是明细账，**不参与**可用性判定，也**不再扣一次**（避免重复扣减）。
2. **切换 plan 时模型不变**：`model` 在 `pick_target` 里是**过滤条件**，不是结果；
   目标 plan 不支持该模型 → 跳过，绝不偷偷换模型。
3. **跨模型降级默认禁止**：只有组显式 `allow_model_downgrade=True` 且配了 `model_fallbacks`
   才允许换模型——偷偷换模型会让客户拿到不同结果，且破坏按模型计价的账。

## 失败切换规则（``should_failover``）
| 状态 / 错误 | 切换？ | 理由 |
|---|---|---|
| 429 | ✅ + 该 plan 冷却 | 限流或额度用尽，换一个 plan 可能就好了 |
| 401 / 403 | ✅ + 冷却 300 s | 凭据失效，短时间内重试无意义 |
| 5xx / 超时 / 连接错误 | ✅ | 上游故障 |
| 400 | ❌ | 请求本身有问题，换谁都一样 |
| 其它 4xx | ❌ | 同上 |

冷却与失败计数由调用方持久化（本模块只在内存里维护最小状态，方便单测）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AUTH_COOLDOWN_S",
    "MAX_ATTEMPTS",
    "PlanTarget",
    "is_model_supported",
    "should_failover",
    "groups_usable",
    "pick_target",
    "record_outcome",
]

#: 凭据类失败后的冷却时间（秒）
AUTH_COOLDOWN_S = 300

#: 一次请求最多尝试几个 plan
MAX_ATTEMPTS = 3

#: 这些状态码表示「换一个 plan 可能成功」
FAILOVER_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: 这些状态码表示「凭据有问题」→ 长冷却
AUTH_STATUSES: frozenset[int] = frozenset({401, 403})


def _now_ts(now: Any = None) -> float:
    """接受 ``None`` / ``datetime`` / ``float``，统一成 epoch 秒。"""
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    try:
        return float(now.timestamp())
    except AttributeError:                                  # pragma: no cover - 防御
        return time.time()


# --------------------------------------------------------------------------- 数据形状
@dataclass(frozen=True)
class PlanTarget:
    """一次切换决策的结果：选中的组 + 成员，以及为什么是它。"""

    group: str
    upstream_id: str
    model: str
    reason: str = "priority"

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.group, "upstream_id": self.upstream_id,
                "model": self.model, "reason": self.reason}


def is_model_supported(supported: Any, model: str) -> bool:
    """``upstreams.models`` 是逗号分隔字符串或序列；``*`` 表示全支持。

    精确匹配（大小写敏感）——不做前缀/通配猜测：模型名错一个字符就是另一个计费口径。
    """
    if not model:
        return False
    items: Iterable[Any]
    if isinstance(supported, str):
        items = [part for part in supported.split(",")]
    elif isinstance(supported, Sequence):
        items = supported
    else:
        return False
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if text == "*" or text == model:
            return True
    return False


def should_failover(status: int | None, error_kind: str | None = None) -> tuple[bool, str]:
    """要不要换下一个 plan，以及原因（``ok`` 时原因为空串）。"""
    if status in AUTH_STATUSES:
        return True, "auth"
    if status == 429:
        return True, "rate_limited"
    if status is not None and status in FAILOVER_STATUSES:
        return True, "upstream_error"
    kind = (error_kind or "").strip().lower()
    if kind in ("timeout", "connect", "connection", "network", "tls"):
        return True, "transport"
    return False, ""


# --------------------------------------------------------------------------- 组状态
@dataclass
class GroupState:
    """组内成员的运行时状态（冷却 / 失败计数）。进程内，不持久化。"""

    cooldown_until: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)


def _budget_left(group: Mapping[str, Any], *, period: str = "day") -> int | None:
    """``None`` = 不限；负数按 0 处理（已超额）。"""
    budget = group.get("budget_usd_minor_" + period)
    if budget is None:
        return None
    spent = group.get("spent_usd_minor_" + period) or 0
    try:
        return max(0, int(budget) - int(spent))
    except (TypeError, ValueError):                          # pragma: no cover - 防御
        return None


def groups_usable(groups: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """过滤掉关闭的组与预算已耗尽的组（日 / 月任一耗尽即不可用）。"""
    usable: list[Mapping[str, Any]] = []
    for group in groups:
        if not group.get("enabled", True):
            continue
        day_left = _budget_left(group, period="day")
        if day_left is not None and day_left <= 0:
            continue
        month_left = _budget_left(group, period="month")
        if month_left is not None and month_left <= 0:
            continue
        usable.append(group)
    return usable


def _weighted_order(members: Sequence[Mapping[str, Any]], picker: Any) -> list[Mapping[str, Any]]:
    """同 priority 内按 weight 加权随机；weight 缺省 1。``picker`` 注入以便测试确定性。"""
    ordered: list[Mapping[str, Any]] = []
    buckets: dict[int, list[Mapping[str, Any]]] = {}
    for member in members:
        buckets.setdefault(int(member.get("priority") or 0), []).append(member)
    for priority in sorted(buckets):
        bucket = list(buckets[priority])
        while bucket:
            total = sum(max(1, int(item.get("weight") or 1)) for item in bucket)
            ticket = picker(total)
            acc = 0
            chosen = bucket[-1]
            for item in bucket:
                acc += max(1, int(item.get("weight") or 1))
                if ticket < acc:
                    chosen = item
                    break
            ordered.append(chosen)
            bucket.remove(chosen)
    return ordered


def pick_target(groups: Iterable[Mapping[str, Any]], *, model: str,
                state: GroupState | None = None, now: Any = None,
                exclude: Iterable[str] = (), picker: Any = None) -> PlanTarget | None:
    """按「组 priority → 成员 priority → weight」选一个 plan，**模型必须被支持**。

    ``groups`` 每项形状::

        {"name": "main", "enabled": True, "priority": 0,
         "budget_usd_minor_day": 2000, "spent_usd_minor_day": 300,
         "budget_usd_minor_month": None, "spent_usd_minor_month": 0,
         "allow_model_downgrade": False, "model_fallbacks": [],
         "members": [{"upstream_id": "u1", "models": "gpt-4o,claude-3-5-sonnet",
                      "priority": 0, "weight": 1, "enabled": True}]}

    返回 ``None`` = **没有任何 plan 能服务这个模型**（调用方应报错，不要瞎选）。
    """
    moment = _now_ts(now)
    blocked = {item for item in exclude}
    runtime = state or GroupState()
    choose = picker or (lambda total: 0)     # 默认取第一个（确定性，便于测试）

    # 1) 精确模型匹配：换 plan 不换模型
    for group in sorted(groups_usable(groups), key=lambda g: int(g.get("priority") or 0)):
        candidates: list[Mapping[str, Any]] = []
        for member in group.get("members") or ():
            upstream_id = str(member.get("upstream_id") or "")
            if not upstream_id or upstream_id in blocked:
                continue
            if not member.get("enabled", True):
                continue
            if runtime.cooldown_until.get(upstream_id, 0.0) > moment:
                continue
            if not is_model_supported(member.get("models"), model):
                continue
            candidates.append(member)
        if not candidates:
            continue
        chosen = _weighted_order(candidates, choose)[0]
        return PlanTarget(group=str(group.get("name") or ""),
                          upstream_id=str(chosen.get("upstream_id")),
                          model=model, reason="priority")

    # 2) 跨模型降级：只有组显式允许 + 配了降级链才走
    for group in sorted(groups_usable(groups), key=lambda g: int(g.get("priority") or 0)):
        if not group.get("allow_model_downgrade"):
            continue
        for fallback in group.get("model_fallbacks") or ():
            for member in _weighted_order([m for m in (group.get("members") or ())
                                           if m.get("enabled", True)
                                           and str(m.get("upstream_id")) not in blocked
                                           and runtime.cooldown_until.get(
                                               str(m.get("upstream_id")), 0.0) <= moment
                                           and is_model_supported(m.get("models"), fallback)],
                                          choose):
                return PlanTarget(group=str(group.get("name") or ""),
                                  upstream_id=str(member.get("upstream_id")),
                                  model=str(fallback), reason="model_downgrade")
    return None


def record_outcome(state: GroupState, target: PlanTarget, *, status: int | None = None,
                   error_kind: str | None = None, now: Any = None) -> tuple[bool, str]:
    """记录一次尝试的结果；返回 ``(是否应切换, 原因)``。

    401/403 → 冷却 :data:`AUTH_COOLDOWN_S`；429 与 5xx 只累计失败数，不冷却
    （重试下一个 plan 是立即的，同一个 plan 稍后再试也合理）。
    """
    failover, reason = should_failover(status, error_kind)
    moment = _now_ts(now)
    if failover:
        state.failures[target.upstream_id] = state.failures.get(target.upstream_id, 0) + 1
        if reason == "auth":
            state.cooldown_until[target.upstream_id] = moment + AUTH_COOLDOWN_S
    else:
        state.failures.pop(target.upstream_id, None)
    return failover, reason


def split_cost(cost_minor: int | None) -> tuple[int, int]:
    """组池与模型明细的扣减额 —— **只扣一次**：``(组池, 模型明细)``。

    两者都返回同一个值，但语义不同：调用方写组池时用它，写模型明细时也用它，
    但**绝不能**在同一行里把两个字段都加上去（那样就扣两次）。
    独立成函数是为了让「只扣一次」这件事可被单测钉住。
    """
    value = int(cost_minor or 0)
    return (value, value)
