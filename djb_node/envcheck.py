# SPDX-License-Identifier: Apache-2.0
"""环境检测：硬件 / 系统 / 运行时 / 加速栈 / 网络 -> 「结论 + 下一步」（M15 §1、§6.1、INV-09）。

四条硬规则：

* **每条结论必须带下一步**（INV-09）；禁止把原始检测数据直接甩给用户。
* 检测总预算 ≤10 s（§8.2）；单项超时按「检测失败 = 更保守」记录结论 + 下一步。
* **一次授权**：补全动作先出计划，用户 djb env --authorize 后一次性应用；
  djb env --rollback 原样回滚（记录改动前后的值）。
* 不装驱动 / 内核模块、不静默提权：缺加速栈只给指引（§1 不做清单）。

S1 的补全动作只有本地安全项（降并发、收紧资源上限、打开让路），不动系统。
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import facts
from .config import ClientConfig, GATEWAY_PLACEHOLDER, Paths
from .limits import ResourceMonitor
from .store import ProfileRecord, Store
from .util import file_mode, iso, utcnow

PROBE_TIMEOUT_S = 2.0


@dataclass
class Check:
    id: str
    title: str
    status: str                 # ok / warn / fail / na
    conclusion: str
    next_step: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status,
                "conclusion": self.conclusion, "next_step": self.next_step,
                "evidence": dict(self.evidence)}


@dataclass
class EnvReport:
    checks: list[Check]
    detected_at: str
    elapsed_ms: int
    budget_exceeded: bool = False
    plan: list[dict[str, Any]] = field(default_factory=list)

    def blocking(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    def warn(self) -> list[Check]:
        return [check for check in self.checks if check.status == "warn"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected_at": self.detected_at,
            "elapsed_ms": self.elapsed_ms,
            "budget_exceeded": self.budget_exceeded,
            "checks": [check.to_dict() for check in self.checks],
            "plan": list(self.plan),
            "summary": {"ok": sum(1 for c in self.checks if c.status == "ok"),
                        "warn": len(self.warn()), "fail": len(self.blocking()),
                        "na": sum(1 for c in self.checks if c.status == "na")},
        }

    def text(self) -> str:
        lines: list[str] = []
        for check in self.checks:
            icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "na": "N/A"}[check.status]
            lines.append("[" + icon + "] " + check.title + "：" + check.conclusion)
            lines.append("       下一步：" + check.next_step)
        if self.plan:
            lines.append("")
            lines.append("可授权补全（djb env --authorize 一次授权 / djb env --rollback 回滚）：")
            for action in self.plan:
                lines.append("  - " + str(action.get("title")) + "："
                             + str(action.get("from")) + " -> " + str(action.get("to")))
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────── 探针
def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as handle:
            for line in handle:
                if "=" not in line:
                    continue
                key, value = line.rstrip("\n").split("=", 1)
                values[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _version_pair(text: str) -> tuple[int, int]:
    parts = (text or "").split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return major, minor
    except ValueError:
        return (0, 0)


def _fmt_version(pair: tuple[int, int]) -> str:
    return str(pair[0]) + "." + format(pair[1], "02d")


def _mem_total_gb() -> float:
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kb = int(line.split(":")[1].strip().split(" ")[0])
                    return round(kb / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def check_system(now: datetime) -> Check:
    values = _read_os_release()
    distro = (values.get("ID") or "").lower()
    version = values.get("VERSION_ID") or ""
    pretty = values.get("PRETTY_NAME") or (distro + " " + version).strip() or "未知发行版"
    arch = platform.machine() or "unknown"
    if arch not in facts.SUPPORTED_ARCHES:
        return Check("system", "系统与架构", "fail",
                     "不支持的架构：" + arch + "（S1 只支持 "
                     + " / ".join(facts.SUPPORTED_ARCHES) + "）",
                     "换 x86_64 / aarch64 机器，或等待对应架构的安装包",
                     {"arch": arch})
    if distro == "ubuntu":
        pair = _version_pair(version)
        ok = pair >= facts.MIN_UBUNTU
        return Check("system", "系统与架构", "ok" if ok else "fail",
                     pretty + " · " + arch + ("（受支持）" if ok else "（低于最低版本）"),
                     "无需处理" if ok else "升级到 Ubuntu "
                     + _fmt_version(facts.MIN_UBUNTU) + "+ 后再安装",
                     {"distro": distro, "version": version, "arch": arch})
    if distro == "debian":
        pair = _version_pair(version)
        ok = pair >= facts.MIN_DEBIAN
        return Check("system", "系统与架构", "ok" if ok else "fail",
                     pretty + " · " + arch + ("（受支持）" if ok else "（低于最低版本）"),
                     "无需处理" if ok else "升级到 Debian "
                     + _fmt_version(facts.MIN_DEBIAN) + "+ 后再安装",
                     {"distro": distro, "version": version, "arch": arch})
    if distro == "fedora":
        return Check("system", "系统与架构", "ok", pretty + " · " + arch + "（受支持）",
                     "无需处理", {"distro": distro, "version": version, "arch": arch})
    return Check("system", "系统与架构", "warn", pretty + " · " + arch + "（未在支持矩阵内）",
                 "可在 Ubuntu 22.04+ / Debian 12+ / Fedora 最近两版上获得完整支持；"
                 "继续使用需自行承担兼容性风险",
                 {"distro": distro, "version": version, "arch": arch})


def check_kernel(now: datetime) -> Check:
    release = platform.release() or "unknown"
    pair = _version_pair(release)
    ok = pair >= facts.MIN_KERNEL
    return Check("kernel", "内核", "ok" if ok else "warn",
                 "Linux " + release + ("（≥5.10）" if ok else "（低于 5.10）"),
                 "无需处理" if ok else "升级内核到 5.10+（容器/WSL 环境可能显示宿主内核）",
                 {"release": release})


def check_hardware(now: datetime) -> Check:
    cores = os.cpu_count() or 0
    memory = _mem_total_gb()
    monitor = ResourceMonitor().snapshot()
    status = "ok"
    next_step = "无需处理"
    conclusion = "CPU " + str(cores) + " 核 · 内存 " + str(memory) + " GB"
    if cores < 2 or (0 < memory < 2):
        status = "warn"
        conclusion += "（偏小）"
        next_step = "内存 <2 GB 或 CPU <2 核：建议只用 Token 中转（A1/A2），并把并发设为 1"
    return Check("hardware", "硬件", status, conclusion, next_step,
                 {"cpu_count": cores, "mem_gb": memory,
                  "load_cpu_pct": round(monitor["cpu_pct"] or 0.0, 1),
                  "mem_pct": round(monitor["mem_pct"] or 0.0, 1)})


def check_runtime(now: datetime, paths: Paths) -> Check:
    py = platform.python_version()
    pair = _version_pair(py)
    ok_py = pair >= (3, 10)
    try:
        sqlite_version = sqlite3.sqlite_version
    except Exception:                                       # pragma: no cover
        sqlite_version = "unknown"
    mode = file_mode(paths.home)
    ok_mode = mode is None or (mode & 0o077) == 0
    status = "ok" if (ok_py and ok_mode) else "warn"
    next_step = "无需处理"
    if not ok_py:
        next_step = "升级 Python 到 3.10+（客户端只用标准库，无第三方依赖）"
    elif not ok_mode:
        next_step = "本机目录权限过宽：执行 djb init 会收紧到 0700"
    conclusion = ("Python " + py + " · SQLite " + sqlite_version
                  + " · 本地目录 " + ("0700" if ok_mode else oct(mode or 0)))
    return Check("runtime", "运行时", status, conclusion, next_step,
                 {"python": py, "sqlite": sqlite_version,
                  "home_mode": oct(mode) if mode is not None else None})


def check_acceleration(now: datetime) -> Check:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return Check("acceleration", "加速栈", "warn",
                     "未检测到 NVIDIA 驱动 / 加速栈（不影响 Token 中转）",
                     "只共享 Token 中转（A1/A2）无需驱动；如需共享算力，请自行安装官方驱动后"
                     "重跑 djb env（本客户端不安装 / 不升级驱动，也不静默提权）",
                     {"nvidia_smi": False})
    try:
        completed = subprocess.run(
            [binary, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False)
        first = (completed.stdout or "").strip().splitlines()[0]
        return Check("acceleration", "加速栈", "ok",
                     "检测到 GPU：" + first,
                     "无需处理", {"nvidia_smi": True,
                                  "gpus": len((completed.stdout or "").strip().splitlines())})
    except (OSError, subprocess.SubprocessError, IndexError):
        return Check("acceleration", "加速栈", "warn", "nvidia-smi 存在但执行失败",
                     "确认驱动安装完整（nvidia-smi 可正常输出）；本客户端不会自动修复驱动",
                     {"nvidia_smi": True, "error": "smi_failed"})


def check_network(now: datetime, config: ClientConfig, port: int) -> Check:
    from urllib.parse import urlparse
    url = config.gateway_url or GATEWAY_PLACEHOLDER
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host or host.endswith(".invalid"):
        return Check("network", "网络", "warn", "尚未配置服务端地址（当前为占位地址）",
                     "执行 djb init --gateway https://<你的服务端域名> 后重试",
                     {"configured": False})
    scheme_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, scheme_port), timeout=PROBE_TIMEOUT_S):
            reachable = True
    except OSError:
        reachable = False
    port_free = True
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((facts.MIRROR_HOST, port))
    except OSError:
        port_free = False
    finally:
        probe.close()
    if not reachable:
        return Check("network", "网络", "warn", "无法连接服务端 " + host,
                     "检查出网 / 代理 / DNS；离线时节点只做离线自测，不会产生收益",
                     {"configured": True, "reachable": False, "port_free": port_free,
                      "mirror_port": port})
    if not port_free:
        return Check("network", "网络", "warn", "服务端可达，但本机端口 "
                     + str(port) + " 已被占用",
                     "换端口：djb init --port <新端口>（1024–65535）后重启 daemon",
                     {"configured": True, "reachable": True, "port_free": False,
                      "mirror_port": port})
    return Check("network", "网络", "ok", "服务端可达 · 本机端口 " + str(port) + " 可用",
                 "无需处理", {"configured": True, "reachable": True, "port_free": True,
                              "mirror_port": port})


# ─────────────────────────────────────────────────────────── 检测 / 授权 / 回滚
class EnvChecker:
    def __init__(self, paths: Paths, config: ClientConfig, *, store: Store | None = None,
                 budget_s: int = facts.ENV_CHECK_BUDGET_S,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.paths = paths
        self.config = config
        self.store = store
        self.budget_s = int(budget_s)
        self._now = now

    def detect(self) -> EnvReport:
        started = utcnow()
        checks: list[Check] = []
        budget_exceeded = False
        probes: list[tuple[str, Callable[[], Check]]] = [
            ("system", lambda: check_system(self._now())),
            ("kernel", lambda: check_kernel(self._now())),
            ("hardware", lambda: check_hardware(self._now())),
            ("runtime", lambda: check_runtime(self._now(), self.paths)),
            ("acceleration", lambda: check_acceleration(self._now())),
            ("network", lambda: check_network(self._now(), self.config,
                                              self.config.mirror_port)),
        ]
        for identifier, probe in probes:
            elapsed = (utcnow() - started).total_seconds()
            if elapsed > self.budget_s:
                budget_exceeded = True
                checks.append(Check(identifier, identifier, "warn",
                                    "检测超出 " + str(self.budget_s) + " 秒预算，已跳过",
                                    "稍后重跑 djb env；检测失败按更保守处理（不接新单）",
                                    {"skipped": True}))
                continue
            try:
                checks.append(probe())
            except Exception as exc:                    # INV-09：结论 + 下一步，不甩原始数据
                checks.append(Check(identifier, identifier, "warn",
                                    "检测失败（" + type(exc).__name__ + "）",
                                    "重跑 djb env；失败期间按更保守配置运行",
                                    {"error": type(exc).__name__}))
        elapsed_ms = int((utcnow() - started).total_seconds() * 1000)
        report = EnvReport(checks=checks, detected_at=iso(started), elapsed_ms=elapsed_ms,
                           budget_exceeded=budget_exceeded)
        report.plan = self._build_plan(report)
        return report

    # ── 计划（只做本地安全项）
    def _build_plan(self, report: EnvReport) -> list[dict[str, Any]]:
        profile = self.store.profile() if self.store is not None else ProfileRecord()
        hardware = next((check for check in report.checks if check.id == "hardware"), None)
        plan: list[dict[str, Any]] = []
        low_memory = bool(hardware and float(hardware.evidence.get("mem_gb") or 0) < 2)
        low_cpu = bool(hardware and int(hardware.evidence.get("cpu_count") or 0) < 2)
        if (low_memory or low_cpu) and profile.concurrent > 1:
            plan.append({"id": "concurrent", "title": "把并发降到 1（硬件偏小）",
                         "from": profile.concurrent, "to": 1, "applied": False})
        if low_memory and profile.mem_pct > 60:
            plan.append({"id": "mem_pct", "title": "内存上限收紧到 60%（硬件偏小）",
                         "from": profile.mem_pct, "to": 60, "applied": False})
        if report.budget_exceeded:
            plan.append({"id": "concurrent", "title": "检测超预算：并发降到 1（保守）",
                         "from": profile.concurrent, "to": 1, "applied": False})
        return _dedupe_actions(plan)

    def authorize(self, report: EnvReport | None = None) -> dict[str, Any]:
        """一次授权：应用计划；记录改动前后值供 rollback（幂等）。"""
        if self.store is None:
            raise ValueError("authorize 需要 store")
        report = report or self.detect()
        previous = self.store.env_state()
        if previous.get("authorized_at"):
            return {"authorized": False, "reason": "already_authorized",
                    "authorized_at": previous.get("authorized_at"),
                    "plan": previous.get("plan") or [],
                    "message": "已授权过；要改请先 rollback"}
        profile = self.store.profile()
        applied: list[dict[str, Any]] = []
        for action in report.plan:
            if action.get("applied"):
                continue
            action = dict(action)
            if action["id"] == "concurrent":
                profile.concurrent = int(action["to"])
            elif action["id"] in ("mem_pct", "cpu_pct", "gpu_pct"):
                setattr(profile, action["id"], int(action["to"]))
            elif action["id"] == "yield_enabled":
                profile.yield_enabled = bool(action["to"])
            else:
                continue
            action["applied"] = True
            applied.append(action)
        self.store.save_profile(profile)
        self.store.save_env_state(detected_at=report.detected_at,
                                  authorized_at=iso(self._now()),
                                  plan=applied, report=report.to_dict())
        return {"authorized": True, "applied": applied,
                "plan": applied, "report": report.to_dict()}

    def rollback(self) -> dict[str, Any]:
        if self.store is None:
            raise ValueError("rollback 需要 store")
        state = self.store.env_state()
        plan = state.get("plan") or []
        if not state.get("authorized_at"):
            return {"rolled_back": False, "reason": "not_authorized"}
        profile = self.store.profile()
        for action in plan:
            if not action.get("applied"):
                continue
            identifier = action.get("id")
            if identifier == "concurrent":
                profile.concurrent = int(action["from"])
            elif identifier in ("mem_pct", "cpu_pct", "gpu_pct"):
                setattr(profile, identifier, int(action["from"]))
            elif identifier == "yield_enabled":
                profile.yield_enabled = bool(action["from"])
        self.store.save_profile(profile)
        self.store.save_env_state(detected_at=state.get("detected_at"),
                                  authorized_at=None, plan=[],
                                  report=state.get("report") or {})
        return {"rolled_back": True, "restored": len(plan), "plan": plan}

    def state(self) -> dict[str, Any]:
        if self.store is None:
            return {}
        return self.store.env_state()


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for action in actions:
        seen[action["id"]] = action
    return list(seen.values())
