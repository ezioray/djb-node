# SPDX-License-Identifier: Apache-2.0
"""CLI 接线：``djb tray`` 必须存在，且只把参数转给 tray.main（不在 CLI 里建 Tk）。"""
from __future__ import annotations

import argparse

from djb_node import cli


def test_tray_command_registered_with_defaults() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["tray"])
    assert args.command == "tray"
    assert args.interval_ms == 5000
    assert args.title == "DJB 联脑"
    assert callable(args.handler)


def test_tray_command_accepts_overrides() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["tray", "--interval-ms", "1500", "--title", "测试托盘"])
    assert args.interval_ms == 1500
    assert args.title == "测试托盘"


def test_cmd_tray_delegates_to_tray_main(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_main(argv):
        seen["argv"] = list(argv)
        return 3

    import djb_node.tray.app as tray_app
    monkeypatch.setattr(tray_app, "main", fake_main)
    args = argparse.Namespace(interval_ms=1500, title="测试托盘")
    assert cli.cmd_tray(args) == 3
    assert seen["argv"] == ["--interval-ms", "1500", "--title", "测试托盘"]
