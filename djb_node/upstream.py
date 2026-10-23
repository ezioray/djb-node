# SPDX-License-Identifier: Apache-2.0
"""上游接入 A1 / A2 / B（M15 §1 做、§3 Upstream、§8.2）。

三种接入方式（spec NodeUpstreamRequest.access_type）：

* **A1**：直填 Key 的直连上游（端点 = 厂商 API，凭据在本机 0600 存储）；
* **A2**：本机代理（OpenAI 兼容，端点通常是 127.0.0.1 上的本地服务）；
* **B**：企业网关（同一 OpenAI 兼容端点形态）。

硬规则：

* 申报只上行 access_type / base_url / models / protocol / credential_handle；**凭据永不上行**
  （H1 / INV-02），credential_handle 只是本地引用名。
* 统一端点形态：base_url + /chat/completions、/models（base 以 /v1 结尾时不再重复拼）；
* 逐模型开关与最低价（min_price）保存在本地；价格只作公告输入，不作为结算依据。
* A2 五字段 = endpoint / model / credential_ref / concurrency_limit / receipt_sample
  （M02 CR-6 裁决）；A2 prompt 模板五段 = 目标 / 约束 / 输入 / 输出 / 验收（03-facts §11）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

from . import facts
from .errors import UsageError
from .httpclient import HttpClient, join_url
from .secure import CredentialStore
from .store import Store, UpstreamRecord
from .util import iso, new_request_id, utcnow

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,128}$")
PRICE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
CREDENTIAL_REF_PREFIX = "upstream:"


@dataclass
class ModelEntry:
    id: str
    enabled: bool = True
    min_price: str | None = None
    min_price_unit: str = facts.DEFAULT_MIN_PRICE_UNIT

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "enabled": bool(self.enabled)}
        if self.min_price is not None:
            payload["min_price"] = self.min_price
            payload["min_price_unit"] = self.min_price_unit
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelEntry":
        price = raw.get("min_price")
        return cls(id=str(raw.get("id")),
                   enabled=bool(raw.get("enabled", True)),
                   min_price=str(price) if price is not None else None,
                   min_price_unit=str(raw.get("min_price_unit")
                                      or facts.DEFAULT_MIN_PRICE_UNIT))


def parse_models(raw: str | Iterable[str] | None) -> list[ModelEntry]:
    """接受 "a,b" 或 JSON 数组；id 只允许安全字符（防注入 / 防路径穿越）。"""
    if raw is None:
        raise UsageError("至少声明一个模型（--models）")
    if isinstance(raw, str):
        items = [item.strip() for item in raw.replace(",", " ").split() if item.strip()]
    else:
        items = [str(item).strip() for item in raw if str(item).strip()]
    if not items:
        raise UsageError("至少声明一个模型（--models）")
    out: list[ModelEntry] = []
    seen: set[str] = set()
    for item in items:
        if not MODEL_ID_RE.match(item):
            raise UsageError("模型 id 不合法（只允许字母数字 . _ : / @ + -）：" + item)
        if item in seen:
            continue
        seen.add(item)
        out.append(ModelEntry(id=item))
    return out


def validate_access_type(access_type: Any) -> str:
    value = str(access_type or "").strip()
    if value not in facts.ACCESS_TYPES:
        raise UsageError("接入方式必须是 A1 / A2 / B")
    return value


def validate_base_url(base_url: Any) -> str:
    """只允许 http(s)；拒绝 userinfo（凭据不许进 URL）与空白/控制字符。"""
    text = str(base_url or "").strip()
    if not text:
        raise UsageError("上游端点不能为空（--base-url）")
    if any(char in text for char in "\r\n\t") or " " in text:
        raise UsageError("上游端点不能包含空白或控制字符")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise UsageError("上游端点必须是 http(s)://...")
    if not parsed.hostname:
        raise UsageError("上游端点缺少主机名")
    if parsed.username or parsed.password:
        raise UsageError("上游端点不得携带用户名/口令（凭据只进本机安全存储）")
    if parsed.fragment:
        raise UsageError("上游端点不得带 fragment")
    return text.rstrip("/")


def credential_ref_for(upstream_id: str) -> str:
    """本地凭据句柄：只给服务端一个引用名，不含任何密钥字符。"""
    return CREDENTIAL_REF_PREFIX + upstream_id


def validate_min_price(value: Any) -> str:
    text = str(value).strip()
    if not PRICE_RE.match(text):
        raise UsageError("最低价必须是非负十进制字符串（如 0.35）")
    return text


# ─────────────────────────────────────────────────────────── 端点形态
def chat_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def models_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


# ─────────────────────────────────────────────────────────── A2 五字段 / prompt
def a2_five_fields(*, base_url: str, model: str, credential_ref: str,
                   concurrency_limit: int, receipt_sample: dict[str, Any] | None = None
                   ) -> dict[str, Any]:
    """M02 CR-6：A2 五字段（endpoint / model / credential_ref / concurrency_limit /
    receipt_sample）。这是给用户的接入检查单，不包含任何密钥。"""
    sample = receipt_sample or {
        "receipt_id": "<receipt_id>", "task_id": "<task_id>", "access": "A2",
        "usage": {"amount": "<token 数>", "unit": "token"},
        "price": {"amount": "<USD 分>", "unit": "usd_minor"},
        "sig_provider": "hmac-sha256:<64 hex>",
        "sig_consumer": "hmac-sha256:<64 hex>",
    }
    return {
        "endpoint": chat_endpoint(base_url),
        "model": model,
        "credential_ref": credential_ref,
        "concurrency_limit": int(concurrency_limit),
        "receipt_sample": sample,
    }


def a2_prompt(*, base_url: str, model: str, credential_ref: str,
              concurrency_limit: int, receipt_sample: dict[str, Any] | None = None
              ) -> dict[str, Any]:
    """A2 五字段 prompt 模板（目标 / 约束 / 输入 / 输出 / 验收）。

    给本机代理 / 自建 agent 的一份可执行说明：只描述端点形态与收据要求，
    不含凭据本体；「验收」给出可机检的判定条件。
    """
    fields = a2_five_fields(base_url=base_url, model=model,
                            credential_ref=credential_ref,
                            concurrency_limit=concurrency_limit,
                            receipt_sample=receipt_sample)
    sections = {
        "目标": "把本机模型以 OpenAI 兼容端点接入 DJB 联脑共享供给（A2 本机代理），"
                "只开放已声明的模型，按并发上限接收任务并产出双签收据。",
        "约束": "1) 端点形态 " + fields["endpoint"] + "；2) 只允许模型 "
                + fields["model"] + "；3) 凭据只以本机引用 " + fields["credential_ref"]
                + " 出现，禁止上行；4) 并发不超过 " + str(fields["concurrency_limit"])
                + "；5) 不落 prompt/response 正文；6) 不监听 0.0.0.0。",
        "输入": "OpenAI 兼容 chat.completions 请求（model / messages / stream），"
                "来自 127.0.0.1:1717 的本机镜像端点。",
        "输出": "chat.completion（或 SSE 流）+ usage{prompt_tokens,completion_tokens,"
                "total_tokens}；任务完成后按 receipt_sample 产出双签收据。",
        "验收": "1) GET " + models_endpoint(base_url) + " 返回所声明模型；"
                "2) 一次非流式请求 200 且 usage 齐全；3) 一次流式请求以 data: [DONE] 结束；"
                "4) 收据 6 个规范字段与 hmac-sha256 双签校验通过；"
                "5) 端点不接受携带 userinfo 的 URL。",
    }
    text = "\n".join("【" + name + "】" + sections[name] for name in facts.A2_PROMPT_SECTIONS)
    return {"fields": fields, "sections": sections, "text": text}


# ─────────────────────────────────────────────────────────── 探测（本地发起）
@dataclass
class ProbeResult:
    reachable: bool
    latency_ms: int
    error_class: str | None
    model_checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"reachable": self.reachable, "latency_ms": self.latency_ms,
                "error_class": self.error_class, "model_checks": list(self.model_checks)}


def probe_upstream(base_url: str, models: list[str], *, credential: str | None,
                   http: HttpClient | None = None, timeout: float = 5.0) -> ProbeResult:
    """GET {base}/models：只取「可达 + 延迟 + 是否声明」，不读上游正文。"""
    client = http or HttpClient()
    started = utcnow()
    headers = {}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    try:
        response = client.request("GET", models_endpoint(base_url), headers=headers,
                                  timeout=timeout)
    except Exception as exc:
        elapsed = int((utcnow() - started).total_seconds() * 1000)
        return ProbeResult(False, elapsed, _error_class(exc), [])
    elapsed = int((utcnow() - started).total_seconds() * 1000)
    if response.status == 401 or response.status == 403:
        return ProbeResult(False, elapsed, "unauthorized", [])
    if response.status >= 400:
        return ProbeResult(False, elapsed, "http_" + str(response.status), [])
    payload = response.json()
    listed: set[str] = set()
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        for item in payload["data"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                listed.add(item["id"])
    checks = [{"model": model, "ok": (not listed) or (model in listed),
               "error_class": None if (not listed) or (model in listed)
               else "model_not_listed"} for model in models]
    return ProbeResult(True, elapsed, None, checks)


def _error_class(exc: BaseException) -> str:
    from .errors import NetworkError
    if isinstance(exc, NetworkError):
        return str(exc.reason or "unreachable")
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    return "unreachable"


# ─────────────────────────────────────────────────────────── 管理
class UpstreamManager:
    def __init__(self, store: Store, credentials: CredentialStore, *,
                 http: HttpClient | None = None) -> None:
        self.store = store
        self.credentials = credentials
        self.http = http or HttpClient()

    def add(self, *, access_type: str, base_url: str, models: str | Iterable[str],
            secret: str | None = None, protocol: str = facts.DEFAULT_UPSTREAM_PROTOCOL,
            upstream_id: str | None = None) -> UpstreamRecord:
        access = validate_access_type(access_type)
        url = validate_base_url(base_url)
        entries = [entry.to_dict() for entry in parse_models(models)]
        identifier = upstream_id or new_request_id()
        if secret:
            self.credentials.set_upstream_secret(identifier, secret)
        ref = credential_ref_for(identifier) if (secret or access == "A2") else None
        return self.store.add_upstream(
            upstream_id=identifier, access_type=access, base_url=url,
            models=entries, credential_ref=ref, protocol=protocol)

    def list(self) -> list[UpstreamRecord]:
        return self.store.upstreams()

    def set_model_enabled(self, upstream_id: str, model_id: str, enabled: bool) -> UpstreamRecord:
        return self.store.set_model_enabled(upstream_id, model_id, enabled)

    def set_model_price(self, upstream_id: str, model_id: str, price: str) -> UpstreamRecord:
        return self.store.set_model_price(upstream_id, model_id, validate_min_price(price))

    def remove(self, upstream_id: str) -> bool:
        self.credentials.delete_upstream_secret(upstream_id)
        return self.store.delete_upstream(upstream_id)

    def probe(self, upstream_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
        record = self.store.upstream(upstream_id)
        secret = self.credentials.upstream_secret(upstream_id)
        result = probe_upstream(record.base_url, record.model_ids(),
                                credential=secret, http=self.http, timeout=timeout)
        state = "verified" if result.reachable else "rejected"
        self.store.update_upstream(upstream_id, state=state,
                                   last_probe_at=iso(utcnow()))
        payload = result.to_dict()
        payload.update({"upstream_id": upstream_id, "checked_at": iso(utcnow())})
        return payload

    # ── 公告 / 目录 / 路由
    def announce_capacity(self, profile: Any) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        enabled_models = [(record, item) for record in self.list() if record.enabled
                          for item in record.models if item.get("enabled", True)]
        per_model_qps = max(0.1, round(max(1, int(profile.concurrent))
                                       / max(1, len(enabled_models)), 2))
        for record, item in enabled_models:
            entries.append({
                "kind": facts.TOKEN_RELAY_KIND,
                "access": record.access_type,
                "model": str(item.get("id")),
                "max_qps": per_model_qps,
                "ask_price": str(item.get("min_price") or "0"),
                "unit": str(item.get("min_price_unit") or facts.DEFAULT_MIN_PRICE_UNIT),
                "resource_level": str(profile.preset),
            })
        return entries

    def catalog(self, *, price_version: str = "local") -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for record in self.list():
            if not record.enabled:
                continue
            for item in record.models:
                enabled = bool(item.get("enabled", True))
                price = item.get("min_price")
                entries.append({
                    "id": str(item.get("id")),
                    "object": "model",
                    "created": 0,
                    "owned_by": "djb-node",
                    "access": record.access_type,
                    "upstream_id": record.id,
                    "enabled": enabled,
                    "available": enabled and record.state in ("declared", "probing", "verified"),
                    "price_version": price_version,
                    "price": ({"amount": str(price),
                               "unit": str(item.get("min_price_unit")
                                           or facts.DEFAULT_MIN_PRICE_UNIT)}
                              if price is not None else None),
                })
        return entries

    def route(self, model: str) -> tuple[UpstreamRecord, dict[str, Any]] | None:
        for record in self.list():
            if not record.enabled:
                continue
            for item in record.models:
                if str(item.get("id")) == model and item.get("enabled", True):
                    return record, dict(item)
        return None

    def declare_payload(self, record: UpstreamRecord) -> dict[str, Any]:
        """M02 /node/upstream 请求体：只有引用，没有密钥。"""
        return {
            "access_type": record.access_type,
            "base_url": record.base_url,
            "models": record.model_ids(),
            "protocol": record.protocol,
            **({"credential_handle": record.credential_ref}
               if record.credential_ref else {}),
        }
