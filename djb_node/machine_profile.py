# SPDX-License-Identifier: Apache-2.0
"""机器评估 / 算力画像（任务 T4 / R1-5，验收矩阵 M3）。

产出**事实**，不产出结论：CPU 型号与核数、内存、磁盘可用、GPU 型号/显存/驱动/利用率、
OS 与架构、可选 tokens/s 基准。**缺失字段一律 None 或空列表，绝不编造**；每段附
`probe` 说明「从哪来、为什么没有」（`ok` / `source` / `error`），让「没有数据」可解释。

数据来源（按平台，全部只读、不装驱动、不提权）：

=========  ============================================================
CPU 型号    Linux ``/proc/cpuinfo`` · Windows WMI · macOS ``sysctl -n machdep.cpu.brand_string``
核数        ``os.cpu_count()`` + 物理核（``/proc/cpuinfo`` · ``hw.physicalcpu``）
频率        ``/proc/cpuinfo`` 的 cpu MHz 最大值 · macOS ``hw.cpufrequency`` · Windows WMI
内存        ``/proc/meminfo`` · ``sysctl hw.memsize`` · Windows WMI（KB → MB）
磁盘        ``shutil.disk_usage(home)``（三端同一口径）
GPU         ``nvidia-smi --query-gpu``（NVIDIA，三端）→ AMD ``rocm-smi`` →
            Windows WMI ``Win32_VideoController`` → macOS ``system_profiler``
加载        ``os.getloadavg()``（Windows 无此语义，留 None）
基准        可选、显式开启：``benchmark_tokens_per_s``（合成参考口径，不是模型吞吐实测）
=========  ============================================================

口径边界（写进 docstring 免得被当成能力承诺）：

* ``benchmark`` 是**合成参考**：固定语料 + 纯 Python 计数，只用来横向比较同一台机器
  的 CPU 侧处理能力；它**不是**模型 tokens/s，字段名与方法名都带 ``synthetic`` 前缀，
  不给「我们能跑多少 token/s」这种结论。
* GPU 利用率是**采样瞬间**的值（nvidia-smi 查询那一刻），不做平滑、不做估计。
* 全程 ≤ `COLLECT_BUDGET_S`；单项超时按「该项不可用」记，不拖垮整体，也不重试到超预算。
"""
from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .util import iso, utcnow

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_S = 2.0
COLLECT_BUDGET_S = 4.0
BENCHMARK_DEFAULT_S = 1.0
BENCHMARK_MIN_S = 0.2
BENCHMARK_MAX_S = 5.0
_CPUINFO = "/proc/cpuinfo"
_MEMINFO = "/proc/meminfo"


# ─────────────────────────────────────────────────────────── 执行器（可注入，测试不真跑命令）
Runner = Callable[..., Any]


def _run(args: list[str], *, timeout: float = DEFAULT_TIMEOUT_S) -> tuple[int, str, str]:
    """跑一条只读命令 -> (returncode, stdout, stderr)。失败归一成 (-1, "", 原因)。"""
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                   check=False)
    except FileNotFoundError:
        return -1, "", "not_found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as exc:                               # pragma: no cover - 极端环境
        return -1, "", type(exc).__name__
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _first_line(text: str) -> str | None:
    for line in (text or "").splitlines():
        value = line.strip()
        if value:
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if not value or value.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]"):
                return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:                                 # NaN
        return None
    return int(round(number))


def _float_or_none(value: Any) -> float | None:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if not value or value.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]"):
                return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return round(number, 3)


@dataclass
class Probe:
    """一段探测的来源与结论（**不含任何原始长文本**）。"""

    ok: bool = False
    source: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "source": self.source, "error": self.error}


