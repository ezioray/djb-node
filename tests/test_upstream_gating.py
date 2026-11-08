# SPDX-License-Identifier: Apache-2.0
"""C7 · 公告门控（未验证 / 过期 / 被拒不进容量）+ 探针字段落盘 + schema 兼容。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from djb_node import facts
from djb_node.config import ClientConfig, feature_flag, flag_spec
from djb_node.upstream import ModelEntry, UpstreamManager
from tests.mock_probe import UpstreamStub

SPEC_PROBE_REPORT_FIELDS = {"upstream_id", "reachable", "latency_ms", "error_class",
                            "model_checks", "checked_at"}
MODEL_CHECK_FIELDS = {"model", "ok", "error_class"}


@pytest.fixture
def stub():
    server = UpstreamStub().start()
    yield server
    server.stop()


@pytest.fixture
def manager(store, credentials):
    return UpstreamManager(store, credentials)


def _add(manager, stub, *, models="probe-model", access="A1"):
    return manager.add(access_type=access, base_url=stub.upstream_base_url,
                       models=models)


def _capacity(manager, store, now=None):
    return manager.announce_capacity(store.profile(), now=now)


# ─────────────────────────────────────────────────────────── 门控：未验证
def test_declared_model_gets_zero_qps(manager, store, stub):
    record = _add(manager, stub)
    entries = _capacity(manager, store)
    assert [item["model"] for item in entries] == ["probe-model"]
    assert entries[0]["max_qps"] == 0.0              # declared = 不进容量
    assert manager.announceable_models() == []
    assert store.upstream(record.id).state == "declared"


def test_verified_fresh_model_is_announced(manager, store, stub, clock):
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    entries = _capacity(manager, store, now=clock())
    assert entries[0]["max_qps"] > 0
    assert store.upstream(record.id).state == "verified"
    assert [entry.id for _, entry in manager.announceable_models(clock())] == [
        "probe-model"]


def test_stale_probe_stops_being_announced(manager, store, stub, clock):
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    later = clock() + timedelta(seconds=facts.PROBE_FRESH_S + 1)
    assert manager.announceable_models(later) == []
    assert _capacity(manager, store, now=later)[0]["max_qps"] == 0.0
    # 边界：正好等于窗口仍然算新鲜
    edge = clock() + timedelta(seconds=facts.PROBE_FRESH_S)
    assert len(manager.announceable_models(edge)) == 1


def test_rejected_upstream_vetoes_capacity(manager, store, stub, clock):
    stub.chat_status = 500                           # L1 可达，但推理失败
    record = _add(manager, stub)
    payload = manager.probe(record.id, now=clock())
    assert payload["reachable"] is True
    assert payload["model_checks"][0]["ok"] is False
    assert store.upstream(record.id).state == "rejected"
    assert manager.announceable_models(clock()) == []
    assert _capacity(manager, store, now=clock())[0]["max_qps"] == 0.0


def test_model_not_listed_is_not_announced(manager, store, stub, clock):
    stub.models = ["another-model"]
    record = _add(manager, stub)
    payload = manager.probe(record.id, now=clock())
    check = payload["model_checks"][0]
    assert check["ok"] is False
    assert check["error_class"] == "model_not_listed"
    assert manager.announceable_models(clock()) == []


def test_disabled_model_not_announced(manager, store, stub, clock):
    stub.models = ["probe-model", "second-model"]
    record = _add(manager, stub, models="probe-model,second-model")
    manager.probe(record.id, now=clock())
    assert len(manager.announceable_models(clock())) == 2
    manager.set_model_enabled(record.id, "second-model", False)
    live = [entry.id for _, entry in manager.announceable_models(clock())]
    assert live == ["probe-model"]


def test_disabled_upstream_not_announced(manager, store, stub, clock):
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    store.update_upstream(record.id, enabled=False)
    assert _capacity(manager, store, now=clock()) == []
    assert manager.announceable_models(clock()) == []


# ─────────────────────────────────────────────────────────── 门控：降级 / 申报
def test_stream_failure_degrades_but_keeps_inference(manager, store, stub, clock):
    stub.stream_mode = "json"                        # 端点不支持 SSE
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    details = {item["model"]: item for item in manager.probe_details(record.id)}
    assert details["probe-model"]["inference_ok"] is True
    assert details["probe-model"]["stream_ok"] is False
    assert _capacity(manager, store, now=clock())[0]["max_qps"] > 0


def test_declare_reset_does_not_erase_verification(manager, store, stub, clock):
    """`djb upstream declare` 会把 state 写回 declared（cli.py:675）；门控以模型证据为准。"""
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    store.update_upstream(record.id, state="declared")
    assert _capacity(manager, store, now=clock())[0]["max_qps"] > 0
    # 但 rejected 仍然一票否决
    store.update_upstream(record.id, state="rejected")
    assert _capacity(manager, store, now=clock())[0]["max_qps"] == 0.0


# ─────────────────────────────────────────────────────────── schema 兼容
def test_probe_payload_stays_within_node_probe_report(manager, store, stub, clock):
    record = _add(manager, stub)
    payload = manager.probe(record.id, now=clock())
    assert set(payload) <= SPEC_PROBE_REPORT_FIELDS
    for item in payload["model_checks"]:
        assert set(item) <= MODEL_CHECK_FIELDS
    # 必需字段齐全（spec required: upstream_id / reachable / checked_at）
    assert {"upstream_id", "reachable", "checked_at"} <= set(payload)
    assert payload["checked_at"].endswith("Z")
    assert isinstance(payload["latency_ms"], int)
    # L2/L3 细节只落本地，不在上行 payload 里
    for local_only in ("inference_ok", "stream_ok", "usage_ok", "token_usage",
                       "probe_version", "last_probe_at"):
        assert local_only not in payload
        assert local_only not in str(payload["model_checks"])


def test_probe_writes_local_fields_back(manager, store, stub, clock):
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    stored = store.upstream(record.id)
    model = stored.models[0]
    assert model["inference_ok"] is True
    assert model["stream_ok"] is True
    assert model["usage_ok"] is True
    assert model["probe_version"] == facts.PROBE_VERSION
    assert model["last_probe_at"] == stored.last_probe_at
    assert stored.state == "verified"
    # 原有字段不被改写
    assert model["id"] == "probe-model"
    assert model["enabled"] is True


def test_model_entry_roundtrip_is_tolerant():
    legacy = ModelEntry.from_dict({"id": "m1", "enabled": True})
    assert legacy.inference_ok is None and legacy.last_probe_at is None
    assert legacy.to_dict() == {"id": "m1", "enabled": True}     # 旧形状不变
    verified = ModelEntry.from_dict({"id": "m1", "enabled": True, "inference_ok": True,
                                     "stream_ok": False, "usage_ok": True,
                                     "probe_version": "c7.1",
                                     "last_probe_at": "2026-10-24T00:00:00Z"})
    payload = verified.to_dict()
    assert payload["inference_ok"] is True
    assert payload["stream_ok"] is False
    assert payload["probe_version"] == "c7.1"
    # 脏值不炸：非 bool 的 inference_ok 视为「未验证」
    assert ModelEntry.from_dict({"id": "m", "inference_ok": "yes"}).verified() is False


def test_future_last_probe_at_counts_as_fresh():
    entry = ModelEntry(id="m", inference_ok=True,
                       last_probe_at="2026-10-24T00:10:00Z")
    base = ModelEntry(id="m", inference_ok=True,
                      last_probe_at="2026-10-24T00:00:00Z")
    from datetime import datetime, timezone
    now = datetime(2026, 10, 23, 23, 55, tzinfo=timezone.utc)     # 早于 last_probe_at
    assert entry.fresh(now) is True
    assert base.fresh(now) is True
    assert ModelEntry(id="m", inference_ok=True).fresh(now) is False


# ─────────────────────────────────────────────────────────── 容量形状 / qps
def test_qps_split_only_between_verified_models(manager, store, stub, clock):
    stub.models = ["probe-model", "second-model"]
    record = _add(manager, stub, models="probe-model,second-model")
    manager.probe(record.id, now=clock())
    entries = {item["model"]: item for item in _capacity(manager, store, now=clock())}
    profile = store.profile()
    expected = max(0.1, round(max(1, int(profile.concurrent)) / 2, 2))
    assert entries["probe-model"]["max_qps"] == expected
    assert entries["second-model"]["max_qps"] == expected


def test_capacity_rows_stay_shape_and_order_stable(manager, store, stub, clock):
    """旧调用方（`djb announce`）依赖清单非空与行序：门控只改数值，不改形状。"""
    stub.models = ["probe-model", "second-model"]
    record = _add(manager, stub, models="probe-model,second-model")
    before = _capacity(manager, store, now=clock())
    assert [item["model"] for item in before] == ["probe-model", "second-model"]
    assert {frozenset(item) for item in before} == {
        frozenset({"kind", "access", "model", "max_qps", "ask_price", "unit",
                   "resource_level"})}
    manager.probe(record.id, now=clock())
    after = _capacity(manager, store, now=clock())
    assert [item["model"] for item in after] == ["probe-model", "second-model"]
    assert all(item["max_qps"] > 0 for item in after)
    assert after[0]["kind"] == facts.TOKEN_RELAY_KIND


def test_catalog_exposes_probe_visibility_additively(manager, store, stub, clock):
    record = _add(manager, stub)
    manager.probe(record.id, now=clock())
    entry = manager.catalog(now=clock())[0]
    assert entry["probe"]["inference_ok"] is True
    assert entry["probe"]["fresh"] is True
    assert entry["probe"]["announceable"] is True
    # 老字段不动
    assert entry["available"] is True
    assert entry["id"] == "probe-model"


# ─────────────────────────────────────────────────────────── 特性开关
def test_feature_flag_shape_and_default_on():
    """C-01b：周期复探开关**默认开**，三字段与 DECISIONS §D-R3-01 一致。"""
    spec = facts.FEATURE_FLAGS[facts.PROBE_FLAG_PERIODIC]
    assert spec["default"] is True
    assert spec["owner"] == "client-seat"
    assert spec["expire_at"] == "2026-12-15T00:00:00+08:00"
    assert spec["rollback_rule"]
    assert "last_probe_at" in spec["rollback_rule"]
    assert flag_spec(facts.PROBE_FLAG_PERIODIC)["owner"] == "client-seat"
    # 干净配置（没有 extra）与「空 mapping」都按登记表默认值 → 开
    assert feature_flag(ClientConfig(), facts.PROBE_FLAG_PERIODIC) is True
    assert feature_flag({}, facts.PROBE_FLAG_PERIODIC) is True
    assert feature_flag(ClientConfig(), "unknown.flag") is False


def test_feature_flag_accepts_config_and_mapping_forms():
    name = facts.PROBE_FLAG_PERIODIC
    assert feature_flag(ClientConfig(extra={name: True}), name) is True
    assert feature_flag({name: True}, name) is True
    assert feature_flag({"feature_flags": {name: True}}, name) is True
    assert feature_flag({"extra": {name: True}}, name) is True
    assert feature_flag(ClientConfig(extra={name: "true"}), name) is True
    assert feature_flag({name: "1"}, name) is True
    assert feature_flag({name: "off"}, name) is False
    assert feature_flag({name: False}, name) is False
