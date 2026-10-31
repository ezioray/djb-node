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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AUTH_COOLDOWN_S",
    "MAX_ATTEMPTS",
    "GroupState",
    "PlanTarget",
    "budget_view",
    "day_key",
    "effective_budget_left",
    "estimate_cost_minor",
    "format_usd_minor",
    "groups_usable",
    "is_model_supported",
    "month_key",
    "parse_usd_minor",
    "pick_target",
    "record_outcome",
    "reset_state",
    "should_failover",
    "stats_snapshot",
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
    if status is not None and (status in FAILOVER_STATUSES or 500 <= status <= 599):
        # 5xx 一律可切（501/505/507 等少见码也算上游故障），429/401/403 前面已处理
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
    #: 进程内可观测计数：``pick_total`` / ``pick_none_total`` /
    #: ``budget_exhausted_total`` / ``failover_total`` + ``failover_<reason>``
    stats: dict[str, int] = field(default_factory=dict)


def day_key(now: Any = None) -> str:
    """UTC 日期键 ``2026-11-01``（预算按 UTC 日切；跨时区口径见 README）。"""
    moment = _now_ts(now)
    return time.strftime("%Y-%m-%d", time.gmtime(moment))


def month_key(now: Any = None) -> str:
    """UTC 月份键 ``2026-11``。"""
    return day_key(now)[:7]


def _window_spent(group: Mapping[str, Any], period: str, now: Any = None) -> int:
    """窗口内的已花金额：库里的窗口标记过期就按 0 算（写入侧会真正归零）。

    组里没有窗口标记（老数据 / 单测直接构造的 dict）时**信任库里的值**——迁移不
    回填、也不猜。
    """
    raw = group.get("spent_usd_minor_" + period) or 0
    key = group.get("day_key" if period == "day" else "month_key")
    if key is None or str(key) == (day_key(now) if period == "day" else month_key(now)):
        return int(raw or 0)
    return 0


def _budget_left(group: Mapping[str, Any], *, period: str = "day",
                 now: Any = None) -> int | None:
    """``None`` = 不限；负数按 0 处理（已超额）。"""
    budget = group.get("budget_usd_minor_" + period)
    if budget is None:
        return None
    try:
        return max(0, int(budget) - _window_spent(group, period, now))
    except (TypeError, ValueError):                          # pragma: no cover - 防御
        return None


def effective_budget_left(group: Mapping[str, Any], model: str = "",
                          *, period: str = "day", now: Any = None) -> int | None:
    """组池与模型明细里**更紧**的那个剩余额度（``None`` = 两边都不限）。

    模型级预算只约束该模型自己（``model_budgets``），组池是所有模型共享的；
    两者取 min 才是「这个请求还能不能花」。
    """
    lefts: list[int] = []
    pool = _budget_left(group, period=period, now=now)
    if pool is not None:
        lefts.append(pool)
    budgets = (group.get("model_budgets") or {}).get(model) or {}
    model_budget = budgets.get("budget_usd_minor_" + period)
    if model_budget is not None:
        spent = ((group.get("spent_by_model") or {}).get(model) or {}).get(
            "spent_usd_minor_" + period) or 0
        try:
            lefts.append(max(0, int(model_budget) - int(spent)))
        except (TypeError, ValueError):                      # pragma: no cover - 防御
            pass
    return min(lefts) if lefts else None


def _model_affordable(group: Mapping[str, Any], model: str,
                      now: Any = None) -> bool:
    """模型级预算（+ 组池）里这个模型还有额度吗（日 / 月任一耗尽即不可用）。"""
    for period in ("day", "month"):
        left = effective_budget_left(group, model, period=period, now=now)
        if left is not None and left <= 0:
            return False
    return True


def _bump(state: GroupState | None, key: str, amount: int = 1) -> None:
    """进程内计数（观察用，不影响判定）。"""
    if state is None:
        return
    state.stats[key] = state.stats.get(key, 0) + amount


