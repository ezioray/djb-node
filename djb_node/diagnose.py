# SPDX-License-Identifier: Apache-2.0
"""诊断包（M15 §4.2 / §9 验收 ①：grep 不到凭据与正文）。

包含：manifest（版本 / 状态 / 计数 / 策略摘要）、env（结论 + 下一步）、
config（非敏感）、logs（脱敏副本）、db_summary；**不含**凭据、签名、prompt / response 正文。

包装前做三道：

1. 收集本机所有凭据的**精确值**，逐个替换成 [REDACTED]；
2. 正则脱敏（sk- / hmac-sha256 / Bearer / key=...）；
3. 复扫：仍有命中则**拒绝生成**诊断包（fail-closed），绝不把疑似凭据打进去。

诊断包 0600；默认落在 <home>/diagnostics/。
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from datetime import datetime
from typing import Any, Callable, Iterable

from . import facts
from .config import ClientConfig, Paths
from .envcheck import EnvChecker, EnvReport
from .errors import VerifyError
from .secure import CredentialStore
from .store import Store
from .util import ensure_dir, iso, new_request_id, redact_obj, redact_text, utcnow

MAX_LOG_BYTES = 512 * 1024
BUNDLE_README = """DJB 联脑 · 诊断包（M15 client-runtime）

本诊断包由 djb diagnose 生成，用于排查问题。刻意不包含：

- 任何凭据（node client_key / 上游 API Key / 签名密钥 / 缓冲密钥 / 本机令牌）；
- prompt / response 正文（客户端从不落正文，日志只记元数据）；
- 收据签名原文（只统计条数）。

