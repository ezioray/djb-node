# SPDX-License-Identifier: Apache-2.0
"""机器评估（M3 / R1-5）：采集、无 GPU、命令失败、解析垃圾、`djb.machine-eval/1` 合规。

三类必须覆盖的失败姿态（任务单要求）：

1. **没有 GPU**：`gpus=[]`，不得编造设备、不得写 0 显存；
2. **命令失败 / 不存在**：`probe.error` 记原因，字段留 None，整体不崩；
3. **输出解析异常**（nvidia-smi 少列、rocm-smi 非 JSON、WMI 空、sysctl 缺键）：不抛异常、
   不产生假数据。

另外用 **qa-e2e 自己的校验函数**（`ops/e2e/qa/modules/m03_machine.py::_check_doc`）跑一遍
我们产出的文档——契约以他们为准，避免各写一套判断。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from djb_node import machine_profile as mp

# ─────────────────────────────────────────────────────────── 替身 runner
class FakeRunner:
    """按「命令行首词 + 子串」匹配回放 (code, stdout, stderr)。"""

    def __init__(self, table: dict[str, tuple[int, str, str]] | None = None,
                 default: tuple[int, str, str] = (-1, "", "not_found")) -> None:
        self.table = table or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=None):                 # noqa: ANN001
        self.calls.append(list(args))
        joined = " ".join(args)
        for marker, result in self.table.items():
            if marker in joined:
                return result
        return self.default


def _which(monkeypatch, available: set[str] | None = None):
    """模拟「命令是否在 PATH 上」（真机可能装了也可能没装，测试要可控）。"""
    names = available or set()
    monkeypatch.setattr(mp.shutil, "which", lambda name: ("/usr/bin/" + name)
                        if name in names else None)


def _collect(monkeypatch, runner, *, system=None, home="/tmp", benchmark=False,
             available=("nvidia-smi",)):
    if system is not None:
        monkeypatch.setattr(mp._platform, "system", lambda: system)
    _which(monkeypatch, set(available))
    return mp.collect(home=home, benchmark=benchmark, runner=runner)


# ─────────────────────────────────────────────────────────── 基本结构
def test_collect_has_all_sections(monkeypatch):
    runner = FakeRunner({
        "nvidia-smi": (0, "NVIDIA GeForce RTX 4070 Laptop GPU, 8188, 1234, 37, 550.54\n", ""),
    }, default=(-1, "", "not_found"))
    profile = _collect(monkeypatch, runner, system="Linux", benchmark=True)
    data = profile.to_dict()
    for key in ("os", "cpu", "memory", "disk", "load", "gpu", "benchmark", "probe"):
        assert key in data
    assert data["cpu"]["logical_cores"] == os.cpu_count()
    assert data["memory"]["total_bytes"]
    assert data["disk"]["free_bytes"]
    # 探针如实：每段都有来源
    assert set(data["probe"]) >= {"os", "cpu", "memory", "disk", "gpu", "benchmark"}


def test_no_gpu_reports_empty_list_not_fake_device(monkeypatch):
    runner = FakeRunner(default=(-1, "", "not_found"))
    profile = _collect(monkeypatch, runner, system="Linux", benchmark=True, available=())
    gpu = profile.to_dict()["gpu"]
    assert gpu["available"] is False
    assert gpu["devices"] == []
    doc = mp.to_machine_eval(profile.to_dict(),
                             benchmark=profile.to_dict()["benchmark"])
    assert doc["gpus"] == []          # 空数组，不是 [{model: null}]
    assert doc["schema"] == mp.MACHINE_EVAL_SCHEMA


def test_nvidia_smi_failure_is_recorded_not_raised(monkeypatch):
    runner = FakeRunner({"nvidia-smi": (9, "", "NVIDIA-SMI has failed")})
    profile = _collect(monkeypatch, runner, system="Linux")
    data = profile.to_dict()
    assert data["gpu"]["devices"] == []
    assert "exit_9" in str(data["gpu"].get("last_error") or data["probe"]["gpu"]["error"])


def test_nvidia_smi_short_output_is_parse_error(monkeypatch):
    """少列（只有名字）-> parse_failed，不编造显存 / 利用率。"""
    runner = FakeRunner({"nvidia-smi": (0, "RTX 4070\n", "")})
    profile = _collect(monkeypatch, runner, system="Linux")
    data = profile.to_dict()
    assert data["gpu"]["devices"] == []
    assert data["gpu"].get("last_error") == "parse_failed"


def test_nvidia_smi_na_values_become_none(monkeypatch):
    """驱动对不支持字段回 ``[N/A]``：必须是 None，不能变成 0。"""
    runner = FakeRunner({"nvidia-smi": (0, "Tesla T4, 15360, [N/A], [N/A], 535.104\n", "")})
    profile = _collect(monkeypatch, runner, system="Linux")
    device = profile.to_dict()["gpu"]["devices"][0]
    assert device["memory_total_mb"] == 15360
    assert device["memory_used_bytes"] is None
    assert device["utilization_pct"] is None
    doc = mp.to_machine_eval(profile.to_dict())
    assert doc["gpus"][0]["vram_bytes"] == 15360 * 1024 * 1024


def test_rocm_non_json_is_tolerated(monkeypatch):
    runner = FakeRunner({"rocm-smi": (0, "not json at all", "")})
    profile = _collect(monkeypatch, runner, system="Linux", available=("rocm-smi",))
    assert profile.to_dict()["gpu"]["devices"] == []


def test_rocm_json_maps_vram_bytes(monkeypatch):
    payload = json.dumps({"card0": {"Card series": "Radeon RX 7900 XTX",
                                    "VRAM Total Memory (B)": "25753026560",
                                    "GPU use (%)": "12"}})
    runner = FakeRunner({"rocm-smi": (0, payload, "")})
    profile = _collect(monkeypatch, runner, system="Linux", available=("rocm-smi",))
    device = profile.to_dict()["gpu"]["devices"][0]
    assert device["vendor"] == "amd"
    assert device["memory_total_bytes"] == 25753026560
    assert device["utilization_pct"] == 12


def test_windows_wmi_json(monkeypatch):
    payload = json.dumps([{"Name": "NVIDIA GeForce RTX 4070 Laptop GPU",
                           "AdapterRAM": 8589934592, "DriverVersion": "31.0.15.5222"}])
    runner = FakeRunner({"Win32_VideoController": (0, payload, ""),
                         "Win32_Processor": (0, json.dumps(
                             {"Name": "Intel Core i9-13900H", "NumberOfCores": 14,
                              "NumberOfLogicalProcessors": 20, "MaxClockSpeed": 2600}), ""),
                         "Win32_OperatingSystem": (0, json.dumps(
                             {"TotalVisibleMemorySize": 33554432,
                              "FreePhysicalMemory": 16777216}), "")})
    profile = _collect(monkeypatch, runner, system="Windows", available=())
    data = profile.to_dict()
    assert data["cpu"]["model"] == "Intel Core i9-13900H"
    assert data["cpu"]["physical_cores"] == 14
    assert data["memory"]["total_bytes"] == 33554432 * 1024
    device = data["gpu"]["devices"][0]
    assert device["memory_total_bytes"] == 8589934592
    # Windows 无 load average -> None + 原因（不是 0.0）
    doc = mp.to_machine_eval(data)
    assert doc["load"]["load1"] is None
    assert doc["load"]["unavailable_reason"] == "windows_has_no_load_average"


def test_windows_wmi_garbage_does_not_crash(monkeypatch):
    runner = FakeRunner({"Win32_VideoController": (0, "{oops", ""),
                         "Win32_Processor": (0, "", ""),
                         "Win32_OperatingSystem": (0, "[]", "")})
    profile = _collect(monkeypatch, runner, system="Windows", available=())
    data = profile.to_dict()
    assert data["gpu"]["devices"] == []
    assert data["memory"]["total_bytes"] is None
    assert data["probe"]["memory"]["error"] in ("parse_failed", "exit_0")


def test_macos_system_profiler(monkeypatch):
    payload = json.dumps({"SPDisplaysDataType": [
        {"sppci_model": "Apple M3 Pro", "spdisplays_vram": "18 GB",
         "spdisplays_metal": "Metal 3"}]})
    runner = FakeRunner({"system_profiler": (0, payload, ""),
                         "machdep.cpu.brand_string": (0, "Apple M3 Pro\n", ""),
                         "hw.physicalcpu": (0, "11\n", ""),
                         "hw.memsize": (0, "38654705664\n", ""),
                         "hw.cpufrequency": (-1, "", "unknown oid")})
    profile = _collect(monkeypatch, runner, system="Darwin", available=())
    data = profile.to_dict()
    assert data["cpu"]["model"] == "Apple M3 Pro"
    assert data["cpu"]["physical_cores"] == 11
    assert data["memory"]["total_bytes"] == 38654705664
    device = data["gpu"]["devices"][0]
    assert device["vendor"] == "apple"
    assert device["memory_total_bytes"] == 18 * 1024 * 1024 * 1024
    # sysctl 缺键 -> 频率 None（Apple Silicon 本来就不提供）
    assert data["cpu"]["freq_mhz_max"] is None


def test_macos_missing_sysctl_keeps_none(monkeypatch):
    runner = FakeRunner(default=(-1, "", "not_found"))
    profile = _collect(monkeypatch, runner, system="Darwin", available=())
    data = profile.to_dict()
    assert data["cpu"]["model"] in (None, "")
    assert data["memory"]["total_bytes"] is None
    assert data["gpu"]["devices"] == []


def test_benchmark_is_positive_and_labelled_synthetic():
    result = mp.benchmark_tokens_per_s(seconds=0.25)
    assert result["enabled"] is True
    assert result["tokens_per_s"] > 0
    assert "synthetic" in result["method"]
    assert "不是模型" in result["note"]


def test_ollama_benchmark_unreachable_degrades(monkeypatch):
    """本机没 Ollama：不打网络、不抛异常，给 unavailable_reason。"""
    result = mp.benchmark_ollama("http://127.0.0.1:1", model="qwen2.5:7b", timeout=0.5)
    assert result["enabled"] is False
    assert result["unavailable_reason"]


def test_build_machine_eval_falls_back_to_synthetic_when_ollama_down(monkeypatch):
    doc = mp.build_machine_eval(home="/tmp", benchmark=True, benchmark_mode="ollama",
                                ollama_url="http://127.0.0.1:1")
    bench = doc["benchmark"]
    assert bench["tokens_per_second"] > 0
    assert bench["model"] == "synthetic-cpu-reference"
    assert str(bench.get("fallback_from") or "").startswith("ollama:")


# ─────────────────────────────────────────────────────────── qa 契约（用他们的校验器）
def _qa_check_doc():
    """加载 qa-e2e 的 M3 校验函数；不可用返回 None。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _ in range(6):
        qa_dir = os.path.join(root, "ops", "e2e", "qa")
        if os.path.isdir(os.path.join(qa_dir, "modules")):
            if qa_dir not in sys.path:
                sys.path.insert(0, qa_dir)
            try:
                from modules.m03_machine import _check_doc        # type: ignore
            except Exception:                                    # pragma: no cover
                return None
            return _check_doc
        root = os.path.dirname(root)
    return None


