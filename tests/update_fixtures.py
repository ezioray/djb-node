# SPDX-License-Identifier: Apache-2.0
"""更新链路测试夹具（G10 之后的 ed25519 口径）。

**测试密钥与发布公钥无关**：这里用固定的 32 字节 seed 在测试内现算密钥对，
再 patch `facts.UPDATE_PUBLIC_KEY_HEX` / `UPDATE_KEY_ID` 指向它。这样：

* 单测不依赖 `~/.djb/secrets/e2e/` 里的任何文件（CI 上也没有）；
* 仓库里不会出现「测试私钥」这种东西（seed 是常量字符串，任何人可复算，
  但它只在本进程内当签名密钥，且 `facts` 的默认公钥不是它 —— 拿它签不出被默认客户端接受的清单）。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from djb_node import ed25519, facts
from djb_node.util import canonical_json

TEST_SEED = bytes.fromhex("7f" * 32)
"""deterministic test-only seed（不是发布密钥，也不在 facts 里当信任锚）。"""
TEST_PUBLIC_KEY = ed25519.public_key_from_seed(TEST_SEED)
TEST_KEY_ID = "test-key-0001"

_SOURCE_COUNTER = [0]


def sign_bytes(data: bytes) -> str:
    """artifact / 清单体 → ``ed25519:<hex>`` 签名串（测试侧签名）。"""
    return facts.UPDATE_ED25519_PREFIX + ed25519.sign(TEST_SEED, data).hex()


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {"payload": payload, "signature": sign_bytes(canonical_json(payload).encode("utf-8"))}


def artifact_bytes(version: str = "0.2.0") -> bytes:
    return ("djb-artifact-" + version).encode("utf-8")


def build_manifest(*, version: str = "0.2.0", channel: str = "stable",
                   rollout: dict[str, Any] | None = None, artifact_path: str | None = None,
                   data: bytes | None = None, key_id: str | None = TEST_KEY_ID,
                   artifact_signature: str | None = None) -> dict[str, Any]:
    blob = data if data is not None else artifact_bytes(version)
    payload: dict[str, Any] = {
        "version": version,
        "channel": channel,
        "released_at": "2026-10-24T00:00:00Z",
        "notes": "test",
        "artifacts": [{
            "os": "linux",
            "arch": "x86_64",
            "url": artifact_path or ("file:///nonexistent-" + version),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size": len(blob),
            "signature": artifact_signature or sign_bytes(blob),
        }],
    }
    if key_id is not None:
        payload["key_id"] = key_id
    if rollout is not None:
        payload["rollout"] = rollout
    return sign_payload(payload)


def build_source(tmp_path, *, version: str = "0.2.0", **kwargs: Any) -> str:
    """在 tmp 下造一个「更新源目录」：manifest.json + artifact.bin。"""
    _SOURCE_COUNTER[0] += 1
    directory = tmp_path / ("src-" + version + "-" + str(_SOURCE_COUNTER[0]))
    directory.mkdir()
    data = artifact_bytes(version)
    artifact = directory / "artifact.bin"
    artifact.write_bytes(data)
    manifest = build_manifest(version=version, artifact_path=str(artifact), data=data,
                              **kwargs)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(directory)


def use_test_anchor(monkeypatch) -> None:
    """把内置信任钥环指向测试公钥（只影响本测试进程）。

    RC8 起信任来源是 facts.UPDATE_KEYRING（钥环），所以这里**同时**改钥环与
    primary 扁平视图；生产钥/过渡钥/吊销在测试里一律不生效（避免测试无意依赖真钥）。
    """
    monkeypatch.setattr(facts, "UPDATE_KEYRING", (
        {"key_id": TEST_KEY_ID, "pubkey_hex": TEST_PUBLIC_KEY.hex(), "state": "primary"},
    ))
    monkeypatch.setattr(facts, "UPDATE_REVOKED_KEY_IDS", ())
    monkeypatch.setattr(facts, "UPDATE_PUBLIC_KEY_HEX", TEST_PUBLIC_KEY.hex())
    monkeypatch.setattr(facts, "UPDATE_KEY_ID", TEST_KEY_ID)


def enable_mock(monkeypatch, key: bytes) -> dict[str, Any]:
    """打开 test-only mock 三条件，返回可传给 ClientConfig(extra=...) 的配置。"""
    monkeypatch.setenv(facts.UPDATE_TEST_ENV, facts.UPDATE_TEST_ENV_VALUE)
    monkeypatch.setenv(facts.UPDATE_MOCK_KEY_ENV, key.hex())
    return {"feature_flags": {facts.UPDATE_MOCK_FLAG: True}}


def mock_manifest(key: bytes, *, version: str = "0.2.0", channel: str = "stable",
                  artifact_path: str | None = None, data: bytes | None = None,
                  key_id: str | None = None) -> dict[str, Any]:
    """test-only HMAC 形态清单（仅 mock 分支用例）。"""
    import hmac as _hmac
    blob = data if data is not None else artifact_bytes(version)
    payload: dict[str, Any] = {
        "version": version,
        "channel": channel,
        "released_at": "2026-10-24T00:00:00Z",
        "artifacts": [{
            "os": "linux",
            "arch": "x86_64",
            "url": artifact_path or ("file:///nonexistent-" + version),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size": len(blob),
            "signature": facts.UPDATE_SIGNATURE_PREFIX + _hmac.new(
                key, blob, hashlib.sha256).hexdigest(),
        }],
    }
    if key_id is not None:
        payload["key_id"] = key_id
    body = canonical_json(payload).encode("utf-8")
    return {"payload": payload,
            "signature": facts.UPDATE_SIGNATURE_PREFIX + _hmac.new(
                key, body, hashlib.sha256).hexdigest()}


def mock_source(tmp_path, key: bytes, *, version: str = "0.2.0", **kwargs: Any) -> str:
    _SOURCE_COUNTER[0] += 1
    directory = tmp_path / ("msrc-" + version + "-" + str(_SOURCE_COUNTER[0]))
    directory.mkdir()
    data = artifact_bytes(version)
    artifact = directory / "artifact.bin"
    artifact.write_bytes(data)
    manifest = mock_manifest(key, version=version, artifact_path=str(artifact), data=data,
                             **kwargs)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(directory)
