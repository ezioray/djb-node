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
import subprocess
import sys
import time
from typing import Any, Callable

from . import __version__, facts
from .api import NodeApi
from .attest import AttestationManager
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
from . import tokens as tokens_mod
from .update import UpdateManager
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


# ─────────────────────────────────────────────────────────── init
def cmd_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = load_config(paths)
    if getattr(args, "gateway", None):
        config.gateway_url = str(args.gateway).strip()
    if getattr(args, "port", None):
        config.mirror_port = int(args.port)
    if getattr(args, "channel", None):
        config.update_channel = args.channel
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
    _emit(info, as_json=_json_flag(args),
          text=lambda value: ("节点 " + str(value.get("node_id") or "(未注册)")
                              + " · " + ("已绑定" if value.get("bound") else "未绑定（只做离线自测）")
                              + "\n凭据后端：" + str(value["credentials"].get("storage"))
                              + "（" + str(value["credentials"].get("file_mode")) + "）"
                              + "\n服务端：" + str(value.get("gateway"))))
    return 0


# ─────────────────────────────────────────────────────────── start / stop
def cmd_start(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    if getattr(args, "port", None):
        config.mirror_port = int(args.port)
        config.validate()
        save_config(paths, config)
    token = new_token(32)
    if getattr(args, "foreground", False):
        daemon = Daemon(paths, config=config, local_token=token)
        if not getattr(args, "no_token", False):
            print("local_token=" + token, flush=True)
        return daemon.run()
    _spawn_daemon(paths, config, token)
    deadline = time.monotonic() + float(getattr(args, "timeout", 20) or 20)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = ControlClient(paths).call({"cmd": "ping"}, timeout=1.0)
            if response.get("ok"):
                payload = {"started": True, "pid": response.get("pid"),
                           "local_api": "http://" + facts.MIRROR_HOST + ":"
                                        + str(config.mirror_port) + "/v1",
                           "local_token": token,
                           "token_note": "令牌只在本次进程存活期间有效；不落盘，可用 djb status --show-token 再次读取",
                           "next_step": "export DJB_LOCAL_TOKEN=<token>; 之后用 Bearer 调用本机 /v1"}
                _emit(payload, as_json=_json_flag(args),
                      text=lambda value: "daemon 已启动（pid " + str(value["pid"]) + "）\n"
                      + "本机镜像：" + str(value["local_api"]) + "（仅 127.0.0.1）\n"
                      + "local_token=" + str(value["local_token"]) + "\n"
                      + "令牌只在本次进程存活期间有效，不落盘；需要时 djb status --show-token")
                return 0
        except NotRunningError as exc:
            last_error = exc.message
        time.sleep(0.25)
    print("djb: daemon 启动超时（" + last_error + "）；查看 djb logs --tail 50", file=sys.stderr)
    return facts.EXIT_NOT_RUNNING


def _spawn_daemon(paths: Paths, config: ClientConfig, token: str) -> None:
    log_path = os.path.join(paths.logs_dir, "daemon.out")
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
        print("daemon 未在运行（幂等：无需停止）")
        return 0
    deadline = time.monotonic() + float(getattr(args, "timeout", 15) or 15)
    while time.monotonic() < deadline:
        if not os.path.exists(control_socket_file(paths)):
            print("daemon 已停止")
            return 0
        time.sleep(0.2)
    if getattr(args, "force", False):
        pid = _read_pid(paths)
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            print("已强制结束 daemon（pid " + str(pid) + "）")
            return 0
    print("djb: 停止超时；可用 djb stop --force", file=sys.stderr)
    return facts.EXIT_CONFLICT


def _read_pid(paths: Paths) -> int | None:
    try:
        with open(paths.pid_file, "r", encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


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
    return {
        "daemon_running": False,
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


def _status_text(value: dict[str, Any]) -> str:
    lines = [
        "状态：" + str(value.get("state")) + (
            "（" + str(value.get("reason_label")) + "）" if value.get("reason_label") else ""),
        "接单：" + ("是" if value.get("accepting") else "否"),
        "版本：" + str(value.get("version")) + " · 协议 " + str(value.get("protocol_version")),
        "daemon：" + ("运行中" if value.get("daemon_running") else "未运行"),
    ]
    if value.get("next_step"):
        lines.append("下一步：" + str(value["next_step"]))
    install = value.get("install") or {}
    if install:
        lines.append("安装：" + str(install.get("version")) + " · 频道 "
                     + str(install.get("channel")) + " · 桶 " + str(install.get("bucket")))
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
    local_api = value.get("local_api") or {}
    if local_api:
        lines.append("本机镜像：http://" + str(local_api.get("host")) + ":"
                     + str(local_api.get("port")) + "/v1（仅回环）")
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
        print(("已记录暂停偏好" if pause else "已记录恢复偏好")
              + "（daemon 未运行；下次 djb start 生效）")
        return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return _pause_resume(args, pause=True)


def cmd_resume(args: argparse.Namespace) -> int:
    return _pause_resume(args, pause=False)


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
def cmd_upstream(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    credentials = CredentialStore(paths)
    manager = UpstreamManager(store, credentials)
    action = getattr(args, "upstream_action", "list") or "list"
    if action == "add":
        secret = getattr(args, "secret", None)
        if getattr(args, "secret_stdin", False):
            secret = sys.stdin.readline().strip()
        relay_profile = None
        if getattr(args, "kind", None) == facts.RELAY_KIND:
            relay_mgr = RelayManager(store, credentials, config=config)
            if not relay_mgr.enabled():
                raise UsageError("relay 接入开关关闭（默认关闭）：先 djb flags --enable "
                                 + facts.RELAY_FLAG + " 再添加（本次未创建上游）")
        record = manager.add(access_type=args.access, base_url=args.base_url,
                             models=args.models, secret=secret, protocol=args.protocol)
        if getattr(args, "kind", None) == facts.RELAY_KIND:
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
                  [str(item["id"]) + " · " + str(item["access_type"]) + " · "
                   + str(item["base_url"]) + " · " + str(item["state"]) + " · 模型 "
                   + ",".join(str(model.get("id")) for model in item["models"])
                   for item in value["upstreams"]]) or "（没有上游）")
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
            api.report_probe({"upstream_id": record.id, **report})
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


# ─────────────────────────────────────────────────────────── update
def cmd_update(args: argparse.Namespace) -> int:
    paths, config, store = _context(args)
    source = getattr(args, "source", None)
    manager = UpdateManager(paths, store,
                            source=source if source is not None else config.update_source,
                            channel=getattr(args, "channel", None) or config.update_channel,
                            http=HttpClient(timeout=20.0))
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
        if result.get("state") == "failed_verify":
            return facts.EXIT_VERIFY
        return 0
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
               os.path.join(paths.runtime_dir, "daemon.out")]
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
    argv = ["--preset", str(getattr(args, "preset", "balanced")),
            "--upload-mbps", str(getattr(args, "upload_mbps", 0.0))]
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

    init = add_common(cmd_init, "init", "初始化本机数据目录 / 预设 / 限额，并做一次环境检测")
    init.add_argument("--gateway", help="服务端地址（默认占位 api.example.invalid）")
    init.add_argument("--port", type=int, help="本机镜像端口（1024–65535，默认 1717）")
    init.add_argument("--channel", choices=facts.UPDATE_CHANNELS, help="更新频道")
    init.add_argument("--update-source", help="更新源（URL / 本地目录；留空用 mock 源）")
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

    flags = add_common(cmd_flags, "flags", "查看 / 设置本机特性开关（默认关闭）")
    flags.add_argument("--enable", metavar="NAME")
    flags.add_argument("--disable", metavar="NAME")

    attest_cmd = add_common(cmd_attest, "attest", "查看本地模型身份证明记录（客户端线索）")
    attest_cmd.add_argument("--id", help="上游 id")
    attest_cmd.add_argument("--model", help="模型 id（与 --id 同用时看单条）")

    announce = add_common(cmd_announce, "announce", "手动公告容量（带模型 / 价格 / QPS）")
    announce.add_argument("--dry-run", action="store_true", help="只打印公告内容")

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
    gui.add_argument("--preset", choices=("saver", "balanced", "full"), default="balanced")
    gui.add_argument("--no-realtime", action="store_true", help="关闭实时算力")
    gui.add_argument("--no-batch", action="store_true", help="关闭慢任务")
    gui.add_argument("--network", action="store_true", help="开启组网参与（默认关）")
    gui.add_argument("--upload-mbps", type=float, default=0.0, help="实测上行 Mbps（判达标）")

    uninstall = add_common(cmd_uninstall, "uninstall", "卸载：清除服务 / 配置 / 凭据 / 缓存并自检残留")
    uninstall.add_argument("--yes", action="store_true", help="确认卸载")
    uninstall.add_argument("--keep-logs", action="store_true", help="保留日志")
    uninstall.add_argument("--dry-run", action="store_true", help="只预览将删除的内容")

    return parser


def main(argv: list[str] | None = None) -> int:
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
