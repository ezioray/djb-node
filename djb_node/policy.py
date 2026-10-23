# SPDX-License-Identifier: Apache-2.0
"""策略快照的解析、校验与新鲜度（M15 §3 / INV-06）。

INV-06 要求「策略必须验签且未过期；过期 -> 保守模式；不得内置服务端配额默认值」。

S1 的现实约束（见 README-CLIENT CR-M15-05）：

* spec 的 NodePolicy 只有 digest，**没有签名与 expires_at**；因此本客户端做
  「digest 形状 + 协议版本 + 新鲜度」三道校验，verify_state 记为 digest_only；
  服务端补签名后只需把状态升级成 verified，调用方不变。
* 新鲜度：优先 payload.expires_at；否则 issued_at + settings.policy_ttl_s；
  再否则 issued_at + 本地常量 POLICY_DEFAULT_TTL_S（明确登记为 CR，不冒充服务端口径）。
* 配额只存服务端下发的结构，不写入任何本地默认数值（quota.value 缺省 = 不限额）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from . import facts, protocol
from .errors import PolicyError
from .store import PolicyRecord
from .util import iso, parse_iso, utcnow

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_QUOTAS = 64
MAX_DIRECTIVES = 64


def parse_policy(payload: Any, *, now: datetime | None = None,
                 ttl_default_s: int = facts.POLICY_DEFAULT_TTL_S) -> PolicyRecord:
    """把 NodePolicy 响应转成 PolicyRecord；不合法 -> PolicyError（调用方转保守模式）。"""
    if not isinstance(payload, dict):
        raise PolicyError("策略响应不是 JSON 对象")
    moment = now or utcnow()
    version = payload.get("policy_version")
    protocol_version = payload.get("protocol_version")
    issued_raw = payload.get("issued_at")
    digest = payload.get("digest")
    if not isinstance(version, str) or not version.strip():
        raise PolicyError("策略缺 policy_version（spec NodePolicy.required）")
    if not isinstance(protocol_version, str) or not version_tuple_ok(protocol_version):
        raise PolicyError("策略缺合法 protocol_version")
    if not protocol.is_protocol_compatible(protocol_version):
        raise PolicyError("策略协议版本与服务端不兼容：" + str(protocol_version))
    if not isinstance(digest, str) or not DIGEST_RE.match(digest.strip()):
        raise PolicyError("策略 digest 缺失或格式非法（无法校验完整性）")
    issued = parse_iso(issued_raw)
    if issued is None:
        raise PolicyError("策略 issued_at 缺失或不是 RFC 3339")
    if issued > moment + timedelta(seconds=facts.POLICY_MAX_CLOCK_SKEW_S):
        raise PolicyError("策略 issued_at 在未来（时钟偏差超过 "
                          + str(facts.POLICY_MAX_CLOCK_SKEW_S) + " 秒）")
    quotas = _parse_quotas(payload.get("quotas"))
    directives = _parse_directives(payload.get("directives"))
    settings = payload.get("settings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    expires = parse_iso(payload.get("expires_at"))
    if expires is None:
        ttl = settings.get("policy_ttl_s")
        if isinstance(ttl, int) and not isinstance(ttl, bool) and 1 <= ttl <= 30 * 86400:
            expires = issued + timedelta(seconds=ttl)
        else:
            expires = issued + timedelta(seconds=int(ttl_default_s))
    if expires <= issued:
        raise PolicyError("策略 expires_at 不晚于 issued_at")
    return PolicyRecord(
        policy_version=version.strip(), protocol_version=protocol_version,
        digest=digest.strip(), issued_at=iso(issued), expires_at=iso(expires),
        quotas=quotas, directives=directives, settings=settings,
        verify_state="digest_only", fetched_at=iso(moment))


def version_tuple_ok(text: str) -> bool:
    return protocol.version_tuple(text) is not None


def _parse_quotas(raw: Any) -> list[dict[str, Any]]:
    quotas: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return quotas
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        window = entry.get("window_seconds")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(window, int) or isinstance(window, bool) or window < 1:
            continue
        value = entry.get("value")
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value < 0):
            continue
        quotas.append({"name": name.strip(), "window_seconds": int(window),
                       "value": int(value) if value is not None else None})
        if len(quotas) >= MAX_QUOTAS:
            break
    return quotas


def _parse_directives(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = [str(item) for item in raw if isinstance(item, str) and item.strip()]
    return out[:MAX_DIRECTIVES]


def verify(record: PolicyRecord, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """返回 (可用, 原因)；不可用即保守模式（不接新单，但继续心跳与上报）。"""
    moment = now or utcnow()
    if record.verify_state not in ("digest_only", "verified"):
        return False, "unverified"
    if not DIGEST_RE.match(str(record.digest)):
        return False, "bad_digest"
    issued = parse_iso(record.issued_at)
    if issued is None:
        return False, "bad_issued_at"
    if issued > moment + timedelta(seconds=facts.POLICY_MAX_CLOCK_SKEW_S):
        return False, "issued_in_future"
    run_until = parse_iso(record.expires_at)
    if run_until is None:
        return False, "missing_expiry"
    if moment >= run_until:
        return False, "expired"
    if not protocol.is_protocol_compatible(record.protocol_version):
        return False, "protocol_incompatible"
    return True, None


def directives_pause(record: PolicyRecord) -> tuple[bool, str | None]:
    """服务端指令（未知指令必须忽略）：pause_accepting / upgrade_required / retire。"""
    known = {"pause_accepting": "服务端指令暂停接单", "upgrade_required": "服务端要求升级客户端"}
    for directive in record.directives:
        if directive in known:
            return True, known[directive]
    return False, None


def remaining_seconds(record: PolicyRecord, *, now: datetime | None = None) -> int:
    moment = now or utcnow()
    expiry = parse_iso(record.expires_at)
    if expiry is None:
        return 0
    return max(0, int((expiry - moment).total_seconds()))


def summary(record: PolicyRecord | None, *, now: datetime | None = None) -> dict[str, Any]:
    if record is None:
        return {"present": False, "usable": False, "reason": "missing"}
    ok, reason = verify(record, now=now)
    return {
        "present": True,
        "policy_version": record.policy_version,
        "protocol_version": record.protocol_version,
        "digest": record.digest[:12] + "…",
        "verify_state": record.verify_state,
        "issued_at": record.issued_at,
        "expires_at": record.expires_at,
        "remaining_seconds": remaining_seconds(record, now=now),
        "quotas": len(record.quotas),
        "directives": list(record.directives),
        "usable": ok,
        "reason": reason,
    }
