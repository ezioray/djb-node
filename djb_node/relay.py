# SPDX-License-Identifier: Apache-2.0
"""第三方中转站接入（M17 §4 / C11 客户端侧）。

对应规格：``product/v4/modules/M17-supply-integrity.md`` §4.1–§4.4、§8（接口只加不改）、
§9（开关默认关闭）。

设计边界（硬约束）：

* **不改 upstreams 表**：relay 配置与别名只落本机 kv（``relay:<upstream_id>`` JSON）。
* **不改 cli.py / server.py / tokens.py**：本模块只给 Lead 的 CLI 接线提供函数面。
* **凭据**：只经既有 :class:`djb_node.secure.CredentialStore`（``upstream:<id>`` 引用）；
  key 只从参数 / stdin 进入 ``set_upstream_secret``，**禁止**出现在 URL query / userinfo、
  日志、异常文案、诊断包。本模块所有错误文案只提「参数名 / 头名」，不回显值。
* **未映射不公告、不接单**（§4.4.3）：``announceable_models`` / ``is_serviceable`` 只看
  ``state == mapped`` 的模型。
* **只加不改**：不新增契约端点、不改既有 A1/A2/B 语义；L0–L5 探针上报走 §8.1 的既有路径
  （由 Lead 接线，本批不调服务端）。

数据形状（kv ``relay:<upstream_id>``）::

    {"schema": 1, "upstream_id": "...", "kind": "third_party_relay",
     "base_url": "https://relay.example.com/v1",
     "auth_scheme": "bearer", "header_name": null, "auth_prefix": null,
     "extra_headers": {"x-title": "djb-node"},
     "timeouts": {"connect": 5.0, "first_byte": 30.0, "total": 120.0},
     "tls": {"verify": true, "pin_sha256": null},
     "models": [{"id": "gpt-4o", "canonical": "openai/gpt-4o", "state": "mapped",
                 "source": "auto", "candidates": [], "enabled": true}],
     "aliases": {"claude-3-7": "anthropic/claude-3-7-sonnet"},
     "rejected": [{"id": "gpt-4o", "reason": "duplicate_id"}],
     "last_error": null,
     "discovered_at": "...", "updated_at": "..."}
"""
from __future__ import annotations

import hashlib
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlparse

from . import facts, upstream
from .config import ClientConfig, feature_flag, flag_spec
from .errors import DjbError, FeatureDisabled, UsageError, VerifyError
from .httpclient import HttpClient, HttpResponse
from .secure import CredentialStore
from .store import Store
from .util import iso, utcnow

HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]{1,64}$")
PIN_RE = re.compile(r"^[0-9a-f]{64}$")

def relay_enabled(config: ClientConfig | None) -> bool:
    """开关 ``client.relay.enabled``（默认关闭；统一走 config.feature_flag）。"""
    return bool(config is not None and feature_flag(config, facts.RELAY_FLAG))



class RelayError(UsageError):
    """relay 配置 / 发现语义错误（用法层，不带凭据）。"""


class TLSVerifyError(VerifyError):
    """TLS 证书 / 指纹校验失败（fail-closed，不静默降级）。"""