包含：manifest.json（版本 / 状态 / 计数 / 策略摘要）、env.json（结论 + 下一步）、
config.json（非敏感配置）、db_summary.json、logs/（脱敏日志副本）、
redaction_report.json（脱敏扫描结果）。
"""


class DiagnoseError(VerifyError):
    """诊断包仍含疑似凭据：拒绝生成（fail-closed）。"""


class DiagnoseBuilder:
    def __init__(self, paths: Paths, store: Store, credentials: CredentialStore,
                 config: ClientConfig, *, env_checker: EnvChecker | None = None,
                 version: str = facts.CLIENT_VERSION,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.paths = paths
        self.store = store
        self.credentials = credentials
        self.config = config
        self.env_checker = env_checker or EnvChecker(paths, config, store=store)
        self.version = version
        self._now = now

    # ── 凭据精确值（只用于就地脱敏，不写进任何文件）
    def _secret_values(self) -> list[str]:
        values: list[str] = []
        data = self._credentials_raw()
        for key in ("client_key",):
            value = data.get(key)
            if isinstance(value, str) and value:
                values.append(value)
        upstream = data.get("upstream_keys")
        if isinstance(upstream, dict):
            values.extend([str(item) for item in upstream.values() if item])
        # G10：更新验签的对称 mock 密钥只在 test 环境出现过，但它一旦被写进日志就必须脱敏；
        # 内置 Ed25519 公钥是公开数据，不进脱敏清单（进了会把正常内容打成 [REDACTED]）。
        mock_key = os.environ.get(facts.UPDATE_MOCK_KEY_ENV, "").strip()
        if len(mock_key) >= 32:
            values.append(mock_key)
        for path in (self.paths.buffer_key_file, self.paths.provider_key_file,
                     os.path.join(self.paths.keys_dir, "update_mock.key")):
            try:
                with open(path, "r", encoding="ascii") as handle:
                    text = handle.read().strip()
                if len(text) >= 32:
                    values.append(text)
            except OSError:
                continue
        return [item for item in values if len(item) >= 8]

    def _credentials_raw(self) -> dict[str, Any]:
        try:
            with open(self.paths.credentials_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _scrub(self, text: str, secrets: Iterable[str]) -> str:
        out = text
        for secret in secrets:
            if secret:
                out = out.replace(secret, "[REDACTED]")
        return redact_text(out)

    def _scan(self, text: str, secrets: Iterable[str]) -> list[str]:
        hits: list[str] = []
        for pattern in facts.DIAGNOSE_SCAN_PATTERNS:
            if re.search(pattern, text):
                hits.append("pattern:" + pattern[:24])
        for secret in secrets:
            if secret and secret in text:
                hits.append("exact_secret_value")
                break
        return hits

    # ── 内容
    def manifest(self, env: EnvReport | None = None) -> dict[str, Any]:
        state = self.store.get_kv("daemon_state", default={}) or {}
        update = self.store.update_state()
        policy = self.store.policy()
        running = self.store.get_kv("runtime", default={}) or {}
        return {
            "generated_at": iso(self._now()),
            "module": facts.MODULE_ID,
            "client_version": self.version,
            "protocol_version": facts.PROTOCOL_VERSION,
            "os": facts.PLATFORM,
            "install": self.store.install().to_dict(),
            "daemon_state": state,
            "runtime": {"pid": running.get("pid"), "port": running.get("port"),
                        "started_at": running.get("started_at")},
            "update": {"current_version": update.current_version,
                       "rollback_to": update.rollback_to,
                       "verify_state": update.verify_state,
                       "channel": update.channel},
            "counts": self.store.counts(),
            "policy": _policy_summary(policy),
            "env_summary": (env.to_dict().get("summary") if env else None),
            "credentials": self.credentials.describe(),
        }

    def machine_eval(self) -> dict[str, Any] | None:
        """本机 `djb.machine-eval/1`（采集失败 -> None，诊断包不因此失败）。"""
        try:
            from . import machine_profile
            return machine_profile.build_machine_eval(
                home=getattr(self.paths, "home", None), benchmark=True)
        except Exception:                                # pragma: no cover - 防御
            return None

    def db_summary(self) -> dict[str, Any]:
        profile = self.store.profile()
        return {
            "profile": {"preset": profile.preset,
                        "limits": {"daily_tokens": profile.daily_tokens,
                                   "weekly_tokens": profile.weekly_tokens,
                                   "monthly_tokens": profile.monthly_tokens,
                                   "concurrent": profile.concurrent},
                        "resources": {"cpu_pct": profile.cpu_pct,
                                      "gpu_pct": profile.gpu_pct,
                                      "mem_pct": profile.mem_pct},
                        "yield_enabled": profile.yield_enabled,
                        "schedule_windows": len(profile.schedule)},
            "upstreams": [{"id": item.id, "access_type": item.access_type,
                           "state": item.state, "models": len(item.models),
                           # P2：诊断包里也带 kind / protocol（无凭据、无域名之外的隐私）
                           "upstream_kind": item.upstream_kind,
                           "protocol": item.protocol,
                           "credential_ref_set": bool(item.credential_ref)}
                          for item in self.store.upstreams()],
            "receipts": {"count": self.store.receipt_count(),
                         "stale": self.store.stale_receipt_count(),
                         "oldest_buffered_at": self.store.oldest_receipt_at()},
            "counts": self.store.counts(),
        }

    def build(self, output: str | None = None) -> dict[str, Any]:
        secrets = self._secret_values()
        env = self.env_checker.detect()
        manifest = self.manifest(env)
        db_summary = self.db_summary()
        log_files = self._logs(secrets)
        # M3：机器评估单独成文件（与 env.json 里的 hardware.evidence 冗余是有意的：
        # 支持同学只看 machine.json 就能复现清单，不必翻 env 结构）
        machine = self.machine_eval()
        if machine:
            manifest["machine"] = {"schema": machine.get("schema"),
                                   "platform": machine.get("platform"),
                                   "collected_at": machine.get("collected_at"),
                                   "cpu_model": (machine.get("cpu") or {}).get("model"),
                                   "gpus": [item.get("model") for item
                                            in (machine.get("gpus") or [])],
                                   "tokens_per_second": (machine.get("benchmark") or {})
                                   .get("tokens_per_second")}
        files: dict[str, str] = {
            "README.txt": BUNDLE_README,
            "manifest.json": json.dumps(redact_obj(manifest), ensure_ascii=False, indent=2),
            "env.json": json.dumps(env.to_dict(), ensure_ascii=False, indent=2),
            "machine.json": json.dumps(machine or {"schema": None, "unavailable_reason":
                                                   "machine_profile_failed"},
                                       ensure_ascii=False, indent=2),
            "db_summary.json": json.dumps(db_summary, ensure_ascii=False, indent=2),
            "config.json": json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2),
        }
        for name, content in log_files.items():
            files[name] = content
        # 复扫（fail-closed）
        hits: list[str] = []
        for name, content in files.items():
            scanned = self._scan(content, secrets)
            if scanned:
                hits.extend([name + ":" + item for item in scanned])
        report = {
            "generated_at": iso(self._now()),
            "patterns": list(facts.DIAGNOSE_SCAN_PATTERNS),
            "scanned_files": sorted(files.keys()),
            "exact_secret_values_masked": len(secrets),
            "hits": hits,
        }
        files["redaction_report.json"] = json.dumps(report, ensure_ascii=False, indent=2)
        if hits:
            raise DiagnoseError("诊断包脱敏失败（仍有疑似凭据）：" + ", ".join(hits[:3]))
        path = output or os.path.join(
            self.paths.diagnostics_dir,
            facts.DIAGNOSE_BUNDLE_PREFIX + self._now().strftime("%Y%m%dT%H%M%SZ") + ".zip")
        ensure_dir(os.path.dirname(os.path.abspath(path)) or ".", 0o700)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        os.chmod(path, 0o600)
        size = os.path.getsize(path)
        return {"path": path, "bytes": size, "files": sorted(files.keys()),
                "redaction": {"hits": 0, "exact_secret_values_masked": len(secrets)},
                "request_id": new_request_id()}

    def _logs(self, secrets: Iterable[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        directory = self.paths.logs_dir
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return out
        for name in names:
            if not name.startswith(facts.LOG_FILE):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(MAX_LOG_BYTES)
            except OSError:
                continue
            out["logs/" + name] = self._scrub(text, secrets)
        return out


def _policy_summary(policy: Any) -> dict[str, Any]:
    if policy is None:
        return {"present": False}
    from .policy import summary
    return summary(policy)
