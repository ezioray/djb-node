# SPDX-License-Identifier: Apache-2.0
"""按**进程树**回收本测试进程自己 spawn 出来的 daemon。

为什么需要它（2026-11-05 实测，task-8）
--------------------------------------
`djb start` 内部用 ``subprocess.Popen(...)`` 拉起 daemon 后**丢掉了 Popen 对象**
（``djb_node/cli.py:_spawn_daemon``），而 ``paths.pid_file`` 在 node 仓里**只被读、从不被写**
（全仓 grep：只有 ``cli.py`` 的 ``_read_pid`` 读它）。所以测试侧拿不到官方 pid 句柄：

* ``djb start --json`` 成功时 payload 里有 ``pid``（来自控制口 ping），但那是**内层**真实
  python 的 pid；Windows 上 venv 的 ``Scripts\\python.exe`` 是个启动器，会再 CreateProcess
  一个 base 解释器当**子进程**，于是真实形态是两层：
  ``外层 venv 启动器 -> 内层 python311 -m djb_node.daemon``。
  只杀内层会留下外层；只杀外层会留下内层（内层才是真正接单的 daemon）。
* ``djb start`` **失败**时（Windows 上 "控制不可用" 会超时返回 4）连 pid 都拿不到。

因此这里的口径是：**从 pytest 自己的 pid 往下走进程树**，只认「是我后代」且「命令行同时含
``djb_node.daemon`` 和本用例独有的 ``--home`` 路径」的进程，再按**树顶**整体回收。

安全边界（硬要求，勿放宽）
--------------------------
1. 只处理 ``os.getpid()`` 的**后代**；``os.getpid()`` 本身及其祖先永不触碰。
2. 必须是 ``djb_node.daemon`` **且** ``--home`` 精确等于本用例的 home —— 同机上的
   ``hermes.exe`` / ``gpu-broker`` 这类业务进程不可能命中。
3. 禁止任何通配/按名批量（不许 ``Stop-Process`` 通配、不许 ``taskkill /IM``）。
4. 幂等：已经退出的 pid 直接跳过；清理过程不抛异常（teardown 里必须安静）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

__all__ = [
    "descendants",
    "daemon_pids_for_home",
    "kill_daemon_tree",
    "reap_daemon_tree",
    "wait_gone",
]

WINDOWS = sys.platform.startswith("win")
_DAEMON_MARK = "djb_node.daemon"
# 执行环境的 PATH 可能被裁剪到没有 System32（实测 win 分析机的作业上下文就是如此），
# 所以优先用绝对路径，找不到再退回 PATH 查找。
_TASKKILL = next((path for path in (r"C:\Windows\System32\taskkill.exe",
                                    r"C:\Windows\SysWOW64\taskkill.exe")
                  if os.path.exists(path)), "taskkill")


# ── 进程表读取 ──────────────────────────────────────────────────────────────
def _table_posix() -> dict[int, tuple[int, str]]:
    """{pid: (ppid, cmdline)}；/proc 不存在时退回 ps。"""
    table: dict[int, tuple[int, str]] = {}
    if os.path.isdir("/proc"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open("/proc/%d/stat" % pid, "rb") as handle:
                    fields = handle.read().rsplit(b")", 1)[-1].split()
                ppid = int(fields[1])
                with open("/proc/%d/cmdline" % pid, "rb") as handle:
                    cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            except (OSError, IndexError, ValueError):
                continue
            table[pid] = (ppid, cmdline)
        return table
    try:                                            # macOS 等无 /proc 的平台
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                             capture_output=True, timeout=20).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):   # pragma: no cover
        return table
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def _table_windows() -> dict[int, tuple[int, str]]:  # pragma: no cover - Windows only
    """{pid: (ppid, cmdline)}，用 toolhelp32 取 PPID、读 PEB 取命令行（纯 stdlib）。"""
    import ctypes
    import ctypes.wintypes as wt

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    class PBI(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_void_p),
                    ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    TH32CS_SNAPPROCESS = 0x00000002

    def read_ptr(handle, address):
        buf = ctypes.c_void_p()
        read = ctypes.c_size_t()
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(address), ctypes.byref(buf),
                                    ctypes.sizeof(buf), ctypes.byref(read)):
            raise OSError("ReadProcessMemory")
        return buf.value

    def read_unicode_string(handle, address):
        header = (ctypes.c_ubyte * 16)()
        read = ctypes.c_size_t()
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(address), header, 16, ctypes.byref(read)):
            raise OSError("ReadProcessMemory")
        length = int.from_bytes(bytes(header[0:2]), "little")
        if length <= 0 or length > 32767 or length % 2:
            raise OSError("bad UNICODE_STRING length")
        buffer_addr = int.from_bytes(bytes(header[8:16]), "little")
        if not buffer_addr:
            raise OSError("null buffer")
        raw = (ctypes.c_ubyte * length)()
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(buffer_addr), raw, length,
                                     ctypes.byref(read)):
            raise OSError("ReadProcessMemory")
        return bytes(raw).decode("utf-16-le", "replace")

    def cmdline_of(pid):
        handle = k32.OpenProcess(0x0400 | 0x0010, False, pid)   # QUERY_INFORMATION | VM_READ
        if not handle:
            return ""
        try:
            info = PBI()
            size = ctypes.c_size_t()
            if ntdll.NtQueryInformationProcess(ctypes.c_void_p(handle), 0, ctypes.byref(info),
                                               ctypes.sizeof(info), ctypes.byref(size)) != 0:
                return ""
            if not info.PebBaseAddress:
                return ""
            params = read_ptr(handle, info.PebBaseAddress + 0x20)
            if not params:
                return ""
            return read_unicode_string(handle, params + 0x70)    # CommandLine
        except OSError:
            return ""
        finally:
            k32.CloseHandle(handle)

    table: dict[int, tuple[int, str]] = {}
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:                                          # pragma: no cover
        return table
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            table[pid] = (int(entry.th32ParentProcessID), cmdline_of(pid))
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return table


def _table() -> dict[int, tuple[int, str]]:
    return _table_windows() if WINDOWS else _table_posix()


# ── 查询 ────────────────────────────────────────────────────────────────────
def descendants(table: dict[int, tuple[int, str]] | None = None,
                root: int | None = None) -> dict[int, tuple[int, str]]:
    """``root``（默认本进程）的全部后代 pid -> (ppid, cmdline)。"""
    table = _table() if table is None else table
    root = os.getpid() if root is None else root
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: dict[int, tuple[int, str]] = {}
    stack = list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found[pid] = table.get(pid, (root, ""))
        stack.extend(children.get(pid, ()))
    return found


def daemon_pids_for_home(home: str, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    """本进程后代里，命令行同时含 ``djb_node.daemon`` 与 ``--home <home>`` 的 pid（可能两层）。"""
    table = _table() if table is None else table
    wanted = os.path.abspath(str(home))
    hits = []
    for pid, (_ppid, cmdline) in descendants(table).items():
        if _DAEMON_MARK not in cmdline:
            continue
        if wanted not in cmdline and wanted.replace(os.sep, "/") not in cmdline:
            continue
        hits.append(pid)
    return sorted(hits)


def wait_gone(pids, timeout: float = 10.0) -> list[int]:
    """等到这些 pid 全部消失；返回超时后仍存活的 pid。"""
    pending = {int(pid) for pid in pids}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        if WINDOWS:                                            # pragma: no cover
            alive = {pid for pid in pending if _alive_windows(pid)}
        else:
            alive = {pid for pid in pending if os.path.exists("/proc/%d" % pid)}
        pending = alive
        if pending:
            time.sleep(0.1)
    return sorted(pending)


def _alive_windows(pid: int) -> bool:                          # pragma: no cover - Windows only
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SYNCHRONIZE = 0x00100000
    handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x00000102   # WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)


# ── 回收 ────────────────────────────────────────────────────────────────────
def kill_daemon_tree(home: str, timeout: float = 10.0) -> list[int]:
    """把本用例自己 spawn 的 daemon 树整体收掉（幂等、不抛）。

    返回：清理前识别到的 pid（可能为空 = 没有残留，这是好结果）。
    """
    try:
        table = _table()
        hits = daemon_pids_for_home(home, table)
    except Exception:                                          # noqa: BLE001 - teardown 必须安静
        return []
    if not hits:
        return []
    hit_set = set(hits)
    tops = [pid for pid in hits if table.get(pid, (0, ""))[0] not in hit_set]
    if WINDOWS:                                                # pragma: no cover
        # Windows 的 taskkill /T 会连带子孙，所以只从树顶下手即可（避免重复打）
        for pid in tops or hits:
            try:
                subprocess.run([_TASKKILL, "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        # 反向（先子后父）逐个发信号：合成树/被降级的进程不一定自成进程组
        for pid in sorted(hits, reverse=True):
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None
            # 只有「自己就是组长」且「不是本进程所在的组」时才敢整组杀
            if pgid == pid and pgid != os.getpgid(0):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except OSError:
                    pass
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    leftover = wait_gone(hits, timeout=min(timeout, 5.0))
    for pid in leftover:                       # 第二遍：SIGKILL / taskkill 再来一次
        try:
            if WINDOWS:                                        # pragma: no cover
                subprocess.run([_TASKKILL, "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
    wait_gone(leftover, timeout=timeout)
    return hits


def reap_daemon_tree(home: str, timeout: float = 10.0, attempts: int = 3) -> list[int]:
    """反复清理直到没有残留；返回残留 pid（空 = 干净）。供 teardown 与回归测试共用。"""
    for _ in range(max(1, attempts)):
        kill_daemon_tree(home, timeout=timeout)
        if not daemon_pids_for_home(home):
            return []
    return daemon_pids_for_home(home)
