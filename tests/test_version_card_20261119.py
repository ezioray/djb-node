# SPDX-License-Identifier: Apache-2.0
"""版本卡必须显示**实际在跑的程序版本**（2026-11-19 真机 P1）。

现场：0.3.16 的 ``--version`` 是 0.3.16，账户页「当前版本」却写 0.3.15。
根因：``update.status()`` 早就给了 ``package_version``，但
``datasource._update_detail()`` 的投影只搬了 ``current_version``（安装记录），
视图层拿不到 package_version 就回落到记录值。
"""
from __future__ import annotations

import os
import tempfile

from djb_node import facts
from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw


def _source(tmp: str, status: dict):
    return ds.LocalDataSource(home=os.path.join(tmp, "home"),
                              update_status=lambda: status)


def test_update_detail_keeps_package_version():
    with tempfile.TemporaryDirectory() as tmp:
        src = _source(tmp, {"current_version": "0.3.15",
                            "package_version": "0.3.16",
                            "install_record_version": "0.3.15",
                            "channel": "stable", "verify_state": "recorded"})
        detail = src._update_detail()
        assert detail["package_version"] == "0.3.16"
        assert detail["install_record_version"] == "0.3.15"
        assert detail["current_version"] == "0.3.15"      # 既有键语义不变


def test_account_view_shows_running_version_not_record():
    account: dict = {}
    account["update"] = {"current_version": "0.3.15", "package_version": "0.3.16",
                         "install_record_version": "0.3.15", "channel": "stable",
                         "verify_state": "recorded", "checked_at": "", "next_version": ""}
    sections = {"account": account}
    view = vw.build_view("account", ds.Snapshot(sections))
    text = view.plain_text()
    assert "0.3.16" in text
    # 记录值只允许出现在「已按实际程序显示」的提示里，不能当主值
    assert "当前版本：0.3.15" not in text
    assert facts.CLIENT_VERSION