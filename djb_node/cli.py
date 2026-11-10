# SPDX-License-Identifier: Apache-2.0
"""djb CLI 全命令面（M15 §4.3）。

    init · env · auth · start · stop · status · pause · resume · limits ·
    upstream · announce · update · logs · diagnose · uninstall

退出码语义（facts.EXIT_CODE_HELP）：0 成功 / 1 未分类 / 2 用法 / 3 未初始化 /
4 daemon 未运行 / 5 凭据 / 6 网络 / 7 校验 / 8 状态冲突。每个命令 --help 可用。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unicodedata
from typing import Any, Callable

from . import __version__, facts, pidfile, update_source
from .api import NodeApi
from .attest import AttestationManager
from .channel import channel_enabled
from .config import (ClientConfig, Paths, feature_flag, load_config, resolve_home, save_config)
from .daemon import ControlClient, Daemon, control_socket_file
from .diagnose import DiagnoseBuilder, DiagnoseError
from .envcheck import EnvChecker
from .errors import (AuthError, DjbError, NotInstalledError, NotRunningError,
                     UsageError, exit_code_of)
from .httpclient import HttpClient
from .limits import (Limiter, YieldDetector, in_schedule, parse_schedule_windows,
                     resource_caps_valid)
from .logging_setup import get_logger, prune_old_logs, setup_logging
from .model_sync import ModelSyncClient, bundle_file, model_sync_enabled
from .notifications import NotificationStore, notifications_enabled
from .outbox import ReceiptFactory, validate_receipt
from .policy import summary as policy_summary
from .relay import RelayError, RelayManager, capacity_targets
from .secure import CredentialStore
from .state import StateMachine, Snapshot
from .store import ProfileRecord, Store, UpstreamRecord
from . import plan_router
from . import quotes as quotes_mod
from . import store as store_module
from . import tokens as tokens_mod
from .update import UpdateManager, apply_exit_code
from .upstream import (UpstreamManager, a2_prompt, parse_models, probe_upstream,
                       validate_access_type, validate_base_url, validate_min_price)
from .util import (ensure_dir, iso, new_token, parse_iso, redact_text, utcnow)

PROG = "djb"


# ─────────────────────────────────────────────────────────── 输出 / 上下文
def _emit(payload: Any, *, as_json: bool, text: Callable[[Any], str] | None = None) -> None:
    if as_json or text is None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text(payload))


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(resolve_home(getattr(args, "home", None)))


def _open_store(paths: Paths, *, required: bool = True) -> Store:
    if not os.path.exists(paths.db_file):
        if required:
            raise NotInstalledError("本机未初始化：先执行 djb init")
        return Store(paths.db_file)
    return Store(paths.db_file)


def _context(args: argparse.Namespace) -> tuple[Paths, ClientConfig, Store]:
    paths = _paths(args)
    return paths, load_config(paths), _open_store(paths)


def _json_flag(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _gateway_is_placeholder(url: str | None) -> bool:
    """地址是否还没配过（空 / 占位符 / example.invalid 系）。"""
    text = (url or "").strip().lower()
    return (not text) or text.endswith("example.invalid") or "example.invalid" in text


# ─────────────────────────────────────────────────────────── bootstrap
def cmd_bootstrap(args: argparse.Namespace) -> int:
    """首启自举（幂等）：建目录/写配置/建库 + 可选拉起 daemon。

    桌面端启动时自动调用同一套逻辑（``djb_node.bootstrap``）；这里给
    CLI / 机房批量部署 / 自动化测试一个显式入口。
    """
    from .bootstrap import ensure_ready, start_daemon, summary_text
    home = getattr(args, "home", None)
    boot = ensure_ready(home=home, gateway=getattr(args, "gateway", None),
                        preset=getattr(args, "preset", None))
    daemon = None if getattr(args, "no_daemon", False) else start_daemon(home=home)
    payload = {"bootstrap": boot, "daemon": daemon, "summary": summary_text(boot, daemon)}
    _emit(payload, as_json=_json_flag(args), text=lambda value: value["summary"] or "无需处理")
    return 0 if not boot.get("errors") else 1


# ─────────────────────────────────────────────────────────── init
def cmd_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = load_config(paths)
    if getattr(args, "gateway", None):
        config.gateway_url = str(args.gateway).strip()
    elif _gateway_is_placeholder(config.gateway_url):
        # 没显式给地址就用构建期默认（2026-11-06：占位符发到客户手里 = 装完连不上）
        from .bootstrap import DEFAULT_GATEWAY_URL
        config.gateway_url = DEFAULT_GATEWAY_URL
    if getattr(args, "port", None):
        config.mirror_port = int(args.port)
    if getattr(args, "channel", None):
        config.update_channel = args.channel
    edition = getattr(args, "edition", None)
    edition_note = ""
    if edition:
        # 0.3.1（RC8.3-C）：显式给档位 = 明确要该通道的签名更新源。
        # 已有**自定义**源（内网镜像 / 本地目录）不覆盖 —— 那是运维刻意的选择。
        config.edition = str(edition).strip()
        known_sources = {item.update_source for item in update_source.channels()}
        current = config.update_source.strip()
        if getattr(args, "update_source", None) is None:
            if not current or current in known_sources:
                config.update_source = update_source.default_source(config.edition)
            else:
                edition_note = ("保留已有自定义 update_source（--edition 只登记档位，"
                                "不改源；要改请显式 --update-source）")
    if getattr(args, "update_source", None) is not None:
        config.update_source = str(args.update_source).strip()
    if getattr(args, "log_level", None):
        config.log_level = args.log_level
    config.validate()
    paths.prepare()
    save_config(paths, config)
    store = Store.create(paths.db_file, channel=config.update_channel)
    profile = store.profile()
    if getattr(args, "preset", None):
        if args.preset not in facts.PRESETS:
            raise UsageError("预设必须是 " + " / ".join(facts.PRESETS))
        profile.preset = args.preset
    if getattr(args, "daily_tokens", None) is not None:
        profile.daily_tokens = _validate_daily(args.daily_tokens)
    if getattr(args, "concurrent", None) is not None:
        profile.concurrent = _validate_concurrent(args.concurrent)
    if getattr(args, "weekly_tokens", None) is not None:
        profile.weekly_tokens = _validate_optional_tokens(args.weekly_tokens, "weekly")
    if getattr(args, "monthly_tokens", None) is not None:
        profile.monthly_tokens = _validate_optional_tokens(args.monthly_tokens, "monthly")
    if getattr(args, "cpu", None) is not None:
        profile.cpu_pct = int(args.cpu)
    if getattr(args, "gpu", None) is not None:
        profile.gpu_pct = int(args.gpu)
    if getattr(args, "mem", None) is not None:
        profile.mem_pct = int(args.mem)
    try:
        resource_caps_valid(profile.cpu_pct, profile.gpu_pct, profile.mem_pct)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    store.save_profile(profile)
    setup_logging(paths.log_file, level=config.log_level)
    get_logger().info("cli.init", extra={"event": "cli.init",
                                         "fields": {"home": paths.home}})
    # 环境检测（≤10 s；结论 + 下一步）
    checker = EnvChecker(paths, config, store=store)
    report = None
    if not getattr(args, "no_env", False):
        report = checker.detect()
    payload = {
        "home": paths.home,
        "initialized": True,
        "config": config.to_dict(),
        "update_source": update_source.resolve(config.update_source,
                                               edition=config.edition).to_dict(),
        "install": store.install().to_dict(),
        "profile": store.profile().to_dict(),
        "env": report.to_dict() if report else None,
        "next_steps": [
            "djb env                查看环境结论与下一步",
            "djb env --authorize    一次授权应用保守补全（可 djb env --rollback 回滚）",
            "djb auth --status      查看绑定状态；未绑定时仍可离线自测",
            "djb upstream add ...   接入 A1/A2/B 上游并申报",
            "djb start              启动 daemon（打印本机回环令牌）",
        ],
    }
    if _json_flag(args):
        _emit(payload, as_json=True)
    else:
        print("DJB 联脑客户端已初始化：" + paths.home)
        print("  预设 " + store.profile().preset + " · 并发 " + str(store.profile().concurrent)
              + " · 日配额 " + str(store.profile().daily_tokens) + " tokens")
        print("  本机镜像将监听 127.0.0.1:" + str(config.mirror_port) + "/v1（仅回环）")
        resolved = update_source.resolve(config.update_source, edition=config.edition)
        print("  更新源 " + resolved.label()
              + ("（档位 " + config.edition + "）" if config.edition else ""))
        if edition_note:
            print("  注意：" + edition_note)
        if report is not None:
            print("")
            print(report.text())
        print("")
        print("下一步：")
        for item in payload["next_steps"]:
            print("  - " + item)
    return 0


def _validate_daily(value: Any) -> int:
    number = int(value)
    if not (facts.DAILY_TOKENS_MIN <= number <= facts.DAILY_TOKENS_MAX):
        raise UsageError("日配额必须在 " + str(facts.DAILY_TOKENS_MIN) + "-"
                         + str(facts.DAILY_TOKENS_MAX) + " tokens")
    return number


def _validate_concurrent(value: Any) -> int:
    number = int(value)
    if not (facts.CONCURRENT_MIN <= number <= facts.CONCURRENT_MAX):
        raise UsageError("并发必须在 " + str(facts.CONCURRENT_MIN) + "-"
                         + str(facts.CONCURRENT_MAX))
    return number


def _validate_optional_tokens(value: Any, name: str) -> int:
    if value in (None, "", "0", 0):
        return 0
    return _validate_daily(value)


# ─────────────────────────────────────────────────────────── env
def cmd_env(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    checker = EnvChecker(paths, config, store=store,
                         budget_s=int(getattr(args, "budget", facts.ENV_CHECK_BUDGET_S) or
                                      facts.ENV_CHECK_BUDGET_S))
    if getattr(args, "rollback", False):
        result = checker.rollback()
        _emit(result, as_json=_json_flag(args),
              text=lambda value: ("已回滚：" + str(value.get("restored")) + " 项"
                                  if value.get("rolled_back")
                                  else "没有可回滚的授权（" + str(value.get("reason")) + "）"))
        return 0
    report = checker.detect()
    if getattr(args, "authorize", False):
        result = checker.authorize(report)
        payload = {"report": report.to_dict(), "authorization": result}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: report.text() + "\n\n" + (
                  "已授权应用补全：" + str(len(value["authorization"].get("applied") or []))
                  + " 项（djb env --rollback 可回滚）"
                  if value["authorization"].get("authorized")
                  else "未应用：" + str(value["authorization"].get("message")
                                     or value["authorization"].get("reason"))))
        return 0
    checker.store = store
    if not getattr(args, "no_persist", False):
        state = store.env_state()
        store.save_env_state(detected_at=report.detected_at,
                             authorized_at=state.get("authorized_at"),
                             plan=state.get("plan") or [], report=report.to_dict())
    _emit(report.to_dict(), as_json=_json_flag(args), text=lambda value: report.text())
    return 0


# ─────────────────────────────────────────────────────────── auth
def _auth_status_text(value: dict[str, Any]) -> str:
    """``djb auth --status`` 的人类可读输出；未绑定时给出可照抄的绑定步骤（含网页端地址）。"""
    lines = [
        "节点 " + str(value.get("node_id") or "(未注册)")
        + " · " + ("已绑定" if value.get("bound") else "未绑定（只做离线自测）"),
        "凭据后端：" + str(value["credentials"].get("storage"))
        + "（" + str(value["credentials"].get("file_mode")) + "）",
        "服务端：" + str(value.get("gateway")),
    ]
    if not value.get("bound"):
        lines.append("绑定：网页端 → 账户/节点 → 生成绑定码，然后 djb auth --bind <码>")
        lines.append("网页端：" + facts.WEB_CONSOLE_URL)
    return "\n".join(lines)


def cmd_auth(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    client = ControlClient(paths)
    api = NodeApi(config.gateway_url, client_key=credentials.client_key(),
                  http=HttpClient(timeout=15.0))
    node_id = credentials.node_id()
    if getattr(args, "set_key", None) is not None:
        value = args.set_key
        if value == "-":
            value = getpass.getpass("节点客户端密钥（不回显）：").strip()
        if not value:
            raise UsageError("密钥为空")
        credentials.set_client_key(value)
        print("已保存客户端密钥（0600 文件，未写入日志/数据库）")
        return 0
    if getattr(args, "pair", False):
        if not node_id:
            raise AuthError("尚未注册节点：先 djb init + djb start（或 djb auth --status）")
        credentials.require_client_key()
        result = api.pair_key(node_id, rotate=not getattr(args, "no_rotate", False),
                              overlap_seconds=int(getattr(args, "overlap_seconds", 0) or 0))
        new_key = str(result.get("client_key"))
        credentials.set_client_key(new_key)
        _emit({"node_id": node_id, "rotated": True,
               "previous_key_expires_at": result.get("previous_key_expires_at"),
               "key_prefix": result.get("key_prefix")},
              as_json=_json_flag(args),
              text=lambda value: "已轮换客户端密钥（旧密钥重叠至 "
              + str(value.get("previous_key_expires_at")) + "）")
        return 0
    if getattr(args, "bind", None):
        if not node_id:
            raise AuthError("尚未注册节点：先 djb init + djb start")
        result = api.bind(node_id, str(args.bind))
        _emit(result, as_json=_json_flag(args),
              text=lambda value: "绑定成功：" + json.dumps(value, ensure_ascii=False))
        return 0
    if getattr(args, "unbind", False):
        if not node_id:
            raise AuthError("尚未注册节点")
        if not getattr(args, "yes", False):
            raise UsageError("解绑会停止接单：加 --yes 确认")
        result = api.unbind(node_id)
        _emit(result, as_json=_json_flag(args),
              text=lambda value: "已解绑：" + json.dumps(value, ensure_ascii=False))
        return 0
    # 默认 --status
    bound = bool(credentials.client_key())
    info: dict[str, Any] = {"node_id": node_id, "bound": bound,
                            "credentials": credentials.describe(),
                            "gateway": config.gateway_url}
    try:
        if node_id and bound:
            info["server_binding"] = api.binding(node_id)
    except DjbError as exc:
        info["server_binding_error"] = exc.message
    _emit(info, as_json=_json_flag(args), text=_auth_status_text)
    return 0


#: 本地控制通道被关掉（kill switch）时给用户的可行动提示：**不装死、不喊"不可用"**
_CONTROL_DISABLED_HINT = (
    "djb: 本地控制通道已关闭（client.ipc.windows.enabled=false）：daemon 可能仍在运行，"
    "但 CLI 无法确认它是否起来、也没法优雅停止它。\n"
    "  · 打开控制通道：编辑 config.json 的 extra.feature_flags[\"client.ipc.windows.enabled\"] = true 后重试"
    "（发布默认值已是 true，只有你手动关掉才会看到本提示）\n"
    "  · 停止 daemon：djb stop --force（按 pid 文件 + 进程身份校验，不会误杀无关进程）\n"
    "  · 只想起服务、不要控制：djb start --foreground（前台运行，Ctrl-C 退出）\n"
    "  · 排查日志：djb logs --tail 50"
)


def _control_channel_disabled(paths: Paths) -> bool:
    """本机本地控制通道是否被 kill switch 关掉（仅 Windows 有该通道；其它平台恒 False）。"""
    if os.name != "nt":
        return False
    try:
        from .daemon import windows_control_enabled
        return not windows_control_enabled(paths)
    except Exception:
        return False


#: ``djb start`` 认为「已经可以停下等待」的状态：online 是成功，其余是明确的终态
_START_ACCEPT_STATES = ("online",)
_START_SETTLED_STATES = ("online", "paused", "error", "updating", "stopped")


# ─────────────────────────────────────────────────────────── start / stop
#: daemon 子进程 stdout/stderr 的落点（启动失败时回显它的尾行，E2E-09）
_DAEMON_OUT_NAME = facts.DAEMON_OUT_NAME      # 单一口径：bootstrap / uninstall 共用


def _port_busy(port: int) -> bool:
    """127.0.0.1:port 现在还能不能 bind。

    判据用 **bind 本身**（不开 SO_REUSEADDR），而不是 connect：
    connect 只证明「有人在听」，bind 才回答「daemon 还能不能起来」。
    停止竞态里正是后者（socket 文件没了、监听还在）把人骗去排「控制套接字不存在」。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _listening_pid(port: int) -> int | None:
    """谁在 LISTEN 这个端口（只查 Linux /proc；拿不到返回 None —— 不猜、不编 pid）。"""
    if not os.path.isdir("/proc"):
        return None
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, "r", encoding="utf-8", errors="replace") as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 10 or parts[3] != "0A":      # 0A = LISTEN
                continue
            try:
                local_port = int(parts[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == int(port):
                inodes.add(parts[9])
    if not inodes:
        return None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = os.path.join("/proc", entry, "fd")
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return int(entry)
    return None


def _startup_log_hint(paths: Paths, *, limit: int = 3) -> str:
    """启动失败时回显**日志尾行**（E2E-09：真实原因只写在 daemon.out 里）。"""
    lines: list[str] = []
    out_path = os.path.join(paths.logs_dir, _DAEMON_OUT_NAME)
    if os.path.isfile(out_path):
        lines = _read_tail([out_path], limit)
    if not lines:
        lines = _read_tail(_log_files(paths.logs_dir), limit)
    clean = [redact_text(line.rstrip("\n")) for line in lines if line.strip()]
    return "\n".join(clean[-limit:])


def cmd_start(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    if getattr(args, "port", None):
        config.mirror_port = int(args.port)
        config.validate()
        save_config(paths, config)
    token = new_token(32)
    port = int(config.mirror_port)
    if getattr(args, "foreground", False):
        daemon = Daemon(paths, config=config, local_token=token)
        if not getattr(args, "no_token", False):
            print("local_token=" + token, flush=True)
        return daemon.run()
    _spawn_daemon(paths, config, token)
    deadline = time.monotonic() + float(getattr(args, "timeout", 20) or 20)
    last_error = ""
    pid: Any = None
    while time.monotonic() < deadline:
        try:
            response = ControlClient(paths).call({"cmd": "ping"}, timeout=1.0)
            if response.get("ok"):
                pid = response.get("pid")
                last_error = ""      # 控制口已就绪：别把之前的「套接字不存在」当结论（会误导）
                break
        except NotRunningError as exc:
            last_error = exc.message
        time.sleep(0.25)
    else:
        if _control_channel_disabled(paths):
            print(_CONTROL_DISABLED_HINT, file=sys.stderr)
        else:
            # E2E-09：端口被别人占着时，真实原因不是「控制套接字不存在」。
            # 只改**提示**、不改退出码（启动失败仍然是 EXIT_NOT_RUNNING）。
            buried = ""
            if _port_busy(port):
                owner = _listening_pid(port)
                buried = ("；端口 " + str(port) + " 已被"
                          + ((" pid=" + str(owner)) if owner else "其它进程")
                          + " 占用 —— daemon 绑不上本机镜像端口。"
                          + ("先 djb stop --force 清掉旧 daemon，" if owner else "")
                          + "或换端口：djb start --port <新端口>")
            print("djb: daemon 启动超时（" + last_error + "）" + buried
                  + "；查看 djb logs --tail 50", file=sys.stderr)
        # E2E-09：把日志尾行直接印出来 —— [Errno 98] 这类真实原因只在那里
        hint = _startup_log_hint(paths)
        if hint:
            print("最近的日志：\n" + hint, file=sys.stderr)
        return facts.EXIT_NOT_RUNNING
    # 控制口起来 ≠ 已经接单：等状态机真的走到 online（或走到 paused/error 这类终态）。
    # 否则用户看到「已启动」，实际还在 starting、一单也不接。
    state, reason = "", ""
    error_seen = 0
    while time.monotonic() < deadline:
        try:
            status = ControlClient(paths).call({"cmd": "status"}, timeout=2.0).get("status") or {}
        except NotRunningError as exc:
            # 控制口刚起来时 daemon 可能正忙着 bootstrap（网络/数据库），这一次读不到
            # 不代表没起来：继续等到 deadline。Windows 命名管道上尤其常见。
            last_error = exc.message
            time.sleep(0.25)
            continue
        state = str(status.get("state") or "")
        reason = str(status.get("reason") or "")
        if state in _START_SETTLED_STATES:
            # error 可能是**瞬时**的（例：注册被拒 -> 随即落到 paused/unbound）。
            # 连续两次都读到 error 才当终态，否则会误报「启动失败」。
            if state == "error":
                error_seen += 1
                if error_seen < 2:
                    time.sleep(0.25)
                    continue
            break
        time.sleep(0.25)
    if state not in _START_ACCEPT_STATES:
        # 状态优先于「早期连接错误」：daemon 已经起来了，真正的原因是没进接单状态
        # （例如未绑定账户 -> paused/unbound），这时再说「控制套接字不存在」是误导。
        detail = ("daemon 已启动但状态是 " + (state or "未知")
                  + ("（" + reason + "）" if reason else "")
                  + "，没有进入接单状态；查看 djb status / djb logs --tail 50")
        if not state and last_error:
            detail = "daemon 没有起来：" + last_error
        print("djb: " + detail, file=sys.stderr)
        return facts.EXIT_NOT_RUNNING
    payload = {"started": True, "pid": pid, "state": state,
               "local_api": "http://" + facts.MIRROR_HOST + ":"
                            + str(config.mirror_port) + "/v1",
               "local_token": token,
               "token_note": "令牌只在本次进程存活期间有效；不落盘，可用 djb status --show-token 再次读取",
               "next_step": "export DJB_LOCAL_TOKEN=<token>; 之后用 Bearer 调用本机 /v1"}
    if reason:
        payload["reason"] = reason
    _emit(payload, as_json=_json_flag(args),
          text=lambda value: "daemon 已启动（pid " + str(value["pid"]) + "），状态 "
          + str(value.get("state") or "—") + "\n"
          + "本机镜像：" + str(value["local_api"]) + "（仅 127.0.0.1）\n"
          + "local_token=" + str(value["local_token"]) + "\n"
          + "令牌只在本次进程存活期间有效，不落盘；需要时 djb status --show-token")
    return 0


def _spawn_daemon(paths: Paths, config: ClientConfig, token: str) -> None:
    log_path = os.path.join(paths.logs_dir, _DAEMON_OUT_NAME)
    ensure_dir(paths.logs_dir, 0o700)
    stdout = open(log_path, "ab")
    try:
        os.chmod(log_path, 0o600)
    except OSError:                                            # pragma: no cover
        pass
    env = dict(os.environ)
    env[facts.LOCAL_TOKEN_ENV] = token
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = package_parent + (os.pathsep + existing if existing else "")
    command = [sys.executable, "-m", "djb_node.daemon", "--home", paths.home,
               "--port", str(config.mirror_port)]
    try:
        subprocess.Popen(command, stdout=stdout, stderr=stdout, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env, close_fds=True,
                         cwd=package_parent)
    finally:
        stdout.close()


def cmd_stop(args: argparse.Namespace) -> int:
    paths = _paths(args)
    client = ControlClient(paths)
    try:
        client.call({"cmd": "stop"}, timeout=5.0)
    except NotRunningError:
        # 控制面不可用 ≠ 进程没在跑：命名管道/套接字丢了、或 ipc 开关关着时，
        # daemon 可能还活得好好的。`--force` 的意义正在这里 —— 走 pid 文件 + 身份
        # 校验的逃生门，而不是打印一句"未在运行"把用户堵死。
        if getattr(args, "force", False):
            has_record = pidfile.read(paths) is not None or pidfile.read_raw(paths) is not None
            if not has_record:
                print("daemon 未在运行（幂等：无需停止）")
                return 0
            return _force_stop(paths)
        print("daemon 未在运行（幂等：无需停止）")
        return 0
    # E2E-09：控制口消失 ≠ 进程已经放端口。等**端口真的能再 bind** 再说「已停止」，
    # 否则紧跟一条 djb start 会撞 Errno 98，却被报成「控制套接字不存在」。
    try:
        port = int(load_config(paths).mirror_port)
    except DjbError:
        port = int(facts.MIRROR_PORT)
    deadline = time.monotonic() + float(getattr(args, "timeout", 15) or 15)
    while time.monotonic() < deadline:
        if not os.path.exists(control_socket_file(paths)) and not _port_busy(port):
            print("daemon 已停止")
            return 0
        time.sleep(0.2)
    if getattr(args, "force", False):
        return _force_stop(paths)
    owner = _listening_pid(port) if _port_busy(port) else None
    detail = ("控制口已关闭，但端口 " + str(port) + " 仍被"
              + ((" pid=" + str(owner)) if owner else "其它进程") + " 占用"
              ) if _port_busy(port) else "控制套接字仍在"
    print("djb: 停止超时（" + detail + "）；可用 djb stop --force", file=sys.stderr)
    return facts.EXIT_CONFLICT


#: 拒绝强杀时给用户的下一步（每种原因码对应"安全且真的有用"的动作）
_FORCE_HINTS = {
    pidfile.REASON.MISSING: "记录里的进程已经没了；先跑 djb status 确认状态，必要时删掉 pid 文件",
    pidfile.REASON.LEGACY: "pid 文件是旧版格式（只有 pid，防不了 PID 复用）；删掉它再重试",
    pidfile.REASON.MALFORMED: "pid 文件内容不完整；删掉它再重试",
    pidfile.REASON.TOKEN_UNREADABLE: "读不到目标进程的启动时刻（权限不足或进程刚退）；先 djb status 确认",
    pidfile.REASON.TOKEN_MISMATCH: "pid 已被系统复用给别的进程；本命令不会杀它，删掉 pid 文件再重试",
    pidfile.REASON.CMDLINE_UNREADABLE: "读不到目标进程的命令行（权限不足或进程刚退）；先 djb status 确认",
    pidfile.REASON.NOT_DAEMON: "pid 指向的不是 djb daemon（多半是 pid 复用）；本命令不会杀它",
    pidfile.REASON.HOME_MISMATCH: "pid 指向的 daemon 属于另一个 --home；确认是不是你想起的那个",
}


def _force_stop(paths: Paths) -> int:
    """`djb stop --force`：强杀**已确认身份**的 daemon；身份校验不过就拒绝。

    pid 会被复用 ⇒ 只认 pid 就是「误杀门」。这里要求 pid 文件是 (pid, 启动时刻) 双键，
    并且强杀前重新核一遍：令牌一致 + 命令行含 djb_node.daemon + --home 等于本 paths。
    """
    record = pidfile.read(paths)
    raw = None if record is not None else pidfile.read_raw(paths)
    ok, reason = pidfile.verify(record, paths)
    if not ok:
        print("djb: 拒绝强制结束：pid 文件不可信（" + reason + "）", file=sys.stderr)
        print("下一步：" + _FORCE_HINTS.get(reason, "手动确认进程身份后再处理"), file=sys.stderr)
        if raw is not None:
            print("pid 文件：" + paths.pid_file, file=sys.stderr)
        return facts.EXIT_CONFLICT
    pid = int(record["pid"])
    try:
        # Windows 没有 SIGKILL（os.kill 只认 SIGTERM/CTRL_*）；SIGTERM 在 Windows 上
        # 走 TerminateProcess，语义就是要的"立即结束"。
        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    except OSError as exc:
        print("djb: 强制结束失败（pid " + str(pid) + "）：" + str(exc), file=sys.stderr)
        return facts.EXIT_CONFLICT
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and pidfile.probe.is_alive(pid):
        time.sleep(0.1)
    if pidfile.probe.is_alive(pid):
        print("djb: 已发强制结束但 pid " + str(pid) + " 仍在；先 djb status 确认", file=sys.stderr)
        return facts.EXIT_CONFLICT
    pidfile.remove(paths, pid=pid)
    print("已强制结束 daemon（pid " + str(pid) + "）")
    return 0


# ─────────────────────────────────────────────────────────── status / pause / resume
def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = _open_store(paths)
    client = ControlClient(paths)
    payload: dict[str, Any]
    try:
        response = client.call({"cmd": "status"}, timeout=5.0)
        payload = dict(response.get("status") or {})
        payload["daemon_running"] = True
    except NotRunningError:
        payload = _offline_status(paths, store)
    if getattr(args, "show_token", False):
        try:
            payload["local_token"] = ControlClient(paths).call({"cmd": "token"},
                                                              timeout=5.0).get("local_token")
        except NotRunningError:
            payload["local_token"] = None
            payload["token_error"] = "daemon 未运行：令牌只在 daemon 进程存活期间有效"
    _emit(payload, as_json=_json_flag(args), text=lambda value: _status_text(value))
    return 0


def _offline_status(paths: Paths, store: Store) -> dict[str, Any]:
    snapshot = Snapshot.from_dict(store.get_kv("daemon_state"))
    install = store.install()
    policy = store.policy()
    runtime = store.get_kv("runtime", default={}) or {}
    config = load_config(paths)
    # P4 / P6：daemon 没起时也要如实回答（离线不等于「什么都没开」）
    negotiated = store.get_kv(facts.CHANNEL_REQUIRED_KV, default={}) or {}
    required = (negotiated.get("required") is True
                if isinstance(negotiated, dict) else False)
    manager = UpstreamManager(store, CredentialStore(paths))
    local_models = [entry for item in manager.list()
                    for entry in manager.probe_details(item.id)]
    return {
        "daemon_running": False,
        "channel": {"enabled": channel_enabled(config), "running": False,
                    "required": bool(required),
                    "wanted": channel_enabled(config, required=required),
                    "state": "offline", "wait_s": facts.CHANNEL_POLL_WAIT_S,
                    "jobs_done": 0, "jobs_failed": 0, "last_error": None},
        # ROUND3：daemon 未运行时也必须能回答「周期复探：每 …」——真机取证（Windows
        # win-probe.json）就是停止态采的，此时 interval_s=None 会让整行消失，
        # 又被读成「周期复探没开」。这里给**计划节奏**（健康启动后的基础周期）并显式标注
        # planned，渲染层据此加「daemon 未运行，启动后生效」。
        "probe": {"periodic_enabled": feature_flag(config, facts.PROBE_FLAG_PERIODIC),
                  "interval_s": facts.PROBE_PERIODIC_DEFAULT_S,
                  "cap_s": facts.PROBE_PERIODIC_CAP_S,
                  "planned": True, "status": "daemon 未运行",
                  "fresh_window_s": facts.PROBE_FRESH_S,
                  "models": local_models},
        "state": snapshot.state if snapshot.state == "stopped" else "stopped",
        "reason": None,
        "reason_label": "daemon 未运行",
        "next_step": "djb start 启动 daemon",
        "since": snapshot.since,
        "accepting": False,
        "version": facts.CLIENT_VERSION,
        "protocol_version": facts.PROTOCOL_VERSION,
        "install": install.to_dict(),
        "last_runtime": runtime,
        "policy": policy_summary(policy),
        "receipts": {"count": store.receipt_count(),
                     "stale": store.stale_receipt_count(),
                     "oldest_age_s": _age_seconds(store.oldest_receipt_at()),
                     "backlog": False},
        "credentials": CredentialStore(paths).describe(),
        "timestamp": iso(utcnow()),
    }


def _age_seconds(value: str | None) -> int:
    moment = parse_iso(value)
    if moment is None:
        return 0
    return max(0, int((utcnow() - moment).total_seconds()))


def _clock(moment: Any) -> str:
    """时间戳 → 本机时区 ``HH:MM``（只用于人看；缺失/非法返回 ``?``）。"""
    try:
        return moment.astimezone().strftime("%H:%M")
    except (AttributeError, ValueError, OSError):          # pragma: no cover - 防御
        return "?"


def _minutes(seconds: Any) -> str:
    """秒 → 人话（<90 s 用秒，其余**向下取整**到分钟）；缺失给「?」，绝不补 0。

    向下取整是 ROUND3 / D-R3-02 的刻意选择：这一族文案几乎都在说「还剩多久掉容量」，
    ``round()`` 会把 350 s 说成「约 6 分钟」（实际 5.8 min）——把剩余新鲜度说多，
    正是 P6 要消灭的「谎报」。宁可少说 30 秒，不许多说 1 秒。
    """
    try:
        value = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    if value < 90:
        return str(max(1, value)) + " 秒"
    return str(int(value // 60)) + " 分钟"


def _duration_exact(seconds: Any) -> str:
    """周期/上限这类**承诺值**的精确说法（ROUND3 / D-R3-02）：450 s → 「7.5 分钟」。

    与 :func:`_minutes` 不同，这里不能取整：`PROBE_PERIODIC_CAP_S=450` 若渲染成
    「8 分钟」，显示值就**越过了自己声明的上限**（450 s = 7.5 min < 8 min），
    用户没法判断哪个是真的。整分钟才省略小数，其余保留一位。
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if value < 90:
        return str(max(1, int(round(value)))) + " 秒"
    minutes = value / 60.0
    if abs(minutes - round(minutes)) < 0.05:
        return str(int(round(minutes))) + " 分钟"
    return ("%.1f" % minutes) + " 分钟"


def _probe_status_line(models: Any, *, now: Any = None, fresh_s: Any = None) -> str:
    """「最近探针：ok @ 14:32（约 7 分钟后掉容量）」（P6/E2E-10）。

    ``djb status`` 与 ``djb upstream list`` 共用这一处口径：
    · 没探过 → 明确说「从未探测」，不编时间、不说「正常」；
    · 探过 → 结论（ok / 失败分类）+ 时间 + **还剩多少新鲜度**；
    · 已过期 → 直接说「已掉容量」并给出超期时长（不再显示「接单=是」而实际 0 容量）。
    """
    moment = now or utcnow()
    ttl = int(fresh_s or facts.PROBE_FRESH_S)
    entries = [item for item in (models or []) if isinstance(item, dict)]
    newest = None
    for item in entries:
        stamp = parse_iso(item.get("last_probe_at")) if item.get("last_probe_at") else None
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    if newest is None:
        return ("最近探针：从未探测（不产生容量；手动补一次：djb upstream probe --id <上游>）")
    same = [item for item in entries
            if parse_iso(item.get("last_probe_at") or "") == newest]
    ok = any(item.get("inference_ok") is True for item in same)
    label = "ok"
    if not ok:
        klass = next((str(item.get("probe_error_class")) for item in same
                      if item.get("probe_error_class")), "")
        label = klass or "失败"
    age = max(0, int((moment - newest).total_seconds()))
    remain = ttl - age
    suffix = ("（约 " + _minutes(remain) + "后掉容量）" if remain > 0
              else "（**已掉容量**：超出新鲜度 " + _minutes(-remain) + "）")
    return "最近探针：" + label + " @ " + _clock(newest) + suffix


def _capacity_reason(item: dict[str, Any]) -> str:
    """单个模型「为什么没进容量」——按证据排序，不猜。"""
    if item.get("enabled") is False:
        return "上游已停用"
    if item.get("inference_ok") is None:
        return "从未探测"
    if item.get("inference_ok") is False:
        return "探针未通过（" + str(item.get("probe_error_class") or "未记录原因") + "）"
    age = item.get("probe_age_s")
    return ("探针过期（" + _minutes(age) + "前）" if age is not None else "探针过期")


def _capacity_lines(value: dict[str, Any]) -> list[str]:
    """容量与「掉容量原因」+ 周期复探节奏（P6：容量下降必须可见，不许静默）。"""
    probe = value.get("probe") if isinstance(value.get("probe"), dict) else {}
    models = [item for item in (probe.get("models") or []) if isinstance(item, dict)]
    lines: list[str] = []
    if models:
        live = [item for item in models if item.get("announceable")]
        lines.append("容量：" + str(len(live)) + "/" + str(len(models)) + " 个模型可广播")
        reasons = [str(item.get("model")) + " —— " + _capacity_reason(item)
                   for item in models if not item.get("announceable")]
        if reasons:
            shown = reasons[:4]
            if len(reasons) > 4:
                shown.append("还有 " + str(len(reasons) - 4) + " 个")
            lines.append("掉容量原因：" + "；".join(shown))
    if probe:
        if probe.get("periodic_enabled") is False:
            lines.append("周期复探：关（client.probe.periodic.enabled）→ 探针一过期就掉容量，"
                         "只能手动 djb upstream probe")
        elif isinstance(probe.get("interval_s"), int):
            # D-R3-02：周期是**承诺值**，必须精确（450 s → 7.5 分钟）；上限一并印出来，
            # 让「显示值 ≤ 上限」肉眼可核（round() 曾把 450 s 说成「每 8 分钟」越过 cap）。
            interval = int(probe.get("interval_s"))
            cap = probe.get("cap_s")
            parts = ["周期复探：每 " + _duration_exact(interval) + "（" + str(interval) + " 秒）"]
            if isinstance(cap, int) and cap > 0:
                parts.append("上限 " + _duration_exact(cap) + "（" + str(cap) + " 秒）")
            parts.append("退避不得越过上限")
            if probe.get("planned") is True:
                parts.append("daemon 未运行，启动后生效")
            lines.append(" · ".join(parts))
    return lines


def _channel_text(channel: dict[str, Any]) -> str:
    """「通道：已连接 / 未开启 / 已断开」——只按证据说，**不得谎报**（P4 落点 C）。

    判据只有三个字段：``wanted``（该不该跑）、``running``（线程在不在）、
    ``state``（通道状态机的当前态）。任何一项拿不到证据都不说「已连接」。
    """
    wanted = bool(channel.get("wanted", channel.get("enabled")))
    running = bool(channel.get("running"))
    state = str(channel.get("state") or "idle")
    tail = ""
    if channel.get("required") is True:
        tail += " · 服务端要求通道"
    done = int(channel.get("jobs_done") or 0)
    failed = int(channel.get("jobs_failed") or 0)
    if done or failed:
        tail += " · 已接单 " + str(done) + ("（失败 " + str(failed) + "）" if failed else "")
    if not wanted:
        return ("未开启（云端派单要等超时；开：djb flags --enable "
                + facts.CHANNEL_FLAG + " 后重启 daemon）")
    if state == "offline":
        return "已断开（daemon 未运行）"
    if not running:
        reason = channel.get("last_error")
        return ("已断开（通道线程未运行" + ("：上次错误 " + str(reason) if reason else "")
                + "）" + tail)
    if state in ("polling", "executing"):
        return "已连接（长轮询 wait_s=" + str(channel.get("wait_s")) + "）" + tail
    return "已断开（线程在跑但状态 " + state + "，正在退出）" + tail


def _pause_detail_text(detail: Any) -> str:
    """暂停细节 → 人话（E2E-06：只说「已暂停」不够，要说**为什么**）。"""
    if not isinstance(detail, dict) or not detail:
        return ""
    source = detail.get("source")
    if source:
        parts = []
        for item in detail.get("signals") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") in ("in_use", "error"):
                parts.append(str(item.get("source") or "?") + "："
                             + str(item.get("detail") or item.get("state")))
        return ("让路（" + str(source) + "）"
                + ("；" + "、".join(parts) if parts else ""))
    over = detail.get("over")
    if isinstance(over, dict) and over:
        return "资源超上限：" + "、".join(
            str(key) + " " + str(item.get("value")) + " > " + str(item.get("cap"))
            for key, item in over.items() if isinstance(item, dict))
    return ""


def _status_text(value: dict[str, Any]) -> str:
    lines = [
        "状态：" + str(value.get("state")) + (
            "（" + str(value.get("reason_label")) + "）" if value.get("reason_label") else ""),
        "接单：" + ("是" if value.get("accepting") else "否"),
        "版本：" + str(value.get("version")) + " · 协议 " + str(value.get("protocol_version")),
        "daemon：" + ("运行中" if value.get("daemon_running") else "未运行"),
    ]
    # P4 落点 C：通道三态。没这一行时「路由成功但节点收不到单」根本看不出来。
    channel = value.get("channel")
    if isinstance(channel, dict) and channel:
        lines.append("通道：" + _channel_text(channel))
    # E2E-06：暂停原因要能看到「为什么」（让路信号/资源超限/时段外…恢复路径各不相同）
    pause_detail = _pause_detail_text(value.get("pause_detail"))
    if pause_detail:
        lines.append("暂停细节：" + pause_detail)
    if value.get("next_step"):
        lines.append("下一步：" + str(value["next_step"]))
    install = value.get("install") or {}
    if install:
        # ROUND3：install.version 是**本机 home 的安装记录**（只在 djb init / 自升级 apply·rollback
        # 时写，见 store.py:438 / update.py:502,522），**不是**运行中的二进制版本。
        # 覆盖安装不会更新它，于是 0.2.5 的二进制旁边会显示「安装：0.2.2」——两行并列就是谎报。
        # 两者不一致时补一句事实括注；相等时输出与改前逐字一致（不加噪音）。
        recorded = str(install.get("version"))
        running = str(value.get("version") or "")
        mismatch = bool(running) and bool(recorded) and recorded != running
        lines.append("安装：" + recorded + " · 频道 "
                     + str(install.get("channel")) + " · 桶 " + str(install.get("bucket"))
                     + ("（本机 home 记录，运行版本 " + running + "）" if mismatch else ""))
    policy = value.get("policy") or {}
    lines.append("策略：" + ("可用" if policy.get("usable") else "不可用（保守模式）")
                 + (" · " + str(policy.get("policy_version"))
                    if policy.get("policy_version") else ""))
    receipts = value.get("receipts") or {}
    lines.append("收据缓冲：" + str(receipts.get("count", 0)) + " 条 · 最旧 "
                 + str(receipts.get("oldest_age_s", 0)) + " 秒")
    credentials = value.get("credentials") or {}
    lines.append("凭据：" + ("已配置" if credentials.get("client_key_set") else "未配置")
                 + " · " + str(credentials.get("storage")))
    machine = value.get("machine") or {}
    if isinstance(machine, dict) and machine.get("schema"):
        cpu = machine.get("cpu") or {}
        memory = machine.get("memory") or {}
        disks = machine.get("disks") or []
        gpus = machine.get("gpus") or []
        bench = machine.get("benchmark") or {}
        detail = "机器：" + str(cpu.get("model") or "未知 CPU")
        if cpu.get("threads"):
            detail += " · " + str(cpu.get("threads")) + " 线程"
        if memory.get("total_bytes"):
            detail += " · 内存 " + str(round(memory["total_bytes"] / 1024 ** 3, 1)) + " GB"
        if disks and disks[0].get("free_bytes"):
            detail += " · 磁盘可用 " + str(round(disks[0]["free_bytes"] / 1024 ** 3, 1)) + " GB"
        lines.append(detail)
        if gpus:
            for item in gpus:
                line = "GPU：" + str(item.get("model") or "未知")
                if item.get("vram_bytes"):
                    line += " · 显存 " + str(round(item["vram_bytes"] / 1024 ** 3, 1)) + " GB"
                if item.get("utilization_pct") is not None:
                    line += " · 利用率 " + str(item["utilization_pct"]) + "%"
                lines.append(line)
        else:
            lines.append("GPU：未检测到（无 GPU 不编造）")
        if bench.get("tokens_per_second"):
            lines.append("基准：" + str(bench.get("tokens_per_second")) + " tok/s（"
                         + str(bench.get("method")) + "）")
        lines.append("机器画像上报：" + ("开" if value.get("machine_reported") else "关"))
    # P6 / E2E-10：最近探针 + 掉容量原因（容量下降必须可见，不许静默归零）
    probe = value.get("probe") if isinstance(value.get("probe"), dict) else {}
    if probe.get("models"):
        lines.append(_probe_status_line(probe.get("models"),
                                        fresh_s=probe.get("fresh_window_s")))
    lines.extend(_capacity_lines(value))
    local_api = value.get("local_api") or {}
    if local_api:
        lines.append("本机镜像：http://" + str(local_api.get("host")) + ":"
                     + str(local_api.get("port")) + "/v1（仅回环）")
    if value.get("resume_note"):
        lines.append("提示：" + str(value["resume_note"]))
    if value.get("daemon_running"):
        lines.append("提示：djb status --show-token 可读取本次进程的 local_token")
    return "\n".join(lines)


def _pause_resume(args: argparse.Namespace, *, pause: bool) -> int:
    paths = _paths(args)
    store = _open_store(paths)
    try:
        response = ControlClient(paths).call({"cmd": "pause" if pause else "resume"},
                                             timeout=5.0)
        payload = dict(response.get("status") or {})
        _emit(payload, as_json=_json_flag(args), text=lambda value: _status_text(value))
        return 0
    except NotRunningError:
        store.set_kv("user_paused", bool(pause))
        note = ""
        if not pause:
            # E2E-06 的等价可靠路径：daemon 没起时也要能把「让路暂停」清掉，
            # 否则用户 resume 了、下次 start 又从 paused(yield_paused) 开始。
            # 只按**持久化快照**判断（不猜）：上次确实是让路暂停才动让路设置。
            snapshot = Snapshot.from_dict(store.get_kv("daemon_state"))
            profile = store.profile()
            if snapshot.reason == "yield_paused" and profile.yield_enabled:
                profile.yield_enabled = False
                store.save_profile(profile)
                note = ("；已同时关闭「自动让路」（上次停在让路暂停）。"
                        "重新打开：djb limits --yield")
        print(("已记录暂停偏好" if pause else "已记录恢复偏好")
              + "（daemon 未运行；下次 djb start 生效）" + note)
        return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return _pause_resume(args, pause=True)


def cmd_resume(args: argparse.Namespace) -> int:
    return _pause_resume(args, pause=False)


# ─────────────────────────────────────────────────────────── daemon 控制（F14 reload）
def _reload_text(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("message") or "配置已重载")]
    changed = payload.get("changed") or []
    if changed:
        lines.append("变更字段：" + ", ".join(str(item) for item in changed))
    notes = payload.get("notes") or []
    for note in notes:
        lines.append("· " + str(note))
    for item in payload.get("restart_required") or []:
        if isinstance(item, dict):
            lines.append("需要重启：" + str(item.get("field")) + " "
                         + str(item.get("from")) + "→" + str(item.get("to"))
                         + "（" + str(item.get("reason")) + "）")
    lines.append("上游地址：" + str(payload.get("upstreams", 0)) + " 条（已按磁盘重读）")
    lines.append("在途任务：" + str(payload.get("inflight", 0)) + "（重载不中断）")
    if payload.get("fingerprint"):
        lines.append("配置指纹：" + str(payload["fingerprint"])
                     + " · 第 " + str(payload.get("reload_count", 1)) + " 次重载")
    return "\n".join(lines)


def cmd_daemon_reload(args: argparse.Namespace) -> int:
    """`djb daemon reload`：重读 config.json 与上游地址，不中断在途任务。"""
    paths = _paths(args)
    try:
        response = dict(ControlClient(paths).call({"cmd": "reload"}, timeout=15.0))
    except NotRunningError:
        # 配置已在磁盘：下次 djb start 一定读到，如实说明「现在没有进程可重载」
        print("daemon 未运行；配置已写入磁盘，下次 djb start 生效。")
        return facts.EXIT_OK
    except UsageError:
        print("djb: 当前 daemon 没有 reload 命令（版本较旧）；"
              "重启 daemon 后生效。", file=sys.stderr)
        return facts.EXIT_USAGE
    if response.get("ok") is not True:
        reason = str(response.get("error") or "原因未知")
        print("djb: 重载失败：" + reason + "；已保留旧配置（daemon 未中断）",
              file=sys.stderr)
        if _json_flag(args):
            _emit(response, as_json=True)
        return facts.EXIT_VERIFY
    _emit(response, as_json=_json_flag(args), text=_reload_text)
    return facts.EXIT_OK


def cmd_daemon(args: argparse.Namespace) -> int:
    action = getattr(args, "daemon_action", None)
    if action == "reload":
        return cmd_daemon_reload(args)
    print("用法：djb daemon reload [--json]（重载配置与上游地址，不中断在途任务）",
          file=sys.stderr)
    return facts.EXIT_USAGE


# ─────────────────────────────────────────────────────────── limits
def cmd_limits(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    profile = store.profile()
    if getattr(args, "clear_schedule", False):
        profile.schedule = []
    if getattr(args, "schedule", None):
        try:
            profile.schedule = parse_schedule_windows(args.schedule)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
    if getattr(args, "schedule_json", None):
        try:
            parsed = json.loads(args.schedule_json)
        except ValueError as exc:
            raise UsageError("--schedule-json 不是合法 JSON") from exc
        if not isinstance(parsed, list):
            raise UsageError("--schedule-json 必须是窗口数组")
        profile.schedule = parsed
    changed = False
    if getattr(args, "preset", None):
        if args.preset not in facts.PRESETS:
            raise UsageError("预设必须是 " + " / ".join(facts.PRESETS))
        profile.preset = args.preset
        changed = True
    if getattr(args, "daily_tokens", None) is not None:
        profile.daily_tokens = _validate_daily(args.daily_tokens)
        changed = True
    if getattr(args, "weekly_tokens", None) is not None:
        profile.weekly_tokens = _validate_optional_tokens(args.weekly_tokens, "weekly")
        changed = True
    if getattr(args, "monthly_tokens", None) is not None:
        profile.monthly_tokens = _validate_optional_tokens(args.monthly_tokens, "monthly")
        changed = True
    if getattr(args, "concurrent", None) is not None:
        profile.concurrent = _validate_concurrent(args.concurrent)
        changed = True
    if getattr(args, "cpu", None) is not None:
        profile.cpu_pct = int(args.cpu)
        changed = True
    if getattr(args, "gpu", None) is not None:
        profile.gpu_pct = int(args.gpu)
        changed = True
    if getattr(args, "mem", None) is not None:
        profile.mem_pct = int(args.mem)
        changed = True
    if getattr(args, "yield_enabled", None) is not None:
        profile.yield_enabled = args.yield_enabled
        changed = True
    try:
        resource_caps_valid(profile.cpu_pct, profile.gpu_pct, profile.mem_pct)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if changed or getattr(args, "clear_schedule", False) or getattr(args, "schedule", None) \
            or getattr(args, "schedule_json", None):
        store.save_profile(profile)
    limiter = Limiter(store, detector=YieldDetector(enabled=profile.yield_enabled))
    payload = {
        "profile": profile.to_dict(),
        "remaining": limiter.remaining(profile, store.policy()),
        "schedule_allowed_now": in_schedule(profile.schedule, utcnow())[0],
        "resource_caps": {key: {"default": default, "min": low, "max": high}
                          for key, (default, low, high) in facts.RESOURCE_CAPS.items()},
    }
    _emit(payload, as_json=_json_flag(args), text=lambda value: (
        "预设 " + str(value["profile"]["preset"]) + " · 并发 "
        + str(value["profile"]["limits"]["concurrent"])
        + " · 日 " + str(value["profile"]["limits"]["daily_tokens"])
        + " / 周 " + str(value["profile"]["limits"]["weekly_tokens"] or "未启用")
        + " / 月 " + str(value["profile"]["limits"]["monthly_tokens"] or "未启用")
        + "\n资源上限：CPU " + str(value["profile"]["resources"]["cpu_pct"]) + "% · GPU "
        + str(value["profile"]["resources"]["gpu_pct"]) + "% · 内存 "
        + str(value["profile"]["resources"]["mem_pct"]) + "%"
        + "\n让路：" + ("开" if value["profile"]["yield_enabled"] else "关")
        + "\n时段：" + ("全天" if not value["profile"]["schedule"]
                       else json.dumps(value["profile"]["schedule"], ensure_ascii=False))
        + "\n当前时段允许接单：" + ("是" if value["schedule_allowed_now"] else "否")))
    return 0


# ─────────────────────────────────────────────────────────── upstream
def _upstream_line(item: dict[str, Any]) -> str:
    """`djb upstream list` 的一行文本。

    P2（CR-20261106 客户端）：有 `upstream_kind` 才多印一段（如 `third_party_relay`），
    旧上游（A1/A2、未标注 kind）的输出**逐字不变**。
    """
    parts = [str(item.get("id")), str(item.get("access_type"))]
    kind = item.get("upstream_kind")
    if kind:
        parts.append(str(kind))
    parts.extend([str(item.get("base_url")), str(item.get("state"))])
    models = ",".join(str(model.get("id")) for model in item.get("models") or [])
    # P6：把「最近探针 / 还有多久掉容量」直接印在这一行（原来只能看 state=verified，
    # 掉容量时用户完全无感 —— E2E-10 实测「公告 qps=0 而 status 仍说接单=是」）。
    probe = _probe_status_line(item.get("models"))
    return " · ".join(parts) + " · 模型 " + models + " · " + probe


def preset_upstream_kind(config: Any) -> str | None:
    """预置档**固定**的接入类型（``extra["upstream.kind"]``；平铺与嵌套写法都认）。

    P3/P5（CR-20261106-OFFICIAL-SUPPLY-01）：官方特供版预置档要求所有聚合上游都按
    ``third_party_relay`` 接入 —— 把这一条写进预置档，比让每台机器的人记参数可靠；
    没写（普通客户端）就仍然是「B 类必须显式 ``--kind``」，不做静默默认。

    值非法 → :class:`UsageError`：配置写错要当场报，绝不能静默退回「服务端默认
    ``enterprise_gateway``」那条错路。
    """
    extra = getattr(config, "extra", None)
    if not isinstance(extra, dict):
        # 形状等价的 mapping（config.json 的 {"extra": {...}} 或 extra 本身）也认
        if not isinstance(config, dict):
            return None
        nested_extra = config.get("extra")
        extra = dict(nested_extra) if isinstance(nested_extra, dict) else dict(config)
    value = extra.get("upstream.kind")
    if value is None:
        nested = extra.get("upstream")
        if isinstance(nested, dict):
            value = nested.get("kind")
    if value is None:
        return None
    kind = str(value).strip()
    if kind not in facts.UPSTREAM_KINDS:
        raise UsageError("预置档 upstream.kind=" + repr(value) + " 不认识（可选："
                         + " / ".join(facts.UPSTREAM_KINDS)
                         + "）；请改 config.json 或删掉该键")
    return kind


def cmd_upstream(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    manager = UpstreamManager(store, credentials)
    action = getattr(args, "upstream_action", "list") or "list"
    if action == "add":
        # P3（CR-20261106-OFFICIAL-SUPPLY-01）：`access=B` 不显式给 `--kind` 时，服务端会
        # 把 kind 默认推成 `enterprise_gateway`（`app/supply/facts.py:31-32`）——中转站随即
        # 被当成「企业网关」，`ignored_kinds` / 策略分流全部判错。所以这里**拒绝静默套默认值**。
        #
        # 唯一例外是**预置档**（`profiles/official-supply/config.json` 的 `upstream.kind`）：
        # 预置档已经把 kind **固定**成 third_party_relay，此时 `--kind` 可省略；给了别的值
        # 直接拒绝（预置档是承诺，不是默认值，不能在命令行上悄悄漂走）。
        access = validate_access_type(args.access)
        kind = getattr(args, "kind", None)
        preset_kind = preset_upstream_kind(config)
        if preset_kind and access == "B":       # 预置档只约定 B 类（聚合上游）的 kind
            if kind and kind != preset_kind:
                raise UsageError("本机是预置档，接入类型已固定为 " + preset_kind
                                 + "（当前 --kind " + str(kind) + "）：要换类型请改 "
                                 "config.json 的 upstream.kind 后重试（本次未创建上游）")
            kind = preset_kind
        if access == "B" and not kind:
            raise UsageError("B 类接入必须显式 --kind（中转站用 " + facts.RELAY_KIND
                             + "）：不显式指定会被默认当成 enterprise_gateway，"
                             "策略分流与 ignored_kinds 会全部判错（本次未创建上游）")
        secret = getattr(args, "secret", None)
        if getattr(args, "secret_stdin", False):
            secret = sys.stdin.readline().strip()
        relay_profile = None
        if kind == facts.RELAY_KIND:
            relay_mgr = RelayManager(store, credentials, config=config)
            if not relay_mgr.enabled():
                raise UsageError("relay 接入开关关闭（默认关闭）：先 djb flags --enable "
                                 + facts.RELAY_FLAG + " 再添加（本次未创建上游）")
        record = manager.add(access_type=args.access, base_url=args.base_url,
                             models=args.models, secret=secret, protocol=args.protocol,
                             upstream_kind=kind)
        if kind == facts.RELAY_KIND:
            relay_mgr = RelayManager(store, credentials, config=config)
            relay_profile = relay_mgr.add(
                base_url=args.base_url, upstream_id=record.id,
                auth_scheme=getattr(args, "auth", "bearer") or "bearer",
                header_name=getattr(args, "auth_header", None),
                auth_prefix=getattr(args, "auth_prefix", None),
                tls_pin=getattr(args, "tls_pin", None), secret=secret)
        declared: dict[str, Any] = {"declared": False, "reason": "daemon 未运行（稍后 djb upstream declare --id）"}
        try:
            api = NodeApi(config.gateway_url, client_key=credentials.client_key(),
                          http=HttpClient(timeout=15.0))
            response = api.declare_upstream(manager.declare_payload(record))
            declared = {"declared": True, "response": response}
            store.update_upstream(record.id, state=str(response.get("state") or "declared"))
            # 服务端自生成的 upstream_id 与本地 id 不同；体检上报必须带服务端 id
            # （否则 POST /node/upstream/probe 恒 404 NodeNotFound，上游永远停在 declared）。
            server_upstream_id = response.get("upstream_id")
            if isinstance(server_upstream_id, str) and server_upstream_id:
                mapping = store.get_kv("upstream_server_ids", default={}) or {}
                mapping[str(record.id)] = server_upstream_id
                store.set_kv("upstream_server_ids", mapping)
        except DjbError as exc:
            declared = {"declared": False, "reason": exc.message}
        payload = {"upstream": manager.store.upstream(record.id).to_dict(),
                   "relay": relay_profile.to_dict() if relay_profile else None,
                   "declare": declared,
                   "credential_stored": bool(secret),
                   "credential_note": "凭据只在 0600 本机文件；申报只上行 credential_handle"}
        _emit(payload, as_json=_json_flag(args), text=lambda value: (
            "已添加上游 " + str(value["upstream"]["id"]) + "（"
            + str(value["upstream"]["access_type"]) + " · "
            + str(value["upstream"]["base_url"]) + "）\n"
            + ("已申报：" + json.dumps(value["declare"].get("response"), ensure_ascii=False)
               if value["declare"].get("declared")
               else "暂未申报：" + str(value["declare"].get("reason")))))
        return 0
    if action == "list":
        records = [item.to_dict() for item in manager.list()]
        _emit({"upstreams": records, "count": len(records)}, as_json=_json_flag(args),
              text=lambda value: "\n".join(
                  [_upstream_line(item) for item in value["upstreams"]])
              or "（没有上游）")
        return 0
    if action == "probe":
        record = store.upstream(args.id)
        result = manager.probe(record.id, level=getattr(args, "level", None))
        details = manager.probe_details(record.id)
        try:
            api = NodeApi(config.gateway_url, client_key=credentials.client_key(),
                          http=HttpClient(timeout=15.0))
            report = dict(result)
            report.pop("upstream_id", None)
            mapping = store.get_kv("upstream_server_ids", default={}) or {}
            server_upstream_id = mapping.get(str(record.id), record.id)
            api.report_probe({"upstream_id": server_upstream_id, **report})
            result["reported"] = True
        except DjbError as exc:
            result["reported"] = False
            result["report_error"] = exc.message
        payload = {**result, "local_details": details}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("探测 " + str(value["upstream_id"]) + "："
                                  + ("可达" if value["reachable"] else "不可达")
                                  + " · " + str(value["latency_ms"]) + " ms"
                                  + (" · " + str(value["error_class"])
                                     if value.get("error_class") else "")
                                  + " · 通过 " + str(sum(1 for item in value["model_checks"]
                                                          if item.get("ok"))) + "/"
                                  + str(len(value["model_checks"])) + " 模型（L2/L3）"))
        return 0

    if action == "discover":
        relay_mgr = RelayManager(store, credentials, config=config)
        if not relay_mgr.enabled():
            raise UsageError("relay 接入开关关闭（默认关闭）：先 djb flags --enable "
                             + facts.RELAY_FLAG)
        registry = None
        if getattr(args, "registry", None):
            registry = [item.strip() for item in str(args.registry).split(",") if item.strip()]
        result = relay_mgr.discover(args.id, registry=registry)
        payload = {"upstream_id": args.id, "discovered": result.to_dict(),
                   "aliases": relay_mgr.load(args.id).aliases if relay_mgr.load(args.id) else {}}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("发现 " + str(len(value["discovered"].get("models") or []))
                                  + " 个模型；已映射 "
                                  + str(len(value["aliases"] or {}))
                                  + " 个。未映射模型不公告，用 djb upstream map 绑定"))
        return 0

    if action == "map":
        relay_mgr = RelayManager(store, credentials, config=config)
        if not relay_mgr.enabled():
            raise UsageError("relay 接入开关关闭（默认关闭）：先 djb flags --enable "
                             + facts.RELAY_FLAG)
        profile = relay_mgr.map(args.id, args.from_model, args.to_model,
                                replace=bool(getattr(args, "replace", False)))
        _emit({"upstream_id": args.id, "aliases": profile.aliases},
              as_json=_json_flag(args),
              text=lambda value: "已映射：" + str(args.from_model) + " → " + str(args.to_model))
        return 0

    if action == "unmap":
        relay_mgr = RelayManager(store, credentials, config=config)
        if not relay_mgr.enabled():
            raise UsageError("relay 接入开关关闭（默认关闭）：先 djb flags --enable "
                             + facts.RELAY_FLAG)
        profile = relay_mgr.unmap(args.id, args.from_model)
        _emit({"upstream_id": args.id, "aliases": profile.aliases},
              as_json=_json_flag(args),
              text=lambda value: "已取消映射：" + str(args.from_model))
        return 0

    if action == "relay":
        relay_mgr = RelayManager(store, credentials, config=config)
        profiles = [item.to_dict() for item in relay_mgr.list()]
        payload = {"enabled": relay_mgr.enabled(), "profiles": profiles,
                   "count": len(profiles)}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("relay " + ("已启用" if value["enabled"] else "关闭（只读）")
                                  + "；" + str(value["count"]) + " 个配置"))
        return 0
    if action in ("enable", "disable"):
        record = manager.set_model_enabled(args.id, args.model, action == "enable")
        _emit(record.to_dict(), as_json=_json_flag(args),
              text=lambda value: "模型 " + str(args.model) + " 已"
              + ("启用" if action == "enable" else "停用"))
        return 0
    if action == "price":
        record = manager.set_model_price(args.id, args.model, args.min_price)
        _emit(record.to_dict(), as_json=_json_flag(args),
              text=lambda value: "模型 " + str(args.model) + " 最低价设为 "
              + str(args.min_price))
        return 0
    if action == "remove":
        if not getattr(args, "yes", False):
            raise UsageError("删除上游会同时删除本地凭据：加 --yes 确认")
        removed = manager.remove(args.id)
        print("已删除上游" if removed else "上游不存在")
        return 0
    if action == "a2-prompt":
        if getattr(args, "id", None):
            record = store.upstream(args.id)
            model = record.model_ids()[0] if record.model_ids() else ""
            fields = {"base_url": record.base_url, "model": model,
                      "credential_ref": record.credential_ref or "",
                      "concurrency_limit": store.profile().concurrent}
        else:
            fields = {"base_url": validate_base_url(args.base_url),
                      "model": (parse_models(args.models)[0].id if getattr(args, "models", None)
                                else ""),
                      "credential_ref": "upstream:<id>",
                      "concurrency_limit": store.profile().concurrent}
        result = a2_prompt(base_url=fields["base_url"], model=fields["model"],
                           credential_ref=str(fields["credential_ref"]),
                           concurrency_limit=int(fields["concurrency_limit"]))
        _emit(result, as_json=_json_flag(args), text=lambda value: value["text"])
        return 0
    if action == "declare":
        record = store.upstream(args.id)
        api = NodeApi(config.gateway_url, client_key=credentials.require_client_key(),
                      http=HttpClient(timeout=15.0))
        response = api.declare_upstream(manager.declare_payload(record))
        store.update_upstream(record.id, state=str(response.get("state") or "declared"))
        _emit(response, as_json=_json_flag(args),
              text=lambda value: "已申报：" + json.dumps(value, ensure_ascii=False))
        return 0
    raise UsageError("未知 upstream 子命令：" + action)


# ─────────────────────────────────────────────────────────── token / flags / attest
def cmd_token(args: argparse.Namespace) -> int:
    """本地 token 计数（C8）：零网络、零落盘正文；估算值显式标 estimated。"""
    paths = _paths(args)
    action = getattr(args, "token_action", "count") or "count"
    if action != "count":
        raise UsageError("未知 token 子命令：" + action)
    model = str(getattr(args, "model", "") or "").strip()
    if not model:
        raise UsageError("缺少 --model")
    messages = None
    tools = None
    if getattr(args, "messages", None):
        try:
            messages = json.loads(args.messages)
        except json.JSONDecodeError as exc:
            raise UsageError("--messages 不是合法 JSON：" + str(exc)) from exc
    if getattr(args, "tools", None):
        try:
            tools = json.loads(args.tools)
        except json.JSONDecodeError as exc:
            raise UsageError("--tools 不是合法 JSON：" + str(exc)) from exc
    if messages is not None:
        result = tokens_mod.count_messages(model, messages, tools)
        source = "messages"
    else:
        text = getattr(args, "text", None)
        if getattr(args, "file", None):
            try:
                with open(args.file, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                raise UsageError("读取 --file 失败：" + str(exc)) from exc
        if text is None and not sys.stdin.isatty():
            text = sys.stdin.read()
        result = tokens_mod.count_for_model(model, text or "")
        source = "text"
    payload = {"model": model, "source": source, **result.as_dict()}
    _emit(payload, as_json=_json_flag(args),
          text=lambda value: (str(value["tokens"]) + " tokens · " + str(value["tokenizer_id"])
                              + ("（估算）" if value["estimated"] else "（精确）")))
    return 0


# ─────────────────────────────────────────── plan（多 plan 组 / 组级共享预算）
def _plan_flag_state(config: ClientConfig) -> dict[str, Any]:
    """当前 ``plan.groups`` 开关状态（写操作也必须报「现在生效吗」）。"""
    spec = facts.FEATURE_FLAGS.get(facts.PLAN_GROUPS_FLAG) or {}
    enabled = feature_flag(config, facts.PLAN_GROUPS_FLAG)
    return {"name": facts.PLAN_GROUPS_FLAG, "enabled": enabled,
            "default": bool(spec.get("default", False)),
            "owner": spec.get("owner"), "expire_at": spec.get("expire_at"),
            "note": ("开关已开启（重启 daemon 后生效）" if enabled else
                     "开关关闭：配置照常保存，但**当前不生效**；"
                     "djb flags --enable " + facts.PLAN_GROUPS_FLAG + " 后重启 daemon 生效")}


def _plan_group_text(group: dict[str, Any], now: Any = None) -> str:
    """一个组的人类可读摘要（CLI 文本输出用）。"""
    view = plan_router.budget_view(group, now)
    members = ", ".join(
        str(item.get("upstream_id")) + "(p" + str(item.get("priority", 0))
        + "/w" + str(item.get("weight", 1)) + ("" if item.get("enabled", True) else "/关")
        + ")" for item in view["members"]) or "（没有成员）"
    return (str(view["group"]) + " · priority " + str(view["priority"])
            + ("" if view["enabled"] else " · 组已关")
            + " · 日 " + str(view["spent_day_usd"]) + "/" + str(view["budget_day_usd"])
            + " · 月 " + str(view["spent_month_usd"]) + "/" + str(view["budget_month_usd"])
            + (" · ⛔ 预算耗尽（" + str(view["budget_exhausted"]) + "）"
               if view["budget_exhausted"] else "")
            + " · token 日/月 " + str(view["spent_tokens_day"]) + "/"
            + str(view["spent_tokens_month"])
            + (" · 无 usage 请求 " + str(view["unknown_usage_count"])
               if view["unknown_usage_count"] else "")
            + (" · 允许降级" if view["allow_model_downgrade"] else "")
            + "\n    成员：" + members)


def _plan_minor(value: Any) -> int:
    """CLI 金额字符串 -> minor；非法输入按用法错误（退出码 2）。"""
    try:
        return plan_router.parse_usd_minor(value)
    except ValueError as exc:
        raise UsageError(str(exc))


def cmd_plan(args: argparse.Namespace) -> int:
    """多 plan 组 / 组级共享预算：只动本机 state.db，不碰凭据、不发请求。"""
    paths, config, store = _context(args)
    action = getattr(args, "plan_action", "group.list") or "group.list"
    flag = _plan_flag_state(config)
    now = utcnow()
    note = "\n" + str(flag["note"]) if action != "group.list" and action != "budget.show" else ""
    show_note = "\n" + str(flag["note"])

    if action == "group.add":
        day = None if getattr(args, "no_day", False) else (
            _plan_minor(args.day) if getattr(args, "day", None) else None)
        month = None if getattr(args, "no_month", False) else (
            _plan_minor(args.month) if getattr(args, "month", None) else None)
        group = store.add_plan_group(
            name=args.name, budget_day_minor=day, budget_month_minor=month,
            priority=int(getattr(args, "priority", 100) or 100),
            enabled=not getattr(args, "disabled", False),
            allow_model_downgrade=bool(getattr(args, "allow_downgrade", False)),
            model_fallbacks=getattr(args, "fallback", None) or (), now=now)
        payload = {"action": action, "group": plan_router.budget_view(group, now), "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("已建组 " + str(value["group"]["group"]) + "（id "
                                  + str(value["group"]["id"]) + "）\n"
                                  + _plan_group_text(group, now) + note))
        return 0

    if action == "group.list":
        groups = store.plan_groups()
        payload = {"action": action, "count": len(groups),
                   "groups": [plan_router.budget_view(item, now) for item in groups],
                   "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("\n".join(_plan_group_text(item, now) for item in groups)
                                 if groups else "（没有 plan 组）") + show_note)
        return 0

    if action == "group.remove":
        removed = store.remove_plan_group(args.group)
        payload = {"action": action, "group": args.group, "removed": removed, "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: (("已删组 " + str(value["group"])) if removed
                                  else ("组不存在，未改动：" + str(value["group"]))))
        return 0

    if action == "group.add-upstream":
        group = store.add_plan_group_member(
            args.group, upstream_id=args.id,
            priority=int(getattr(args, "priority", 100) or 100),
            weight=int(getattr(args, "weight", 1) or 1),
            enabled=not getattr(args, "disabled", False), now=now)
        payload = {"action": action, "group": plan_router.budget_view(group, now), "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("已加入 " + str(args.id) + " → "
                                  + str(value["group"]["group"]) + "\n"
                                  + _plan_group_text(group, now) + note))
        return 0

    if action == "group.remove-upstream":
        removed = store.remove_plan_group_member(args.group, args.id, now=now)
        payload = {"action": action, "group": args.group, "upstream_id": args.id,
                   "removed": removed, "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: (("已移出 " + str(args.id) + " ← " + str(value["group"]))
                                  if removed else
                                  ("该上游不在组里，未改动：" + str(args.id))))
        return 0

    if action == "budget.set":
        if not getattr(args, "group", None):
            raise UsageError("budget set 需要 --group（组名或 id）")
        day: Any = store_module.UNSET
        month: Any = store_module.UNSET
        downgrade: Any = store_module.UNSET
        fallbacks: Any = store_module.UNSET
        if getattr(args, "no_day", False):
            day = None
        elif getattr(args, "day", None):
            day = _plan_minor(args.day)
        if getattr(args, "no_month", False):
            month = None
        elif getattr(args, "month", None):
            month = _plan_minor(args.month)
        if getattr(args, "allow_downgrade", False):
            downgrade = True
        if getattr(args, "no_downgrade", False):
            downgrade = False
        if getattr(args, "fallback", None) is not None:
            fallbacks = args.fallback
        if (day is store_module.UNSET and month is store_module.UNSET
                and downgrade is store_module.UNSET and fallbacks is store_module.UNSET):
            raise UsageError("没给要改的东西：--day/--month/--no-day/--no-month/"
                             "--allow-downgrade/--no-downgrade/--fallback 至少一个")
        group = store.set_plan_budget(args.group, day_minor=day, month_minor=month,
                                      allow_model_downgrade=downgrade,
                                      model_fallbacks=fallbacks, now=now)
        payload = {"action": action, "group": plan_router.budget_view(group, now), "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("已更新预算 " + str(value["group"]["group"]) + "\n"
                                  + _plan_group_text(group, now) + note))
        return 0

    if action == "budget.show":
        key = getattr(args, "group", None)
        groups = [store.plan_group(key)] if key else store.plan_groups()
        payload = {"action": action, "count": len(groups),
                   "groups": [plan_router.budget_view(item, now) for item in groups],
                   "flag": flag}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("\n".join(_plan_group_text(item, now) for item in groups)
                                  if groups else "（没有 plan 组）")
              + "\n金额口径：minor（USD x 100 整数）；拿不到单价只记 token、"
                "没有 usage 只计 unknown_usage_count；切换与耗尽都会写事件日志。" + show_note)
        return 0

    raise UsageError("未知的 plan 动作：" + str(action))


def cmd_flags(args: argparse.Namespace) -> int:
    """查看 / 设置本机特性开关（默认关闭；只允许登记过的开关）。"""
    paths, config, store = _context(args)
    enable = getattr(args, "enable", None)
    disable = getattr(args, "disable", None)
    changed = None
    if enable and disable:
        raise UsageError("--enable 与 --disable 只能二选一")
    name = enable or disable
    if name:
        if name not in facts.FEATURE_FLAGS:
            raise UsageError("未登记的开关：" + name + "（登记表见 facts.FEATURE_FLAGS）")
        extra = dict(config.extra or {})
        current = extra.get("feature_flags")
        flags = dict(current) if isinstance(current, dict) else {}
        flags[name] = bool(enable)
        extra["feature_flags"] = flags
        config.extra = extra
        save_config(paths, config)
        changed = {"name": name, "value": bool(enable)}
    rows = []
    for flag_name, spec in facts.FEATURE_FLAGS.items():
        rows.append({"name": flag_name,
                     "enabled": feature_flag(config, flag_name),
                     "default": bool(spec.get("default", False)),
                     "owner": spec.get("owner"),
                     "expire_at": spec.get("expire_at"),
                     "rollback_rule": spec.get("rollback_rule"),
                     "description": spec.get("description")})
    payload = {"flags": rows, "changed": changed, "count": len(rows)}
    _emit(payload, as_json=_json_flag(args),
          text=lambda value: "\n".join(
              ("✅ " if item["enabled"] else "⛔ ") + item["name"]
              + "  owner=" + str(item["owner"]) + "  expire=" + str(item["expire_at"])
              for item in value["flags"]) + (
              "\n（默认关闭；开启需 owner/expire_at/rollback_rule 齐全，到期强制关）"
              if not value["changed"] else
              "\n已" + ("开启 " if value["changed"]["value"] else "关闭 ")
              + value["changed"]["name"] + "（重启 daemon 生效）"))
    return 0


def cmd_attest(args: argparse.Namespace) -> int:
    """查看本地模型身份证明记录（客户端证据只作线索，不自我宣称 verified）。"""
    paths, config, store = _context(args)
    manager = AttestationManager(store, config=config)
    upstream_id = getattr(args, "id", None)
    model = getattr(args, "model", None)
    if upstream_id and model:
        payload = {"attestation": manager.status(upstream_id, model)}
    else:
        rows = manager.list()
        if upstream_id:
            rows = [item for item in rows if item.get("upstream_id") == upstream_id]
        payload = {"enabled": manager.enabled(), "records": rows, "count": len(rows)}
    _emit(payload, as_json=_json_flag(args),
          text=lambda value: ("证明开关：" + ("开" if value.get("enabled") else "关")
                              + ("；" + str(value.get("count", 1)) + " 条记录"
                                 if "records" in value else ""))
          + ("\n" + "\n".join(str(item.get("model")) + " · L" + str(item.get("level"))
                                + " · " + str(item.get("state"))
                                for item in (value.get("records") or []))
             if value.get("records") else ""))
    return 0


# ─────────────────────────────────────────────────────────── notify（C16）
def _notify_store(args: argparse.Namespace) -> tuple[Any, NotificationStore]:
    paths, config, _store = _context(args)
    credentials = CredentialStore(paths)
    api = NodeApi(config.gateway_url, client_key=credentials.client_key())
    return config, NotificationStore(paths, config=config, api=api)


def cmd_notify(args: argparse.Namespace) -> int:
    """消息中心：list / read / ack / unread / sync（本地优先；离线读态进队列不丢）。"""
    config, store = _notify_store(args)
    action = getattr(args, "notify_action", "list") or "list"
    as_json = _json_flag(args)
    if action == "list":
        rows = store.list(category=getattr(args, "category", None),
                          unread_only=bool(getattr(args, "unread_only", False)),
                          limit=int(getattr(args, "limit", 0)
                                    or facts.NOTIFICATIONS_DEFAULT_LIMIT))
        payload = {"items": rows, "count": len(rows), "unread": store.unread_count(),
                   "enabled": notifications_enabled(config)}
        _emit(payload, as_json=as_json,
              text=lambda value: ("\n".join(
                  ("• " if item["unread"] else "  ") + item["severity"] + " "
                  + item["created_at"] + " " + item["category"] + " "
                  + item["template_key"] + " (" + item["id"] + ")"
                  for item in value["items"])
                  + ("\n" if value["items"] else "") + "未读 " + str(value["unread"]) + " 条"))
        return 0
    if action in ("read", "ack"):
        notification_id = str(getattr(args, "id", "") or "").strip()
        if not notification_id:
            raise UsageError("缺 --id")
        result = store.mark_read(notification_id) if action == "read" \
            else store.ack(notification_id)
        payload = {"action": action, **result}
        _emit(payload, as_json=as_json,
              text=lambda value: (("已标记" + ("已读 " if value["action"] == "read"
                                              else "确认 "))
                                  + value["id"]
                                  + ("（已上报）" if value["synced"]
                                     else "（离线：已排队，联网后自动补发）")))
        return 0
    if action == "unread":
        local = store.unread_count()
        remote = store.server_unread_count()
        payload = {"unread": local, "server_unread": remote,
                   "pending": len(store.pending_acks()),
                   "enabled": notifications_enabled(config)}
        _emit(payload, as_json=as_json,
              text=lambda value: ("未读 " + str(value["unread"]) + " 条"
                                  + ("（服务端 " + str(value["server_unread"]) + "）"
                                     if value["server_unread"] is not None else "")
                                  + ("；待补发 " + str(value["pending"])
                                     if value["pending"] else "")))
        return 0
    if action == "sync":
        result = store.sync()
        flushed = store.flush()
        payload = {"action": "sync", **result, "flush": flushed,
                   "unread": store.unread_count()}
        _emit(payload, as_json=as_json,
              text=lambda value: ("同步完成：新增 " + str(value["added"])
                                  + "，更新 " + str(value["updated"])
                                  + ("，过期跳过 " + str(value["skipped_expired"])
                                     if value["skipped_expired"] else "")
                                  + ("；失败 " + str(value["error_class"])
                                     if value.get("error_class") else "")
                                  + "；未读 " + str(value["unread"])))
        return 0
    raise UsageError("未知 notify 子命令：" + str(action))


# ─────────────────────────────────────────────────────────── model-sync（M18b）
def cmd_model_sync(args: argparse.Namespace) -> int:
    """模型 / tokenizer 同步：``status`` 只读本地；``sync`` 手动拉一次（不打印 key/token）。"""
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    action = getattr(args, "model_sync_action", "status") or "status"
    client = ModelSyncClient(paths, config, credentials,
                             api=NodeApi(config.gateway_url,
                                         client_key=credentials.client_key()))
    if action == "sync":
        outcome = client.sync()
        payload = {"action": "sync", "enabled": model_sync_enabled(config), **outcome,
                   "status": client.status()}
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: ("同步完成：版本 " + str(value["applied_version"])
                                  + ("（已更新）" if value["changed"] else "（无变化）")
                                  + ("；错误 " + str(value["error_class"])
                                     if value.get("error_class") else "")
                                  + "；本地模型 " + str(value["status"]["models"]) + " 个"
                                  + ("；unsupported " + str(len(value["status"]["unsupported"]))
                                     if value["status"]["unsupported"] else "")))
        return 0
    if action != "status":
        raise UsageError("未知 model-sync 子命令：" + action)
    status = client.status()
    _emit(status, as_json=_json_flag(args),
          text=lambda value: ("model-sync 开关：" + ("开" if value["enabled"] else "关（默认）")
                              + "；当前版本 " + str(value["current_version"])
                              + "；模型 " + str(value["models"]) + " 个"
                              + ("；unsupported " + str(len(value["unsupported"]))
                                 if value["unsupported"] else "")
                              + ("；漂移 " + str(len(value["drift"])) if value["drift"] else "")
                              + ("；最近错误 " + str(value["last_error"])
                                 if value["last_error"] else "")))
    return 0


# ─────────────────────────────────────────────────────────── announce
def cmd_announce(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    profile = store.profile()
    manager = UpstreamManager(store, credentials)
    relay_mgr = RelayManager(store, credentials, config=config)
    relay_targets = capacity_targets(relay_mgr.list()) if relay_mgr.enabled() else {}
    entries = manager.announce_capacity(profile, relay=relay_targets)
    if not entries:
        raise UsageError("没有可公告的模型：先 djb upstream add / enable")
    if getattr(args, "dry_run", False):
        _emit({"capacity": entries, "dry_run": True}, as_json=_json_flag(args),
              text=lambda value: json.dumps(value["capacity"], ensure_ascii=False, indent=2))
        return 0
    api = NodeApi(config.gateway_url, client_key=credentials.require_client_key(),
                  http=HttpClient(timeout=20.0))
    policy = store.policy()
    snapshot = Snapshot.from_dict(store.get_kv("daemon_state"))
    payload = {
        "node_id": credentials.node_id() or "",
        "protocol_version": facts.PROTOCOL_VERSION,
        "policy_version": policy.policy_version if policy else "",
        "runtime_state": snapshot.runtime_state(),
        "capacity": entries,
        "load": {"inflight": 0, "queue_seconds": 0.0},
    }
    result = api.heartbeat(payload)
    announcement = result.get("announcement")
    if isinstance(announcement, dict):
        seq = announcement.get("seq")
        if isinstance(seq, int):
            store.record_announcement(seq, announcement)
    _emit({"capacity": entries, "response": result}, as_json=_json_flag(args),
          text=lambda value: ("已公告 " + str(len(value["capacity"])) + " 个模型；"
                              + ("服务端受理" if value["response"].get("accepted", True)
                                 else "服务端未受理")))
    return 0


# ─────────────────────────────────────────────────────────── quotes（报价发布）
#: `djb quotes show / publish` 的人读表头（只有本地可见字段）
_QUOTES_COLUMNS = ("上游", "access", "模型", "单价(USD分/1M)", "TTL(s)", "unit")


def _display_width(text: str) -> int:
    """终端显示宽度（东亚全角按 2 列算，只为表格对得齐）。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def _pad_cell(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _quotes_table(value: dict[str, Any]) -> str:
    """报价行的人读表格 + 一行脚注（周期 / TTL / 跳过原因计数）。"""
    rows = list(value.get("rows") or [])
    table: list[list[str]] = []
    for row in rows:
        table.append([str(row.get("upstream_id") or ""), str(row.get("access") or ""),
                      str(row.get("model") or ""), str(row.get("price_min")),
                      str(row.get("ttl_s")), str(row.get("unit") or "")])
    header = list(_QUOTES_COLUMNS)
    widths = [max([_display_width(header[index])]
                  + [_display_width(item[index]) for item in table])
              for index in range(len(header))]
    lines = ["  ".join(_pad_cell(cell, widths[index]) for index, cell in enumerate(header)),
             "  ".join("-" * width for width in widths)]
    for item in table:
        lines.append("  ".join(_pad_cell(cell, widths[index])
                              for index, cell in enumerate(item)))
    footer = ("周期 " + str(value.get("period_s")) + "s · TTL " + str(value.get("ttl_s"))
              + "s · unit " + str(value.get("unit")) + " · 共 " + str(len(rows)) + " 行")
    skipped = value.get("skipped") or {}
    if skipped:
        footer += " · 跳过：" + "，".join(name + "=" + str(count)
                                        for name, count in sorted(skipped.items()))
    lines.append(footer)
    return "\n".join(lines)


def _quotes_skip_summary(plan: Any) -> str:
    skipped = dict(getattr(plan, "skipped", None) or {})
    if not skipped:
        return "本机没有启用中的上游模型"
    return "跳过原因 " + "，".join(name + "=" + str(count)
                                  for name, count in sorted(skipped.items()))


def cmd_quotes(args: argparse.Namespace) -> int:
    """报价能力（CR-20261106-OFFICIAL-SUPPLY-01）：`show` 看将发送的行，`publish` 真发。

    * `show` / `--dry-run` **不联网**、不需要开关、不需要凭据；
    * `publish` 需要（1）开关 `client.quotes.publish.enabled` 打开、
      （2）本机已用 `djb auth` 拿到节点密钥 —— 报价必须挂在**自己的**节点身份上；
    * TTL 一律 = 2 × 发布周期（P2），周期取 `--period-s` > config `quotes.period_s` > 60。
    """
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    manager = UpstreamManager(store, credentials)
    action = getattr(args, "quotes_action", None) or "show"
    period_s = quotes_mod.resolve_period(config, period_s=getattr(args, "period_s", None))
    plan = quotes_mod.plan_quotes(manager, now=utcnow(), period_s=period_s,
                                  upstream_id=getattr(args, "id", None))
    if action == "show":
        _emit(plan.to_dict(), as_json=_json_flag(args), text=_quotes_table)
        return 0
    if action != "publish":
        raise UsageError("未知 quotes 动作：" + str(action))
    if not plan.rows:
        raise UsageError("没有可报价的模型：" + _quotes_skip_summary(plan)
                         + "；先 djb upstream probe 体检，"
                         "再用 djb upstream price --id <id> --model <model> "
                         "--min-price <USD/1M> 定价")
    if getattr(args, "dry_run", False):
        payload = dict(plan.to_dict())
        payload["dry_run"] = True
        payload["requests"] = [row.payload() for row in plan.rows]
        _emit(payload, as_json=_json_flag(args),
              text=lambda value: _quotes_table(value) + "\n（--dry-run：只打印，未发送）")
        return 0
    if not feature_flag(config, facts.QUOTES_FLAG):
        raise UsageError("报价发布开关关闭（默认关闭）：先 djb flags --enable "
                         + facts.QUOTES_FLAG + "（本次未发送任何报价）")
    api = NodeApi(config.gateway_url, client_key=credentials.require_client_key(),
                  http=HttpClient(timeout=20.0))
    result = quotes_mod.publish(api, plan.rows, node_id=credentials.node_id() or None)
    payload = dict(plan.to_dict())
    payload["result"] = result.to_dict()
    payload["dry_run"] = False

    def _publish_text(value: dict[str, Any]) -> str:
        outcome = value.get("result") or {}
        text = ("已发布 " + str(outcome.get("total")) + " 行：新建 "
                + str(outcome.get("created")) + " · 复用 " + str(outcome.get("reused"))
                + " · 失败 " + str(outcome.get("failed"))
                + "（周期 " + str(value.get("period_s")) + "s · TTL "
                + str(value.get("ttl_s")) + "s）")
        errors = list(outcome.get("errors") or [])
        if errors:
            text += "\n失败明细：" + "；".join(
                str(item.get("upstream_id")) + "/" + str(item.get("model")) + " → "
                + str(item.get("error")) + "(" + str(item.get("code")) + ")"
                for item in errors[:5])
            if len(errors) > 5:
                text += " …（共 " + str(len(errors)) + " 条，用 --json 看全量）"
        return text

    _emit(payload, as_json=_json_flag(args), text=_publish_text)
    # 单行失败不影响其它行（已在 publish 内隔离），但退出码要如实反映「有失败」
    return 0 if result.ok else facts.EXIT_ERROR


# ─────────────────────────────────────────────────────────── update
def cmd_update(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    source = getattr(args, "source", None)
    manager = UpdateManager(paths, store,
                            source=source if source is not None else config.update_source,
                            channel=getattr(args, "channel", None) or config.update_channel,
                            edition=config.edition,
                            http=HttpClient(timeout=20.0),
                            # 开关求值需要 config（mock 验签三条件之一）；生产路径用不到
                            config=config)
    if getattr(args, "status", False):
        _emit(manager.status(), as_json=_json_flag(args),
              text=lambda value: json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if getattr(args, "rollback", False):
        result = manager.rollback()
        _emit(result, as_json=_json_flag(args),
              text=lambda value: ("已回滚到 " + str(value.get("to_version"))
                                  if value.get("rolled_back")
                                  else "未回滚：" + str(value.get("reason"))))
        return 0 if result.get("rolled_back") else facts.EXIT_CONFLICT
    if getattr(args, "apply", False):
        result = manager.apply()
        _emit(result, as_json=_json_flag(args),
              text=lambda value: (("已安装 " + str(value.get("version"))
                                   + "（上一版 " + str(value.get("previous_version")) + "）")
                                  if value.get("installed")
                                  else "未安装：" + str(value.get("state")) + " · "
                                  + str(value.get("message"))))
        # RC8：统一映射（failed_verify→7、download_failed→6、其它→0）
        return apply_exit_code(result)
    result = manager.check()
    _emit(result, as_json=_json_flag(args),
          text=lambda value: ("更新：" + str(value.get("state"))
                              + " · 当前 " + str(value.get("current_version"))
                              + (" · 可用 " + str(value.get("manifest_version"))
                                 if value.get("manifest_version") else "")
                              + (" · " + str(value.get("message"))
                                 if value.get("message") else "")
                              + ("\n下一步：djb update --apply" if value.get("state") == "eligible"
                                 else "")))
    return facts.EXIT_VERIFY if result.get("state") == "failed_verify" else 0


# ─────────────────────────────────────────────────────────── machine（M3）
def cmd_machine(args: argparse.Namespace) -> int:
    """机器评估：CPU / 内存 / 磁盘 / GPU / 负载 + 可选 tokens/s 基准。

    输出口径 = qa-e2e 钉死的 `djb.machine-eval/1`（`ops/e2e/qa/modules/m03_machine.py`）。
    拿不到的字段写 null + unavailable_reason，**绝不写 0 充数**。
    """
    from . import machine_profile
    paths = _paths(args)
    benchmark = not getattr(args, "no_benchmark", False)
    mode = getattr(args, "benchmark_mode", "synthetic") or "synthetic"
    doc = machine_profile.build_machine_eval(
        home=paths.home, benchmark=benchmark, benchmark_mode=mode,
        ollama_url=getattr(args, "ollama_url", None),
        ollama_model=getattr(args, "ollama_model", None))
    written: str | None = None
    out = getattr(args, "out", None)
    if getattr(args, "e2e", False):
        out = out or machine_profile.default_eval_path()
    if out:
        written = machine_profile.write_machine_eval(out, doc)
    if _json_flag(args):
        if written:
            doc = dict(doc, written_path=written)
        _emit(doc, as_json=True, text=lambda value: json.dumps(value, ensure_ascii=False,
                                                               indent=2))
        return 0
    _emit(doc, as_json=False,
          text=lambda value: machine_profile.machine_eval_text(value))
    if written:
        print("已写入：" + written + "（0600）")
    return 0


# ─────────────────────────────────────────────────────────── logs
def cmd_logs(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if not os.path.isdir(paths.logs_dir):
        raise NotInstalledError("本机未初始化：先执行 djb init")
    prune_old_logs(paths.logs_dir)
    files = _log_files(paths.logs_dir)
    if not files:
        print("（还没有日志）")
        return 0
    tail = int(getattr(args, "tail", 50) or 50)
    level = (getattr(args, "level", None) or "").lower()
    if getattr(args, "follow", False):
        return _follow_logs(files, tail=tail, level=level)
    lines = _read_tail(files, tail)
    if level:
        lines = [line for line in lines if _line_level(line) == level]
    for line in lines:
        print(redact_text(line.rstrip("\n")))
    return 0


def _log_files(directory: str) -> list[str]:
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith(facts.LOG_FILE))
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names]


def _read_tail(files: list[str], tail: int) -> list[str]:
    chunks: list[str] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                chunks.extend(handle.readlines())
        except OSError:
            continue
    return chunks[-tail:]


def _line_level(line: str) -> str:
    try:
        payload = json.loads(line)
    except ValueError:
        return ""
    return str(payload.get("level") or "")


def _follow_logs(files: list[str], *, tail: int, level: str) -> int:
    seen = {path: os.path.getsize(path) if os.path.exists(path) else 0 for path in files}
    lines = _read_tail(files, tail)
    if level:
        lines = [line for line in lines if _line_level(line) == level]
    for line in lines:
        print(redact_text(line.rstrip("\n")), flush=True)
    try:
        while True:
            time.sleep(1.0)
            for path in _log_files(os.path.dirname(files[0]) if files else "."):
                if path not in seen:
                    seen[path] = 0
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size <= seen[path]:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(seen[path])
                    for line in handle:
                        if level and _line_level(line) != level:
                            continue
                        print(redact_text(line.rstrip("\n")), flush=True)
                seen[path] = size
    except KeyboardInterrupt:
        return 0


# ─────────────────────────────────────────────────────────── diagnose
def cmd_diagnose(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    builder = DiagnoseBuilder(paths, store, credentials, config)
    try:
        result = builder.build(getattr(args, "output", None))
    except DiagnoseError as exc:
        print("djb: " + exc.message, file=sys.stderr)
        return facts.EXIT_VERIFY
    _emit(result, as_json=_json_flag(args),
          text=lambda value: ("诊断包已生成：" + str(value["path"]) + "\n"
                              + "大小：" + str(value["bytes"]) + " 字节 · 文件 "
                              + str(len(value["files"])) + " 个\n"
                              + "脱敏：0 命中（已屏蔽 "
                              + str(value["redaction"]["exact_secret_values_masked"])
                              + " 个凭据值）；不含 prompt/response 正文"))
    return 0


# ─────────────────────────────────────────────────────────── uninstall
def cmd_uninstall(args: argparse.Namespace) -> int:
    paths = _paths(args)
    keep_logs = bool(getattr(args, "keep_logs", False))
    dry_run = bool(getattr(args, "dry_run", False))
    if not getattr(args, "yes", False) and not dry_run:
        raise UsageError("卸载会清除本机配置 / 凭据 / 数据：加 --yes 确认（或 --dry-run 预览）")
    removed: list[str] = []
    # 1) 停止 daemon
    try:
        ControlClient(paths).call({"cmd": "stop"}, timeout=5.0)
        time.sleep(0.5)
        removed.append("daemon (stopped)")
    except NotRunningError:
        pass
    # 2) 服务单元（只在内容确认是本客户端时删除）
    for unit in (os.path.expanduser("~/.config/systemd/user/djb-node.service"),
                 "/etc/systemd/system/djb-node.service"):
        if _is_our_unit(unit):
            if dry_run:
                removed.append("unit:" + unit)
            elif _safe_unlink(unit):
                removed.append("unit:" + unit)
    # 3) 数据 / 凭据 / 缓存 / 版本
    credentials = CredentialStore(paths)
    targets = [paths.credentials_file, paths.buffer_key_file,
               paths.provider_key_file,
               os.path.join(paths.keys_dir, "update_mock.key"),
               paths.db_file, paths.db_file + "-wal", paths.db_file + "-shm",
               paths.config_file, bundle_file(paths),
               # 老版本把 daemon.out 写在 runtime/（现役在 logs/）：卸载时两处都清，文件名同源
               os.path.join(paths.runtime_dir, _DAEMON_OUT_NAME)]
    if not keep_logs:
        targets.extend(_log_files(paths.logs_dir))
    versions = []
    try:
        versions = [os.path.join(paths.versions_dir, name)
                    for name in os.listdir(paths.versions_dir)]
    except OSError:
        versions = []
    targets.extend(versions)
    for target in targets:
        if os.path.exists(target):
            if dry_run:
                removed.append(target)
            elif _safe_unlink(target):
                removed.append(target)
    # 4) 目录（keep-logs 时保留 logs）
    for directory in (paths.diagnostics_dir, paths.cache_dir, paths.runtime_dir,
                      paths.keys_dir, os.path.dirname(paths.credentials_file),
                      paths.versions_dir):
        _safe_rmdir(directory)
    if not keep_logs:
        _safe_rmdir(paths.logs_dir)
    home_empty = True
    try:
        home_empty = not os.listdir(paths.home)
    except OSError:
        home_empty = True
    if home_empty and not dry_run:
        _safe_rmdir(paths.home)
    # 5) 残留自检（0 命中）；dry-run 只预览，不把「将删除」当成残留
    residual = [] if dry_run else _residual_hits(paths, keep_logs=keep_logs)
    payload = {"uninstalled": True, "dry_run": dry_run, "removed": removed,
               "residual": residual, "residual_count": len(residual),
               "home": paths.home, "logs_kept": keep_logs}
    _emit(payload, as_json=_json_flag(args),
          text=lambda value: ("预览（未删除）：" if dry_run else "已卸载。")
          + " 清理 " + str(len(value["removed"])) + " 项；残留自检 "
          + str(value["residual_count"]) + " 命中"
          + ("" if not value["residual"] else "：" + ", ".join(value["residual"])))
    return 0 if not residual else facts.EXIT_ERROR


def _is_our_unit(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return "djb_node.daemon" in handle.read()
    except OSError:
        return False


def _safe_unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def _safe_rmdir(path: str) -> bool:
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def _residual_hits(paths: Paths, *, keep_logs: bool) -> list[str]:
    """卸载残留自检：服务 / 凭据 / 数据 / 缓存 / 配置（日志可显式保留）。"""
    candidates = [paths.credentials_file, paths.buffer_key_file, paths.provider_key_file,
                  paths.db_file, paths.db_file + "-wal", paths.db_file + "-shm",
                  paths.config_file, bundle_file(paths),
                  os.path.join(paths.runtime_dir, "daemon.sock"),
                  os.path.join(paths.runtime_dir, "daemon.pid"),
                  control_socket_file(paths)]
    if not keep_logs:
        candidates.append(paths.log_file)
    candidates.extend([
        "/etc/systemd/system/djb-node.service",
        os.path.expanduser("~/.config/systemd/user/djb-node.service"),
    ])
    return [path for path in candidates if os.path.exists(path)]


# ─────────────────────────────────────────────────────────── parser
def cmd_tray(args: argparse.Namespace) -> int:
    """启动托盘壳（C3/C4）；无图形会话时 tray.main 返回 3，不抛 traceback。"""
    from .tray.app import main as tray_main
    argv = ["--interval-ms", str(getattr(args, "interval_ms", 5000))]
    title = getattr(args, "title", None)
    if title:
        argv += ["--title", str(title)]
    return tray_main(argv)


def cmd_proxy(args: argparse.Namespace) -> int:
    """打印 S4 代理偏好口径；不联网、不改 daemon。"""
    from .proxy import ProxyPreferences

    prefs = ProxyPreferences(
        effort=getattr(args, "effort", None),
        composition=getattr(args, "composition", "single"),
        proxy_mode=getattr(args, "proxy_mode", None),
    )
    payload = {"fields": prefs.fields(), "effective": prefs.describe()}
    _emit(payload, as_json=_json_flag(args),
          text=lambda item: "\n".join(
              ["请求字段: " + (", ".join(item["fields"]) or "（无，走服务端缺省）"),
               "生效口径: " + ", ".join(f"{k}={v}" for k, v in item["effective"].items())]))
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """启动 S3 共享设置窗口；无图形会话时 gui.main 返回 3，不抛 traceback。"""
    from .gui.window import main as gui_main
    argv = ["--upload-mbps", str(getattr(args, "upload_mbps", 0.0))]
    if getattr(args, "home", None):
        argv.extend(["--home", str(args.home)])
    if getattr(args, "preset", None):
        argv.extend(["--preset", str(args.preset)])
    if getattr(args, "no_realtime", False):
        argv.append("--no-realtime")
    if getattr(args, "no_batch", False):
        argv.append("--no-batch")
    if getattr(args, "network", False):
        argv.append("--network")
    return gui_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="DJB 联脑 · 共享节点客户端（M15 / S2 P1 Linux CLI + daemon）",
        epilog="退出码：" + "；".join(str(code) + "=" + text for code, text
                                     in sorted(facts.EXIT_CODE_HELP.items())))
    parser.add_argument("--home", help="本机数据目录（默认 $DJB_HOME 或 ~/.djb）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于脚本 / 托盘）")
    parser.add_argument("--version", action="version",
                        version=PROG + " " + __version__ + " (M15 client-runtime / S2 P1)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add_common(handler: Callable[[argparse.Namespace], int], name: str,
                   help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text, description=help_text)
        item.add_argument("--home", help=argparse.SUPPRESS)
        item.add_argument("--json", action="store_true", help="输出 JSON")
        item.set_defaults(handler=handler)
        return item

    boot = add_common(cmd_bootstrap, "bootstrap",
                      "首启自举：建目录/写真实云端地址/建库，并拉起节点（幂等）")
    boot.add_argument("--gateway", help="覆盖云端地址（默认取构建默认或已有配置）")
    boot.add_argument("--preset", choices=facts.PRESETS, help="本机预设（默认 省心）")
    boot.add_argument("--no-daemon", action="store_true", help="只自举，不拉起 daemon")

    init = add_common(cmd_init, "init", "初始化本机数据目录 / 预设 / 限额，并做一次环境检测")
    init.add_argument("--gateway", help="服务端地址（默认占位 api.example.invalid）")
    init.add_argument("--port", type=int, help="本机镜像端口（1024–65535，默认 1717）")
    init.add_argument("--channel", choices=facts.UPDATE_CHANNELS, help="更新频道")
    init.add_argument("--update-source", help="更新源（URL / 本地目录；留空用 mock 源）")
    init.add_argument("--edition", choices=update_source.editions(),
                      help="发行档位：落该通道的显式签名更新源（consumer/machine-room/"
                           "official-supply）；--update-source 优先级更高")
    init.add_argument("--log-level", choices=facts.LOG_LEVELS, help="日志级别")
    init.add_argument("--preset", choices=facts.PRESETS, help="预设：省心 / 均衡 / 全力以赴")
    init.add_argument("--daily-tokens", type=int, help="日配额（10000–100000000）")
    init.add_argument("--weekly-tokens", help="周配额（0=不启用）")
    init.add_argument("--monthly-tokens", help="月配额（0=不启用）")
    init.add_argument("--concurrent", type=int, help="并发上限（1–64）")
    init.add_argument("--cpu", type=int, help="CPU 上限百分比（默认 40）")
    init.add_argument("--gpu", type=int, help="GPU 上限百分比（默认 70）")
    init.add_argument("--mem", type=int, help="内存上限百分比（默认 70）")
    init.add_argument("--no-env", action="store_true", help="跳过环境检测")

    env = add_common(cmd_env, "env", "环境检测：结论 + 下一步；一次授权 / 可回滚")
    env.add_argument("--authorize", action="store_true", help="一次授权应用保守补全")
    env.add_argument("--rollback", action="store_true", help="回滚上次授权")
    env.add_argument("--budget", type=int, default=facts.ENV_CHECK_BUDGET_S,
                     help="检测时间预算（秒，默认 10）")
    env.add_argument("--no-persist", action="store_true",
                     help="只打印结论，不写入检测快照")

    machine = add_common(cmd_machine, "machine",
                         "机器评估：CPU/内存/磁盘/GPU/负载 + tokens/s 基准（djb.machine-eval/1）")
    machine.add_argument("--no-benchmark", action="store_true", help="不跑 tokens/s 基准")
    machine.add_argument("--benchmark-mode", choices=("synthetic", "ollama"),
                         default="synthetic",
                         help="synthetic=纯 Python 合成参考；ollama=本机 Ollama 实测（默认 synthetic）")
    machine.add_argument("--ollama-url", default="http://127.0.0.1:11434",
                         help="本机 Ollama 地址（只允许回环）")
    machine.add_argument("--ollama-model", default="qwen2.5:7b", help="Ollama 模型名")
    machine.add_argument("--out", metavar="PATH", help="把评估 JSON 写到指定路径（0600）")
    machine.add_argument("--e2e", action="store_true",
                         help="写到 E2E 约定位置 ~/.djb/secrets/e2e/machine-eval/<platform>.json（0600）")

    auth = add_common(cmd_auth, "auth", "节点密钥 / 绑定 / 轮换")
    auth.add_argument("--status", action="store_true", help="查看绑定状态（默认）")
    auth.add_argument("--pair", action="store_true", help="生成 / 轮换客户端密钥")
    auth.add_argument("--no-rotate", action="store_true", help="pair 时只取新密钥不轮换旧密钥")
    auth.add_argument("--overlap-seconds", type=int, default=0, help="旧密钥重叠秒数")
    auth.add_argument("--bind", metavar="CODE", help="用网页端一次性码绑定")
    auth.add_argument("--unbind", action="store_true", help="解绑（需 --yes）")
    auth.add_argument("--set-key", metavar="KEY", help="直接设置客户端密钥（- 从终端读取不回显）")
    auth.add_argument("--yes", action="store_true", help="确认高危动作")

    start = add_common(cmd_start, "start", "启动 daemon（本机回环 127.0.0.1:1717/v1）")
    start.add_argument("--foreground", action="store_true", help="前台运行（调试）")
    start.add_argument("--no-token", action="store_true",
                       help="不把一次性 local_token 打到 stdout（容器/日志采集；用 djb status --show-token 读取）")
    start.add_argument("--port", type=int, help="覆盖镜像端口")
    start.add_argument("--timeout", type=float, default=20.0, help="等待启动秒数")

    stop = add_common(cmd_stop, "stop", "停止 daemon")
    stop.add_argument("--force", action="store_true", help="超时后 SIGKILL")
    stop.add_argument("--timeout", type=float, default=15.0)

    status = add_common(cmd_status, "status", "查看 daemon / 策略 / 收据 / 限额状态")
    status.add_argument("--show-token", action="store_true",
                        help="读取本次进程的 local_token（不落盘）")

    add_common(cmd_pause, "pause", "暂停接单（用户暂停，重启后保留）")
    add_common(cmd_resume, "resume", "恢复接单")

    # F14：daemon 控制组（当前只有 reload）；同一 handler 也挂一个顶层 reload 别名
    daemon_cmd = add_common(cmd_daemon, "daemon",
                            "daemon 控制（reload：重载配置与上游地址，不中断在途任务）")
    daemon_sub = daemon_cmd.add_subparsers(dest="daemon_action", metavar="<action>")
    daemon_reload = daemon_sub.add_parser(
        "reload", help="重载 ~/.djb/config.json 与上游地址（不中断在途任务）",
        description="重读磁盘配置并生效；坏配置保留旧值并报错")
    daemon_reload.add_argument("--home", help=argparse.SUPPRESS)
    daemon_reload.add_argument("--json", action="store_true", help="输出 JSON")
    daemon_reload.set_defaults(handler=cmd_daemon_reload)
    add_common(cmd_daemon_reload, "reload", "同 djb daemon reload：重载配置与上游地址")

    limits = add_common(cmd_limits, "limits", "查看 / 设置限额（并发、日周月、资源、时段、让路）")
    limits.add_argument("--preset", choices=facts.PRESETS)
    limits.add_argument("--daily-tokens", type=int)
    limits.add_argument("--weekly-tokens")
    limits.add_argument("--monthly-tokens")
    limits.add_argument("--concurrent", type=int)
    limits.add_argument("--cpu", type=int)
    limits.add_argument("--gpu", type=int)
    limits.add_argument("--mem", type=int)
    limits.add_argument("--yield", dest="yield_enabled", action="store_true",
                        default=None, help="开启让路")
    limits.add_argument("--no-yield", dest="yield_enabled", action="store_false",
                        help="关闭让路")
    limits.add_argument("--schedule", help="允许接单时段，如 09:00-23:00（逗号分隔多段）")
    limits.add_argument("--schedule-json", help="时段窗口 JSON（含 days / start / end）")
    limits.add_argument("--clear-schedule", action="store_true", help="清空时段（全天允许）")

    upstream = add_common(cmd_upstream, "upstream", "上游接入 A1/A2/B、模型开关与最低价、探测")
    upstream_sub = upstream.add_subparsers(dest="upstream_action", metavar="<action>")

    def _sub_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help=argparse.SUPPRESS)
        target.add_argument("--home", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    add = upstream_sub.add_parser("add", help="添加并申报上游")
    add.add_argument("--access", required=True, choices=facts.ACCESS_TYPES)
    add.add_argument("--base-url", required=True)
    add.add_argument("--models", required=True, help="逗号分隔的模型 id")
    add.add_argument("--kind", choices=facts.UPSTREAM_KINDS,
                     help="可选 upstream_kind（第三方中转站选 " + facts.RELAY_KIND + "）")
    add.add_argument("--auth", choices=facts.RELAY_AUTH_SCHEMES,
                     help="relay 认证方式（默认 bearer）")
    add.add_argument("--auth-header", help="custom 认证的 header 名")
    add.add_argument("--auth-prefix", help="认证值前缀（默认 Bearer ）")
    add.add_argument("--tls-pin", help="自签 relay 的证书 SPKI SHA-256 pin（显式确认）")
    add.add_argument("--secret", help="上游密钥（建议 --secret-stdin，避免进 shell 历史）")
    add.add_argument("--secret-stdin", action="store_true", help="从 stdin 读密钥")
    add.add_argument("--protocol", default=facts.DEFAULT_UPSTREAM_PROTOCOL)
    upstream_sub.add_parser("list", help="列出本机上游")
    probe = upstream_sub.add_parser("probe", help="本地探测上游并把结论上报（服务端不探测）")
    probe.add_argument("--id", required=True)
    probe.add_argument("--level", choices=facts.PROBE_LEVELS,
                       help="探针级别（默认 L3；流式失败回退 L2）")
    discover = upstream_sub.add_parser("discover", help="relay 模型自动发现（未映射不公告）")
    discover.add_argument("--id", required=True)
    discover.add_argument("--registry", help="可选 canonical 模型清单（逗号分隔）")
    mapping = upstream_sub.add_parser("map", help="relay 模型别名映射")
    mapping.add_argument("--id", required=True)
    mapping.add_argument("--from", dest="from_model", required=True)
    mapping.add_argument("--to", dest="to_model", required=True)
    mapping.add_argument("--replace", action="store_true", help="覆盖已有映射")
    unmap = upstream_sub.add_parser("unmap", help="取消 relay 模型映射")
    unmap.add_argument("--id", required=True)
    unmap.add_argument("--from", dest="from_model", required=True)
    upstream_sub.add_parser("relay", help="查看 relay 配置（开关关闭时只读）")
    for action, help_text in (("enable", "启用模型"), ("disable", "停用模型")):
        item = upstream_sub.add_parser(action, help=help_text)
        item.add_argument("--id", required=True)
        item.add_argument("--model", required=True)
    price = upstream_sub.add_parser("price", help="设置逐模型最低价")
    price.add_argument("--id", required=True)
    price.add_argument("--model", required=True)
    price.add_argument("--min-price", required=True)
    remove = upstream_sub.add_parser("remove", help="删除上游（同时删除本地凭据）")
    remove.add_argument("--id", required=True)
    remove.add_argument("--yes", action="store_true")
    prompt = upstream_sub.add_parser("a2-prompt", help="A2 五字段 prompt 模板")
    prompt.add_argument("--id")
    prompt.add_argument("--base-url")
    prompt.add_argument("--models")
    declare = upstream_sub.add_parser("declare", help="重新申报上游")
    declare.add_argument("--id", required=True)
    for child in upstream_sub.choices.values():
        _sub_flags(child)

    token = add_common(cmd_token, "token", "本地 token 计数（不联网；估算值显式标记）")
    token_sub = token.add_subparsers(dest="token_action", metavar="<action>")
    token_count = token_sub.add_parser("count", help="计数文本或 messages 的 token")
    token_count.add_argument("--model", required=True)
    token_count.add_argument("--text", help="直接给文本（不落盘）")
    token_count.add_argument("--file", help="从文件读文本（只读）")
    token_count.add_argument("--messages", help="JSON messages 数组（含 chat template 开销）")
    token_count.add_argument("--tools", help="JSON tools 数组")
    for child in token_sub.choices.values():
        _sub_flags(child)

    notify = add_common(cmd_notify, "notify",
                        "消息中心：列表 / 已读 / 确认 / 未读 / 同步（本地优先，离线读态排队）")
    notify_sub = notify.add_subparsers(dest="notify_action", metavar="<action>")
    notify_list = notify_sub.add_parser("list", help="列出消息（默认按时间倒序）")
    notify_list.add_argument("--category", help="按分类过滤")
    notify_list.add_argument("--unread", dest="unread_only", action="store_true",
                             help="只看未读")
    notify_list.add_argument("--limit", type=int,
                             default=facts.NOTIFICATIONS_DEFAULT_LIMIT)
    for name, help_text in (("read", "标记已读（本地先落，失败离线补发）"),
                            ("ack", "确认消息（本地先落，失败离线补发）")):
        item = notify_sub.add_parser(name, help=help_text)
        item.add_argument("--id", required=True, help="消息 id")
    notify_sub.add_parser("unread", help="未读数（本地 + 服务端）")
    notify_sub.add_parser("sync", help="手动拉取一次并补发离线读态")
    for child in notify_sub.choices.values():
        _sub_flags(child)

    model_sync = add_common(cmd_model_sync, "model-sync",
                            "模型/tokenizer 同步：查看本地版本或手动拉取签名 bundle")
    model_sync_sub = model_sync.add_subparsers(dest="model_sync_action",
                                               metavar="<action>")
    model_sync_sub.add_parser("status", help="查看本地已应用版本 / unsupported / 漂移")
    model_sync_sub.add_parser("sync", help="手动拉取并应用一次（失败保留旧版）")
    for child in model_sync_sub.choices.values():
        _sub_flags(child)

    plan_cmd = add_common(cmd_plan, "plan",
                          "多 plan 组：组内自动切换 + 组级共享预算（开关 plan.groups）")
    plan_sub = plan_cmd.add_subparsers(dest="plan_action", metavar="<group|budget>")
    plan_group = plan_sub.add_parser("group", help="组与成员：add / list / remove / add-upstream / remove-upstream")
    plan_group_sub = plan_group.add_subparsers(dest="plan_group_action", metavar="<action>")
    _sub_flags(plan_group)
    pg_add = plan_group_sub.add_parser("add", help="建组（金额是 USD 字符串，如 1.50）")
    pg_add.add_argument("--name", required=True, help="组名（唯一）")
    pg_add.add_argument("--priority", type=int, default=100, help="组优先级（小的先选）")
    pg_add.add_argument("--day", help="日预算 USD（如 1.50；不填=不限）")
    pg_add.add_argument("--month", help="月预算 USD（如 30.00；不填=不限）")
    pg_add.add_argument("--no-day", action="store_true", help="日预算不限")
    pg_add.add_argument("--no-month", action="store_true", help="月预算不限")
    pg_add.add_argument("--disabled", action="store_true", help="建成关闭状态")
    pg_add.add_argument("--allow-downgrade", action="store_true",
                        help="允许跨模型降级（默认禁止；需同时配 --fallback）")
    pg_add.add_argument("--fallback", help="降级链，逗号分隔（默认空）")
    _sub_flags(pg_add)
    pg_add.set_defaults(plan_action="group.add")
    pg_list = plan_group_sub.add_parser("list", help="列出组 / 成员 / 已花预算")
    _sub_flags(pg_list)
    pg_list.set_defaults(plan_action="group.list")
    pg_remove = plan_group_sub.add_parser("remove", help="删组（连同成员；不动上游）")
    pg_remove.add_argument("--group", required=True, help="组名或 id")
    _sub_flags(pg_remove)
    pg_remove.set_defaults(plan_action="group.remove")
    pg_member = plan_group_sub.add_parser("add-upstream", help="把上游加进组")
    pg_member.add_argument("--group", required=True, help="组名或 id")
    pg_member.add_argument("--id", required=True, help="上游 id")
    pg_member.add_argument("--priority", type=int, default=100, help="组内优先级（小的先用）")
    pg_member.add_argument("--weight", type=int, default=1, help="同优先级内权重（1–1000）")
    pg_member.add_argument("--disabled", action="store_true", help="加入但先停用")
    _sub_flags(pg_member)
    pg_member.set_defaults(plan_action="group.add-upstream")
    pg_unmember = plan_group_sub.add_parser("remove-upstream", help="把上游移出组")
    pg_unmember.add_argument("--group", required=True, help="组名或 id")
    pg_unmember.add_argument("--id", required=True, help="上游 id")
    _sub_flags(pg_unmember)
    pg_unmember.set_defaults(plan_action="group.remove-upstream")

    plan_budget = plan_sub.add_parser("budget", help="组预算是 / 设置（日 / 月，USD）")
    plan_budget_sub = plan_budget.add_subparsers(dest="plan_budget_action", metavar="<action>")
    _sub_flags(plan_budget)
    pb_set = plan_budget_sub.add_parser("set", help="设置日 / 月预算（不改上游与凭据）")
    pb_set.add_argument("--group", required=True, help="组名或 id")
    pb_set.add_argument("--day", help="日预算 USD（如 1.50）")
    pb_set.add_argument("--month", help="月预算 USD（如 30.00）")
    pb_set.add_argument("--no-day", action="store_true", help="取消日预算上限")
    pb_set.add_argument("--no-month", action="store_true", help="取消月预算上限")
    pb_set.add_argument("--allow-downgrade", action="store_true", help="允许跨模型降级")
    pb_set.add_argument("--no-downgrade", action="store_true", help="禁止跨模型降级")
    pb_set.add_argument("--fallback", help="降级链，逗号分隔（空串=清空）")
    _sub_flags(pb_set)
    pb_set.set_defaults(plan_action="budget.set")
    pb_show = plan_budget_sub.add_parser("show", help="看已花 / 剩余 / 是否耗尽")
    pb_show.add_argument("--group", help="只看一个组（组名或 id）")
    _sub_flags(pb_show)
    pb_show.set_defaults(plan_action="budget.show")

    flags = add_common(cmd_flags, "flags", "查看 / 设置本机特性开关（默认关闭）")
    flags.add_argument("--enable", metavar="NAME")
    flags.add_argument("--disable", metavar="NAME")

    attest_cmd = add_common(cmd_attest, "attest", "查看本地模型身份证明记录（客户端线索）")
    attest_cmd.add_argument("--id", help="上游 id")
    attest_cmd.add_argument("--model", help="模型 id（与 --id 同用时看单条）")

    announce = add_common(cmd_announce, "announce", "手动公告容量（带模型 / 价格 / QPS）")
    announce.add_argument("--dry-run", action="store_true", help="只打印公告内容")

    quotes_cmd = add_common(cmd_quotes, "quotes",
                            "报价发布（供方）：show 看将发送的行 / publish 发布（TTL = 2 × 周期）")
    quotes_sub = quotes_cmd.add_subparsers(dest="quotes_action", metavar="<action>")
    quotes_show = quotes_sub.add_parser("show", help="列出将发送的报价行（不联网、不发送）")
    quotes_show.add_argument("--id", help="只看一个上游")
    quotes_show.add_argument("--period-s", type=int,
                             help="发布周期（秒，默认 60；TTL = 2 × 周期）")
    quotes_show.set_defaults(quotes_action="show")
    quotes_publish = quotes_sub.add_parser(
        "publish", help="发布报价（内容不变时服务端复用；需要报价开关）")
    quotes_publish.add_argument("--id", help="只发一个上游")
    quotes_publish.add_argument("--period-s", type=int,
                                help="发布周期（秒，默认 60；TTL = 2 × 周期）")
    quotes_publish.add_argument("--dry-run", action="store_true",
                                help="只打印请求体，不发送")
    quotes_publish.set_defaults(quotes_action="publish")
    for child in quotes_sub.choices.values():
        _sub_flags(child)

    update = add_common(cmd_update, "update", "检查 / 安装 / 回滚更新（S1 骨架，mock 源）")
    update.add_argument("--check", action="store_true", help="只检查（默认）")
    update.add_argument("--apply", action="store_true", help="校验并安装")
    update.add_argument("--rollback", action="store_true", help="回滚到上一版")
    update.add_argument("--status", action="store_true", help="显示更新状态")
    update.add_argument("--source", help="更新源（URL / 本地目录 / file://）")
    update.add_argument("--channel", choices=facts.UPDATE_CHANNELS)

    logs = add_common(cmd_logs, "logs", "查看脱敏日志（7 天保留）")
    logs.add_argument("--tail", type=int, default=50)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--level", choices=("info", "debug", "warning", "error"))

    diagnose = add_common(cmd_diagnose, "diagnose", "生成脱敏诊断包（无凭据 / 无正文）")
    diagnose.add_argument("--output", help="输出路径（默认 diagnostics/）")

    tray = add_common(cmd_tray, "tray",
                      "启动托盘壳（Windows/macOS；只发动作建议，不执行命令）")
    tray.add_argument("--interval-ms", type=int, default=5000,
                      help="刷新间隔（毫秒，默认 5000）")
    tray.add_argument("--title", default="DJB 联脑", help="窗口 / 菜单栏标题前缀")

    proxy = add_common(cmd_proxy, "proxy",
                       "显示 S4 代理偏好（composition / effort / proxy_mode；只打印不联网）")
    proxy.add_argument("--composition", choices=("single", "mono_pair", "hetero_pair"),
                       default="single", help="组合形态（客户端当前只支持 single）")
    proxy.add_argument("--effort", choices=("minimal", "low", "medium", "high", "auto"),
                       help="推理强度；auto 仅 single 可用")
    proxy.add_argument("--proxy-mode", choices=("fidelity", "balanced", "max_save"),
                       help="代理档位；缺省 fidelity")

    gui = add_common(cmd_gui, "gui", "打开 S3 共享设置窗口（预设 / 三档 / 组网）")
    gui.add_argument("--preset", choices=("saver", "balanced", "full"), default=None)
    gui.add_argument("--no-realtime", action="store_true", help="关闭实时算力")
    gui.add_argument("--no-batch", action="store_true", help="关闭慢任务")
    gui.add_argument("--network", action="store_true", help="开启组网参与（默认关）")
    gui.add_argument("--upload-mbps", type=float, default=0.0, help="实测上行 Mbps（判达标）")

    uninstall = add_common(cmd_uninstall, "uninstall", "卸载：清除服务 / 配置 / 凭据 / 缓存并自检残留")
    uninstall.add_argument("--yes", action="store_true", help="确认卸载")
    uninstall.add_argument("--keep-logs", action="store_true", help="保留日志")
    uninstall.add_argument("--dry-run", action="store_true", help="只预览将删除的内容")

    return parser


def _enable_utf8_stdio() -> None:
    """Windows 控制台默认 GBK：中文输出会变成「鑱旇剳」。只对齐输出编码，**不改代码页**。

    P1（2026-11-07 实测）：Windows 上 ``djb init`` / ``djb env`` 的中文全乱码，因为 CLI 输出
    UTF-8 而控制台按 cp936（GBK）解码。这里把 ``sys.stdout`` / ``sys.stderr`` 的编码改成
    UTF-8 + ``errors="replace"``（宁可显示 '?' 也不抛 UnicodeEncodeError 打断命令）。

    为什么不用 ``chcp 65001``：那是改**调用方**（cmd / PowerShell 窗口）的全局代码页，
    会连带影响用户同一窗口里的其它程序，而且 CLI 无权替用户改终端设置。

    任何失败（流被替换成不支持 ``reconfigure`` 的替身、控制台不可写…）都静默跳过：
    乱码是显示问题，不能让它变成命令失败。
    """
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:                       # pragma: no cover - 平台差异，失败即跳过
            continue


def main(argv: list[str] | None = None) -> int:
    _enable_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return facts.EXIT_USAGE
    try:
        return int(handler(args) or 0)
    except DjbError as exc:
        print("djb: " + exc.message, file=sys.stderr)
        if exc.details:
            print("djb: details=" + json.dumps(exc.details, ensure_ascii=False),
                  file=sys.stderr)
        return exit_code_of(exc)
    except KeyboardInterrupt:
        print("djb: 已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:                                    # pragma: no cover
        return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
