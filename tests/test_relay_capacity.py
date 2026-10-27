# SPDX-License-Identifier: Apache-2.0
"""C9 接线 · relay 发现结果进入容量公告（task-4）。

口径：
* 只公告 ``state=mapped`` + ``enabled`` 的模型（**未映射不公告**）；
* ``discovered_at`` 超过 ``facts.RELAY_FRESH_S``（900s）→ 出行但 ``max_qps=0``；
* 关闭 relay 开关时调用方不传 relay 目标，公告形状与旧版逐字相同（只多不少）。
"""
from __future__ import annotations

from datetime import timedelta

from djb_node import facts
from djb_node.config import feature_flag, flag_spec
from djb_node.relay import (RelayProfile, capacity_targets, discovery_age_s,
                            discovery_fresh, relay_enabled, save_profile)
from djb_node.upstream import UpstreamManager
from djb_node.util import iso

BASE_URL = "https://relay.example.com/v1"


def _manager(store, credentials):
    return UpstreamManager(store, credentials)


def _record(manager, *, models="gpt-4o,gpt-4o-mini,other-model", access="B"):
    return manager.add(access_type=access, base_url=BASE_URL, models=models)


def _relay(store, record, *, discovered_at, mapped=("gpt-4o",), enabled=True):
    profile = RelayProfile(
        upstream_id=record.id, base_url=BASE_URL,
        models=[
            {"id": "gpt-4o", "canonical": "openai/gpt-4o",
             "state": "mapped" if "gpt-4o" in mapped else "unmapped",
             "source": "auto", "enabled": enabled},
            {"id": "gpt-4o-mini", "canonical": "openai/gpt-4o-mini",
             "state": "mapped" if "gpt-4o-mini" in mapped else "unmapped",
             "source": "auto", "enabled": True},
            {"id": "other-model", "canonical": None, "state": "unmapped",
             "source": None, "enabled": True},
        ],
        discovered_at=discovered_at, updated_at=discovered_at)
    return save_profile(store, profile)


def test_mapped_relay_model_enters_capacity(store, credentials, clock):
    manager = _manager(store, credentials)
    record = _record(manager)
    profile = _relay(store, record, discovered_at=iso(clock()))
    targets = capacity_targets([profile], now=clock())
    entries = manager.announce_capacity(store.profile(), now=clock(), relay=targets)

    assert [item["model"] for item in entries] == ["gpt-4o"]
    assert entries[0]["max_qps"] > 0
    assert entries[0]["access"] == "B"
    assert entries[0]["canonical"] == "openai/gpt-4o"
    assert entries[0]["unit"] == facts.DEFAULT_MIN_PRICE_UNIT


def test_unmapped_relay_model_is_not_announced(store, credentials, clock):
    manager = _manager(store, credentials)
    record = _record(manager)
    profile = _relay(store, record, discovered_at=iso(clock()), mapped=())
    targets = capacity_targets([profile], now=clock())
    entries = manager.announce_capacity(store.profile(), now=clock(), relay=targets)

    assert entries == []


def test_stale_discovery_keeps_row_with_zero_qps(store, credentials, clock):
    manager = _manager(store, credentials)
    record = _record(manager)
    stale = iso(clock() - timedelta(seconds=facts.RELAY_FRESH_S + 1))
    profile = _relay(store, record, discovered_at=stale)

    assert discovery_age_s(profile, clock()) == facts.RELAY_FRESH_S + 1
    assert discovery_fresh(profile, clock()) is False

    targets = capacity_targets([profile], now=clock())
    entries = manager.announce_capacity(store.profile(), now=clock(), relay=targets)
    assert [item["model"] for item in entries] == ["gpt-4o"]
    assert entries[0]["max_qps"] == 0.0


def test_fresh_boundary_is_inclusive(store, credentials, clock):
    manager = _manager(store, credentials)
    record = _record(manager)
    edge = iso(clock() - timedelta(seconds=facts.RELAY_FRESH_S))
    profile = _relay(store, record, discovered_at=edge)
    assert discovery_fresh(profile, clock()) is True
    entries = manager.announce_capacity(
        store.profile(), now=clock(), relay=capacity_targets([profile], now=clock()))
    assert entries[0]["max_qps"] > 0


def test_no_relay_targets_keeps_legacy_shape(store, credentials, clock):
    """开关关闭（不传 relay）时：行为与旧版逐字一致 —— 全部出行、QPS=0、无 canonical。"""
    manager = _manager(store, credentials)
    _record(manager)
    entries = manager.announce_capacity(store.profile(), now=clock())
    assert [item["model"] for item in entries] == ["gpt-4o", "gpt-4o-mini", "other-model"]
    assert all(item["max_qps"] == 0.0 for item in entries)
    assert all("canonical" not in item for item in entries)


def test_relay_flag_default_off_and_three_fields():
    spec = flag_spec(facts.RELAY_FLAG)
    assert spec["owner"] and spec["expire_at"] and spec["rollback_rule"]
    assert spec["default"] is False

    class _Config:
        extra: dict = {}

    assert relay_enabled(_Config()) is False
    assert feature_flag(_Config(), facts.RELAY_FLAG) is False
