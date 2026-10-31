# SPDX-License-Identifier: Apache-2.0
"""多 plan 组路由：组内自动切换 + 组级共享预算。**纯逻辑**（无 IO、无凭据）。

唯一口径（``server.py`` 的转发路径与 ``cli.py`` 的 plan 命令都按这里执行）：

``should_failover(status, error_kind)``
    * ``429``（限流 / 单 plan 额度用尽）→ ``True``：换下一个 plan 才有意义
    * ``401`` / ``403``（凭据无效 / 无权限）→ ``True``：该上游标记 ``auth_failed``，
      冷却 ``AUTH_COOLDOWN_S``（300 s）内不再被选中
    * ``5xx`` / 超时 / 连接错误（``status is None`` 且 ``error_kind`` 在
      ``NETWORK_ERROR_KINDS`` 里）→ ``True``
    * ``408``（请求超时，可重试）→ ``True``
    * 其它 ``4xx``（**含 ``400``**：请求本身有问题）→ ``False``：换 plan 也没用
    * ``2xx`` / 未知状态 → ``False``

``pick_target(groups, *, now=None, exclude=(), available=None, rng=None, state=None)``
    1. 组按 ``(priority, name)`` 升序；``enabled`` 为假的组 / 成员跳过；
    2. 组预算耗尽（``spent >= budget``；日 / 月任一超限；``budget is None`` = 不限）→
       整组跳过，计 ``budget_exhausted``；
    3. 组内先取**最小成员 priority 档**，同档按 ``weight`` 加权随机；
    4. ``exclude``（本请求已试过的上游）与 ``auth_failed`` 冷却中的上游跳过；
    5. 全部不可用 → 返回 ``None``（**不**瞎选一个；让调用方决定报错）。

``record_outcome(target, *, ok, status, cost_minor)``
    记录一次尝试的结论：成功清失败计数；失败累加计数，``401/403`` 触发 300 s 冷却。
    **金额累加不在这里做**（组预算账本归 ``store.add_plan_spend``，避免路由逻辑碰 SQL）。

计量口径
--------
* **金额** 只在「有 usage 且拿得到单价」时记；``cost_minor`` 是美元的百分之一
  （整数，不用浮点）。拿不到单价 → ``cost_minor=None``，只记 token 数
  （见 ``estimate_cost_minor`` 与 ``store.add_plan_spend``）。
* **没有 usage** → 不记金额、不记 token，只把 ``unknown_usage_count`` +1（可观测）。
* 单次请求金额按 minor 四舍五入（``ROUND_HALF_UP``），因此 < 0.5 美分的请求会记 0：
  组预算是**保守阈值**而非精确结算，核对口径看 ``spent_tokens_*`` 与
  ``unknown_usage_count``（报告里写明）。

进程内状态（``PlanState``）
---------------------------
冷却表 / 失败计数 / ``stats`` 计数都在**进程内内存**：不改库、不跨进程共享、随进程重启
归零。``stats`` 键形如 ``failover_total{reason=auth_failed}``、``budget_exhausted_total``。
需要持久痕迹时看日志：server 每次切换都写一条 ``plan.failover`` 事件（不含凭据）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence

from . import facts
from .errors import UsageError
from .util import utcnow

MAX_ATTEMPTS = 3
"""一次请求最多尝试的 plan 数（任务单硬上限；server 侧不再另设）。"""

AUTH_COOLDOWN_S = 300
"""凭据类失败（401/403）后该上游的冷却秒数。"""

NETWORK_ERROR_KINDS = frozenset({"timeout", "unreachable", "connect", "connection",
                                 "connection_reset", "network", "dns", "tls"})
"""网络层失败的错误类别（``NetworkError.reason`` 的取值集合）。"""

RATE_LIMITED_STATUS = 429
AUTH_FAILED_STATUSES = frozenset({401, 403})
TIMEOUT_STATUS = 408
CENT = 100
"""USD → minor 的倍数（minor = 美分 = USD x 100）。"""

_1M = 1_000_000


# ─────────────────────────────────────────────────────────── 数据模型
@dataclass
class PlanMember:
    """组内成员：一个上游（plan）在某个组里的接入点。"""

    upstream_id: str
    priority: int = 100
    weight: int = 1
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"upstream_id": self.upstream_id, "priority": int(self.priority),
                "weight": int(self.weight), "enabled": bool(self.enabled)}


@dataclass
class PlanGroup:
    """一个 plan 组：一组上游 + 组级共享预算（日 / 月各一个上限，``None`` = 不限）。"""

    id: str
    name: str
    priority: int = 100
    enabled: bool = True
    budget_day_minor: int | None = None
    budget_month_minor: int | None = None
    spent_day_minor: int = 0
    spent_month_minor: int = 0
    day_key: str | None = None
    month_key: str | None = None
    # 附加列（纯增量，默认值）：拿不到单价时只记 token；没有 usage 时记计数
    spent_tokens_day: int = 0
    spent_tokens_month: int = 0
    unknown_usage_count: int = 0
    members: list[PlanMember] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "priority": int(self.priority),
            "enabled": bool(self.enabled),
            "budget_day_minor": self.budget_day_minor,
            "budget_month_minor": self.budget_month_minor,
            "spent_day_minor": int(self.spent_day_minor),
            "spent_month_minor": int(self.spent_month_minor),
            "day_key": self.day_key,
            "month_key": self.month_key,
            "spent_tokens_day": int(self.spent_tokens_day),
            "spent_tokens_month": int(self.spent_tokens_month),
            "unknown_usage_count": int(self.unknown_usage_count),
            "members": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class PlanTarget:
    """``pick_target`` 的返回值：这一次该用哪个组的哪个上游。"""

    group_id: str
    group_name: str
    upstream_id: str
    priority: int = 100
    weight: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"group_id": self.group_id, "group_name": self.group_name,
                "upstream_id": self.upstream_id, "priority": int(self.priority),
                "weight": int(self.weight)}


@dataclass
class PlanState:
    """进程内路由状态（冷却 / 失败计数 / stats）。测试可自建实例做隔离。"""

    auth_failed_until: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    last_failure: dict[str, dict[str, Any]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_DEFAULT_STATE = PlanState()


def default_state() -> PlanState:
    """进程级默认状态（server 侧使用）；测试用 ``reset_state()`` 清空。"""
    return _DEFAULT_STATE


def reset_state(state: PlanState | None = None) -> None:
    """清空冷却 / 计数 / stats（测试与手工排障用；不碰库）。"""
    target = state or _DEFAULT_STATE
    with target.lock:
        target.auth_failed_until.clear()
        target.failures.clear()
        target.last_failure.clear()
        target.stats.clear()


def stats_snapshot(state: PlanState | None = None) -> dict[str, int]:
    """统计快照：``failover_total{reason=...}`` / ``budget_exhausted_total`` / …"""
    target = state or _DEFAULT_STATE
    with target.lock:
        return dict(target.stats)


# ─────────────────────────────────────────────────────────── 时间窗口
def _moment(now: datetime | None) -> datetime:
    value = now or utcnow()
    if value.tzinfo is None:                        # 无时区按 UTC 解释（防御）
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def day_key(now: datetime | None = None) -> str:
    """预算日窗口键（UTC，``YYYY-MM-DD``）。"""
    return _moment(now).date().isoformat()


def month_key(now: datetime | None = None) -> str:
    """预算月窗口键（UTC，``YYYY-MM``）。"""
    return _moment(now).strftime("%Y-%m")


def effective_spend(group: PlanGroup, now: datetime | None = None) -> tuple[int, int]:
    """窗口过期即视为 0：返回 ``(day_minor, month_minor)``（只读，不改 group）。"""
    moment = _moment(now)
    today = day_key(moment)
    this_month = month_key(moment)
    day_spent = int(group.spent_day_minor)
    month_spent = int(group.spent_month_minor)
    if group.day_key != today:
        day_spent = 0
    if group.month_key != this_month:
        month_spent = 0
    return day_spent, month_spent


def budget_exhausted_reason(group: PlanGroup, now: datetime | None = None) -> str | None:
    """组预算是否已耗尽：返回 ``"day"`` / ``"month"`` / ``None``（未耗尽）。"""
    day_spent, month_spent = effective_spend(group, now)
    if group.budget_day_minor is not None and day_spent >= int(group.budget_day_minor):
        return "day"
    if group.budget_month_minor is not None and month_spent >= int(group.budget_month_minor):
        return "month"
    return None


def budget_view(group: PlanGroup, now: datetime | None = None) -> dict[str, Any]:
    """CLI / 报告用的预算视图（含窗口归一化后的已花金额与剩余额度）。"""
    day_spent, month_spent = effective_spend(group, now)
    exhausted = budget_exhausted_reason(group, now)
    view: dict[str, Any] = {
        "group": group.name,
        "id": group.id,
        "priority": int(group.priority),
        "enabled": bool(group.enabled),
        "day_key": day_key(now),
        "month_key": month_key(now),
        "budget_day_minor": group.budget_day_minor,
        "budget_month_minor": group.budget_month_minor,
        "spent_day_minor": day_spent,
        "spent_month_minor": month_spent,
        "remaining_day_minor": (None if group.budget_day_minor is None
                                else max(0, int(group.budget_day_minor) - day_spent)),
        "remaining_month_minor": (None if group.budget_month_minor is None
                                  else max(0, int(group.budget_month_minor) - month_spent)),
        "budget_exhausted": exhausted,
        "spent_tokens_day": int(group.spent_tokens_day),
        "spent_tokens_month": int(group.spent_tokens_month),
        "unknown_usage_count": int(group.unknown_usage_count),
        "members": [member.to_dict() for member in group.members],
        "note": ("金额按 minor（USD x 100 整数）累加；拿不到单价的请求只记 token，"
                 "没有 usage 的请求只记 unknown_usage_count"),
    }
    if group.budget_day_minor is not None:
        view["budget_day_usd"] = format_usd_minor(int(group.budget_day_minor))
        view["spent_day_usd"] = format_usd_minor(day_spent)
    if group.budget_month_minor is not None:
        view["budget_month_usd"] = format_usd_minor(int(group.budget_month_minor))
        view["spent_month_usd"] = format_usd_minor(month_spent)
    return view


# ─────────────────────────────────────────────────────────── 失败判定
def _status_of(status: Any) -> int | None:
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    return None


def _kind_of(error_kind: Any) -> str | None:
    if not isinstance(error_kind, str):
        return None
    text = error_kind.strip().lower()
    return text or None


def failover_reason(status: int | None, error_kind: str | None) -> str | None:
    """可切换的原因标签（用于 ``stats`` / 事件）；不切换时返回 ``None``。"""
    code = _status_of(status)
    kind = _kind_of(error_kind)
    if code == RATE_LIMITED_STATUS:
        return "rate_limited"
    if code in AUTH_FAILED_STATUSES:
        return "auth_failed"
    if code is not None and 500 <= code <= 599:
        return "server_error"
    if code == TIMEOUT_STATUS:
        return "timeout"
    if code is None and kind is not None:
        if kind in NETWORK_ERROR_KINDS:
            return "timeout" if kind == "timeout" else "network_error"
        if kind.startswith("http_"):
            inner_text = kind[5:].split("_")[0].strip()
            inner = int(inner_text) if inner_text.isdigit() else None
            return failover_reason(inner, None)
    return None


def should_failover(status: int | None, error_kind: str | None) -> bool:
    """这次失败该不该换 plan（口径见模块 docstring）。

    ``400`` 与其它非 408/429 的 4xx 返回 ``False``：请求本身有问题，换谁都不行。
    未知状态（``None`` 且没有网络错误类别）返回 ``False``：不猜。
    """
    return failover_reason(status, error_kind) is not None


# ─────────────────────────────────────────────────────────── 选择
def _bump(state: PlanState, key: str) -> None:
    state.stats[key] = int(state.stats.get(key, 0)) + 1


def _bump_failover(state: PlanState, reason: str) -> None:
    _bump(state, "failover_total")
    _bump(state, "failover_total{reason=" + reason + "}")


def _weighted_choice(members: Sequence[PlanMember], rng: Any) -> PlanMember:
    total = sum(max(1, int(item.weight)) for item in members)
    if len(members) == 1 or total <= 0:
        return members[0]
    shot = float(rng.random()) * total
    upto = 0.0
    for item in members:
        upto += max(1, int(item.weight))
        if shot < upto:
            return item
    return members[-1]


def pick_target(groups: Iterable[PlanGroup], *, now: datetime | None = None,
                exclude: Iterable[str] = (),
                available: Callable[[str], bool] | None = None,
                rng: Any = None, state: PlanState | None = None) -> PlanTarget | None:
    """按优先级 + 预算 + 冷却挑一个上游；全部不可用返回 ``None``。

    * ``exclude``：本次请求已经试过的 ``upstream_id``（不重复试同一个）；
    * ``available``：可选谓词，判断该上游能否服务本次请求的模型（``False`` = 跳过）；
    * ``rng``：可选 ``random.Random``（测试注入 seed 用；缺省 ``random``）；
    * ``state``：可选路由状态（缺省进程级；测试可自建隔离实例）。

    只读 ``groups``，不改任何对象；跳过原因只进 ``stats``（``budget_exhausted_total`` /
    ``skip_total{reason=...}``），不写库、不写日志。
    """
    import random as _random

    moment = _moment(now)
    generator = rng if rng is not None else _random
    shared = state or _DEFAULT_STATE
    blocked = tuple(str(item) for item in exclude)
    now_ts = time.time() if now is None else moment.timestamp()

    with shared.lock:
        ordered = sorted([group for group in groups if group is not None and group.enabled],
                         key=lambda group: (int(group.priority), str(group.name)))
        for group in ordered:
            reason = budget_exhausted_reason(group, moment)
            if reason is not None:
                _bump(shared, "budget_exhausted_total")
                _bump(shared, "skip_total{reason=budget_exhausted}")
                continue
            candidates = [member for member in group.members
                          if member is not None and member.enabled
                          and str(member.upstream_id) not in blocked]
            if not candidates:
                continue
            if callable(available):
                candidates = [member for member in candidates
                              if available(str(member.upstream_id))]
                if not candidates:
                    _bump(shared, "skip_total{reason=model_unavailable}")
                    continue
            live = [member for member in candidates
                    if shared.auth_failed_until.get(str(member.upstream_id), 0.0) <= now_ts]
            if not live:
                _bump(shared, "skip_total{reason=auth_cooldown}")
                continue
            best = min(int(member.priority) for member in live)
            tier = [member for member in live if int(member.priority) == best]
            chosen = _weighted_choice(tier, generator)
            return PlanTarget(group_id=group.id, group_name=group.name,
                              upstream_id=str(chosen.upstream_id),
                              priority=int(chosen.priority),
                              weight=int(chosen.weight))
        _bump(shared, "pick_none_total")
        return None


def record_outcome(target: PlanTarget | None, *, ok: bool, status: int | None,
                   cost_minor: int | None = None, error_kind: str | None = None,
                   now: datetime | None = None, state: PlanState | None = None) -> None:
    """记录一次尝试的结论（切换计数在这里累加；金额不在这里记）。

    * ``ok=True``：清掉该上游的失败计数与冷却；
    * ``ok=False``：失败计数 +1；``401/403`` → 冷却 ``AUTH_COOLDOWN_S`` 秒并计
      ``failover_total{reason=auth_failed}``；其它失败只在 ``should_failover`` 为真时
      计 ``failover_total{reason=...}``（``400`` 这类不切的情况不计 failover）。
    """
    if target is None:                                # 没有目标就没什么可记的
        return
    shared = state or _DEFAULT_STATE
    upstream_id = str(target.upstream_id)
    moment = _moment(now)
    with shared.lock:
        if ok:
            shared.failures.pop(upstream_id, None)
            shared.auth_failed_until.pop(upstream_id, None)
            shared.last_failure.pop(upstream_id, None)
            return
        shared.failures[upstream_id] = int(shared.failures.get(upstream_id, 0)) + 1
        reason = failover_reason(_status_of(status), _kind_of(error_kind))
        shared.last_failure[upstream_id] = {
            "status": _status_of(status),
            "error_kind": _kind_of(error_kind),
            "reason": reason,
            "at": moment.isoformat(),
            "group_id": target.group_id,
            "cost_minor": cost_minor,
        }
        if _status_of(status) in AUTH_FAILED_STATUSES:
            shared.auth_failed_until[upstream_id] = moment.timestamp() + AUTH_COOLDOWN_S
        if reason is not None:
            _bump_failover(shared, reason)


def auth_cooldown_until(upstream_id: str, *, state: PlanState | None = None) -> float | None:
    """该上游当前的冷却截止时间戳（epoch 秒；没有冷却返回 ``None``）——排障 / 测试用。"""
    shared = state or _DEFAULT_STATE
    with shared.lock:
        value = shared.auth_failed_until.get(str(upstream_id))
        return float(value) if value else None


def failures_of(upstream_id: str, *, state: PlanState | None = None) -> int:
    """该上游累计失败次数（进程内）。"""
    shared = state or _DEFAULT_STATE
    with shared.lock:
        return int(shared.failures.get(str(upstream_id), 0))


# ─────────────────────────────────────────────────────────── 金额
def estimate_cost_minor(*, tokens: int | None, price: Any,
                        unit: str | None = None) -> int | None:
    """按「单价 x token 数」算金额（minor = USD x 100 整数）。

    * ``price`` 是 ``upstreams.models[].min_price``（美元字符串，如 ``"12.34"``）；
    * ``unit`` 缺省 ``facts.DEFAULT_MIN_PRICE_UNIT``（``usd_per_1m_tokens``）；
      其它单位一律返回 ``None``（**不认识就不记金额**，不猜汇率 / 不猜口径）；
    * ``tokens`` 缺失 / 非正数 / 单价非法 → ``None``（同上：宁可不记也不记错）。

    单次金额按 minor 四舍五入（``ROUND_HALF_UP``）：< 0.5 美分记 0，属已知低估。
    """
    if tokens is None or isinstance(tokens, bool) or not isinstance(tokens, int):
        return None
    if tokens <= 0:
        return None
    if price is None:
        return None
    resolved_unit = str(unit or facts.DEFAULT_MIN_PRICE_UNIT).strip()
    if resolved_unit != facts.DEFAULT_MIN_PRICE_UNIT:
        return None
    text = str(price).strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    minor = (Decimal(tokens) * amount * CENT / _1M).quantize(Decimal(1),
                                                             rounding=ROUND_HALF_UP)
    return int(minor)


def parse_usd_minor(value: Any) -> int:
    """美元字符串 → minor（x100 整数）。拒绝空 / 负数 / 非法 / 超过 2 位小数。"""
    text = "" if value is None else str(value).strip()
    if not text:
        raise UsageError("金额不能为空（美元，如 12.34）")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise UsageError("金额必须是十进制数字（美元，如 12.34）：" + text)
    if not amount.is_finite():
        raise UsageError("金额必须是有限数字：" + text)
    if amount < 0:
        raise UsageError("金额不能为负：" + text)
    minor = amount * CENT
    if minor != minor.to_integral_value():
        raise UsageError("金额最多 2 位小数（美分为最小单位）：" + text)
    return int(minor)


def format_usd_minor(minor: Any) -> str:
    """minor（整数）→ 美元字符串（不用浮点，避免 ``0.30000000000000004``）。"""
    value = int(minor)
    sign = "-" if value < 0 else ""
    value = abs(value)
    return sign + str(value // CENT) + "." + str(value % CENT).zfill(2)
