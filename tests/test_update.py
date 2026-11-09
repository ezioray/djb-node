# SPDX-License-Identifier: Apache-2.0
"""自动更新：清单/安装包验签（G10：内置 Ed25519 公钥 + test-only mock）与安装/回滚。

口径：
* 生产信任锚 = `facts.UPDATE_PUBLIC_KEY_HEX`（内置公钥），单测用测试密钥对 patch 它；
* mock 分支 = `DJB_ENV=test` + 开关 + 显式 `DJB_UPDATE_SIGNING_KEY` 三条件齐备；
* 任何「公钥缺失 / 签名形态降级 / key_id 不一致」都必须 failed_verify，且**不安装**。
"""
import hashlib
import json
import os
from datetime import timedelta

import pytest

from djb_node import ed25519, facts, update
from djb_node.config import ClientConfig
from djb_node.errors import UpdateError
from djb_node.update import (UpdateManager, bucket_eligible, ed25519_anchor, mock_anchor,
                             select_artifact, sign_manifest, verify_artifact,
                             verify_manifest)
from tests.update_fixtures import (TEST_KEY_ID, TEST_PUBLIC_KEY, TEST_SEED, build_manifest,
                                   build_source, enable_mock, mock_source, sign_bytes,
                                   use_test_anchor)


# ─────────────────────────────────────────────────────────── 清单 / 签名（ed25519）
def test_manifest_signature_required(monkeypatch):
    use_test_anchor(monkeypatch)
    anchor = ed25519_anchor()
    manifest = build_manifest()
    payload = verify_manifest(manifest, anchor)
    assert payload["version"] == "0.2.0"
    # 换一把公钥 -> 拒
    other = ed25519.public_key_from_seed(bytes.fromhex("11" * 32))
    with pytest.raises(UpdateError):
        verify_manifest(manifest, mock_anchor(b"k" * 32, key_id=other.hex()))
    # 篡改 payload -> 拒
    tampered = json.loads(json.dumps(manifest))
    tampered["payload"]["version"] = "9.9.9"
    with pytest.raises(UpdateError):
        verify_manifest(tampered, anchor)
    # 形态非法 / 缺字段 -> 拒
    for broken in ({"payload": {}}, None, [1, 2, 3],
                   {"payload": {}, "signature": "ed25519:" + "0" * 127},
                   {"payload": {}, "signature": "hmac-sha256:" + "0" * 64}):
        with pytest.raises(UpdateError):
            verify_manifest(broken, anchor)


def test_ed25519_signature_shape_is_enforced(monkeypatch):
    """hmac 形态在 ed25519 信任锚下必须被拒（防签名降级）。"""
    use_test_anchor(monkeypatch)
    anchor = ed25519_anchor()
    manifest = build_manifest()
    manifest["signature"] = facts.UPDATE_SIGNATURE_PREFIX + "0" * 64
    with pytest.raises(UpdateError) as excinfo:
        verify_manifest(manifest, anchor)
    assert "形态非法" in str(excinfo.value)


def test_key_id_mismatch_rejected(monkeypatch):
    use_test_anchor(monkeypatch)
    anchor = ed25519_anchor()
    with pytest.raises(UpdateError) as excinfo:
        verify_manifest(build_manifest(key_id="djb-e2e-2026-11-02"), anchor)
    assert "key_id" in str(excinfo.value)
    # 不带 key_id 的旧清单仍然可用（tolerant）
    assert verify_manifest(build_manifest(key_id=None), anchor)["version"] == "0.2.0"


def test_artifact_hash_and_signature_checks(monkeypatch):
    use_test_anchor(monkeypatch)
    anchor = ed25519_anchor()
    data = b"payload"
    artifact = {"sha256": hashlib.sha256(data).hexdigest(), "url": "u",
                "signature": sign_bytes(data)}
    verify_artifact(data, artifact, anchor)
    # 哈希不符（先拒，不进验签）
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(b"other", artifact, anchor)
    assert "哈希不一致" in str(excinfo.value)
    # 哈希对但签名被换
    swapped = dict(artifact, signature=facts.UPDATE_ED25519_PREFIX + "11" * 64)
    with pytest.raises(UpdateError):
        verify_artifact(data, swapped, anchor)
    # 签名形态降级
    downgraded = dict(artifact, signature=facts.UPDATE_SIGNATURE_PREFIX + "0" * 64)
    with pytest.raises(UpdateError):
        verify_artifact(data, downgraded, anchor)


