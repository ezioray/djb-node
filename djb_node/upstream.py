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
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from . import facts, probe as probe_mod
from .errors import UsageError
from .httpclient import HttpClient, join_url
from .secure import CredentialStore
from .store import Store, UpstreamRecord
from .util import iso, new_request_id, parse_iso, utcnow

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,128}$")
PRICE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
CREDENTIAL_REF_PREFIX = "upstream:"


@dataclass
class ModelEntry:
    id: str
    enabled: bool = True
    min_price: str | None = None
    min_price_unit: str = facts.DEFAULT_MIN_PRICE_UNIT
    # C7 探针结论（**可选 / 本地**）：spec 1.2.7 的 NodeProbeReport 没有这些字段，
    # 它们只写 model JSON、只用于本地公告门控，不上行。
    inference_ok: bool | None = None
    stream_ok: bool | None = None
    usage_ok: bool | None = None
    probe_version: str | None = None
    last_probe_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "enabled": bool(self.enabled)}
        if self.min_price is not None:
            payload["min_price"] = self.min_price
            payload["min_price_unit"] = self.min_price_unit
        # 只加不改：探针字段有值才落，旧库 / 旧配置的 JSON 形状保持不变。
        if self.inference_ok is not None:
            payload["inference_ok"] = bool(self.inference_ok)
        if self.stream_ok is not None:
            payload["stream_ok"] = bool(self.stream_ok)
        if self.usage_ok is not None:
            payload["usage_ok"] = bool(self.usage_ok)
        if self.probe_version is not None:
            payload["probe_version"] = self.probe_version
        if self.last_probe_at is not None:
            payload["last_probe_at"] = self.last_probe_at
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelEntry":
        price = raw.get("min_price")
        return cls(id=str(raw.get("id")),
                   enabled=bool(raw.get("enabled", True)),
                   min_price=str(price) if price is not None else None,
                   min_price_unit=str(raw.get("min_price_unit")
                                      or facts.DEFAULT_MIN_PRICE_UNIT),
                   inference_ok=_optional_bool(raw.get("inference_ok")),
                   stream_ok=_optional_bool(raw.get("stream_ok")),
                   usage_ok=_optional_bool(raw.get("usage_ok")),
                   probe_version=_optional_str(raw.get("probe_version")),
                   last_probe_at=_optional_str(raw.get("last_probe_at")))

    # ── 公告门控（C7）
    def verified(self) -> bool:
        """L2 通过（`inference_ok`）才算验证；缺省 = 从未验证。"""
        return self.inference_ok is True

    def probe_age_s(self, now: Any) -> float | None:
        stamp = parse_iso(self.last_probe_at)
        if stamp is None:
            return None
        return (now - stamp).total_seconds()

    def fresh(self, now: Any, *, ttl: int = facts.PROBE_FRESH_S) -> bool:
        """`last_probe_at` 是否新鲜；未来时间（时钟漂移）按新鲜处理。"""
        age = self.probe_age_s(now)
        if age is None:
            return False
        return age <= ttl

    def announceable(self, now: Any) -> bool:
        return self.enabled and self.verified() and self.fresh(now)


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


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


