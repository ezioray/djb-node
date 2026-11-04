# SPDX-License-Identifier: Apache-2.0
"""``runtime/daemon.pid``：daemon 自证身份的文件，供 ``djb stop --force`` 逃生门使用。

为什么不是「一行 pid」
----------------------
pid 会被复用。只记 pid 的 pid 文件，等于把「逃生门」做成「误杀门」：

    daemon 崩了 → 用户按提示敲 `djb stop --force` → pid 已被系统分配给别人 → 杀掉无辜进程

所以文件是 **JSON 双键**：``pid`` + ``start_token``（启动时刻令牌，见
:mod:`djb_node.platform.process`），另带 ``home`` 与 ``marker`` 供强杀前二次校验。

三条纪律
--------
1. **原子写**：``util.atomic_write``（同目录 tmp + fsync + rename，0600），不出现"半截文件"窗口。
2. **校验不过就拒绝**：读不到令牌 / 令牌不符 / 命令行不含 ``djb_node.daemon`` /
   ``--home`` 不是本 paths ⇒ 一律不杀，返回原因让用户自己确认（fail-closed）。
3. **只删自己的**：`remove()` 只在文件里的 pid 与本进程一致时才删，避免误删别人的记录。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from .platform import process as probe
from .util import atomic_write

SCHEMA = 1

__all__ = ["SCHEMA", "probe", "build_record", "write", "read", "read_raw", "remove",
           "verify", "REASON"]


class REASON:
    """拒绝强杀的原因码（也给测试断言用，避免比中文字符串）。"""

    MISSING = "missing"
    LEGACY = "legacy"                 # 旧版只有一行 pid
    MALFORMED = "malformed"
    TOKEN_UNREADABLE = "token-unreadable"
    TOKEN_MISMATCH = "token-mismatch"  # PID 复用
    CMDLINE_UNREADABLE = "cmdline-unreadable"
    NOT_DAEMON = "not-daemon"
    HOME_MISMATCH = "home-mismatch"


def build_record(home: str, *, pid: int | None = None,
                 token: str | None = None, now: float | None = None) -> dict[str, Any]:
    """构造 pid 记录。令牌默认现取（``--force`` 校验时用同一套探测重新取一遍再比）。"""
    pid = os.getpid() if pid is None else int(pid)
    token = probe.start_token(pid) if token is None else token
    return {
        "schema": SCHEMA,
        "pid": pid,
        "start_token": token,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                    time.localtime(time.time() if now is None else now)),
        "home": os.path.abspath(str(home)),
        "marker": probe.DAEMON_MARKER,
    }


def write(paths: Any, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """原子写入 pid 文件；返回写入的记录。"""
    record = build_record(paths.home) if record is None else record
    atomic_write(paths.pid_file, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def read(paths: Any) -> dict[str, Any] | None:
    """读 pid 文件；不存在/不是 JSON/不是本 schema 都返回 ``None``（不抛）。"""
    try:
        with open(paths.pid_file, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SCHEMA:
        return None
    return data


def read_raw(paths: Any) -> str | None:
    """原始内容（区分"旧版一行 pid"与"完全不存在"，只用于给用户更准的提示）。"""
    try:
        with open(paths.pid_file, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def remove(paths: Any, *, pid: int | None = None) -> bool:
    """删 pid 文件；给了 ``pid`` 时只删记录属于该 pid 的文件。返回是否删了。"""
    if pid is not None:
        record = read(paths)
        if record is not None and int(record.get("pid") or 0) != int(pid):
            return False
    try:
        os.unlink(paths.pid_file)
    except OSError:
        return False
    return True


def _norm_home(value: str | None) -> str:
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def verify(record: dict[str, Any] | None, paths: Any, *,
           token_lookup: Callable[[int], str | None] | None = None,
           cmdline_lookup: Callable[[int], str | None] | None = None,
           alive_lookup: Callable[[int], bool] | None = None) -> tuple[bool, str]:
    """强杀前的校验。``(True, "")`` 才允许动手；否则 ``(False, 原因码)``。

    校验顺序（任一不过即拒）：
    1. 记录存在且是本 schema、pid 合法、令牌非空；
    2. 目标进程还活着；
    3. 目标进程**当前**的启动令牌与记录一致（PID 复用就卡在这里）；
    4. 命令行含 ``djb_node.daemon``；
    5. 命令行的 ``--home`` 与本 paths 的 home 一致。
    """
    token_lookup = probe.start_token if token_lookup is None else token_lookup
    cmdline_lookup = probe.cmdline if cmdline_lookup is None else cmdline_lookup
    alive_lookup = probe.is_alive if alive_lookup is None else alive_lookup

    if not record:
        return False, REASON.MISSING
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return False, REASON.LEGACY
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False, REASON.MALFORMED
    if pid <= 0:
        return False, REASON.MALFORMED
    recorded_token = record.get("start_token")
    if not isinstance(recorded_token, str) or not recorded_token:
        return False, REASON.MALFORMED
    if not alive_lookup(pid):
        return False, REASON.MISSING
    actual_token = token_lookup(pid)
    if not actual_token:
        return False, REASON.TOKEN_UNREADABLE
    if actual_token != recorded_token:
        return False, REASON.TOKEN_MISMATCH
    text = cmdline_lookup(pid)
    if not text:
        return False, REASON.CMDLINE_UNREADABLE
    if probe.DAEMON_MARKER not in text:
        return False, REASON.NOT_DAEMON
    home = probe.home_from_cmdline(text)
    if _norm_home(home) != _norm_home(paths.home):
        return False, REASON.HOME_MISMATCH
    return True, ""
