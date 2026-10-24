# SPDX-License-Identifier: Apache-2.0
"""M18b · 客户端模型 / tokenizer 同步（拉取 → 校验 → 热应用 → 上报）。

目标（``product/v4/modules/M18-model-sync.md`` §5–§6）：厂商升级后，客户端在分钟级拿到
「模型 → tokenizer 版本 / 指纹 / 上下文」的**签名 bundle**，校验后热应用；无法本地支持的
tokenizer 标 ``unsupported``，**不伪造精确计数、不自动装依赖**；任何校验失败都保留旧版。

    GET  /api/v1/node/model-catalog?since=<version>   → bundle（200 / 304）
    POST /api/v1/node/model-sync/state                → {applied_version, drift, error_class}

bundle 形状（M18a 定稿）::

    {"version": 7, "payload_hash": "<sha256>", "signature": "hmac-sha256:...",
     "expires_at": "2026-10-26T00:00:00Z", "not_modified": false,
     "models": [{"vendor": "openai", "canonical": "gpt-4o",
                 "aliases": ["gpt-4o-2024-11-20"], "context_window": 128000,
                 "tokenizer_family": "tiktoken", "tokenizer_version": "o200k_base",
                 "tokenizer_hash": "<sha256>", "effective_at": "2026-10-20T00:00:00Z"}]}

校验（任一失败 → 保留旧版 + 上报 ``error_class``）：

1. ``version`` 必须是整数且**单调递增**（回退/相等 → ``version_regression``）；
2. ``payload_hash`` 必须等于 canonical JSON 的 sha256（``hash_mismatch``）；
3. ``expires_at`` 必须未过期（``expired``）；
4. 可选 ``signature``：注入 ``verifier`` 时必须通过（``signature_invalid``）；未注入 verifier
   时按 S2 测试网口径**不拒绝**，只标 ``verified=False`` + 告警事件；
5. ``models[*].canonical`` 必填（缺的整条丢弃）。

硬约束：

* **不改 tokenizer 实现**：本模块只写元数据；真分词仍由 ``djb_node.tokens`` 决定。
* **不认识就标 unsupported**：``tokenizer_family`` 不在 ``facts.MODEL_SYNC_KNOWN_TOKENIZERS``
  或 ``tokenizer_version`` 为空 → 进 ``unsupported``，计数按 ``estimated`` 参考。
* **不删旧模型**：只合并更新（旧模型不在新 bundle 里也保留）。
* **不落正文 / 凭据**：状态与事件只有版本号、canonical、字段名、错误类；本地文件只有元数据。
* 同步失败不影响接单（M18 §5）：只在 status 与事件里显形。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping

from . import facts
from .api import NodeApi
from .config import ClientConfig, Paths, feature_flag
from .errors import DjbError
from .httpclient import HttpClient
from .logging_setup import event
from .secure import CredentialStore
from .util import atomic_write, iso, parse_iso, read_json, sha256_hex, utcnow

__all__ = [
    "BundleModel",
    "ModelSyncClient",
    "SyncRejected",
    "bundle_file",
    "canonical_bundle",
    "model_sync_enabled",
    "payload_hash",
]

#: 签名校验器：``verifier(canonical_payload, signature) -> bool``（S3 用 Ed25519 / S2 用 HMAC）
Verifier = Callable[[dict[str, Any], str], bool]


def model_sync_enabled(config: Any) -> bool:
    """开关 ``client.model_sync.enabled``（默认关闭；唯一求值入口 config.feature_flag）。"""
    return bool(config is not None and feature_flag(config, facts.MODEL_SYNC_FLAG))


def bundle_file(paths: Paths) -> str:
    """本地已应用 bundle 的路径（不进 config.py，保持写域最小）。"""
    return os.path.join(paths.home, facts.MODEL_SYNC_FILE)


# ─────────────────────────────────────────────────────────── 规范化 / hash
def canonical_bundle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """参与 ``payload_hash`` 的部分：version / expires_at / models（顺序稳定）。"""
    models = payload.get("models")
    clean: list[dict[str, Any]] = []
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, Mapping):
                continue
            clean.append({key: item.get(key) for key in facts.MODEL_SYNC_FIELDS
                          if key in item})
    canonical: dict[str, Any] = {"version": payload.get("version"),
                                 "expires_at": payload.get("expires_at"),
                                 "models": clean}
    return canonical


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    """canonical JSON 的 sha256（十六进制小写）。"""
    return sha256_hex(canonical_json(canonical_bundle(payload)))


# ─────────────────────────────────────────────────────────── 模型条目
@dataclass
class BundleModel:
    vendor: str = ""
    canonical: str = ""
    aliases: tuple[str, ...] = ()
    context_window: int | None = None
    tokenizer_family: str = ""
    tokenizer_version: str = ""
    tokenizer_hash: str = ""
    effective_at: str | None = None
    #: 本地是否有该 tokenizer 的实现；False → unsupported（计数标 estimated）
    supported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"vendor": self.vendor, "canonical": self.canonical,
                "aliases": list(self.aliases), "context_window": self.context_window,
                "tokenizer_family": self.tokenizer_family,
                "tokenizer_version": self.tokenizer_version,
                "tokenizer_hash": self.tokenizer_hash,
                "effective_at": self.effective_at, "supported": self.supported}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BundleModel":
        family = str(raw.get("tokenizer_family") or "").strip()
        version = str(raw.get("tokenizer_version") or "").strip()
        aliases = raw.get("aliases")
        context = raw.get("context_window")
        return cls(
            vendor=str(raw.get("vendor") or "").strip(),
            canonical=str(raw.get("canonical") or "").strip(),
            aliases=tuple(str(item) for item in aliases) if isinstance(aliases, list) else (),
            context_window=(int(context) if isinstance(context, int)
                            and not isinstance(context, bool) else None),
            tokenizer_family=family,
            tokenizer_version=version,
            tokenizer_hash=str(raw.get("tokenizer_hash") or "").strip(),
            effective_at=(str(raw.get("effective_at")) if raw.get("effective_at") else None),
            supported=(family in facts.MODEL_SYNC_KNOWN_TOKENIZERS and bool(version)),
        )


class SyncRejected(Exception):
    """校验失败（保留旧版并上报 error_class）。"""

    def __init__(self, error_class: str, detail: str = "") -> None:
        super().__init__(error_class + (": " + detail if detail else ""))
        self.error_class = str(error_class)
        self.detail = detail


@dataclass
class SyncOutcome:
    """一次 sync 的结果（可直接 JSON 化给 CLI / status）。"""

    changed: bool = False
    applied_version: int = 0
    error_class: str | None = None
    verified: bool = False
    models: int = 0
    unsupported: list[str] = field(default_factory=list)
    drift: list[dict[str, Any]] = field(default_factory=list)
    reported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "applied_version": self.applied_version,
                "error_class": self.error_class, "verified": self.verified,
                "models": self.models, "unsupported": list(self.unsupported),
                "drift": list(self.drift), "reported": self.reported}


# ─────────────────────────────────────────────────────────── 客户端
class ModelSyncClient:
    """拉取签名 bundle → 校验 → 应用本地 JSON → 上报状态（不接单、不改 tokenizer 实现）。"""

    def __init__(self, paths: Paths, config: ClientConfig, credentials: CredentialStore, *,
                 http: HttpClient | None = None, api: NodeApi | None = None,
                 now: Callable[[], Any] = utcnow, verifier: Verifier | None = None,
                 sleep: Callable[[float], Any] | None = None) -> None:
        self.paths = paths
        self.config = config
        self.credentials = credentials
        self.http = http or HttpClient(timeout=float(facts.MODEL_SYNC_TIMEOUT_S))
        self._now = now or utcnow
        self._sleep = sleep
        self.verifier = verifier
        self.api = api or NodeApi(config.gateway_url,
                                  client_key=credentials.client_key(), http=self.http)
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._last_outcome: dict[str, Any] = {}
        self._next_at: str | None = None
        self._syncs = 0

    # ── 本地状态（只元数据；原子写 0600）
    def load_state(self) -> dict[str, Any]:
        raw = read_json(bundle_file(self.paths), default=None)
        if not isinstance(raw, dict):
            return {"schema": facts.MODEL_SYNC_SCHEMA, "current_version": 0,
                    "applied_at": None, "models": {}, "unsupported": [], "drift": [],
                    "payload_hash": None, "verified": False, "expires_at": None}
        state = dict(raw)
        state.setdefault("schema", facts.MODEL_SYNC_SCHEMA)
        state["current_version"] = _as_int(state.get("current_version")) or 0
        models = state.get("models")
        state["models"] = dict(models) if isinstance(models, dict) else {}
        state["unsupported"] = [str(item) for item in (state.get("unsupported") or [])
                                if isinstance(item, str)]
        state["drift"] = [item for item in (state.get("drift") or [])
                          if isinstance(item, dict)]
        return state

    def _save_state(self, state: Mapping[str, Any]) -> str:
        path = bundle_file(self.paths)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        atomic_write(path, json.dumps(dict(state), ensure_ascii=False, sort_keys=True),
                     mode=0o600)
        return path

    # ── 状态面
    def status(self) -> dict[str, Any]:
        """只读快照：版本 / 应用时间 / 模型数 / unsupported / drift / last_error。"""
        with self._lock:
            state = self.load_state()
            models = state.get("models") or {}
            return {
                "enabled": model_sync_enabled(self.config),
                "current_version": int(state.get("current_version") or 0),
                "applied_at": state.get("applied_at"),
                "payload_hash": state.get("payload_hash"),
                "verified": bool(state.get("verified")),
                "expires_at": state.get("expires_at"),
                "models": len(models),
                "canonical": sorted(models)[:200],
                "unsupported": list(state.get("unsupported") or []),
                "drift": list(state.get("drift") or []),
                "last_error": self._last_error or state.get("last_error"),
                "last_outcome": dict(self._last_outcome),
                "syncs": self._syncs,
                "interval_s": facts.MODEL_SYNC_INTERVAL_S,
                "next_at": self._next_at,
                "file": facts.MODEL_SYNC_FILE,
            }

    # ── 校验
    def _verify(self, payload: Mapping[str, Any]) -> bool:
        """签名校验：有 verifier 必须通过；没有则只标记未验证（S2 测试网口径）。"""
        signature = str(payload.get("signature") or "").strip()
        if self.verifier is None:
            event("model_sync.unverified", level="warning",
                  reason="no_verifier", version=payload.get("version"))
            return False
        if not signature:
            raise SyncRejected("signature_invalid", "bundle 未签名")
        try:
            ok = bool(self.verifier(canonical_bundle(payload), signature))
        except Exception as exc:                               # verifier 自身异常 → 拒绝
            raise SyncRejected("signature_invalid", "verifier error:"
                               + type(exc).__name__) from exc
        if not ok:
            raise SyncRejected("signature_invalid", "签名校验不通过")
        return True

    def _validate(self, payload: Mapping[str, Any], *, current: int) -> tuple[int, bool]:
        version = _as_int(payload.get("version"))
        if version is None or version < 0:
            raise SyncRejected("version_invalid", "version 必须是整数")
        if version < current:
            raise SyncRejected("version_regression",
                               "服务端版本 " + str(version) + " < 本地 " + str(current))
        expected = str(payload.get("payload_hash") or "").strip().lower()
        if not expected:
            raise SyncRejected("payload_invalid", "缺 payload_hash")
        actual = payload_hash(payload)
        if expected != actual:
            raise SyncRejected("hash_mismatch", "payload_hash 不匹配")
        expires = parse_iso(payload.get("expires_at"))
        if expires is None:
            raise SyncRejected("payload_invalid", "expires_at 缺失或不合法")
        if expires <= self._now():
            raise SyncRejected("expired", "bundle 已过期")
        models = payload.get("models")
        if not isinstance(models, list) or not models:
            raise SyncRejected("payload_invalid", "models 必须是非空数组")
        verified = self._verify(payload)
        return version, verified

    # ── 应用
    def sync(self) -> dict[str, Any]:
        """拉取并应用一次；返回 outcome（失败也返回，不抛给调用方）。"""
        with self._lock:
            self._syncs += 1
            state = self.load_state()
            current = int(state.get("current_version") or 0)
            payload: dict[str, Any] | None = None
            try:
                payload = self.api.model_catalog(since=current or None)
                if bool(payload.get("not_modified")):
                    return self._finish(SyncOutcome(changed=False, applied_version=current,
                                                    error_class=None,
                                                    models=len(state.get("models") or {})),
                                        state)
                version, verified = self._validate(payload, current=current)
                if version == current:
                    event("model_sync.not_modified", version=version)
                    return self._finish(SyncOutcome(changed=False, applied_version=current,
                                                    models=len(state.get("models") or {})),
                                        state)
                outcome = self._apply(state, payload, version=version, verified=verified)
            except SyncRejected as exc:
                outcome = SyncOutcome(changed=False, applied_version=current,
                                      error_class=exc.error_class,
                                      models=len(state.get("models") or {}))
                self._last_error = exc.error_class
                event("model_sync.rejected", level="warning",
                      error_class=exc.error_class, version=payload.get("version")
                      if isinstance(payload, Mapping) else None)
            except DjbError as exc:
                outcome = SyncOutcome(changed=False, applied_version=current,
                                      error_class="fetch_failed",
                                      models=len(state.get("models") or {}))
                self._last_error = "fetch_failed"
                event("model_sync.fetch_failed", level="warning",
                      error=type(exc).__name__, code=exc.code)
            except Exception as exc:                           # 防御：线程不炸
                outcome = SyncOutcome(changed=False, applied_version=current,
                                      error_class="payload_invalid",
                                      models=len(state.get("models") or {}))
                self._last_error = "payload_invalid"
                event("model_sync.failed", level="warning", error=type(exc).__name__)
            return self._finish(outcome, state)

    def _apply(self, state: Mapping[str, Any], payload: Mapping[str, Any], *,
               version: int, verified: bool) -> SyncOutcome:
        previous = {str(key): value for key, value in (state.get("models") or {}).items()
                    if isinstance(value, dict)}
        merged: dict[str, dict[str, Any]] = dict(previous)
        drift: list[dict[str, Any]] = []
        unsupported: list[str] = []
        added = 0
        for raw in payload.get("models") or []:
            if not isinstance(raw, Mapping):
                continue
            model = BundleModel.from_dict(raw)
            if not model.canonical:
                continue
            old = previous.get(model.canonical)
            if old is None:
                added += 1
            else:
                for field_name in facts.MODEL_SYNC_DRIFT_FIELDS:
                    before = old.get(field_name)
                    after = getattr(model, field_name)
                    if before != after:
                        drift.append({"canonical": model.canonical, "field": field_name,
                                      "from": before, "to": after})
            merged[model.canonical] = model.to_dict()
            if not model.supported:
                unsupported.append(model.canonical)
        if added == 0 and not drift and not unsupported:
            event("model_sync.no_change", version=version)
        applied = {
            "schema": facts.MODEL_SYNC_SCHEMA,
            "current_version": version,
            "applied_at": iso(self._now()),
            "payload_hash": str(payload.get("payload_hash") or ""),
            "verified": bool(verified),
            "expires_at": payload.get("expires_at"),
            "models": merged,
            "unsupported": sorted(set(unsupported) | {
                item for item in (state.get("unsupported") or []) if item in merged
                and not (merged[item] or {}).get("supported")}),
            "drift": drift,
            "last_error": None,
            "source": "model-catalog",
        }
        self._save_state(applied)
        self._last_error = None
        event("model_sync.applied", version=version, models=len(merged),
              unsupported=len(applied["unsupported"]), drift=len(drift),
              verified=bool(verified))
        return SyncOutcome(changed=True, applied_version=version, error_class=None,
                           verified=bool(verified), models=len(merged),
                           unsupported=list(applied["unsupported"]), drift=drift)

    # ── 上报（只版本/漂移/错误类）
    def _finish(self, outcome: SyncOutcome, state: Mapping[str, Any]) -> dict[str, Any]:
        payload = {"applied_version": int(outcome.applied_version),
                   "drift": list(outcome.drift),
                   "error_class": outcome.error_class}
        if not outcome.changed and outcome.error_class is None:
            # 无变化（304 / 同版本）：不重复上报，省流量
            self._last_outcome = outcome.to_dict()
            return self._last_outcome
        try:
            response = self.api.submit_model_sync_state(payload)
            outcome.reported = bool(response.get("accepted"))
        except DjbError as exc:
            event("model_sync.state_report_failed", level="warning",
                  error=type(exc).__name__, code=exc.code)
        self._last_outcome = outcome.to_dict()
        return self._last_outcome

    # ── 周期拉取
    def run_periodic(self, stop_event: threading.Event) -> None:
        """启动即拉一次，之后每 ``MODEL_SYNC_INTERVAL_S``；失败按 60/300/900/1800 s 退避。"""
        event("model_sync.thread_started", interval_s=facts.MODEL_SYNC_INTERVAL_S)
        backoff = 0
        try:
            while not stop_event.is_set():
                result = self.sync()
                if result.get("error_class") is None:
                    backoff = 0
                    wait = float(facts.MODEL_SYNC_INTERVAL_S)
                else:
                    wait = float(facts.MODEL_SYNC_BACKOFF_S[
                        min(backoff, len(facts.MODEL_SYNC_BACKOFF_S) - 1)])
                    backoff += 1
                self._next_at = iso(self._now_from(wait))
                self._wait(wait, stop_event)
        except Exception as exc:                               # pragma: no cover - 防御
            event("model_sync.thread_crashed", level="warning", error=type(exc).__name__)
        finally:
            event("model_sync.thread_stopped", syncs=self._syncs)

    def _now_from(self, wait: float) -> Any:
        try:
            return self._now() + timedelta(seconds=wait)
        except TypeError:                                      # pragma: no cover - 防御
            return self._now()

    def _wait(self, seconds: float, stop_event: threading.Event) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            return
        stop_event.wait(seconds)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