# ─────────────────────────────────────────────────────────── 配置对象
@dataclass
class RelayProfile:
    """第三方中转站配置（不含任何密钥本体）。"""

    upstream_id: str
    base_url: str
    kind: str = facts.RELAY_KIND
    auth_scheme: str = "bearer"
    header_name: str | None = None
    auth_prefix: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeouts: dict[str, float] = field(
        default_factory=lambda: dict(facts.RELAY_TIMEOUT_DEFAULTS))
    tls_verify: bool = True
    tls_pin_sha256: str | None = None
    models: list[dict[str, Any]] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None
    discovered_at: str | None = None
    updated_at: str | None = None
    schema: int = facts.RELAY_PROFILE_SCHEMA

    # ── 派生
    @property
    def total_timeout_s(self) -> float:
        return float(self.timeouts.get("total", facts.RELAY_TIMEOUT_DEFAULTS["total"]))

    @property
    def auth_header_name(self) -> str:
        if self.auth_scheme == "custom":
            return str(self.header_name or "")
        return facts.RELAY_AUTH_HEADER.get(self.auth_scheme, "Authorization")

    @property
    def tls_mode(self) -> str:
        if not self.tls_verify and self.tls_pin_sha256:
            return "pin_only"
        if self.tls_verify and self.tls_pin_sha256:
            return "verify_and_pin"
        return "verify"

    # ── 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": int(self.schema),
            "upstream_id": self.upstream_id,
            "kind": self.kind,
            "base_url": self.base_url,
            "auth_scheme": self.auth_scheme,
            "header_name": self.header_name,
            "auth_prefix": self.auth_prefix,
            "extra_headers": dict(sorted(self.extra_headers.items())),
            "timeouts": {key: float(value) for key, value in self.timeouts.items()},
            "tls": {"verify": bool(self.tls_verify), "pin_sha256": self.tls_pin_sha256},
            "models": [dict(item) for item in self.models],
            "aliases": dict(sorted(self.aliases.items())),
            "rejected": [dict(item) for item in self.rejected],
            "last_error": self.last_error,
            "discovered_at": self.discovered_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RelayProfile":
        tls = raw.get("tls") if isinstance(raw.get("tls"), dict) else {}
        timeouts = raw.get("timeouts") if isinstance(raw.get("timeouts"), dict) else {}
        merged_timeouts = dict(facts.RELAY_TIMEOUT_DEFAULTS)
        for key, value in timeouts.items():
            try:
                merged_timeouts[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return cls(
            upstream_id=str(raw.get("upstream_id") or ""),
            base_url=str(raw.get("base_url") or ""),
            kind=str(raw.get("kind") or facts.RELAY_KIND),
            auth_scheme=str(raw.get("auth_scheme") or "bearer"),
            header_name=(str(raw["header_name"]) if raw.get("header_name") else None),
            auth_prefix=(str(raw["auth_prefix"]) if raw.get("auth_prefix") else None),
            extra_headers={str(key).lower(): str(value)
                           for key, value in (raw.get("extra_headers") or {}).items()},
            timeouts=merged_timeouts,
            tls_verify=bool(tls.get("verify", True)),
            tls_pin_sha256=(str(tls["pin_sha256"]) if tls.get("pin_sha256") else None),
            models=[dict(item) for item in (raw.get("models") or [])
                    if isinstance(item, dict)],
            aliases={str(key): str(value)
                     for key, value in (raw.get("aliases") or {}).items()},
            rejected=[dict(item) for item in (raw.get("rejected") or [])
                      if isinstance(item, dict)],
            last_error=(str(raw["last_error"]) if raw.get("last_error") else None),
            discovered_at=raw.get("discovered_at"),
            updated_at=raw.get("updated_at"),
            schema=int(raw.get("schema") or facts.RELAY_PROFILE_SCHEMA))

    # ── 脱敏视图（给 status / diagnose / 证据文件）
    def describe(self) -> dict[str, Any]:
        return {
            "upstream_id": self.upstream_id,
            "kind": self.kind,
            "base_url": self.base_url,
            "auth_scheme": self.auth_scheme,
            "auth_header": self.auth_header_name,
            "extra_headers": sorted(self.extra_headers),
            "timeouts": dict(self.timeouts),
            "tls_mode": self.tls_mode,
            "tls_pin_set": bool(self.tls_pin_sha256),
            "credential_ref": upstream.credential_ref_for(self.upstream_id),
            "models_mapped": [item["id"] for item in self.models
                              if item.get("state") == "mapped"],
            "models_unmapped": [item["id"] for item in self.models
                                if item.get("state") == "unmapped"],
            "aliases": dict(sorted(self.aliases.items())),
            "discovered_at": self.discovered_at,
        }


# ─────────────────────────────────────────────────────────── 校验（fail-closed）
def validate_relay_base_url(base_url: Any, *, secret: str | None = None) -> str:
    """端点校验：复用 A1/A2/B 规则 + relay 专属（key 不进 URL、默认 https）。

    错误文案只提参数名，**不回显 URL 或密钥**。
    """
    try:
        url = upstream.validate_base_url(base_url)      # http(s) / 无 userinfo / 无 fragment
    except UsageError as exc:
        raise RelayError(exc.message) from None
    parsed = urlparse(url)
    if parsed.scheme == "http" and (parsed.hostname or "") not in facts.RELAY_LOCAL_HOSTS:
        raise RelayError("第三方中转站必须使用 https://（仅回环允许 http://；"
                         "自签证书需显式 pin 指纹）")
    if parsed.query:
        for name, value in parse_qsl(parsed.query, keep_blank_values=True):
            if name.strip().lower() in facts.RELAY_SECRET_QUERY_PARAMS:
                raise RelayError("凭据不得出现在 URL query（参数名：" + name.strip()[:32]
                                 + "）；key 只从 stdin / 本机安全存储读取")
            if secret and secret and secret in (value or ""):
                raise RelayError("凭据不得出现在 URL query；key 只从 stdin / 本机安全存储读取")
    if secret and secret in url:
        raise RelayError("凭据不得出现在端点 URL；key 只从 stdin / 本机安全存储读取")
    return url


def normalize_tls_pin(raw: Any) -> str:
    """接受 64 位十六进制，或 AA:BB:… 冒号分隔形式；统一小写无分隔。"""
    text = str(raw or "").strip().replace(":", "").replace(" ", "").lower()
    if not PIN_RE.match(text):
        raise RelayError("TLS pin 指纹必须是 sha256（64 位十六进制，可带冒号分隔）")
    return text


def _validate_header_name(name: Any, *, role: str) -> str:
    text = str(name or "").strip()
    if not HEADER_NAME_RE.match(text):
        raise RelayError(role + "不是合法 HTTP 头名（只允许 RFC 7230 token 字符）")
    if text.lower() in facts.RELAY_FORBIDDEN_HEADERS:
        raise RelayError(role + " 不允许覆盖传输/认证头：" + text)
    return text


def _validate_extra_headers(raw: Any) -> dict[str, str]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise RelayError("extra_headers 必须是对象（头名 → 值）")
    if len(raw) > facts.RELAY_MAX_EXTRA_HEADERS:
        raise RelayError("extra_headers 超过上限 " + str(facts.RELAY_MAX_EXTRA_HEADERS))
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = _validate_header_name(key, role="extra_headers 头名 ")
        lowered = name.lower()
        if lowered in facts.RELAY_FORBIDDEN_HEADERS:
            raise RelayError("extra_headers 不允许覆盖传输/认证头：" + name)
        if lowered not in facts.RELAY_EXTRA_HEADER_ALLOWLIST:
            raise RelayError("extra_headers 只允许白名单头：" + name)
        text = str(value)
        if any(char in text for char in "\r\n"):
            raise RelayError("extra_headers 值不得包含换行：" + name)
        if len(text) > facts.RELAY_HEADER_VALUE_MAX_LEN:
            raise RelayError("extra_headers 值过长：" + name)
        out[lowered] = text
    return out


def _validate_timeouts(raw: Any) -> dict[str, float]:
    merged = dict(facts.RELAY_TIMEOUT_DEFAULTS)
    if raw not in (None, {}):
        if not isinstance(raw, dict):
            raise RelayError("timeouts 必须是对象（connect / first_byte / total）")
        unknown = sorted(set(str(key) for key in raw) - set(merged))
        if unknown:
            raise RelayError("timeouts 只支持 connect / first_byte / total，未知键："
                             + ", ".join(unknown))
        for key, value in raw.items():
            try:
                merged[str(key)] = float(value)
            except (TypeError, ValueError):
                raise RelayError("timeouts." + str(key) + " 必须是数字（秒）") from None
    for key in ("connect", "first_byte", "total"):
        value = merged[key]
        if value <= 0:
            raise RelayError("timeouts." + key + " 必须 > 0")
        if value > facts.RELAY_TIMEOUT_MAX_S:
            raise RelayError("timeouts." + key + " 不得超过 "
                             + str(int(facts.RELAY_TIMEOUT_MAX_S)) + " 秒")
    if not (merged["connect"] <= merged["first_byte"] <= merged["total"]):
        raise RelayError("timeouts 必须满足 connect ≤ first_byte ≤ total")
    return merged


def validate_relay_profile(profile: RelayProfile, *, credential: str | None = None
                           ) -> RelayProfile:
    """校验并就地规范化（fail-closed）。返回同一个 profile。"""
    if profile.kind != facts.RELAY_KIND:
        raise RelayError("upstream_kind 必须是 " + facts.RELAY_KIND)
    if not str(profile.upstream_id or "").strip():
        raise RelayError("relay 配置缺少 upstream_id")
    profile.base_url = validate_relay_base_url(profile.base_url, secret=credential)

    scheme = str(profile.auth_scheme or "").strip()
    if scheme not in facts.RELAY_AUTH_SCHEMES:
        raise RelayError("auth_scheme 必须是 bearer / x-api-key / api-key / custom")
    profile.auth_scheme = scheme
    if scheme == "custom":
        if not profile.header_name:
            raise RelayError("auth_scheme=custom 必须显式给 header_name")
        profile.header_name = _validate_header_name(profile.header_name,
                                                    role="自定义认证头名 ")
    else:
        default_header = facts.RELAY_AUTH_HEADER[scheme]
        if profile.header_name and profile.header_name.lower() != default_header.lower():
            raise RelayError("非 custom 认证方式不得改认证头名（" + scheme + " 固定用 "
                             + default_header + "）")
        profile.header_name = None
        if profile.auth_prefix and profile.auth_prefix != facts.RELAY_AUTH_PREFIX[scheme]:
            raise RelayError("非 custom 认证方式不得改前缀（" + scheme + " 固定）")
    if profile.auth_prefix is not None:
        if any(char in str(profile.auth_prefix) for char in "\r\n"):
            raise RelayError("auth_prefix 不得包含换行")
        profile.auth_prefix = str(profile.auth_prefix)

    profile.extra_headers = _validate_extra_headers(profile.extra_headers)
    if profile.auth_header_name.lower() in profile.extra_headers:
        raise RelayError("extra_headers 不得覆盖认证头：" + profile.auth_header_name)
    profile.timeouts = _validate_timeouts(profile.timeouts)

    if profile.tls_pin_sha256 is not None:
        profile.tls_pin_sha256 = normalize_tls_pin(profile.tls_pin_sha256)
    if not profile.tls_verify and not profile.tls_pin_sha256:
        raise RelayError("自签 / 免校验 TLS 必须显式 pin 证书指纹（sha256），"
                         "不允许静默跳过校验")

    for name in list(profile.aliases):
        _validate_model_id(name, role="别名来源模型 ")
        _validate_model_id(profile.aliases[name], role="别名目标模型 ")
    return profile


def _validate_model_id(value: Any, *, role: str = "模型 id ") -> str:
    text = str(value or "").strip()
    if not upstream.MODEL_ID_RE.match(text):
        raise RelayError(role + "不合法（只允许字母数字 . _ : / @ + -，≤128）")
    return text


# ─────────────────────────────────────────────────────────── 认证头
def auth_headers(profile: RelayProfile, credential: str | None) -> dict[str, str]:
    """按 auth_scheme 构造认证头；**返回值只进 HTTP 头，不进日志/异常**。"""
    if not credential:
        raise RelayError("缺少 relay 凭据：先 djb upstream add --key-stdin（凭据只进本机存储）")
    prefix = profile.auth_prefix
    if prefix is None:
        prefix = facts.RELAY_AUTH_PREFIX.get(profile.auth_scheme, "")
    return {profile.auth_header_name: prefix + credential}


def request_headers(profile: RelayProfile, credential: str | None) -> dict[str, str]:
    headers = dict(profile.extra_headers)
    headers.update(auth_headers(profile, credential))
    return headers


# ─────────────────────────────────────────────────────────── TLS pin
def pin_of_der(der: bytes) -> str:
    return hashlib.sha256(der or b"").hexdigest()


def check_peer_pin(sock: Any, expected_pin: str) -> str:
    """比对已建立连接的对端证书指纹；失败抛 :class:`TLSVerifyError`（fail-closed）。"""
    expected = normalize_tls_pin(expected_pin)
    try:
        der = sock.getpeercert(binary_form=True)
    except Exception:                                      # pragma: no cover - 传输异常
        raise TLSVerifyError("无法读取对端证书指纹：TLS pin 校验失败") from None
    if not der:
        raise TLSVerifyError("对端未提供证书：TLS pin 校验失败")
    actual = pin_of_der(der)
    if actual != expected:
        raise TLSVerifyError("TLS 证书指纹不匹配（pin 校验失败）：期望 "
                             + expected[:12] + "…，实际 " + actual[:12] + "…")
    return actual


def build_ssl_context(profile: RelayProfile) -> ssl.SSLContext | None:
    """按 profile 构造 SSLContext；默认（verify）返回 None = 交回 urllib 默认校验。"""
    if profile.tls_verify and not profile.tls_pin_sha256:
        return None
    context = ssl.create_default_context()
    if not profile.tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def preflight_tls_pin(profile: RelayProfile, *, timeout: float | None = None) -> str:
    """真实握手一次取证书指纹并比对（不改 httpclient.py 的 TLS 口径）。"""
    if not profile.tls_pin_sha256:
        raise TLSVerifyError("未配置 TLS pin，无需 pin 预检")
    parsed = urlparse(profile.base_url)
    host = parsed.hostname or ""
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    context = ssl.create_default_context()
    if not profile.tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout or profile.timeouts["connect"]
                                  ) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls_sock:
            return check_peer_pin(tls_sock, profile.tls_pin_sha256)


