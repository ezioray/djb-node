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
* 逐模型开关与最低价（min_price / min_price_unit）保存在本地；价格只作公告输入，
  不作为结算依据。公告 / 报价的 **计量单位** 一律走 :func:`capacity_unit`
  （= ``token``，P6）；``min_price_unit`` 只留给本地成本估算（``daemon.plan_charge``）。
* A2 五字段 = endpoint / model / credential_ref / concurrency_limit / receipt_sample
  （M02 CR-6 裁决）；A2 prompt 模板五段 = 目标 / 约束 / 输入 / 输出 / 验收（03-facts §11）。
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from . import facts, probe as probe_mod, upstream_protocols as up
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
    #: 最近一次探针的失败分类（``timeout`` / ``connect`` …）。只落本地、只用于把
    #: 「为什么掉了容量」说出来（P6/E2E-10：原来只有 state=rejected，用户看不到原因）。
    probe_error_class: str | None = None

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
        if self.probe_error_class is not None:
            payload["probe_error_class"] = self.probe_error_class
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
                   last_probe_at=_optional_str(raw.get("last_probe_at")),
                   probe_error_class=_optional_str(raw.get("probe_error_class")))

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


def record_age_s(record: UpstreamRecord, now: Any) -> float | None:
    """上游**最近一轮**探针距今秒数；从未探过返回 ``None``。"""
    stamp = parse_iso(record.last_probe_at or "")
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def covered(record: UpstreamRecord, entry: ModelEntry, now: Any, *,
            ttl: int | None = None) -> bool:
    """模型级证据是否仍可采信（日探针口径；CHG-20261121-PROBE-DAILY）。

    每链接每天只探 **1 个代表模型**（``probe.pick_daily_model``），所以「模型自己今天被实探过」
    这件事不再天天成立。门控因此有两条来源，任一成立即算「有新鲜证据」：

    1. **实探新鲜且通过**：模型自己的 ``last_probe_at`` 在 ``ttl``（默认 ``PROBE_FRESH_S``）内
       **且**该次结论是 L2 通过（``inference_ok is True``）；
    2. **链接级覆盖**：上游本轮通过（``state != rejected`` 且上游 ``last_probe_at`` 在
       ``ttl`` 内）**且**该模型上次结论是 L2 通过（``inference_ok is True``）
       **且**那份结论不超过 ``PROBE_MODEL_MAX_AGE_S``（7 天）。

    第 2 条与云端同口径：云端 ``verified_at`` 是**按上游**存的，``_verified_pairs`` 会把
    ``node_upstreams.models``（申报清单）整条展开 —— 「上游今天通过」对云端等于
    「申报的每个模型都新鲜」。本地若坚持只认模型级时间戳，就会出现
    「云端认（容量还在）而节点自己不发（max_qps=0）」的单边掉档，且**没有任何自动恢复路径**。

    第 3 条（7 天上限，lead 2026-11-21 复核补充）是这条继承的刹车：没有它，一个**已经坏掉
    但没轮到**的模型会被「上游健康」永久带住，用户看到「可公告」、实际打过去 4xx。
    超过 7 天未被实探的模型自然掉容量 —— 这是日频采样换额度的明示代价（票面 §3 风险）。

    注意本函数**不伪造新鲜度**：它不改 ``last_probe_at``，只在读取侧决定「这条证据还算不算数」；
    ``probe_details()`` 同时给出忠实的 ``fresh`` 与本口径的 ``covered``/``announceable``。
    """
    window = int(facts.PROBE_FRESH_S if ttl is None else ttl)
    # 注意 ``verified()`` 不能省（lead 2026-11-21 复核阻断）：L2 失败/未知**也会**写
    # ``last_probe_at``，只判 ``fresh()`` 会把「刚探过但推理失败/没结论」的模型算成
    # 「有新鲜证据」⇒ 公告/报价误放行（比 CHG 之前的 ``ModelEntry.announceable()`` 还松，
    # 是本次改动引入的回归）。两条分支都必须以「L2 通过」为前提。
    if entry.verified() and entry.fresh(now, ttl=window):
        return True
    if not entry.verified():                       # inference_ok is not True：没有正面证据
        return False
    if record.state == "rejected":                 # 本轮整条被拒（含不可达）
        return False
    age = entry.probe_age_s(now)
    if age is None or age > facts.PROBE_MODEL_MAX_AGE_S:
        return False                               # 模型自身结论太旧：不再继承链接级健康
    record_age = record_age_s(record, now)
    if record_age is None or record_age > window:
        return False                               # 上游这一轮也过期了
    return True


