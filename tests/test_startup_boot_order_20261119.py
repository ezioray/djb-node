# SPDX-License-Identifier: Apache-2.0
"""先出窗口、后跑自举（2026-11-19 真机：首窗 6~15 s 像"双击没反应"）。

守两件事：
  1. ``DesktopController.set_boot()`` 能把自举结论回填进数据源；
  2. ``main()`` 的 ``--no-auto`` 路径不再触发自举（窗口先出现），且 ``--export-text`` 仍然可用。
"""
from __future__ import annotations

import os
import tempfile

from djb_node.desktop.app import make_controller


def test_set_boot_backfills_datasource():
    with tempfile.TemporaryDirectory() as tmp:
        controller = make_controller(home=os.path.join(tmp, "home"))
        # 启动时还没自举：数据源里是空的
        assert dict(controller.source._boot) == {}
        controller.set_boot({"reason": "started", "pid": 1234})
        assert controller.source._boot["reason"] == "started"
        assert controller.source._boot["pid"] == 1234
        # 回填 None 也要安全（没自举就保持空）
        controller.set_boot(None)
        assert dict(controller.source._boot) == {}


def test_set_boot_without_source_is_noop():
    class _Bare:
        pass

    from djb_node.desktop.controller import DesktopController

    # 不构造真实数据源，只验证防御性分支不抛
    obj = object.__new__(DesktopController)
    obj.set_boot({"reason": "started"})          # 不应抛异常


def test_main_export_text_no_auto_returns_ok():
    """--no-auto 不跑自举，但仍要把页面导出来（回归：改启动顺序不许弄坏导出）。"""
    import pytest

    pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("无 DISPLAY：主窗口起不来（有 Xvfb 的环境才会跑这条）")
    from djb_node.desktop import app as app_mod

    with tempfile.TemporaryDirectory() as tmp:
        rc = app_mod.main(["--view", "overview", "--export-text", "--no-auto",
                           "--home", os.path.join(tmp, "home")])
        assert rc == 0