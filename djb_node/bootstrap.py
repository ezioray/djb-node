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

from . import facts, pidfile
from .config import (GATEWAY_PLACEHOLDER, Paths, load_config, resolve_home,
                     save_config)
from .platform.no_window import subprocess_window_kwargs
from .store import Store

__all__ = ["DEFAULT_GATEWAY_URL", "default_gateway", "is_placeholder",
           "ensure_ready", "start_daemon", "control_ping", "disabled_by_env"]

#: 一键关掉自举（打包自检 / 机房批量部署 / 离线环境）：``DJB_BOOTSTRAP_DISABLE=1``
DISABLE_ENV = "DJB_BOOTSTRAP_DISABLE"


def disabled_by_env() -> bool:
    return (os.environ.get(DISABLE_ENV) or "").strip().lower() in ("1", "true", "on", "yes")

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
    if not enabled or disabled_by_env():
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


def _live_daemon_record(paths: Any) -> dict[str, Any] | None:
    """pid 文件里是否有一个**可验证**的 daemon（控制面还没起来时用它认人）。

    判据与 ``djb stop --force`` 完全一致（``pidfile.verify``）：宁可多起一次，
    也不许因为一条陈旧/损坏的记录把启动卡死。
    """
    if not facts.single_instance_enabled():
        return None
    try:
        record = pidfile.read(paths)
    except Exception:                                  # pragma: no cover - 文件不可读
        return None
    if not record:
        return None
    try:
        ok, _reason = pidfile.verify(record, paths)
    except Exception:                                  # pragma: no cover - 探针不可用
        return None
    return record if ok else None



#: 启动闸门：同一 home 同时只允许一个进程走「探测 → 拉起 → 等应答」。
SPAWN_LOCK_STALE_S = 20.0


def _spawn_lock(paths: Any) -> str | None:
    """拿 runtime/spawn.lock；拿到返回路径，拿不到返回 None（有别人在启动）。

    跨进程互斥用 ``O_CREAT|O_EXCL``：桌面端重复点击、桌面端 + 命令行同时起，
    都只放一个过去。锁超过 :data:`SPAWN_LOCK_STALE_S` 视为残留（进程崩了没删），
    直接抢过来——宁可多起一次，也不能因为一个残锁让节点永远起不来。
    """
    try:
        os.makedirs(paths.runtime_dir, mode=0o700, exist_ok=True)
    except OSError:
        return None
    lock_path = os.path.join(paths.runtime_dir, "spawn.lock")
    for attempt in (1, 2):
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = SPAWN_LOCK_STALE_S + 1.0
            if attempt == 1 and age > SPAWN_LOCK_STALE_S:
                try:
                    os.unlink(lock_path)          # 残留锁：抢过来（下一轮重试）
                except OSError:
                    return None
                continue
            return None
        except OSError:
            return None
        try:
            os.write(handle, (str(os.getpid()) + "\n").encode())
        except OSError:
            pass
        finally:
            os.close(handle)
        return lock_path
    return None


def _spawn_unlock(lock_path: str | None) -> None:
    if not lock_path:
        return
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def start_daemon(home: str | os.PathLike | None = None, *,
                 enabled: bool = True,
                 wait_seconds: float = 8.0) -> dict[str, Any]:
    """daemon 没在跑就后台拉起（幂等）；返回可展示的结果，**不抛异常**。"""
    paths = Paths(resolve_home(None if home is None else str(home)))
    result: dict[str, Any] = {"enabled": bool(enabled), "started": False,
                              "reason": "", "errors": []}
    if not enabled or disabled_by_env():
        return result
    try:
        if control_ping(paths):
            result["reason"] = "already-running"
            return result
        # F36（2026-11-24 真机）：一次桌面启动会在 2 秒内拉起**两个** daemon
        # （04:18:36 / 04:18:38 各一个），后一个才因单实例检查退出。两个进程都会写
        # pid 文件、都会试着轮换 client_key——这段时间是竞态窗口。
        # 用 runtime/spawn.lock 做「只许一个启动者」的闸门（跨进程 O_EXCL）。
        lock = _spawn_lock(paths)
        if lock is None:
            result["reason"] = "spawning"
            return result
        try:
            record = _live_daemon_record(paths)
            if record is not None:
                # 控制面还没应答，但 pid 文件里那个进程**验得过**（pid 活 + 令牌一致 +
                # 命令行是本 daemon + 同一个 home）：再起一个只会互相轮换 client_key。
                result["reason"] = "already-running"
                result["pid"] = int(record.get("pid") or 0)
                return result
            config = load_config(paths)
            command = _daemon_command(paths, config)
            if command is None:
                result["reason"] = "no-cli"
                return result
            log_path = os.path.join(paths.logs_dir, facts.DAEMON_OUT_NAME)
            result["log"] = log_path
            os.makedirs(paths.logs_dir, mode=0o700, exist_ok=True)
            with open(log_path, "ab") as stdout:
                subprocess.Popen(command, stdout=stdout, stderr=stdout,
                                 stdin=subprocess.DEVNULL, start_new_session=True,
                                 close_fds=True, **subprocess_window_kwargs())
            deadline = time.monotonic() + max(0.0, float(wait_seconds))
            while time.monotonic() < deadline:
                if control_ping(paths):
                    result["started"] = True
                    result["reason"] = "started"
                    return result
                time.sleep(0.25)
            result["reason"] = "timeout"
            result["log"] = log_path
        finally:
            _spawn_unlock(lock)
    except Exception as exc:                      # noqa: BLE001
        result["errors"].append(type(exc).__name__ + ": " + str(exc)[:160])
    return result


#: daemon 启动失败的**客户可执行**文案（旧实现只处理 started/already-running/errors，
#: 而 ``no-cli`` / ``timeout`` 两种最常见失败不产出任何字 → 用户只看到「先启动一次」）。
DAEMON_REASON_TEXT = {
    "no-cli": "节点没启动：同目录缺少 djb-node.exe（请从官方压缩包**整体解压**，"
              "只留桌面端一个 exe 拉不起节点）",
    "spawning": "节点正在启动（另一个启动请求刚发出去），稍等几秒点「刷新」",
    "timeout": "节点启动超时：8 秒内没连上本机控制口",
}


def daemon_reason_text(reason: str, log: str = "") -> str:
    """reason → 一句可执行文案（没有对应文案就返回空串，绝不编原因）。"""
    text = DAEMON_REASON_TEXT.get(str(reason or ""), "")
    if text and str(reason) == "timeout":
        text += "，看日志 " + (log or facts.DAEMON_OUT_NAME) + " 末尾的报错"
    return text


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
        elif daemon.get("reason") == "spawning":
            parts.append("节点正在启动（已有一个启动请求，稍等几秒）")
        elif daemon.get("errors"):
            parts.append("节点启动失败：" + daemon["errors"][0])
        else:
            # no-cli / timeout / 其它：**必须**有字，否则故障是静默的（S3 根因之一）
            hint = daemon_reason_text(str(daemon.get("reason") or ""),
                                      str(daemon.get("log") or ""))
            if hint:
                parts.append(hint)
    return "；".join(parts)