def coverage_announceable(record: UpstreamRecord, entry: ModelEntry, now: Any,
                          *, ttl: int | None = None) -> bool:
    """``covered()`` + 「模型启用」：公告/报价共用的唯一门控出口。"""
    return bool(entry.enabled) and covered(record, entry, now, ttl=ttl)


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


# ─────────────────────────────────────── base_url 来源分类（M17 先验；只作信任先验，永不拦截）
#: 来源三分类（INTEGRITY-REVIEW-20261117 §5 / CR-20261117-AUDIT-FOLLOWUP）。
#: **只作探针强度与提示的先验**：official 降探针频率但**永不免检**（前置代理仍可
#: 作弊）；unknown 提示更严格抽检。分类失败/不认识一律 unknown（fail-closed）。
ORIGIN_OFFICIAL = "official"
ORIGIN_TRUSTED_OPEN_SOURCE = "trusted_open_source"
ORIGIN_UNKNOWN = "unknown"

#: 厂商官方端点（host 精确匹配；缺省端口）。**新增厂商只加这里，不写逻辑**。
OFFICIAL_ENDPOINT_HOSTS: frozenset[str] = frozenset({
    "api.openai.com",             # OpenAI
    "api.anthropic.com",          # Anthropic
    "api.deepseek.com",           # DeepSeek
    "api.moonshot.cn", "api.moonshot.ai",   # 月之暗面 Kimi
    "open.bigmodel.cn",           # 智谱 GLM
    "dashscope.aliyuncs.com",     # 阿里通义
    "api.minimax.chat", "api.minimaxi.com", # MiniMax
    "generativelanguage.googleapis.com",    # Google Gemini
    "api.mistral.ai",             # Mistral
    "api.cohere.com", "api.cohere.ai",      # Cohere
    "api.groq.com",               # Groq（自托管官方端点）
    "api.x.ai",                   # xAI
})

#: 知名开源/聚合端点（域名背后是知名运营方；**OneAPI/NewAPI 自部署不属于**——
#: 域名可同、背后可改，只能按 unknown 处理，除非走 M17 §5.6 独立探针凭据）。
TRUSTED_OPEN_SOURCE_HOSTS: frozenset[str] = frozenset({
    "openrouter.ai",
})

#: 缺省端口（host 精确匹配且端口为空或等于缺省端口才算命中；非标端口一律 unknown）。
_DEFAULT_PORTS = {"https": 443, "http": 80}

ORIGIN_HINTS: dict[str, str] = {
    ORIGIN_OFFICIAL: "官方端点（信任先验高；仍保留抽检，不因官方免检）",
    ORIGIN_TRUSTED_OPEN_SOURCE: "知名开源/聚合端点（信任先验中；保留常规探针）",
    ORIGIN_UNKNOWN: "未识别端点（不拦截；接入后将按更严格的探针与抽检处理）",
}


