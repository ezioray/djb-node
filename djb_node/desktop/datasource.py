# SPDX-License-Identifier: Apache-2.0
"""桌面客户端数据层：协议 + 真实现 + 假实现；任何失败降级为「暂不可用」。

设计边界
========

* **纯 stdlib**：本模块不 import tkinter；无图形环境也能跑全量单测。
* **永不抛栈**：每个数据域由 :meth:`LocalDataSource._safe` 包裹；外部依赖（CLI 子进程 /
  云端 HTTP / GPU 工具 / 日志文件 / 本机状态库）任何失败都变成
  :class:`Unavailable`（含中文原因 + 下一步），UI 只渲染
  :attr:`Unavailable.text`（``暂不可用（原因）``），不会空白、不会 0 冒充无数据。
* **只读**：本层不写 daemon、不写 store；需要写的地方（接入配置）在
  :mod:`djb_node.desktop.access`，且落的是本机自有文件。
* **真实现走既有接口**：状态用 ``python -m djb_node status --json``（与托盘同一条只读路径）；
  云端只用 :class:`djb_node.api.NodeApi` 上**已存在**的只读方法（模型目录 / 绑定 / 策略 /
  能力），不臆造端点 —— 云端暂无「任务 / 收据 / 收益明细」接口时，这些视图如实显示
  「暂不可用（云端接口未接线）」并回落到本地事实。
* **可替换**：:class:`DataSource` 是协议；测试注入 :class:`FakeDataSource` /
  :class:`NullDataSource`，不需要真 CLI、真网络、真 GPU。

数据域
======
:data:`SECTIONS` 的 12 项对应 12 个视图的数据来源（视图映射见 :mod:`djb_node.desktop.views`）。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .access import ACCESS_FILE, AccessConfig, FileAccessStore, mask_secret

__all__ = [
    "DATA_SOURCE_HINT", "DataSource", "FakeDataSource", "LocalDataSource", "NullDataSource",
    "ProviderError", "RingLog", "SECTIONS", "Snapshot", "UNAVAILABLE", "Unavailable",
    "benchmark", "gpu_devices", "local_facts", "parse_rocm_smi", "parse_nvidia_smi",
]

#: 统一不可用文案（基准 §3：未知/未接线 = 灰 + 「暂不可用」）
UNAVAILABLE = "暂不可用"
#: 无数据时的下一步提示
DATA_SOURCE_HINT = "稍后刷新；仍未恢复请查看日志或运行 djb diagnose"

#: 12 个数据域（顺序即视图顺序，见 :mod:`djb_node.desktop.views`）
SECTIONS: tuple[str, ...] = (
    "status", "bench", "gpu", "models", "tasks", "receipts",
    "earnings", "agent", "access", "yield", "account", "logs",
)
#: 日志级别（与 :data:`djb_node.facts.LOG_LEVELS` 一致 + warning/error）
LOG_LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error", "critical")


class ProviderError(RuntimeError):
    """某个数据域取数失败；消息只放**短中文原因**，不带 URL / 令牌 / 原始异常文本。"""

    def __init__(self, reason: str, *, hint: str = DATA_SOURCE_HINT) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.hint = str(hint)

    @property
    def text(self) -> str:
        """统一口径：``暂不可用（原因）``（拼给 note 用，避免各处自己造词）。"""
        return UNAVAILABLE + "（" + self.reason + "）"


@dataclass(frozen=True)
class Unavailable:
    """「暂不可用」：有原因、有下一步；不是一个空字符串，也不是 0。"""

    reason: str
    hint: str = DATA_SOURCE_HINT

    def __post_init__(self) -> None:
        if not str(self.reason or "").strip():
            raise ValueError("Unavailable 必须给出原因")

    @property
    def text(self) -> str:
        return UNAVAILABLE + "（" + self.reason + "）"

    def to_dict(self) -> dict[str, str]:
        return {"status": "unavailable", "reason": self.reason, "hint": self.hint,
                "text": self.text}


@dataclass(frozen=True)
class Snapshot:
    """一次取数的结果：每个数据域要么是映射，要么是 :class:`Unavailable`。"""

    sections: Mapping[str, Any]
    fetched_at: str = ""
    source: str = "local"
    notes: tuple[str, ...] = ()

    def get(self, name: str, default: Any = None) -> Any:
        return self.sections.get(name, default)

    def ok(self, name: str) -> bool:
        return isinstance(self.sections.get(name), Mapping)

    def missing(self, name: str) -> Unavailable | None:
        value = self.sections.get(name)
        return value if isinstance(value, Unavailable) else None

    def text_of(self, name: str, field: str = "", default: str = UNAVAILABLE) -> str:
        """渲染取值：域不可用时直接给「暂不可用（原因）」，字段缺失给 ``default``。"""
        value = self.sections.get(name)
        if isinstance(value, Unavailable):
            return value.text
        if not isinstance(value, Mapping):
            return default
        if not field:
            return default
        raw = value.get(field)
        return default if raw is None or raw == "" else str(raw)


@runtime_checkable
class DataSource(Protocol):
    """数据源协议：一次取全部（或指定）数据域；不抛异常。"""

    def snapshot(self, *, sections: Sequence[str] | None = None) -> Snapshot:
        ...


# ─────────────────────────────────────────────────────────────── 本机事实 / 跑分
def _read_meminfo() -> tuple[int, int]:
    """返回 (总内存字节, 可用字节)；读不到给 (0, 0)（调用方按不可用处理）。"""
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii", errors="ignore")
    except OSError:
        return 0, 0
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        chunk = rest.strip().split()
        if chunk and chunk[0].isdigit():
            values[key.strip()] = int(chunk[0]) * 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def local_facts(home: str | os.PathLike | None = None) -> dict[str, Any]:
    """本机 CPU / 内存 / 磁盘 / 系统事实（纯读取，无网络）。"""
    total_mem, avail_mem = _read_meminfo()
    disk_total = disk_free = 0
    try:
        usage = shutil.disk_usage(str(home or Path.home()))
        disk_total, disk_free = int(usage.total), int(usage.free)
    except OSError:
        pass
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "cpu_count": os.cpu_count() or 0,
        "cpu_load_1m": round(float(load[0]), 2),
        "mem_total_bytes": total_mem,
        "mem_available_bytes": avail_mem,
        "disk_total_bytes": disk_total,
        "disk_free_bytes": disk_free,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def benchmark(*, budget_s: float = 10.0, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """本地跑分（本地计算，≤budget_s 秒；不联网、不读写用户数据）。

    三项：sha256 吞吐（MB/s）、浮点循环（Mops/s）、整数位运算（Mops/s）。
    每项都在 deadline 前收手，避免慢机器卡住 UI。
    """
    import hashlib
    deadline = clock() + max(0.5, float(budget_s))
    block = b"djb-daylight-signal" * 4096            # 80 KiB
    digest_mb = 0.0
    started = clock()
    while clock() < deadline:
        for _ in range(32):
            hashlib.sha256(block).digest()
            digest_mb += len(block) / (1024 * 1024)
        if clock() - started >= 0.25:
            break
    hash_seconds = max(1e-6, clock() - started)

    floats = 0
    started = clock()
    value = 1.000001
    while clock() < deadline and clock() - started < 0.25:
        for _ in range(20000):
            value = value * 1.0000001 + 0.0000001
        floats += 20000
    float_seconds = max(1e-6, clock() - started)

    integers = 0
    started = clock()
    acc = 12345
    while clock() < deadline and clock() - started < 0.25:
        for _ in range(50000):
            acc = (acc * 1103515245 + 12345) & 0x7FFFFFFF
        integers += 50000
    int_seconds = max(1e-6, clock() - started)
    return {
        "hash_mbps": round(digest_mb / hash_seconds, 1),
        "float_mops": round(floats / float_seconds / 1e6, 2),
        "int_mops": round(integers / int_seconds / 1e6, 2),
        "budget_s": float(budget_s),
        "elapsed_s": round(hash_seconds + float_seconds + int_seconds, 2),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ─────────────────────────────────────────────────────────────── GPU
def parse_nvidia_smi(text: str) -> list[dict[str, Any]]:
    """解析 ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits``。"""
    devices = []
    for line in str(text or "").splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 5 or not parts[0]:
            continue
        devices.append({"vendor": "nvidia", "name": parts[0],
                        "memory_total_mb": _to_int(parts[1]), "memory_used_mb": _to_int(parts[2]),
                        "temperature_c": _to_int(parts[3]), "utilization_pct": _to_int(parts[4])})
    return devices


def parse_rocm_smi(payload: Any) -> list[dict[str, Any]]:
    """解析 ``rocm-smi --json``；字段名随版本变化，做容错读取。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(payload, Mapping):
        return []
    devices = []
    for key, value in payload.items():
        if not isinstance(value, Mapping) or not str(key).lower().startswith("card"):
            continue
        name = str(value.get("Card series") or value.get("Card SKU")
                   or value.get("Card Series") or value.get("Device Name") or key)
        devices.append({
            "vendor": "amd", "name": name,
            "memory_total_mb": _to_int(value.get("VRAM Total Memory (B)"), scale=1024 * 1024),
            "memory_used_mb": _to_int(value.get("VRAM Total Used Memory (B)"), scale=1024 * 1024),
            "temperature_c": _to_int(value.get("Temperature (Sensor edge) (C)")
                                     or value.get("Temperature (Sensor junction) (C)")),
            "utilization_pct": _to_int(value.get("GPU use (%)")),
        })
    return devices


