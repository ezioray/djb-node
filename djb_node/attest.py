# SPDX-License-Identifier: Apache-2.0
"""模型身份证明（M17 §5.1–5.3 / C12 客户端侧）。

对应规格：``product/v4/modules/M17-supply-integrity.md`` §5.1（L0–L5）、§5.3（真实请求
tokenizer 校验）、§5.7（状态机 / TTL）、§8.1（上报字段，只加不改）、§9（开关默认关闭）。

**客户端证据只作线索**（§1「不做」/ §11「篡改客户端 probe → 服务端定级不受影响」）：

* 本模块产出的等级一律标 ``trust = client_side_clue``，``verified`` 恒为 ``False``；
  真正的定级只由服务端可观测证据给出（C10 通道未接线，本批**不调服务端**）。
* 不落 prompt / response 正文：证据只存 ``response_hash``、token 计数、usage、耗时类标量。
* 不写账本 / 收据 / 服务端表；记录只落本机 kv（``attest:<upstream_id>:<model>``）。

状态机（§5.7，只升不降）::

    L0 declared → L1 reachable → L2 inference → L3 tokenizer → L4 behavioral → L5 provider
                        ↘ failed / expired / suspended

* ``advance()`` 只允许**逐级向上**，任何降级一律拒绝（``ConflictError``）。
* 唯一的降级路径是 :func:`fail` 显式给出的 ``downgrade_to``（§5.3「tokenizer 偏差 → 降为 L2」），
  且 ``best_level`` 永远保留历史最高，便于审计「曾经到过哪一级」。
* TTL（§5.7）：L1/L2 24h、L3 7d、L4 30d、L5 90d；过期 → ``state=expired``，公告/接单门控失败。
"""
from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import facts
from .config import ClientConfig, feature_flag, flag_spec
from .errors import ConflictError, FeatureDisabled, UsageError, VerifyError
from .store import Store
from .upstream import MODEL_ID_RE
from .util import iso, new_request_id, parse_iso, sha256_hex, utcnow

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")
METHOD_RE = re.compile(r"^[a-z0-9_.\-]{2,32}$")


class AttestError(UsageError):
    """证明记录 / 证据形状错误（用法层）。"""


class ReplayError(VerifyError):
    """nonce / challenge 重放（§2 T4：100% 拒绝，不静默降级）。"""


def attestation_enabled(config: ClientConfig | None) -> bool:
    """开关 ``supply.attestation.enabled``（默认关闭；统一走 config.feature_flag）。"""
    return bool(config is not None and feature_flag(config, facts.ATTEST_FLAG))


# ─────────────────────────────────────────────────────────── 等级 / TTL
def level_index(level: Any) -> int:
    try:
        return facts.ATTEST_LEVELS.index(str(level))
    except ValueError:
        raise AttestError("未知证明等级：" + str(level)) from None


def validate_level(level: Any) -> str:
    text = str(level or "").strip().upper()
    level_index(text)
    return text


