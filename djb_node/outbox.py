# SPDX-License-Identifier: Apache-2.0
"""双签收据：规范串、签名、加密缓冲（M05 §8.2 / M15 §5 / INV-07）。

规范口径（M05 CR-2 已裁决，写进 04-api-contracts / 02-events）：

* 算法：hmac-sha256: + 64 位小写十六进制；
* 字段顺序：receipt_id, task_id, access, usage_amount, usage_unit, price_amount；
* 原样字符串：直接用上报文本拼接（1000 与 1000.0 是不同规范串），不做数值规整；
* 两侧密钥必须不同：供方（本机）签 sig_provider，需方（平台）签 sig_consumer。

缓冲规则（INV-07）：密封后落本地 SQLite，≤24 h；超过 24 h 丢弃**并停止接单**，
不静默继续跑。缓冲里只有收据元数据，不含任何 prompt / response 正文。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from . import crypto, facts
from .errors import ReceiptError
from .store import Store
from .util import constant_time_equals, hmac_sha256, iso, new_request_id, utcnow

AMOUNT_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
DEFAULT_PRICE_UNIT = "usd_minor"
DEFAULT_USAGE_UNIT = "token"


# ─────────────────────────────────────────────────────────── 规范串 / 签名
def canonical_string(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for name in facts.RECEIPT_CANONICAL_FIELDS:
        if name not in fields:
            raise ReceiptError("收据缺规范字段：" + name)
        value = fields[name]
        if not isinstance(value, str) or not value:
            raise ReceiptError("收据规范字段必须是原样非空字符串：" + name)
        if "\n" in value or "\r" in value:
            raise ReceiptError("收据规范字段不得包含换行：" + name)
        parts.append(value)
    return facts.RECEIPT_CANONICAL_SEPARATOR.join(parts)


def signature(key: bytes, fields: dict[str, Any]) -> str:
    return facts.RECEIPT_SIGNATURE_PREFIX + hmac_sha256(key, canonical_string(fields))


def sign_receipt(fields: dict[str, Any], provider_key: bytes,
                 consumer_key: bytes | None = None) -> dict[str, str]:
    """参考实现（客户端生产 / 测试构造）；服务端只验不算。"""
    out = {"sig_provider": signature(provider_key, fields)}
    if consumer_key is not None:
        out["sig_consumer"] = signature(consumer_key, fields)
    return out


def well_formed_signature(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(facts.RECEIPT_SIGNATURE_PREFIX):
        return False
    body = value[len(facts.RECEIPT_SIGNATURE_PREFIX):]
    return len(body) == facts.RECEIPT_SIG_HEX_LEN and all(
        char in "0123456789abcdef" for char in body)


def canonical_fields_of(receipt: dict[str, Any]) -> dict[str, Any]:
    """从 ReceiptInput 结构提取 6 个规范字段（按上报原样字符串）。"""
    usage = receipt.get("usage")
    price = receipt.get("price")
    if not isinstance(usage, dict) or not isinstance(price, dict):
        raise ReceiptError("收据 usage / price 必须是对象（spec ReceiptInput）")
    return {
        "receipt_id": receipt.get("receipt_id"),
        "task_id": receipt.get("task_id"),
        "access": receipt.get("access"),
        "usage_amount": usage.get("amount"),
        "usage_unit": usage.get("unit"),
        "price_amount": price.get("amount"),
    }


def validate_receipt(receipt: Any) -> list[str]:
    """返回问题列表（空 = 形态合法）。双签缺一即不合法（spec：单边签名直接拒收）。"""
    problems: list[str] = []
    if not isinstance(receipt, dict):
        return ["收据必须是 JSON 对象"]
    for name in ("receipt_id", "task_id", "access"):
        value = receipt.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append("缺字段 " + name)
    access = receipt.get("access")
    if isinstance(access, str) and access not in facts.ACCESS_TYPES:
        problems.append("access 必须是 A1/A2/B")
    usage = receipt.get("usage")
    if not isinstance(usage, dict):
        problems.append("缺 usage 对象")
    else:
        amount = usage.get("amount")
        if not isinstance(amount, str) or not AMOUNT_RE.match(amount):
            problems.append("usage.amount 必须是十进制字符串")
        if not isinstance(usage.get("unit"), str) or not usage.get("unit"):
            problems.append("usage.unit 必填")
    price = receipt.get("price")
    if not isinstance(price, dict):
        problems.append("缺 price 对象")
    else:
        amount = price.get("amount")
        if not isinstance(amount, str) or not AMOUNT_RE.match(amount):
            problems.append("price.amount 必须是十进制字符串")
        if not isinstance(price.get("unit"), str) or not price.get("unit"):
            problems.append("price.unit 必填")
    for side in ("sig_provider", "sig_consumer"):
        if not well_formed_signature(receipt.get(side)):
            problems.append("签名缺失或格式不合法：" + side)
    if not problems:
        try:
            canonical_string(canonical_fields_of(receipt))
        except ReceiptError as exc:
            problems.append(str(exc))
    return problems


def provider_signature_valid(receipt: dict[str, Any], provider_key: bytes) -> bool:
    try:
        payload = canonical_string(canonical_fields_of(receipt))
        expected = facts.RECEIPT_SIGNATURE_PREFIX + hmac_sha256(provider_key, payload)
    except ReceiptError:
        return False
    return constant_time_equals(expected, str(receipt.get("sig_provider") or ""))


class ReceiptFactory:
    """本机（供方）签名工厂：生成双签收据，sig_consumer 由调用方（需方）给定。"""

    def __init__(self, provider_key: bytes, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.provider_key = provider_key
        self._clock = clock

    def build(self, *, task_id: str, access: str, usage_amount: str | int,
              unit: str = DEFAULT_USAGE_UNIT, price_amount: str | int,
              price_unit: str = DEFAULT_PRICE_UNIT,
              sig_consumer: str | None = None,
              receipt_id: str | None = None,
              usage_source: str = "client_reported") -> dict[str, Any]:
        receipt = {
            "receipt_id": receipt_id or new_request_id(),
            "task_id": str(task_id),
            "access": access,
            "usage": {"amount": str(usage_amount), "unit": unit},
            "price": {"amount": str(price_amount), "unit": price_unit},
            "sig_provider": "",
            "sig_consumer": sig_consumer or "",
            "usage_source": usage_source,
        }
        if not well_formed_signature(receipt["sig_consumer"]):
            raise ReceiptError("需方签名缺失或格式不合法：双签缺一即不得入缓冲")
        receipt["sig_provider"] = signature(self.provider_key, canonical_fields_of(receipt))
        problems = validate_receipt(receipt)
        if problems:
            raise ReceiptError("收据不合法：" + "；".join(problems[:3]))
        return receipt


class SealedReceiptBuffer:
    """把收据密封进本机 SQLite（≤24 h）；不含正文、不含凭据。"""

    def __init__(self, store: Store, key: bytes, *, max_age_s: int = facts.RECEIPT_BUFFER_MAX_AGE_S,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.key = key
        self.max_age_s = int(max_age_s)
        self._now = now

    def enqueue(self, receipt: dict[str, Any], *, provider_key: bytes | None = None) -> bool:
        problems = validate_receipt(receipt)
        if problems:
            raise ReceiptError("收据不合法：" + "；".join(problems[:3]))
        if provider_key is not None and not provider_signature_valid(receipt, provider_key):
            raise ReceiptError("供方签名与本机密钥不匹配")
        sealed = crypto.seal(self.key, json.dumps(receipt, ensure_ascii=False,
                                                  sort_keys=True))
        return self.store.add_receipt(str(receipt["receipt_id"]), sealed,
                                      buffered_at=iso(self._now()))

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        for row in self.store.receipts(limit=10000):
            if row.receipt_id == receipt_id:
                return self._open(row.sealed)
        return None

    def pending(self, *, limit: int = facts.RECEIPT_BATCH_MAX,
                now: datetime | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self.store.pending_receipts(now or self._now(), limit=limit):
            try:
                out.append(self._open(row.sealed))
            except crypto.TamperError:
                # 密封串坏了：删掉坏条目（不再重试），并让 daemon 记审计事件
                self.store.delete_receipt(row.receipt_id)
        return out

    def _open(self, sealed: str) -> dict[str, Any]:
        payload = crypto.open_sealed(self.key, sealed).decode("utf-8")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise crypto.TamperError("收据载荷不是对象")
        return value

    def ack(self, receipt_ids: list[str]) -> int:
        removed = 0
        for receipt_id in receipt_ids:
            if self.store.delete_receipt(receipt_id):
                removed += 1
        return removed

    def retry(self, receipt_id: str, *, attempts: int, next_at: str | None,
              error: str | None = None) -> None:
        self.store.mark_receipt_retry(receipt_id, attempts=attempts,
                                      next_attempt_at=next_at, last_error=error)

    # ── 24 h 规则
    def age_of_oldest_s(self, *, now: datetime | None = None) -> int:
        oldest = self.store.oldest_receipt_at()
        if not oldest:
            return 0
        from .util import parse_iso
        moment = parse_iso(oldest)
        if moment is None:
            return 0
        return max(0, int(((now or self._now()) - moment).total_seconds()))

    def must_stop_accepting(self, *, now: datetime | None = None) -> bool:
        return self.age_of_oldest_s(now=now) >= self.max_age_s

    def mark_stale_expired(self, *, now: datetime | None = None) -> list[str]:
        """超 24 h：**不丢弃**，标 stale（仍退避重推）并计入诊断。

        Lead 2026-10-24 裁决（CR-2）：本地缓冲上限 min(24h, M05 约束)，M05 未规定更短
        TTL -> 保留 24 h；超期标 stale + 停接单 + 退避重推；收据按 receipt_id 幂等，
        服务端可重收。不静默丢弃（INV-01）。
        """
        cutoff = (now or self._now()) - timedelta(seconds=self.max_age_s)
        return self.store.mark_receipts_stale(cutoff)

    def stale_count(self) -> int:
        return self.store.stale_receipt_count()

    def count(self) -> int:
        return self.store.receipt_count()
