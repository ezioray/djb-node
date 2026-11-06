# SPDX-License-Identifier: Apache-2.0
import platform

import pytest

from djb_node import envcheck
from djb_node.config import ClientConfig
from djb_node.envcheck import (Check, EnvChecker, EnvReport, check_acceleration,
                               check_kernel, check_network, check_runtime,
                               check_system)


def test_check_system_matrix(monkeypatch):
    now = None
    monkeypatch.setattr(envcheck, "_read_os_release",
                        lambda: {"ID": "ubuntu", "VERSION_ID": "20.04",
                                 "PRETTY_NAME": "Ubuntu 20.04"})
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    result = check_system(now)
    assert result.status == "fail"
    assert "22.04" in result.next_step

    monkeypatch.setattr(envcheck, "_read_os_release",
                        lambda: {"ID": "debian", "VERSION_ID": "12",
                                 "PRETTY_NAME": "Debian 12"})
    assert check_system(now).status == "ok"

    monkeypatch.setattr(envcheck, "_read_os_release",
                        lambda: {"ID": "fedora", "VERSION_ID": "41",
                                 "PRETTY_NAME": "Fedora 41"})
    assert check_system(now).status == "ok"

    monkeypatch.setattr(envcheck, "_read_os_release",
                        lambda: {"ID": "arch", "VERSION_ID": "",
                                 "PRETTY_NAME": "Arch"})
    assert check_system(now).status == "warn"

    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    assert check_system(now).status == "fail"


# ───────────────────────────── 内存探测：Windows 上不再恒为 0.0 GB（任务 B4）
def test_windows_mem_total_reads_global_memory_status_ex(monkeypatch):
    """ctypes 结构体接线：kernel32 填 ullTotalPhys → 32.0 GB（不依赖真跑在 Windows 上）。"""
    import ctypes

    class _Kernel:
        def GlobalMemoryStatusEx(self, pointer):       # noqa: N802 - Win32 名字
            pointer._obj.ullTotalPhys = 32 * 1024 ** 3
            return 1

    class _Windll:
        kernel32 = _Kernel()

    monkeypatch.setattr(ctypes, "windll", _Windll(), raising=False)
    assert envcheck._windows_mem_total_gb() == 32.0


def test_windows_mem_total_failure_returns_zero_and_falls_back(monkeypatch):
    """调用失败 / 非 Windows（没有 windll）都不能抛，只能回落。"""
    monkeypatch.setattr(envcheck, "_windows_mem_total_gb", lambda: 0.0)
    monkeypatch.setattr(envcheck.sys, "platform", "win32")
    assert envcheck._mem_total_gb() >= 0.0             # 回落 /proc/meminfo（没有就是 0.0）
    monkeypatch.setattr(envcheck.sys, "platform", "linux")
    monkeypatch.setattr(envcheck, "_windows_mem_total_gb",
                        lambda: pytest.fail("Linux 上不该调 Windows 分支"))
    assert envcheck._mem_total_gb() >= 0.0


def test_windows_mem_total_is_used_when_available(monkeypatch):
    monkeypatch.setattr(envcheck, "_windows_mem_total_gb", lambda: 63.9)
    monkeypatch.setattr(envcheck.sys, "platform", "win32")
    assert envcheck._mem_total_gb() == 63.9


def test_each_check_has_next_step():
    config = ClientConfig()
    checks = [check_system(None), check_kernel(None), check_runtime(None, __import__(
        "djb_node.config", fromlist=["Paths"]).Paths("/tmp")), check_acceleration(None),
        check_network(None, config, 1717)]
    for check in checks:
        assert check.conclusion
        assert check.next_step
        serialized = check.to_dict()
        assert serialized["id"] == check.id


def test_network_placeholder_and_unreachable():
    config = ClientConfig()
    result = check_network(None, config, 1717)
    assert result.status == "warn"
    assert "服务端地址" in result.conclusion
    config.gateway_url = "http://127.0.0.1:1"
    result = check_network(None, config, 1717)
    assert result.status == "warn"
    assert result.evidence["reachable"] is False


def test_detect_report_and_budget(paths, store, monkeypatch):
    monkeypatch.setattr(envcheck, "check_acceleration",
                        lambda now: Check("acceleration", "加速栈", "warn",
                                          "未检测到", "安装驱动后重跑"))
    checker = EnvChecker(paths, ClientConfig(), store=store, budget_s=0)
    report = checker.detect()
    assert len(report.checks) == 6
    assert report.elapsed_ms >= 0
    assert report.budget_exceeded is True
    assert all(check.next_step for check in report.checks)
    assert "下一步" in report.text()
    payload = report.to_dict()
    assert payload["summary"]["ok"] + payload["summary"]["warn"] + payload["summary"][
        "fail"] + payload["summary"]["na"] == 6


def test_authorize_and_rollback(paths, store, monkeypatch):
    monkeypatch.setattr(envcheck, "check_hardware",
                        lambda now: Check("hardware", "硬件", "warn",
                                          "内存 1 GB（偏小）",
                                          "只做 Token 中转并把并发设为 1",
                                          {"cpu_count": 2, "mem_gb": 1.0}))
    checker = EnvChecker(paths, ClientConfig(), store=store, budget_s=10)
    report = checker.detect()
    ids = {action["id"] for action in report.plan}
    assert "concurrent" in ids
    assert "mem_pct" in ids
    result = checker.authorize(report)
    assert result["authorized"] is True
    profile = store.profile()
    assert profile.concurrent == 1
    assert profile.mem_pct == 60
    again = checker.authorize(report)
    assert again["authorized"] is False
    rollback = checker.rollback()
    assert rollback["rolled_back"] is True
    profile = store.profile()
    assert profile.concurrent == 3
    assert profile.mem_pct == 70
    assert checker.rollback()["rolled_back"] is False
    assert checker.state().get("authorized_at") is None
