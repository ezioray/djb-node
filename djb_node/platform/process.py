# SPDX-License-Identifier: Apache-2.0
"""进程身份探测：给 pid 文件提供 **(pid, 启动时刻)** 双键，防 PID 复用误杀。

为什么需要
----------
``djb stop --force`` 要杀一个"可能已经挂死"的 daemon。pid 会被操作系统复用：
如果只记 pid，用户在一个「daemon 已退出、pid 被别的进程占用」的时刻执行强杀，
被杀的就是**不相干的进程**（2026-11-05 清 34 个测试残留时，正是靠 `(pid, created_ms)`
双键才没杀到同表的 hermes / gpu-broker）。

所以：pid 文件记不光是 pid，还有该进程的**启动时刻令牌**；强杀前重新读一次令牌比对，
不一致 ⇒ 拒绝。读不到 ⇒ 也拒绝（fail-closed，不猜）。

令牌只在本机同一开机周期内可比：
- Linux：``boot_id`` + ``/proc/<pid>/stat`` 第 22 字段（开机以来的 clock ticks）
- Windows：``GetProcessTimes`` 的创建时间（FILETIME，100ns）
- 其它（macOS）：``ps -o lstart=``
"""
from __future__ import annotations

import os
import re
import subprocess

from .no_window import subprocess_window_kwargs

WINDOWS = os.name == "nt"

#: pid 文件五元组里用来判「这确实是个 djb daemon」的标记
DAEMON_MARKER = "djb_node.daemon"

__all__ = ["DAEMON_MARKER", "start_token", "cmdline", "is_alive", "home_from_cmdline"]


# ── 启动时刻 ────────────────────────────────────────────────────────────────
def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:                                            # pragma: no cover - 老内核/容器
        return ""


def _linux_start_ticks(pid: int) -> int | None:
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    # comm 字段（第 2 项）可以用任意字符包括空格和括号，所以从**最后一个** ')' 之后切
    cut = data.rfind(b")")
    if cut < 0:
        return None
    fields = data[cut + 2:].split()
    # fields[0] 是 state（stat 的第 3 字段）⇒ 第 22 字段在 fields[19]
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _windows_creation(pid: int) -> int | None:                 # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wt

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ok = kernel32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created),
                                      ctypes.byref(exited), ctypes.byref(kernel),
                                      ctypes.byref(user))
        if not ok:
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def start_token(pid: int) -> str | None:
    """该进程的启动时刻令牌；读不到返回 ``None``（调用方必须按"不可信"处理）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if WINDOWS:                                                # pragma: no cover - Windows only
        value = _windows_creation(pid)
        return None if value is None else "win:" + str(value)
    if os.path.isdir("/proc"):
        ticks = _linux_start_ticks(pid)
        if ticks is None:
            return None
        return "linux:" + _boot_id() + ":" + str(ticks)
    try:                                                       # pragma: no cover - macOS/BSD
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, timeout=10,
                             **subprocess_window_kwargs()).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):              # pragma: no cover
        return None
    text = out.strip()
    return ("ps:" + text) if text else None


# ── 命令行 ──────────────────────────────────────────────────────────────────
def _windows_cmdline(pid: int) -> str | None:                  # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wt

    class PBI(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_void_p),
                    ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010

    def read_ptr(handle, address):
        buf = ctypes.c_void_p()
        read = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), ctypes.byref(buf),
                                          ctypes.sizeof(buf), ctypes.byref(read)):
            raise OSError("ReadProcessMemory")
        return buf.value

    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        info = PBI()
        size = ctypes.c_size_t()
        if ntdll.NtQueryInformationProcess(ctypes.c_void_p(handle), 0, ctypes.byref(info),
                                           ctypes.sizeof(info), ctypes.byref(size)) != 0:
            return None
        if not info.PebBaseAddress:
            return None
        params = read_ptr(handle, info.PebBaseAddress + 0x20)     # ProcessParameters
        if not params:
            return None
        header = (ctypes.c_ubyte * 16)()                           # UNICODE_STRING CommandLine
        read = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(params + 0x70), header, 16,
                                          ctypes.byref(read)):
            return None
        length = int.from_bytes(bytes(header[0:2]), "little")
        if length <= 0 or length > 32767 or length % 2:
            return None
        buffer_addr = int.from_bytes(bytes(header[8:16]), "little")
        if not buffer_addr:
            return None
        raw = (ctypes.c_ubyte * length)()
        if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(buffer_addr), raw, length,
                                          ctypes.byref(read)):
            return None
        return bytes(raw).decode("utf-16-le", "replace")
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def cmdline(pid: int) -> str | None:
    """该进程的完整命令行；读不到返回 ``None``（调用方必须按"不可信"处理）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if WINDOWS:                                                # pragma: no cover - Windows only
        return _windows_cmdline(pid)
    if os.path.isdir("/proc"):
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    try:                                                       # pragma: no cover - macOS/BSD
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, timeout=10,
                             **subprocess_window_kwargs()).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):              # pragma: no cover
        return None
    return out.strip() or None


def is_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:                                                # pragma: no cover - Windows only
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x00000102
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                                    # 存在但不属于我们
        return True
    except OSError:                                            # pragma: no cover
        return False
    return True


# ── 从命令行里取 --home ─────────────────────────────────────────────────────
_HOME_RE = re.compile(r"""--home(?:=|\s+)(?:"([^"]*)"|'([^']*)'|(\S+))""")


def home_from_cmdline(text: str | None) -> str | None:
    """从命令行里解析 ``--home``；解析不到返回 ``None``。"""
    if not text:
        return None
    match = _HOME_RE.search(text)
    if not match:
        return None
    return match.group(1) or match.group(2) or match.group(3)
