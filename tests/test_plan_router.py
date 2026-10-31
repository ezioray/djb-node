# SPDX-License-Identifier: AGPL-3.0-or-later
"""多 plan 组：多模型共享预算 + 同模型自动切换（纯逻辑，无 IO）。"""
from __future__ import annotations

import pytest

from djb_node import plan_router as pr


def _member(uid: str, models: str, *, priority: int = 0, weight: int = 1,
            enabled: bool = True) -> dict:
    return {"upstream_id": uid, "models": models, "priority": priority,
            "weight": weight, "enabled": enabled}


def _group(name: str, members: list, *, priority: int = 0, day: int | None = None,
           month: int | None = None, spent_day: int = 0, spent_month: int = 0,
           enabled: bool = True, downgrade: bool = False,
           fallbacks: list | None = None) -> dict:
    return {"name": name, "enabled": enabled, "priority": priority, "members": members,
            "budget_usd_minor_day": day, "budget_usd_minor_month": month,
            "spent_usd_minor_day": spent_day, "spent_usd_minor_month": spent_month,
            "allow_model_downgrade": downgrade, "model_fallbacks": fallbacks or []}


# --------------------------------------------------------------- 模型匹配
def test_model_exact_match():
    assert pr.is_model_supported("gpt-4o,claude-3-5-sonnet", "gpt-4o") is True
    assert pr.is_model_supported("gpt-4o", "gpt-4") is False
    assert pr.is_model_supported("gpt-4o", "GPT-4O") is False   # 大小写敏感


def test_wildcard_and_empty():
    assert pr.is_model_supported("*", "anything") is True
    assert pr.is_model_supported("", "m") is False
    assert pr.is_model_supported(None, "m") is False
    assert pr.is_model_supported("gpt-4o", "") is False


# --------------------------------------------------------------- 选择
def test_pick_by_priority():
    groups = [_group("b", [_member("u2", "*")], priority=1),
              _group("a", [_member("u1", "*")], priority=0)]
    target = pr.pick_target(groups, model="m")
    assert (target.group, target.upstream_id) == ("a", "u1")


def test_multi_model_share_group_budget():
    """一个组里两个模型落在两个 plan 上，共用同一个预算池。"""
    group = _group("g", [_member("u-gpt", "gpt-4o"), _member("u-claude", "claude-3-5-sonnet")],
                   day=1000)
    assert pr.pick_target([group], model="gpt-4o").upstream_id == "u-gpt"
    assert pr.pick_target([group], model="claude-3-5-sonnet").upstream_id == "u-claude"


def test_model_not_supported_returns_none():
    group = _group("g", [_member("u1", "gpt-4o")])
    assert pr.pick_target([group], model="llama-3") is None


def test_disabled_member_skipped():
    group = _group("g", [_member("u1", "*", enabled=False), _member("u2", "*")])
    assert pr.pick_target([group], model="m").upstream_id == "u2"


def test_disabled_group_skipped():
    groups = [_group("off", [_member("u1", "*")], enabled=False),
              _group("on", [_member("u2", "*")], priority=1)]
    assert pr.pick_target(groups, model="m").group == "on"


# --------------------------------------------------------------- 共享预算
def test_day_budget_exhausted_skips_group():
    groups = [_group("a", [_member("u1", "*")], day=1000, spent_day=1000),
              _group("b", [_member("u2", "*")], priority=1)]
    assert pr.pick_target(groups, model="m").group == "b"


def test_month_budget_exhausted_skips_group():
    group = _group("a", [_member("u1", "*")], month=5000, spent_month=5000)
    assert pr.pick_target([group], model="m") is None


def test_unlimited_budget_never_blocks():
    group = _group("a", [_member("u1", "*")], day=None, month=None, spent_day=10**9)
    assert pr.pick_target([group], model="m") is not None


def test_shared_pool_budget_covers_all_models():
    """组里两个模型花的是同一个池子：耗尽后两个模型都不可用。"""
    group = _group("g", [_member("u-gpt", "gpt-4o"), _member("u-claude", "claude-3-5-sonnet")],
                   day=1000, spent_day=1000)
    assert pr.pick_target([group], model="gpt-4o") is None
    assert pr.pick_target([group], model="claude-3-5-sonnet") is None