def classify_base_url(base_url: Any) -> str:
    """按 host 精确匹配判来源三分类。

    防绕过口径（M17 §2 威胁模型的工程化）：

    * **只认 host 精确匹配**：``api.openai.com.evil.tld`` / ``api-openai-com.tld``
      这类前后缀伪装全部落 unknown；
    * **端口必须缺省**：官方域名 + 非标端口（8443 等）多为前置代理 → unknown；
    * **userinfo / 非 http(s) / 解析失败** → unknown（``validate_base_url`` 会先拦，
      这里防御性兜底，保证任何输入都有确定分类）；
    * IP 直连 / 回环 / 内网 → unknown（本机 Agent 场景由 access/kind 区分，不用来源分）。

    该函数是**纯函数**：不查 DNS（不把解析结果当信任依据）、不联网、不落盘。
    """
    try:
        text = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(text)
    except (ValueError, TypeError):
        return ORIGIN_UNKNOWN
    if parsed.scheme not in ("http", "https"):
        return ORIGIN_UNKNOWN
    if parsed.username or parsed.password or parsed.fragment:
        return ORIGIN_UNKNOWN
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not host.isascii():
        # 非 ASCII（IDN 同形字）→ 不认识就是 unknown
        return ORIGIN_UNKNOWN
    if not (host[0].isalnum() and host[-1].isalnum()):
        return ORIGIN_UNKNOWN
    port = parsed.port
    if port is not None and port != _DEFAULT_PORTS[parsed.scheme]:
        return ORIGIN_UNKNOWN
    # IP 直连（v4/v6）一律 unknown：官方端点不会用裸 IP
    try:
        ipaddress.ip_address(host)
        return ORIGIN_UNKNOWN
    except ValueError:
        pass
    if host in OFFICIAL_ENDPOINT_HOSTS:
        # 官方端点要求 https（http 官方域名 = 中间人/镜像风险）
        return ORIGIN_OFFICIAL if parsed.scheme == "https" else ORIGIN_UNKNOWN
    if host in TRUSTED_OPEN_SOURCE_HOSTS:
        return ORIGIN_TRUSTED_OPEN_SOURCE if parsed.scheme == "https" else ORIGIN_UNKNOWN
    return ORIGIN_UNKNOWN


def origin_hint(origin: Any) -> str:
    """分类 → 客户可见提示文案（未知分类回落 unknown 文案）。"""
    return ORIGIN_HINTS.get(str(origin or "").strip(), ORIGIN_HINTS[ORIGIN_UNKNOWN])


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
                   http: HttpClient | None = None, timeout: float = 5.0,
                   protocol: str | None = None) -> ProbeResult:
    """GET {base}/models：只取「可达 + 延迟 + 是否声明」，不读上游正文。

    ``protocol`` 可选（缺省 = ``openai-chat``，与改前逐字一致）：只影响**鉴权头**
    —— ``anthropic-messages`` 用 ``x-api-key`` + ``anthropic-version``，
    OpenAI 兼容仍是 ``Authorization: Bearer``。URL 两个协议都打 ``/models``
    （Anthropic 也有 ``/v1/models``；Stormesh 这类 base 以 ``/v1`` 结尾的同样通）。
    """
    client = http or HttpClient()
    started = utcnow()
    headers = up.auth_headers(protocol, credential)
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


