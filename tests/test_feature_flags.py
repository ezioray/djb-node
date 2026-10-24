# SPDX-License-Identifier: Apache-2.0
"""M17 §9 特性开关（客户端侧）：三字段齐全 + 默认关闭 + 统一走 config.feature_flag。

Lead 集成口径：开关只登记在 ``facts.FEATURE_FLAGS``，取值统一走
``djb_node.config.feature_flag``，不另起第二套机制。
"""
from __future__ import annotations

from djb_node import facts
from djb_node.attest import attestation_enabled
from djb_node.config import ClientConfig, feature_flag, flag_spec
from djb_node.relay import relay_enabled


def make_config(**extra) -> ClientConfig:
    config = ClientConfig()
    config.extra = dict(extra)
    return config


def test_every_flag_has_three_fields_and_default_off():
    assert facts.FEATURE_FLAGS, "客户端至少要登记 M17 的两条开关"
    for name, spec in facts.FEATURE_FLAGS.items():
        for field_name in ("owner", "expire_at", "rollback_rule", "default", "description"):
            assert str(spec.get(field_name, "")).strip(), name + " 缺少 " + field_name
        assert spec["default"] is False, name + " 必须默认关闭"
        assert spec["expire_at"] == "2026-12-31"
        assert "desc" not in spec, name + " 不要用 desc 字段（统一 description）"


def test_m17_flags_registered_with_expected_owners():
    relay = facts.FEATURE_FLAGS[facts.RELAY_FLAG]
    attest = facts.FEATURE_FLAGS[facts.ATTEST_FLAG]
    assert facts.RELAY_FLAG == "client.relay.enabled"
    assert facts.ATTEST_FLAG == "supply.attestation.enabled"
    assert relay["owner"] == "client-seat"
    assert attest["owner"] == "lead"
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