def test_machine_eval_matches_qa_validator(tmp_path):
    doc = mp.build_machine_eval(home="/tmp", benchmark=True)
    path = tmp_path / "linux.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    check = _qa_check_doc()
    if check is None:
        pytest.skip("qa-e2e 校验函数不可导入（跨仓/未就位）")
    problems = check(path)
    assert problems == [], problems


def test_machine_eval_matches_qa_validator_without_gpu_or_benchmark(tmp_path):
    """无 GPU + 基准没跑：仍要能过 qa 校验（靠 unavailable_reason，而不是 0）。"""
    doc = mp.build_machine_eval(home="/tmp", benchmark=False)
    doc["gpus"] = []
    path = tmp_path / "windows.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    check = _qa_check_doc()
    if check is None:
        pytest.skip("qa-e2e 校验函数不可导入（跨仓/未就位）")
    assert check(path) == []


def test_machine_eval_zero_values_are_not_used_as_present():
    """反向验证 qa 口径：把拿不到的字段写成 0 会被判 FAIL —— 我们的适配器不许这么干。"""
    check = _qa_check_doc()
    if check is None:
        pytest.skip("qa-e2e 校验函数不可导入（跨仓/未就位）")
    import tempfile
    doc = mp.build_machine_eval(home="/tmp", benchmark=False)
    doc["memory"]["total_bytes"] = 0
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as handle:
        json.dump(doc, handle)
        bad = handle.name
    try:
        problems = check(__import__("pathlib").Path(bad))
        assert any("memory.total_bytes" in item for item in problems)
    finally:
        os.unlink(bad)