# ─────────────────────────────────────────────────────────── 内置公钥（发布口径）
def test_builtin_public_key_is_valid_and_fingerprint_matches():
    """仓库里的公钥常量必须是合法曲线点，且指纹与安全席登记口径一致。"""
    assert ed25519.is_valid_public_key_hex(facts.UPDATE_PUBLIC_KEY_HEX)
    raw = bytes.fromhex(facts.UPDATE_PUBLIC_KEY_HEX)
    assert hashlib.sha256(raw).hexdigest()[:12] == facts.UPDATE_PUBLIC_KEY_FINGERPRINT
    assert facts.UPDATE_KEY_ID == "djb-prod-2026-11-20"
    assert not facts.UPDATE_KEY_ID.startswith("djb-e2e")      # 生产锚不得再是 E2E 名
    # 内置公钥 != 测试公钥（防止「拿测试密钥当生产锚」）
    assert raw != TEST_PUBLIC_KEY
    anchor = ed25519_anchor()
    assert anchor.to_dict() == {"scheme": "ed25519", "key_id": facts.UPDATE_KEY_ID,
                                "key_fingerprint": facts.UPDATE_PUBLIC_KEY_FINGERPRINT}
    # 钥环：primary + transition 双钥，primary 必须是生产锚
    ring = update.keyring_anchors()
    assert ring.primary().key_id == facts.UPDATE_KEY_ID
    assert {a.key_id for a in ring.anchors} == {"djb-prod-2026-11-20", "djb-e2e-2026-11-02"}
    assert ring.state_of("djb-e2e-2026-11-02") == "transition"
    view = ring.to_dict()
    assert view["scheme"] == "ed25519" and view["revoked"] == []
    # RC8 安全口径（lead 2026-11-10）：primary 是**占位**公钥（仓库内无对应私钥），
    # 发布门禁必须 False，直到 owner 注入离线签名机生成的最终公钥。
    assert ring.placeholders == ("djb-prod-2026-11-20",)
    ready, reason = update.anchor_release_ready(ring)
    assert ready is False and "占位" in reason
    # to_dict 不含任何密钥材料
    assert facts.UPDATE_PUBLIC_KEY_HEX not in json.dumps(view)
    assert all("pubkey" not in item for item in view["keys"])


def test_sec_auditor_fixture_cross_verification(monkeypatch):
    """交叉验证：安全席用 **独立实现**（openssl 签 / 自写纯 python 验）签的 fixture，
    用本客户端的 Ed25519 实现 + 内置公钥必须验过；篡改必须拒。

    fixture 在 `ops/e2e/security/fixtures/update/`（不在 node 仓内），缺失则跳过。
    """
    fixture = _find_sec_fixture()
    if fixture is None:
        pytest.skip("安全席 fixture 不在本机（跨仓，CI 上正常跳过）")
    manifest_path = os.path.join(fixture, "manifest.json")
    artifact_path = os.path.join(fixture, "artifact.bin")
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    data = open(artifact_path, "rb").read()
    # fixture 是过渡期旧钥（E2E）签的：走钥环 resolve，验证「双钥过渡」真的兼容旧清单
    anchor = update.keyring_anchors().resolve("djb-e2e-2026-11-02")
    body = update.canonical_json(manifest["payload"]).encode("utf-8")
    update._check_signature(body, manifest["signature"], anchor, what="清单")
    update._check_signature(data, manifest["payload"]["artifacts"][0]["signature"], anchor,
                            what="下载包")
    with pytest.raises(UpdateError):
        update._check_signature(body.replace(b"0.0.0-e2e", b"9.9.9-e2e"),
                                manifest["signature"], anchor, what="清单")


def _find_sec_fixture() -> str | None:
    """向上找 `ops/e2e/security/fixtures/update`（worktree 可能嵌在 workspace 里）。"""
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(current, "ops", "e2e", "security", "fixtures", "update")
        if os.path.exists(os.path.join(candidate, "manifest.json")):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