class PinnedHttpClient:
    """在首次请求前做一次 pin 预检，然后委托给标准 HttpClient。"""

    def __init__(self, profile: RelayProfile, *, timeout: float | None = None) -> None:
        self.profile = profile
        self._client = HttpClient(timeout=timeout or profile.total_timeout_s,
                                  ssl_context=build_ssl_context(profile))
        self._checked = False

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        if not self._checked:
            preflight_tls_pin(self.profile)
            self._checked = True
        return self._client.request(method, url, **kwargs)


def relay_http_client(profile: RelayProfile) -> Any:
    """带 pin 时用预检客户端，否则用标准客户端（不新增/不改 httpclient 接口）。"""
    if profile.tls_pin_sha256:
        return PinnedHttpClient(profile)
    return HttpClient(timeout=profile.total_timeout_s,
                      ssl_context=build_ssl_context(profile))


# ─────────────────────────────────────────────────────────── 模型发现
@dataclass
class DiscoveredModel:
    upstream_model_id: str
    canonical: str | None = None
    state: str = "unmapped"
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {"id": self.upstream_model_id, "state": self.state,
                   "canonical": self.canonical}
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        return payload


@dataclass
class DiscoveryResult:
    upstream_id: str
    base_url: str
    reachable: bool = False
    http_status: int | None = None
    latency_ms: int = 0
    error_class: str | None = None
    models: list[DiscoveredModel] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    registry_used: bool = False
    fetched_at: str | None = None

    # ── 便捷视图
    @property
    def mapped(self) -> list[DiscoveredModel]:
        return [item for item in self.models if item.state == "mapped"]

    @property
    def unmapped(self) -> list[DiscoveredModel]:
        return [item for item in self.models if item.state == "unmapped"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream_id": self.upstream_id,
            "base_url": self.base_url,
            "reachable": self.reachable,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "error_class": self.error_class,
            "registry_used": self.registry_used,
            "models": [item.to_dict() for item in self.models],
            "rejected": [dict(item) for item in self.rejected],
            "mapped_count": len(self.mapped),
            "unmapped_count": len(self.unmapped),
            "fetched_at": self.fetched_at,
        }