@dataclass
class MachineProfile:
    sections: dict[str, Any] = field(default_factory=dict)
    probes: dict[str, Probe] = field(default_factory=dict)
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "collected_at": self.collected_at,
                "os": self.sections.get("os") or {},
                "cpu": self.sections.get("cpu") or {},
                "memory": self.sections.get("memory") or {},
                "disk": self.sections.get("disk") or {},
                "load": self.sections.get("load") or {},
                "gpu": self.sections.get("gpu") or {"available": False, "devices": []},
                "benchmark": self.sections.get("benchmark") or {"enabled": False},
                "probe": {name: probe.to_dict() for name, probe in self.probes.items()}}

    def summary(self) -> dict[str, Any]:
        """状态行 / 诊断包用的紧凑视图（字段名与完整画像一致）。"""
        data = self.to_dict()
        cpu = data.get("cpu") or {}
        memory = data.get("memory") or {}
        gpu = data.get("gpu") or {}
        devices = gpu.get("devices") or []
        return {
            "cpu_model": cpu.get("model"),
            "cpu_cores_logical": cpu.get("logical_cores"),
            "cpu_cores_physical": cpu.get("physical_cores"),
            "cpu_freq_mhz": cpu.get("freq_mhz_max"),
            "memory_total_mb": memory.get("total_mb"),
            "memory_available_mb": memory.get("available_mb"),
            "memory_total_bytes": memory.get("total_bytes"),
            "disk_free_gb": (data.get("disk") or {}).get("free_gb"),
            "disk_free_bytes": (data.get("disk") or {}).get("free_bytes"),
            "gpu": [{"model": item.get("model"), "memory_total_mb": item.get("memory_total_mb"),
                     "vram_bytes": item.get("memory_total_bytes"),
                     "utilization_pct": item.get("utilization_pct"),
                     "vendor": item.get("vendor")} for item in devices],
            "gpu_available": bool(gpu.get("available")),
            "os": data.get("os"),
            "benchmark": data.get("benchmark"),
        }

    def text(self) -> str:
        data = self.to_dict()
        cpu = data.get("cpu") or {}
        memory = data.get("memory") or {}
        disk = data.get("disk") or {}
        gpu = data.get("gpu") or {}
        os_info = data.get("os") or {}
        lines: list[str] = []
        os_name = str(os_info.get("distro") or os_info.get("system") or "未知")
        version = str(os_info.get("distro_version") or "").strip()
        # PRETTY_NAME 里通常已含版本（"Debian GNU/Linux 12 (bookworm)"），别拼成 "… 12 (bookworm) 12"
        show_version = bool(version) and version not in os_name
        line = "OS：" + os_name + (" " + version if show_version else "")
        if os_info.get("arch"):
            line += " · " + str(os_info.get("arch")) + " 架构"
        if os_info.get("kernel"):
            line += " · 内核 " + str(os_info.get("kernel"))
        lines.append(line)
        cpu_line = "CPU：" + str(cpu.get("model") or "未知")
        if cpu.get("logical_cores"):
            cpu_line += " · " + str(cpu.get("logical_cores")) + " 线程"
        if cpu.get("physical_cores"):
            cpu_line += " / " + str(cpu.get("physical_cores")) + " 核"
        if cpu.get("freq_mhz_max"):
            cpu_line += " · " + str(cpu.get("freq_mhz_max")) + " MHz"
        lines.append(cpu_line)
        if memory.get("total_mb"):
            lines.append("内存：" + str(memory.get("total_mb")) + " MB"
                         + ("（可用 " + str(memory.get("available_mb")) + " MB）"
                            if memory.get("available_mb") else ""))
        else:
            lines.append("内存：未取到（" + str((data.get("probe") or {}).get("memory", {})
                                              .get("error")) + "）")
        if disk.get("total_gb"):
            lines.append("磁盘：" + str(disk.get("free_gb")) + " GB 可用 / "
                         + str(disk.get("total_gb")) + " GB")
        devices = gpu.get("devices") or []
        if devices:
            for item in devices:
                detail = "GPU：" + str(item.get("model") or "未知")
                if item.get("memory_total_mb"):
                    detail += " · 显存 " + str(item.get("memory_total_mb")) + " MB"
                if item.get("driver_version"):
                    detail += " · 驱动 " + str(item.get("driver_version"))
                if item.get("utilization_pct") is not None:
                    detail += " · 利用率 " + str(item.get("utilization_pct")) + "%"
                lines.append(detail)
        else:
            lines.append("GPU：未检测到（" + str(gpu.get("source") or "无可用探测")
                         + "）")
        benchmark = data.get("benchmark") or {}
        if benchmark.get("enabled"):
            lines.append("基准：" + str(benchmark.get("tokens_per_s")) + " tok/s（"
                         + str(benchmark.get("method")) + "，合成参考口径）")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────── 各段探测