# ─────────────────────────────────────────────────────────── 分桶 / 选择
def test_actions_and_bucket_eligibility(clock):
    payload = {"version": "0.2.0", "rollout": {"wave": 1, "pct": 5,
                                               "since": clock.current.isoformat()}}
    assert bucket_eligible(3, payload, now=clock())[0] is True
    assert bucket_eligible(28, payload, now=clock())[0] is False
    # wave 2 需要 ≥24h 观测
    payload["rollout"] = {"wave": 2, "pct": 25, "since": clock.current.isoformat()}
    assert bucket_eligible(10, payload, now=clock())[0] is False
    later = clock.current + timedelta(seconds=facts.UPDATE_BUCKET_OBSERVE_S + 1)
    assert bucket_eligible(10, payload, now=later)[0] is True
    assert bucket_eligible(10, {"version": "0.2.0"}, now=clock())[0] is True


def test_select_artifact():
    payload = {"artifacts": [{"os": "linux", "arch": "x86_64", "url": "u",
                              "sha256": "a" * 64, "signature": "ed25519:" + "b" * 128}]}
    assert select_artifact(payload, arch="x86_64")["url"] == "u"
    with pytest.raises(UpdateError):
        select_artifact(payload, arch="riscv64")


# ─────────────────────────────────────────────────────────── 全流程
def test_check_and_apply_install_and_rollback(tmp_path, paths, store, clock, monkeypatch):
    use_test_anchor(monkeypatch)
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    os.makedirs(os.path.join(paths.versions_dir, "0.1.0"), exist_ok=True)
    manager.source = build_source(tmp_path)
    check = manager.check()
    assert check["state"] == "eligible"
    assert check["signature"]["scheme"] == "ed25519"
    result = manager.apply()
    assert result["installed"] is True
    assert result["version"] == "0.2.0"
    assert store.install().version == "0.2.0"
    assert os.path.isdir(os.path.join(paths.versions_dir, "0.2.0"))
    rolled = manager.rollback()
    assert rolled["rolled_back"] is True
    assert rolled["to_version"] == "0.1.0"
    assert store.install().version == "0.1.0"


def test_tampered_artifact_refused(tmp_path, paths, store, clock, monkeypatch):
    use_test_anchor(monkeypatch)
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    source = build_source(tmp_path)
    manager.source = source
    with open(os.path.join(source, "artifact.bin"), "wb") as handle:
        handle.write(b"tampered")
    result = manager.apply()
    assert result["installed"] is False
    assert result["state"] == "failed_verify"
    assert store.install().version == "0.1.0"
    assert not os.path.isdir(os.path.join(paths.versions_dir, "0.2.0"))


def test_offline_and_no_source(paths, store, clock):
    manager = UpdateManager(paths, store, source="http://127.0.0.1:1", now=clock)
    assert manager.check()["state"] == "offline"
    result = manager.apply()
    assert result["installed"] is False and result["state"] == "offline"
    manager.source = ""
    assert manager.check()["state"] == "no_source"


def test_up_to_date_channel_and_not_eligible(tmp_path, paths, store, clock, monkeypatch):
    use_test_anchor(monkeypatch)
    manager = UpdateManager(paths, store, source="", channel="stable",
                            client_version="0.1.0", now=clock)
    manager.source = build_source(tmp_path, version="0.1.0")
    assert manager.check()["state"] == "up_to_date"
    manager.source = build_source(tmp_path, version="0.2.0", channel="beta")
    assert manager.check()["state"] == "channel_mismatch"
    store.update_install(bucket=28)
    manager.source = build_source(tmp_path, version="0.2.0",
                                  rollout={"wave": 1, "pct": 5,
                                           "since": clock.current.isoformat()})
    assert manager.check()["state"] == "not_eligible"


def test_keep_two_versions(tmp_path, paths, store, clock, monkeypatch):
    use_test_anchor(monkeypatch)
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    for version in ("0.1.0", "0.2.0"):
        os.makedirs(os.path.join(paths.versions_dir, version), exist_ok=True)
    manager.source = build_source(tmp_path, version="0.3.0")
    result = manager.apply()
    assert result["installed"] is True
    kept = manager._versions()
    assert kept == ["0.2.0", "0.3.0"]
    assert len(kept) == facts.UPDATE_KEEP_VERSIONS


def test_status_and_no_rollback(paths, store, clock):
    manager = UpdateManager(paths, store, source="", now=clock)
    status = manager.status()
    assert status["current_version"] == "0.1.0"
    assert status["signature"]["available"] is True
    assert status["signature"]["scheme"] == "ed25519"
    assert status["signature"]["key_id"] == facts.UPDATE_KEY_ID
    assert status["signature"]["mock"] is False
    assert manager.rollback() == {"rolled_back": False, "reason": "no_rollback_target"}


