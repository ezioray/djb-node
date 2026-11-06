# SPDX-License-Identifier: Apache-2.0
"""首启自举：把「装完还要手敲 3 条命令」变成「装完就能连上」。

背景（2026-11-06 外部测试客户实测）
====================================
客户装好客户端、双击打开，界面只显示两行红字：

* ``云端未接通``  —— 默认云端地址还是**占位符** ``https://api.example.invalid``
  （:data:`djb_node.config.GATEWAY_PLACEHOLDER`），没人会去改 `config.json`；
* ``本机未初始化`` —— 没跑过 ``djb init``，而**桌面端没有初始化入口**。

结果：客户端发出去就是死的，必须由我们口头/文档教三条命令才能用。

本模块负责三件事（全部幂等、失败不抛给 UI）
============================================
1. :func:`ensure_ready` —— 首次启动就地把本机数据目录建好、写配置（真实默认地址）；
   已有配置里若是占位符，**顺手修掉**（这正是卡住存量客户的那一行）。
2. :func:`is_placeholder` —— 判断"这地址是不是还没配过"。
3. :func:`start_daemon` —— 后台拉起 daemon（daemon 自己在启动时完成节点注册与取密钥），
   于是"未初始化/未接通"两个状态都不再需要人工介入。

设计约束
========
* **只用 stdlib + 本包**，不引新依赖；
* **不联网、不写凭据**：注册/配对仍由 daemon 走它原本的路径；
* **可关**：调用方（桌面/CLI）可传 ``enabled=False``，测试与离线环境不受影响；
* **不吞错**：返回值里带 ``errors`` 列表，UI 可以照实显示"自动初始化失败：…"。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Sequence

from . import facts
from .config import (GATEWAY_PLACEHOLDER, Paths, load_config, resolve_home,
                     save_config)
from .store import Store

__all__ = ["DEFAULT_GATEWAY_URL", "default_gateway", "is_placeholder",
           "ensure_ready", "start_daemon", "control_ping"]

#: 构建期可用 ``DJB_DEFAULT_GATEWAY`` 覆盖（自建部署 / 预发环境）
DEFAULT_GATEWAY_URL = (os.environ.get("DJB_DEFAULT_GATEWAY") or "").strip() \
    or "https://tkbank.ai"

#: ``ensure_ready`` 默认预设：省心（并发 2，客户机优先低负载；用户可随时在界面改）
DEFAULT_PRESET = "省心"


def default_gateway() -> str:
    return DEFAULT_GATEWAY_URL


def is_placeholder(url: str | None) -> bool:
    """地址是否"从没配过"（空 / 占位符 / example.invalid 系）。"""
    text = (url or "").strip().lower()
    if not text:
        return True
    if text == GATEWAY_PLACEHOLDER.lower():
        return True
    return "example.invalid" in text


def ensure_ready(home: str | os.PathLike | None = None, *,
                 gateway: str | None = None,
                 preset: str | None = None,
                 enabled: bool = True) -> dict[str, Any]:
    """幂等自举本机数据目录；返回一份可展示的结果（**不抛异常**）。

    返回值::

        {"enabled": True, "home": "...", "created": bool, "gateway_fixed": bool,
         "gateway": "https://tkbank.ai", "preset": "均衡", "errors": []}
    """
    paths = Paths(resolve_home(None if home is None else str(home)))
    result: dict[str, Any] = {"enabled": bool(enabled), "home": paths.home,
                              "created": False, "gateway_fixed": False,
                              "gateway": "", "preset": "", "errors": []}
    if not enabled:
        return result
    try:
        target = (gateway or default_gateway()).strip()
        existed = os.path.exists(paths.config_file)
        config = load_config(paths)
        changed = False
        if not existed:
            config.gateway_url = target
            changed = True
        elif is_placeholder(config.gateway_url):
            # 存量客户就卡在这一行：装完地址还是 api.example.invalid
            config.gateway_url = target
            changed = True
            result["gateway_fixed"] = True
        if gateway and config.gateway_url != target:
            config.gateway_url = target
            changed = True
        config.validate()
        paths.prepare()
        if changed or not existed:
            save_config(paths, config)
        result["created"] = not existed
        result["gateway"] = config.gateway_url
        # 建库（等价于 init 的最后一步）：建好即可读写 profile/schedule
        store = Store.create(paths.db_file, channel=config.update_channel)
        profile = store.profile()
        chosen = preset if preset in facts.PRESETS else DEFAULT_PRESET
        if not existed:
            # 新建的库显式落一次预设，别依赖 store 的默认值（默认就是「省心」，但要可追溯）
            profile.preset = chosen
            result["preset"] = chosen
        else:
            result["preset"] = getattr(profile, "preset", "") or ""
    except Exception as exc:                      # noqa: BLE001 - 自举失败不许炸 UI
        result["errors"].append(type(exc).__name__ + ": " + str(exc)[:160])
    return result


def control_ping(paths: Paths, *, timeout: float = 1.0) -> bool:
    """daemon 是否在跑（控制口能 ping 通）。"""
    try:
        from .daemon import ControlClient
        response = ControlClient(paths).call({"cmd": "ping"}, timeout=timeout)
        return bool(response.get("ok"))
    except Exception:                             # noqa: BLE001 - 没跑/管道不在都算 False
        return False


def _daemon_command(paths: Paths, config: Any) -> list[str] | None:
    """拼 daemon 启动命令：优先同目录的 ``djb-node`` 可执行文件（打包场景）。

    冻结后 ``sys.executable`` 是 **桌面端自己**，拿它拼 ``-m djb_node.daemon`` 会把
    桌面端再拉起一次（无限窗口），所以必须优先找兄弟可执行文件。
    """
    here = os.path.dirname(os.path.abspath(sys.executable))
    names = ("djb-node.exe",) if os.name == "nt" else ("djb-node",)
    for name in names:
        candidate = os.path.join(here, name)
        if os.path.isfile(candidate):
            return [candidate, "start", "--home", str(paths.home)]
    if getattr(sys, "frozen", False):
        # 冻结且没有兄弟 CLI：不要乱起进程，交给 UI 提示
        return None
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-m", "djb_node.daemon", "--home", str(paths.home),
            "--port", str(getattr(config, "mirror_port", facts.MIRROR_PORT))]


def start_daemon(home: str | os.PathLike | None = None, *,
                 enabled: bool = True,
                 wait_seconds: float = 8.0) -> dict[str, Any]:
    """daemon 没在跑就后台拉起（幂等）；返回可展示的结果，**不抛异常**。"""
    paths = Paths(resolve_home(None if home is None else str(home)))
    result: dict[str, Any] = {"enabled": bool(enabled), "started": False,
                              "reason": "", "errors": []}
    if not enabled:
        return result
    try:
        if control_ping(paths):
            result["reason"] = "already-running"
            return result
        config = load_config(paths)
        command = _daemon_command(paths, config)
        if command is None:
            result["reason"] = "no-cli"
            return result
        log_path = os.path.join(paths.logs_dir, "daemon.out")
        os.makedirs(paths.logs_dir, mode=0o700, exist_ok=True)
        with open(log_path, "ab") as stdout:
            subprocess.Popen(command, stdout=stdout, stderr=stdout,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while time.monotonic() < deadline:
            if control_ping(paths):
                result["started"] = True
                result["reason"] = "started"
                return result
            time.sleep(0.25)
        result["reason"] = "timeout"
    except Exception as exc:                      # noqa: BLE001
        result["errors"].append(type(exc).__name__ + ": " + str(exc)[:160])
    return result


def summary_text(boot: dict[str, Any], daemon: dict[str, Any] | None = None) -> str:
    """一句话状态（状态栏/日志用），不暴露内部细节。"""
    parts: list[str] = []
    if boot.get("errors"):
        parts.append("自动初始化失败：" + boot["errors"][0])
    elif boot.get("created"):
        parts.append("已自动初始化本机数据目录")
    elif boot.get("gateway_fixed"):
        parts.append("已修正云端地址为 " + str(boot.get("gateway")))
    if daemon is not None:
        if daemon.get("started"):
            parts.append("节点已启动")
        elif daemon.get("reason") == "already-running":
            parts.append("节点已在运行")
        elif daemon.get("errors"):
            parts.append("节点启动失败：" + daemon["errors"][0])
    return "；".join(parts)
