# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timedelta, timezone

import pytest

from djb_node import facts
from djb_node.limits import (Decision, Limiter, ResourceMonitor, Signal, YieldDetector,
                             discrepancy_alert, in_schedule, parse_schedule_windows,
                             resource_caps_valid)
from djb_node.store import ProfileRecord
from djb_node.util import iso


class FakeProbe:
    def __init__(self, state: str, name: str = "fake") -> None:
        self.state = state
        self.name = name

    def read(self, now):
        return Signal(self.name, self.state, "test")


class FakeResources:
    def __init__(self, cpu=None, mem=None, gpu=None) -> None:
        self.values = {"cpu_pct": cpu, "mem_pct": mem, "gpu_pct": gpu}

    def snapshot(self):
        return dict(self.values)


def _limiter(store, *, probes=(), resources=None, clock=None):
    detector = YieldDetector(list(probes))
    return Limiter(store, detector=detector,
                   resources=resources or FakeResources(cpu=1, mem=1, gpu=None),
                   now=clock or (lambda: datetime(2026, 10, 24, 0, 0,
                                                 tzinfo=timezone.utc)))


def test_accept_when_all_clear(store):
    limiter = _limiter(store, probes=[FakeProbe("idle")])
    decision = limiter.evaluate(profile=ProfileRecord(), policy=None)
    assert decision.accept is True
    assert decision.reason == "ok"


def test_l1_yield_wins_over_quota_and_resources(store):
    profile = ProfileRecord(daily_tokens=10, cpu_pct=5)
    policy = None
    limiter = _limiter(store, probes=[FakeProbe("in_use", "load")],
                       resources=FakeResources(cpu=99, mem=99))
    limiter.record_usage(10, profile=profile, policy=policy)
    decision = limiter.evaluate(profile=profile, policy=policy)
    assert decision.accept is False
    assert decision.reason == "yield_paused"
    assert decision.pause_reason == "yield_paused"
    assert decision.detail["source"] == "load"
    assert decision.next_step


def test_detection_failure_is_conservative(store):
    limiter = _limiter(store, probes=[FakeProbe("error", "session")])
    decision = limiter.evaluate(profile=ProfileRecord(), policy=None)
    assert decision.reason == "yield_paused"
    assert decision.detail["source"] == "session"


def test_yield_disabled_by_profile(store):
    profile = ProfileRecord(yield_enabled=False)
    limiter = _limiter(store, probes=[FakeProbe("in_use")])
    decision = limiter.evaluate(profile=profile, policy=None)
    assert decision.accept is True


def test_l2_resource_cap(store):
    profile = ProfileRecord(cpu_pct=40, gpu_pct=70, mem_pct=70)
    limiter = _limiter(store, probes=[FakeProbe("idle")],
                       resources=FakeResources(cpu=90, mem=10))
    decision = limiter.evaluate(profile=profile, policy=None)
    assert decision.reason == "resource_limited"
    assert decision.detail["over"]["cpu_pct"]["cap"] == 40
    assert decision.next_step


def test_l3_policy_quota_soft_stop_no_carryover(store, clock):
    profile = ProfileRecord()
    policy = type("P", (), {"quotas": [{"name": "daily", "window_seconds": 86400,
                                        "value": 100}]})()
    limiter = _limiter(store, probes=[FakeProbe("idle")], clock=clock)
    limiter.record_usage(60, profile=profile, policy=policy, now=clock())
    assert limiter.evaluate(profile=profile, policy=policy, now=clock()).accept
    limiter.record_usage(40, profile=profile, policy=policy, now=clock())
    decision = limiter.evaluate(profile=profile, policy=policy, now=clock())
    assert decision.reason == "quota_exhausted"
    assert decision.retry_at
    assert decision.detail["used"] == 100
    # 下一个窗口：配额不结转 -> 重新可用
    tomorrow = clock.current + timedelta(days=1)
    assert limiter.evaluate(profile=profile, policy=policy, now=tomorrow).accept


def test_policy_quota_without_value_is_not_enforced(store):
    profile = ProfileRecord()
    policy = type("P", (), {"quotas": [{"name": "daily", "window_seconds": 86400,
                                        "value": None}]})()
    limiter = _limiter(store, probes=[FakeProbe("idle")])
    assert limiter.evaluate(profile=profile, policy=policy).accept


