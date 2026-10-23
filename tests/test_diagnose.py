# SPDX-License-Identifier: Apache-2.0
import json
import os
import zipfile

import pytest

from djb_node import envcheck
from djb_node.config import ClientConfig
from djb_node.diagnose import DiagnoseBuilder, DiagnoseError
from djb_node.envcheck import Check

CLIENT_KEY = "ck_" + "supersecret_client_key_0123456789"
UPSTREAM_SECRET = "upstream-" + "supersecret-value-9876543210"


def _prepare(paths, store, credentials):
    credentials.set_node("node-1", CLIENT_KEY)
    credentials.set_upstream_secret("u1", UPSTREAM_SECRET)
    credentials.buffer_key()
    credentials.provider_signing_key()
    os.makedirs(paths.logs_dir, exist_ok=True)
    with open(paths.log_file, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "x", "client_key": CLIENT_KEY}) + "\n")
        handle.write(json.dumps({"event": "y",
                                 "note": "sk-" + "abcdefghijklmnopqrstuvwxyz"}) + "\n")
        handle.write(json.dumps({"event": "z", "messages": "正文不应出现"}) + "\n")


def _bundle_text(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8", errors="ignore")
                for name in archive.namelist()}


def test_bundle_is_redacted_and_private(paths, store, credentials, monkeypatch):
    _prepare(paths, store, credentials)
    monkeypatch.setattr(envcheck, "check_acceleration",
                        lambda now: Check("acceleration", "加速栈", "warn", "未检测到",
                                          "安装官方驱动后重跑 djb env"))
    builder = DiagnoseBuilder(paths, store, credentials, ClientConfig())
    result = builder.build()
    assert result["bytes"] > 0
    assert oct(os.stat(result["path"]).st_mode)[-3:] == "600"
    assert result["redaction"]["hits"] == 0
    assert result["redaction"]["exact_secret_values_masked"] >= 2
    files = _bundle_text(result["path"])
    assert "manifest.json" in files
    assert "env.json" in files
    assert "redaction_report.json" in files
    assert any(name.startswith("logs/") for name in files)
    joined = "\n".join(files.values())
    assert CLIENT_KEY not in joined
    assert UPSTREAM_SECRET not in joined
    assert "sk-" + "abcdefghijklmnopqrstuvwxyz" not in joined
    assert "[REDACTED]" in joined
    manifest = json.loads(files["manifest.json"])
    assert manifest["module"] == "M15"
    assert manifest["credentials"]["client_key_set"] is True
    assert manifest["credentials"]["client_key_masked"].endswith("6789")
    assert json.loads(files["redaction_report.json"])["hits"] == []
    env = json.loads(files["env.json"])
    assert env["checks"]
    assert all(check["next_step"] for check in env["checks"])
    db_summary = json.loads(files["db_summary.json"])
    assert "receipts" in db_summary and "stale" in db_summary["receipts"]


def test_bundle_fails_closed_when_rescan_hits(paths, store, credentials, monkeypatch):
    builder = DiagnoseBuilder(paths, store, credentials, ClientConfig())
    monkeypatch.setattr(builder, "_scan", lambda text, secrets: ["pattern:hit"])
    with pytest.raises(DiagnoseError):
        builder.build()


def test_bundle_readme_explains_no_bodies(paths, store, credentials):
    builder = DiagnoseBuilder(paths, store, credentials, ClientConfig())
    result = builder.build()
    files = _bundle_text(result["path"])
    assert "prompt" in files["README.txt"]
    assert "凭据" in files["README.txt"]
