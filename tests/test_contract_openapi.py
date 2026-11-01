# SPDX-License-Identifier: Apache-2.0
"""契约测试：node 侧手写的**云端路径常量**必须存在于 `openapi.yaml`。

为什么需要这条（审计 FUNC-UX F3 的建议）：G4 就是「一个字符」——客户端写
`notifications/unread-count`，服务端是 `notifications/unread_count`，结果未读角标
100% 404，而所有单测都是绿的（因为测试替身照着客户端的错路径回）。这类缺口
只有「拿客户端的路径去对契约」才能提前挡住。

口径：
* 只用 stdlib 解析（正则抓 ``^  /path:`` 行），不引入 yaml 依赖；
* `openapi.yaml` 缺失则跳过（例如精简安装包）；
* 本地回环路径（`/v1/*`）不是云端契约，显式排除。
"""
from __future__ import annotations

import os
import re

import pytest

from djb_node import facts, protocol

_PATH_LINE = re.compile(r"^  (/[^\s:]+):\s*$", re.MULTILINE)


def _openapi_paths() -> set[str] | None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(root, "openapi.yaml")
    if not os.path.exists(candidate):
        return None
    with open(candidate, "r", encoding="utf-8") as handle:
        return set(_PATH_LINE.findall(handle.read()))


def _node_cloud_paths() -> dict[str, str]:
    """node 引用的**云端**路径（排除本机回环）。"""
    paths = {name: value for name, value in protocol.PATHS.items()
             if not value.startswith(protocol.PATHS["local_health"].rsplit("/", 1)[0])}
    paths.update({
        "notifications": facts.NOTIFICATIONS_PATH,
        "notifications_unread": facts.NOTIFICATIONS_UNREAD_PATH,
        "model_catalog": facts.MODEL_CATALOG_PATH,
        "model_sync_state": facts.MODEL_SYNC_STATE_PATH,
    })
    return paths


@pytest.mark.parametrize("name,path", sorted(_node_cloud_paths().items()))
def test_node_path_exists_in_openapi(name, path):
    known = _openapi_paths()
    if known is None:
        pytest.skip("openapi.yaml 不在本仓")
    assert path in known, ("node 常量 " + name + "=" + path
                           + " 不在 openapi.yaml 的 paths 里（G4 类缺口）")


def test_notification_paths_use_spec_spelling():
    """显式钉住 G4：下划线 + 响应字段 ``unread``。"""
    assert facts.NOTIFICATIONS_UNREAD_PATH.endswith("/notifications/unread_count")
    assert "unread-count" not in facts.NOTIFICATIONS_UNREAD_PATH
    assert facts.NOTIFICATIONS_UNREAD_FIELDS[0] == "unread"
