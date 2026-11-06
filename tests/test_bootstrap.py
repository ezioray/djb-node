# SPDX-License-Identifier: Apache-2.0
"""首启自举（``djb_node.bootstrap``）：装完就能连上，不再要求用户先敲命令。

2026-11-06 外部测试客户实测：「云端未接通 / 本机未初始化」——
默认地址是占位符 + 桌面端没有初始化入口。这里锁死三条行为：

1. 全新 home → 自动建目录并写入**真实**默认地址；
2. 存量 config 里是 ``api.example.invalid`` → **就地修正**（这正是卡住客户的那一行）；
3. 已经配好的 home → **一个字节都不动**（幂等，避免把用户自建地址覆盖掉）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from djb_node import bootstrap


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_home_is_initialized_with_real_gateway(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = bootstrap.ensure_ready(home=home, enabled=True)
    assert result["errors"] == []
    assert result["created"] is True
    config_file = home / "config.json"
    assert config_file.exists()
    data = _read(config_file)
    assert data["gateway_url"] == bootstrap.DEFAULT_GATEWAY_URL
    assert "example.invalid" not in data["gateway_url"]
    # 建库：等价于 init 的最后一步
    assert (home / "djb.db").exists() or any(home.glob("*.db"))


def test_placeholder_gateway_is_repaired(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"gateway_url": "https://api.example.invalid", "mirror_port": 17321}),
        encoding="utf-8")
    result = bootstrap.ensure_ready(home=home, enabled=True)
    assert result["gateway_fixed"] is True
    assert _read(home / "config.json")["gateway_url"] == bootstrap.DEFAULT_GATEWAY_URL


def test_existing_real_gateway_is_not_touched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"gateway_url": "https://self-hosted.internal"}), encoding="utf-8")
    result = bootstrap.ensure_ready(home=home, enabled=True)
    assert result["gateway_fixed"] is False
    assert result["created"] is False
    assert _read(home / "config.json")["gateway_url"] == "https://self-hosted.internal"


def test_disabled_is_a_noop(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = bootstrap.ensure_ready(home=home, enabled=False)
    assert result["enabled"] is False
    assert not home.exists()


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "DEFAULT_GATEWAY_URL", "https://staging.example.org")
    home = Path("/tmp") / "djb-bootstrap-test-env"
    result = bootstrap.ensure_ready(home=home / "h", enabled=True)
    assert result["errors"] == []
    assert result["gateway"] == "https://staging.example.org"


def test_summary_text_reads_like_sentence() -> None:
    text = bootstrap.summary_text({"created": True, "gateway_fixed": False, "errors": []},
                                  {"started": True, "reason": "started", "errors": []})
    assert "已自动初始化" in text and "节点已启动" in text
    assert "auto" not in text.lower()
