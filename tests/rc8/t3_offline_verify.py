#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""T3-a：离线更新验签矩阵（RC8.3-C）。12 类负例 + 3 类正例，全部本机离线跑。

用法：
    PYTHONPATH=<djb-node 仓库根> python3 tests/rc8/t3_offline_verify.py [--keep]
    # 退出码：0 = 全 PASS；1 = 有 FAIL；2 = 环境不合格

判据口径（与 djb_node/update.py 一致）：
    * 清单必须是 {payload, signature}（Ed25519 over canonical_json(payload)）；
    * 每制品必须带 sha256 + url + signature（对 raw bytes 验签）；
    * 未知/吊销/过渡到期的 key_id 一律拒；无 key_id 的旧清单在任何可用钥下可过；
    * 下载阶段 HTTP 非 200 = 网络类（download_failed / 退出码 6），不是验签失败；
    * 拒装时版本不变、versions/ 目录不留半包。

测试密钥是**常数 seed**（与单测 tests/update_fixtures.py 同源），只在本进程内签，
不落盘、不进仓库；生产钥只以公钥形式出现在 facts.UPDATE_KEYRING。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from djb_node import ed25519, facts, update
from djb_node.config import Paths
from djb_node.errors import UpdateError
from djb_node.store import Store
from djb_node.util import canonical_json

PRIMARY_SEED = bytes.fromhex("7f" * 32)
PRIMARY_ID = "test-primary-0001"
LEGACY_SEED = bytes.fromhex("11" * 32)
LEGACY_ID = "test-legacy-0002"
CURRENT_VERSION = "0.2.5"
TARGET_VERSION = "0.2.6"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("RESULT T3-%s %s %s" % (name, "PASS" if ok else "FAIL", detail))


def entry(key_id: str, seed: bytes, state: str) -> dict:
    return {"key_id": key_id,
            "pubkey_hex": ed25519.public_key_from_seed(seed).hex(),
            "state": state}


def use_ring(*, revoked=(), until="2099-12-31") -> None:
    facts.UPDATE_KEYRING = (entry(PRIMARY_ID, PRIMARY_SEED, "primary"),
                            entry(LEGACY_ID, LEGACY_SEED, "transition"))
    facts.UPDATE_KEY_ID = PRIMARY_ID
    facts.UPDATE_PUBLIC_KEY_HEX = ed25519.public_key_from_seed(PRIMARY_SEED).hex()
    facts.UPDATE_REVOKED_KEY_IDS = tuple(revoked)
    facts.UPDATE_TRANSITION_UNTIL = until


def make_machine(workdir: str) -> tuple[Paths, Store]:
    paths = Paths(home=os.path.join(workdir, "home"))
    paths.prepare()
    store = Store.create(paths.db_file, version=CURRENT_VERSION)
    store.update_install(version=CURRENT_VERSION)
    return paths, store


def manifest_for(data: bytes, *, seed=PRIMARY_SEED, key_id=PRIMARY_ID,
                 version=TARGET_VERSION, url="", extra_payload=None,
                 artifact_seed=None) -> dict:
    artifact_seed = artifact_seed if artifact_seed is not None else seed
    payload = {
        "version": version,
        "channel": "stable",
        "released_at": "2026-11-10T00:00:00Z",
        "artifacts": [{
            "os": "linux",
            "arch": "x86_64",
            "url": url,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "signature": facts.UPDATE_ED25519_PREFIX + ed25519.sign(artifact_seed, data).hex(),
        }],
    }
    if extra_payload is None or "rollout" not in (extra_payload or {}):
        # 波次 3 = 100%，since 取很早 => 任意分桶都命中（测试的是验签，不是灰度）
        payload["rollout"] = {"wave": 3, "since": "2020-01-01T00:00:00Z"}
    if key_id is not None:
        payload["key_id"] = key_id
    if extra_payload:
        payload.update(extra_payload)
    signature = facts.UPDATE_ED25519_PREFIX + ed25519.sign(
        seed, canonical_json(payload).encode("utf-8")).hex()
    return {"payload": payload, "signature": signature}