def test_cost_deducted_once():
    """组池与模型明细扣同一个值，但只扣一次（函数语义钉住）。"""
    pool, per_model = pr.split_cost(250)
    assert pool == per_model == 250
    assert pr.split_cost(None) == (0, 0)


# --------------------------------------------------------------- 切换规则
@pytest.mark.parametrize("status,expected", [(429, True), (401, True), (403, True),
                                             (500, True), (502, True), (503, True),
                                             (504, True), (400, False), (404, False),
                                             (200, False), (None, False)])
def test_should_failover_statuses(status, expected):
    assert pr.should_failover(status)[0] is expected


@pytest.mark.parametrize("kind,expected", [("timeout", True), ("connect", True),
                                           ("tls", True), ("bad_request", False),
                                           (None, False)])
def test_should_failover_transport(kind, expected):
    assert pr.should_failover(None, kind)[0] is expected


def test_auth_failure_cools_down():
    state, target = pr.GroupState(), pr.PlanTarget("g", "u1", "m")
    failover, reason = pr.record_outcome(state, target, status=401, now=1000.0)
    assert (failover, reason) == (True, "auth")
    assert state.cooldown_until["u1"] == 1000.0 + pr.AUTH_COOLDOWN_S
    # 冷却期内不再被选
    group = _group("g", [_member("u1", "*"), _member("u2", "*")])
    assert pr.pick_target([group], model="m", state=state,
                          now=1000.0 + 10).upstream_id == "u2"
    # 冷却结束后可选
    assert pr.pick_target([group], model="m", state=state,
                          now=1000.0 + pr.AUTH_COOLDOWN_S + 1) is not None


def test_rate_limit_does_not_cool_down():
    state, target = pr.GroupState(), pr.PlanTarget("g", "u1", "m")
    assert pr.record_outcome(state, target, status=429, now=1000.0) == (True, "rate_limited")
    assert "u1" not in state.cooldown_until
    assert state.failures["u1"] == 1


def test_success_clears_failures():
    state, target = pr.GroupState(), pr.PlanTarget("g", "u1", "m")
    pr.record_outcome(state, target, status=503)
    assert state.failures["u1"] == 1
    assert pr.record_outcome(state, target, status=200) == (False, "")
    assert "u1" not in state.failures


def test_exclude_prevents_retry_same_plan():
    group = _group("g", [_member("u1", "*"), _member("u2", "*")])
    assert pr.pick_target([group], model="m", exclude={"u1"}).upstream_id == "u2"
    assert pr.pick_target([group], model="m", exclude={"u1", "u2"}) is None


# --------------------------------------------------------------- 跨模型降级
def test_downgrade_disabled_by_default():
    group = _group("g", [_member("u1", "gpt-4o-mini")], fallbacks=["gpt-4o-mini"])
    assert pr.pick_target([group], model="gpt-4o") is None


def test_downgrade_requires_explicit_opt_in():
    group = _group("g", [_member("u1", "gpt-4o-mini")], downgrade=True,
                   fallbacks=["gpt-4o-mini"])
    target = pr.pick_target([group], model="gpt-4o")
    assert target.model == "gpt-4o-mini"
    assert target.reason == "model_downgrade"


def test_downgrade_never_changes_model_when_disabled():
    """切换 plan 时模型保持不变：断言选中的 model 与原请求一致。"""
    group = _group("g", [_member("u1", "gpt-4o"), _member("u2", "gpt-4o")])
    assert pr.pick_target([group], model="gpt-4o").model == "gpt-4o"


# --------------------------------------------------------------- 权重
def test_weighted_order_is_deterministic_with_picker():
    group = _group("g", [_member("u1", "*", weight=1), _member("u2", "*", weight=3)])
    assert pr.pick_target([group], model="m", picker=lambda total: 0).upstream_id == "u1"
    # total=4，ticket=2 落在第二个（权重 3）区间
    assert pr.pick_target([group], model="m", picker=lambda total: 2).upstream_id == "u2"


def test_priority_beats_weight():
    group = _group("g", [_member("u1", "*", priority=1, weight=9),
                         _member("u2", "*", priority=0, weight=1)])
    assert pr.pick_target([group], model="m", picker=lambda total: total - 1).upstream_id == "u2"
