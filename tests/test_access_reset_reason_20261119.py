# SPDX-License-Identifier: Apache-2.0
"""2026-11-19 验收 P3：「重新注册这台机器」置灰理由拆两因。

旧文案把「只有节点被云端清退后才需要」与「当前原因：daemon 未运行」糊成一句，
节点没跑时读起来自相矛盾。修法：daemon 未运行时理由只留「节点进程未运行：先启动节点」。
"""
from __future__ import annotations

from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw
from djb_node.desktop.app import demo_snapshot


def _reset_action(snapshot):
    view = vw.build_view("access", snapshot)
    return next(a for a in view.actions if a.key == "access.reset_node")


def _snapshot_with_daemon(**daemon):
    sections = dict(demo_snapshot().sections)
    access = dict(sections["access"])
    access["daemon"] = daemon
    sections["access"] = access
    return ds.Snapshot(sections)


def test_reset_node_reason_when_daemon_not_running():
    action = _reset_action(_snapshot_with_daemon(running=False, reason="daemon 未运行",
                                                 state="stopped"))
    assert action.enabled is False
    assert "节点进程未运行" in action.reason
    assert "当前原因" not in action.reason


def test_reset_node_reason_when_daemon_running_but_not_retired():
    action = _reset_action(_snapshot_with_daemon(running=True, reason="conservative",
                                                 state="paused"))
    assert "只有节点被云端清退后才需要" in action.reason
    assert "conservative" in action.reason