def write_source(root: str, name: str, data: bytes, manifest: dict) -> str:
    directory = os.path.join(root, name)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "artifact.bin"), "wb") as handle:
        handle.write(data)
    path = os.path.join(directory, "manifest.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    return path


def check(source: str, workdir: str):
    paths, store = make_machine(workdir)
    manager = update.UpdateManager(paths, store, source=source,
                                   client_version=CURRENT_VERSION)
    return manager, manager.check()


def case_valid(workdir: str, root: str) -> None:
    data = b"artifact-" + TARGET_VERSION.encode()
    source = write_source(root, "valid", data, manifest_for(data, url="file://" + root + "/valid/artifact.bin"))
    manager, result = check(source, workdir)
    ok = result.get("state") == "eligible"
    if ok:
        applied = manager.apply()
        ok = applied.get("state") == "installed" and os.path.isfile(
            os.path.join(manager.paths.versions_dir, TARGET_VERSION, "artifact.bin"))
        detail = applied.get("state", "")
    else:
        detail = result.get("state", "") + ":" + str(result.get("message"))
    record("valid-install", ok, detail)


def case_tampered_artifact(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    source = write_source(root, "tamper-art", data, manifest_for(data, url="file://" + root + "/tamper-art/artifact.bin"))
    with open(os.path.join(root, "tamper-art", "artifact.bin"), "ab") as handle:
        handle.write(b"x")
    manager, result = check(source, workdir)
    applied = manager.apply()
    ok = applied.get("state") == "failed_verify" and "哈希不一致" in str(applied.get("message"))
    record("tampered-artifact", ok, str(applied.get("state")) + ":" + str(applied.get("message"))[:60])


def case_tampered_payload(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, url="file://" + root + "/tamper-payload/artifact.bin")
    manifest["payload"]["version"] = "9.9.9"
    source = write_source(root, "tamper-payload", data, manifest)
    _, result = check(source, workdir)
    ok = result.get("state") == "failed_verify" and "签名" in str(result.get("message"))
    record("tampered-payload", ok, str(result.get("state")) + ":" + str(result.get("message"))[:60])


def case_missing_payload(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, url="file://" + root + "/missing-payload/artifact.bin")
    broken = {"signature": manifest["signature"]}
    source = write_source(root, "missing-payload", data, broken)
    _, result = check(source, workdir)
    ok = result.get("state") == "failed_verify" and "缺 payload" in str(result.get("message"))
    record("missing-payload", ok, str(result.get("message"))[:60])


def case_missing_signature(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, url="file://" + root + "/missing-signature/artifact.bin")
    source = write_source(root, "missing-signature", data, {"payload": manifest["payload"]})
    _, result = check(source, workdir)
    ok = result.get("state") == "failed_verify" and "签名缺失" in str(result.get("message"))
    record("missing-signature", ok, str(result.get("message"))[:60])


def case_artifact_missing_signature(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, url="file://" + root + "/artifact-no-sig/artifact.bin")
    manifest["payload"]["artifacts"][0].pop("signature")
    body = canonical_json(manifest["payload"]).encode("utf-8")
    manifest["signature"] = facts.UPDATE_ED25519_PREFIX + ed25519.sign(PRIMARY_SEED, body).hex()
    source = write_source(root, "artifact-no-sig", data, manifest)
    _, result = check(source, workdir)
    ok = result.get("state") == "failed_verify" and "artifact 缺合法签名" in str(result.get("message"))
    record("artifact-missing-signature", ok, str(result.get("message"))[:60])


def case_unknown_key_id(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, key_id="djb-unknown-9999",
                            url="file://" + root + "/unknown-key/artifact.bin")
    source = write_source(root, "unknown-key", data, manifest)
    _, result = check(source, workdir)
    ok = result.get("state") == "failed_verify" and "key_id" in str(result.get("message"))
    record("unknown-key-id", ok, str(result.get("message"))[:70])


def case_revoked_key(workdir: str, root: str) -> None:
    use_ring(revoked=(LEGACY_ID,))
    data = b"artifact-bytes"
    manifest = manifest_for(data, seed=LEGACY_SEED, key_id=LEGACY_ID,
                            url="file://" + root + "/revoked/artifact.bin")
    source = write_source(root, "revoked", data, manifest)
    _, result = check(source, workdir)
    use_ring()
    ok = result.get("state") == "failed_verify" and "已吊销" in str(result.get("message"))
    record("revoked-key", ok, str(result.get("message"))[:70])


def case_transition_expired(workdir: str, root: str) -> None:
    use_ring(until="2020-01-01")
    data = b"artifact-bytes"
    manifest = manifest_for(data, seed=LEGACY_SEED, key_id=LEGACY_ID,
                            url="file://" + root + "/expired/artifact.bin")
    source = write_source(root, "expired", data, manifest)
    _, result = check(source, workdir)
    use_ring()
    ok = result.get("state") == "failed_verify" and "过渡期已结束" in str(result.get("message"))
    record("transition-expired", ok, str(result.get("message"))[:70])


def case_hmac_downgrade(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, url="file://" + root + "/hmac/artifact.bin")
    manifest["signature"] = facts.UPDATE_SIGNATURE_PREFIX + "0" * 64
    manifest["payload"]["artifacts"][0]["signature"] = facts.UPDATE_SIGNATURE_PREFIX + "0" * 64
    source = write_source(root, "hmac", data, manifest)
    _, result = check(source, workdir)
    ok = result.get("state") == "failed_verify" and "形态" in str(result.get("message"))
    record("signature-downgrade-hmac", ok, str(result.get("message"))[:70])


def case_downgrade_version(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    source = write_source(root, "downgrade", data,
                          manifest_for(data, version="0.2.4",
                                       url="file://" + root + "/downgrade/artifact.bin"))
    _, result = check(source, workdir)
    ok = result.get("state") == "up_to_date"
    record("downgrade-refused", ok, str(result.get("state")))


def case_offline(workdir: str, root: str) -> None:
    _, result = check("http://127.0.0.1:9/manifest.json", workdir)
    ok = result.get("state") == "offline"
    record("offline-keeps-version", ok, str(result.get("state")) + " cur=" + str(result.get("current_version")))


def case_no_source(workdir: str, root: str) -> None:
    _, result = check("", workdir)
    ok = result.get("state") == "no_source"
    record("no-source", ok, str(result.get("state")))


def case_channel_mismatch(workdir: str, root: str) -> None:
    data = b"artifact-bytes"
    manifest = manifest_for(data, url="file://" + root + "/channel/artifact.bin",
                            extra_payload={"channel": "beta"})
    source = write_source(root, "channel", data, manifest)
    _, result = check(source, workdir)
    ok = result.get("state") == "channel_mismatch"
    record("channel-mismatch", ok, str(result.get("state")))


class _Handler502(BaseHTTPRequestHandler):
    def do_GET(self):                                      # noqa: N802
        self.send_response(502)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"bad gate")

    def log_message(self, *args):
        pass


def case_download_502(workdir: str, root: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler502)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % server.server_address[1]
        data = b"artifact-bytes"
        source = write_source(root, "dl502", data, manifest_for(data, url=base + "/artifact.bin"))
        manager, _ = check(source, workdir)
        applied = manager.apply()
        ok = (applied.get("state") == "download_failed"
              and update.apply_exit_code(applied) == facts.EXIT_NETWORK == 6)
        record("download-502-network", ok,
               str(applied.get("state")) + ":" + str(applied.get("message"))[:50])
    finally:
        server.shutdown()
        server.server_close()


def case_rollback(workdir: str, root: str) -> None:
    manager, _ = check("", workdir)
    result = manager.rollback()
    ok = result.get("rolled_back") is False or result.get("reason") == "no_rollback_target"
    record("rollback-without-target", ok, str(result.get("reason") or result))


CASES = (
    case_valid, case_tampered_artifact, case_tampered_payload, case_missing_payload,
    case_missing_signature, case_artifact_missing_signature, case_unknown_key_id,
    case_revoked_key, case_transition_expired, case_hmac_downgrade, case_downgrade_version,
    case_offline, case_no_source, case_channel_mismatch, case_download_502, case_rollback,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = parser.parse_args()
    workroot = tempfile.mkdtemp(prefix="t3-offline-")
    try:
        for index, case in enumerate(CASES):
            workdir = os.path.join(workroot, "case-%02d" % index)
            root = os.path.join(workdir, "rel")
            os.makedirs(root, exist_ok=True)
            use_ring()
            case(workdir, root)
    finally:
        if args.keep:
            print("kept:", workroot)
        else:
            shutil.rmtree(workroot, ignore_errors=True)
    failures = [name for name, ok, _ in RESULTS if not ok]
    passed = len(RESULTS) - len(failures)
    print("RESULT T3-SUMMARY %s pass=%d fail=%d" %
          ("PASS" if not failures else "FAIL", passed, len(failures)))
    if failures:
        print("failed:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