# ─────────────────────────────────────────────────────────── fail-closed（G10 核心）
def test_fail_closed_without_builtin_public_key(tmp_path, paths, store, clock, monkeypatch):
    """内置公钥缺失 -> 拒绝验签；不安装、不改版本、不本地生成密钥（旧的自动生成路径已删）。"""
    use_test_anchor(monkeypatch)
    monkeypatch.setattr(facts, "UPDATE_KEYRING", ())
    monkeypatch.setattr(facts, "UPDATE_PUBLIC_KEY_HEX", "")
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    manager.source = build_source(tmp_path)
    check = manager.check()
    assert check["state"] == "failed_verify"
    assert "钥环为空" in check["message"] or "公钥缺失" in check["message"]
    result = manager.apply()
    assert result["installed"] is False and result["state"] == "failed_verify"
    assert store.install().version == "0.1.0"
    # 没有自动生成任何本地密钥文件（审计 P1-3：客户端不得自造信任锚）
    assert not os.path.exists(os.path.join(paths.keys_dir, "update_mock.key"))


def test_invalid_builtin_public_key_fails_closed(paths, store, clock, monkeypatch):
    monkeypatch.setattr(facts, "UPDATE_KEYRING", (
        {"key_id": "bad-key", "pubkey_hex": "zz" * 32, "state": "primary"},))
    monkeypatch.setattr(facts, "UPDATE_PUBLIC_KEY_HEX", "zz" * 32)
    manager = UpdateManager(paths, store, source="", now=clock)
    with pytest.raises(UpdateError):
        manager.trust_anchor()
    assert manager.status()["signature"]["available"] is False
    assert manager.status()["signature"]["error"]


def test_hmac_manifest_rejected_outside_test_env(tmp_path, paths, store, clock, monkeypatch):
    """非 test 环境：只有 ed25519 信任锚，hmac 形态清单一律拒（不能「签名降级」）。"""
    monkeypatch.delenv(facts.UPDATE_TEST_ENV, raising=False)
    monkeypatch.delenv(facts.UPDATE_MOCK_KEY_ENV, raising=False)
    key = b"m" * 32
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    manager.source = mock_source(tmp_path, key)
    check = manager.check()
    assert check["state"] == "failed_verify"
    assert "形态非法" in check["message"]
    assert manager.apply()["installed"] is False


def test_mock_branch_requires_env_flag_and_key(tmp_path, paths, store, clock, monkeypatch):
    key = b"m" * 32
    # ① 三条件齐备 -> hmac 清单可装（仅测试）
    extra = enable_mock(monkeypatch, key)
    config = ClientConfig(extra=extra)
    manager = UpdateManager(paths, store, source="", client_version="0.1.0",
                            config=config, now=clock)
    assert manager.mock_requested() is True
    # 清单版本要明显高于已装版本（store fixture 的安装记录版本 = 包版本，
    # 会随 facts.CLIENT_VERSION 变），这里测的是 mock 门禁，不是版本比较。
    manager.source = mock_source(tmp_path, key, version="0.9.0")
    check = manager.check()
    assert check["state"] == "eligible"
    assert manager.apply()["installed"] is True

    # ② DJB_ENV=test + 开关，但没给密钥 -> 拒绝（不静默回退、不自签）
    monkeypatch.delenv(facts.UPDATE_MOCK_KEY_ENV, raising=False)
    manager2 = UpdateManager(paths, store, source="", client_version="0.1.0",
                             config=config, now=clock)
    assert manager2.mock_requested() is True
    with pytest.raises(UpdateError) as excinfo:
        manager2.trust_anchor()
    assert facts.UPDATE_MOCK_KEY_ENV in str(excinfo.value)

    # ③ 有 env + 密钥，但开关关闭 -> 回到 ed25519，hmac 清单必拒
    nodefault = ClientConfig(extra={})
    manager3 = UpdateManager(paths, store, source="", client_version="0.1.0",
                             config=nodefault, now=clock)
    assert manager3.mock_requested() is False
    manager3.source = build_source(tmp_path, version="0.9.0")
    assert manager3.check()["state"] in ("failed_verify", "not_eligible")