def test_profile_daily_quota(store, clock):
    profile = ProfileRecord(daily_tokens=100)
    limiter = _limiter(store, probes=[FakeProbe("idle")], clock=clock)
    limiter.record_usage(100, profile=profile, policy=None, now=clock())
    decision = limiter.evaluate(profile=profile, policy=None, now=clock())
    assert decision.reason == "quota_exhausted"


def test_weekly_and_monthly_windows(store, clock):
    profile = ProfileRecord(daily_tokens=10_000, weekly_tokens=100, monthly_tokens=1000)
    limiter = _limiter(store, probes=[FakeProbe("idle")], clock=clock)
    limiter.record_usage(100, profile=profile, policy=None, now=clock())
    decision = limiter.evaluate(profile=profile, policy=None, now=clock())
    assert decision.reason == "quota_exhausted"
    remaining = limiter.remaining(profile, None, now=clock())
    names = {item["name"] for item in remaining}
    assert any("weekly" in name for name in names)
    assert any("monthly" in name for name in names)


def test_schedule_window(store, clock):
    profile = ProfileRecord(schedule=[])
    limiter = _limiter(store, probes=[FakeProbe("idle")], clock=clock)
    assert limiter.evaluate(profile=profile, policy=None, now=clock()).accept
    profile.schedule = parse_schedule_windows("00:00-00:01")
    decision = limiter.evaluate(profile=profile, policy=None,
                                now=clock().replace(hour=12))
    assert decision.reason == "schedule_paused"
    assert decision.retry_at


def test_concurrency_limited_stays_online(store):
    profile = ProfileRecord(concurrent=2)
    limiter = _limiter(store, probes=[FakeProbe("idle")])
    decision = limiter.evaluate(profile=profile, policy=None, inflight=2)
    assert decision.accept is False
    assert decision.reason == "concurrency_limited"
    assert decision.pause_reason is None


def test_unbound_and_conservative_gates(store):
    limiter = _limiter(store, probes=[FakeProbe("idle")])
    assert limiter.evaluate(profile=ProfileRecord(), policy=None, unbound=True).reason == "unbound"
    conservative = limiter.evaluate(profile=ProfileRecord(), policy=None, conservative=True)
    assert conservative.reason == "conservative"
    assert conservative.next_step


def test_parse_schedule_windows_and_in_schedule():
    windows = parse_schedule_windows("09:00-12:00,13:00-18:00")
    assert len(windows) == 2
    with pytest.raises(ValueError):
        parse_schedule_windows("9-12")
    with pytest.raises(ValueError):
        parse_schedule_windows("25:00-26:00")
    assert parse_schedule_windows("") == []
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    assert in_schedule([], now)[0] is True
    assert in_schedule([{"days": list(range(7)), "start": "00:00",
                         "end": "23:59"}], now)[0] is True


def test_cross_day_window():
    local_tz = datetime.now().astimezone().tzinfo
    now = datetime(2026, 10, 24, 3, 0, tzinfo=local_tz)
    window = {"days": [now.weekday()], "start": "22:00", "end": "05:00"}
    assert in_schedule([window], now)[0] is True
    outside = now.replace(hour=12)
    assert in_schedule([window], outside)[0] is False


def test_discrepancy_alert_and_caps_validation():
    assert discrepancy_alert(100, None) is False
    assert discrepancy_alert(100, 100) is False
    assert discrepancy_alert(102, 100) is True
    assert discrepancy_alert(100, 0) is True
    resource_caps_valid(40, 70, 70)
    for args in ((1, 70, 70), (101, 70, 70), (40, 5, 70)):
        with pytest.raises(ValueError):
            resource_caps_valid(*args)


def test_resource_monitor_tolerates_missing_gpu(monkeypatch):
    monitor = ResourceMonitor()
    monkeypatch.setattr("shutil.which", lambda name: None)
    snapshot = monitor.snapshot()
    assert set(snapshot) == {"cpu_pct", "mem_pct", "gpu_pct"}
    assert snapshot["gpu_pct"] is None
