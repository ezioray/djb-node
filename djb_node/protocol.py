# SPDX-License-Identifier: Apache-2.0
"""协议常量与 tolerant reader（契约见 `openapi.yaml` 的 `info.version`；§6 / 04-api-contracts）。

头名、路径、能力集合只在这里出现一次；解析一律容忍未知字段 / 未知枚举
（spec「客户端必须容忍未知字段」，旧网关仍可用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import facts

# ─────────────────────────────────────────────────────────── 头名（spec parameters）
NODE_KEY_HEADER = "X-Client-Key"
PROTOCOL_HEADER = "DJB-Protocol-Version"
CAPABILITIES_HEADER = "DJB-Capabilities"
IDEMPOTENCY_HEADER = "X-DJB-Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
ADMIN_STEP_UP_HEADER = "X-Admin-Step-Up"
IF_NONE_MATCH = "If-None-Match"

# ─────────────────────────────────────────────────────────── 路径（只读契约，不新造）
PATHS: dict[str, str] = {
    "register": "/api/v1/node/register",
    "pair": "/api/v1/node/pair",
    "binding": "/api/v1/node/binding",
    "heartbeat": "/api/v1/node/heartbeat",
    "upstream": "/api/v1/node/upstream",
    "upstream_probe": "/api/v1/node/upstream/probe",
    "policy": "/api/v1/node/policy",
    "capabilities": "/api/v1/protocol/capabilities",
    "deprecations": "/api/v1/protocol/deprecations",
    "receipts": "/api/v1/node/receipts",
    # 供方区间报价（CR-20261106-OFFICIAL-SUPPLY-01）：云端早已存在、
    # 但客户端一直没有这条路径 ⇒ 客户端永远进不了候选集（F5 no_quote）。
    "quotes": "/api/v1/schedule/quotes",
    # 本机回环（非开放契约，M15 §4.1 / CR-1）
    "local_models": "/v1/models",
    "local_chat": "/v1/chat/completions",
    "local_health": "/v1/health",
}

#: 客户端能力（与服务端能力取交集；未知 capability 忽略不报错）
CLIENT_CAPABILITIES: tuple[str, ...] = (
    "chat_completions", "receipts", "policy", "capacity_announcement",
    "upstream_probe", "protocol_negotiation",
    # 报价能力位（CR-20261106-OFFICIAL-SUPPLY-01）**追加在末尾**，老服务端忽略未知位。
    # 服务端 `POST /api/v1/schedule/quotes` 不校验能力位（`app/scheduling/router.py`
    # 只看 X-Client-Key），所以即使服务端未公告 quotes、交集为空也不影响调用。
    "quotes")

#: 接入方式（spec NodeRegisterRequest.access_modes）
ACCESS_MODES: tuple[str, ...] = facts.ACCESS_TYPES


def version_tuple(text: Any) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(item) for item in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def is_protocol_compatible(server_version: Any, *, client_version: str = facts.PROTOCOL_VERSION,
                           min_supported: Any = None) -> bool:
    """MAJOR 必须相同；若服务端给了 min_supported，客户端版本不得低于它（N/N-1/N-2 口径）。"""
    server = version_tuple(server_version)
    client = version_tuple(client_version)
    if server is None or client is None:
        return False
    if server[0] != client[0]:
        return False
    minimum = version_tuple(min_supported) if min_supported else None
    if minimum is not None and client < minimum:
        return False
    return True


@dataclass
class CapabilityView:
    protocol_version: str | None = None
    min_supported_version: str | None = None
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    server_time: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def ids(self) -> list[str]:
        return [str(item.get("id")) for item in self.capabilities if item.get("id")]

    def supports(self, capability: str) -> bool:
        if self.enabled:
            return capability in self.enabled
        return capability in self.ids()


def parse_capabilities(payload: Any) -> CapabilityView:
    """tolerant reader：不合法条目跳过，不因未知字段 / 未知状态失败。"""
    view = CapabilityView()
    if not isinstance(payload, dict):
        return view
    view.raw = dict(payload)
    version = payload.get("protocol_version")
    view.protocol_version = version if isinstance(version, str) else None
    minimum = payload.get("min_supported_version")
    view.min_supported_version = minimum if isinstance(minimum, str) else None
    server_time = payload.get("server_time")
    view.server_time = server_time if isinstance(server_time, str) else None
    entries = payload.get("capabilities")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            if not isinstance(identifier, str) or not identifier:
                continue
            item = {"id": identifier, "version": str(entry.get("version") or ""),
                    "status": entry.get("status") if isinstance(entry.get("status"), str)
                    else "stable"}
            view.capabilities.append(item)
    enabled = payload.get("enabled")
    if isinstance(enabled, list):
        view.enabled = [str(item) for item in enabled if isinstance(item, str)]
    return view


def parse_deprecations(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        sunset = entry.get("sunset_at")
        if not isinstance(path, str) or not isinstance(sunset, str):
            continue
        out.append({
            "path": path,
            "method": str(entry.get("method") or "").upper(),
            "deprecated_at": entry.get("deprecated_at"),
            "sunset_at": sunset,
            "replacement": entry.get("replacement"),
            "supported_majors": [str(item) for item in (entry.get("supported_majors") or [])
                                 if isinstance(item, (str, int))],
        })
    return out


def build_headers(*, client_key: str | None = None,
                  protocol_version: str = facts.PROTOCOL_VERSION,
                  capabilities: tuple[str, ...] | list[str] | None = None,
                  request_id: str | None = None,
                  idempotency_key: str | None = None,
                  extra: dict[str, str] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if client_key:
        headers[NODE_KEY_HEADER] = client_key
    if protocol_version:
        headers[PROTOCOL_HEADER] = protocol_version
    if capabilities:
        headers[CAPABILITIES_HEADER] = ",".join(capabilities)
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    if idempotency_key:
        headers[IDEMPOTENCY_HEADER] = idempotency_key
    if extra:
        headers.update({key: value for key, value in extra.items() if value})
    return headers
