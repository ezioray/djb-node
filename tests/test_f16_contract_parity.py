# SPDX-License-Identifier: Apache-2.0
"""F16 · 契约键位与云端实现的静态一致性检查（跨仓，缺仓自动跳过）。

`test_desktop_nodecloud_http.py` 锁的是**客户端对某个形状的处理**；这里锁的是
**那个形状还是云端实际返回的**——直接读 `repos/djb-cloud/app/metering/node_console.py`
的语法树，把四个端点返回字典的键抓出来，和客户端依赖的键逐一比对。

为什么值得单独一层：云端不在本仓，接口一改键（`data`/`next_cursor`/`amount_micro`…）
两边 CI 各跑各的谁都不红，等真联调才发现。这里用 AST 读源码，不需要数据库、不写任何东西。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import cross_repo

NODE = Path(__file__).resolve().parents[1]
CLOUD = NODE.parent / "djb-cloud" / "app" / "metering" / "node_console.py"

#: 缺兄弟仓时**不许静默跳过**（task-15）：登记 ENV-MISSING，严格模式下直接失败。
#  为什么不用 `pytest.mark.skipif`：skip 是"本环境测不了"的**结论**，不是"通过"；
#  用 skipif 时它和普通 skip 长得一样 ⇒ 同一 commit 因 stage 布局不同 11/17 两副面孔，
#  值班侧把 +6 读成"跳过集与基线一致"，delta 建在流沙上（已由 lead 落
#  ops/release/TEST-BASELINE-CORRECTION-2026-11-05.md 纠正口径）。
cross_repo.require_file(
    CLOUD,
    what="f16 跨仓契约检查（本模块全部用例）",
    reason="同仓外的 djb-cloud 不在预期路径（缺 ../djb-cloud/app/metering/node_console.py）",
    module_level=True,
)


def _returned_keys(function: str) -> set[str]:
    """函数体里所有字典字面量的键。

    刻意放宽到「函数内全部 dict」：行字典常写在列表推导/中间变量里（`rows = [...]`
    后再 `_page(rows, ...)`），只盯 `return {...}` 会漏掉真正的字段。
    """
    tree = ast.parse(CLOUD.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Dict):
                for key in inner.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
    assert keys, "没在 " + function + " 里找到返回字典"
    return keys


CASES = {
    # 端点函数 → 客户端依赖的键（少一个就会显示「—」或直接判不可用）
    "tasks_page": {"task_id", "request_id", "state", "inflight", "tokens_in", "tokens_out",
                   "amount_micro", "started_at", "finished_at"},
    "receipts_page": {"receipt_id", "state", "issued_at", "revenue_minor", "tokens"},
    "earnings": {"settled_minor", "pending_minor", "corrected_minor", "rows",
                 "unavailable_reason", "currency", "next_settlement_at"},
    "profile": {"node_id", "state", "region", "platform", "capabilities", "bound",
                "last_seen_at"},
}


@pytest.mark.parametrize("function,needed", sorted(CASES.items()))
def test_client_keys_still_exist_in_cloud_implementation(function, needed):
    missing = needed - _returned_keys(function)
    assert not missing, function + " 少了客户端依赖的键：" + ", ".join(sorted(missing))


def test_pagination_keys_unchanged():
    keys = _returned_keys("_page")
    assert {"data", "page", "next_cursor", "has_more"} <= keys


def test_receipt_states_are_the_ones_client_shows_verbatim():
    """客户端**原样显示**云端状态词（accepted/rejected/expired），只另给中文。

    真数据联调时发现旧实现把 accepted 翻成 verified——本机一次校验都没做却说「已验证」。
    现在词表变了也要一起改：这里挂一句话，改的时候能看到。
    """
    source = CLOUD.read_text(encoding="utf-8")
    for word in ("accepted", "rejected", "expired"):
        assert word in source, "收据状态词表里没有 " + word
    client = NODE / "djb_node" / "desktop" / "nodecloud.py"
    text = client.read_text(encoding="utf-8")
    for word in ("accepted", "rejected", "expired"):
        assert word in text, "客户端的中文状态表没覆盖 " + word