def _vendor_of(slug: str, vendors: set[str]) -> str | None:
    head = slug.split("/", 1)[0].strip().lower()
    return head if head in vendors else None


def _known_vendors(registry: Iterable[str] | None) -> set[str]:
    vendors = set(facts.RELAY_VENDOR_HINTS.values())
    for slug in registry or ():
        if "/" in str(slug):
            vendors.add(str(slug).split("/", 1)[0].lower())
    return vendors


def _hint_vendor(model_id: str) -> str | None:
    lowered = model_id.lower()
    best: tuple[int, str] | None = None
    for prefix, vendor in facts.RELAY_VENDOR_HINTS.items():
        if lowered == prefix or lowered.startswith(prefix + "-") or \
                lowered.startswith(prefix + ".") or lowered.startswith(prefix + ":"):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), vendor)
    return best[1] if best else None


def _candidates_for(model_id: str, registry: set[str] | None) -> list[str]:
    if not registry:
        return []
    lowered = model_id.lower()
    tail = lowered.split("/")[-1]
    hits: list[str] = []
    for slug in sorted(registry):
        slug_tail = slug.split("/")[-1].lower()
        if slug_tail == tail or slug_tail.startswith(tail) or tail.startswith(slug_tail):
            hits.append(slug)
    return hits[:facts.RELAY_CANONICAL_CANDIDATE_LIMIT]