def _os_section() -> tuple[dict[str, Any], Probe]:
    system = _platform.system() or ""
    probe = Probe(ok=bool(system), source="platform")
    info: dict[str, Any] = {"system": system.lower() or None, "arch": _arch(),
                            "kernel": _platform.release() or None,
                            "version": _platform.version() or None,
                            "distro": None, "distro_version": None}
    if system.lower() == "linux":
        release = _read_os_release()
        info["distro"] = release.get("PRETTY_NAME") or release.get("NAME") or None
        info["distro_version"] = release.get("VERSION_ID") or None
        info["distro_id"] = release.get("ID") or None
        probe.source = "platform+/etc/os-release"
    elif system.lower() == "darwin":
        info["distro"] = "macOS"
        info["distro_version"] = _platform.mac_ver()[0] or None
    elif system.lower() == "windows":
        info["distro"] = "Windows"
        info["distro_version"] = _platform.version() or None
    if not system:
        probe.error = "platform_unavailable"
    return info, probe


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "=" not in line:
                    continue
                key, value = line.rstrip("\n").split("=", 1)
                values[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _arch() -> str | None:
    try:
        from .platform import normalize_arch
        return normalize_arch(_platform.machine() or "")
    except Exception:                                    # pragma: no cover - 极端环境
        return (_platform.machine() or "").lower() or None


def _cpuinfo() -> dict[str, str]:
    """``/proc/cpuinfo`` 第一块的 key/value（小写键）。"""
    values: dict[str, str] = {}
    try:
        with open(_CPUINFO, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    break
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                values.setdefault(key.strip().lower(), value.strip())
    except OSError:
        pass
    return values


def _linux_cores() -> tuple[int | None, int | None, int | None]:
    """(物理核, 逻辑核, 最高 MHz) —— 只读 /proc/cpuinfo，取不到就 None。"""
    physical = logical = None
    freq: float | None = None
    try:
        with open(_CPUINFO, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None, os.cpu_count(), None
    pairs: set[tuple[str, str]] = set()
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip().lower()] = value.strip()
        if "processor" in fields:
            physical_id = fields.get("physical id")
            core_id = fields.get("core id")
            if physical_id is not None and core_id is not None:
                pairs.add((physical_id, core_id))
        mhz = _float_or_none(fields.get("cpu mhz"))
        if mhz is not None and (freq is None or mhz > freq):
            freq = mhz
    if pairs:
        physical = len(pairs)
    elif "cpu cores" in _cpuinfo():
        physical = _int_or_none(_cpuinfo().get("cpu cores"))
    logical = os.cpu_count()
    if freq is not None:
        return physical, logical, int(round(freq))
    return physical, logical, None


def _cpu_section(runner: Runner) -> tuple[dict[str, Any], Probe]:
    system = (_platform.system() or "").lower()
    info: dict[str, Any] = {"model": None, "vendor": None, "physical_cores": None,
                            "logical_cores": os.cpu_count(), "freq_mhz_max": None,
                            "arch": _arch()}
    probe = Probe(source="platform")
    if system == "linux":
        probe.source = "/proc/cpuinfo"
        fields = _cpuinfo()
        info["model"] = fields.get("model name") or fields.get("processor") or None
        info["vendor"] = fields.get("vendor_id") or None
        physical, logical, freq = _linux_cores()
        info["physical_cores"] = physical
        info["logical_cores"] = logical or info["logical_cores"]
        info["freq_mhz_max"] = freq
        probe.ok = bool(info["model"])
        if not probe.ok:
            probe.error = "model_missing"
    elif system == "darwin":
        probe.source = "sysctl"
        code, out, err = runner(["sysctl", "-n", "machdep.cpu.brand_string"])
        if code == 0:
            info["model"] = _first_line(out)
        code, out, _ = runner(["sysctl", "-n", "hw.physicalcpu"])
        info["physical_cores"] = _int_or_none(_first_line(out) if code == 0 else None)
        code, out, _ = runner(["sysctl", "-n", "hw.cpufrequency"])
        hz = _int_or_none(_first_line(out) if code == 0 else None)
        info["freq_mhz_max"] = int(round(hz / 1_000_000)) if hz else None
        probe.ok = bool(info["model"])
        if not probe.ok:
            probe.error = "model_missing"
    elif system == "windows":
        probe.source = "wmi"
        code, out, _ = runner([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Processor | Select-Object -First 1 "
            "Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed "
            "| ConvertTo-Json -Compress"])
        if code == 0 and _first_line(out):
            try:
                payload = json.loads(out)
                if isinstance(payload, dict):
                    info["model"] = payload.get("Name") or None
                    info["physical_cores"] = _int_or_none(payload.get("NumberOfCores"))
                    info["logical_cores"] = (_int_or_none(
                        payload.get("NumberOfLogicalProcessors")) or info["logical_cores"])
                    info["freq_mhz_max"] = _int_or_none(payload.get("MaxClockSpeed"))
            except ValueError:
                probe.error = "parse_failed"
        elif os.environ.get("PROCESSOR_IDENTIFIER"):
            info["model"] = os.environ.get("PROCESSOR_IDENTIFIER")
            probe.source = "env:PROCESSOR_IDENTIFIER"
        probe.ok = bool(info["model"])
        if not probe.ok and not probe.error:
            probe.error = "model_missing"
    else:
        info["model"] = _platform.processor() or None
        probe.ok = bool(info["model"])
        if not probe.ok:
            probe.error = "unsupported_platform"
    if not info["model"] and _platform.processor():
        info["model"] = _platform.processor()
    return info, probe


def _memory_section(runner: Runner) -> tuple[dict[str, Any], Probe]:
    system = (_platform.system() or "").lower()
    info: dict[str, Any] = {"total_mb": None, "available_mb": None,
                            "total_bytes": None, "available_bytes": None}
    probe = Probe()
    if system == "linux":
        probe.source = "/proc/meminfo"
        try:
            with open(_MEMINFO, "r", encoding="utf-8", errors="replace") as handle:
                fields = {}
                for line in handle:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        fields[key.strip()] = value.strip()
        except OSError:
            fields = {}
        total_kb = _int_or_none(str(fields.get("MemTotal", "")).split()[0]
                                if fields.get("MemTotal") else None)
        avail_kb = _int_or_none(str(fields.get("MemAvailable", "")).split()[0]
                               if fields.get("MemAvailable") else None)
        info["total_mb"] = int(round(total_kb / 1024)) if total_kb else None
        info["available_mb"] = int(round(avail_kb / 1024)) if avail_kb else None
        info["total_bytes"] = total_kb * 1024 if total_kb else None
        info["available_bytes"] = avail_kb * 1024 if avail_kb else None
        probe.ok = bool(info["total_mb"])
        if not probe.ok:
            probe.error = "parse_failed"
    elif system == "darwin":
        probe.source = "sysctl"
        code, out, _ = runner(["sysctl", "-n", "hw.memsize"])
        total = _int_or_none(_first_line(out) if code == 0 else None)
        info["total_mb"] = int(round(total / (1024 * 1024))) if total else None
        info["total_bytes"] = total or None
        probe.ok = bool(info["total_mb"])
        if not probe.ok:
            probe.error = "parse_failed"
    elif system == "windows":
        probe.source = "wmi"
        code, out, _ = runner([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_OperatingSystem | Select-Object "
            "TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json -Compress"])
        if code == 0 and _first_line(out):
            try:
                payload = json.loads(out)
                if isinstance(payload, dict):
                    total_kb = _int_or_none(payload.get("TotalVisibleMemorySize"))
                    free_kb = _int_or_none(payload.get("FreePhysicalMemory"))
                    info["total_mb"] = int(round(total_kb / 1024)) if total_kb else None
                    info["available_mb"] = int(round(free_kb / 1024)) if free_kb else None
                    info["total_bytes"] = total_kb * 1024 if total_kb else None
                    info["available_bytes"] = free_kb * 1024 if free_kb else None
            except ValueError:
                probe.error = "parse_failed"
        probe.ok = bool(info["total_mb"])
        if not probe.ok and not probe.error:
            probe.error = "parse_failed"
    else:
        probe.source = "unsupported"
        probe.error = "unsupported_platform"
    return info, probe


def _disk_section(home: str | None) -> tuple[dict[str, Any], Probe]:
    target = home or os.path.expanduser("~")
    probe = Probe(source="shutil.disk_usage")
    info: dict[str, Any] = {"path": target, "total_gb": None, "free_gb": None,
                            "used_gb": None, "total_bytes": None, "free_bytes": None,
                            "used_bytes": None, "rotational": None}
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        probe.error = type(exc).__name__
        return info, probe
    gb = 1024 ** 3
    info["total_gb"] = round(usage.total / gb, 2)
    info["free_gb"] = round(usage.free / gb, 2)
    info["used_gb"] = round(usage.used / gb, 2)
    info["total_bytes"] = int(usage.total)
    info["free_bytes"] = int(usage.free)
    info["used_bytes"] = int(usage.used)
    info["rotational"] = _rotational(target)
    probe.ok = True
    return info, probe


def _rotational(mount: str) -> bool | None:
    """Linux：home 所在块设备是否机械盘（``/sys/block/<dev>/queue/rotational``）。

    Windows / macOS 上标准的「物理盘是否旋转」查询要么需要提权（WMI MSFT_PhysicalDisk）
    要么要 IOKit，**本客户端不做**——返回 None 并让调用方标 ``unavailable_reason``。
    """
    if (_platform.system() or "").lower() != "linux":
        return None
    try:
        device = os.stat(mount).st_dev
        major, minor = os.major(device), os.minor(device)
        for entry in os.listdir("/sys/dev/block"):
            if entry.split(":") != [str(major), str(minor)]:
                continue
            # 分区的 sysfs 目录没有 queue/，向上找到承载它的整盘
            node = os.path.realpath("/sys/dev/block/" + entry)
            while node not in ("/", "") and not os.path.exists(node + "/queue/rotational"):
                node = os.path.dirname(node)
            with open(node + "/queue/rotational", "r", encoding="ascii") as handle:
                return handle.read().strip() == "1"
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _load_section() -> tuple[dict[str, Any], Probe]:
    probe = Probe(source="os.getloadavg")
    info: dict[str, Any] = {"avg_1m": None, "avg_5m": None, "avg_15m": None}
    if (_platform.system() or "").lower() == "windows":
        # Windows 没有 load average 语义（不是「暂时取不到」），如实标注
        probe.error = "windows_has_no_load_average"
        return info, probe
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):                    # Windows 无此语义
        probe.error = "not_supported"
        return info, probe
    info.update({"avg_1m": round(one, 2), "avg_5m": round(five, 2),
                 "avg_15m": round(fifteen, 2)})
    probe.ok = True
    return info, probe


# ── GPU：nvidia-smi -> rocm-smi -> WMI -> system_profiler
NVIDIA_QUERY = "name,memory.total,memory.used,utilization.gpu,driver_version"


def _gpu_section(runner: Runner) -> tuple[dict[str, Any], Probe]:
    info: dict[str, Any] = {"available": False, "source": None, "devices": []}
    for probe_fn in (_gpu_nvidia, _gpu_rocm, _gpu_windows_wmi, _gpu_macos):
        devices, probe = probe_fn(runner)
        if devices:
            info.update({"available": True, "source": probe.source, "devices": devices})
            return info, probe
        if probe.error not in ("not_found", "not_supported", "no_devices"):
            # 命令存在但失败 / 解析失败：如实记下来（不掩盖），继续下一个来源
            info["last_error"] = probe.error
    return info, Probe(ok=False, source="nvidia-smi/rocm-smi/wmi/system_profiler",
                       error=info.get("last_error") or "no_devices")


def _gpu_nvidia(runner: Runner) -> tuple[list[dict[str, Any]], Probe]:
    if shutil.which("nvidia-smi") is None:
        return [], Probe(ok=False, source="nvidia-smi", error="not_found")
    code, out, err = runner(["nvidia-smi", "--query-gpu=" + NVIDIA_QUERY,
                             "--format=csv,noheader,nounits"], timeout=3.0)
    if code != 0:
        return [], Probe(ok=False, source="nvidia-smi",
                         error="exit_" + str(code) + ("|" + _first_line(err) if err else ""))
    devices: list[dict[str, Any]] = []
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        parts = [item.strip() for item in text.split(",")]
        if len(parts) < 5:
            return [], Probe(ok=False, source="nvidia-smi", error="parse_failed")
        total_mb = _int_or_none(parts[1])
        used_mb = _int_or_none(parts[2])
        devices.append({"vendor": "nvidia", "model": parts[0] or None,
                        "memory_total_mb": total_mb,
                        "memory_total_bytes": total_mb * 1024 * 1024 if total_mb else None,
                        "memory_used_mb": used_mb,
                        "memory_used_bytes": used_mb * 1024 * 1024 if used_mb else None,
                        "utilization_pct": _int_or_none(parts[3]),
                        "driver_version": parts[4] or None,
                        "source": "nvidia-smi"})
    if not devices:
        return [], Probe(ok=False, source="nvidia-smi", error="no_devices")
    return devices, Probe(ok=True, source="nvidia-smi")


def _gpu_rocm(runner: Runner) -> tuple[list[dict[str, Any]], Probe]:
    if shutil.which("rocm-smi") is None:
        return [], Probe(ok=False, source="rocm-smi", error="not_found")
    code, out, _ = runner(["rocm-smi", "--showproductname", "--showmeminfo", "vram",
                           "--showuse", "--json"], timeout=3.0)
    if code != 0:
        return [], Probe(ok=False, source="rocm-smi", error="exit_" + str(code))
    try:
        payload = json.loads(out)
    except ValueError:
        return [], Probe(ok=False, source="rocm-smi", error="parse_failed")
    devices: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for card, values in payload.items():
            if not isinstance(values, dict):
                continue
            if not (card.lower().startswith("card") or "vram" in str(values).lower()):
                continue
            total = _vram_bytes(values)
            devices.append({
                "vendor": "amd",
                "model": (values.get("Card series") or values.get("Card SKU")
                          or values.get("Card Type") or card),
                "memory_total_mb": int(round(total / (1024 * 1024))) if total else None,
                "memory_total_bytes": total or None,
                "memory_used_mb": None, "memory_used_bytes": None,
                "utilization_pct": _int_or_none(values.get("GPU use (%)")
                                                or values.get("GPU use")),
                "driver_version": values.get("Driver version") or None,
                "source": "rocm-smi"})
    return (devices, Probe(ok=True, source="rocm-smi")) if devices \
        else ([], Probe(ok=False, source="rocm-smi", error="no_devices"))


def _vram_bytes(values: dict[str, Any]) -> int | None:
    for key, value in values.items():
        if "vram total" not in str(key).lower():
            continue
        number = _int_or_none(value)
        if number is None:
            continue
        return number * 1024 * 1024 if number < 1024 * 1024 else number
    return None


def _gpu_windows_wmi(runner: Runner) -> tuple[list[dict[str, Any]], Probe]:
    if (_platform.system() or "").lower() != "windows":
        return [], Probe(ok=False, source="wmi", error="not_supported")
    code, out, _ = runner([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_VideoController | Select-Object "
        "Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress"], timeout=3.0)
    if code != 0 or not _first_line(out):
        return [], Probe(ok=False, source="wmi", error="exit_" + str(code))
    try:
        payload = json.loads(out)
    except ValueError:
        return [], Probe(ok=False, source="wmi", error="parse_failed")
    items = payload if isinstance(payload, list) else [payload]
    devices: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or item.get("name")
        if not name:
            continue
        adapter = _int_or_none(item.get("AdapterRAM") or item.get("adapterRAM"))
        devices.append({"vendor": _gpu_vendor(str(name)),
                        "model": str(name),
                        "memory_total_mb": int(round(adapter / (1024 * 1024)))
                        if adapter else None,
                        "memory_total_bytes": adapter or None,
                        "memory_used_mb": None, "memory_used_bytes": None,
                        "utilization_pct": None,
                        "driver_version": item.get("DriverVersion") or None,
                        "source": "wmi"})
    return (devices, Probe(ok=True, source="wmi")) if devices \
        else ([], Probe(ok=False, source="wmi", error="no_devices"))


def _gpu_vendor(name: str) -> str:
    lowered = name.lower()
    for vendor, marker in (("nvidia", "nvidia"), ("amd", "amd"), ("amd", "radeon"),
                           ("intel", "intel"), ("apple", "apple")):
        if marker in lowered:
            return vendor
    return "unknown"


def _gpu_macos(runner: Runner) -> tuple[list[dict[str, Any]], Probe]:
    if (_platform.system() or "").lower() != "darwin":
        return [], Probe(ok=False, source="system_profiler", error="not_supported")
    code, out, _ = runner(["system_profiler", "SPDisplaysDataType", "-json"], timeout=3.0)
    if code != 0:
        return [], Probe(ok=False, source="system_profiler", error="exit_" + str(code))
    try:
        payload = json.loads(out)
    except ValueError:
        return [], Probe(ok=False, source="system_profiler", error="parse_failed")
    devices: list[dict[str, Any]] = []
    for item in (payload.get("SPDisplaysDataType") or []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("sppci_model") or item.get("_name")
        if not name:
            continue
        vram = item.get("spdisplays_vram") or item.get("spdisplays_vram_shared")
        devices.append({"vendor": _gpu_vendor(str(name)), "model": str(name),
                        "memory_total_mb": _parse_macos_vram(vram),
                        "memory_total_bytes": (_parse_macos_vram(vram) or 0) * 1024 * 1024
                        if _parse_macos_vram(vram) else None,
                        "memory_used_mb": None, "memory_used_bytes": None,
                        "utilization_pct": None,
                        "driver_version": item.get("spdisplays_metal") or None,
                        "source": "system_profiler"})
    return (devices, Probe(ok=True, source="system_profiler")) if devices \
        else ([], Probe(ok=False, source="system_profiler", error="no_devices"))


def _parse_macos_vram(value: Any) -> int | None:
    """``"4 GB"`` / ``"512 MB"`` -> MB。"""
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace("VRAM", "").strip()
    parts = text.split()
    if len(parts) < 2:
        return None
    number = _int_or_none(parts[0])
    if number is None:
        return None
    unit = parts[1]
    if unit.startswith("GB"):
        return number * 1024
    if unit.startswith("MB"):
        return number
    if unit.startswith("KB"):
        return int(round(number / 1024))
    return None


# ─────────────────────────────────────────────────────────── 可选基准（合成口径）
_CORPUS = ("djb-node machine profile synthetic benchmark: the quick brown fox jumps "
           "over the lazy dog 0123456789 ") * 64
_HIDDEN = 256
_WEIGHTS = [((index * 37) % 101) / 101.0 for index in range(_HIDDEN)]


def _activation(token: str) -> list[float]:
    seed = 2166136261
    for char in token:
        seed = ((seed ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return [((seed >> (index % 24)) & 0xFF) / 255.0 for index in range(_HIDDEN)]


def benchmark_tokens_per_s(*, seconds: float = BENCHMARK_DEFAULT_S,
                           minimum_s: float = BENCHMARK_MIN_S,
                           maximum_s: float = BENCHMARK_MAX_S) -> dict[str, Any]:
    """合成 CPU 侧吞吐参考（**不是模型 tokens/s**，方法名写死 ``synthetic``）。

    每个 token 做「256 维激活 × 固定权重」的点积（≈ 一次极小的打分前向），
    统计每秒能处理多少 token。用途只有一个：**同一台机器横向/纵向比较 CPU 处理能力**；
    数量级是「纯 Python 实现」，与真实模型推理吞吐无可比性，故字段/文案都带「合成参考」。
    """
    budget = max(minimum_s, min(maximum_s, float(seconds)))
    tokens = _CORPUS.split(" ")
    weights = _WEIGHTS
    start = time.monotonic()
    processed = 0
    rounds = 0
    while True:
        for token in tokens:
            activation = _activation(token)
            total = 0.0
            for index in range(_HIDDEN):
                total += activation[index] * weights[index]
            processed += 1
        rounds += 1
        elapsed = time.monotonic() - start
        if elapsed >= budget:
            break
    elapsed = max(time.monotonic() - start, 1e-6)
    return {"enabled": True, "method": "synthetic_pure_python_dot256_v1",
            "model": "synthetic-cpu-reference",
            "tokens_per_s": round(processed / elapsed, 1),
            "duration_s": round(elapsed, 3), "rounds": rounds, "tokens": processed,
            "note": "纯 Python 合成参考口径，不是模型推理吞吐"}


# ─────────────────────────────────────────────────────────── 汇总
def collect(*, home: str | None = None, benchmark: bool = False,
            benchmark_seconds: float = BENCHMARK_DEFAULT_S,
            runner: Runner | None = None,
            now: Callable[[], Any] = utcnow,
            timeout_s: float = DEFAULT_TIMEOUT_S,
            budget_s: float = COLLECT_BUDGET_S) -> MachineProfile:
    """采集机器画像（只读、无提权、总预算 ``budget_s``）。"""
    run = runner or (lambda args, timeout=None: _run(args, timeout=timeout or timeout_s))
    started = time.monotonic()
    profile = MachineProfile(collected_at=iso(now()))

    os_info, os_probe = _os_section()
    profile.sections["os"] = os_info
    profile.probes["os"] = os_probe

    cpu_info, cpu_probe = _cpu_section(run)
    profile.sections["cpu"] = cpu_info
    profile.probes["cpu"] = cpu_probe

    memory_info, memory_probe = _memory_section(run)
    profile.sections["memory"] = memory_info
    profile.probes["memory"] = memory_probe

    disk_info, disk_probe = _disk_section(home)
    profile.sections["disk"] = disk_info
    profile.probes["disk"] = disk_probe

    load_info, load_probe = _load_section()
    profile.sections["load"] = load_info
    profile.probes["load"] = load_probe

    if time.monotonic() - started < budget_s:
        gpu_info, gpu_probe = _gpu_section(run)
    else:
        gpu_info, gpu_probe = {"available": False, "devices": []}, Probe(
            ok=False, source="nvidia-smi", error="budget_exceeded")
    profile.sections["gpu"] = gpu_info
    profile.probes["gpu"] = gpu_probe

    if benchmark and time.monotonic() - started < budget_s:
        profile.sections["benchmark"] = benchmark_tokens_per_s(seconds=benchmark_seconds)
        profile.probes["benchmark"] = Probe(ok=True, source="synthetic_pure_python_dot256_v1")
    else:
        profile.sections["benchmark"] = {"enabled": False}
        profile.probes["benchmark"] = Probe(
            ok=False, source="synthetic_pure_python_dot256_v1",
            error=None if not benchmark else "budget_exceeded")
    return profile


def collect_dict(**kwargs: Any) -> dict[str, Any]:
    """`collect(...).to_dict()` 的便捷入口（daemon / CLI 共用）。"""
    return collect(**kwargs).to_dict()


def summary_of(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """已有画像 dict -> 紧凑摘要（不重新采集；用于 status / 诊断包）。"""
    if not isinstance(profile, dict) or not profile:
        return None
    built = MachineProfile(collected_at=str(profile.get("collected_at") or ""))
    for key in ("os", "cpu", "memory", "disk", "load", "gpu", "benchmark"):
        if isinstance(profile.get(key), dict):
            built.sections[key] = profile[key]
    return built.summary()


def text_of(profile: dict[str, Any] | None) -> str:
    if not isinstance(profile, dict) or not profile:
        return "机器画像：未采集"
    built = MachineProfile(collected_at=str(profile.get("collected_at") or ""))
    for key in ("os", "cpu", "memory", "disk", "load", "gpu", "benchmark"):
        if isinstance(profile.get(key), dict):
            built.sections[key] = profile[key]
    return built.text()


# ─────────────────────────────────────────────────────────── djb.machine-eval/1（qa 钉的契约）
MACHINE_EVAL_SCHEMA = "djb.machine-eval/1"
_EVAL_PLATFORM = {"linux": "linux", "darwin": "macos", "windows": "windows"}


def eval_platform() -> str:
    """`facts.PLATFORM` 口径（linux/darwin/windows）-> machine-eval 的 platform 取值。"""
    system = (_platform.system() or "").lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    try:
        from . import facts
        return {"darwin": "macos"}.get(str(facts.PLATFORM), str(facts.PLATFORM) or "linux")
    except Exception:                                    # pragma: no cover - 极端环境
        return "linux"


def to_machine_eval(profile: dict[str, Any], *, platform_name: str | None = None,
                    benchmark: dict[str, Any] | None = None) -> dict[str, Any]:
    """把内部画像转成 qa-e2e 钉死的 ``djb.machine-eval/1``。

    契约要点（`ops/e2e/qa/modules/m03_machine.py` 的判分逻辑，逐条对齐）：

    * 顶层 9 个必填键一个不少；
    * ``memory.total_bytes`` / ``gpus[].vram_bytes`` 用**字节**（不是 MB）；
    * 拿不到的字段写 ``null`` + ``unavailable_reason``，**绝不写 0 充数**
      （qa 明确「0 不算有」）；
    * ``benchmark.tokens_per_second`` 必须是正数，否则要 ``unavailable_reason``。
    """
    data = profile or {}
    cpu = data.get("cpu") or {}
    memory = data.get("memory") or {}
    disk = data.get("disk") or {}
    load = data.get("load") or {}
    gpu = data.get("gpu") or {}
    probe = data.get("probe") or {}
    bench = benchmark or data.get("benchmark") or {}

    gpus: list[dict[str, Any]] = []
    for item in gpu.get("devices") or []:
        vram = item.get("memory_total_bytes")
        entry: dict[str, Any] = {
            "vendor": item.get("vendor") or "unknown",
            "model": item.get("model") or None,
            "vram_bytes": vram if vram else None,
            "driver": item.get("driver_version") or None,
        }
        if not vram:
            entry["unavailable_reason"] = ("driver_did_not_report_vram"
                                           if item.get("model") else "gpu_partial_probe")
        if item.get("utilization_pct") is not None:
            entry["utilization_pct"] = item.get("utilization_pct")
        if item.get("memory_used_bytes"):
            entry["vram_used_bytes"] = item.get("memory_used_bytes")
        if item.get("source"):
            entry["source"] = item.get("source")
        gpus.append(entry)

    load_doc: dict[str, Any] = {"load1": load.get("avg_1m"), "load5": load.get("avg_5m"),
                                "load15": load.get("avg_15m")}
    if load_doc["load1"] is None:
        load_doc["unavailable_reason"] = (
            "windows_has_no_load_average" if eval_platform() == "windows"
            else (probe.get("load") or {}).get("error") or "probe_unavailable")

    benchmark_doc: dict[str, Any] = {
        "tokens_per_second": bench.get("tokens_per_s") if bench.get("enabled") else None,
        "method": bench.get("method") or "none",
        "model": bench.get("model") or bench.get("method") or "n/a",
        "at": data.get("collected_at") or None,
    }
    for extra_key, extra_value in bench.items():
        # 附加信息（tokens / duration_s / fallback_from / note）原样带给读方，
        # 但绝不覆盖契约字段。
        if extra_key in ("enabled", "tokens_per_s") or extra_key in benchmark_doc:
            continue
        benchmark_doc.setdefault(extra_key, extra_value)
    if benchmark_doc["tokens_per_second"] is None:
        benchmark_doc["unavailable_reason"] = (
            bench.get("unavailable_reason")
            or (probe.get("benchmark") or {}).get("error") or "benchmark_not_run")

    cpu_doc: dict[str, Any] = {"model": cpu.get("model") or None,
                              "cores": cpu.get("physical_cores") or cpu.get("logical_cores"),
                              "threads": cpu.get("logical_cores"),
                              "vendor": cpu.get("vendor"),
                              "freq_mhz_max": cpu.get("freq_mhz_max")}
    if not cpu_doc["model"]:
        cpu_doc["unavailable_reason"] = (probe.get("cpu") or {}).get("error") or "probe_failed"

    disks_doc = []
    if disk.get("total_bytes"):
        entry = {"mount": disk.get("path"), "total_bytes": disk.get("total_bytes"),
                 "free_bytes": disk.get("free_bytes"),
                 "rotational": disk.get("rotational")}
        if disk.get("rotational") is None:
            entry["unavailable_reason"] = "rotational_not_queryable_without_privilege"
        disks_doc.append(entry)

    doc: dict[str, Any] = {
        "schema": MACHINE_EVAL_SCHEMA,
        "platform": platform_name or eval_platform(),
        "cpu": cpu_doc,
        "memory": {"total_bytes": memory.get("total_bytes"),
                   "available_bytes": memory.get("available_bytes")},
        "gpus": gpus,
        "disks": disks_doc,
        "load": load_doc,
        "benchmark": benchmark_doc,
        "collected_at": data.get("collected_at") or iso(utcnow()),
        # 加性：来源与错误说明（qa 不看，排障要看）
        "probe": probe,
        "profile_schema": data.get("schema", SCHEMA_VERSION),
    }
    if not doc["memory"]["total_bytes"]:
        doc["memory"]["unavailable_reason"] = (probe.get("memory") or {}).get("error") \
            or "probe_failed"
    if not disks_doc:
        doc["disks_unavailable_reason"] = (probe.get("disk") or {}).get("error") or "probe_failed"
    return doc


def build_machine_eval(*, home: str | None = None, benchmark: bool = True,
                       benchmark_mode: str = "synthetic",
                       ollama_url: str | None = None,
                       ollama_model: str | None = None,
                       platform_name: str | None = None,
                       runner: Runner | None = None,
                       budget_s: float = COLLECT_BUDGET_S) -> dict[str, Any]:
    """采集 + 基准 + 转契约（E2E 交付与 CLI 共用这一条路径）。"""
    seconds = 1.0 if benchmark else 0.0
    profile = collect(home=home, benchmark=False, runner=runner, budget_s=budget_s)
    data = profile.to_dict()
    bench: dict[str, Any] = {"enabled": False, "unavailable_reason": "benchmark_not_requested"}
    if benchmark:
        if benchmark_mode == "ollama":
            bench = benchmark_ollama(ollama_url or DEFAULT_OLLAMA_URL,
                                     model=ollama_model or DEFAULT_OLLAMA_MODEL)
            if not bench.get("enabled"):
                # 真机基准不可用：退回合成参考，但把原因一起带走（不冒充真机）
                synthetic = benchmark_tokens_per_s(seconds=seconds)
                synthetic["fallback_from"] = "ollama:" + str(bench.get("unavailable_reason"))
                bench = synthetic
        else:
            bench = benchmark_tokens_per_s(seconds=seconds)
    doc = to_machine_eval(data, platform_name=platform_name, benchmark=bench)
    doc["benchmark"]["model"] = bench.get("model") or bench.get("method") or "n/a"
    doc.setdefault("probe", {})["benchmark"] = {
        "ok": bool(bench.get("enabled")), "source": bench.get("method"),
        "error": bench.get("unavailable_reason")}
    return doc


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"


def benchmark_ollama(base_url: str = DEFAULT_OLLAMA_URL, *, model: str = DEFAULT_OLLAMA_MODEL,
                     prompt: str = "用一句话解释什么是 HTTP 长轮询。",
                     timeout: float = 60.0) -> dict[str, Any]:
    """**真机** tokens/s（本机 Ollama，纯 stdlib，无凭据）。

    只打 ``127.0.0.1``（本机回环），不上网、不带任何密钥。用 Ollama 返回的
    ``eval_count`` / ``eval_duration``（纳秒）算生成吞吐。失败 → enabled=False + 原因。
    """
    import urllib.error
    import urllib.request
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False,
                          "options": {"num_predict": 64}}).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 本机回环
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"enabled": False, "method": "ollama_api_generate",
                "model": model, "unavailable_reason": type(exc).__name__}
    eval_count = _int_or_none(body.get("eval_count"))
    eval_duration = _int_or_none(body.get("eval_duration"))
    if not eval_count or not eval_duration:
        return {"enabled": False, "method": "ollama_api_generate", "model": model,
                "unavailable_reason": "ollama_missing_eval_fields"}
    return {"enabled": True, "method": "ollama_api_generate", "model": model,
            "tokens_per_s": round(eval_count / (eval_duration / 1e9), 1),
            "tokens": eval_count, "duration_s": round(eval_duration / 1e9, 3),
            "wall_s": round(time.monotonic() - started, 3),
            "note": "本机 Ollama 实测生成吞吐（eval_count/eval_duration）"}


def write_machine_eval(path: str, doc: dict[str, Any], *, mode: int = 0o600) -> str:
    """落盘（默认 0600）：目录 0700、文件 0600、原子写。"""
    from .util import atomic_write, ensure_dir
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory, 0o700)
    atomic_write(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", mode=mode)
    return path


def default_eval_path(platform_name: str | None = None) -> str:
    """`~/.djb/secrets/e2e/machine-eval/<platform>.json`（qa 只读校验的默认位置）。"""
    name = platform_name or eval_platform()
    return os.path.join(os.path.expanduser("~"), ".djb", "secrets", "e2e",
                        "machine-eval", name + ".json")


def machine_eval_text(doc: dict[str, Any] | None) -> str:
    """按 `djb.machine-eval/1` 渲染人读文本（CLI / 诊断包共用；缺失字段如实说「未取到」）。"""
    if not isinstance(doc, dict) or not doc.get("schema"):
        return "机器评估：未采集"
    cpu = doc.get("cpu") or {}
    memory = doc.get("memory") or {}
    disks = doc.get("disks") or []
    gpus = doc.get("gpus") or []
    load = doc.get("load") or {}
    bench = doc.get("benchmark") or {}
    lines: list[str] = ["平台：" + str(doc.get("platform") or "未知")
                        + " · 采集于 " + str(doc.get("collected_at") or "未知")]
    cpu_line = "CPU：" + str(cpu.get("model") or "未取到")
    if cpu.get("threads"):
        cpu_line += " · " + str(cpu.get("threads")) + " 线程"
    if cpu.get("cores"):
        cpu_line += " / " + str(cpu.get("cores")) + " 核"
    if cpu.get("freq_mhz_max"):
        cpu_line += " · " + str(cpu.get("freq_mhz_max")) + " MHz"
    lines.append(cpu_line)
    if memory.get("total_bytes"):
        line = ("内存：" + str(round(memory["total_bytes"] / 1024 ** 3, 1)) + " GB")
        if memory.get("available_bytes"):
            line += "（可用 " + str(round(memory["available_bytes"] / 1024 ** 3, 1)) + " GB）"
        lines.append(line)
    else:
        lines.append("内存：未取到（" + str(memory.get("unavailable_reason")) + "）")
    for disk in disks:
        lines.append("磁盘：" + str(disk.get("mount")) + " · "
                     + str(round((disk.get("free_bytes") or 0) / 1024 ** 3, 1)) + " GB 可用 / "
                     + str(round((disk.get("total_bytes") or 0) / 1024 ** 3, 1)) + " GB"
                     + ("（机械盘）" if disk.get("rotational") is True else ""))
    if not disks:
        lines.append("磁盘：未取到（" + str(doc.get("disks_unavailable_reason")) + "）")
    if gpus:
        for item in gpus:
            line = "GPU：" + str(item.get("model") or "未知")
            if item.get("vram_bytes"):
                line += " · 显存 " + str(round(item["vram_bytes"] / 1024 ** 3, 1)) + " GB"
            if item.get("driver"):
                line += " · 驱动 " + str(item.get("driver"))
            if item.get("utilization_pct") is not None:
                line += " · 利用率 " + str(item.get("utilization_pct")) + "%"
            if not item.get("vram_bytes") and item.get("unavailable_reason"):
                line += " · 显存未取到（" + str(item.get("unavailable_reason")) + "）"
            lines.append(line)
    else:
        lines.append("GPU：无（gpus=[]，非缺字段）")
    if load.get("load1") is not None:
        lines.append("负载：" + str(load.get("load1")) + " / " + str(load.get("load5"))
                     + " / " + str(load.get("load15")))
    else:
        lines.append("负载：未取到（" + str(load.get("unavailable_reason")) + "）")
    if bench.get("tokens_per_second"):
        lines.append("基准：" + str(bench.get("tokens_per_second")) + " tok/s · 方法 "
                     + str(bench.get("method")) + " · 模型 " + str(bench.get("model")))
    else:
        lines.append("基准：未跑（" + str(bench.get("unavailable_reason")) + "）")
    return "\n".join(lines)
