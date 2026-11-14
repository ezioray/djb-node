# SPDX-License-Identifier: Apache-2.0
"""RC14 E：IA 拆分（绑定/收款/安全 与 Token Plan 核页分开）的可验收断言。

报告 §E 的客户投诉是「同一个动作散在好几页」：绑定在「接入配置」、收款也在「接入配置」、
Token Plan 页只有一颗「去接入配置（添加上游 / 绑定节点）」——两件事挤一句话，点过去那页
既不能添加上游、绑定按钮也不在该页可达的位置。

本文件用 **AST** 而不是 grep 来做断言：``_access`` 里出现 ``binding`` 这个词是正常的
（它要显示「已绑定 / 未绑定」状态），但**动作键**里不许再有 ``bind``。
所以断言口径是「每个视图函数里出现的动作键 / 字段键」，精确且不会被同名词误伤。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw

SOURCE = pathlib.Path(vw.__file__)
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
KEY_RE = re.compile(r"^(account|access|token_plan|agent|models|update)\.[a-z0-9_]+$")

#: 只有「账户与安全」允许出现的动作键（绑定 / 解绑 / 收款 / 2FA / 更新）
ACCOUNT_ONLY_ACTIONS = {
    "account.bind", "account.unbind", "account.logout", "account.submit_payout",
    "account.enable_2fa", "account.open_2fa_page", "account.open_web",
    "update.check", "update.apply", "update.download",
}
#: 只有「账户与安全」允许出现的字段键（收款地址）
ACCOUNT_ONLY_FIELDS = {"usdt_address"}


def _view_functions() -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_"):
            out[node.name] = node
    return out


def _action_keys(func: ast.FunctionDef) -> set[str]:
    """函数里 ``Action("xxx.yyy"`` 的第一个参数（动作键）。"""
    keys: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "Action" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            value = first.value
        elif isinstance(first, ast.JoinedStr):          # f-string：取不出静态值就跳过
            continue
        else:
            continue
        if KEY_RE.match(value):
            keys.add(value)
    return keys


def _field_keys(func: ast.FunctionDef) -> set[str]:
    """函数里 ``Field(..., key="xxx")`` 的 key 关键字。"""
    keys: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "Field":
            continue
        for keyword in node.keywords:
            if keyword.arg == "key" and isinstance(keyword.value, ast.Constant):
                keys.add(str(keyword.value.value))
    return keys


def test_bind_and_payout_actions_only_live_on_the_account_view():
    """**可 grep 的验收断言**：绑定 / 解绑 / 收款 / 2FA / 更新只出现在 ``_account``。"""
    functions = _view_functions()
    offenders: list[str] = []
    for name, func in functions.items():
        if name == "_account":
            continue
        hit = _action_keys(func) & ACCOUNT_ONLY_ACTIONS
        if hit:
            offenders.append(name + " → " + ", ".join(sorted(hit)))
    assert not offenders, ("这些视图里也出现了只该在「账户与安全」的动作：\n  "
                           + "\n  ".join(offenders))


def test_usdt_field_only_lives_on_the_account_view():
    functions = _view_functions()
    offenders = [name for name, func in functions.items()
                 if name != "_account" and (_field_keys(func) & ACCOUNT_ONLY_FIELDS)]
    assert not offenders, "USDT 收款地址字段出现在了：" + ", ".join(offenders)


def test_access_page_is_local_settings_and_carries_no_binding():
    """接入配置 → 本机设置：不含绑定动作，也没有收款字段。"""
    view = vw.build_view("access", desktop_app.demo_snapshot())
    assert view.title == "本机设置"
    keys = set(_action_keys(_view_functions()["_access"]))
    assert not [key for key in keys if "bind" in key], keys
    assert "access.submit_payout" not in set(action.key for action in view.actions)
    assert {"access.save", "access.copy_node_id", "access.reload"} <= set(
        action.key for action in view.actions)
    assert "usdt_address" not in {field.key for field in view.fields}


def test_token_plan_entry_is_split_into_two_actions():
    """``token_plan.open_access`` 那句「添加上游 / 绑定节点」必须拆成两颗。"""
    view = vw.build_view("token_plan", desktop_app.demo_snapshot())
    keys = [action.key for action in view.actions]
    assert "token_plan.open_access" not in keys, "旧的二合一按钮还在"
    assert "token_plan.add_upstream" in keys
    assert "token_plan.bind_node" in keys
    labels = {action.key: action.label for action in view.actions}
    assert "接入配置" not in labels["token_plan.add_upstream"]
    assert "接入配置" not in labels["token_plan.bind_node"]
    # 两颗动作跳到**不同**的地方：添加上游留在本页，绑定去账户页
    controller = desktop_app.make_controller(home=None, demo=True)
    assert controller.action("token_plan.add_upstream").payload == "token_plan"
    assert controller.action("token_plan.bind_node").payload == "account"
    assert controller.action("access.goto_account").payload == "account"


def test_bound_state_disables_the_bind_action_with_a_reason():
    """已绑定 ⇒ 「绑定这台电脑」置灰 + 说清解绑在哪（不是消失得无影无踪）。"""
    sections = dict(desktop_app.demo_snapshot().sections)
    access = dict(sections.get("access") or {})
    access["binding"] = {"state": "bound", "node_id": "node-7c31"}
    account = dict(sections.get("account") or {})
    account["profile"] = {"node_id": "node-7c31", "bound": True, "state": "active"}
    sections["access"] = access
    sections["account"] = account
    view = vw.build_view("token_plan", ds.Snapshot(sections=sections))
    actions = {action.key: action for action in view.actions}
    assert actions["token_plan.bind_node"].enabled is False
    assert "账户与安全" in actions["token_plan.bind_node"].reason


@pytest.mark.parametrize("key", ["token_plan.add_upstream", "token_plan.bind_node",
                                 "access.goto_account", "account.submit_payout"])
def test_new_actions_are_wired(key):
    """新键必须有对应处理函数（否则点下去是「未接线动作」）。"""
    controller = desktop_app.make_controller(home=None, demo=True)
    result = controller.action(key)
    assert "未接线" not in result.message, result.message


def test_views_source_has_no_open_access_action_left():
    assert "token_plan.open_access" not in SOURCE.read_text(encoding="utf-8")