def canonical_slug(raw_id: Any, *, registry: Iterable[str] | None = None
                   ) -> tuple[str | None, list[str]]:
    """把上游 id 归一成 canonical slug（§4.4.2）。

    返回 ``(canonical, candidates)``：能归一 → canonical；不能归一 → ``(None, 候选)``，
    由用户 ``djb upstream map`` 确认。``registry`` 为空时用本地 vendor 前缀知识兜底
    （离线可用的降级路径，仍标 auto，等 Lead 接入服务端 registry 后以 registry 为准）。
    """
    text = str(raw_id or "").strip()
    if not text or not upstream.MODEL_ID_RE.match(text):
        return None, []
    known: set[str] | None = ({str(item).strip() for item in registry} if registry else None)
    vendors = _known_vendors(known)
    if known is not None and text in known:
        return text, []
    if "/" in text:
        head, rest = text.split("/", 1)
        head_l, rest = head.strip().lower(), rest.strip()
        if rest and head_l in vendors:
            candidate = head_l + "/" + rest.lower()
            if known is None or candidate in known:
                return candidate, []
            return None, _candidates_for(text, known)
        return None, _candidates_for(text, known)
    vendor = _hint_vendor(text)
    if vendor:
        candidate = vendor + "/" + text.lower()
        if known is None or candidate in known:
            return candidate, []
    return None, _candidates_for(text, known)