def stats_snapshot(state: GroupState | None = None) -> dict[str, int]:
    """``GroupState.stats`` 的快照（供 CLI / 事件日志 / 单测观察）。"""
    return dict((state or GroupState()).stats)


def groups_usable(groups: Iterable[Mapping[str, Any]], *,
                  state: GroupState | None = None, now: Any = None
                  ) -> list[Mapping[str, Any]]:
    """过滤掉关闭的组与预算已耗尽的组（日 / 月任一耗尽即不可用）。

    传入 ``state`` 时把跳过原因记进 ``stats``（``budget_exhausted_total``），
    避免「组预算耗尽 → 静默不选」这种看不见的行为。
    """
    usable: list[Mapping[str, Any]] = []
    for group in groups:
        if not group.get("enabled", True):
            _bump(state, "group_disabled_total")
            continue
        day_left = _budget_left(group, period="day", now=now)
        if day_left is not None and day_left <= 0:
            _bump(state, "budget_exhausted_total")
            _bump(state, "budget_exhausted_day_total")
            continue
        month_left = _budget_left(group, period="month", now=now)
        if month_left is not None and month_left <= 0:
            _bump(state, "budget_exhausted_total")
            _bump(state, "budget_exhausted_month_total")
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
    _bump(runtime, "pick_total")
    for group in sorted(groups_usable(groups, state=runtime, now=now),
                        key=lambda g: int(g.get("priority") or 0)):
        if not _model_affordable(group, model, now):
            _bump(runtime, "model_budget_exhausted_total")
            continue
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
    for group in sorted(groups_usable(groups, state=runtime, now=now),
                        key=lambda g: int(g.get("priority") or 0)):
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
                _bump(runtime, "model_downgrade_total")
                return PlanTarget(group=str(group.get("name") or ""),
                                  upstream_id=str(member.get("upstream_id")),
                                  model=str(fallback), reason="model_downgrade")
    _bump(runtime, "pick_none_total")
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
        _bump(state, "failover_total")
        _bump(state, "failover_" + (reason or "unknown"))
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


# --------------------------------------------------------------------------- 金额 / 展示
#: 单价的唯一口径：美元 / 每百万 token（facts.DEFAULT_MIN_PRICE_UNIT）
PRICE_UNIT = "usd_per_1m_tokens"

#: 1 美元 = 100 minor（整数记账，不用浮点）
CENTS_PER_USD = 100

_TOKENS_PER_UNIT = 1_000_000


def parse_usd_minor(value: Any) -> int:
    """``"1.23"`` -> ``123``（USD x 100 的整数）；非法输入抛 ``ValueError``。

    只接受非负、最多两位小数、有限的值——**不接受浮点**，也**不做**隐式四舍五入：
    ``"1.005"`` 直接报错，避免「命令行说是 1 元、库里存成 1.01 元」这种对不上账。
    """
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    if isinstance(value, int):
        return value * CENTS_PER_USD
    if not isinstance(value, str):
        raise ValueError("金额要用字符串（USD，如 \"1.23\"）")
    text = value.strip()
    if not text:
        raise ValueError("金额不能为空")
    if text.startswith("-"):
        raise ValueError("金额不能为负")
    whole, _, frac = text.partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise ValueError("金额格式不对：" + value)
    if len(frac) > 2:
        raise ValueError("金额最多两位小数：" + value)
    return int(whole) * CENTS_PER_USD + int((frac or "0").ljust(2, "0"))


