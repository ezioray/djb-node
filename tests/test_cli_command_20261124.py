# -*- coding: utf-8 -*-
"""冻结包里只读 CLI 的命令行选择（2026-11-24）。

为什么盯这个：onefile 里 `sys.executable` 是桌面端自己，`-m djb_node` 会让它
再解包一次（首屏前两次 ⇒ 十几秒）。有兄弟 `djb-node.exe` 时必须用它；
没有时必须**逐字**回到旧命令，别为了提速把源码运行搞坏。
"""
from __future__ import annotations

import os
import sys

from djb_node.desktop.datasource import LocalDataSource


def _ds() -> LocalDataSource:
    # 只测命令行拼装，不碰任何实例状态
    return object.__new__(LocalDataSource)


def test_frozen_uses_sibling_cli(tmp_path, monkeypatch):
    exe = tmp_path / ("djb-desktop.exe" if os.name == "nt" else "djb-desktop")
    exe.write_bytes(b"")
    sibling = tmp_path / ("djb-node.exe" if os.name == "nt" else "djb-node")
    sibling.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    cmd = _ds()._cli_command(["status", "--json"])
    assert cmd[0] == str(sibling)
    assert cmd[1:] == ["status", "--json"]


def test_frozen_without_sibling_falls_back(tmp_path, monkeypatch):
    exe = tmp_path / ("djb-desktop.exe" if os.name == "nt" else "djb-desktop")
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert _ds()._cli_command(["status"]) == [sys.executable, "-m", "djb_node", "status"]


def test_source_checkout_unchanged(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert _ds()._cli_command(["model-sync", "status"]) == [
        sys.executable, "-m", "djb_node", "model-sync", "status"]
