# SPDX-License-Identifier: Apache-2.0
"""护栏与限额（M15 §3 INV-04/05/06、§6.3、§8.2）。

优先级 **L1 > L2 > L3**（INV-04）：

* **L1 让路**：检测用户在用（全屏 / 游戏 / 会议 / 渲染 / 锁屏 / 电源 / 温度）。
  任一检测器**异常** -> 视为用户在用（更保守），立即暂停接单；
  「不适用」（如无显示器的服务器）不算失败。
* **L2 资源**：CPU 40% / GPU 70% / 内存 70% 为默认上限（可调，不越上限）。
  超限只停新单（软停），不中断在途任务。
* **L3 配额**：服务端 policy 的 quotas[] + 本地 Profile 的 daily/weekly/monthly +
  时段 schedule；超限一律软停、不结转、不中断在途。

账务口径：本地只累计 token 用量（用于软停），不产生金额；金额与账本在 M05/M06。
差异告警：本地计数与平台回传用量差 >1% 触发告警（LIMIT_DIFF_ALERT_PCT）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol

from . import facts
from .platform.no_window import subprocess_window_kwargs
from .state import NEXT_STEP
from .store import ProfileRecord, Store, next_window_start, window_key, utc_window_start
from .util import human_duration, iso, parse_iso, utcnow

# ─────────────────────────────────────────────────────────── L1 让路
YIELD_LOAD_RATIO = 0.9
YIELD_THERMAL_C = 85
THERMAL_BASE = "/sys/class/thermal"
POWER_BASE = "/sys/class/power_supply"
PROC_BASE = "/proc"
MEMINFO_PATH = "/proc/meminfo"
RENDER_PROCESSES = frozenset({"blender", "ffmpeg", "maya", "houdini", "nuke",
                              "unreal", "unity", "obs", "steam", "wine"})
POLL_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class Signal:
    """单个让路探针的结论：in_use / idle / not_applicable / error。"""

    source: str
    state: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in ("in_use", "idle", "not_applicable", "error"):
            raise ValueError("未知让路信号：" + str(self.state))


class Probe(Protocol):
    name: str

    def read(self, now: datetime) -> Signal: ...


class LoadProbe:
    """系统负载高于核数 90%：用户在做重活（编译 / 渲染 / 游戏）。"""

    name = "load"

    def read(self, now: datetime) -> Signal:
        try:
            load = os.getloadavg()[0]
            cores = os.cpu_count() or 1
        except OSError:
            return Signal(self.name, "error", "无法读取系统负载")
        ratio = float(load) / float(cores)
        state = "in_use" if ratio >= YIELD_LOAD_RATIO else "idle"
        return Signal(self.name, state, "load=" + format(ratio, ".2f") + "/核")


class ThermalProbe:
    """温度 ≥85°C：让路，避免把机器烤了。"""

    name = "thermal"

    def __init__(self, threshold_c: int = YIELD_THERMAL_C) -> None:
        self.threshold_c = threshold_c

    def read(self, now: datetime) -> Signal:
        base = THERMAL_BASE
        try:
            zones = [name for name in os.listdir(base) if name.startswith("thermal_zone")]
        except OSError:
            return Signal(self.name, "not_applicable", "无 thermal zone")
        hottest = 0
        for zone in zones:
            path = os.path.join(base, zone, "temp")
            try:
                with open(path, "r", encoding="ascii") as handle:
                    milli = int(handle.read().strip() or "0")
            except (OSError, ValueError):
                continue
            hottest = max(hottest, milli // 1000)
        if hottest == 0:
            return Signal(self.name, "not_applicable", "无可用温度读数")
        state = "in_use" if hottest >= self.threshold_c else "idle"
        return Signal(self.name, state, "最高 " + str(hottest) + "°C")


class BatteryProbe:
    """电池供电（未插电）：让路（省电优先）。"""

    name = "battery"

    def read(self, now: datetime) -> Signal:
        base = POWER_BASE
        try:
            entries = [name for name in os.listdir(base) if name.startswith("BAT")]
        except OSError:
            return Signal(self.name, "not_applicable", "无电源信息")
        for entry in entries:
            path = os.path.join(base, entry, "status")
            try:
                with open(path, "r", encoding="ascii") as handle:
                    status = handle.read().strip().lower()
            except OSError:
                continue
            if status == "discharging":
                return Signal(self.name, "in_use", "电池供电中")
        if not entries:
            return Signal(self.name, "not_applicable", "无电池")
        return Signal(self.name, "idle", "已接电")


class SessionProbe:
    """图形会话空闲检测（X11：xprintidle；Wayland：不确定 -> 保守）。

    M15 §7：让路检测失败 -> 视为用户在用，立即暂停。服务器（无 DISPLAY / WAYLAND）
    属于「不适用」，不暂停。
    """

    name = "session"

    def read(self, now: datetime) -> Signal:
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not has_display:
            return Signal(self.name, "not_applicable", "无图形会话（服务器）")
        binary = shutil.which("xprintidle")
        if not binary:
            return Signal(self.name, "error", "有图形会话但无空闲检测工具（xprintidle）")
        try:
            completed = subprocess.run([binary], capture_output=True, text=True,
                                       timeout=POLL_TIMEOUT_S, check=False,
                                       **subprocess_window_kwargs())
        except (OSError, subprocess.SubprocessError):
            return Signal(self.name, "error", "空闲检测工具执行失败")
        try:
            idle_ms = int(completed.stdout.strip() or "0")
        except ValueError:
            return Signal(self.name, "error", "空闲检测输出不可解析")
        return Signal(self.name, "idle" if idle_ms >= 60_000 else "in_use",
                      "空闲 " + str(idle_ms // 1000) + " 秒")


class RenderProbe:
    """渲染 / 游戏 / 录屏进程在跑：让路。"""

    name = "render"

    def read(self, now: datetime) -> Signal:
        base = PROC_BASE
        try:
            entries = os.listdir(base)
        except OSError:
            return Signal(self.name, "error", "无法读取进程表")
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(os.path.join(base, entry, "comm"), "r", encoding="utf-8",
                          errors="replace") as handle:
                    name = handle.read().strip()
            except OSError:
                continue
            if name in RENDER_PROCESSES:
                return Signal(self.name, "in_use", "检测到 " + name)
        return Signal(self.name, "idle", "无渲染/游戏进程")


@dataclass
class YieldResult:
    in_use: bool
    source: str           # in_use 的来源或 idle
    signals: list[Signal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"in_use": self.in_use, "source": self.source,
                "signals": [{"source": item.source, "state": item.state,
                             "detail": item.detail} for item in self.signals]}


class YieldDetector:
    def __init__(self, probes: Iterable[Probe] | None = None, *,
                 enabled: bool = True) -> None:
        self.probes = list(probes) if probes is not None else [
            LoadProbe(), ThermalProbe(), BatteryProbe(), SessionProbe(), RenderProbe()]
        self.enabled = bool(enabled)

    def evaluate(self, now: datetime | None = None) -> YieldResult:
        moment = now or utcnow()
        if not self.enabled:
            return YieldResult(in_use=False, source="disabled",
                               signals=[Signal("yield", "not_applicable", "已关闭让路")])
        signals: list[Signal] = []
        errors: list[Signal] = []
        in_use: list[Signal] = []
        for probe in self.probes:
            try:
                signal = probe.read(moment)
            except Exception as exc:                 # INV-04：检测失败 = 保守
                signal = Signal(getattr(probe, "name", "probe"), "error",
                                "检测器异常：" + type(exc).__name__)
            signals.append(signal)
            if signal.state == "error":
                errors.append(signal)
            elif signal.state == "in_use":
                in_use.append(signal)
        if errors:
            return YieldResult(in_use=True, source=errors[0].source, signals=signals)
        if in_use:
            return YieldResult(in_use=True, source=in_use[0].source, signals=signals)
        return YieldResult(in_use=False, source="idle", signals=signals)


# ─────────────────────────────────────────────────────────── L2 资源
class ResourceMonitor:
    """CPU / 内存 / GPU 占用（取不到 GPU 就返回 None，不猜测）。"""

    def cpu_pct(self) -> float | None:
        try:
            load = os.getloadavg()[0]
        except (OSError, AttributeError):
            # Windows 上 `os` 模块没有 getloadavg（抛 AttributeError 而不是 OSError）——
            # 之前只捕 OSError，导致 `djb limits --no-yield` 后守护进程在 _bootstrap →
            # _evaluate → resources.snapshot() 处直接崩溃退出。取不到就不猜（与 mem_pct 同口径）。
            return None
        cores = os.cpu_count() or 1
        return max(0.0, min(100.0, 100.0 * float(load) / float(cores)))

    def mem_pct(self) -> float | None:
        try:
            with open(MEMINFO_PATH, "r", encoding="ascii") as handle:
                values: dict[str, int] = {}
                for line in handle:
                    parts = line.split(":")
                    if len(parts) != 2:
                        continue
                    value = parts[1].strip().split(" ")[0]
                    try:
                        values[parts[0].strip()] = int(value)
                    except ValueError:
                        continue
        except OSError:
            return None
        total = values.get("MemTotal") or 0
        available = values.get("MemAvailable")
        if not total or available is None:
            return None
        return max(0.0, min(100.0, 100.0 * (total - available) / total))

    def gpu_pct(self) -> float | None:
        binary = shutil.which("nvidia-smi")
        if not binary:
            return None
        try:
            completed = subprocess.run(
                [binary, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=POLL_TIMEOUT_S, check=False,
                **subprocess_window_kwargs())
            first = completed.stdout.strip().splitlines()[0]
            return max(0.0, min(100.0, float(first.strip())))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def snapshot(self) -> dict[str, float | None]:
        return {"cpu_pct": self.cpu_pct(), "mem_pct": self.mem_pct(),
                "gpu_pct": self.gpu_pct()}


# ─────────────────────────────────────────────────────────── L3 配额 / 时段
@dataclass
class Decision:
    accept: bool
    reason: str
    pause_reason: str | None = None
    next_step: str = ""
    retry_at: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"accept": self.accept, "reason": self.reason,
                "pause_reason": self.pause_reason, "next_step": self.next_step,
                "retry_at": self.retry_at, "detail": dict(self.detail)}


def parse_schedule_windows(spec: str) -> list[dict[str, Any]]:
    """把 "09:00-23:00" 或逗号分隔的多段解析成窗口（默认全周）。

    也可传 JSON 列表（含 days 与跨天窗口），见 djb limits set --schedule-json。
    """
    text = (spec or "").strip()
    if not text:
        return []
    windows: list[dict[str, Any]] = []
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" not in token:
            raise ValueError("时段格式必须是 HH:MM-HH:MM：" + token)
        start, end = (item.strip() for item in token.split("-", 1))
        _validate_clock(start)
        _validate_clock(end)
        windows.append({"days": list(range(7)), "start": start, "end": end})
    return windows


def _validate_clock(text: str) -> None:
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("时间必须是 HH:MM：" + text)
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时间越界：" + text)


def in_schedule(windows: list[dict[str, Any]], now: datetime) -> tuple[bool, str | None]:
    """空窗口 = 全天允许；跨天窗口（start > end）按两天计算。"""
    if not windows:
        return True, None
    local = now.astimezone() if now.tzinfo else now
    weekday = local.weekday()
    current = local.hour * 60 + local.minute
    for window in windows:
        try:
            days = [int(item) for item in window.get("days", list(range(7)))]
            start = _minutes(window.get("start", "00:00"))
            end = _minutes(window.get("end", "23:59"))
        except (TypeError, ValueError):
            continue
        if weekday not in days and not (start > end and ((weekday - 1) % 7) in days):
            continue
        if start <= end:
            if start <= current <= end:
                return True, None
        else:
            # 跨天：当天 [start, 24:00) 或 [00:00, end]
            if current >= start or current <= end:
                return True, None
    return False, iso(now)


def _minutes(text: Any) -> int:
    parts = str(text).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _day_start(now: datetime) -> datetime:
    moment = now.astimezone(timezone.utc)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(now: datetime) -> datetime:
    day = _day_start(now)
    return day - timedelta(days=day.weekday())


def _month_start(now: datetime) -> datetime:
    day = _day_start(now)
    return day.replace(day=1)


@dataclass
class QuotaWindow:
    key: str
    window_seconds: int
    limit: int
    start: datetime


class Limiter:
    """L1 > L2 > L3 的统一裁决器（纯逻辑 + store 用量读写）。"""

    def __init__(self, store: Store, *, detector: YieldDetector | None = None,
                 resources: ResourceMonitor | None = None,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.detector = detector or YieldDetector()
        self.resources = resources or ResourceMonitor()
        self._now = now

    # ── L3 窗口
    def _profile_windows(self, profile: ProfileRecord, now: datetime) -> list[QuotaWindow]:
        out: list[QuotaWindow] = []
        if profile.daily_tokens > 0:
            start = _day_start(now)
            out.append(QuotaWindow(window_key("profile:daily", 86400), 86400,
                                   int(profile.daily_tokens), start))
        if profile.weekly_tokens > 0:
            start = _week_start(now)
            out.append(QuotaWindow(window_key("profile:weekly", 7 * 86400), 7 * 86400,
                                   int(profile.weekly_tokens), start))
        if profile.monthly_tokens > 0:
            start = _month_start(now)
            out.append(QuotaWindow(window_key("profile:monthly", 30 * 86400),
                                   30 * 86400, int(profile.monthly_tokens), start))
        return out

    def _policy_windows(self, policy: Any, now: datetime) -> list[QuotaWindow]:
        out: list[QuotaWindow] = []
        quotas = getattr(policy, "quotas", None) if policy is not None else None
        if not isinstance(quotas, list):
            return out
        for quota in quotas:
            if not isinstance(quota, dict):
                continue
            value = quota.get("value")
            window = quota.get("window_seconds")
            name = quota.get("name")
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                continue                       # 未下发数值 = 本地不限额（不内置默认）
            if not isinstance(window, int) or window < 1 or not isinstance(name, str):
                continue
            out.append(QuotaWindow(window_key("policy:" + name, window), window, value,
                                   utc_window_start(window, now)))
        return out

    def windows(self, profile: ProfileRecord, policy: Any,
                now: datetime | None = None) -> list[QuotaWindow]:
        moment = now or self._now()
        return self._profile_windows(profile, moment) + self._policy_windows(policy, moment)

    def record_usage(self, tokens: int, *, now: datetime | None = None,
                     profile: ProfileRecord | None = None,
                     policy: Any = None) -> None:
        if tokens <= 0:
            return
        moment = now or self._now()
        windows = self.windows(profile or self.store.profile(), policy, moment)
        for window in windows:
            self.store.add_usage(window.key, iso(window.start), int(tokens), 1)

    def remaining(self, profile: ProfileRecord, policy: Any,
                  now: datetime | None = None) -> list[dict[str, Any]]:
        moment = now or self._now()
        out: list[dict[str, Any]] = []
        for window in self.windows(profile, policy, moment):
            used = self.store.usage_total(window.key, since=iso(window.start))
            out.append({"name": window.key, "limit": window.limit,
                        "used": used, "remaining": max(0, window.limit - used),
                        "window_seconds": window.window_seconds,
                        "resets_at": iso(moment + timedelta(seconds=window.window_seconds)
                                         - timedelta(seconds=int(moment.timestamp())
                                                     % window.window_seconds))})
        return out

    # ── 统一裁决
    def evaluate(self, *, profile: ProfileRecord | None = None, policy: Any = None,
                 inflight: int = 0, unbound: bool = False, conservative: bool = False,
                 yield_enabled: bool | None = None,
                 now: datetime | None = None) -> Decision:
        moment = now or self._now()
        profile = profile or self.store.profile()
        # 1) 未绑定 / 保守模式（不是「离线」，但确实不接单，INV-01 给原因）
        if unbound:
            return Decision(False, "unbound", "unbound", NEXT_STEP["unbound"])
        if conservative:
            return Decision(False, "conservative", "conservative", NEXT_STEP["conservative"])
        # 2) L1 让路
        enabled = profile.yield_enabled if yield_enabled is None else bool(yield_enabled)
        self.detector.enabled = enabled
        yielded = self.detector.evaluate(moment)
        if yielded.in_use:
            return Decision(False, "yield_paused", "yield_paused", NEXT_STEP["yield_paused"],
                            detail={"source": yielded.source,
                                    "signals": yielded.to_dict()["signals"]})
        # 3) L2 资源
        caps = {"cpu_pct": profile.cpu_pct, "gpu_pct": profile.gpu_pct,
                "mem_pct": profile.mem_pct}
        snapshot = self.resources.snapshot()
        over = {}
        for key, cap in caps.items():
            value = snapshot.get(key)
            if value is not None and value > cap:
                over[key] = {"value": round(float(value), 1), "cap": cap}
        if over:
            return Decision(False, "resource_limited", "resource_limited",
                            NEXT_STEP["resource_limited"], detail={"over": over,
                                                                  "snapshot": snapshot})
        # 4) L3 时段
        ok, _ = in_schedule(profile.schedule, moment)
        if not ok:
            retry = _next_schedule_start(profile.schedule, moment)
            return Decision(False, "schedule_paused", "schedule_paused",
                            NEXT_STEP["schedule_paused"], retry_at=iso(retry),
                            detail={"schedule": profile.schedule, "resumes_at": iso(retry)})
        # 5) L3 配额（服务端 policy 优先展示；本地 Profile 一起判）
        for window in self.windows(profile, policy, moment):
            used = self.store.usage_total(window.key, since=iso(window.start))
            if used >= window.limit:
                retry = next_window_start(window.window_seconds, moment)
                return Decision(False, "quota_exhausted", "quota_exhausted",
                                NEXT_STEP["quota_exhausted"], retry_at=iso(retry),
                                detail={"window": window.key, "limit": window.limit,
                                        "used": used,
                                        "resets_in": human_duration(
                                            (retry - moment).total_seconds())})
        # 6) 并发（在线但不接这一单：429，不暂停）
        if inflight >= max(1, int(profile.concurrent)):
            return Decision(False, "concurrency_limited", None,
                            "并发已满：稍后重试或 djb limits set --concurrent 调整",
                            retry_at=None,
                            detail={"inflight": inflight, "concurrent": profile.concurrent})
        return Decision(True, "ok")


def _next_schedule_start(windows: list[dict[str, Any]], now: datetime) -> datetime:
    """粗略计算下次进入允许时段的时间（最多看 7 天）。"""
    moment = now
    for _ in range(7 * 24 * 4):
        moment = moment + timedelta(minutes=15)
        ok, _ = in_schedule(windows, moment)
        if ok:
            return moment
    return now + timedelta(hours=24)


def discrepancy_alert(local_tokens: int, server_tokens: int | None) -> bool:
    """本地执行 / 平台记账差异 >1% 告警（INV-05）；平台未回传则不判。"""
    if server_tokens is None:
        return False
    if server_tokens <= 0:
        return local_tokens > 0
    diff = abs(int(local_tokens) - int(server_tokens)) / float(server_tokens) * 100.0
    return diff > facts.LIMIT_DIFF_ALERT_PCT


def resource_caps_valid(cpu: int, gpu: int, mem: int) -> None:
    for key, value in (("cpu_pct", cpu), ("gpu_pct", gpu), ("mem_pct", mem)):
        _default, low, high = facts.RESOURCE_CAPS[key]
        if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
            raise ValueError(key + " 必须在 " + str(low) + "-" + str(high) + " 之间")