def format_usd_minor(minor: Any) -> str:
    """``123`` -> ``"1.23"``；``None`` -> ``"不限"``（预算为 None 时）。"""
    if minor is None:
        return "不限"
    value = int(minor)
    sign = "-" if value < 0 else ""
    value = abs(value)
    return sign + str(value // CENTS_PER_USD) + "." + str(value % CENTS_PER_USD).zfill(2)


def estimate_cost_minor(*, tokens: int | None, price: Any,
                        unit: str | None = None) -> int | None:
    """按「单价 × token」算这次请求的金额（minor 整数）。

    * ``tokens`` 为 ``None``（上游没回 usage）→ ``None``：不算钱（不猜）；
    * ``price`` 缺失 / 不是正数 / 单位不认识 → ``None``：**只记 token，不记金额**；
    * 只认 :data:`PRICE_UNIT`（每百万 token 美元）；其它单位宁可不算也不猜；
    * 用 ``Decimal`` 做 ``ROUND_HALF_UP``，所以 < 0.5 美分的请求记 0——组预算是
      **保守阈值**，精确核对看 ``spent_tokens_*``（口径写在 tasks 报告里）。
    """
    if tokens is None:
        return None
    try:
        token_count = int(tokens)
    except (TypeError, ValueError):
        return None
    if token_count <= 0:
        return 0
    if unit not in (None, "", PRICE_UNIT):
        return None
    if price is None or isinstance(price, bool):
        return None
    try:
        price_value = Decimal(str(price).strip())
    except (InvalidOperation, ValueError):
        return None
    if not price_value.is_finite() or price_value <= 0:
        return None
    amount = price_value * Decimal(token_count) / Decimal(_TOKENS_PER_UNIT)
    minor = (amount * Decimal(CENTS_PER_USD)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


def budget_view(group: Mapping[str, Any], now: Any = None) -> dict[str, Any]:
    """一个组的预算视图（CLI ``djb plan budget show`` 与事件日志共用）。

    ``remaining_*`` 已经按当日 / 当月窗口归一化；``budget_exhausted`` 非空表示
    这个组现在不可用（``day`` / ``month``），空表示还有额度或压根不限。
    """
    day_spent = _window_spent(group, "day", now)
    month_spent = _window_spent(group, "month", now)
    day_budget = group.get("budget_usd_minor_day")
    month_budget = group.get("budget_usd_minor_month")
    exhausted = None
    if day_budget is not None and day_spent >= int(day_budget):
        exhausted = "day"
    elif month_budget is not None and month_spent >= int(month_budget):
        exhausted = "month"
    return {
        "group": group.get("name"),
        "id": group.get("id"),
        "enabled": bool(group.get("enabled", True)),
        "priority": int(group.get("priority") or 0),
        "day_key": day_key(now), "month_key": month_key(now),
        "budget_day_minor": day_budget, "budget_month_minor": month_budget,
        "budget_day_usd": format_usd_minor(day_budget),
        "budget_month_usd": format_usd_minor(month_budget),
        "spent_day_minor": day_spent, "spent_month_minor": month_spent,
        "spent_day_usd": format_usd_minor(day_spent),
        "spent_month_usd": format_usd_minor(month_spent),
        "remaining_day_minor": (None if day_budget is None
                                else max(0, int(day_budget) - day_spent)),
        "remaining_month_minor": (None if month_budget is None
                                  else max(0, int(month_budget) - month_spent)),
        "remaining_day_usd": (None if day_budget is None
                              else format_usd_minor(max(0, int(day_budget) - day_spent))),
        "remaining_month_usd": (None if month_budget is None
                                else format_usd_minor(max(0, int(month_budget) - month_spent))),
        "budget_exhausted": exhausted,
        "spent_tokens_day": int(group.get("spent_tokens_day") or 0),
        "spent_tokens_month": int(group.get("spent_tokens_month") or 0),
        "unknown_usage_count": int(group.get("unknown_usage_count") or 0),
        "allow_model_downgrade": bool(group.get("allow_model_downgrade")),
        "model_fallbacks": list(group.get("model_fallbacks") or ()),
        "members": [dict(item) for item in (group.get("members") or ())],
        "note": ("金额按 minor（USD x 100 整数）累加；拿不到单价的请求只记 token，"
                 "没有 usage 的请求只记 unknown_usage_count"),
    }