def capacity_unit() -> str:
    """容量公告与报价共用的**计量单位**（P6 单一来源，CR-20261106-OFFICIAL-SUPPLY-01）。

    返回固定值 ``"token"``（``facts.CAPACITY_UNIT``）。云端候选集按
    ``(node_id, access, unit)`` 对齐报价与容量公告，两边只要有一个字不同，
    节点就永远进不了候选集（F5 ``no_quote``），所以这里**只有这一个出口**。

    历史上的缺陷：容量公告把**价格单位** ``entry.min_price_unit``
    （默认 ``usd_per_1m_tokens``）写进了 ``unit``。``min_price_unit`` 从此只作
    本地成本估算口径（``daemon.plan_charge`` → ``plan_router.estimate_cost_minor``），
    不再出现在任何上行公告里。
    """
    return facts.CAPACITY_UNIT


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
              deadline: Any = None, clock: Callable[[], Any] | None = None,
              daily_day: int | None = None
              ) -> dict[str, Any]:
        """L1（/models）+ 逐模型 L2/L3，结论写回本地模型 JSON。

        返回值**只含 spec 1.2.7 `NodeProbeReport` 已定义的字段**
        （upstream_id / reachable / latency_ms / error_class /
        model_checks[model, ok, error_class] / checked_at），可直接上报。
        加上 RC14 §2.3 的两个**加性**字段 ``models[]``（这轮探了哪些，只含 enabled）与
        ``not_probed[]{model, reason}``（没探的及原因）；旧服务端忽略未知字段。
        L2/L3 细节（inference_ok / stream_ok / usage_ok / token_usage /
        probe_version / last_probe_at）只落本地：`probe_details()` 可读。

        ``daily_day``（CHG-20261121-PROBE-DAILY）：传了就是一个**日探针轮次** ——
        L1（一次 ``GET /models``，不计推理额度）照旧覆盖全部 enabled 模型，
        L2/L3 只打 **1 个代表模型**（``probe.pick_daily_model`` 按日期从 L1 清单里轮换）。
        没轮到的模型进 ``not_probed[]`` 且原因记 ``rotation``（**不是** ``budget``）：
        否则 daemon 会把它当 backlog 每 5 s 续跑一次，退回额度燃烧。
        传 ``None``（默认）= 旧行为（``djb upstream probe`` 手动路径），随时可用、全模型实探。
        """
        moment = now or utcnow()
        seconds = float(timeout if timeout is not None else facts.PROBE_TIMEOUT_S)
        record = self.store.upstream(upstream_id)
        secret = self.credentials.upstream_secret(upstream_id)
        # 协议必须从**记录**传下去（CR-20261106 客户端 P2）：daemon 的周期探针、
        # 启动探针、CLI `djb upstream probe` 全走这一个入口，所以在这一处接线
        # 就覆盖了所有调用方；`record.protocol` 缺省即 `openai-chat`，旧上游行为不变。
        # W6-FIX：只探测 enabled 模型。disabled 模型可能已不在上游 /models 清单，
        # 若仍参与探针，云端 `_probe_verdict` 会「任一失败即 rejected」，
        # 导致「停用」无法把上游恢复为 verified（RC13 W6 实测 6/7）。
        probe_models = [str(item.get("id")) for item in record.models
                        if item.get("id") and item.get("enabled", True)]
        result = probe_upstream(record.base_url, probe_models,
                                credential=secret, http=self.http, timeout=seconds,
                                protocol=record.protocol)
        checks: list[dict[str, Any]] = [dict(item) for item in result.model_checks]
        outcomes: dict[str, probe_mod.ProbeOutcome] = {}
        reused: set[str] = set()
        daily_pick: str | None = None
        deferred: set[str] = set()
        if result.reachable:
            listed = [str(item.get("model")) for item in checks if item.get("ok")]
            # 0.3.9 / D-RCA P1-b：排定探测顺序（没轮到的先探、已知失败排最后），并在
            # 预算受限的轮次里复用「刚探过且 ok」的结论 —— 5s backlog 续跑只补缺口。
            targets, reused = self._order_probe_targets(
                record, listed, moment, budgeted=deadline is not None)
            # CHG-20261121-PROBE-DAILY：日探针**只探 1 个代表模型**。
            # 代表模型从 L1 清单里挑（``listed``）：清单是上游此刻真实的模型集合，
            # 从这里挑就不会把「本地记着、上游已下架」的幽灵模型当代表 —— 那会让整轮
            # 判 rejected（``model_not_listed`` 在 upstream 侧算显式失败），一天都白探。
            # 清单为空（L1 通过但模型全不在清单 / 无 enabled 模型）时退回 enabled 集合。
            if daily_day is not None:
                daily_pick = probe_mod.pick_daily_model(listed or probe_models, daily_day)
                deferred = {model for model in probe_models if model != daily_pick}
                targets = [model for model in targets if model == daily_pick]
            outcomes = probe_mod.probe_models(
                record.base_url, targets, credential=secret,
                level=level or facts.PROBE_DEFAULT_LEVEL, timeout=seconds,
                http=self.http, deadline=deadline, clock=clock,
                protocol=record.protocol)
        # 逐模型合并：L2/L3 结论优先；未在清单 / 预算内没探到 -> 不 ok。
        for item in checks:
            model_id = str(item.get("model"))
            if model_id in reused:
                # P1-b：本轮复用上一轮 ok 证据（``PROBE_REUSE_OK_S`` 内实探过）。
                # 保留 L1 的 ok；不写 ``last_probe_at``（不伪造新鲜度，见 probe() 的落盘）。
                item.pop("error_class", None)
                continue
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
        # 0.3.9：``verified`` 取自「本轮合并后的逐模型结论」，因此**复用**的模型也算证据
        # （它的 ok 是几秒前实探出来的），与 P0-a 的「任一 verified ⇒ 整条 verified」一致。
        verified = [str(item.get("model")) for item in checks if item.get("ok")]
        # RC14 复盘 P0-2（0.3.8）：**没有证据的轮次不改状态**。
        # 旧写法 `verified if reachable and verified else rejected` 会在「预算截断、
        # 一个模型都没探到」时把本地 state 抹成 rejected；而 `quotes.py` 的
        # `gated = ... or record.state == "rejected"` 会因此把这条上游的全部报价门掉
        # ⇒ 容量随**采样预算**抖动（与云端 `verified_at = moment if verified else None`
        # 是同一个 bug 的两端，cloud-auditor 2026-11-15 已确认）。
        # 语义对齐云端 b420f59：没有新证据 ⇒ 什么都不改，旧结论按新鲜度自然过期。
        # 0.3.9 / D-RCA P0-a：**任何显式失败都先于任何 verified** 是 0.3.8 灰度失败的
        # 直接原因——4 个模型 ok + qwen 一个 ``timeout`` 就把整条上游判 ``rejected``，
        # `quotes.py` 的 `gated` 随即跳过该上游**全部**模型（2026-11-16 13:55 现场
        # `quotes rows=0` 整条断流）。判定顺序必须与云端 `_probe_verdict_scoped` 的
        # `if real_ok: verified` 对齐：**一个模型能用就是这条上游能用的证据**，
        # 单模型失败只降该模型自己（它的 `inference_ok=false` 已被 quotes 按模型跳过）。
        explicit_failures = [item for item in checks
                             if not item.get("ok")
                             and item.get("error_class") != facts.PROBE_NOT_PROBED]
        if not result.reachable or not checks:
            # 不可达 / **一个 enabled 模型都没有** ⇒ rejected（安全侧：没启用模型的上游
            # 不该被当成可用容量）。
            state = "rejected"
        elif verified:
            # 任一模型 L2 通过 ⇒ 整条 verified（0.3.4 语义；与云端 real_ok 分支一致）。
            state = "verified"
        elif explicit_failures:
            # 一个都没通**且**确有失败（timeout / 5xx / auth / 下架 …）⇒ rejected。
            # P0-c：``timeout`` 是真实失败，必须留在这里，否则「全部模型 timeout」会被
            # 洗成可用；单模型 timeout 的降级由「上面那条 verified 优先」完成，不靠删失败。
            state = "rejected"
        else:
            state = record.state          # 全是 not_probed：没证据，保留上一轮结论
        stamp = iso(moment)
        self.store.update_upstream(upstream_id, models=self._probe_models_payload(
            record, outcomes, stamp), state=state, last_probe_at=stamp)
        # RC14 复盘 P0-2（0.3.8）：**不要**把 ``not_probed`` 从 ``model_checks`` 里剔掉。
        # 云端 main（745eddc）的 ``_probe_verdict_scoped`` 已按 ``models[]``（enabled 集合）
        # + ``not_probed[]`` 判定：not_probed 记 ``ok=None``、不算失败，全都没探到时判 verified；
        # 节点侧再剔除会让 ``models[]`` 从「enabled 集合」退化成「探过的集合」，
        # 把云端的 ``not_reported`` 分支和排障信息一起抹掉（2026-11-15 cloud-auditor 校正）。
        # 节点只负责**如实上报**：``models[]`` = 本轮 L1 进过清单的 enabled 模型，
        # ``not_probed[]`` = 没探到的及原因；「算不算失败」由云端 scoped 判定负责。
        payload = {"upstream_id": upstream_id, "reachable": result.reachable,
                   "latency_ms": result.latency_ms,
                   "error_class": result.error_class,
                   "model_checks": checks, "checked_at": stamp,
                   # RC14 §2.3 加性字段（只加不改）：明确「这一轮探了哪些 / 哪些没探」。
                   # 云端据此判「至少一个 enabled 模型通过即 ok」；旧服务端忽略未知字段。
                   "models": [str(item.get("model")) for item in checks],
                   # 注意：本轮**实际发了推理请求**的模型数**不进**这里 —— payload 只允许
                   # ``NodeProbeReport`` 已定义的字段（tests/test_upstream_gating 钉着
                   # 「不许多字段」）。daemon 侧用 passed + real_failures 计数代替
                   # （日探针下每链接正好 1），见 `_probe_upstreams`。
                   "not_probed": self._not_probed(record, checks, outcomes,
                                                reused=reused, deferred=deferred)}
        return payload

    @staticmethod
    def _order_probe_targets(record: UpstreamRecord, listed: list[str], moment: Any,
                             *, budgeted: bool) -> tuple[list[str], set[str]]:
        """0.3.9 / D-RCA P1-b：排定「这一轮探谁、按什么顺序」，并挑出可复用证据的模型。

        三条口径（都是 0.3.4/0.3.8 现场事故的直接教训）：

        1. **没轮到的先探**：按 ``last_probe_at`` 升序，从未探过的（``""``）排最前。
           0.3.4 的逐模型 ``stale_probe``（``last_probe_at`` 爬到 635s > 600s，5 行掉到
           1 行）就是这个顺序没做：固定按上游 ``/models`` 顺序探，尾部模型永远轮不到。
        2. **已知失败排最后**：``last_probe_at`` 相同时（例如刚在同一轮里一个 timeout、
           一个 ok），用 ``probe_error_class`` 把刚失败的模型推到队尾 —— 否则慢模型
           每轮第一个吃预算，其余模型永远 ``not_probed``（饥饿）。
        3. **不重探刚 ok 的模型**：``budgeted``（本轮有 ``deadline``）且**确有缺口**
           （有 enabled 模型没有新鲜 ok 证据）时，``PROBE_REUSE_OK_S`` 内刚实探过且 ok
           的模型直接复用结论 —— 5s backlog 续跑只补缺口，不再把预算花在 5 秒前刚验过
           的模型上。没有缺口就整轮实探（不给「靠复用混过一轮」留后门）；
           手动 ``djb upstream probe``（``deadline=None``，不是 ``budgeted``）永远实探。

        返回 ``(targets, reused)``。``reused`` 的模型本轮**不发请求**、**不刷新**
        ``last_probe_at``（不伪造新鲜度），也不算 backlog。
        """
        entries = {entry.id: entry
                   for entry in (ModelEntry.from_dict(raw) for raw in record.models)
                   if entry.id}
        tolerated = facts.PROBE_TOLERATED_ERROR_CLASSES

        def known_failure(model: str) -> int:
            entry = entries.get(model)
            error = entry.probe_error_class if entry is not None else None
            # 1 = 已知真实失败（timeout/5xx/…）⇒ 排最后；not_probed/未下架容忍类不算。
            return 1 if (error and error not in tolerated) else 0

        def last_probe(model: str) -> str:
            entry = entries.get(model)
            return (entry.last_probe_at or "") if entry is not None else ""

        # ``dict.fromkeys`` 去重但保序：同一模型一轮只探一次（不重复探已 ok 的模型）。
        ordered = sorted(dict.fromkeys(str(model) for model in listed),
                         key=lambda model: (known_failure(model), last_probe(model)))
        if not budgeted or facts.PROBE_REUSE_OK_S <= 0:
            return ordered, set()
        gap = [model for model in ordered
               if not (entries.get(model) is not None
                       and entries[model].inference_ok is True
                       and entries[model].fresh(moment, ttl=facts.PROBE_FRESH_AFTER_S))]
        if not gap:
            return ordered, set()
        reused: set[str] = set()
        for model in ordered:
            if model in gap:
                continue
            age = entries[model].probe_age_s(moment)
            if age is not None and age <= facts.PROBE_REUSE_OK_S:
                reused.add(model)
        return [model for model in ordered if model not in reused], reused

    @staticmethod
    def _not_probed(record: UpstreamRecord, checks: list[dict[str, Any]],
                    outcomes: dict[str, probe_mod.ProbeOutcome],
                    reused: set[str] | None = None,
                    deferred: Iterable[str] = ()) -> list[dict[str, Any]]:
        """``not_probed[]``：本地记录里有、但这一轮**没探到结论**的模型（原因可读）。

        四种原因（与 W6 的口径一致，不再让「一个没探到」拖垮整条上游）：

        * ``disabled``：模型被停用（本轮本来就不探，见 :meth:`probe` 的 W6-FIX 注释）；
        * ``budget``：enabled 且进了 L1 清单，但本轮预算/期限到了（``PROBE_PASS_BUDGET_S``），
          L2/L3 没轮到，留给下一轮，**不算失败**；
        * ``rotation``：**日探针**下按日期轮换没选中的模型（CHG-20261121-PROBE-DAILY）。
          与 ``budget`` 分开记的原因：daemon 只把 ``budget`` 当 backlog 去 5 s 续跑，
          轮换是**设计如此**，若也记 ``budget`` 就会每 5 s 重探一轮 —— 又回到烧额度。
        * ``not_listed``：enabled，但这一轮连 L1 请求都没进（记录在探针过程中被改写等边界）。
          正常路径上「上游清单没列某模型」会落在 ``model_checks`` 里（``ok=False`` +
          ``model_not_listed``），不在这里。

        ``reused``（P1-b）：本轮**复用**了上一轮 ok 结论的模型 —— 它不是缺口（几秒前刚实探
        过），既不进 ``not_probed[]``，也不该被算成 backlog 去触发 5s 续跑。
        """
        rows: list[dict[str, Any]] = []
        checked = {str(item.get("model")): item for item in checks}
        rotated = {str(model) for model in deferred}
        for raw in record.models:
            entry = ModelEntry.from_dict(raw)
            if not entry.id:
                continue
            if not entry.enabled:
                rows.append({"model": entry.id, "reason": "disabled"})
            elif entry.id not in checked:
                rows.append({"model": entry.id, "reason": "not_listed"})
            elif entry.id in (reused or ()):
                continue                      # P1-b：刚 ok 且复用证据，不算缺口
            elif entry.id in rotated:
                rows.append({"model": entry.id, "reason": "rotation"})
            elif outcomes.get(entry.id) is None:
                # 已有明确结论（如 model_not_listed / L2 失败）的模型属于**失败**，
                # 不能重复记成 backlog；只有「L1 通过但预算没轮到」才算 budget。
                reason = checked[entry.id].get("error_class")
                if reason and reason != facts.PROBE_NOT_PROBED:
                    continue
                rows.append({"model": entry.id, "reason": "budget"})
        return rows

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
                # P6/E2E-10：失败分类也落本地（只在失败时写，成功即清掉），
                # 否则「容量为什么掉了」只能靠猜，用户只看到 rejected。
                if outcome.inference_ok:
                    item.pop("probe_error_class", None)
                elif outcome.error_class:
                    item["probe_error_class"] = str(outcome.error_class)
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
                            # P6：失败分类 + 已过秒数（「X 分钟后掉容量」的唯一来源）
                            "probe_error_class": entry.probe_error_class,
                            "probe_age_s": entry.probe_age_s(moment),
                            # 忠实字段：这个模型自己多久前被**实探**过
                            "fresh": entry.fresh(moment),
                            # 加性字段（CHG-20261121-PROBE-DAILY）：日轮换下「本轮没轮到，
                            # 但链接级覆盖仍算数」的模型在这里为 True —— 渲染层据此把
                            # 「结论过期」与「没轮到但被上游带住」区分开。
                            "covered": covered(record, entry, moment),
                            "announceable": coverage_announceable(record, entry, moment)})
        return details

    # ── 公告 / 目录 / 路由
    def announceable_models(self, now: Any = None) -> list[tuple[UpstreamRecord, ModelEntry]]:
        """可**真正**进容量的模型（>0 QPS）：模型级 L2 证据 + 新鲜/覆盖 + 启用。

        门控口径（C7 + CHG-20261121-PROBE-DAILY）：

        * 模型级：`inference_ok is True`（L2 通过过）；
        * 新鲜度：走 :func:`covered` —— 要么模型自己 ``last_probe_at`` 在
          `facts.PROBE_FRESH_S`（25 h）内（实探过），要么「上游本轮通过 + 该模型上次结论
          L2 通过且不超过 7 天」（日探针的链接级覆盖，与云端 ``verified_at`` 同口径）；
          从未探过 / L2 失败 / 模型结论超过 7 天一律不算；
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
                if coverage_announceable(record, entry, moment):
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
                        # P6：公告的 unit 是**计量单位**（token），不是价格单位
                        "unit": capacity_unit(),
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
                    # P6：同 relay 分支，公告 unit 一律走单一来源（token）
                    "unit": capacity_unit(),
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
                              "covered": covered(record, entry, moment),
                              "announceable": coverage_announceable(record, entry, moment)},
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
