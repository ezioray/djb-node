# SPDX-License-Identifier: Apache-2.0
"""报价发布（CR-20261106-OFFICIAL-SUPPLY-01 / `design/17` §15）。

**为什么需要这个模块**：今天没有任何客户端会报价 —— `protocol.PATHS` 里没有
`schedule/quotes`、能力位里也没有报价能力，云侧 `POST /api/v1/schedule/quotes`
只有一个端点在等调用。结果是「供方接入 ≠ 能接单」：节点进了容量，却因为拿不出报价
永远进不了候选集（F5 `no_quote`）。

本模块只做三件事，全部是纯函数（**不 import daemon、可离线测试**）：

* :func:`quote_rows` / :func:`plan_quotes` —— 从**可公告模型**生成报价行
  （P1 0 价拒发 / P6 `unit` 单一口径 / P2 `ttl_s = 2 × 周期` / P4 探针不新鲜不报）；
* :func:`publish` —— 逐行调 API，**一行失败不影响其它行**，返回计数 + 失败分类；
* :func:`resolve_period` / :func:`validate_ttl` —— 配置与参数校验，
  **硬性拒绝** `ttl_s != 2 * period_s`。

硬规则（违反任何一条都会让账或候选集坏掉）：

* 价格一律**整数分**（USD 分 / 每 1M tokens）。云端 `parse_amount` 对 ``int`` 按
  「已经是分」处理，对 ``float`` 直接拒绝 —— 所以这里用 :class:`~decimal.Decimal`
  换算，绝不做浮点运算。
* 凭据只在 ``X-Client-Key`` 头里（由 :class:`djb_node.api.NodeApi` 负责）；
  本模块不读、不写、不打印任何凭据，也不把价格明细写进日志。
* 报价是**追加**语义：内容不变时云端原样复用（``created=false``，不升版本、
  **不延长** ``expires_at``）。因此 TTL 必须等于 2 倍发布周期（P2），见
  :data:`djb_node.facts.QUOTE_TTL_MULTIPLE`。
* **故意不使用幂等键**：云端 ``_replay_if_recorded`` 会按幂等键回放旧响应；
  同一内容跨周期复用同一幂等键，会让「已过期的报价」永远不再登记（报价到期后
  没有新行 ⇒ 节点掉出候选集）。去重交给服务端的内容指纹。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import facts
from .errors import (AuthError, ConflictError, ForbiddenError, NetworkError,
                     RateLimitError, UsageError, VerifyError)
from .upstream import ModelEntry, UpstreamManager, capacity_unit
from .util import utcnow

__all__ = [
    "QuoteRow", "QuotePlan", "PublishResult",
    "quote_rows", "plan_quotes", "publish", "price_to_minor",
    "resolve_period", "validate_period", "validate_ttl", "classify_error",
    "SKIP_DISABLED", "SKIP_ZERO_PRICE", "SKIP_INVALID_PRICE", "SKIP_NOT_PROBED",
    "SKIP_STALE", "SKIP_UPSTREAM", "SKIP_DUPLICATE",
]

# ── 跳过原因（只用于本地计数 / 日志，不上行、不含价格明细）
SKIP_DISABLED = "disabled"
"""模型被本地关闭（`enabled=false`）。"""
SKIP_ZERO_PRICE = "zero_price"
"""未定价或 0 价（P1）：云侧 ``charge_minor_for_tokens(price=0)=0`` ⇒ 等于白送。"""
SKIP_INVALID_PRICE = "invalid_price"
"""`min_price` 不是合法十进制（拒发，不猜）。"""
SKIP_NOT_PROBED = "not_probed"
"""探针从未通过（`inference_ok is not True`）——P4：探针失败即停发。"""
SKIP_STALE = "stale_probe"
"""`last_probe_at` 超出 `facts.PROBE_FRESH_AFTER_S`（比云侧 900 s 更保守）。"""
SKIP_UPSTREAM = "upstream_gated"
"""上游整体被门控（`enabled=false` 或 `state=rejected`）。"""
SKIP_DUPLICATE = "duplicate"
"""同一 `(access, model, unit)` 已有更便宜的行（云端只认最新一条，多发只会互相顶掉）。"""


# ─────────────────────────────────────────────────────────── 数据形状
@dataclass(frozen=True)
class QuoteRow:
    """一条待发布报价。字段名与云端 `QuoteRequest` 逐字一致（不要改名）。"""

    upstream_id: str
    access: str
    model: str
    price_min: int
    ttl_s: int
    unit: str = facts.CAPACITY_UNIT
    price_max: int | None = None
    currency: str = facts.QUOTE_CURRENCY

    def __post_init__(self) -> None:
        # P1 兜底：即使有人手工造行，0 价也进不了发布路径（0 价 = 白送）。
        if isinstance(self.price_min, bool) or not isinstance(self.price_min, int):
            raise UsageError("price_min 必须是整数分（USD 分 / 每 1M tokens）")
        if self.price_min <= 0:
            raise UsageError("拒绝 0 价报价：price_min 必须 > 0（P1，0 价等于白送）")
        if self.price_max is not None and int(self.price_max) < self.price_min:
            raise UsageError("price_max 必须 ≥ price_min（云端 INV-01）")
        if isinstance(self.ttl_s, bool) or not isinstance(self.ttl_s, int) or self.ttl_s <= 0:
            raise UsageError("ttl_s 必须是正整数秒")
        if not str(self.unit or "").strip():
            raise UsageError("unit 不能为空（必须与容量公告逐字一致）")

    def payload(self, *, node_id: str | None = None) -> dict[str, Any]:
        """云端请求体。``node_id`` 缺省时**不出现**（服务端按密钥身份取）。"""
        body: dict[str, Any] = {
            "access": self.access,
            "model": self.model,
            "unit": self.unit,
            "price_min": int(self.price_min),
            "ttl_s": int(self.ttl_s),
            "currency": self.currency,
        }
        if self.price_max is not None:
            body["price_max"] = int(self.price_max)
        if node_id:
            body["node_id"] = str(node_id)
        return body

    def to_dict(self) -> dict[str, Any]:
        """本地展示（含 ``upstream_id``；请求体里没有这个字段）。"""
        return {"upstream_id": self.upstream_id, "access": self.access,
                "model": self.model, "unit": self.unit,
                "price_min": int(self.price_min),
                "price_max": None if self.price_max is None else int(self.price_max),
                "currency": self.currency, "ttl_s": int(self.ttl_s)}


@dataclass
class QuotePlan:
    """一轮报价的完整结论：要发的行 + 每条被跳过的原因计数（审计用）。"""

    period_s: int = facts.QUOTES_PERIOD_DEFAULT_S
    ttl_s: int = facts.QUOTE_TTL_MULTIPLE * facts.QUOTES_PERIOD_DEFAULT_S
    rows: list[QuoteRow] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + int(count)

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def to_dict(self) -> dict[str, Any]:
        return {"period_s": self.period_s, "ttl_s": self.ttl_s,
                "unit": capacity_unit(), "currency": facts.QUOTE_CURRENCY,
                "count": len(self.rows), "rows": [row.to_dict() for row in self.rows],
                "skipped": dict(self.skipped), "skipped_total": self.skipped_total}


@dataclass
class PublishResult:
    """逐行发布结果（**永不抛整批**）：计数 + 失败分类。"""

    created: int = 0
    reused: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.reused + self.failed

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        classes: dict[str, int] = {}
        for item in self.errors:
            name = str(item.get("error"))
            classes[name] = classes.get(name, 0) + 1
        return {"created": self.created, "reused": self.reused,
                "failed": self.failed, "total": self.total, "ok": self.ok,
                "error_classes": classes,
                "errors": [dict(item) for item in self.errors]}


# ─────────────────────────────────────────────────────────── 价格换算（P1）
def price_to_minor(text: Any) -> int | None:
    """USD/1M 的十进制字符串 → **整数分**（``ROUND_HALF_UP``）。

    ``0.005`` → ``1``（半分进一）；``0.0049`` → ``0``（调用方按 ≤0 拒发）；
    ``0.75`` → ``75``；``8`` → ``800``。

    非十进制、负数、NaN/Inf、``bool``、``float`` 一律返回 ``None``（**拒发，不猜**）：
    云端明确拒绝 float，客户端先做一次类型收敛比事后 400 更早暴露配置错。
    """
    if isinstance(text, bool) or text is None:
        return None
    if isinstance(text, int):
        text = str(text)
    if isinstance(text, Decimal):
        text = str(text)
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    try:
        minor = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError):        # pragma: no cover - 防御
        return None
    return int(minor)


# ─────────────────────────────────────────────────────────── 周期 / TTL 校验（P2）
def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise UsageError(name + " 必须是整数（当前 " + repr(value) + "）")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    raise UsageError(name + " 必须是整数（当前 " + repr(value) + "）")


def validate_period(period_s: Any) -> int:
    """发布周期必须是 ``QUOTES_PERIOD_MIN_S``–``QUOTES_PERIOD_MAX_S`` 的整数秒。"""
    value = _require_int(period_s, "发布周期 quotes.period_s")
    if not facts.QUOTES_PERIOD_MIN_S <= value <= facts.QUOTES_PERIOD_MAX_S:
        raise UsageError(
            "发布周期必须在 " + str(facts.QUOTES_PERIOD_MIN_S) + "–"
            + str(facts.QUOTES_PERIOD_MAX_S) + " 秒之间（当前 " + str(value) + "）；"
            "TTL = " + str(facts.QUOTE_TTL_MULTIPLE) + " × 周期 必须 ≤ "
            + str(facts.QUOTE_TTL_MAX_S))
    return value


def validate_ttl(*, ttl_s: Any, period_s: Any) -> int:
    """**硬性**校验 ``ttl_s == QUOTE_TTL_MULTIPLE * period_s``（P2）。

    为什么不能随便取：云端 ``quotes.register`` 命中内容指纹就原样返回、**不延长**
    ``expires_at``。TTL 偏小 ⇒ 每个周期末尾都有空档（节点掉出候选集）；TTL 偏大
    ⇒ 停供后仍被调度（回滚不干净，`official_share` 归不了 0）。
    """
    period = _require_int(period_s, "发布周期 period_s")
    ttl = _require_int(ttl_s, "ttl_s")
    expected = facts.QUOTE_TTL_MULTIPLE * period
    if ttl != expected:
        raise UsageError(
            "ttl_s 必须 = " + str(facts.QUOTE_TTL_MULTIPLE) + " × 发布周期 = "
            + str(expected) + "（当前 ttl_s=" + str(ttl) + ", period_s="
            + str(period) + "）：重报相同内容云端不延长 expires_at（quotes.register），"
            "TTL 偏小会在每个周期末尾掉出候选集，偏大则停供后仍被调度")
    if not 1 <= ttl <= facts.QUOTE_TTL_MAX_S:
        raise UsageError("ttl_s 必须是 1–" + str(facts.QUOTE_TTL_MAX_S)
                         + " 的整数（云端硬性限制，当前 " + str(ttl) + "）")
    return ttl


def _extra(config: Any) -> dict[str, Any]:
    """把 ``ClientConfig`` / 形状等价的 mapping 统一成 extra 字典。"""
    if config is None:
        return {}
    extra = getattr(config, "extra", None)
    if isinstance(extra, Mapping):
        return dict(extra)
    if isinstance(config, Mapping):
        nested = config.get("extra")
        return dict(nested) if isinstance(nested, Mapping) else dict(config)
    return {}


def resolve_period(config: Any = None, *, period_s: Any = None) -> int:
    """决定发布周期（秒）：显式参数 > config ``quotes.period_s`` > 默认 60。

    同时校验 config 里显式给出的 ``quotes.ttl_s``（若存在）：必须
    ``== 2 × period_s``，否则抛 :class:`UsageError`（P2 的「配置校验硬性拒绝」）。
    两种写法都认：平铺 ``extra["quotes.period_s"]`` 或嵌套 ``extra["quotes"]``。
    """
    extra = _extra(config)
    nested = extra.get("quotes") if isinstance(extra.get("quotes"), Mapping) else {}
    configured_period = extra.get("quotes.period_s")
    if configured_period is None:
        configured_period = nested.get("period_s") if isinstance(nested, Mapping) else None
    raw = period_s if period_s is not None else configured_period
    period = validate_period(facts.QUOTES_PERIOD_DEFAULT_S if raw is None else raw)
    configured_ttl = extra.get("quotes.ttl_s")
    if configured_ttl is None:
        configured_ttl = nested.get("ttl_s") if isinstance(nested, Mapping) else None
    if configured_ttl is not None:
        validate_ttl(ttl_s=configured_ttl, period_s=period)
    return period


# ─────────────────────────────────────────────────────────── 生成报价行
def plan_quotes(upstreams: UpstreamManager, *, now: Any = None,
                period_s: Any = None, upstream_id: str | None = None) -> QuotePlan:
    """从**可公告模型**生成一轮报价计划（纯函数，不联网）。

    门控顺序（与 `UpstreamManager.announceable_models` 同源，另加报价专属两关）：

    1. 上游：``enabled=false`` / ``state=rejected`` → ``upstream_gated``；
    2. 模型：``enabled=false`` → ``disabled``；
    3. 探针：``inference_ok is not True`` → ``not_probed``（P4：失败即停发）；
    4. 新鲜度：``last_probe_at`` 超过 ``PROBE_FRESH_AFTER_S``（600 s）→ ``stale_probe``；
    5. 价格：未定价 / 换算后 ≤0 分 → ``zero_price``（P1）；非法十进制 → ``invalid_price``；
    6. 去重：同一 ``(access, model, unit)`` 只留**最便宜**的一条（同价取上游 id 较小的），
       其余记 ``duplicate`` —— 云端按 ``(node, access, model, unit)`` 只认最新一条，
       多上游同时报同价键会互相顶掉、造成版本抖动。
    """
    moment = now or utcnow()
    period = validate_period(facts.QUOTES_PERIOD_DEFAULT_S if period_s is None else period_s)
    plan = QuotePlan(period_s=period, ttl_s=facts.QUOTE_TTL_MULTIPLE * period)
    planned: dict[tuple[str, str, str], QuoteRow] = {}
    for record in upstreams.list():
        if upstream_id and record.id != upstream_id:
            continue
        gated = (not record.enabled) or record.state == "rejected"
        for raw in record.models:
            entry = ModelEntry.from_dict(raw)
            if gated:
                plan.skip(SKIP_UPSTREAM)
                continue
            if not entry.enabled:
                plan.skip(SKIP_DISABLED)
                continue
            if entry.inference_ok is not True:
                plan.skip(SKIP_NOT_PROBED)
                continue
            if not entry.fresh(moment, ttl=facts.PROBE_FRESH_AFTER_S):
                plan.skip(SKIP_STALE)
                continue
            if entry.min_price is None:
                plan.skip(SKIP_ZERO_PRICE)
                continue
            minor = price_to_minor(entry.min_price)
            if minor is None:
                plan.skip(SKIP_INVALID_PRICE)
                continue
            if minor <= 0:
                plan.skip(SKIP_ZERO_PRICE)
                continue
            row = QuoteRow(upstream_id=record.id, access=record.access_type,
                           model=entry.id, price_min=minor, ttl_s=plan.ttl_s)
            key = (row.access, row.model, row.unit)
            existing = planned.get(key)
            if existing is None:
                planned[key] = row
                continue
            plan.skip(SKIP_DUPLICATE)
            if (row.price_min, row.upstream_id) < (existing.price_min,
                                                   existing.upstream_id):
                planned[key] = row
    plan.rows = [planned[key] for key in sorted(planned)]
    return plan


def quote_rows(upstreams: UpstreamManager, *, now: Any = None,
               period_s: Any = None, upstream_id: str | None = None) -> list[QuoteRow]:
    """:func:`plan_quotes` 的「只要行」视图（任务单口径的入口）。"""
    return plan_quotes(upstreams, now=now, period_s=period_s,
                       upstream_id=upstream_id).rows


# ─────────────────────────────────────────────────────────── 发布
def classify_error(exc: BaseException) -> str:
    """失败分类（**只按异常类型**，不解析文案、不外泄请求体）。"""
    if isinstance(exc, AuthError):
        return "auth"
    if isinstance(exc, ForbiddenError):
        return "forbidden"
    if isinstance(exc, ConflictError):
        return "conflict"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, VerifyError):
        return "verify"
    if isinstance(exc, NetworkError):
        return "network"
    if isinstance(exc, UsageError):
        return "invalid"
    return "other"


def _error_row(row: QuoteRow, exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    status = details.get("status") if isinstance(details, Mapping) else None
    code = getattr(exc, "code", None)
    return {"upstream_id": row.upstream_id, "model": row.model,
            "error": classify_error(exc), "code": code, "status": status}


def publish(client: Any, rows: Iterable[QuoteRow], *,
            node_id: str | None = None) -> PublishResult:
    """逐行调 ``client.publish_quote``；**一行失败不影响其它行**（不抛整批）。

    ``client`` 只需要有 ``publish_quote(payload) -> dict``（鸭子类型，便于离线单测）。
    计数口径：``created`` = 服务端新建（``created`` 不为 False）；``reused`` =
    内容指纹命中、原样复用（``created=false``，不升版本、不续期）。
    """
    result = PublishResult()
    for row in rows:
        try:
            response = client.publish_quote(row.payload(node_id=node_id))
        except Exception as exc:                        # 单行失败：计数 + 分类后继续
            result.failed += 1
            result.errors.append(_error_row(row, exc))
            continue
        if isinstance(response, Mapping) and response.get("created") is False:
            result.reused += 1
        else:
            result.created += 1
    return result