def _to_int(value: Any, *, scale: int = 1) -> int | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number < 0 or not number < float("inf"):
        return None
    return int(number // scale) if scale != 1 else int(number)


def gpu_devices(*, run: Callable[[Sequence[str]], str] | None = None,
                which: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    """GPU 探测：nvidia-smi 优先，rocm-smi 兜底；都没有 → 无 GPU（不是错误）。"""
    runner = run or _run_text
    attempts: list[str] = []
    if which("nvidia-smi"):
        try:
            text = runner(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,"
                                      "temperature.gpu,utilization.gpu",
                           "--format=csv,noheader,nounits"])
            devices = parse_nvidia_smi(text)
            if devices:
                return {"present": True, "devices": devices, "note": "nvidia-smi"}
            attempts.append("nvidia-smi 无输出")
        except (OSError, subprocess.SubprocessError) as exc:
            attempts.append("nvidia-smi 失败：" + type(exc).__name__)
    else:
        attempts.append("没有 nvidia-smi")
    if which("rocm-smi"):
        try:
            devices = parse_rocm_smi(runner(["rocm-smi", "--json", "--showproductname",
                                             "--showmeminfo", "vram", "--showtemp", "--showuse"]))
            if devices:
                return {"present": True, "devices": devices, "note": "rocm-smi"}
            attempts.append("rocm-smi 无输出")
        except (OSError, subprocess.SubprocessError) as exc:
            attempts.append("rocm-smi 失败：" + type(exc).__name__)
    else:
        attempts.append("没有 rocm-smi")
    return {"present": False, "devices": [], "note": "本机无可用 GPU（" + "；".join(attempts) + "）"}


def _run_text(argv: Sequence[str], *, timeout: float = 4.0) -> str:
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                               check=False, stdin=subprocess.DEVNULL)
    if completed.returncode != 0:
        raise ProviderError("命令返回非零")
    return completed.stdout