def validate_upstream_kind(value: Any) -> str | None:
    """M17 §4.1 ``upstream_kind``：合法四值原样返回；未知/空/非串 → ``None``。

    容忍口径与服务端一致：**未知值忽略为未标注，不报错**（新客户端打老服务端不应被拒）。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text in facts.UPSTREAM_KINDS else None


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
            upstream_kind: str | None = None,
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
            models=entries, credential_ref=ref, protocol=protocol,
            upstream_kind=validate_upstream_kind(upstream_kind))

    def list(self) -> list[UpstreamRecord]:
        return self.store.upstreams()

    def set_model_enabled(self, upstream_id: str, model_id: str, enabled: bool) -> UpstreamRecord:
        return self.store.set_model_enabled(upstream_id, model_id, enabled)

    def set_model_price(self, upstream_id: str, model_id: str, price: str) -> UpstreamRecord:
        return self.store.set_model_price(upstream_id, model_id, validate_min_price(price))

    def remove(self, upstream_id: str) -> bool:
        self.credentials.delete_upstream_secret(upstream_id)
        return self.store.delete_upstream(upstream_id)

    def probe(self, upstream_id: str, *, timeout: float | None = None,
              level: str | None = None, now: Any = None,
              deadline: Any = None, clock: Callable[[], Any] | None = None
              ) -> dict[str, Any]:
        """L1（/models）+ 逐模型 L2/L3，结论写回本地模型 JSON。

        返回值**只含 spec 1.2.7 `NodeProbeReport` 已定义的字段**
        （upstream_id / reachable / latency_ms / error_class /
        model_checks[model, ok, error_class] / checked_at），可直接上报。
        L2/L3 细节（inference_ok / stream_ok / usage_ok / token_usage /
        probe_version / last_probe_at）只落本地：`probe_details()` 可读。
        """
        moment = now or utcnow()
        seconds = float(timeout if timeout is not None else facts.PROBE_TIMEOUT_S)
        record = self.store.upstream(upstream_id)
        secret = self.credentials.upstream_secret(upstream_id)
        result = probe_upstream(record.base_url, record.model_ids(),
                                credential=secret, http=self.http, timeout=seconds)
        checks: list[dict[str, Any]] = [dict(item) for item in result.model_checks]
        outcomes: dict[str, probe_mod.ProbeOutcome] = {}
        if result.reachable:
            listed = [str(item.get("model")) for item in checks if item.get("ok")]
            outcomes = probe_mod.probe_models(
                record.base_url, listed, credential=secret,
                level=level or facts.PROBE_DEFAULT_LEVEL, timeout=seconds,
                http=self.http, deadline=deadline, clock=clock)
        # 逐模型合并：L2/L3 结论优先；未在清单 / 预算内没探到 -> 不 ok。
        for item in checks:
            model_id = str(item.get("model"))
            outcome = outcomes.get(model_id)
            if outcome is None:
                if item.get("ok"):
                    item["ok"] = False
                    item["error_class"] = facts.PROBE_NOT_PROBED
                continue
            item["ok"] = bool(outcome.inference_ok)
            if outcome.error_class:
                item["error_class"] = outcome.error_class
            else:
                item.pop("error_class", None)
        verified = [model_id for model_id, outcome in outcomes.items()
                    if outcome.inference_ok]
        state = "verified" if (result.reachable and verified) else "rejected"
        stamp = iso(moment)
        self.store.update_upstream(upstream_id, models=self._probe_models_payload(
            record, outcomes, stamp), state=state, last_probe_at=stamp)
        payload = {"upstream_id": upstream_id, "reachable": result.reachable,
                   "latency_ms": result.latency_ms,
                   "error_class": result.error_class,
                   "model_checks": checks, "checked_at": stamp}
        return payload

    @staticmethod
    def _probe_models_payload(record: UpstreamRecord,
                              outcomes: dict[str, probe_mod.ProbeOutcome],
                              stamp: str) -> list[dict[str, Any]]:
        """把探针结论写回模型 JSON 的五个可选字段（只加不改）。

        网络失败（没有 outcome）**不覆盖**上一次成功结论：凭证/新鲜度由
        `last_probe_at` 与上游 `state` 共同门控，避免抖动把最后一次好结果抹掉。
        """
        models: list[dict[str, Any]] = []
        for raw in record.models:
            item = dict(raw)
            outcome = outcomes.get(str(item.get("id")))
            if outcome is not None:
                item["inference_ok"] = bool(outcome.inference_ok)
                if outcome.stream_ok is not None:
                    item["stream_ok"] = bool(outcome.stream_ok)
                item["usage_ok"] = bool(outcome.usage_present)
                item["probe_version"] = facts.PROBE_VERSION
                item["last_probe_at"] = stamp
            models.append(item)
        return models

    def probe_details(self, upstream_id: str, *, now: Any = None) -> list[dict[str, Any]]:
        """本地 L2/L3 细节视图（**不上行**；spec 无这些字段）。"""
        record = self.store.upstream(upstream_id)
        moment = now or utcnow()
        details: list[dict[str, Any]] = []
        for raw in record.models:
            entry = ModelEntry.from_dict(raw)
            details.append({"model": entry.id, "enabled": entry.enabled,
                            "inference_ok": entry.inference_ok,
                            "stream_ok": entry.stream_ok,
                            "usage_ok": entry.usage_ok,
                            "probe_version": entry.probe_version,
                            "last_probe_at": entry.last_probe_at,
                            "fresh": entry.fresh(moment),
                            "announceable": entry.announceable(moment)})
        return details

    # ── 公告 / 目录 / 路由
    def announceable_models(self, now: Any = None) -> list[tuple[UpstreamRecord, ModelEntry]]:
        """可**真正**进容量的模型（>0 QPS）：模型级 L2 证据 + 新鲜 + 启用。

        门控口径（C7）：

        * 模型级：`inference_ok is True`（L2 通过）且 `last_probe_at` 在
          `facts.PROBE_FRESH_S`（默认 15 min）内；从未探过 / 过期 / L2 失败一律不算；
        * 上游级：`state=rejected` 一票否决；`declared` / `probing` / `verified`
          的差别只在聚合标签上（`djb upstream declare` 会把 state 写回 declared，
          所以判定以**模型级证据**为准，避免申报一次就把体检结论抹掉）；
        * `enabled=false` 的上游 / 模型不进容量。
        """
        moment = now or utcnow()
        out: list[tuple[UpstreamRecord, ModelEntry]] = []
        for record in self.list():
            if not record.enabled or record.state == "rejected":
                continue
            for raw in record.models:
                entry = ModelEntry.from_dict(raw)
                if entry.announceable(moment):
                    out.append((record, entry))
        return out

    def announce_capacity(self, profile: Any, *, now: Any = None,
                          relay: dict[str, list[dict[str, Any]]] | None = None
                          ) -> list[dict[str, Any]]:
        """容量公告（spec CapacityAnnouncement，`max_qps` 最小 0）。

        未验证 / 过期 / 被拒的模型仍以**同形状**条目出现，但 `max_qps=0`
        （= 不进容量，服务端调度拿不到吞吐）；只有 verified + 新鲜 + 启用的模型
        按并发分摊到正的 `max_qps`。这样旧调用方（`djb announce`）的清单结构与
        行序不变，只在数值上体现门控。

        ``relay``（C9 接线，可选）：``{upstream_id: [{"model", "canonical", "fresh"}]}``。
        由 :func:`djb_node.relay.capacity_targets` 生成，**只含已映射模型**：

        * 命中 relay 的上游不再出行级条目，改出映射后的模型条目（未映射不公告）；
        * ``fresh=False``（发现过期）仍出行，但 ``max_qps=0``（同 C7/C9 口径）；
        * 正的 QPS 在「探针 verified 模型 + relay 新鲜映射模型」之间统一分摊；
        * 附加 ``canonical`` 字段（spec ``additionalProperties: true``，只加不改）。
        """
        moment = now or utcnow()
        relay_targets = dict(relay or {})
        live = {(record.id, entry.id) for record, entry in self.announceable_models(moment)}
        live_relay = {(upstream_id, str(row.get("model")))
                      for upstream_id, rows in relay_targets.items()
                      for row in rows if row.get("fresh")}
        per_model_qps = max(0.1, round(max(1, int(profile.concurrent))
                                       / max(1, len(live) + len(live_relay)), 2))
        entries: list[dict[str, Any]] = []
        for record in self.list():
            if not record.enabled:
                continue
            if record.id in relay_targets:
                targets = {str(row.get("model")): row for row in relay_targets[record.id]}
                for raw in record.models:
                    entry = ModelEntry.from_dict(raw)
                    target = targets.get(entry.id)
                    if not entry.enabled or target is None:
                        continue
                    entries.append({
                        "kind": facts.TOKEN_RELAY_KIND,
                        "access": record.access_type,
                        "model": entry.id,
                        "max_qps": (per_model_qps
                                    if (record.id, entry.id) in live_relay else 0.0),
                        "ask_price": str(entry.min_price or "0"),
                        "unit": entry.min_price_unit,
                        "resource_level": str(profile.preset),
                        "canonical": str(target.get("canonical") or ""),
                    })
                continue
            for raw in record.models:
                entry = ModelEntry.from_dict(raw)
                if not entry.enabled:
                    continue
                entries.append({
                    "kind": facts.TOKEN_RELAY_KIND,
                    "access": record.access_type,
                    "model": entry.id,
                    "max_qps": per_model_qps if (record.id, entry.id) in live else 0.0,
                    "ask_price": str(entry.min_price or "0"),
                    "unit": entry.min_price_unit,
                    "resource_level": str(profile.preset),
                })
        return entries

    def catalog(self, *, price_version: str = "local", now: Any = None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        moment = now or utcnow()
        for record in self.list():
            if not record.enabled:
                continue
            for raw in record.models:
                entry = ModelEntry.from_dict(raw)
                enabled = entry.enabled
                price = entry.min_price
                entries.append({
                    "id": entry.id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "djb-node",
                    "access": record.access_type,
                    "upstream_id": record.id,
                    "enabled": enabled,
                    "available": enabled and record.state in ("declared", "probing", "verified"),
                    "price_version": price_version,
                    "price": ({"amount": str(price),
                               "unit": entry.min_price_unit}
                              if price is not None else None),
                    # 只加不改：本地探针可见性（spec Model 无这些字段，本机 API 仅自用）
                    "probe": {"inference_ok": entry.inference_ok,
                              "stream_ok": entry.stream_ok,
                              "usage_ok": entry.usage_ok,
                              "probe_version": entry.probe_version,
                              "last_probe_at": entry.last_probe_at,
                              "fresh": entry.fresh(moment),
                              "announceable": entry.announceable(moment)},
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
        payload = {
            "access_type": record.access_type,
            "base_url": record.base_url,
            "models": record.model_ids(),
            "protocol": record.protocol,
            **({"credential_handle": record.credential_ref}
               if record.credential_ref else {}),
        }
        # M17 §4.1：kind 是可选加性字段；未标注时不出现（旧服务端行为不变）
        kind = validate_upstream_kind(record.upstream_kind)
        if kind:
            payload["upstream_kind"] = kind
        return payload
