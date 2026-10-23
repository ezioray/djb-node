# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
from datetime import timedelta

import pytest

from djb_node import facts
from djb_node.errors import UpdateError
from djb_node.update import (UpdateManager, bucket_eligible, select_artifact,
                             sign_manifest, verify_artifact, verify_manifest)


def _artifact_bytes(version="0.2.0"):
    return ("djb-artifact-" + version).encode("utf-8")


def _manifest(key, *, version="0.2.0", channel="stable", rollout=None,
              artifact_path=None, data=None):
    data = data if data is not None else _artifact_bytes(version)
    payload = {
        "version": version,
        "channel": channel,
        "released_at": "2026-10-24T00:00:00Z",
        "notes": "test",
        "artifacts": [{
            "os": "linux",
            "arch": "x86_64",
            "url": artifact_path or ("file:///nonexistent-" + version),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "signature": "hmac-sha256:" + __import__("hmac").new(
                key, data, hashlib.sha256).hexdigest(),
        }],
    }
    if rollout is not None:
        payload["rollout"] = rollout
    return sign_manifest(payload, key)


_SOURCE_COUNTER = [0]


def _source(tmp_path, key, *, version="0.2.0", **kwargs):
    _SOURCE_COUNTER[0] += 1
    directory = tmp_path / ("src-" + version + "-" + str(_SOURCE_COUNTER[0]))
    directory.mkdir()
    data = _artifact_bytes(version)
    artifact = directory / "artifact.bin"
    artifact.write_bytes(data)
    manifest = _manifest(key, version=version, artifact_path=str(artifact), data=data,
                         **kwargs)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(directory)


def test_manifest_signature_required(tmp_path, paths, store):
    key = b"k" * 32
    manifest = _manifest(key)
    payload = verify_manifest(manifest, key)
    assert payload["version"] == "0.2.0"
    with pytest.raises(UpdateError):
        verify_manifest(manifest, b"x" * 32)
    tampered = json.loads(json.dumps(manifest))
    tampered["payload"]["version"] = "9.9.9"
    with pytest.raises(UpdateError):
        verify_manifest(tampered, key)
    with pytest.raises(UpdateError):
        verify_manifest({"payload": {}}, key)
    with pytest.raises(UpdateError):
        verify_manifest(None, key)


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
                              "sha256": "a" * 64, "signature": "hmac-sha256:"
                              + "b" * 64}]}
    assert select_artifact(payload, arch="x86_64")["url"] == "u"
    with pytest.raises(UpdateError):
        select_artifact(payload, arch="riscv64")


def test_check_and_apply_install_and_rollback(tmp_path, paths, store, clock):
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    key = manager.signing_key()
    os.makedirs(os.path.join(paths.versions_dir, "0.1.0"), exist_ok=True)
    source = _source(tmp_path, key)
    manager.source = source
    check = manager.check()
    assert check["state"] == "eligible"
    result = manager.apply()
    assert result["installed"] is True
    assert result["version"] == "0.2.0"
    assert store.install().version == "0.2.0"
    assert os.path.isdir(os.path.join(paths.versions_dir, "0.2.0"))
    rolled = manager.rollback()
    assert rolled["rolled_back"] is True
    assert rolled["to_version"] == "0.1.0"
    assert store.install().version == "0.1.0"


def test_tampered_artifact_refused(tmp_path, paths, store, clock):
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    key = manager.signing_key()
    source = _source(tmp_path, key)
    manager.source = source
    artifact_path = os.path.join(source, "artifact.bin")
    with open(artifact_path, "wb") as handle:
        handle.write(b"tampered")
    result = manager.apply()
    assert result["installed"] is False
    assert result["state"] == "failed_verify"
    assert store.install().version == "0.1.0"
    assert not os.path.isdir(os.path.join(paths.versions_dir, "0.2.0"))


def test_artifact_hash_and_signature_checks():
    key = b"k" * 32
    data = b"payload"
    artifact = {"sha256": hashlib.sha256(data).hexdigest(), "url": "u",
                "signature": "hmac-sha256:" + __import__("hmac").new(
                    key, data, hashlib.sha256).hexdigest()}
    verify_artifact(data, artifact, key)
    with pytest.raises(UpdateError):
        verify_artifact(b"other", artifact, key)
    with pytest.raises(UpdateError):
        verify_artifact(data, artifact, b"x" * 32)


def test_offline_and_no_source(tmp_path, paths, store, clock):
    manager = UpdateManager(paths, store, source="http://127.0.0.1:1", now=clock)
    check = manager.check()
    assert check["state"] == "offline"
    result = manager.apply()
    assert result["installed"] is False
    assert result["state"] == "offline"
    manager.source = ""
    assert manager.check()["state"] == "no_source"


def test_up_to_date_channel_and_not_eligible(tmp_path, paths, store, clock):
    manager = UpdateManager(paths, store, source="", channel="stable",
                            client_version="0.1.0", now=clock)
    key = manager.signing_key()
    same = _source(tmp_path, key, version="0.1.0")
    manager.source = same
    assert manager.check()["state"] == "up_to_date"
    beta = _source(tmp_path, key, version="0.2.0", channel="beta")
    manager.source = beta
    assert manager.check()["state"] == "channel_mismatch"
    store.update_install(bucket=28)
    wave = _source(tmp_path, key, version="0.2.0",
                   rollout={"wave": 1, "pct": 5, "since": clock.current.isoformat()})
    manager.source = wave
    assert manager.check()["state"] == "not_eligible"


def test_keep_two_versions(tmp_path, paths, store, clock):
    manager = UpdateManager(paths, store, source="", client_version="0.1.0", now=clock)
    key = manager.signing_key()
    for version in ("0.1.0", "0.2.0"):
        os.makedirs(os.path.join(paths.versions_dir, version), exist_ok=True)
    source = _source(tmp_path, key, version="0.3.0")
    manager.source = source
    result = manager.apply()
    assert result["installed"] is True
    kept = manager._versions()
    assert kept == ["0.2.0", "0.3.0"]
    assert len(kept) == facts.UPDATE_KEEP_VERSIONS


def test_status_and_no_rollback(paths, store, clock):
    manager = UpdateManager(paths, store, source="", now=clock)
    status = manager.status()
    assert status["current_version"] == "0.1.0"
    assert manager.rollback() == {"rolled_back": False, "reason": "no_rollback_target"}