# ─────────────────────────────────────────────────────────────── 日志 ring buffer
class RingLog:
    """本地日志 ring buffer：尾部读 JSONL 到定长 deque，按级别筛选。

    只读、不轮转、不删除；解析失败的行记为 ``level="unknown"`` 而不是丢弃
    （否则「日志为空」和「日志坏了」无法区分）。
    """

    def __init__(self, path: str | os.PathLike, *, limit: int = 2000) -> None:
        self._path = Path(path)
        self._limit = max(1, int(limit))

    @property
    def path(self) -> Path:
        return self._path

    def tail(self, *, limit: int = 200) -> list[dict[str, Any]]:
        from collections import deque
        count = max(1, min(int(limit), self._limit))
        buffer: deque[str] = deque(maxlen=self._limit)
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    buffer.append(line)
        except FileNotFoundError:
            raise ProviderError("日志文件尚未生成", hint="daemon 启动后会出现 " + self._path.name)
        except OSError as exc:
            raise ProviderError("日志不可读（" + type(exc).__name__ + "）") from None
        rows = [_parse_log_line(line) for line in list(buffer)[-count:]]
        return rows

    def read(self, *, level: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.tail(limit=limit if not level else self._limit)
        if level and level != "all":
            wanted = str(level).lower()
            rows = [row for row in rows if str(row.get("level")) == wanted]
        return rows[-limit:]


def _parse_log_line(line: str) -> dict[str, Any]:
    text = line.strip()
    if not text:
        return {"ts": "", "level": "unknown", "event": "（空行）"}
    try:
        payload = json.loads(text)
    except ValueError:
        return {"ts": "", "level": "unknown", "event": text[:160]}
    if not isinstance(payload, Mapping):
        return {"ts": "", "level": "unknown", "event": text[:160]}
    level = str(payload.get("level") or "unknown").lower()
    return {"ts": str(payload.get("ts") or ""), "level": level,
            "event": str(payload.get("event") or ""),
            "fields": {key: value for key, value in payload.items()
                       if key not in ("ts", "level", "event", "module")}}


# ─────────────────────────────────────────────────────────────── 真实现
class LocalDataSource:
    """真实数据源：只读 CLI + 本机事实 + GPU 工具 + 日志 + 云端（可选）。

    ``runner`` / ``cloud`` / ``which`` / ``facts`` / ``bench`` / ``store_factory``
    都可注入：单测不需要真 CLI、真网络、真 GPU。
    """

    def __init__(self, home: str | os.PathLike | None = None, *,
                 runner: Callable[[Sequence[str]], Mapping[str, Any]] | None = None,
                 cloud: Any | None = None,
                 which: Callable[[str], str | None] = shutil.which,
                 facts: Callable[[], Mapping[str, Any]] = local_facts,
                 bench: Callable[..., Mapping[str, Any]] = benchmark,
                 store_factory: Callable[[Path], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 cli_timeout: float = 8.0,
                 log_limit: int = 2000) -> None:
        from ..config import resolve_home
        self._home = Path(home or resolve_home(None))
        self._runner = runner or self._cli_runner
        self._cloud = cloud
        self._which = which
        self._facts = facts
        self._bench = bench
        self._store_factory = store_factory
        self._clock = clock
        self._cli_timeout = float(cli_timeout)
        self._access = FileAccessStore(self._home / ACCESS_FILE)
        self._logs = RingLog(self._home / "logs" / "djb.log", limit=log_limit)
        self.notes: list[str] = []

    # ── 基础
    @property
    def home(self) -> Path:
        return self._home

    @property
    def access_store(self) -> FileAccessStore:
        return self._access

    @property
    def log_ring(self) -> RingLog:
        return self._logs

    def _cli_runner(self, argv: Sequence[str]) -> Mapping[str, Any]:
        """只读子进程：``python -m djb_node <argv> --json``（与托盘同一条路径）。"""
        if "--json" not in argv:
            argv = list(argv) + ["--json"]
        command = [sys.executable, "-m", "djb_node", *argv]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=self._cli_timeout, check=False,
                                       stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ProviderError("本地命令超时") from None
        except OSError as exc:
            raise ProviderError("本地命令无法执行（" + type(exc).__name__ + "）") from None
        if completed.returncode != 0:
            raise ProviderError("本地命令退出码 " + str(completed.returncode))
        try:
            payload = json.loads(completed.stdout or "{}")
        except ValueError:
            raise ProviderError("本地命令输出不是 JSON") from None
        if not isinstance(payload, Mapping):
            raise ProviderError("本地命令输出格式不符")
        return payload

    def _store(self):
        if self._store_factory is not None:
            return self._store_factory(Path(self._home) / "state.db")
        from ..store import Store
        return Store(str(Path(self._home) / "state.db"))

    def _safe(self, name: str, loader: Callable[[], Any]) -> Any:
        try:
            value = loader()
        except ProviderError as exc:
            return Unavailable(exc.reason, exc.hint)
        except KeyError as exc:                        # 依赖模块字段改名（并行写者）
            return Unavailable(name + " 数据结构变化（缺 " + str(exc.args[0])[:24] + "）")
        except Exception as exc:                       # 任何未预期失败都不许冒泡到 UI
            return Unavailable(name + " 读取失败（" + type(exc).__name__ + "）")
        return value

    # ── 组装
    def snapshot(self, *, sections: Sequence[str] | None = None) -> Snapshot:
        wanted = tuple(sections) if sections else SECTIONS
        unknown = [name for name in wanted if name not in SECTIONS]
        if unknown:
            raise ValueError("未知数据域：" + ", ".join(unknown))
        loader = {
            "status": self._status, "bench": self._bench_section, "gpu": self._gpu,
            "models": self._models, "tasks": self._tasks, "receipts": self._receipts,
            "earnings": self._earnings, "agent": self._agent, "access": self._access_section,
            "yield": self._yield, "account": self._account, "logs": self._logs_section,
        }
        collected: dict[str, Any] = {}
        for name in wanted:
            collected[name] = self._safe(name, loader[name])
        return Snapshot(sections=collected,
                        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock())),
                        source="local", notes=tuple(self.notes))

    # ── 各数据域
    def status(self) -> Mapping[str, Any]:
        payload = self._runner(["status"])
        if not isinstance(payload, Mapping) or "state" not in payload:
            raise ProviderError("状态回显不完整")
        return payload

    def _status(self) -> Mapping[str, Any]:
        return dict(self.status())

    def _bench_section(self) -> Mapping[str, Any]:
        facts = dict(self._facts())
        checks = []
        try:
            from ..envcheck import check_hardware, check_runtime, check_system
            from ..config import ClientConfig, Paths
            from ..util import utcnow
            now = utcnow()
            paths = Paths(str(self._home))
            for probe in (check_system, lambda: check_hardware(now),
                          lambda: check_runtime(now, paths)):
                try:
                    check = probe()
                    checks.append({"id": getattr(check, "identifier", ""),
                                   "level": getattr(check, "level", ""),
                                   "summary": getattr(check, "message", ""),
                                   "next_step": getattr(check, "next_step", "")})
                except Exception as exc:             # 单项失败不影响其余
                    checks.append({"id": "probe", "level": "warn",
                                   "summary": "检测失败（" + type(exc).__name__ + "）",
                                   "next_step": "稍后重试"})
            _ = ClientConfig
        except ImportError:
            checks.append({"id": "envcheck", "level": "warn", "summary": "环境检测模块不可用",
                           "next_step": "升级客户端后重试"})
        return {**facts, "checks": checks,
                "gpu": self._safe("gpu", self._gpu)}

    def _gpu(self) -> Mapping[str, Any]:
        return gpu_devices(which=self._which)

    def _cloud_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._cloud is None:
            raise ProviderError("云端未接通（未登录节点）", hint="完成配对后重试")
        handler = getattr(self._cloud, method, None)
        if handler is None:
            raise ProviderError("云端接口未接线（" + method + "）", hint="等待平台提供该接口")
        try:
            return handler(*args, **kwargs)
        except ProviderError:
            raise                       # 已经带可读原因（401 / 服务端降级 / 未上线）
        except Exception as exc:
            raise ProviderError("云端请求失败（" + type(exc).__name__ + "）") from None

    def _cloud_pages(self, kind: str, method: str) -> dict[str, Any]:
        """按 ``page.next_cursor`` 取全量（客户端负责翻页上限）；无 ``fetch`` 的桩只取单页。"""
        fetch = getattr(self._cloud, "fetch", None) if self._cloud is not None else None
        if callable(fetch):
            payload = fetch(kind)
            rows = payload.get("items") if isinstance(payload, Mapping) else payload
            return {"rows": [row for row in (rows or []) if isinstance(row, Mapping)],
                    "next_cursor": payload.get("next_cursor") if isinstance(payload, Mapping) else None,
                    "truncated": bool(payload.get("truncated")) if isinstance(payload, Mapping) else False}
        payload = self._cloud_call(method)
        rows = _as_rows(payload)
        cursor = None
        if isinstance(payload, Mapping):
            page = payload.get("page") if isinstance(payload.get("page"), Mapping) else payload
            cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
        return {"rows": rows, "next_cursor": cursor, "truncated": False}

    def _local_models(self) -> list[dict[str, Any]]:
        try:
            payload = self._runner(["model-sync", "status"])
        except ProviderError as exc:
            self.notes.append("本地模型清单不可用：" + exc.reason)
            return []
        rows = payload.get("applied") or payload.get("models") or []
        return [row for row in rows if isinstance(row, Mapping)]

    def _models(self) -> Mapping[str, Any]:
        catalog_rows: list[dict[str, Any]] = []
        error: Unavailable | None = None
        try:
            catalog = self._cloud_call("model_catalog")
        except ProviderError as exc:
            error = Unavailable(exc.reason, exc.hint)
        else:
            for row in _as_rows(catalog):
                catalog_rows.append({
                    "id": str(row.get("id") or row.get("model") or ""),
                    "context": row.get("context") or row.get("context_length"),
                    "offered": bool(row.get("offerable", True)),
                    "reason": str(row.get("incompatible_reason") or ""),
                })
        local = [{"id": str(row.get("model") or row.get("id") or ""),
                  "state": str(row.get("state") or row.get("status") or "")} for row in self._local_models()]
        if not catalog_rows and not local:
            raise ProviderError(error.reason if error else "模型目录为空",
                                hint=error.hint if error else DATA_SOURCE_HINT)
        return {"catalog": catalog_rows, "local": local,
                "online": error is None,
                "note": "" if error is None else error.text}

    def _tasks(self) -> Mapping[str, Any]:
        status = self._safe("status", self._status)
        inflight = status.get("inflight") if isinstance(status, Mapping) else None
        rows = [row for row in inflight if isinstance(row, Mapping)] if isinstance(inflight, list) else []
        try:
            pages = self._cloud_pages("tasks", "list_tasks")
        except ProviderError as exc:
            if not rows:
                raise ProviderError(exc.reason, hint="本地也没有在途任务；有任务时再刷新") from None
            return {"rows": rows, "origin": "local",
                    "note": "云端任务列表暂不可用：" + exc.reason}
        note = _more_note(pages)
        return {"rows": pages["rows"], "origin": "cloud", "note": note,
                "next_cursor": pages["next_cursor"]}

    def _receipts(self) -> Mapping[str, Any]:
        rows: list[dict[str, Any]] = []
        origin = "local"
        note = ""
        next_cursor = None
        try:
            pages = self._cloud_pages("receipts", "list_receipts")
        except ProviderError as exc:
            note = "云端收据暂不可用：" + exc.reason
        else:
            rows, origin, next_cursor = pages["rows"], "cloud", pages["next_cursor"]
            note = _more_note(pages)
        local_ok = True
        if not rows:
            try:
                store = self._store()
                for row in store.receipts(limit=200):
                    record = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                    rows.append(record)
            except Exception as exc:
                local_ok = False
                if not note:
                    raise ProviderError("本地收据不可读（" + type(exc).__name__ + "）") from None
                note += "；本地收据不可读"
        if not rows and not local_ok:
            # 两边都读不到：不能说「0 张」，那是把缺数据说成零
            raise ProviderError(note, hint="daemon 起来后再刷新；云端收据需要登录节点")
        if not rows and not note:
            note = "还没有收据"
        return {"rows": rows, "origin": origin, "note": note, "next_cursor": next_cursor}

    def _earnings(self) -> Mapping[str, Any]:
        status = self._safe("status", self._status)
        data = status if isinstance(status, Mapping) else {}
        balance = data.get("balance")
        today = data.get("today_earnings")
        try:
            payload = self._cloud_call("earnings")
        except ProviderError as exc:
            if balance is None and today is None:
                raise ProviderError(exc.reason, hint="完成后台结算并登录节点后重试") from None
            return {"balance": balance, "today": today, "currency": "USDT",
                    "pending": None, "settled": None, "payouts": [],
                    "online": False, "note": "结算明细暂不可用：" + exc.reason}
        rows = payload if isinstance(payload, Mapping) else {}
        # 客户端已把 *_minor（micro-USD）归一化成「至少 2 位、最多 6 位」文本；
        # 这里只补「本地 status 有、云端没有」的字段（例如余额额度），不反向覆盖云端值。
        def pick(key: str, fallback: Any) -> Any:
            value = rows.get(key)
            return fallback if value is None else value

        return {"balance": pick("balance", balance), "today": pick("today", today),
                "currency": str(rows.get("currency") or "USDT"),
                "pending": rows.get("pending"), "settled": rows.get("settled"),
                "corrected": rows.get("corrected"),
                "settled_minor": rows.get("settled_minor"),
                "pending_minor": rows.get("pending_minor"),
                "next_settlement_at": rows.get("next_settlement_at"),
                "payouts": [row for row in (rows.get("payouts") or []) if isinstance(row, Mapping)],
                "online": True, "note": str(rows.get("note") or "")}

    def _agent(self) -> Mapping[str, Any]:
        config = self._access.load()
        return {
            "upstream": {"openai": config.openai_base_url,
                         "anthropic": config.anthropic_base_url},
            "downlink": {"url": config.agent_downlink_url,
                         "token": mask_secret(config.scoped_token)},
            "note": "下行令牌只读展示；轮换请到服务端控制台。",
        }

    def _access_section(self) -> Mapping[str, Any]:
        config = self._access.load()
        binding: Mapping[str, Any] | None = None
        note = ""
        try:
            node_id = config.node_id
            if node_id:
                binding = self._cloud_call("binding", node_id)
        except ProviderError as exc:
            note = "云端绑定状态暂不可用：" + exc.reason
        # 文件不存在 ≠ 通过校验：没保存过就说没保存过，避免「通过校验」误导
        saved = bool(getattr(self._access, "path", None)
                     and Path(self._access.path).exists())
        return {"config": config.display(), "valid": _valid(config) if saved else None,
                "saved": saved, "binding": binding,
                "note": note, "path": str(self._access.path)}

    def _yield(self) -> Mapping[str, Any]:
        status = self._safe("status", self._status)
        data = status if isinstance(status, Mapping) else {}
        mode = "unknown"
        try:
            store = self._store()
            stored = store.get_kv("yield_mode")
            if isinstance(stored, str) and stored:
                mode = stored
        except Exception:
            pass
        return {"state": data.get("state") or "unknown",
                "reason": data.get("reason"),
                "reason_label": data.get("reason_label"),
                "accepting": data.get("accepting"),
                "mode": mode,
                "yield_enabled": data.get("yield_enabled")}

    def _account(self) -> Mapping[str, Any]:
        config = self._access.load()
        profile: Mapping[str, Any] | None = None
        note = ""
        try:
            profile = self._cloud_call("profile")
        except ProviderError as exc:
            note = "账户资料暂不可用：" + exc.reason
        security = {}
        try:
            from ..secure import CredentialStore
            from ..config import Paths
            credentials = CredentialStore(Paths(str(self._home)))
            security = {"node_id": credentials.node_id() or config.node_id or None,
                        "storage": str(credentials.describe().get("storage") or "")}
        except Exception as exc:
            note = (note + "；" if note else "") + "本机凭据状态不可读（" + type(exc).__name__ + "）"
        if profile is None and not security:
            raise ProviderError("节点资料未接线", hint="登录后重试")
        return {"profile": profile, "security": security, "note": note}

    def _logs_section(self) -> Mapping[str, Any]:
        rows = self._logs.tail(limit=200)
        return {"rows": rows, "path": str(self._logs.path),
                "levels": list(LOG_LEVELS), "note": ""}