def ttl_for(level: str, overrides: dict[str, int] | None = None) -> int:
    """§5.7 TTL（秒）；``overrides`` 来自配置，允许按环境调整（不硬编码在业务流程里）。"""
    table = dict(facts.ATTEST_LEVEL_TTL_S)
    if overrides:
        for key, value in overrides.items():
            if str(key) in table:
                try:
                    table[str(key)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
    return int(table[validate_level(level)])


# ─────────────────────────────────────────────────────────── 证据
def hash_response(payload: bytes | str) -> str:
    """响应正文 → sha256（正文本身不落盘）。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return sha256_hex(payload or b"")


def _check_scalar(name: str, value: Any, *, limit: int = 512,
                  pattern: re.Pattern[str] | None = None) -> str:
    text = str(value)
    if any(char in text for char in "\r\n"):
        raise AttestError("证据字段 " + name + " 不得包含换行（不落正文）")
    if len(text) > limit:
        raise AttestError("证据字段 " + name + " 过长（不落正文，只存 hash / 计数）")
    if pattern is not None and not pattern.match(text):
        raise AttestError("证据字段 " + name + " 形状不合法")
    return text


@dataclass
class Evidence:
    """一条证据（§8.1 字段；不含任何正文 / 凭据）。"""

    challenge_id: str
    model: str
    method: str
    level: str
    nonce: str
    result: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    response_hash: str | None = None
    signature: str | None = None
    confidence: float = 0.0
    reason_code: str | None = None
    created_at: str | None = None
    schema: int = 1

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": int(self.schema),
            "challenge_id": self.challenge_id,
            "model": self.model,
            "method": self.method,
            "level": self.level,
            "nonce": self.nonce,
            "result": self.result,
            "confidence": round(float(self.confidence), 4),
            "created_at": self.created_at,
        }
        if self.tokens_in is not None:
            payload["tokens_in"] = int(self.tokens_in)
        if self.tokens_out is not None:
            payload["tokens_out"] = int(self.tokens_out)
        if self.usage:
            payload["usage"] = {str(key): int(value)
                                for key, value in self.usage.items()}
        if self.response_hash:
            payload["response_hash"] = self.response_hash
        if self.signature:
            payload["signature"] = self.signature
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Evidence":
        return cls(
            challenge_id=str(raw.get("challenge_id") or ""),
            model=str(raw.get("model") or ""),
            method=str(raw.get("method") or ""),
            level=str(raw.get("level") or "L0"),
            nonce=str(raw.get("nonce") or ""),
            result=str(raw.get("result") or "unknown"),
            tokens_in=_int_or_none(raw.get("tokens_in")),
            tokens_out=_int_or_none(raw.get("tokens_out")),
            usage={str(key): int(value)
                   for key, value in (raw.get("usage") or {}).items()
                   if _int_or_none(value) is not None},
            response_hash=(str(raw["response_hash"]) if raw.get("response_hash") else None),
            signature=(str(raw["signature"]) if raw.get("signature") else None),
            confidence=float(raw.get("confidence") or 0.0),
            reason_code=(str(raw["reason_code"]) if raw.get("reason_code") else None),
            created_at=raw.get("created_at"),
            schema=int(raw.get("schema") or 1))


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def validate_evidence(evidence: Evidence) -> Evidence:
    """证据形状校验（fail-closed）：等级 / 方法 / nonce / hash / 计数，禁止正文。"""
    if not isinstance(evidence, Evidence):
        raise AttestError("证据必须是 Evidence")
    evidence.level = validate_level(evidence.level)
    if not METHOD_RE.match(str(evidence.method or "")):
        raise AttestError("证据 method 不合法（小写字母 / 数字 / _ . -，2–32）")
    if str(evidence.result) not in facts.ATTEST_EVIDENCE_RESULTS:
        raise AttestError("证据 result 必须是 "
                          + " / ".join(facts.ATTEST_EVIDENCE_RESULTS))
    evidence.challenge_id = _check_scalar("challenge_id", evidence.challenge_id,
                                          limit=128)
    if not evidence.challenge_id:
        raise AttestError("证据缺少 challenge_id")
    evidence.nonce = _check_scalar("nonce", evidence.nonce, limit=128)
    if not TOKEN_RE.match(evidence.nonce):
        raise AttestError("证据 nonce 不合法（8–128，字母数字 . _ : -）")
    evidence.model = _check_scalar("model", evidence.model, limit=128,
                                   pattern=MODEL_ID_RE)
    if evidence.response_hash is not None:
        evidence.response_hash = _check_scalar("response_hash", evidence.response_hash,
                                               limit=64, pattern=HASH_RE)
    if evidence.signature is not None:
        evidence.signature = _check_scalar("signature", evidence.signature, limit=512)
    for name in ("tokens_in", "tokens_out"):
        value = getattr(evidence, name)
        if value is not None and not isinstance(value, int):
            raise AttestError("证据 " + name + " 必须是整数")
        if value is not None and value < 0:
            raise AttestError("证据 " + name + " 不能为负")
    if len(evidence.usage) > 8:
        raise AttestError("证据 usage 字段过多（只存计数，不存正文）")
    for key, value in evidence.usage.items():
        _check_scalar("usage." + str(key), key, limit=32)
        if not isinstance(value, int) or value < 0:
            raise AttestError("证据 usage 值必须是非负整数")
    evidence.confidence = round(min(1.0, max(0.0, float(evidence.confidence))), 4)
    evidence.created_at = evidence.created_at or iso(utcnow())
    return evidence


def build_evidence(*, challenge_id: str, model: str, method: str, level: str,
                   nonce: str, result: str, tokens_in: int | None = None,
                   tokens_out: int | None = None, usage: dict[str, int] | None = None,
                   response_hash: str | None = None, signature: str | None = None,
                   confidence: float = 0.0, reason_code: str | None = None,
                   created_at: datetime | None = None) -> Evidence:
    evidence = Evidence(challenge_id=challenge_id, model=model, method=method,
                        level=level, nonce=nonce, result=result, tokens_in=tokens_in,
                        tokens_out=tokens_out, usage=dict(usage or {}),
                        response_hash=response_hash, signature=signature,
                        confidence=confidence, reason_code=reason_code,
                        created_at=iso(created_at or utcnow()))
    return validate_evidence(evidence)


NO_BODY_FORBIDDEN_KEYS = frozenset({
    "prompt", "response", "messages", "content", "body", "text", "completion"})


def assert_no_body(payload: dict[str, Any]) -> None:
    """测试 / 自检用：证据里不得出现正文类字段或长文本。"""
    for key, value in payload.items():
        if str(key).lower() in NO_BODY_FORBIDDEN_KEYS:
            raise AttestError("证据不得包含正文类字段：" + str(key))
        if isinstance(value, str) and (len(value) > 512 or "\n" in value):
            raise AttestError("证据字段过长 / 含换行，疑似正文：" + str(key))
        if isinstance(value, dict):
            assert_no_body(value)


# ─────────────────────────────────────────────────────────── tokenizer 校验
def tokenizer_family(model: str) -> str | None:
    """本地已知 tokenizer 家族（最长前缀优先）；未知返回 None（不猜）。"""
    lowered = str(model or "").lower()
    best: tuple[int, str] | None = None
    for prefix, family in facts.ATTEST_TOKENIZER_HINTS.items():
        if lowered == prefix or lowered.startswith(prefix + "-") \
                or lowered.startswith(prefix + ".") or lowered.startswith(prefix + ":"):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), family)
    return best[1] if best else None


def tolerance_tokens(expected: int, *, pct: float | None = None,
                     tokens: int | None = None) -> int:
    """§5.1 容差「±1 token 或 1%」→ 取较大者；整数化用 ceil（对供应商更宽松一侧）。"""
    rate = facts.ATTEST_TOKENIZER_TOLERANCE_PCT if pct is None else float(pct)
    floor = facts.ATTEST_TOKENIZER_TOLERANCE_TOKENS if tokens is None else int(tokens)
    by_pct = int(math.ceil(max(0, int(expected)) * max(0.0, rate) / 100.0))
    return max(int(floor), by_pct)


def default_tokenizer_counter(model: str, text: str) -> int | None:
    """默认计数器：惰性 import ``djb_node.tokens``（C8 写域），缺失即返回 None。

    约定（接口只加不改，C8 侧实现其一即可）::

        djb_node.tokens.count_prompt_tokens(model=..., text=...) -> int
        djb_node.tokens.count_tokens(model, text) -> int

    任何异常 / 非法返回值都降级为 ``None`` → 结果 ``unknown``，绝不猜、绝不抛。
    """
    try:
        module = importlib.import_module(".tokens", __package__)
    except Exception:
        return None
    for name in facts.ATTEST_COUNTER_CANDIDATES:
        func = getattr(module, name, None)
        if not callable(func):
            continue
        value: Any = None
        try:
            value = func(model=model, text=text)
        except TypeError:
            try:
                value = func(model, text)
            except Exception:
                return None
        except Exception:
            return None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None
    return None


@dataclass
class TokenizerCheck:
    """L3 tokenizer 校验结果（§5.3）。"""

    result: str
    expected: int | None = None
    actual: int | None = None
    diff: int | None = None
    tolerance: int | None = None
    tokenizer_id: str | None = None
    reason_code: str | None = None

    @property
    def level(self) -> str:
        return "L3"

    @property
    def confidence(self) -> float:
        if self.result == "match":
            return 0.9
        if self.result == "mismatch":
            return 0.0
        return 0.3

    def to_evidence(self, *, challenge_id: str, model: str, nonce: str,
                    response_hash: str | None = None, tokens_out: int | None = None,
                    signature: str | None = None,
                    created_at: datetime | None = None) -> Evidence:
        return build_evidence(
            challenge_id=challenge_id, model=model, method=facts.ATTEST_LEVEL_METHOD["L3"],
            level="L3", nonce=nonce, result=self.result,
            tokens_in=self.actual, tokens_out=tokens_out,
            usage={"prompt_tokens": int(self.actual)} if self.actual is not None else {},
            response_hash=response_hash, signature=signature,
            confidence=self.confidence, reason_code=self.reason_code,
            created_at=created_at)

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result, "expected": self.expected, "actual": self.actual,
                "diff": self.diff, "tolerance": self.tolerance,
                "tokenizer_id": self.tokenizer_id, "reason_code": self.reason_code}


def tokenizer_match(prompt: str, claimed_model: str, usage_prompt_tokens: int | None, *,
                    counter: Callable[[str, str], int | None] | None = None,
                    tolerance_pct: float | None = None,
                    tolerance_floor: int | None = None) -> TokenizerCheck:
    """L3：用 claimed model 的 tokenizer 计数，与响应 ``usage.prompt_tokens`` 比对。

    返回 ``match`` / ``mismatch`` / ``unknown``：

    * usage 缺失或 ≤0 → ``unknown``（不判负，也不放过）；
    * 本地计数器不可用（C8 未接线 / 该模型无 tokenizer）→ ``unknown``；
    * 差异 ≤ 容差 → ``match``；否则 ``mismatch``（**换不同 tokenizer 必然触发**）。
    """
    actual = _int_or_none(usage_prompt_tokens)
    family = tokenizer_family(claimed_model)
    if actual is None or actual <= 0:
        return TokenizerCheck("unknown", actual=actual, tokenizer_id=family,
                              reason_code="usage_missing")
    fn = counter or default_tokenizer_counter
    try:
        expected = fn(claimed_model, prompt)
    except Exception:
        expected = None
    expected_value = _int_or_none(expected)
    if expected_value is None or expected_value <= 0:
        return TokenizerCheck("unknown", actual=actual, tokenizer_id=family,
                              reason_code="tokenizer_unavailable")
    allowed = tolerance_tokens(expected_value, pct=tolerance_pct, tokens=tolerance_floor)
    diff = abs(expected_value - actual)
    result = "match" if diff <= allowed else "mismatch"
    return TokenizerCheck(result, expected=expected_value, actual=actual, diff=diff,
                          tolerance=allowed, tokenizer_id=family,
                          reason_code=None if result == "match" else "tokenizer_mismatch")


# ─────────────────────────────────────────────────────────── nonce 防重放
class NonceLedger:
    """nonce / challenge 一次性账本（kv ``attest:nonces``，按 TTL 清理）。"""

    def __init__(self, store: Store, *, now=utcnow, ttl_s: int | None = None,
                 max_entries: int | None = None) -> None:
        self.store = store
        self._now = now
        self.ttl_s = int(ttl_s if ttl_s is not None else facts.ATTEST_NONCE_TTL_S)
        self.max_entries = int(max_entries if max_entries is not None
                               else facts.ATTEST_NONCE_MAX_ENTRIES)

    # ── 存储
    def _load(self) -> dict[str, dict[str, Any]]:
        raw = self.store.get_kv(facts.ATTEST_NONCE_KEY, default=None)
        entries = raw.get("entries") if isinstance(raw, dict) else None
        return {str(key): dict(value) for key, value in (entries or {}).items()
                if isinstance(value, dict)}

    def _save(self, entries: dict[str, dict[str, Any]], now: datetime) -> None:
        live = {key: value for key, value in entries.items()
                if (parse_iso(value.get("expires_at")) or now) > now}
        if len(live) > self.max_entries:
            ordered = sorted(live.items(),
                             key=lambda item: str(item[1].get("first_seen") or ""))
            live = dict(ordered[-self.max_entries:])
        self.store.set_kv(facts.ATTEST_NONCE_KEY, {"schema": 1, "entries": live})

    # ── 查询 / 消费
    def seen(self, token: str, *, now: datetime | None = None) -> bool:
        moment = now or self._now()
        entry = self._load().get(self._key(token))
        if not entry:
            return False
        expires = parse_iso(entry.get("expires_at"))
        return bool(expires and expires > moment)

    def consume(self, token: str, *, challenge_id: str | None = None,
                now: datetime | None = None, ttl_s: int | None = None) -> dict[str, Any]:
        """消费一次；重复即抛 :class:`ReplayError`（§11「重放 100% 拒绝」）。"""
        moment = now or self._now()
        keys = [self._key(token)]
        if challenge_id:
            keys.append("cid:" + _check_scalar("challenge_id", challenge_id, limit=128))
        entries = self._load()
        for key in keys:
            entry = entries.get(key)
            expires = parse_iso(entry.get("expires_at")) if entry else None
            if entry and expires and expires > moment:
                raise ReplayError("检测到重放：该 nonce / challenge 已使用过（拒绝，"
                                  "不静默降级）；first_seen="
                                  + str(entry.get("first_seen"))[:32])
        expires_at = iso(moment + timedelta(seconds=int(ttl_s or self.ttl_s)))
        for key in keys:
            entries[key] = {"first_seen": iso(moment), "expires_at": expires_at}
        self._save(entries, moment)
        return {"nonce": keys[0], "challenge_id": challenge_id, "expires_at": expires_at}

    @staticmethod
    def _key(token: str) -> str:
        return "n:" + _check_scalar("nonce", token, limit=128)


# ─────────────────────────────────────────────────────────── 记录 + 状态机
@dataclass
class AttestationRecord:
    upstream_id: str
    model: str
    attestation_id: str = field(default_factory=new_request_id)
    node_id: str | None = None
    level: str = "L0"
    best_level: str = "L0"
    state: str = "declared"
    confidence: float = 0.0
    verified_at: str | None = None
    expires_at: str | None = None
    failed_at: str | None = None
    reason_code: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    trust: str = facts.ATTEST_TRUST
    schema: int = facts.ATTEST_RECORD_SCHEMA

    # ── 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": int(self.schema),
            "attestation_id": self.attestation_id,
            "upstream_id": self.upstream_id,
            "model": self.model,
            "node_id": self.node_id,
            "level": self.level,
            "best_level": self.best_level,
            "state": self.state,
            "confidence": round(float(self.confidence), 4),
            "verified_at": self.verified_at,
            "expires_at": self.expires_at,
            "failed_at": self.failed_at,
            "reason_code": self.reason_code,
            "evidence": [dict(item) for item in self.evidence],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "trust": self.trust,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AttestationRecord":
        return cls(
            upstream_id=str(raw.get("upstream_id") or ""),
            model=str(raw.get("model") or ""),
            attestation_id=str(raw.get("attestation_id") or new_request_id()),
            node_id=(str(raw["node_id"]) if raw.get("node_id") else None),
            level=str(raw.get("level") or "L0"),
            best_level=str(raw.get("best_level") or raw.get("level") or "L0"),
            state=str(raw.get("state") or "declared"),
            confidence=float(raw.get("confidence") or 0.0),
            verified_at=raw.get("verified_at"),
            expires_at=raw.get("expires_at"),
            failed_at=raw.get("failed_at"),
            reason_code=(str(raw["reason_code"]) if raw.get("reason_code") else None),
            evidence=[dict(item) for item in (raw.get("evidence") or [])
                      if isinstance(item, dict)],
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            trust=str(raw.get("trust") or facts.ATTEST_TRUST),
            schema=int(raw.get("schema") or facts.ATTEST_RECORD_SCHEMA))

    # ── 视图（永不自我宣称 verified）
    def describe(self, *, now: datetime | None = None) -> dict[str, Any]:
        payload = self.to_dict()
        payload.update({
            "level_label": facts.ATTEST_LEVEL_NAMES.get(self.level, "unknown"),
            "trust": facts.ATTEST_TRUST,
            "verified": False,
            "server_verified": False,
            "note": "客户端证据只作线索；定级以服务端可观测证据为准（M17 §1 / §11）",
        })
        payload.update(freshness(self, now=now))
        return payload


def new_record(upstream_id: str, model: str, *, node_id: str | None = None,
               now: datetime | None = None) -> AttestationRecord:
    moment = now or utcnow()
    return AttestationRecord(upstream_id=str(upstream_id), model=str(model),
                             node_id=node_id, created_at=iso(moment),
                             updated_at=iso(moment))


def _touch(record: AttestationRecord, now: datetime) -> None:
    record.updated_at = iso(now)


def _append_evidence(record: AttestationRecord, evidence: Evidence | None) -> None:
    if evidence is None:
        return
    record.evidence.append(evidence.to_dict())
    if len(record.evidence) > facts.ATTEST_EVIDENCE_MAX_KEPT:
        record.evidence = record.evidence[-facts.ATTEST_EVIDENCE_MAX_KEPT:]


def advance(record: AttestationRecord, target_level: Any, *,
            evidence: Evidence | None = None, now: datetime | None = None,
            confidence: float | None = None,
            ttl_overrides: dict[str, int] | None = None) -> AttestationRecord:
    """逐级向上推进（只升不降）；非法目标一律 :class:`ConflictError`。"""
    moment = now or utcnow()
    target = validate_level(target_level)
    if record.state == "suspended":
        raise ConflictError("证明处于 suspended：需人工复核后 reopen 才能重新推进")
    current = level_index(record.level)
    goal = level_index(target)
    if goal <= current:
        raise ConflictError("等级只升不降：当前 " + record.level + " → 目标 " + target)
    if goal != current + 1:
        raise ConflictError("等级必须逐级推进：当前 " + record.level + " → 目标 " + target
                            + "（不允许跳级）")
    record.level = target
    record.best_level = facts.ATTEST_LEVELS[max(level_index(record.best_level), goal)]
    record.state = facts.ATTEST_LEVEL_NAMES[target]
    record.reason_code = None
    record.verified_at = iso(moment)
    record.expires_at = iso(moment + timedelta(seconds=ttl_for(target, ttl_overrides)))
    if confidence is not None:
        record.confidence = round(min(1.0, max(0.0, float(confidence))), 4)
    _append_evidence(record, evidence)
    _touch(record, moment)
    return record


def reopen(record: AttestationRecord, *, now: datetime | None = None,
           reason_code: str | None = None) -> AttestationRecord:
    """过期 / 失败后重新取证：回到 L0（``best_level`` 保留历史最高，不删除）。"""
    moment = now or utcnow()
    if record.state == "suspended":
        raise ConflictError("suspended 需人工复核放行，不能本地 reopen")
    record.level = "L0"
    record.state = "declared"
    record.expires_at = None
    record.verified_at = None
    record.reason_code = reason_code or "reopened"
    _touch(record, moment)
    return record


def fail(record: AttestationRecord, *, reason_code: str, state: str = "failed",
         downgrade_to: Any | None = None, evidence: Evidence | None = None,
         now: datetime | None = None) -> AttestationRecord:
    """证据失败：置 ``failed`` / ``suspended``（可选显式降级，唯一允许降级的路径）。"""
    moment = now or utcnow()
    if state not in facts.ATTEST_BLOCKING_STATES:
        raise AttestError("失败状态必须是 " + " / ".join(facts.ATTEST_BLOCKING_STATES))
    if downgrade_to is not None:
        target = validate_level(downgrade_to)
        if level_index(target) >= level_index(record.level):
            raise ConflictError("显式降级目标必须低于当前等级：" + record.level
                                + " → " + target)
        record.level = target
    record.state = state
    record.failed_at = iso(moment)
    record.reason_code = str(reason_code or "attestation_failed")[:64]
    record.confidence = 0.0
    _append_evidence(record, evidence)
    _touch(record, moment)
    return record


def refresh(record: AttestationRecord, *, now: datetime | None = None
            ) -> AttestationRecord:
    """按 TTL 刷新状态：过期 → ``expired``（不删除等级，供审计与重新取证）。"""
    moment = now or utcnow()
    if record.state in facts.ATTEST_BLOCKING_STATES:
        return record
    expires = parse_iso(record.expires_at)
    if expires is not None and expires <= moment:
        record.state = "expired"
        record.reason_code = record.reason_code or "ttl_expired"
        _touch(record, moment)
    return record


def freshness(record: AttestationRecord, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or utcnow()
    expires = parse_iso(record.expires_at)
    blocked = record.state in facts.ATTEST_BLOCKING_STATES
    seconds_left = int((expires - moment).total_seconds()) if expires else None
    fresh = (not blocked) and expires is not None and expires > moment
    if expires is None and not blocked and record.level == "L0":
        fresh = False
    return {
        "fresh": bool(fresh),
        "expired": bool(expires is not None and expires <= moment),
        "blocked": bool(blocked),
        "seconds_left": seconds_left,
        "checked_at": iso(moment),
        "ttl_s": ttl_for(record.level) if record.level in facts.ATTEST_LEVELS else None,
    }


def service_gate(record: AttestationRecord, *, now: datetime | None = None,
                 min_level: str = facts.ATTEST_MIN_SERVICE_LEVEL) -> dict[str, Any]:
    """公告 / 调度 / 结算放行门控：只认「等级 ≥ L3 且状态正常且未过期」。"""
    moment = now or utcnow()
    refresh(record, now=moment)
    info = freshness(record, now=moment)
    if record.state in facts.ATTEST_BLOCKING_STATES:
        return {"serviceable": False, "reason": "state_" + record.state,
                "level": record.level, "min_level": min_level, **info}
    if level_index(record.level) < level_index(min_level):
        return {"serviceable": False, "reason": "level_below_" + min_level,
                "level": record.level, "min_level": min_level, **info}
    if not info["fresh"]:
        return {"serviceable": False, "reason": "ttl_expired",
                "level": record.level, "min_level": min_level, **info}
    return {"serviceable": True, "reason": None, "level": record.level,
            "min_level": min_level, **info}


# ─────────────────────────────────────────────────────────── kv 持久化
def attest_key(upstream_id: str, model: str) -> str:
    return facts.ATTEST_KEY_PREFIX + str(upstream_id) + ":" + str(model)


def save_record(store: Store, record: AttestationRecord) -> AttestationRecord:
    store.set_kv(attest_key(record.upstream_id, record.model), record.to_dict())
    index = [dict(item) for item in (store.get_kv(facts.ATTEST_INDEX_KEY, default=[])
                                     or []) if isinstance(item, dict)]
    marker = {"upstream_id": record.upstream_id, "model": record.model}
    if marker not in index:
        index.append(marker)
    store.set_kv(facts.ATTEST_INDEX_KEY, index)
    return record


def load_record(store: Store, upstream_id: str, model: str) -> AttestationRecord | None:
    raw = store.get_kv(attest_key(upstream_id, model), default=None)
    if not isinstance(raw, dict):
        return None
    return AttestationRecord.from_dict(raw)


def list_records(store: Store) -> list[AttestationRecord]:
    out: list[AttestationRecord] = []
    for marker in store.get_kv(facts.ATTEST_INDEX_KEY, default=[]) or []:
        if not isinstance(marker, dict):
            continue
        record = load_record(store, str(marker.get("upstream_id")),
                             str(marker.get("model")))
        if record is not None:
            out.append(record)
    return out


def delete_record(store: Store, upstream_id: str, model: str) -> bool:
    existed = store.get_kv(attest_key(upstream_id, model), default=None) is not None
    store.delete_kv(attest_key(upstream_id, model))
    index = [dict(item) for item in (store.get_kv(facts.ATTEST_INDEX_KEY, default=[])
                                     or []) if isinstance(item, dict)]
    index = [item for item in index
             if not (item.get("upstream_id") == upstream_id and item.get("model") == model)]
    store.set_kv(facts.ATTEST_INDEX_KEY, index)
    return existed


# ─────────────────────────────────────────────────────────── 管理器（CLI 接线面）
class AttestationManager:
    """证明记录的写路径（受开关 ``supply.attestation.enabled`` 门控，默认关闭）。

    读路径（``status / list / report_payload``）永远可用：§9 rollback_rule 要求
    「关闭 → 已存证明保留只读」。
    """

    def __init__(self, store: Store, *, config: ClientConfig | None = None,
                 counter: Callable[[str, str], int | None] | None = None,
                 node_id: str | None = None, now=utcnow,
                 ttl_overrides: dict[str, int] | None = None) -> None:
        self.store = store
        self.config = config
        self.counter = counter
        self.node_id = node_id
        self._now = now
        self.ttl_overrides = dict(ttl_overrides or {})
        self.nonces = NonceLedger(store, now=now)

    # ── 开关
    def enabled(self) -> bool:
        return attestation_enabled(self.config)

    def require_enabled(self) -> None:
        if not self.enabled():
            raise FeatureDisabled(facts.ATTEST_FLAG)

    # ── 记录
    def get(self, upstream_id: str, model: str) -> AttestationRecord | None:
        record = load_record(self.store, upstream_id, model)
        if record is not None:
            refresh(record, now=self._now())
        return record

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for record in list_records(self.store):
            payload = record.describe(now=self._now())
            payload.update(service_gate(record, now=self._now()))
            items.append(payload)
        return items

    def status(self, upstream_id: str, model: str) -> dict[str, Any]:
        record = self.get(upstream_id, model)
        if record is None:
            return {"upstream_id": upstream_id, "model": model, "level": "L0",
                    "state": "declared", "known": False,
                    "trust": facts.ATTEST_TRUST, "verified": False,
                    "serviceable": False, "reason": "not_attested",
                    "enabled": self.enabled()}
        payload = record.describe(now=self._now())
        payload.update({"known": True, "enabled": self.enabled()})
        payload.update(service_gate(record, now=self._now()))
        return payload

    # ── 写路径
    def open_record(self, upstream_id: str, model: str) -> AttestationRecord:
        """登记 L0（declared）；已存在则返回（不降级、不重置）。"""
        self.require_enabled()
        existing = load_record(self.store, upstream_id, model)
        if existing is not None:
            return existing
        record = new_record(upstream_id, model, node_id=self.node_id, now=self._now())
        return save_record(self.store, record)

    def apply_evidence(self, upstream_id: str, model: str, evidence: Evidence
                       ) -> AttestationRecord:
        """消费证据：先查 nonce 重放，再按结果推进 / 失败降级 / 保持 unknown。"""
        self.require_enabled()
        validate_evidence(evidence)
        moment = self._now()
        self.nonces.consume(evidence.nonce, challenge_id=evidence.challenge_id, now=moment)
        record = load_record(self.store, upstream_id, model)
        if record is None:
            record = new_record(upstream_id, model, node_id=self.node_id, now=moment)
        refresh(record, now=moment)
        if record.state == "expired":
            reopen(record, now=moment, reason_code="ttl_expired")
        if evidence.result == "mismatch":
            if evidence.method == facts.ATTEST_LEVEL_METHOD["L3"]:
                fail(record, reason_code=evidence.reason_code or "tokenizer_mismatch",
                     downgrade_to="L2", evidence=evidence, now=moment)
            else:
                fail(record, reason_code=evidence.reason_code or "behavioral_mismatch",
                     evidence=evidence, now=moment)
        elif evidence.result == "error":
            fail(record, reason_code=evidence.reason_code or "probe_error",
                 evidence=evidence, now=moment)
        elif evidence.result == "unknown":
            _append_evidence(record, evidence)
            record.updated_at = iso(moment)
        else:                                              # ok / match
            advance(record, evidence.level, evidence=evidence, now=moment,
                    confidence=evidence.confidence, ttl_overrides=self.ttl_overrides)
        return save_record(self.store, record)

    def record_tokenizer_check(self, upstream_id: str, model: str, check: TokenizerCheck,
                               *, challenge_id: str, nonce: str,
                               response_hash: str | None = None,
                               tokens_out: int | None = None,
                               signature: str | None = None) -> dict[str, Any]:
        """L3 校验 → 证据 → 落记录（不联网；challenge 由服务端下发时由 Lead 接线）。"""
        evidence = check.to_evidence(challenge_id=challenge_id, model=model, nonce=nonce,
                                     response_hash=response_hash, tokens_out=tokens_out,
                                     signature=signature, created_at=self._now())
        record = self.apply_evidence(upstream_id, model, evidence)
        view = record.describe(now=self._now())
        view.update(service_gate(record, now=self._now()))
        return {"check": check.to_dict(), "record": view}

    # ── 上报载荷（§8.1，只构造不发送）
    def report_payload(self, record: AttestationRecord, evidence: Evidence) -> dict[str, Any]:
        """构造 ``POST /api/v1/node/upstream/attest`` 的本地载荷（**不发送**）。

        C10 通道未接线，本批只固定字段形状：challenge_id / model / tokens_in / tokens_out /
        usage / response_hash / nonce / signature（§8.1）+ attestation_id（§8.4 可选）。
        """
        payload = evidence.to_dict()
        for key in ("schema", "method", "level", "result", "confidence",
                    "reason_code", "created_at"):
            payload.pop(key, None)
        payload["attestation_id"] = record.attestation_id
        payload["upstream_id"] = record.upstream_id
        assert_no_body(payload)
        return payload
