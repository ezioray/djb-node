# SPDX-License-Identifier: Apache-2.0
"""M17 §9 特性开关（客户端侧）：三字段齐全 + 默认关闭 + 统一走 config.feature_flag。

Lead 集成口径：开关只登记在 ``facts.FEATURE_FLAGS``，取值统一走
``djb_node.config.feature_flag``，不另起第二套机制。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from djb_node import facts
from djb_node.attest import attestation_enabled
from djb_node.config import (ClientConfig, _parse_expire_at, feature_flag, flag_expired,
                             flag_spec, flag_env_name)
from djb_node.relay import relay_enabled
from tests.feature_flag_policy import DEFAULT_ON_APPROVED

EXPIRE_DAY = date(2027, 6, 30)    # facts.FEATURE_FLAGS 全表统一的到期日（0.3.4 续期后）
AFTER_EXPIRE = date(2027, 7, 1)


def make_config(**extra) -> ClientConfig:
    config = ClientConfig()
    config.extra = dict(extra)
    return config


#: 各开关自己的到期日（默认 2027-06-30；按任务单另定的在这里列明）
EXPIRE_OVERRIDES = {facts.PLAN_GROUPS_FLAG: "2027-06-30",
                    # M3 机器画像上报：跟 E2E 周期一起，2027-06-30 前必须收敛成默认开或删掉
                    facts.MACHINE_PROFILE_FLAG: "2027-06-30",
                    # ROUND3 / C-01b：批准窗口写到「日期时间 + 时区」（DECISIONS.md §D-R3-01），
                    # 登记表原样保留这个写法，求值只取该时区里的日期（config._parse_expire_at）
                    facts.PROBE_FLAG_PERIODIC: "2027-06-30T00:00:00+08:00"}


def test_every_flag_has_three_fields_and_default_off():
    assert facts.FEATURE_FLAGS, "客户端至少要登记 M17 的两条开关"
    for name, spec in facts.FEATURE_FLAGS.items():
        for field_name in ("owner", "expire_at", "rollback_rule", "default", "description"):
            assert str(spec.get(field_name, "")).strip(), name + " 缺少 " + field_name
        if name in DEFAULT_ON_APPROVED:
            # 例外：必须**确实是开**的，且回滚三字段齐全（否则「开着的开关」回滚不回去）
            assert spec["default"] is True, name + " 已批准默认开启，不应是关闭"
            for field_name in ("owner", "expire_at", "rollback_rule"):
                assert str(spec.get(field_name, "")).strip(), name + " 例外仍需 " + field_name
        else:
            assert spec["default"] is False, name + " 必须默认关闭（要开口子请登记 DEFAULT_ON_APPROVED + 批准依据）"
        assert spec["expire_at"] == EXPIRE_OVERRIDES.get(name, "2027-06-30")
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
    for name, spec in facts.FEATURE_FLAGS.items():
        expiry = _parse_expire_at(spec["expire_at"])
        assert expiry is not None, name + " 的 expire_at 必须可解析（否则 fail-closed 直接关）"
        assert flag_expired(name, today=far_future) is True, name
        assert feature_flag(all_on, name, today=far_future) is False, name
        # 到期日**当天仍有效**、次日起强制关（按各开关自己的 expire_at，不写死日期：
        # C-01b 的批准窗口是 2026-12-15，写死 2026-12-30 就会把「还没到期」判成错）
        assert flag_expired(name, today=expiry) is False, name
        assert flag_expired(name, today=expiry + timedelta(days=1)) is True, name


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


def test_expire_at_accepts_iso_datetime_with_offset():
    """批准窗口允许写成「日期时间 + 时区」（ROUND3 / D-R3-01）。

    修前 `_parse_expire_at` 只认 `date.fromisoformat`，`2026-12-15T00:00:00+08:00` 直接
    ValueError → `flag_expired` fail-closed 返回 True ⇒ 批准窗口把开关**立刻关掉**，
    而登记表上看起来「写着 2026-12-15」，没人会想到它其实已经到期。
    """
    assert _parse_expire_at("2026-12-15T00:00:00+08:00") == date(2026, 12, 15)
    # 取**写明的时区**里的日期，不换算到 UTC：+08:00 的 12-16 00:30 = UTC 12-15 16:30，
    # 若按 UTC 取日期会少一天（12-15），批准窗口就提前一天结束。
    assert _parse_expire_at("2026-12-16T00:30:00+08:00") == date(2026, 12, 16)
    assert _parse_expire_at("2026-12-15") == date(2026, 12, 15)
    assert _parse_expire_at("2026-12-15T00:00:00Z") == date(2026, 12, 15)


def test_probe_periodic_flag_is_the_registered_default_on_case():
    """C-01b 的三字段必须与 DECISIONS.md §D-R3-01 逐字一致（批准留痕的可核对性）。"""
    spec = flag_spec(facts.PROBE_FLAG_PERIODIC)
    assert spec["default"] is True
    assert spec["owner"] == "client-seat"
    assert spec["expire_at"] == "2027-06-30T00:00:00+08:00"   # 2026-11-09 复核续期（原 12-15）
    assert "djb flags --disable" in spec["rollback_rule"]
    assert _parse_expire_at(spec["expire_at"]) == date(2027, 6, 30)
    # 干净 home（没有任何显式配置、没有环境变量）下就是开着的
    assert feature_flag(ClientConfig(), facts.PROBE_FLAG_PERIODIC) is True


def test_env_var_overrides_config_and_default(monkeypatch):
    """环境变量闸门：`DJB_FLAG_<NAME>` 压过配置与默认值（ROUND3 只加不改）。

    它是回滚规则里写的那条「一键停」：不需要改配置文件、不需要碰代码。
    """
    assert flag_env_name(facts.PROBE_FLAG_PERIODIC) == \
        "DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED"
    env = flag_env_name(facts.PROBE_FLAG_PERIODIC)
    monkeypatch.delenv(env, raising=False)
    assert feature_flag(ClientConfig(), facts.PROBE_FLAG_PERIODIC) is True

    monkeypatch.setenv(env, "false")           # 压过默认开
    assert feature_flag(ClientConfig(), facts.PROBE_FLAG_PERIODIC) is False
    on = make_config(feature_flags={facts.PROBE_FLAG_PERIODIC: True})
    assert feature_flag(on, facts.PROBE_FLAG_PERIODIC) is False   # 也压过配置里的 true

    monkeypatch.setenv(env, "true")             # 反过来也成立（默认关的开关可临时开）
    off = make_config(feature_flags={facts.ATTEST_FLAG: False})
    monkeypatch.setenv(flag_env_name(facts.ATTEST_FLAG), "on")
    assert feature_flag(off, facts.ATTEST_FLAG) is True

    monkeypatch.setenv(env, "   ")              # 空值 = 没设，继续看配置/默认
    assert feature_flag(ClientConfig(), facts.PROBE_FLAG_PERIODIC) is True


def test_env_var_cannot_resurrect_expired_flag(monkeypatch):
    """到期是硬闸门：环境变量写 true 也不能把过期开关重新打开（fail-closed 优先）。

    「到期日」从开关自己的 expire_at 推（+1 天），续期后本用例仍然成立 —— 写死日期
    会让每次复核续期都要改测试，反而容易改错。
    """
    monkeypatch.setenv(flag_env_name(facts.PROBE_FLAG_PERIODIC), "true")
    expired_day = _parse_expire_at(flag_spec(facts.PROBE_FLAG_PERIODIC)["expire_at"]) + timedelta(days=1)
    assert feature_flag(ClientConfig(), facts.PROBE_FLAG_PERIODIC, today=expired_day) is False


def test_flag_readers_inherit_expiry(monkeypatch):
    # relay/attest 等 reader 都走 feature_flag，到期后应自动跟随为 False
    on = make_config(feature_flags={facts.RELAY_FLAG: True, facts.ATTEST_FLAG: True})
    assert relay_enabled(on) is True and attestation_enabled(on) is True
    monkeypatch.setattr("djb_node.config._utc_today", lambda: date(2031, 1, 1))
    assert relay_enabled(on) is False
    assert attestation_enabled(on) is False
