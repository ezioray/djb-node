# SPDX-License-Identifier: Apache-2.0
"""UX-E2E-20261121 P1-4：「提供 Token Plan」页的信息架构与费率说明。

走查原文：单页 **8 个并列按钮**、没有主操作；费率写成「平台服务费 15%（需方实付 =
你的报价 ÷ 1.15）」——客户算不出来也看不出钱从哪边出；密钥说明是病句
「云侧只收「已登记」这个布」。

这里的断言全部落在**客户能看见的字**上，且费率示例的数字由 ``facts`` 现算
（不在这里手写第二份口径——两份口径就是下一次事故）。
"""
from __future__ import annotations

from djb_node import facts
from djb_node.desktop import controller as desktop_controller
from djb_node.desktop import views as vw
from djb_node.desktop.datasource import FakeDataSource, Snapshot
from djb_node.desktop.views import action_groups, build_view

ACCESS, OPS, PRICE = (vw.GROUP_TP_ACCESS, vw.GROUP_TP_OPS, vw.GROUP_TP_PRICE)


def _snapshot(**plan) -> Snapshot:
    """空页形状（没上游、没模型、云端未开通），与真机首装一致。"""
    base = {"upstreams": [],
            "protocol": facts.DEFAULT_UPSTREAM_PROTOCOL,
            "base_url": "", "secret_seen": "",
            "bids": {"rows": {}, "cloud": {"state": "pending"},
                     "report_blocker": "上报开关关闭", "preview": {"models": []}}}
    base.update(plan)
    return Snapshot({"token_plan": base, "access": {"binding": {}}, "account": {},
                     "status": {"state": "stopped"}, "receipts": {"rows": []}})


def _label(view, key: str) -> str:
    return next(item.label for item in view.actions if item.key == key)


# ───────────────────────────────────────────── 1 一棵主操作 + 三块
def test_exactly_one_primary_action_on_every_stage():
    """走查「8 个并列按钮」：无论走到哪一步，屏幕上只能有一颗主按钮。"""
    stages = [
        _snapshot(),                                        # 没上游
        _snapshot(base_url="https://api.example.com/v1", secret_seen="已保存"),  # 有上游
    ]
    for snapshot in stages:
        view = build_view("token_plan", snapshot)
        primaries = [item.key for item in view.actions if item.primary]
        assert len(primaries) == 1, primaries


def test_actions_are_grouped_in_the_order_you_do_them():
    view = build_view("token_plan", _snapshot())
    titles = [title for title, _ in action_groups(view.actions)]
    assert titles == [ACCESS, OPS, PRICE]                   # 三块，且只有三块


def test_each_group_holds_what_it_says():
    view = build_view("token_plan", _snapshot())
    groups = {title: [item.key for item in items] for title, items in action_groups(view.actions)}
    assert groups[ACCESS] == ["token_plan.add_upstream", "token_plan.discover",
                              "token_plan.test_connectivity", "token_plan.enable_relay"]
    assert groups[OPS] == ["token_plan.bind_node", "token_plan.refresh"]
    assert groups[PRICE] == ["token_plan.save_price", "token_plan.preview_report",
                             "token_plan.report"]


def test_save_comes_before_test_so_the_three_steps_cannot_be_misread():
    """空态写「② 保存 → ③ 测一次」，按钮顺序必须一致。

    反过来的话客户会先点「测试联通」——那条**不保存**，填完等于白填（P1-4 的同一个坑）。
    """
    view = build_view("token_plan", _snapshot())
    keys = [item.key for item in view.actions]
    assert keys.index("token_plan.discover") < keys.index("token_plan.test_connectivity")
    empty = view.table.empty
    assert empty.index("读取模型清单并保存接入") < empty.index("测试联通")


# ───────────────────────────────────────────── 2 空态三步
def test_empty_state_gives_three_actionable_steps():
    view = build_view("token_plan", _snapshot())
    text = view.table.empty
    for marker in ("①", "②", "③", "请求地址", "密钥", "读取模型清单并保存接入", "测试联通"):
        assert marker in text, marker
    # 页面顶部的说明与表格空态同一句（客户在哪块看到都知道下一步点哪）
    assert any(text in note for note in view.notes)
    assert "去接入配置" not in text          # 那颗按钮 2026-11-19 已经拆掉了


# ───────────────────────────────────────────── 3 费率说明（与 facts 同一口径）
def test_fee_hint_has_a_computable_example_that_matches_facts():
    view = build_view("token_plan", _snapshot())
    hint = next(item.hint for item in view.fields if item.key == "min_price")
    pct = format(facts.service_fee_percent(), "g")
    paid = facts.customer_pay_text(vw.FEE_EXAMPLE_ASK)
    assert pct + "%" in hint
    assert vw.FEE_EXAMPLE_ASK + " → 客户实付 " + paid in hint, hint
    assert "到手 " + vw.FEE_EXAMPLE_ASK in hint             # 平台费不倒扣供方
    assert "÷" not in hint and "1.15" not in hint.replace("115.00", "")   # 走查点名的算式退场


def test_fee_hint_follows_facts_when_the_rate_changes(monkeypatch):
    """费率是服务端事实：换成 20% 时界面必须跟着变（说明它不是手写的 15）。"""
    monkeypatch.setattr(facts, "SERVICE_FEE_BPS", 2000)
    view = build_view("token_plan", _snapshot())
    hint = next(item.hint for item in view.fields if item.key == "min_price")
    assert "20%" in hint and facts.customer_pay_text(vw.FEE_EXAMPLE_ASK) in hint


# ───────────────────────────────────────────── 4 病句
def test_secret_hint_is_a_sentence_not_a_fragment():
    view = build_view("token_plan", _snapshot())
    hint = next(item.hint for item in view.fields if item.key == "upstream_secret")
    assert "这个布" not in hint and "这一个布尔" not in hint
    assert "只存本机" in hint and "云端" in hint


# ───────────────────────────────────────────── 5 第一步的动作说明与空态同序
def test_add_upstream_message_matches_the_three_steps():
    controller = desktop_controller.DesktopController(FakeDataSource({}))
    message = controller.action("token_plan.add_upstream").message
    assert "读取模型清单并保存接入" in message and "测试联通" in message
    assert message.index("读取模型清单并保存接入") < message.index("测试联通")