def test_write_machine_eval_is_0600(tmp_path):
    path = str(tmp_path / "sub" / "linux.json")
    doc = mp.build_machine_eval(home="/tmp", benchmark=False)
    mp.write_machine_eval(path, doc)
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (os.stat(os.path.dirname(path)).st_mode & 0o777) == 0o700
    assert json.loads(open(path, encoding="utf-8").read())["schema"] == "djb.machine-eval/1"
    assert mp.default_eval_path("linux").endswith("/.djb/secrets/e2e/machine-eval/linux.json")


def test_eval_platform_mapping(monkeypatch):
    monkeypatch.setattr(mp._platform, "system", lambda: "Darwin")
    assert mp.eval_platform() == "macos"
    monkeypatch.setattr(mp._platform, "system", lambda: "Windows")
    assert mp.eval_platform() == "windows"
    monkeypatch.setattr(mp._platform, "system", lambda: "Linux")
    assert mp.eval_platform() == "linux"


# ─────────────────────────────────────────────────────────── Windows 服务会话坑
def test_resolve_tool_survives_missing_which(monkeypatch, tmp_path):
    """S4U/服务会话 PATH 里没有 System32 时，which=None 也要能用绝对路径起来。"""
    tool = tmp_path / "nvidia-smi.exe"
    tool.write_text("", encoding="utf-8")
    monkeypatch.setattr(mp.shutil, "which", lambda name: None)
    argv = mp._resolve_tool("nvidia-smi", str(tool))
    assert argv == [str(tool)]
    # 绝对路径也不存在时，退回原名（Windows CreateProcess 仍可能自己找到）
    assert mp._resolve_tool("nvidia-smi", str(tmp_path / "nope.exe")) == ["nvidia-smi"]