def _rejected_entry(raw_id: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": str(raw_id)[:128], "reason": reason}
    payload.update(extra)
    return payload


def parse_models_payload(payload: Any, *, registry: Iterable[str] | None = None
                         ) -> tuple[list[DiscoveredModel], list[dict[str, Any]]]:
    """解析 ``GET /models`` 的 ``data[].id``；重复 / 冲突 / 非法条目拒绝并留原因。"""
    models: list[DiscoveredModel] = []
    rejected: list[dict[str, Any]] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RelayError("上游 /models 响应缺少 data 数组（tolerant reader 不猜）")
    seen_exact: set[str] = set()
    seen_norm: dict[str, str] = {}
    for item in payload["data"]:
        raw_id: Any = item.get("id") if isinstance(item, dict) else item
        if not isinstance(raw_id, str) or not raw_id.strip():
            rejected.append(_rejected_entry(str(raw_id or ""), "invalid_item"))
            continue
        text = raw_id.strip()
        if not upstream.MODEL_ID_RE.match(text):
            rejected.append(_rejected_entry(text, "invalid_id"))
            continue
        if text in seen_exact:
            rejected.append(_rejected_entry(text, "duplicate_id"))
            continue
        norm = text.casefold()
        if norm in seen_norm and seen_norm[norm] != text:
            rejected.append(_rejected_entry(text, "id_conflict",
                                           conflict_with=seen_norm[norm]))
            continue
        seen_exact.add(text)
        seen_norm.setdefault(norm, text)
        canonical, candidates = canonical_slug(text, registry=registry)
        models.append(DiscoveredModel(
            upstream_model_id=text, canonical=canonical,
            state="mapped" if canonical else "unmapped", candidates=candidates))
    return models, rejected


def discover_models(profile: RelayProfile, credential: str | None, *,
                    http: Any | None = None, registry: Iterable[str] | None = None,
                    now: datetime | None = None) -> DiscoveryResult:
    """``GET {base}/models`` 拉清单 + 归一（L1 客户端探针，§4.5）。

    * 401/403 → ``error_class=unauthorized``；4xx/5xx → ``http_<code>``；网络异常 → 粗粒度原因。
    * 不读上游正文、不落响应体；失败不抛（返回结果），配置错误才抛 :class:`RelayError`。
    """
    moment = now or utcnow()
    validate_relay_profile(profile, credential=credential)
    client = http if http is not None else relay_http_client(profile)
    result = DiscoveryResult(upstream_id=profile.upstream_id, base_url=profile.base_url,
                             registry_used=bool(registry),
                             fetched_at=iso(moment))
    url = upstream.models_endpoint(profile.base_url)
    started = time.monotonic()
    try:
        response = client.request("GET", url, headers=request_headers(profile, credential),
                                  timeout=profile.total_timeout_s)
    except DjbError as exc:
        result.error_class = str(exc.reason or "unreachable")
        result.latency_ms = 0
        return result
    except Exception:                                      # pragma: no cover - 兜底
        result.error_class = "unreachable"
        return result
    result.latency_ms = int(max(0.0, (time.monotonic() - started) * 1000))
    result.http_status = int(getattr(response, "status", 0) or 0)
    if result.http_status in (401, 403):
        result.error_class = "unauthorized"
        return result
    if result.http_status >= 400:
        result.error_class = "http_" + str(result.http_status)
        return result
    payload = response.json() if hasattr(response, "json") else None
    try:
        result.models, result.rejected = parse_models_payload(payload, registry=registry)
    except RelayError as exc:
        result.error_class = "invalid_payload"
        result.rejected.append({"id": "", "reason": str(exc.reason or "invalid_payload")})
        return result
    result.reachable = True
    return result


def apply_discovery(profile: RelayProfile, result: DiscoveryResult
                    ) -> RelayProfile:
    """把发现结果并入 profile（用户确认的映射优先，§4.4.5）。"""
    previous = {str(item.get("id")): item for item in profile.models}
    merged: list[dict[str, Any]] = []
    for discovered in result.models:
        prior = previous.get(discovered.upstream_model_id) or {}
        canonical: str | None = None
        source: str | None = None
        alias_target = profile.aliases.get(discovered.upstream_model_id)
        if alias_target:
            canonical, source = alias_target, "user"
        elif prior.get("state") == "mapped" and prior.get("canonical"):
            canonical = str(prior["canonical"])
            source = str(prior.get("source") or "auto")
        elif discovered.canonical:
            canonical, source = discovered.canonical, "auto"
        merged.append({
            "id": discovered.upstream_model_id,
            "canonical": canonical,
            "state": "mapped" if canonical else "unmapped",
            "source": source,
            "candidates": list(discovered.candidates),
            "enabled": bool(prior.get("enabled", True)),
        })
    profile.models = merged
    profile.rejected = [dict(item) for item in result.rejected]
    profile.last_error = result.error_class
    profile.discovered_at = result.fetched_at
    profile.updated_at = result.fetched_at or iso(utcnow())
    return profile


# ─────────────────────────────────────────────────────────── 别名 / 公告
def map_alias(profile: RelayProfile, from_model: Any, to_model: Any, *,
              replace: bool = False) -> RelayProfile:
    """本地别名映射（``djb upstream map --from X --to vendor/X``）。"""
    source = _validate_model_id(from_model, role="别名来源模型 ")
    target = _validate_model_id(to_model, role="别名目标模型 ")
    existing = profile.aliases.get(source)
    if existing and existing != target and not replace:
        raise RelayError("别名冲突：" + source + " 已映射到 " + existing
                         + "（确认后先 unmap 或用 replace）")
    profile.aliases[source] = target
    for item in profile.models:
        if str(item.get("id")) == source:
            item["canonical"] = target
            item["state"] = "mapped"
            item["source"] = "user"
            item["candidates"] = []
    profile.updated_at = iso(utcnow())
    return profile


def unmap_alias(profile: RelayProfile, from_model: Any) -> bool:
    source = _validate_model_id(from_model, role="别名来源模型 ")
    existed = profile.aliases.pop(source, None) is not None
    for item in profile.models:
        if str(item.get("id")) == source and item.get("source") == "user":
            item["canonical"] = None
            item["state"] = "unmapped"
            item["source"] = None
    profile.updated_at = iso(utcnow())
    return existed


def list_aliases(profile: RelayProfile) -> dict[str, str]:
    return dict(sorted(profile.aliases.items()))


def resolve_model(profile: RelayProfile, upstream_model_id: Any) -> str | None:
    """上游 id → canonical slug；未映射返回 None（调用方不得接单）。"""
    text = str(upstream_model_id or "").strip()
    if not text:
        return None
    if text in profile.aliases:
        return profile.aliases[text]
    for item in profile.models:
        if str(item.get("id")) == text and item.get("state") == "mapped":
            canonical = item.get("canonical")
            return str(canonical) if canonical else None
    return None


def announceable_models(profile: RelayProfile) -> list[str]:
    """可公告的 canonical 模型：只含 mapped + enabled（unmapped 不公告，§4.4.3）。"""
    out: list[str] = []
    for item in profile.models:
        if item.get("state") != "mapped" or not item.get("enabled", True):
            continue
        canonical = item.get("canonical")
        if canonical and str(canonical) not in out:
            out.append(str(canonical))
    return out


def is_serviceable(profile: RelayProfile, canonical: str) -> bool:
    """未映射 / 已关闭的模型不接单（§4.4.3）。"""
    return str(canonical) in announceable_models(profile)


def set_model_enabled(profile: RelayProfile, upstream_model_id: Any, enabled: bool
                      ) -> RelayProfile:
    text = _validate_model_id(upstream_model_id)
    for item in profile.models:
        if str(item.get("id")) == text:
            item["enabled"] = bool(enabled)
            profile.updated_at = iso(utcnow())
            return profile
    profile.models.append({"id": text, "canonical": profile.aliases.get(text),
                           "state": "mapped" if profile.aliases.get(text) else "unmapped",
                           "source": "user" if profile.aliases.get(text) else None,
                           "candidates": [], "enabled": bool(enabled)})
    profile.updated_at = iso(utcnow())
    return profile


# ─────────────────────────────────────────────────────────── kv 持久化
def relay_key(upstream_id: str) -> str:
    return facts.RELAY_PROFILE_KEY_PREFIX + str(upstream_id)


def save_profile(store: Store, profile: RelayProfile) -> RelayProfile:
    profile.updated_at = profile.updated_at or iso(utcnow())
    store.set_kv(relay_key(profile.upstream_id), profile.to_dict())
    index = [item for item in (store.get_kv(facts.RELAY_INDEX_KEY, default=[]) or [])
             if isinstance(item, str) and item != profile.upstream_id]
    index.append(profile.upstream_id)
    store.set_kv(facts.RELAY_INDEX_KEY, sorted(set(index)))
    return profile


def load_profile(store: Store, upstream_id: str) -> RelayProfile | None:
    raw = store.get_kv(relay_key(upstream_id), default=None)
    if not isinstance(raw, dict):
        return None
    return RelayProfile.from_dict(raw)


def require_profile(store: Store, upstream_id: str) -> RelayProfile:
    profile = load_profile(store, upstream_id)
    if profile is None:
        raise RelayError("没有 relay 配置：" + str(upstream_id) + "（先 djb upstream add --kind "
                         + facts.RELAY_KIND + "）")
    return profile


def list_profiles(store: Store) -> list[RelayProfile]:
    out: list[RelayProfile] = []
    for upstream_id in store.get_kv(facts.RELAY_INDEX_KEY, default=[]) or []:
        if not isinstance(upstream_id, str):
            continue
        profile = load_profile(store, upstream_id)
        if profile is not None:
            out.append(profile)
    return out


def delete_profile(store: Store, upstream_id: str) -> bool:
    existed = store.get_kv(relay_key(upstream_id), default=None) is not None
    store.delete_kv(relay_key(upstream_id))
    index = [item for item in (store.get_kv(facts.RELAY_INDEX_KEY, default=[]) or [])
             if isinstance(item, str) and item != upstream_id]
    store.set_kv(facts.RELAY_INDEX_KEY, sorted(set(index)))
    return existed


# ─────────────────────────────────────────────────────────── 管理器（CLI 接线面）
class RelayManager:
    """relay 接入的写路径（受开关 ``client.relay.enabled`` 门控，默认关闭）。

    读路径（``load / aliases / resolve / announceable / status``）永远可用：
    §9 rollback_rule 要求「关闭 → 已有配置只读、不删除」。
    """

    def __init__(self, store: Store, credentials: CredentialStore, *,
                 config: ClientConfig | None = None,
                 http: Any | None = None, now=utcnow) -> None:
        self.store = store
        self.credentials = credentials
        self.config = config
        self.http = http
        self._now = now

    # ── 开关（M17 §9：默认关闭；关闭 = 已有配置只读、不删除）
    def enabled(self) -> bool:
        return relay_enabled(self.config)

    def require_enabled(self) -> None:
        if not self.enabled():
            raise FeatureDisabled(facts.RELAY_FLAG)

    # ── 配置
    def add(self, *, base_url: str, upstream_id: str, auth_scheme: str = "bearer",
            header_name: str | None = None, auth_prefix: str | None = None,
            extra_headers: dict[str, str] | None = None,
            timeouts: dict[str, float] | None = None,
            tls_verify: bool = True, tls_pin: str | None = None,
            secret: str | None = None) -> RelayProfile:
        self.require_enabled()
        profile = RelayProfile(upstream_id=str(upstream_id), base_url=str(base_url),
                              auth_scheme=auth_scheme, header_name=header_name,
                              auth_prefix=auth_prefix,
                              extra_headers=dict(extra_headers or {}),
                              timeouts=dict(timeouts or facts.RELAY_TIMEOUT_DEFAULTS),
                              tls_verify=bool(tls_verify),
                              tls_pin_sha256=(normalize_tls_pin(tls_pin) if tls_pin else None))
        validate_relay_profile(profile, credential=secret)
        if secret:
            self.credentials.set_upstream_secret(profile.upstream_id, secret)
        profile.updated_at = iso(self._now())
        return save_profile(self.store, profile)

    def load(self, upstream_id: str) -> RelayProfile | None:
        return load_profile(self.store, upstream_id)

    def require(self, upstream_id: str) -> RelayProfile:
        return require_profile(self.store, upstream_id)

    def list(self) -> list[RelayProfile]:
        return list_profiles(self.store)

    def secret(self, upstream_id: str) -> str | None:
        """只返回给 HTTP 头构造用；调用方不得打印 / 落盘 / 进异常文案。"""
        return self.credentials.upstream_secret(upstream_id)

    def credential_ref(self, upstream_id: str) -> str:
        return upstream.credential_ref_for(upstream_id)

    def discover(self, upstream_id: str, *, registry: Iterable[str] | None = None
                 ) -> DiscoveryResult:
        self.require_enabled()
        profile = self.require(upstream_id)
        result = discover_models(profile, self.secret(upstream_id), http=self.http,
                                 registry=registry, now=self._now())
        apply_discovery(profile, result)
        save_profile(self.store, profile)
        return result

    def apply_server_models(self, upstream_id: str, result: DiscoveryResult) -> RelayProfile:
        self.require_enabled()
        profile = self.require(upstream_id)
        apply_discovery(profile, result)
        return save_profile(self.store, profile)

    def map(self, upstream_id: str, from_model: str, to_model: str, *,
            replace: bool = False) -> RelayProfile:
        self.require_enabled()
        profile = self.require(upstream_id)
        map_alias(profile, from_model, to_model, replace=replace)
        return save_profile(self.store, profile)

    def unmap(self, upstream_id: str, from_model: str) -> RelayProfile:
        self.require_enabled()
        profile = self.require(upstream_id)
        unmap_alias(profile, from_model)
        return save_profile(self.store, profile)

    def set_enabled(self, upstream_id: str, model_id: str, enabled: bool) -> RelayProfile:
        self.require_enabled()
        profile = self.require(upstream_id)
        set_model_enabled(profile, model_id, enabled)
        return save_profile(self.store, profile)

    def aliases(self, upstream_id: str) -> dict[str, str]:
        return list_aliases(self.require(upstream_id))

    def resolve(self, upstream_id: str, upstream_model_id: str) -> str | None:
        profile = self.load(upstream_id)
        return resolve_model(profile, upstream_model_id) if profile else None

    def announceable(self, upstream_id: str) -> list[str]:
        profile = self.load(upstream_id)
        return announceable_models(profile) if profile else []

    def serviceable(self, upstream_id: str, canonical: str) -> bool:
        profile = self.load(upstream_id)
        return is_serviceable(profile, canonical) if profile else False

    def remove(self, upstream_id: str) -> bool:
        self.require_enabled()
        self.credentials.delete_upstream_secret(upstream_id)
        return delete_profile(self.store, upstream_id)

    def status(self, upstream_id: str) -> dict[str, Any]:
        profile = self.require(upstream_id)
        payload = profile.describe()
        payload.update({
            "enabled": self.enabled(),
            "switch": {**flag_spec(facts.RELAY_FLAG), "name": facts.RELAY_FLAG,
                       "enabled": self.enabled()},
            "announceable": announceable_models(profile),
            "aliases": list_aliases(profile),
            "rejected": [dict(item) for item in profile.rejected],
            "last_error": profile.last_error,
        })
        return payload
