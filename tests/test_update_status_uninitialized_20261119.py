# SPDX-License-Identifier: Apache-2.0
"""2026-11-19 验收缺陷：空 home（未 djb init）下 update.status() 不许拖垮版本显示。

报障：空 home 启动客户端 → 账户页「当前版本」= 暂不可用，而侧栏写着 v0.3.14。
根因：status() 里 self.store.install() 抛 StoreNotInitializedError（NotInstalledError
子类），整个 status() 失败；但 package_version 的来源是 facts.CLIENT_VERSION（正在跑的
程序版本），不依赖安装记录。修法：只让 install_record_version 为空，package_version /
current_version 仍然给出真实程序版本。
"""
from __future__ import annotations

from types import SimpleNamespace

from djb_node import facts
from djb_node.config import Paths
from djb_node.errors import StoreNotInitializedError
from djb_node.update import UpdateManager


class _UninitializedStore:
    """未 djb init 的 home：update_state 可读（空记录），install 抛。"""

    def update_state(self):
        return SimpleNamespace(current_version=None, next_version=None, rollback_to=None,
                               verify_state=None, checked_at=None)

    def install(self):
        raise StoreNotInitializedError("安装记录缺失：先执行 djb init")


def test_status_keeps_package_version_without_install_record(tmp_path):
    manager = UpdateManager(Paths(str(tmp_path)), _UninitializedStore())
    status = manager.status()
    assert status["package_version"] == facts.CLIENT_VERSION
    assert status["current_version"] == facts.CLIENT_VERSION
    assert status["install_record_version"] == ""
    assert status["bucket"] is None
