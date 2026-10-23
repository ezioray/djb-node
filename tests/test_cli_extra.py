# SPDX-License-Identifier: Apache-2.0
"""CLI 命令的端到端路径（mock 服务端）：auth / announce / upstream / update / uninstall。"""
from __future__ import annotations

import json
import os

from djb_node import envcheck
from djb_node.cli import main
from djb_node.config import Paths
from djb_node.envcheck import Check
from djb_node.store import Store
from djb_node.update import UpdateManager
from tests.mock_m02 import CLIENT_KEY
from tests.test_update import _source


def test_auth_pair_bind_unbind(home, mock_server, capsys):
    assert main(["init", "--no-env", "--gateway", mock_server.base_url]) == 0
    capsys.readouterr()
    assert main(["auth", "--set-key", CLIENT_KEY]) == 0
    capsys.readouterr()
    assert main(["upstream", "add", "--access", "A2",
                 "--base-url", mock_server.upstream_base_url, "--models", "m1"]) == 0
    capsys.readouterr()
    # 注册一个 node_id（mock register 只经 daemon；这里直接写凭据模拟已配对）
    from djb_node.secure import CredentialStore
    paths = Paths(str(home))
    CredentialStore(paths).set_node("node-mock-1", CLIENT_KEY)
    assert main(["auth", "--pair", "--json"]) == 0
    paired = json.loads(capsys.readouterr().out.strip())
    assert paired["rotated"] is True
    assert main(["auth", "--bind", "code-1", "--json"]) == 0
    capsys.readouterr()
    assert main(["auth", "--unbind"]) == 2
    assert main(["auth", "--unbind", "--yes", "--json"]) == 0
    capsys.readouterr()


def test_announce_and_upstream_probe_declare(home, mock_server, capsys):
    from djb_node.secure import CredentialStore
    assert main(["init", "--no-env", "--gateway", mock_server.base_url]) == 0
    capsys.readouterr()
    paths = Paths(str(home))
    CredentialStore(paths).set_node("node-mock-1", CLIENT_KEY)
    assert main(["upstream", "add", "--access", "A1",
                 "--base-url", mock_server.upstream_base_url, "--models", "mock-model",
                 "--secret", "secret-value", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    upstream_id = payload["upstream"]["id"]
    assert payload["declare"]["declared"] is True
    assert main(["upstream", "probe", "--id", upstream_id, "--json"]) == 0
    probe = json.loads(capsys.readouterr().out.strip())
    assert probe["reachable"] is True
    assert probe["reported"] is True
    assert main(["upstream", "declare", "--id", upstream_id, "--json"]) == 0
    capsys.readouterr()
    assert main(["announce", "--json"]) == 0
    announced = json.loads(capsys.readouterr().out.strip())
    assert announced["capacity"]
    assert announced["response"]["announcement"]["accepted"] is True
    assert main(["announce", "--dry-run", "--json"]) == 0
    capsys.readouterr()


def test_update_cli_apply_and_rollback(home, tmp_path, capsys):
    assert main(["init", "--no-env"]) == 0
    capsys.readouterr()
    paths = Paths(str(home))
    store = Store(paths.db_file)
    try:
        manager = UpdateManager(paths, store)
        key = manager.signing_key()
    finally:
        store.close()
    os.makedirs(os.path.join(paths.versions_dir, "0.1.0"), exist_ok=True)
    source = _source(tmp_path, key, version="0.2.0")
    assert main(["init", "--no-env", "--update-source", source]) == 0
    capsys.readouterr()
    assert main(["update", "--check", "--json"]) == 0
    check = json.loads(capsys.readouterr().out.strip())
    assert check["state"] == "eligible"
    assert main(["update", "--apply", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out.strip())
    assert applied["installed"] is True
    assert main(["update", "--rollback", "--json"]) == 0
    rolled = json.loads(capsys.readouterr().out.strip())
    assert rolled["rolled_back"] is True


def test_update_cli_failed_verify(home, tmp_path, capsys):
    assert main(["init", "--no-env"]) == 0
    capsys.readouterr()
    paths = Paths(str(home))
    store = Store(paths.db_file)
    try:
        manager = UpdateManager(paths, store)
        key = manager.signing_key()
    finally:
        store.close()
    source = _source(tmp_path, key, version="0.2.0")
    assert main(["init", "--no-env", "--update-source", source]) == 0
    capsys.readouterr()
    store = Store(paths.db_file)
    try:
        manager = UpdateManager(paths, store)
        manager.source = source
        manager.check()
        manifest_path = os.path.join(source, "manifest.json")
        manifest = json.loads(open(manifest_path, encoding="utf-8").read())
        manifest["payload"]["version"] = "9.9.9"
        open(manifest_path, "w", encoding="utf-8").write(json.dumps(manifest))
    finally:
        store.close()
    assert main(["update", "--check", "--json"]) == 7
    capsys.readouterr()


def test_uninstall_keep_logs_and_status_show_token(home, capsys):
    assert main(["init", "--no-env"]) == 0
    capsys.readouterr()
    assert main(["status", "--show-token", "--json"]) == 0
    status = json.loads(capsys.readouterr().out.strip())
    assert status.get("token_error")
    assert main(["uninstall", "--yes", "--keep-logs", "--json"]) == 0
    result = json.loads(capsys.readouterr().out.strip())
    assert result["residual_count"] == 0
    assert os.path.isdir(os.path.join(str(home), "logs"))


def test_env_authorize_with_low_memory(home, capsys, monkeypatch):
    assert main(["init", "--no-env"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(envcheck, "check_hardware",
                        lambda now: Check("hardware", "硬件", "warn", "内存 1 GB（偏小）",
                                          "只做 Token 中转并把并发设为 1",
                                          {"cpu_count": 2, "mem_gb": 1.0}))
    assert main(["env", "--authorize", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["authorization"]["authorized"] is True
    assert payload["authorization"]["applied"]
    assert main(["env", "--rollback", "--json"]) == 0
    capsys.readouterr()


def test_logs_level_filter_and_diagnose_output(home, tmp_path, capsys):
    assert main(["init", "--no-env"]) == 0
    capsys.readouterr()
    from djb_node.logging_setup import event
    event("test.error", level="error", model="m1")
    event("test.info", level="info", model="m1")
    assert main(["logs", "--level", "error", "--tail", "20"]) == 0
    output = capsys.readouterr().out
    assert "test.error" in output
    target = str(tmp_path / "diag" / "bundle.zip")
    assert main(["diagnose", "--output", target]) == 0
    capsys.readouterr()
    assert os.path.exists(target)