def test_decode_output_handles_utf16_and_gbk():
    assert mp._decode_output("中文".encode("utf-16")) == "中文"      # 带 BOM
    assert mp._decode_output("中文".encode("gbk")) == "中文"
    assert mp._decode_output(b"plain ascii") == "plain ascii"
    assert mp._decode_output(b"") == ""


def test_gpu_found_even_if_which_fails(monkeypatch):
    """RTX 4070 场景：which 说没有，nvidia-smi 其实能跑 -> 必须采到真显存。"""
    monkeypatch.setattr(mp.shutil, "which", lambda name: None)
    runner = FakeRunner({"nvidia-smi": (0, "NVIDIA GeForce RTX 4070 Laptop GPU, 8188, 1234, 3, 596.49\n", "")})
    devices, probe = mp._gpu_nvidia(runner)
    assert probe.ok and len(devices) == 1
    assert devices[0]["model"] == "NVIDIA GeForce RTX 4070 Laptop GPU"
    assert devices[0]["memory_total_bytes"] == 8188 * 1024 * 1024


def test_disk_section_falls_back_when_home_missing(tmp_path):
    """DJB_HOME 还没建出来时，磁盘不能报 FileNotFoundError（改用用户目录/根）。"""
    info, probe = mp._disk_section(str(tmp_path / "not-yet-created" / "home"))
    assert probe.ok is True
    assert info["total_bytes"] and info["free_bytes"]


def test_client_version_matches_package_version():
    """M10 版本如实：`djb --version`、心跳/User-Agent、安装记录必须是同一个版本号。"""
    from djb_node import __version__
    from djb_node import facts

    assert facts.CLIENT_VERSION == __version__
    assert __version__ in facts.USER_AGENT
