# SPDX-License-Identifier: Apache-2.0
import json
import os

import pytest

from djb_node import crypto, secure
from djb_node.errors import AuthError

CLIENT_KEY = "ck_" + "secret_value_0123456789"


def test_seal_open_roundtrip():
    key = crypto.new_key()
    token = crypto.seal(key, "双签收据 payload")
    assert token.startswith("s1:")
    assert crypto.open_sealed(key, token).decode("utf-8") == "双签收据 payload"


def test_seal_tamper_and_wrong_key_rejected():
    key = crypto.new_key()
    token = crypto.seal(key, "abc")
    parts = token.split(":")
    parts[2] = ("00" if parts[2][:2] != "00" else "11") + parts[2][2:]
    with pytest.raises(crypto.TamperError):
        crypto.open_sealed(key, ":".join(parts))
    with pytest.raises(crypto.TamperError):
        crypto.open_sealed(crypto.new_key(), token)
    with pytest.raises(crypto.TamperError):
        crypto.open_sealed(key, "v9:aa:bb:cc")
    with pytest.raises(ValueError):
        crypto.seal(b"", "x")


def test_key_fingerprint_is_stable_and_short():
    key = crypto.new_key()
    assert crypto.key_fingerprint(key) == crypto.key_fingerprint(key)
    assert len(crypto.key_fingerprint(key)) == 12


def test_credential_store_file_mode_and_secrets(paths, credentials):
    credentials.set_node("node-1", CLIENT_KEY)
    raw = json.loads(open(paths.credentials_file, encoding="utf-8").read())
    assert raw["storage"] == "file-0600"
    assert "S1" in raw["note"]
    assert oct(os.stat(paths.credentials_file).st_mode)[-3:] == "600"
    assert credentials.node_id() == "node-1"
    assert credentials.require_client_key() == CLIENT_KEY
    described = credentials.describe()
    assert described["client_key_masked"].endswith("6789")
    assert CLIENT_KEY not in json.dumps(described)


def test_credential_store_upstream_and_keys(paths, credentials):
    credentials.set_upstream_secret("u1", "upstream-secret-xyz")
    assert credentials.upstream_secret("u1") == "upstream-secret-xyz"
    assert credentials.delete_upstream_secret("u1") is True
    assert credentials.delete_upstream_secret("u1") is False
    buffer_key = credentials.buffer_key()
    assert len(buffer_key) == 32
    assert credentials.buffer_key() == buffer_key
    provider = credentials.provider_signing_key()
    assert len(provider) == 32
    assert oct(os.stat(paths.provider_key_file).st_mode)[-3:] == "600"
    # 权限被放宽后会被纠正
    os.chmod(paths.credentials_file, 0o644)
    fixed = credentials.ensure_permissions()
    assert paths.credentials_file in fixed
    assert oct(os.stat(paths.credentials_file).st_mode)[-3:] == "600"
    removed = credentials.clear()
    assert paths.credentials_file in removed
    assert not os.path.exists(paths.credentials_file)
    with pytest.raises(AuthError):
        credentials.require_client_key()


def test_missing_client_key_raises(credentials):
    with pytest.raises(AuthError):
        credentials.require_client_key()
    assert credentials.client_key() is None
