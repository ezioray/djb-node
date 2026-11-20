# SPDX-License-Identifier: Apache-2.0
"""0.5.4：子命令的 --home 不许覆盖全局 --home（真机两个 state.db 的根因）。"""
from __future__ import annotations

from djb_node.cli import build_parser


def test_global_home_survives_subcommand_parsing() -> None:
    args = build_parser().parse_args(["--home", r"C:\tmp\custom-home", "bid", "list"])
    assert args.home == r"C:\tmp\custom-home", args.home


def test_subcommand_home_still_works() -> None:
    args = build_parser().parse_args(["bid", "--home", "/tmp/h2", "list"])
    assert args.home == "/tmp/h2"


def test_no_home_means_no_attribute_override() -> None:
    args = build_parser().parse_args(["status"])
    assert not hasattr(args, "home") or args.home in (None, "")