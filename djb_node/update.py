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

S1 签名口径（G10 已换）：信任锚是**内置 Ed25519 公钥**（`facts.UPDATE_PUBLIC_KEY_HEX`），
私钥只在发布侧 / E2E 的 `~/.djb/secrets/e2e/update-signing.key`（0600），客户端永不持有；
test-only 的对称 HMAC mock 必须同时满足 `DJB_ENV=test` + 开关
`client.update.mock_signature.enabled`（三字段、默认关）+ 显式 `DJB_UPDATE_SIGNING_KEY`；
其余情况一律按内置公钥验签，公钥缺失/非法 → fail-closed（`failed_verify`，版本不变，
**绝不本地自生成密钥自签通过**）。见 README CR-M15-06 / 审计 G10、P1-3。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from . import ed25519, facts, update_source
from .config import Paths, feature_flag, load_config
from .errors import NetworkError, UpdateError
from .httpclient import HttpClient
from .logging_setup import event
from .store import Store, UpdateRecord
from .util import (atomic_write, canonical_json, ensure_dir, hmac_sha256, iso,
                   parse_iso, read_json, utcnow, write_json)

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
#: 新信任锚只认 ed25519；hmac 形态保留解析能力，但只在 test-only mock 分支被接受。
SIG_RE = re.compile(r"^(?:hmac-sha256:[0-9a-f]{64}|ed25519:[0-9a-f]{128})$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_MAX_BYTES = 1024 * 1024
ARTIFACT_MAX_BYTES = 256 * 1024 * 1024
MOCK_KEY_ENV = facts.UPDATE_MOCK_KEY_ENV


# ─────────────────────────────────────────────────────────── 信任锚（G10）
@dataclass(frozen=True)
class TrustAnchor:
    """验签用的信任锚（方案 + 材料 + 可展示标识）。

    ``material`` 只在内存里用：ed25519 是**公钥**（公开数据）；hmac 是 test-only 对称密钥。
    ``to_dict`` 只给标识与指纹，**任何路径都不回显材料**（状态 / 日志 / 诊断包）。
    """

    scheme: str
    material: bytes
    key_id: str
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {"scheme": self.scheme, "key_id": self.key_id,
                "key_fingerprint": self.fingerprint}


def _fingerprint(material: bytes) -> str:
    return hashlib.sha256(material).hexdigest()[:12]


def _anchor_from_hex(text: Any, *, key_id: str, where: str) -> TrustAnchor:
    raw = str(text or "").strip()
    if not raw:
        raise UpdateError(where + "公钥缺失：拒绝验签（fail-closed；不会本地自生成密钥）")
    if not ed25519.is_valid_public_key_hex(raw):
        raise UpdateError(where + "公钥非法（不是合法 Ed25519 公钥）：拒绝验签（fail-closed）")
    material = bytes.fromhex(raw)
    return TrustAnchor(facts.UPDATE_SIGNATURE_ALG_ED25519, material, key_id,
                       _fingerprint(material))


def ed25519_anchor(public_key_hex: str | None = None, *,
                   key_id: str | None = None) -> TrustAnchor:
    """**primary 单钥视图**；显式传 hex/key_id 时用它构造（测试/工具用）。

    公钥缺失/非法 → 抛 UpdateError（fail-closed）。生产验签请用 keyring_anchors()
    （钥环 + 双钥过渡 + 吊销）。
    """
    if public_key_hex is not None or key_id is not None:
        return _anchor_from_hex(public_key_hex, key_id=key_id or facts.UPDATE_KEY_ID,
                               where="内置更新")
    primary = keyring_anchors().primary()
    if primary is None:
        raise UpdateError("内置更新钥环里没有 primary 公钥：拒绝验签（fail-closed）")
    return primary


@dataclass(frozen=True)
class AnchorSet:
    """信任钥环视图：primary / transition 双钥 + 吊销 + 过渡到期（RC8）。

    口径：

    * **清单验签精确取钥**：payload 声明 key_id 时用 resolve()（未知 / 已吊销 /
      过渡到期一律拒，不"拿别的钥试一下"）；
    * **制品验签**用 verify_any（制品条目没有 key_id），但只试「可用钥」；
    * 吊销优先于 state；transition 锚过了 UPDATE_TRANSITION_UNTIL 当天即失效
      （期限写坏按过期处理，fail-closed）。
    """

    anchors: tuple[TrustAnchor, ...]
    states: tuple[tuple[str, str], ...] = ()
    revoked: tuple[str, ...] = ()
    transition_until: str = ""
    placeholders: tuple[str, ...] = ()
    transition_override: bool = False

    def state_of(self, key_id: str) -> str:
        for known, state in self.states:
            if known == key_id:
                return state
        return "unknown"

    def _expired(self, key_id: str) -> bool:
        if self.state_of(key_id) != "transition" or not self.transition_until:
            return False
        try:
            last_day = date.fromisoformat(str(self.transition_until))
        except ValueError:
            return True                      # 期限写坏 = 视为过期（fail-closed）
        return utcnow().date() > last_day

    def unusable_reason(self, key_id: str) -> str | None:
        """该 key_id 不可用的原因；可用返回 None（吊销 > 占位 > 过渡到期）。"""
        if key_id in self.revoked:
            # 应急覆盖：仅 transition 锚、且硬上限日期未到（owner 书面 + incident 才允许）
            if self.transition_override and self.state_of(key_id) == "transition":
                if self._expired(key_id):
                    return "过渡期已结束（" + str(self.transition_until) + "）"
                return None
            return "已吊销"
        if key_id in self.placeholders:
            return "占位公钥（owner 尚未注入最终生产公钥）"
        if self._expired(key_id):
            return "过渡期已结束（" + str(self.transition_until) + "）"
        return None

    def usable(self) -> tuple[TrustAnchor, ...]:
        return tuple(a for a in self.anchors if self.unusable_reason(a.key_id) is None)

    def resolve(self, key_id: str) -> TrustAnchor:
        for anchor in self.anchors:
            if anchor.key_id == key_id:
                why = self.unusable_reason(key_id)
                if why is not None:
                    raise UpdateError("清单 key_id（" + key_id + "）" + why + "（拒绝安装）")
                return anchor
        known = ", ".join(a.key_id for a in self.anchors) or "(空)"
        raise UpdateError("清单 key_id（" + key_id + "）不在本机信任钥环里（已知："
                          + known + "）（拒绝安装）")

    def primary(self) -> TrustAnchor | None:
        for anchor in self.anchors:
            if self.state_of(anchor.key_id) == "primary":
                return anchor
        return self.anchors[0] if self.anchors else None

    def to_dict(self) -> dict[str, Any]:
        primary = self.primary()
        return {
            "scheme": facts.UPDATE_SIGNATURE_ALG_ED25519,
            "key_id": primary.key_id if primary else None,
            "key_fingerprint": primary.fingerprint if primary else None,
            "keys": [{"key_id": a.key_id, "state": self.state_of(a.key_id),
                      "fingerprint": a.fingerprint,
                      "status": self.unusable_reason(a.key_id) or "usable"}
                     for a in self.anchors],
            "revoked": list(self.revoked),
            "transition_until": self.transition_until,
        }


def keyring_anchors() -> AnchorSet:
    """从 facts.UPDATE_KEYRING 构造信任钥环（**唯一信任来源**，fail-closed）。"""
    entries = tuple(getattr(facts, "UPDATE_KEYRING", ()) or ())
    if not entries:
        raise UpdateError("内置更新钥环为空：拒绝验签（fail-closed）")
    anchors: list[TrustAnchor] = []
    states: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise UpdateError("内置更新钥环条目不是对象：拒绝验签（fail-closed）")
        key_id = str(entry.get("key_id") or "").strip()
        if not key_id:
            raise UpdateError("内置更新钥环有条目缺 key_id：拒绝验签（fail-closed）")
        anchors.append(_anchor_from_hex(entry.get("pubkey_hex"), key_id=key_id,
                                        where="内置更新密钥 " + key_id + " 的"))
        states.append((key_id, str(entry.get("state") or "unknown")))
    placeholders = tuple(
        str(entry.get("key_id") or "").strip()
        for entry in entries
        if str(entry.get("placeholder") or "").lower() in ("1", "true", "yes")
    )
    return AnchorSet(
        anchors=tuple(anchors),
        states=tuple(states),
        revoked=tuple(getattr(facts, "UPDATE_REVOKED_KEY_IDS", ()) or ()),
        transition_until=str(getattr(facts, "UPDATE_TRANSITION_UNTIL", "") or ""),
        placeholders=placeholders,
        transition_override=bool(getattr(facts, "UPDATE_TRANSITION_OVERRIDE", False)),
    )


def anchor_release_ready(anchor: Any = None) -> tuple[bool, str]:
    """**发布门禁**：primary 锚必须是 owner/离线签名机给的最终公钥（非占位）。

    返回 (ready, reason)；release 侧（ops/release/sign-update-manifest.py、
    manifest-gen.py --check）必须调这个函数，False 就拒绝签名/拒绝发版。
    """
    try:
        anchor_set = _as_anchor(anchor) if anchor is not None else keyring_anchors()
    except UpdateError as exc:
        return False, "钥环不可用：" + str(exc)
    primary = anchor_set.primary()
    if primary is None:
        return False, "钥环里没有 primary 公钥"
    why = anchor_set.unusable_reason(primary.key_id)
    if why is not None:
        return False, "primary（" + primary.key_id + "）" + why
    if anchor_set.state_of(primary.key_id) != "primary":
        return False, "primary 锚 state 不是 primary"
    return True, "primary=" + primary.key_id + " fp=" + primary.fingerprint


def mock_anchor(key: bytes, *, key_id: str = "mock-test-only") -> TrustAnchor:
    """test-only HMAC 信任锚（只允许 mock 分支构造）。"""
    if not isinstance(key, (bytes, bytearray)) or len(key) < 32:
        raise UpdateError("mock 验签密钥必须是 ≥32 字节（hex 解码后）")
    return TrustAnchor(facts.UPDATE_SIGNATURE_ALG, bytes(key), key_id,
                       _fingerprint(bytes(key)))


def _as_anchor(value: Any) -> AnchorSet:
    """归一化验签锚：AnchorSet / 单 TrustAnchor / 裸 bytes（test-only HMAC）。"""
    if isinstance(value, AnchorSet):
        return value
    if isinstance(value, TrustAnchor):
        return AnchorSet((value,))
    if isinstance(value, (bytes, bytearray)):
        return AnchorSet((mock_anchor(bytes(value)),))
    raise UpdateError("验签锚缺失（需要 TrustAnchor / AnchorSet）")


def _verify_any(data: bytes, signature: str, anchor_set: AnchorSet,
                *, what: str) -> TrustAnchor:
    """任一「可用钥」验过即可（制品条目没有 key_id）；全失败才拒。"""
    usable = anchor_set.usable()
    if not usable:
        raise UpdateError(what + "验签失败：信任钥环里没有可用公钥（全部吊销/过期或为空）")
    if len(usable) == 1:
        # 单钥时保留原始错误（形态非法 / 校验失败），诊断信息不丢
        _check_signature(data, signature, usable[0], what=what)
        return usable[0]
    errors: list[str] = []
    for anchor in usable:
        try:
            _check_signature(data, signature, anchor, what=what)
            return anchor
        except UpdateError as exc:
            errors.append(str(exc))
    if errors and all("形态非法" in item for item in errors):
        raise UpdateError(what + "签名形态非法（要求 " + facts.UPDATE_ED25519_PREFIX + "…）")
    raise UpdateError(what + "签名校验失败（拒绝安装）")


# ─────────────────────────────────────────────────────────── 清单 / 签名
def sign_manifest(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    """参考实现（**仅 test-only mock / 测试**）：对 payload 的规范 JSON 做 HMAC-SHA256。"""
    signature = facts.UPDATE_SIGNATURE_PREFIX + hmac_sha256(key, canonical_json(payload))
    return {"payload": payload, "signature": signature}


def _check_signature(data: bytes, signature: str, anchor: TrustAnchor, *, what: str) -> None:
    """统一「签名形态 + 验签」（ed25519 走内置公钥；hmac 只走 test-only mock 锚）。"""
    if anchor.scheme == facts.UPDATE_SIGNATURE_ALG_ED25519:
        prefix = facts.UPDATE_ED25519_PREFIX
        if not signature.startswith(prefix):
            raise UpdateError(what + "签名形态非法（要求 " + prefix + "…）")
        try:
            raw = bytes.fromhex(signature[len(prefix):])
        except ValueError as exc:
            raise UpdateError(what + "签名不是合法 hex") from exc
        if not ed25519.verify(anchor.material, data, raw):
            raise UpdateError(what + "签名校验失败（拒绝安装）")
        return
    prefix = facts.UPDATE_SIGNATURE_PREFIX
    if not signature.startswith(prefix):
        raise UpdateError(what + "签名形态非法（mock 分支要求 " + prefix + "…）")
    if prefix + hmac_sha256(anchor.material, data) != signature:
        raise UpdateError(what + "签名校验失败（拒绝安装）")


def verify_manifest(manifest: Any, anchor: Any) -> dict[str, Any]:
    """校验清单签名与形状；失败抛 UpdateError（拒绝安装，版本不变）。"""
    if not isinstance(manifest, dict):
        raise UpdateError("更新清单不是 JSON 对象")
    payload = manifest.get("payload")
    signature = manifest.get("signature")
    if not isinstance(payload, dict):
        raise UpdateError("更新清单缺 payload")
    if not isinstance(signature, str) or not SIG_RE.match(signature):
        raise UpdateError("更新清单签名缺失或格式非法")
    anchor_set = _as_anchor(anchor)
    # 形态先行：钥环只要有 ed25519 锚，hmac 形态一律按"签名降级"拒（不等到取钥阶段）
    if (any(item.scheme == facts.UPDATE_SIGNATURE_ALG_ED25519 for item in anchor_set.anchors)
            and not signature.startswith(facts.UPDATE_ED25519_PREFIX)):
        raise UpdateError("更新清单签名形态非法（要求 "
                          + facts.UPDATE_ED25519_PREFIX + "…）")
    body = canonical_json(payload).encode("utf-8")
    declared_key = payload.get("key_id")
    if declared_key is not None:
        # RC8 钥环口径：精确取钥（未知 / 已吊销 / 过渡到期一律拒，不"换把钥试试"）
        matched = anchor_set.resolve(str(declared_key))
        _check_signature(body, signature, matched, what="更新清单")
    else:
        # 旧清单（无 key_id）容忍：任一可用钥验过即可（轮换前的兼容窗口）
        _verify_any(body, signature, anchor_set, what="更新清单")
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


def verify_artifact(data: bytes, artifact: dict[str, Any], anchor: Any,
                    *, key_id: str | None = None) -> None:
    """先比 sha256（便宜、快速拒），再验签（贵）——**顺序不要调换**。

    key_id 给定时只认该钥（清单已声明 key_id 时用，绑定"清单钥 == 制品钥"）；
    不给则在钥环的可用钥里任一验过即可（旧清单兼容）。
    """
    digest = hashlib.sha256(data).hexdigest()
    if digest != artifact.get("sha256"):
        raise UpdateError("下载包哈希不一致（拒绝安装）")
    signature = artifact.get("signature")
    if not isinstance(signature, str):
        raise UpdateError("下载包缺签名（拒绝安装）")
    anchor_set = _as_anchor(anchor)
    if key_id is not None:
        _check_signature(data, signature, anchor_set.resolve(key_id), what="下载包")
        return
    _verify_any(data, signature, anchor_set, what="下载包")


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
                 config: Any = None,
                 edition: str = "",
                 now: Callable[[], datetime] = utcnow) -> None:
        self.paths = paths
        self.store = store
        self.source = source
        self.channel = channel if channel in facts.UPDATE_CHANNELS \
            else facts.DEFAULT_UPDATE_CHANNEL
        self.client_version = client_version
        self.edition = edition or ""
        self.http = http or HttpClient(timeout=15.0)
        self._config = config
        self._now = now

    # ── 信任锚（G10：内置公钥；mock 三条件齐备才允许对称分支）
    def _effective_config(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            return load_config(self.paths)
        except Exception:                                  # 配置读不到 → 按默认（mock 关）
            return None

    def mock_requested(self) -> bool:
        """是否**被要求**走 test-only mock（环境门 + 开关；不含密钥是否就绪）。"""
        env = os.environ.get(facts.UPDATE_TEST_ENV, "").strip().lower()
        if env != facts.UPDATE_TEST_ENV_VALUE:
            return False
        return bool(feature_flag(self._effective_config(), facts.UPDATE_MOCK_FLAG))

    def trust_anchor(self) -> Any:
        """本次验签的信任锚（AnchorSet）；不可用一律抛 UpdateError（fail-closed）。

        判定顺序（都不满足 → 内置钥环）：

        1. DJB_ENV=test **且** 开关 client.update.mock_signature.enabled 打开
           **且** 显式给了 DJB_UPDATE_SIGNING_KEY → test-only HMAC 锚（单钥）；
        2. 否则 → facts.UPDATE_KEYRING 构造的钥环（primary + transition + 吊销）。

        开启了 mock 却没给密钥 → 直接失败（不静默回退、不本地生成）。
        """
        if self.mock_requested():
            raw = os.environ.get(MOCK_KEY_ENV, "").strip()
            if not raw:
                raise UpdateError("mock 验签已开启但未提供 " + MOCK_KEY_ENV
                                  + "：拒绝自签 / 拒绝静默回退（fail-closed）")
            try:
                key = bytes.fromhex(raw)
            except ValueError as exc:
                raise UpdateError("mock 验签密钥不是合法 hex（fail-closed）") from exc
            return AnchorSet((mock_anchor(key),))
        return keyring_anchors()

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
                # RC8：制品下载失败是**网络类**（DL 502/超时/CDN 故障），
                # 不能记成 failed_verify（会误导监控，也会把退出码算成 7）。
                raise NetworkError("下载安装包失败（HTTP " + str(response.status) + "）",
                                   code=facts.LOCAL_ERROR_CODES["upstream_error"])
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
        # M10「版本与签名状态如实」：把当前**实际会用**的信任锚标识展示出来
        # （只有 scheme / key_id / 指纹，绝无密钥材料）。
        try:
            anchor_info = self.trust_anchor().to_dict()
            anchor_available = True
            anchor_error = None
        except UpdateError as exc:
            anchor_info = {"scheme": None, "key_id": None, "key_fingerprint": None}
            anchor_available = False
            anchor_error = str(exc)
        resolved = update_source.resolve(self.source, edition=self.edition)
        return {"channel": self.channel, "current_version": install.version,
                "recorded_current": state.current_version, "next_version": state.next_version,
                "rollback_to": state.rollback_to, "verify_state": state.verify_state,
                "bucket": install.bucket, "checked_at": state.checked_at,
                "source": self.source or "(mock: versions/mock-manifest.json)",
                # 0.3.1（RC8.3-C）：更新源显式化 —— 只加字段，不动既有键
                "source_kind": resolved.kind, "source_origin": resolved.origin,
                "edition": resolved.edition, "source_channel": resolved.channel,
                "signature": {**anchor_info, "available": anchor_available,
                              "error": anchor_error,
                              "mock": bool(self.mock_requested())}}

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
            anchor = self.trust_anchor()
        except UpdateError as exc:
            # 信任锚缺失/非法：拒绝验签（fail-closed）。版本不变，绝不自生成密钥自签。
            event("update.trust_anchor_unavailable", level="error", error=str(exc)[:120])
            base.update({"state": "failed_verify", "message": str(exc),
                         "current_version": self.store.install().version})
            self._record("failed_verify", None, None)
            return base
        try:
            payload = verify_manifest(manifest, anchor)
        except UpdateError as exc:
            base.update({"state": "failed_verify", "message": str(exc),
                         "current_version": self.store.install().version,
                         "signature": anchor.to_dict()})
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
                     "released_at": payload.get("released_at"),
                     "signature": anchor.to_dict()})
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
        try:
            key = self.trust_anchor()
        except UpdateError as exc:
            self._record("failed_verify", None, None)
            return {"installed": False, "state": "failed_verify", "message": str(exc)}
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
            verify_artifact(data, artifact, key, key_id=payload.get("key_id"))
        except NetworkError as exc:
            # RC8：下载阶段的网络失败（502/超时）是网络类（退出码 6），不是验签失败
            self._record("offline", None, None)
            event("update.download_failed", level="warning", error=str(exc)[:120])
            return {"installed": False, "state": "download_failed", "message": str(exc),
                    "current_version": self.store.install().version}
        except UpdateError as exc:
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
            verify_artifact(readback, artifact, key, key_id=payload.get("key_id"))
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


def apply_exit_code(result: dict[str, Any]) -> int:
    """apply() 结果 → 进程退出码（RC8：网络类失败不再算校验失败）。

    * failed_verify → 7（EXIT_VERIFY）
    * download_failed → 6（EXIT_NETWORK）
    * 其它（installed / up_to_date / offline(清单拉不到) / not_eligible）→ 0
    """
    state = str((result or {}).get("state") or "")
    if state == "failed_verify":
        return facts.EXIT_VERIFY
    if state == "download_failed":
        return facts.EXIT_NETWORK
    return 0


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
