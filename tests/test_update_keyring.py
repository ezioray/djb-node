# SPDX-License-Identifier: Apache-2.0
"""RC8：更新信任钥环（primary/transition 双钥）、吊销、过渡到期、网络错误归类。

口径（与 reports/RC8-DEV/client/EVIDENCE.md 对应）：

* 信任来源只有 facts.UPDATE_KEYRING（公钥），吊销走 facts.UPDATE_REVOKED_KEY_IDS，
  过渡锚到期走 facts.UPDATE_TRANSITION_UNTIL；
* 清单声明 key_id 时**精确取钥**（未知/吊销/到期一律拒，不换钥试）；
* 制品签名绑定清单钥（key_id 给定时），不给 key_id 才回落到"任一可用钥"；
* 下载阶段 HTTP 非 200 是**网络类**（退出码 6），不是 failed_verify（7）。
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from djb_node import ed25519, facts, update
from djb_node.errors import UpdateError
from djb_node.update import (UpdateManager, apply_exit_code, ed25519_anchor, keyring_anchors,
                             verify_artifact, verify_manifest)
from tests.update_fixtures import (TEST_KEY_ID, TEST_PUBLIC_KEY, TEST_SEED, build_manifest,
                                   sign_bytes, use_test_anchor)

OTHER_SEED = bytes.fromhex("22" * 32)
OTHER_PUBLIC_KEY = ed25519.public_key_from_seed(OTHER_SEED)
OTHER_KEY_ID = "djb-prod-2026-12-01"


def _entry(key_id, pubkey, state="primary"):
    return {"key_id": key_id, "pubkey_hex": pubkey.hex(), "state": state}


def _patch_ring(monkeypatch, entries, *, revoked=(), until=""):
    monkeypatch.setattr(facts, "UPDATE_KEYRING", tuple(entries))
    monkeypatch.setattr(facts, "UPDATE_REVOKED_KEY_IDS", tuple(revoked))
    monkeypatch.setattr(facts, "UPDATE_TRANSITION_UNTIL", until)


def _sign_payload_with(seed, payload, key_id):
    body = {"version": payload["version"], "channel": payload["channel"],
            "released_at": payload["released_at"], "key_id": key_id,
            "artifacts": payload["artifacts"]}
    signature = facts.UPDATE_ED25519_PREFIX + ed25519.sign(
        seed, update.canonical_json(body).encode("utf-8")).hex()
    return {"payload": body, "signature": signature}


# ─────────────────────────────────────────── 双钥过渡
def test_dual_key_transition_accepts_old_and_new(monkeypatch):
    """旧 E2E 钥（transition）+ 新生产钥（primary）同时可用；各自签的清单都能验。"""
    _patch_ring(monkeypatch, [
        _entry(OTHER_KEY_ID, OTHER_PUBLIC_KEY, "primary"),
        _entry(TEST_KEY_ID, TEST_PUBLIC_KEY, "transition"),
    ], until="2099-12-31")
    ring = keyring_anchors()
    assert ring.primary().key_id == OTHER_KEY_ID
    assert ring.resolve(TEST_KEY_ID).key_id == TEST_KEY_ID

    old_manifest = build_manifest(key_id=TEST_KEY_ID)
    assert verify_manifest(old_manifest, ring)["version"] == "0.2.0"

    payload = build_manifest(key_id=OTHER_KEY_ID)["payload"]
    new_manifest = _sign_payload_with(OTHER_SEED, payload, OTHER_KEY_ID)
    assert verify_manifest(new_manifest, ring)["version"] == "0.2.0"

    # 用新钥签、却声明旧 key_id -> 拒（声明与签名不匹配）
    lying = _sign_payload_with(OTHER_SEED, payload, TEST_KEY_ID)
    with pytest.raises(UpdateError):
        verify_manifest(lying, ring)


def test_manifest_without_key_id_uses_any_usable_key(monkeypatch):
    _patch_ring(monkeypatch, [
        _entry(TEST_KEY_ID, TEST_PUBLIC_KEY, "primary"),
        _entry(OTHER_KEY_ID, OTHER_PUBLIC_KEY, "transition"),
    ], until="2099-12-31")
    ring = keyring_anchors()
    payload = build_manifest(key_id=None)["payload"]          # 无 key_id 的旧清单
    signature = facts.UPDATE_ED25519_PREFIX + ed25519.sign(
        OTHER_SEED, update.canonical_json(payload).encode("utf-8")).hex()
    manifest = {"payload": payload, "signature": signature}
    assert verify_manifest(manifest, ring)["version"] == "0.2.0"


# ─────────────────────────────────────────── 吊销 / 到期 / 未知
def test_revoked_key_rejected(monkeypatch):
    _patch_ring(monkeypatch, [
        _entry(TEST_KEY_ID, TEST_PUBLIC_KEY, "primary"),
        _entry(OTHER_KEY_ID, OTHER_PUBLIC_KEY, "primary"),
    ], revoked=(TEST_KEY_ID,))
    ring = keyring_anchors()
    with pytest.raises(UpdateError) as excinfo:
        ring.resolve(TEST_KEY_ID)
    assert "已吊销" in str(excinfo.value)
    with pytest.raises(UpdateError):
        verify_manifest(build_manifest(key_id=TEST_KEY_ID), ring)
    # 未被吊销的钥仍可用
    payload = build_manifest(key_id=OTHER_KEY_ID)["payload"]
    assert verify_manifest(_sign_payload_with(OTHER_SEED, payload, OTHER_KEY_ID), ring)


def test_transition_expired_rejected(monkeypatch):
    _patch_ring(monkeypatch, [
        _entry(OTHER_KEY_ID, OTHER_PUBLIC_KEY, "primary"),
        _entry(TEST_KEY_ID, TEST_PUBLIC_KEY, "transition"),
    ], until="2020-01-01")
    ring = keyring_anchors()
    with pytest.raises(UpdateError) as excinfo:
        ring.resolve(TEST_KEY_ID)
    assert "过渡期已结束" in str(excinfo.value)
    with pytest.raises(UpdateError):
        verify_manifest(build_manifest(key_id=TEST_KEY_ID), ring)
    # primary 不受影响
    payload = build_manifest(key_id=OTHER_KEY_ID)["payload"]
    assert verify_manifest(_sign_payload_with(OTHER_SEED, payload, OTHER_KEY_ID), ring)


def test_unknown_key_id_rejected(monkeypatch):
    use_test_anchor(monkeypatch)
    ring = keyring_anchors()
    with pytest.raises(UpdateError) as excinfo:
        verify_manifest(build_manifest(key_id="djb-unknown-9999"), ring)
    assert "key_id" in str(excinfo.value)


def test_keyring_empty_or_invalid_fails_closed(monkeypatch):
    monkeypatch.setattr(facts, "UPDATE_KEYRING", ())
    with pytest.raises(UpdateError):
        keyring_anchors()
    monkeypatch.setattr(facts, "UPDATE_KEYRING", ({"state": "primary", "pubkey_hex": "00" * 32},))
    with pytest.raises(UpdateError):
        keyring_anchors()
    monkeypatch.setattr(facts, "UPDATE_KEYRING", ({"key_id": "k1", "pubkey_hex": "zz" * 32},))
    with pytest.raises(UpdateError):
        keyring_anchors()


# ─────────────────────────────────────────── 制品钥绑定
def test_artifact_signature_bound_to_manifest_key(monkeypatch):
    _patch_ring(monkeypatch, [
        _entry(TEST_KEY_ID, TEST_PUBLIC_KEY, "primary"),
        _entry(OTHER_KEY_ID, OTHER_PUBLIC_KEY, "transition"),
    ], until="2099-12-31")
    ring = keyring_anchors()
    data = b"artifact-bytes"
    artifact = {"sha256": hashlib.sha256(data).hexdigest(), "url": "u",
                "signature": facts.UPDATE_ED25519_PREFIX + ed25519.sign(OTHER_SEED, data).hex()}
    # 声称清单钥是 TEST，但制品是 OTHER 签的 -> 绑定失败
    with pytest.raises(UpdateError):
        verify_artifact(data, artifact, ring, key_id=TEST_KEY_ID)
    # 不给 key_id（旧清单）：任一可用钥通过即可
    verify_artifact(data, artifact, ring)


# ─────────────────────────────────────────── 占位锚 / 发布门禁
def test_placeholder_primary_is_never_usable(monkeypatch):
    """占位公钥（仓库内无对应私钥）不得验签；release gate 必须 False。"""
    monkeypatch.setattr(facts, "UPDATE_KEYRING", (
        {"key_id": "djb-prod-2026-11-20", "pubkey_hex": OTHER_PUBLIC_KEY.hex(),
         "state": "primary", "placeholder": "true"},
        {"key_id": TEST_KEY_ID, "pubkey_hex": TEST_PUBLIC_KEY.hex(), "state": "transition"},
    ))
    monkeypatch.setattr(facts, "UPDATE_REVOKED_KEY_IDS", ())
    monkeypatch.setattr(facts, "UPDATE_TRANSITION_UNTIL", "2099-12-31")
    ring = keyring_anchors()
    assert ring.placeholders == ("djb-prod-2026-11-20",)
    with pytest.raises(UpdateError) as excinfo:
        ring.resolve("djb-prod-2026-11-20")
    assert "占位" in str(excinfo.value)
    ready, reason = update.anchor_release_ready(ring)
    assert ready is False and "占位" in reason
    # 换掉占位（模拟 owner 注入最终公钥）后门禁放行
    monkeypatch.setattr(facts, "UPDATE_KEYRING", (
        {"key_id": "djb-prod-2026-11-20", "pubkey_hex": OTHER_PUBLIC_KEY.hex(),
         "state": "primary"},
    ))
    ready, _ = update.anchor_release_ready()
    assert ready is True


def test_transition_window_is_short():
    """旧 E2E 私钥在 NAS（已泄露态）：过渡期必须是短窗，且 <= 60 天。"""
    import datetime as _dt
    last_day = _dt.date.fromisoformat(facts.UPDATE_TRANSITION_UNTIL)
    today = _dt.datetime.now(_dt.timezone.utc).date()
    assert last_day > today, "过渡期已过期：应显式加 UPDATE_REVOKED_KEY_IDS 而不是延长"
    assert (last_day - today).days <= 60, "过渡期过长（旧钥是已泄露态）"


# ─────────────────────────────────────────── status 视图
def test_status_exposes_keyring_without_key_material(paths, store, clock, monkeypatch):
    _patch_ring(monkeypatch, [
        _entry(OTHER_KEY_ID, OTHER_PUBLIC_KEY, "primary"),
        _entry(TEST_KEY_ID, TEST_PUBLIC_KEY, "transition"),
    ], until="2099-12-31")
    manager = UpdateManager(paths, store, source="", now=clock)
    status = manager.status()
    signature = status["signature"]
    assert signature["available"] is True
    assert signature["key_id"] == OTHER_KEY_ID
    assert signature["scheme"] == "ed25519"
    assert [item["key_id"] for item in signature["keys"]] == [OTHER_KEY_ID, TEST_KEY_ID]
    assert all("pubkey" not in item for item in signature["keys"])
    assert TEST_PUBLIC_KEY.hex() not in json.dumps(signature)
    assert OTHER_PUBLIC_KEY.hex() not in json.dumps(signature)


# ─────────────────────────────────────────── 网络错误归类（502）
class _Always502(BaseHTTPRequestHandler):
    def do_GET(self):                                     # noqa: N802 (http.server API)
        body = b"bad gateway"
        self.send_response(502)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def http502():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Always502)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def test_download_502_is_network_failure_not_verify(tmp_path, paths, store, clock,
                                                    monkeypatch, http502):
    use_test_anchor(monkeypatch)
    data = b"artifact-bytes"
    manifest = build_manifest(version="0.9.0", data=data,
                              artifact_path=http502 + "/artifact.bin")
    source = tmp_path / "src-502"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    manager = UpdateManager(paths, store, source=str(source), client_version="0.1.0", now=clock)
    result = manager.apply()
    assert result["installed"] is False
    assert result["state"] == "download_failed"      # 不是 failed_verify
    assert "HTTP 502" in result["message"]
    assert apply_exit_code(result) == facts.EXIT_NETWORK == 6
    assert store.install().version == "0.1.0"
    assert manager.status()["signature"]["available"] is True


def test_apply_exit_code_mapping():
    assert apply_exit_code({"state": "failed_verify"}) == facts.EXIT_VERIFY
    assert apply_exit_code({"state": "download_failed"}) == facts.EXIT_NETWORK
    for state in ("installed", "up_to_date", "offline", "no_source", "not_eligible"):
        assert apply_exit_code({"state": state}) == 0
    assert apply_exit_code({}) == 0