def _more_note(pages: Mapping[str, Any]) -> str:
    """翻到上限还有游标就如实说「还有更多、本轮没拉全」，不当场承诺刷新就能续上。"""
    if pages.get("truncated") and pages.get("next_cursor"):
        count = len(pages.get("rows") or [])
        return "已显示前 " + str(count) + " 条，还有更多（本轮未全部拉取）"
    return ""


def _valid(config: AccessConfig) -> bool:
    try:
        config.validate()
    except ValueError:
        return False
    return True


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("items", "rows", "data", "models", "receipts", "tasks"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
        return []
    if isinstance(payload, (list, tuple)):
        return [row for row in payload if isinstance(row, Mapping)]
    return []


# ─────────────────────────────────────────────────────────────── 假实现
class NullDataSource:
    """全部不可用：验证「无网络 / 无 GPU / 无权限」时 UI 不抛栈、不空白。"""

    def __init__(self, reason: str = "测试注入：无数据源") -> None:
        self.reason = reason

    def snapshot(self, *, sections: Sequence[str] | None = None) -> Snapshot:
        wanted = tuple(sections) if sections else SECTIONS
        return Snapshot(sections={name: Unavailable(self.reason) for name in wanted},
                        fetched_at="", source="null")


class FakeDataSource:
    """测试注入：给定每个数据域的值；未给的域返回「暂不可用（测试未提供）」。"""

    def __init__(self, sections: Mapping[str, Any] | None = None, *,
                 fetched_at: str = "2026-11-01T12:00:00+0800", source: str = "fake") -> None:
        self._sections = dict(sections or {})
        self._fetched_at = fetched_at
        self._source = source
        self.calls = 0

    def snapshot(self, *, sections: Sequence[str] | None = None) -> Snapshot:
        self.calls += 1
        wanted = tuple(sections) if sections else SECTIONS
        collected: dict[str, Any] = {}
        for name in wanted:
            if name in self._sections:
                collected[name] = self._sections[name]
            else:
                collected[name] = Unavailable("测试未提供 " + name)
        return Snapshot(sections=collected, fetched_at=self._fetched_at, source=self._source)
