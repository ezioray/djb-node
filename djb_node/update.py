# SPDX-License-Identifier: Apache-2.0
"""自动更新骨架（M15 §1 / §6.4 / §7 / INV-08；S1 允许 mock 源）。

流程：拉清单（签名）-> 按桶判定（5% -> 25% -> 100%，每桶观测 ≥24 h）->
下载 + 校验签名与哈希 -> 安装（S1 记录版本目录）-> 首次启动自检 ->
失败回滚上一版（保留最近 2 版）-> 上报（本骨架只落本地事件）。

硬规则：

* 校验失败**拒绝安装**，版本不变，给失败原因（§9 验收 ④）；
* 失败自动回滚，保留最近 2 个安装包；
* 离线降级：拉不到清单不算失败，保持当前版本并明确提示；
* 提权逐次申请：本模块不做任何提权动作。

S1 签名口径：HMAC-SHA256（mock 源，密钥来自 DJB_UPDATE_SIGNING_KEY 或本地
keys/update_mock.key 0600）；S2 换成 M16 发布签名（Ed25519 + 固定公钥）。见 README CR-M15-06。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from . import facts
from .config import Paths
from .errors import NetworkError, UpdateError
from .httpclient import HttpClient
from .logging_setup import event
from .store import Store, UpdateRecord
from .util import (atomic_write, canonical_json, ensure_dir, hmac_sha256, iso,
                   new_key, parse_iso, read_json, utcnow, write_json)

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SIG_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_MAX_BYTES = 1024 * 1024
ARTIFACT_MAX_BYTES = 256 * 1024 * 1024
MOCK_KEY_ENV = "DJB_UPDATE_SIGNING_KEY"


# ─────────────────────────────────────────────────────────── 清单 / 签名
def sign_manifest(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    """参考实现（mock 源 / 测试）：对 payload 的规范 JSON 做 HMAC-SHA256。"""
    signature = facts.UPDATE_SIGNATURE_PREFIX + hmac_sha256(key, canonical_json(payload))
    return {"payload": payload, "signature": signature}


def verify_manifest(manifest: Any, key: bytes) -> dict[str, Any]:
    """校验清单签名与形状；失败抛 UpdateError（拒绝安装，版本不变）。"""
    if not isinstance(manifest, dict):
        raise UpdateError("更新清单不是 JSON 对象")
    payload = manifest.get("payload")
    signature = manifest.get("signature")
    if not isinstance(payload, dict):
        raise UpdateError("更新清单缺 payload")
    if not isinstance(signature, str) or not SIG_RE.match(signature):
        raise UpdateError("更新清单签名缺失或格式非法")
    expected = facts.UPDATE_SIGNATURE_PREFIX + hmac_sha256(key, canonical_json(payload))
    if expected != signature:
        raise UpdateError("更新清单签名校验失败（拒绝安装）")
    version = payload.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise UpdateError("更新清单 version 非法")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise UpdateError("更新清单缺 artifacts")
    for item in artifacts:
        if not isinstance(item, dict):
            raise UpdateError("artifacts 条目必须是对象")
        if not isinstance(item.get("sha256"), str) or not SHA_RE.match(item["sha256"]):
            raise UpdateError("artifact 缺合法 sha256")
        if not isinstance(item.get("url"), str) or not item["url"]:
            raise UpdateError("artifact 缺 url")
        if not isinstance(item.get("signature"), str) or not SIG_RE.match(item["signature"]):
            raise UpdateError("artifact 缺合法签名")
    return payload


def verify_artifact(data: bytes, artifact: dict[str, Any], key: bytes) -> None:
    digest = hashlib.sha256(data).hexdigest()
    if digest != artifact.get("sha256"):
        raise UpdateError("下载包哈希不一致（拒绝安装）")
    expected = facts.UPDATE_SIGNATURE_PREFIX + hmac_sha256(key, data)
    if expected != artifact.get("signature"):
        raise UpdateError("下载包签名校验失败（拒绝安装）")


def select_artifact(payload: dict[str, Any], *, os_name: str = facts.PLATFORM,
                    arch: str | None = None) -> dict[str, Any]:
    import platform
    arch = arch or platform.machine() or "x86_64"
    for item in payload.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("os") or "") != os_name:
            continue
        if str(item.get("arch") or "") not in (arch, "any", ""):
            continue
        return dict(item)
    raise UpdateError("没有匹配本机 os/arch 的安装包（" + os_name + "/" + arch + "）")


def bucket_eligible(bucket: int, payload: dict[str, Any], *,
                    now: datetime | None = None) -> tuple[bool, str]:
    """分桶灰度：wave 1/2/3 对应 5% / 25% / 100%，每桶观测 ≥24 h。"""
    moment = now or utcnow()
    rollout = payload.get("rollout")
    if not isinstance(rollout, dict):
        return True, "no_rollout"
    try:
        wave = int(rollout.get("wave") or len(facts.UPDATE_BUCKET_WAVES))
        since = parse_iso(rollout.get("since"))
    except (TypeError, ValueError):
        return False, "bad_rollout"
    wave = max(1, min(wave, len(facts.UPDATE_BUCKET_WAVES)))
    pct = int(facts.UPDATE_BUCKET_WAVES[wave - 1])
    if int(bucket) >= pct:
        return False, "not_in_wave_" + str(wave) + "_(" + str(pct) + "%)"
    if since is not None:
        observe_needed = facts.UPDATE_BUCKET_OBSERVE_S * (wave - 1)
        if (moment - since) < timedelta(seconds=observe_needed):
            return False, "observing_wave_" + str(wave)
    return True, "wave_" + str(wave)


# ─────────────────────────────────────────────────────────── 更新管理
class UpdateManager:
    def __init__(self, paths: Paths, store: Store, *, source: str = "",
                 channel: str = facts.DEFAULT_UPDATE_CHANNEL,
                 client_version: str = facts.CLIENT_VERSION,
                 http: HttpClient | None = None,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.paths = paths
        self.store = store
        self.source = source
        self.channel = channel if channel in facts.UPDATE_CHANNELS \
            else facts.DEFAULT_UPDATE_CHANNEL
        self.client_version = client_version
        self.http = http or HttpClient(timeout=15.0)
        self._now = now

    # ── 签名密钥（mock；S2 换固定公钥）
    def signing_key(self) -> bytes:
        raw = os.environ.get(MOCK_KEY_ENV)
        if raw:
            try:
                key = bytes.fromhex(raw.strip())
                if len(key) >= 32:
                    return key
            except ValueError:
                pass
        path = os.path.join(self.paths.keys_dir, "update_mock.key")
        try:
            with open(path, "r", encoding="ascii") as handle:
                key = bytes.fromhex(handle.read().strip())
            if len(key) >= 32:
                return key
        except (OSError, ValueError):
            pass
        ensure_dir(self.paths.keys_dir, 0o700)
        key = new_key()
        atomic_write(path, key.hex(), mode=0o600)
        event("update.mock_key_created", level="warning", path=path)
        return key

    # ── 清单获取
    def _load_manifest(self) -> dict[str, Any] | None:
        """source 为空 -> 内置 mock（本机 versions 目录旁的 manifest.json）。"""
        source = (self.source or "").strip()
        if not source:
            candidate = os.path.join(self.paths.versions_dir, "mock-manifest.json")
            if not os.path.exists(candidate):
                return None
            return read_json(candidate)
        parsed = urlparse(source)
        if parsed.scheme in ("http", "https"):
            response = self.http.request("GET", source, timeout=15.0)
            if response.status != 200:
                raise NetworkError("更新清单拉取失败（HTTP " + str(response.status) + "）",
                                   code=facts.LOCAL_ERROR_CODES["upstream_error"])
            if len(response.body) > MANIFEST_MAX_BYTES:
                raise UpdateError("更新清单过大（> " + str(MANIFEST_MAX_BYTES) + " 字节）")
            try:
                return json.loads(response.body.decode("utf-8"))
            except ValueError as exc:
                raise UpdateError("更新清单不是合法 JSON") from exc
        path = source
        if parsed.scheme == "file":
            path = parsed.path
        if os.path.isdir(path):
            path = os.path.join(path, "manifest.json")
        return read_json(path)

    def _download(self, artifact: dict[str, Any]) -> bytes:
        raw = str(artifact["url"])
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https"):
            response = self.http.request("GET", raw, timeout=60.0)
            if response.status != 200:
                raise UpdateError("下载安装包失败（HTTP " + str(response.status) + "）")
            if len(response.body) > ARTIFACT_MAX_BYTES:
                raise UpdateError("安装包过大（> " + str(ARTIFACT_MAX_BYTES) + " 字节）")
            return response.body
        path = raw
        if parsed.scheme == "file":
            path = parsed.path
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise UpdateError("读取安装包失败：" + first_line(str(exc))) from exc
        if len(data) > ARTIFACT_MAX_BYTES:
            raise UpdateError("安装包过大（> " + str(ARTIFACT_MAX_BYTES) + " 字节）")
        return data

    # ── 状态 / 检查
    def status(self) -> dict[str, Any]:
        state = self.store.update_state()
        install = self.store.install()
        return {"channel": self.channel, "current_version": install.version,
                "recorded_current": state.current_version, "next_version": state.next_version,
                "rollback_to": state.rollback_to, "verify_state": state.verify_state,
                "bucket": install.bucket, "checked_at": state.checked_at,
                "source": self.source or "(mock: versions/mock-manifest.json)"}

    def check(self) -> dict[str, Any]:
        moment = self._now()
        base: dict[str, Any] = {"checked_at": iso(moment), "channel": self.channel}
        try:
            manifest = self._load_manifest()
        except NetworkError as exc:
            base.update({"state": "offline", "message": str(exc),
                         "current_version": self.store.install().version})
            self._record("offline", None, None)
            return base
        except UpdateError as exc:
            base.update({"state": "invalid", "message": str(exc),
                         "current_version": self.store.install().version})
            self._record("invalid", None, None)
            return base
        if manifest is None:
            base.update({"state": "no_source", "message": "未配置更新源，也没有本地 mock 清单",
                         "current_version": self.store.install().version})
            self._record("no_source", None, None)
            return base
        try:
            payload = verify_manifest(manifest, self.signing_key())
        except UpdateError as exc:
            base.update({"state": "failed_verify", "message": str(exc),
                         "current_version": self.store.install().version})
            self._record("failed_verify", None, None)
            return base
        version = str(payload["version"])
        channel = str(payload.get("channel") or facts.DEFAULT_UPDATE_CHANNEL)
        if channel != self.channel:
            base.update({"state": "channel_mismatch",
                         "message": "清单频道为 " + channel + "，本机为 " + self.channel,
                         "current_version": self.store.install().version})
            self._record("channel_mismatch", None, version)
            return base
        base.update({"manifest_version": version,
                     "current_version": self.store.install().version,
                     "released_at": payload.get("released_at")})
        if _version_tuple(version) <= _version_tuple(self.store.install().version):
            base.update({"state": "up_to_date", "message": "已是最新版本（不做降级）"})
            self._record("up_to_date", None, version)
            return base
        install = self.store.install()
        eligible, reason = bucket_eligible(install.bucket, payload, now=moment)
        base.update({"eligible": eligible, "bucket": install.bucket, "rollout_reason": reason})
        if not eligible:
            base.update({"state": "not_eligible",
                         "message": "未命中灰度桶（" + reason + "），本轮不更新"})
            self._record("not_eligible", None, version)
            return base
        try:
            artifact = select_artifact(payload)
        except UpdateError as exc:
            base.update({"state": "no_artifact", "message": str(exc)})
            self._record("no_artifact", None, version)
            return base
        base.update({"state": "eligible", "artifact": {
            "url": artifact.get("url"), "sha256": artifact.get("sha256"),
            "size": artifact.get("size")}})
        self._record("eligible", artifact, version)
        return base

    def _record(self, verify_state: str, artifact: dict[str, Any] | None,
                version: str | None) -> None:
        state = self.store.update_state()
        state.verify_state = verify_state
        state.checked_at = iso(self._now())
        state.channel = self.channel
        state.bucket = self.store.install().bucket
        if version:
            state.next_version = version
        self.store.save_update_state(state)

    # ── 安装 / 回滚
    def apply(self, *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        check = self.check()
        if check.get("state") == "offline":
            return {"installed": False, "state": "offline",
                    "message": "离线：保持当前版本 " + str(check.get("current_version")),
                    "current_version": check.get("current_version")}
        if check.get("state") != "eligible":
            return {"installed": False, "state": check.get("state"),
                    "message": check.get("message") or "当前不需要更新",
                    "current_version": check.get("current_version")}
        key = self.signing_key()
        if manifest is None:
            try:
                manifest = self._load_manifest()
            except (NetworkError, UpdateError) as exc:
                return {"installed": False, "state": "offline",
                        "message": "离线：保持当前版本（" + str(exc) + "）"}
        if manifest is None:
            return {"installed": False, "state": "no_source", "message": "没有更新清单"}
        try:
            payload = verify_manifest(manifest, key)
            version = str(payload["version"])
            artifact = select_artifact(payload)
            data = self._download(artifact)
            verify_artifact(data, artifact, key)
        except (UpdateError, NetworkError) as exc:
            self._record("failed_verify", None, None)
            event("update.verify_failed", level="warning", error=str(exc)[:120])
            return {"installed": False, "state": "failed_verify", "message": str(exc)}
        previous = self.store.install().version
        target_dir = os.path.join(self.paths.versions_dir, version)
        try:
            ensure_dir(target_dir, 0o700)
            atomic_write(os.path.join(target_dir, "artifact.bin"), data, mode=0o600)
            write_json(os.path.join(target_dir, "manifest.json"),
                       {"payload": payload, "signature": manifest.get("signature")},
                       mode=0o600)
            # 首次启动自检：读回文件重新校验（防写坏 / 防半包）
            with open(os.path.join(target_dir, "artifact.bin"), "rb") as handle:
                readback = handle.read()
            verify_artifact(readback, artifact, key)
            self._activate(version, previous)
        except (OSError, UpdateError) as exc:
            self._rollback_files(target_dir)
            self._record("failed_install", None, None)
            event("update.install_failed", level="warning", error=str(exc)[:120])
            return {"installed": False, "state": "failed_install", "message": str(exc),
                    "current_version": self.store.install().version}
        self._prune()
        event("update.installed", version=version, previous=previous)
        return {"installed": True, "state": "installed", "version": version,
                "previous_version": previous, "current_version": version,
                "kept_versions": self._versions()}

    def _activate(self, version: str, previous: str) -> None:
        record = self.store.update_state()
        record.current_version = version
        record.rollback_to = previous
        record.next_version = None
        record.verify_state = "verified"
        record.channel = self.channel
        record.checked_at = iso(self._now())
        self.store.save_update_state(record)
        self.store.update_install(version=version, channel=self.channel)

    def _rollback_files(self, target_dir: str) -> None:
        shutil.rmtree(target_dir, ignore_errors=True)

    def rollback(self) -> dict[str, Any]:
        record = self.store.update_state()
        target = record.rollback_to
        current = self.store.install().version
        if not target:
            return {"rolled_back": False, "reason": "no_rollback_target"}
        target_dir = os.path.join(self.paths.versions_dir, target)
        if not os.path.isdir(target_dir):
            self._record("rollback_missing", None, None)
            return {"rolled_back": False, "reason": "target_missing", "target": target}
        record.current_version = target
        record.rollback_to = None
        record.verify_state = "rolled_back"
        record.checked_at = iso(self._now())
        self.store.save_update_state(record)
        self.store.update_install(version=target)
        event("update.rolled_back", from_version=current, to_version=target)
        return {"rolled_back": True, "from_version": current, "to_version": target,
                "current_version": target}

    # ── 版本目录
    def _versions(self) -> list[str]:
        try:
            names = [name for name in os.listdir(self.paths.versions_dir)
                     if os.path.isdir(os.path.join(self.paths.versions_dir, name))
                     and VERSION_RE.match(name)]
        except OSError:
            return []
        return sorted(names, key=_version_tuple)

    def _prune(self) -> list[str]:
        """保留最近 2 个安装包（按版本号），返回被删除的版本。"""
        versions = self._versions()
        removed: list[str] = []
        while len(versions) > facts.UPDATE_KEEP_VERSIONS:
            oldest = versions.pop(0)
            shutil.rmtree(os.path.join(self.paths.versions_dir, oldest), ignore_errors=True)
            removed.append(oldest)
        return removed


def _version_tuple(text: str) -> tuple[int, int, int]:
    try:
        parts = [int(item) for item in str(text).split(".")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0], parts[1], parts[2]
    except (TypeError, ValueError):
        return (0, 0, 0)


def first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0][:160] if (text or "").strip() else ""
