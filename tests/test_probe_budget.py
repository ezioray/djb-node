# SPDX-License-Identifier: Apache-2.0
"""0.3.8 · 探针预算不再把整条上游判死（RC14 复盘 P0-2）。

现场（2026-11-15 生产，0.3.4）：单轮预算 10s 死值，而这条上游 5 个 enabled 模型
跑一次 L3 实测 2 分 37 秒 ~ 6 分 06 秒 ⇒ 每轮必有模型 ``not_probed`` ⇒
``all()`` 恒 false ⇒ ``verified=0``、从不上报 ⇒ 云端 ``verified_at`` 冻结。

本文件钉住 0.3.8 的三条口径：
1. 预算可配，但**硬上限 = 一个心跳周期**（内联在主循环，超了节点会 stale/offline）；
2. ``not_probed`` 不是失败：上报的 ``model_checks`` 里不出现，只进 ``not_probed[]``；
3. 判定 = 「≥1 个 ok 且没有真实失败」；真实失败（timeout/5xx/…）仍 rejected。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from djb_node import facts
from djb_node.config import ClientConfig
from djb_node.upstream import UpstreamManager
from tests.test_daemon import _add_upstream, _daemon


@pytest.fixture
def mock():
    from tests.mock_m02 import MockServer
    server = MockServer().start()
    yield server
    server.stop()


def _payload(*, ok_models, budget_missed=(), real_failures=()):
    """模拟 ``UpstreamManager.probe()`` 的返回：not_probed 已从 model_checks 剔除。"""
    checks = [{"model": name, "ok": True, "error_class": None} for name in ok_models]
    checks += [{"model": name, "ok": False, "error_class": "timeout"}
               for name in real_failures]
    return {
        "upstream_id": "u1",
        "reachable": True,
        "latency_ms": 12.0,
        "error_class": None,
        "model_checks": checks,
        "models": [item["model"] for item in checks],
        "not_probed": [{"model": name, "reason": "budget"} for name in budget_missed],
        "checked_at": "2026-11-15T00:00:00Z",
    }


def _daemon_with_payload(paths, store, credentials, mock, clock, payload):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                                         extra={facts.PROBE_FLAG_PERIODIC: True}))
    reports: list[dict] = []
    daemon.upstreams.probe = lambda *args, **kwargs: dict(payload)
    daemon._report_probe = lambda record, value, *, reason: reports.append(value)
    return daemon, reports


def test_partial_pass_with_backlog_still_verifies_and_reports_only_probed(
        paths, store, credentials, mock, clock):
    """5 模型：3 个 ok + 2 个 not_probed ⇒ verified，且上报里**没有** not_probed。"""
    payload = _payload(ok_models=["m1", "m2", "m3"], budget_missed=["m4", "m5"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 1
    assert result["failed"] == 0
    assert result["backlog"] == 2
    detail = result["per_upstream"][0]
    assert detail["ok"] is True and detail["passed"] == 3 and detail["total"] == 3
    assert detail["not_probed"] == 2 and detail["real_failures"] == []

    assert len(reports) == 1, "有 ok 模型就必须上报，否则云端 verified_at 不刷新"
    reported = [item["model"] for item in reports[0]["model_checks"]]
    assert reported == ["m1", "m2", "m3"], "not_probed 不能进 model_checks（云端按任一失败判 rejected）"

    # backlog 未清空 ⇒ 5s 后续跑，而不是等 half-fresh（450s）
    assert daemon._next_probe_at <= clock() + timedelta(
        seconds=facts.PROBE_BACKLOG_RETRY_S + 1)


def test_real_failure_still_rejects_and_does_not_report(paths, store, credentials, mock,
                                                        clock):
    """真实失败（timeout）⇒ rejected、不上报（不能拿假结论去续云端 verified_at）。"""
    payload = _payload(ok_models=["m1", "m2"], real_failures=["m3"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 0 and result["failed"] == 1
    assert result["per_upstream"][0]["real_failures"] == ["m3"]
    assert reports == []


def test_all_budget_missed_is_backlog_not_failure(paths, store, credentials, mock, clock):
    """一个都没探到（全 backlog）⇒ 不算 failed、不触发退避，5s 后续跑。"""
    payload = _payload(ok_models=[], budget_missed=["m1", "m2"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 0 and result["failed"] == 0
    assert result["backlog"] == 2 and reports == []
    assert daemon._probe_failures == 0
    assert daemon._next_probe_at <= clock() + timedelta(
        seconds=facts.PROBE_BACKLOG_RETRY_S + 1)


def test_periodic_probe_uses_l2_and_budget_is_capped():
    """周期探针只跑 L2（L3 留人工）；预算硬上限 = 一个心跳周期。"""
    assert facts.PROBE_PERIODIC_LEVEL == "L2"
    assert facts.PROBE_PASS_BUDGET_MAX_S == float(facts.HEARTBEAT_DEFAULT_S)
    assert facts.probe_pass_budget_s(600) == facts.PROBE_PASS_BUDGET_MAX_S
    assert facts.probe_pass_budget_s(5) == 5.0
    assert facts.probe_pass_budget_s() <= facts.PROBE_PASS_BUDGET_MAX_S


def test_probe_excludes_budget_missed_models_from_report(store, credentials, monkeypatch):
    """源头（UpstreamManager.probe）：预算没轮到的模型不进 ``model_checks``。"""
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url="https://upstream.invalid/v1",
                       models=[{"id": name, "enabled": True, "min_price": "0.5"}
                               for name in ("m1", "m2", "m3", "m4", "m5")],
                       credential_ref="upstream:u1")
    credentials.set_upstream_secret("u1", "sk-test-not-a-real-key")

    class _Result:
        reachable = True
        latency_ms = 9.5
        error_class = None
        model_checks = [{"model": name, "ok": True, "error_class": None}
                        for name in ("m1", "m2", "m3", "m4", "m5")]

    class _Outcome:
        inference_ok = True
        stream_ok = True
        usage_present = True
        error_class = None

    monkeypatch.setattr("djb_node.upstream.probe_upstream", lambda *a, **k: _Result())
    monkeypatch.setattr("djb_node.upstream.probe_mod.probe_models",
                        lambda *a, **k: {"m1": _Outcome(), "m2": _Outcome(),
                                         "m3": _Outcome()})

    payload = UpstreamManager(store, credentials).probe("u1")

    assert [item["model"] for item in payload["model_checks"]] == ["m1", "m2", "m3"]
    assert payload["models"] == ["m1", "m2", "m3"]
    reasons = {item["model"]: item["reason"] for item in payload["not_probed"]}
    assert reasons["m4"] == "budget" and reasons["m5"] == "budget"
