# SPDX-License-Identifier: Apache-2.0
"""进程内调用"只读"CLI 的最小通道（S1/S2 的进程数瘦身，**默认关闭**）。

为什么需要
==========

冻结包（PyInstaller onefile）里 ``[sys.executable, "-m", "djb_node", …]`` 每次都是一次
**完整冷启动**：真机实测 ``status --json`` 中位 **1611 ms**、``model-sync status`` **1553 ms**。
桌面端一轮刷新要取 ``status`` + ``model-sync status``，光冷启动就 3 秒以上；
窗口启动 + 每次刷新都被它拖住（S2「点了没反应」的一半是它）。

拿掉这 2 次进程的最省事办法是**进程内调用同一个 ``cli.main``**：语义、参数解析、错误
文案、退出码全部复用，不重新实现一套取数逻辑（重新实现就会和 CLI 分叉）。

为什么默认关闭
==============

进程内调用把「子进程的全局状态隔离」换成了「同一进程的全局状态」：``sys.stdout`` 捕获、
sqlite 连接、日志 handler、``os.environ`` 都是共享的。风险不高但**不是零**，所以按
改动纪律做成开关，默认 off；打开后若发现任何异常，删掉环境变量即回到子进程路径。

特性开关三字段（改动纪律硬要求）
================================

* **owner**：``win-client``（DJB 客户端线）
* **expire_at**：``2026-12-31``（到期前必须给出「转正 / 删除」结论，不许无声常驻）
* **rollback_rule**：把 ``DJB_INPROC_CLI`` 删掉或设为 ``0`` —— 立刻回到子进程路径，
  不需要回滚代码、不需要重新发版（环境变量读取点在每次调用时）
* **默认**：关（未设置 = 关）

安全边界
========

* **只读白名单**：只有 :data:`READ_ONLY_COMMANDS` 里的命令允许进程内跑。会写配置 /
  起进程 / 改状态的命令（``init`` / ``start`` / ``stop`` / ``auth`` …）**一律**走子进程，
  副作用必须隔离在子进程里。
* 任何异常（import 失败 / 捕获失败 / 非 JSON）→ 返回 ``None``，调用方自动回落到子进程：
  **进程内只是加速路径，不是唯一路径**。
* 进程内调用串行化（模块级锁）：``redirect_stdout`` 改的是全局 ``sys.stdout``，两个线程
  同时进会把输出搅在一起。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import threading
from typing import Any, Mapping, Sequence

__all__ = ["FLAG", "FEATURE_SWITCH", "READ_ONLY_COMMANDS", "enabled", "allowed", "call"]

#: 打开进程内只读 CLI 的环境变量（``1/true/on/yes`` = 开）
FLAG = "DJB_INPROC_CLI"

#: 开关元数据（owner / expire_at / rollback_rule + 默认值）——改动纪律要求的三字段
FEATURE_SWITCH: Mapping[str, str] = {
    "name": FLAG,
    "owner": "win-client",
    "expire_at": "2026-12-31",
    "rollback_rule": "删除环境变量 DJB_INPROC_CLI（或设为 0）即回到子进程路径，无需改代码",
    "default": "off",
}

#: 允许进程内执行的**只读**命令前缀（元组比较；不做前缀模糊匹配，避免误放行）
READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("status",),
    ("model-sync", "status"),
)

#: ``redirect_stdout`` 是进程级副作用：串行化，别让两个线程搅在一起
_LOCK = threading.Lock()


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """开关是否打开（每次现读环境变量：回滚不需要重启进程…… 但要下一次调用才生效）。"""
    env = os.environ if environ is None else environ
    return str(env.get(FLAG) or "").strip().lower() in ("1", "true", "on", "yes")


def allowed(argv: Sequence[str]) -> bool:
    """这条命令是否在白名单里（只读）。"""
    words = tuple(str(item) for item in argv if not str(item).startswith("-"))
    for prefix in READ_ONLY_COMMANDS:
        if words[:len(prefix)] == prefix:
            return True
    return False


def call(argv: Sequence[str], *, home: str | os.PathLike | None = None,
         environ: Mapping[str, str] | None = None) -> tuple[int, str, str] | None:
    """进程内跑一次只读 CLI。

    返回 ``(returncode, stdout, stderr)``（与 ``subprocess.run`` 的形状一致，调用方的解析 /
    错误文案一字不改）；**不该走进程内 / 走不通**时返回 ``None``，调用方按原路起子进程。
    """
    if not enabled(environ):
        return None
    words = [str(item) for item in argv]
    if not allowed(words):
        return None
    if home is None:
        return None                       # 没有明确 home 就不要在进程内猜（读到别的库）
    if any(item == "--home" or item.startswith("--home=") for item in words):
        return None                       # home 只由本函数显式钉住，调用方不许自带
    from .. import cli                    # 延迟 import：CLI 很重，桌面端只在需要时才拉

    args = ["--home", os.fspath(home), *words]
    if "--json" not in args:
        args.append("--json")
    out, err = io.StringIO(), io.StringIO()
    try:
        with _LOCK:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = int(cli.main(args) or 0)
    except BaseException:                 # noqa: BLE001 - 任何异常都回落子进程，绝不炸 UI
        return None
    return code, out.getvalue(), err.getvalue()


def parse_json_object(text: str | None) -> Mapping[str, Any] | None:
    """``call()`` 的 stdout → Mapping；不是 JSON 对象就 ``None``（调用方报「输出不是 JSON」）。"""
    try:
        payload = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None
