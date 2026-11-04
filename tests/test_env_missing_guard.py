# SPDX-License-Identifier: Apache-2.0
"""「环境缺失不许静默」的元测试（task-15）。

它锁的不是 f16 的业务语义，而是**机制**：缺兄弟仓时不许安静地变成普通 skip。
将来有人把 `cross_repo.require_file` 改回 `pytest.mark.skipif`（更省事），这里会红。
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from tests import cross_repo, env_missing


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    env_missing.reset()
    monkeypatch.delenv(env_missing.STRICT_ENV, raising=False)
    yield
    env_missing.reset()


def test_missing_repo_records_env_missing_and_skips(tmp_path):
    missing = Path(tmp_path) / "djb-cloud" / "app" / "metering" / "node_console.py"
    with pytest.warns(UserWarning, match="ENV-MISSING"):
        with pytest.raises(pytest.skip.Exception) as excinfo:
            cross_repo.require_file(missing, what="f16 跨仓契约检查", reason="缺 ../djb-cloud")
    message = str(excinfo.value)
    assert "环境缺失" in message and "ENV-MISSING" in message
    # 关键：登记过，值班侧能被 grep 拦住
    lines = env_missing.format_lines()
    assert len(lines) == 1 and lines[0].startswith("ENV-MISSING: ")
    assert "f16 跨仓契约检查" in lines[0] and "../djb-cloud" in lines[0]


def test_strict_mode_fails_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setenv(env_missing.STRICT_ENV, "1")
    missing = Path(tmp_path) / "nope" / "node_console.py"
    with pytest.warns(UserWarning, match="ENV-MISSING"):
        with pytest.raises(pytest.fail.Exception) as excinfo:
            cross_repo.require_file(missing, what="f16 跨仓契约检查")
    assert "环境缺失" in str(excinfo.value)
    assert env_missing.records(), "严格模式下也要留下 ENV-MISSING 记录"


def test_present_repo_is_silent(tmp_path):
    present = Path(tmp_path) / "node_console.py"
    present.write_text("# fake\n", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")          # 环境齐全时**不许**留痕
        cross_repo.require_file(present, what="f16 跨仓契约检查")
    assert env_missing.records() == []


def test_registry_is_idempotent():
    env_missing.record("同一条")
    env_missing.record("同一条")
    assert env_missing.records() == ["同一条"]


def test_f16_module_does_not_use_silent_skipif():
    """结构性断言：f16 不许退回 `pytest.mark.skipif`（那正是这轮事故的形态）。"""
    source = (Path(__file__).with_name("test_f16_contract_parity.py")).read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.skipif" not in source
    assert "require_file(" in source
