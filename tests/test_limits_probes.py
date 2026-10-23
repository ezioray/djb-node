# SPDX-License-Identifier: Apache-2.0
"""让路探针 / 资源监控的分支覆盖（全部用临时目录伪装 /sys 与 /proc）。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from djb_node import limits


def test_load_probe(monkeypatch):
    monkeypatch.setattr(limits.os, "getloadavg", lambda: (4.0, 3.0, 2.0))
    monkeypatch.setattr(limits.os, "cpu_count", lambda: 4)
    signal = limits.LoadProbe().read(datetime.now(timezone.utc))
    assert signal.state == "in_use"

    monkeypatch.setattr(limits.os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    assert limits.LoadProbe().read(datetime.now(timezone.utc)).state == "idle"

    def boom():
        raise OSError("no loadavg")

    monkeypatch.setattr(limits.os, "getloadavg", boom)
    assert limits.LoadProbe().read(datetime.now(timezone.utc)).state == "error"


def test_thermal_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "THERMAL_BASE", str(tmp_path))
    assert limits.ThermalProbe().read(datetime.now(timezone.utc)).state == "not_applicable"
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("90000")
    assert limits.ThermalProbe().read(datetime.now(timezone.utc)).state == "in_use"
    (zone / "temp").write_text("40000")
    assert limits.ThermalProbe().read(datetime.now(timezone.utc)).state == "idle"
    (zone / "temp").write_text("not-a-number")
    assert limits.ThermalProbe().read(datetime.now(timezone.utc)).state == "not_applicable"


def test_battery_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "POWER_BASE", str(tmp_path))
    assert limits.BatteryProbe().read(datetime.now(timezone.utc)).state == "not_applicable"
    battery = tmp_path / "BAT0"
    battery.mkdir()
    (battery / "status").write_text("Discharging")
    assert limits.BatteryProbe().read(datetime.now(timezone.utc)).state == "in_use"
    (battery / "status").write_text("Full")
    assert limits.BatteryProbe().read(datetime.now(timezone.utc)).state == "idle"


def test_session_probe(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert limits.SessionProbe().read(datetime.now(timezone.utc)).state == "not_applicable"
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(limits.shutil, "which", lambda name: None)
    assert limits.SessionProbe().read(datetime.now(timezone.utc)).state == "error"

    monkeypatch.setattr(limits.shutil, "which", lambda name: "/usr/bin/xprintidle")
    monkeypatch.setattr(limits.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "120000"})())
    assert limits.SessionProbe().read(datetime.now(timezone.utc)).state == "idle"
    monkeypatch.setattr(limits.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "500"})())
    assert limits.SessionProbe().read(datetime.now(timezone.utc)).state == "in_use"
    monkeypatch.setattr(limits.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "bad"})())
    assert limits.SessionProbe().read(datetime.now(timezone.utc)).state == "error"

    def boom(*args, **kwargs):
        raise OSError("no xprintidle")

    monkeypatch.setattr(limits.subprocess, "run", boom)
    assert limits.SessionProbe().read(datetime.now(timezone.utc)).state == "error"


def test_render_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "PROC_BASE", str(tmp_path / "missing"))
    assert limits.RenderProbe().read(datetime.now(timezone.utc)).state == "error"
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "1234").mkdir()
    (proc / "1234" / "comm").write_text("ffmpeg\n")
    monkeypatch.setattr(limits, "PROC_BASE", str(proc))
    assert limits.RenderProbe().read(datetime.now(timezone.utc)).state == "in_use"
    (proc / "1234" / "comm").write_text("python\n")
    assert limits.RenderProbe().read(datetime.now(timezone.utc)).state == "idle"


def test_resource_monitor_cpu_and_mem(tmp_path, monkeypatch):
    monkeypatch.setattr(limits.os, "getloadavg", lambda: (2.0, 0, 0))
    monkeypatch.setattr(limits.os, "cpu_count", lambda: 4)
    assert limits.ResourceMonitor().cpu_pct() == pytest.approx(50.0)

    def boom():
        raise OSError("no loadavg")

    monkeypatch.setattr(limits.os, "getloadavg", boom)
    assert limits.ResourceMonitor().cpu_pct() is None

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    monkeypatch.setattr(limits, "MEMINFO_PATH", str(meminfo))
    assert limits.ResourceMonitor().mem_pct() == pytest.approx(75.0)
    meminfo.write_text("MemTotal: x\n")
    assert limits.ResourceMonitor().mem_pct() is None
    monkeypatch.setattr(limits, "MEMINFO_PATH", str(tmp_path / "missing"))
    assert limits.ResourceMonitor().mem_pct() is None


def test_resource_monitor_gpu(monkeypatch):
    monkeypatch.setattr(limits.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(limits.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "33\n"})())
    assert limits.ResourceMonitor().gpu_pct() == pytest.approx(33.0)
    monkeypatch.setattr(limits.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    assert limits.ResourceMonitor().gpu_pct() is None
    monkeypatch.setattr(limits.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "abc"})())
    assert limits.ResourceMonitor().gpu_pct() is None
    monkeypatch.setattr(limits.shutil, "which", lambda name: None)
    assert limits.ResourceMonitor().gpu_pct() is None


def test_detector_disabled_and_probe_exception():
    class Boom:
        name = "boom"

        def read(self, now):
            raise RuntimeError("probe exploded")

    detector = limits.YieldDetector([Boom()])
    result = detector.evaluate()
    assert result.in_use is True
    assert result.signals[0].state == "error"
    disabled = limits.YieldDetector([Boom()], enabled=False)
    assert disabled.evaluate().in_use is False
    assert disabled.evaluate().to_dict()["signals"][0]["state"] == "not_applicable"


def test_limiter_remaining_and_zero_usage(store, clock):
    limiter = limits.Limiter(store, detector=limits.YieldDetector([]), now=clock)
    profile = store.profile()
    limiter.record_usage(0, profile=profile, now=clock())
    remaining = limiter.remaining(profile, None, now=clock())
    assert remaining and remaining[0]["remaining"] == profile.daily_tokens
    assert remaining[0]["resets_at"].endswith("Z")


def test_schedule_next_start(store, clock):
    profile = store.profile()
    profile.schedule = limits.parse_schedule_windows("00:00-00:01")
    profile.cpu_pct = 100
    profile.mem_pct = 100
    profile.gpu_pct = 100
    limiter = limits.Limiter(store, detector=limits.YieldDetector([]), now=clock)
    decision = limiter.evaluate(profile=profile, policy=None,
                                now=clock().replace(hour=12))
    assert decision.reason == "schedule_paused"
    assert decision.retry_at
