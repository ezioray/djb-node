# SPDX-License-Identifier: Apache-2.0
"""M17 §9 特性开关（客户端侧）：三字段齐全 + 默认关闭 + 统一走 config.feature_flag。

Lead 集成口径：开关只登记在 ``facts.FEATURE_FLAGS``，取值统一走
``djb_node.config.feature_flag``，不另起第二套机制。
"""
from __future__ import annotations

from datetime import date

import pytest

from djb_node import facts
from djb_node.attest import attestation_enabled
from djb_node.config import ClientConfig, feature_flag, flag_expired, flag_spec
from djb_node.relay import relay_enabled

EXPIRE_DAY = date(2026, 12, 31)   # facts.FEATURE_FLAGS 全表统一的到期日
AFTER_EXPIRE = date(2027, 1, 1)


def make_config(**extra) -> ClientConfig:
    config = ClientConfig()
    config.extra = dict(extra)
    return config


#: 各开关自己的到期日（默认 2026-12-31；plan.groups 按任务单到 2027-06-30）
EXPIRE_OVERRIDES = {facts.PLAN_GROUPS_FLAG: "2027-06-30"}


def test_every_flag_has_three_fields_and_default_off():
    assert facts.FEATURE_FLAGS, "客户端至少要登记 M17 的两条开关"
    for name, spec in facts.FEATURE_FLAGS.items():
        for field_name in ("owner", "expire_at", "rollback_rule", "default", "description"):
            assert str(spec.get(field_name, "")).strip(), name + " 缺少 " + field_name
        assert spec["default"] is False, name + " 必须默认关闭"
        assert spec["expire_at"] == EXPIRE_OVERRIDES.get(name, "2026-12-31")
        assert "desc" not in spec, name + " 不要用 desc 字段（统一 description）"


def test_m17_flags_registered_with_expected_owners():
    relay = facts.FEATURE_FLAGS[facts.RELAY_FLAG]
    attest = facts.FEATURE_FLAGS[facts.ATTEST_FLAG]
    assert facts.RELAY_FLAG == "client.relay.enabled"
    assert facts.ATTEST_FLAG == "supply.attestation.enabled"
    assert relay["owner"] == "client-seat"
    assert attest["owner"] == "platform"
    assert "只读" in relay["rollback_rule"] and "只读" in attest["rollback_rule"]


def test_feature_flag_reads_nested_and_flat_extra():
    assert feature_flag(ClientConfig(), facts.RELAY_FLAG) is False
    nested = make_config(feature_flags={facts.RELAY_FLAG: True})
    assert feature_flag(nested, facts.RELAY_FLAG) is True
    flat = make_config(**{facts.RELAY_FLAG: True})
    assert feature_flag(flat, facts.RELAY_FLAG) is True
    for truthy in ("true", "ON", "1", "yes", 1):
        assert feature_flag(make_config(feature_flags={facts.ATTEST_FLAG: truthy}),
                           facts.ATTEST_FLAG) is True
    for falsy in ("off", "0", "", 0):
        assert feature_flag(make_config(feature_flags={facts.ATTEST_FLAG: falsy}),
                            facts.ATTEST_FLAG) is False


def test_unknown_flag_falls_back_to_default():
    # 未登记开关：没有显式取值时回落 default(False)；显式取值按 config 生效（C7 语义）
    assert feature_flag(ClientConfig(), "nope") is False
    assert feature_flag(make_config(feature_flags={"nope": True}), "nope") is True
    assert flag_spec("nope") == {}


def test_relay_and_attest_readers_delegate_to_feature_flag():
    off = ClientConfig()
    assert relay_enabled(off) is False and attestation_enabled(off) is False
    assert relay_enabled(None) is False and attestation_enabled(None) is False
    on = make_config(feature_flags={facts.RELAY_FLAG: True, facts.ATTEST_FLAG: True})
    assert relay_enabled(on) is True and attestation_enabled(on) is True
    mixed = make_config(feature_flags={facts.RELAY_FLAG: True})
    assert relay_enabled(mixed) is True and attestation_enabled(mixed) is False


# ── 到期强制关（task-1）：expire_at 当日仍有效，次日起 fail-closed ──────────────


def test_expire_at_day_is_inclusive_then_forces_off():
    on = make_config(feature_flags={facts.RELAY_FLAG: True})
    assert feature_flag(on, facts.RELAY_FLAG, today=EXPIRE_DAY) is True
    assert flag_expired(facts.RELAY_FLAG, today=EXPIRE_DAY) is False
    assert feature_flag(on, facts.RELAY_FLAG, today=AFTER_EXPIRE) is False
    assert flag_expired(facts.RELAY_FLAG, today=AFTER_EXPIRE) is True
    # 到期后即使显式写 true 也不能开（否则到期机制形同虚设）
    assert feature_flag(on, facts.RELAY_FLAG, today=date(2031, 6, 1)) is False


def test_default_off_flag_stays_false_after_expiry():
    assert feature_flag(ClientConfig(), facts.ATTEST_FLAG, today=AFTER_EXPIRE) is False


def test_every_registered_flag_is_forced_off_after_expiry():
    all_on = make_config(feature_flags={name: True for name in facts.FEATURE_FLAGS})
    far_future = date(2031, 1, 1)
    for name in facts.FEATURE_FLAGS:
        assert flag_expired(name, today=far_future) is True, name
        assert feature_flag(all_on, name, today=far_future) is False, name
        # 到期前一天仍按登记表判定（不被提前关）
        assert flag_expired(name, today=date(2026, 12, 30)) is False, name


@pytest.mark.parametrize("bad_expire", ["", "   ", None, "not-a-date", "2026-13-01", 20271231])
def test_malformed_expire_at_fails_closed(monkeypatch, bad_expire):
    name = "test.flag.malformed-expire"
    monkeypatch.setitem(facts.FEATURE_FLAGS, name, {
        "default": False,
        "owner": "test-seat",
        "expire_at": bad_expire,
        "rollback_rule": "关闭即回退",
        "description": "到期字段非法的登记项必须 fail-closed",
    })
    on = make_config(feature_flags={name: True})
    assert flag_expired(name) is True
    assert feature_flag(on, name) is False


def test_expire_at_accepts_date_object(monkeypatch):
    name = "test.flag.date-object"
    monkeypatch.setitem(facts.FEATURE_FLAGS, name, {
        "default": False,
        "owner": "test-seat",
        "expire_at": EXPIRE_DAY,
        "rollback_rule": "关闭即回退",
        "description": "expire_at 允许 date 对象（登记表用字符串，防御性兼容）",
    })
    on = make_config(feature_flags={name: True})
    assert feature_flag(on, name, today=EXPIRE_DAY) is True
    assert feature_flag(on, name, today=AFTER_EXPIRE) is False


def test_unregistered_flag_keeps_c7_semantics_without_expiry():
    # 未登记开关没有到期元数据：不额外收紧（C7 既有语义），也不报"已到期"
    assert flag_expired("nope") is False
    assert feature_flag(make_config(feature_flags={"nope": True}), "nope") is True


def test_flag_readers_inherit_expiry(monkeypatch):
    # relay/attest 等 reader 都走 feature_flag，到期后应自动跟随为 False
    on = make_config(feature_flags={facts.RELAY_FLAG: True, facts.ATTEST_FLAG: True})
    assert relay_enabled(on) is True and attestation_enabled(on) is True
    monkeypatch.setattr("djb_node.config._utc_today", lambda: date(2031, 1, 1))
    assert relay_enabled(on) is False
    assert attestation_enabled(on) is False
