# SPDX-License-Identifier: Apache-2.0
"""daemon 主循环（M15 §6 关键流程 / §7 失败与降级）。

一次 tick 做四件事（全部可注入时钟，测试不 sleep）：

1. 到点心跳（携运行态 + 容量公告）；处理 policy_updated / 403 / 429 / 网络失败；
2. 策略拉取（If-None-Match = policy_version，304 省流量）；
3. 收据批量上报（失败按 1/5/30/300/1800 s 退避，≤8 次；>24 h 丢弃并停接单）；
4. L1/L2/L3 裁决（让路 / 资源 / 配额 + 未绑定 + 保守模式）并迁移状态。

对外两个本地端点：

* 127.0.0.1:1717/v1（本机镜像，Bearer 一次性 local_token，进程退出即失效）；
* Unix 控制套接字 0600（djb status / pause / resume / stop / token 用；不落盘任何令牌）。

INV-01：任何「不在赚」的状态都写 reason + next_step，status / health / 日志三处可见。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from . import (facts, machine_profile, pidfile, plan_router, policy as policy_mod,
               protocol)
from . import quotes as quotes_mod
from .api import NodeApi, PolicyFetch
from .channel import ChannelClient, channel_enabled
from .config import ClientConfig, Paths, feature_flag, load_config, load_config_strict
from .errors import (AuthError, ConflictError, DjbError, ForbiddenError, NetworkError,
                     NotInstalledError, PolicyError, PortInUseError, RateLimitError,
                     UsageError)
from .httpclient import HttpClient
from .limits import Decision, Limiter, YieldDetector, YieldResult
from .logging_setup import event, setup_logging
from .model_sync import ModelSyncClient, model_sync_enabled, verifier_from_env
from .notifications import NotificationStore, notifications_enabled
from .outbox import ReceiptFactory, SealedReceiptBuffer
from .probe import periodic_interval_s
from .relay import RelayManager, capacity_targets
from .secure import CredentialStore
from .server import Admission, LocalApiServer, RouteTarget
from .state import StateMachine, Snapshot, reason_label
from .store import Store, UpstreamRecord
from .update import UpdateManager
from .upstream import ModelEntry, UpstreamManager
from .util import (iso, new_request_id, new_token, parse_iso, sha256_hex, utcnow)

#: D-POLICY-01：剩余租约不足该值即主动拉策略（TTL 默认 3600，留 10 分钟提前量）
POLICY_REFRESH_AHEAD_S = 600
#: D-POLICY-01：两次主动拉策略的最小间隔（服务端异常时限频，避免打爆）
POLICY_REFRESH_MIN_INTERVAL_S = 60


# ─────────────────────────────────────────────────────────── 控制套接字
def control_socket_file(paths: Paths) -> str:
    """Unix socket 路径上限 ~108 字节；太长时退到 /tmp 的确定性命中路径。"""
    if os.name == "nt":
        from .ipc_windows import Win32, pipe_name, unavailable
        if not windows_control_enabled(paths):
            raise unavailable()
        return pipe_name(paths.home, Win32().current_identity())
    primary = paths.socket_file
    if len(primary) <= 100:
        return primary
    import hashlib
    digest = hashlib.sha256(paths.home.encode("utf-8")).hexdigest()[:12]
    return os.path.join("/tmp", "djb-" + str(os.getuid()) + "-" + digest + ".sock")


def windows_control_enabled(paths: Paths) -> bool:
    """Fresh loader on every boundary; parse/config/expiry failures are closed."""
    from .ipc_windows import fresh_enabled
    return fresh_enabled(lambda: load_config(paths))


def control_server_factory(paths: Paths, handler, *, stop_event):
    if os.name == "nt":
        from .ipc_windows import WindowsControlServer, unavailable
        if not windows_control_enabled(paths):
            raise unavailable()
        return WindowsControlServer(paths.home, handler, stop_event=stop_event,
                                    enabled=lambda: windows_control_enabled(paths))
    return ControlServer(control_socket_file(paths), handler, stop_event=stop_event)


class ControlServer(threading.Thread):
    def __init__(self, socket_path: str, handler: Callable[[dict[str, Any]], dict[str, Any]],
                 *, stop_event: threading.Event) -> None:
        super().__init__(name="djb-control", daemon=True)
        self.socket_path = socket_path
        self.handler = handler
        self.stop_event = stop_event
        self._socket: socket.socket | None = None

    def open(self) -> None:
        from .util import ensure_dir
        ensure_dir(os.path.dirname(self.socket_path) or ".", 0o700)
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        self._socket = server

    def run(self) -> None:
        if self._socket is None:
            self.open()
        assert self._socket is not None
        while not self.stop_event.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._serve(connection)
            finally:
                try:
                    connection.close()
                except OSError:
                    pass
        try:
            if self._socket is not None:
                self._socket.close()
        finally:
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass

    def _serve(self, connection: socket.socket) -> None:
        connection.settimeout(10.0)
        buffer = b""
        try:
            while b"\n" not in buffer and len(buffer) < 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        except OSError:
            return
        line = buffer.split(b"\n", 1)[0]
        try:
            command = json.loads(line.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            command = {}
        if not isinstance(command, dict):
            command = {}
        try:
            response = self.handler(command)
        except DjbError as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": exc.message,
                                               "type": exc.error_type}}
        except Exception as exc:                            # pragma: no cover - 防御
            response = {"ok": False, "error": {"code": 50000,
                                               "message": "内部错误：" + type(exc).__name__,
                                               "type": "server_error"}}
        try:
            connection.sendall((json.dumps(response, ensure_ascii=False) + "\n")
                               .encode("utf-8"))
        except OSError:
            pass


class ControlClient:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.socket_path = None if os.name == "nt" else control_socket_file(paths)

    def call(self, command: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
        from .errors import NotRunningError
        if os.name == "nt":
            from .ipc_windows import windows_call
            return windows_call(self.paths.home, command, timeout=timeout,
                                enabled=lambda: windows_control_enabled(self.paths))
        if not os.path.exists(self.socket_path):
            raise NotRunningError("daemon 未运行（控制套接字不存在）：先 djb start")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(self.socket_path)
            client.sendall((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
            chunks = b""
            while b"\n" not in chunks and len(chunks) < 4 * 1024 * 1024:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks += chunk
        except (socket.timeout, ConnectionRefusedError, FileNotFoundError) as exc:
            raise NotRunningError("daemon 无响应（" + type(exc).__name__ + "）："
                                  "先 djb start，或 djb logs 查看原因") from exc
        except OSError as exc:
            raise NotRunningError("daemon 控制通道不可用：" + type(exc).__name__) from exc
        finally:
            try:
                client.close()
            except OSError:
                pass
        try:
            response = json.loads(chunks.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise NotRunningError("daemon 返回不可解析（可能正在退出）") from exc
        if not isinstance(response, dict):
            raise NotRunningError("daemon 返回非法响应")
        if response.get("ok") is False and response.get("soft") is not True:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise UsageError(str(error.get("message") or "控制命令失败"))
        # soft=False 才抛；reload 这类「命令合法但结果是否」的返回原样交给调用方
        return response


def quotes_enabled(config: Any) -> bool:
    """报价发布开关（``client.quotes.publish.enabled``，默认关闭）。

    口径与其它子系统一致：只登记在 ``facts.FEATURE_FLAGS``，取值统一走
    ``config.feature_flag``（到期强制关）。关闭时 daemon **不建线程、不发任何请求**，
    行为与无报价能力的版本完全一致。
    """
    return feature_flag(config, facts.QUOTES_FLAG)


# ─────────────────────────────────────────────────────────── Daemon
class Daemon:
    def __init__(self, paths: Paths, *, config: ClientConfig | None = None,
                 store: Store | None = None,
                 credentials: CredentialStore | None = None,
                 local_token: str | None = None,
                 port: int | None = None,
                 http: HttpClient | None = None,
                 api_factory: Callable[[str, str | None], NodeApi] | None = None,
                 yield_detector: YieldDetector | None = None,
                 limiter: Limiter | None = None,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.paths = paths
        self.config = config or load_config(paths)
        self._now = now
        self.store = store or Store(paths.db_file, now=now)
        self.credentials = credentials or CredentialStore(paths)
        self.token = local_token or new_token()
        self.port = int(port if port is not None else self.config.mirror_port)
        self.http = http or HttpClient(timeout=15.0)
        self._api_factory = api_factory or (
            lambda base, key: NodeApi(base, client_key=key, http=self.http))
        self.api: NodeApi | None = None
        # M3：机器画像（与状态机 self.machine 无关，故意另起名字）
        self._machine_eval: dict[str, Any] | None = None
        self.machine = StateMachine(Snapshot.from_dict(self.store.get_kv("daemon_state")),
                                    now=now)
        self.limiter = limiter or Limiter(self.store, detector=yield_detector or YieldDetector(),
                                          now=now)
        self.upstreams = UpstreamManager(self.store, self.credentials, http=self.http)
        self.buffer = SealedReceiptBuffer(self.store, self.credentials.buffer_key(), now=now)
        self.factory = ReceiptFactory(self.credentials.provider_signing_key(), clock=now)
        self.update = UpdateManager(paths, self.store, source=self.config.update_source,
                                    channel=self.config.update_channel,
                                    client_version=facts.CLIENT_VERSION, http=self.http,
                                    config=self.config, now=now)
        self.server = LocalApiServer(self, token=self.token, port=self.port, http=self.http,
                                        settings=self.config.extra)
        self._stop_event = threading.Event()
        self._control: ControlServer | None = None
        self._started = False
        # C10c 出站长轮询通道（0.2.4 起开关默认开；显式关闭时仍不创建线程）
        self._channel: ChannelClient | None = None
        self._channel_thread: threading.Thread | None = None
        self._channel_stop: threading.Event | None = None
        # P4 落点 B：服务端心跳协商 channel_required。落盘（store kv）而不是只留内存
        # —— 进程重启后服务端的要求依然成立。
        self._channel_required = self._load_channel_required()
        # M18b 模型/tokenizer 同步（默认关闭：关闭时不拉取、不建线程）
        self._model_sync: ModelSyncClient | None = None
        self._model_sync_thread: threading.Thread | None = None
        self._model_sync_stop: threading.Event | None = None
        # C16 消息中心（默认关闭：关闭时不拉取、不建线程；本地库只读保留）
        self._notifications: NotificationStore | None = None
        self._notifications_thread: threading.Thread | None = None
        self._notifications_stop: threading.Event | None = None
        # CR-20261106-OFFICIAL-SUPPLY-01 报价发布（默认关闭：关闭时不建线程、不发送）
        self._quotes_thread: threading.Thread | None = None
        self._quotes_stop: threading.Event | None = None
        self._quotes_period_s = facts.QUOTES_PERIOD_DEFAULT_S
        self._last_quotes: dict[str, Any] = {}
        self._quotes_error_logged = ""
        # 运行态
        self._user_paused = bool(self.store.get_kv("user_paused", False))
        self._consecutive_failures = 0
        # 403 的退避计数单独一份：403 不是网络失败，混进 _consecutive_failures 会让
        # 「等审核」被升格成「保守模式」，把原因换成更误导的那一条。
        self._forbidden_failures = 0
        self._next_heartbeat_at = self._now()
        self._heartbeat_interval_s = facts.HEARTBEAT_DEFAULT_S
        self._policy_updated = False
        # D-POLICY-01：本地策略租约到期/临近到期时主动拉一次（限频，见 _policy_refresh_due）
        self._next_policy_fetch_at = self._now()
        self._next_receipt_flush_at = self._now()
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._last_yield: dict[str, Any] = {}
        self._backlog_block = bool(self.store.get_kv("receipt_backlog_block", False))
        self._policy_invalid = bool(self.store.get_kv("policy_invalid", False))
        self._announce_kind = facts.TOKEN_RELAY_KIND
        self._last_health: dict[str, Any] = {}
        # C7 探针调度（全部走注入时钟，测试不 sleep）
        self._probe_failures = 0
        self._probe_interval_s = facts.PROBE_PERIODIC_DEFAULT_S
        self._next_probe_at = self._now()
        self._last_probe: dict[str, Any] = {}
        # F14 热重载：SIGHUP 只置位，实际加载在主循环里做（信号上下文不碰锁）
        self._reload_requested = False
        self.last_reload: dict[str, Any] | None = None
        self._reload_lock = threading.Lock()
        self._reload_count = 0

    # ── 状态持久化
    def _persist(self) -> None:
        self.store.set_kv("daemon_state", self.machine.snapshot.to_dict())
        running = self.store.get_kv("runtime", default={}) or {}
        running.update({"pid": os.getpid(), "port": self.server.port,
                        "updated_at": iso(self._now())})
        self.store.set_kv("runtime", running)

    def _transition_pause(self, reason: str, *, next_step: str | None = None,
                          detail: dict[str, Any] | None = None) -> None:
        snap = self.machine.snapshot
        if snap.state == "paused" and snap.reason == reason:
            return
        try:
            self.machine.pause(reason, next_step=next_step, detail=detail)
            event("daemon.paused", reason=reason, next_step=next_step)
        except DjbError as exc:                                # pragma: no cover - 防御
            event("daemon.pause_rejected", level="warning", reason=reason,
                  error=exc.message)
        self._persist()

    def _transition_online(self) -> None:
        if self.machine.snapshot.state == "online":
            return
        try:
            self.machine.resume()
            event("daemon.online")
        except DjbError:                                      # pragma: no cover
            pass
        self._persist()

    # ── 启动 / 停止
    def start(self) -> dict[str, Any]:
        if getattr(self, "_start_cleanup_incomplete", False):
            raise UsageError("启动清理未完成，拒绝重复启动")
        if self._started:
            return self.status()
        # 先落 pid 文件：**启动过程本身挂住**（控制面起不来/等网络）时，
        # `djb stop --force` 才有东西可校验、可强杀。失败时文件自然被 stop() 或下次启动覆盖。
        self._write_pid_file()
        if os.name == "nt":
            return self._start_windows()
        self.paths.prepare()
        if not self.store.is_initialized():
            raise NotInstalledError("本机未初始化：先执行 djb init")
        setup_logging(self.paths.log_file, level=self.config.log_level)
        self.credentials.ensure_permissions()
        snap = self.machine.snapshot
        if snap.state in ("online", "paused", "error", "updating"):
            event("daemon.recovering", previous_state=snap.state, previous_reason=snap.reason)
        self.machine.restore(Snapshot(state="stopped", since=iso(self._now())))
        self.machine.start()
        self._persist()
        self.server.bind()
        self.store.set_kv("runtime", {"pid": os.getpid(), "port": self.server.port,
                                      "started_at": iso(self._now()),
                                      "loopback_only": True,
                                      "token_fingerprint": self.server.token_fingerprint()})
        self._control = ControlServer(control_socket_file(self.paths), self.control,
                                      stop_event=self._stop_event)
        self._control.open()
        self._control.start()
        self.server.start()
        self._started = True
        event("daemon.started", port=self.server.port, pid=os.getpid(),
              protocol_version=facts.PROTOCOL_VERSION)
        self._bootstrap()
        self._start_channel()
        self._start_model_sync()
        self._start_notifications()
        self._start_quotes()
        if self._user_paused:
            self._transition_pause("user_paused")
        return self.status()

    def _start_windows(self) -> dict[str, Any]:
        """Transport preflight first; rollback only resources from this attempt."""
        if getattr(self, "_start_cleanup_incomplete", False) or self._control is not None:
            raise UsageError("已有控制实例或未完全回收的启动，请勿重复启动")
        from .ipc_windows import IpcUnavailable
        control = None
        self._control_unavailable_reason = None
        previous_state = self.store.get_kv("daemon_state")
        previous_runtime = self.store.get_kv("runtime")
        previous_snapshot = self.machine.snapshot
        opened = False
        mirror_owned = False
        old_stop = self._stop_event.is_set()
        if getattr(self.server, "_server", None) is not None or getattr(self.server, "_thread", None) is not None:
            raise UsageError("本次启动不得接管既有本机镜像listener")
        try:
            try:
                control = control_server_factory(self.paths, self.control, stop_event=self._stop_event)
                control.open()
                opened = True
            except IpcUnavailable:
                # The authenticated control plane is optional, never replaced by TCP.
                if control is not None:
                    try:
                        control.close_bound()
                    except Exception:
                        self._control = control
                        self._start_cleanup_incomplete = True
                        raise
                control = None
                self._control_unavailable_reason = "windows_control_unsupported"
                event("daemon.control_unavailable", reason=self._control_unavailable_reason)
            self.paths.prepare()
            if not self.store.is_initialized():
                raise NotInstalledError("本机未初始化：先执行 djb init")
            setup_logging(self.paths.log_file, level=self.config.log_level)
            self.credentials.ensure_permissions()
            self.server.bind()
            mirror_owned = True
            self.machine.restore(Snapshot(state="stopped", since=iso(self._now())))
            self.machine.start()
            self._persist()
            self.store.set_kv("runtime", {"pid": os.getpid(), "port": self.server.port,
                                          "started_at": iso(self._now()), "loopback_only": True,
                                          "token_fingerprint": self.server.token_fingerprint()})
            self._control = control
            if control is not None:
                control.start()
            self.server.start()
        except BaseException:
            control_alive = getattr(self, "_start_cleanup_incomplete", False)
            try:
                if opened:
                    if control.is_alive():
                        self._stop_event.set()
                        control.join(timeout=2.0)
                        control_alive = control.is_alive()
                    else:
                        control.close_bound()
            except Exception:
                control_alive = True  # retain ownership on uncertain cleanup
            if mirror_owned:
                try:
                    self.server.close_bound()
                except Exception:
                    # Preserve the original startup error; do not stop another running listener.
                    control_alive = True
            if not old_stop and not control_alive:
                self._stop_event.clear()
            self._control = control if control_alive else None
            self._start_cleanup_incomplete = control_alive
            try:
                self.machine.restore(previous_snapshot)
                self.store.set_kv("daemon_state", previous_state)
                self.store.set_kv("runtime", previous_runtime)
            except Exception:
                self._control = control  # preserve any owned control reference
                self._start_cleanup_incomplete = True
            if self._start_cleanup_incomplete:
                event("daemon.start_cleanup_incomplete", level="warning")
            raise
        self._start_cleanup_incomplete = False
        self._started = True
        event("daemon.started", port=self.server.port, pid=os.getpid(),
              protocol_version=facts.PROTOCOL_VERSION)
        self._bootstrap()
        self._start_channel()
        self._start_model_sync()
        self._start_notifications()
        self._start_quotes()
        if self._user_paused:
            self._transition_pause("user_paused")
        return self.status()

    def _bootstrap(self) -> None:
        base_url = self.config.gateway_url
        self.api = self._api_factory(base_url, None)
        # 1) 协议能力（无鉴权）；失败 -> 保守模式，不阻断离线自测
        try:
            view = self.api.capabilities()
            self.store.set_kv("capabilities", {
                "protocol_version": view.protocol_version,
                "min_supported_version": view.min_supported_version,
                "capabilities": view.capabilities, "enabled": view.enabled,
                "server_time": view.server_time, "fetched_at": iso(self._now())})
            if view.protocol_version and not protocol.is_protocol_compatible(
                    view.protocol_version, min_supported=view.min_supported_version):
                self.machine.fail(next_step="服务端协议 " + str(view.protocol_version)
                                  + " 与本客户端 " + facts.PROTOCOL_VERSION
                                  + " 不兼容：升级客户端（djb update）")
                self._persist()
                return
            try:
                deprecations = self.api.deprecations()
                self.store.set_kv("deprecations", deprecations)
            except DjbError as exc:
                event("daemon.deprecations_failed", level="warning",
                      error=type(exc).__name__)
        except DjbError as exc:
            event("daemon.capabilities_failed", level="warning",
                  error=type(exc).__name__)
            self._consecutive_failures += 1
        # 2) 注册 / 客户端密钥
        if not self.credentials.client_key():
            if not self._register():
                if self.machine.snapshot.state not in ("paused", "error"):
                    self.machine.pause(
                        "conservative",
                        next_step="离线/注册失败：只做离线自测（不接单）；"
                                  "恢复网络后自动重试注册")
                self._next_heartbeat_at = self._now()
                self._persist()
                return
        self.api.client_key = self.credentials.client_key()
        # 3) 公告门控前置：启动做一次 L1–L3 校验（预算内），未验证的模型不进容量
        #    （C7）。这是「公告前校验」，与周期复探开关解耦：开关关闭时它只发生一次。
        self._probe_upstreams(self._now(), reason="startup")
        # 4) 首次心跳 + 策略
        self._heartbeat(self._now(), initial=True)
        if self._policy_updated:
            self._policy_updated = False
            self._fetch_policy(self._now())
        self._evaluate(self._now())
        self._persist()

    def _register(self) -> bool:
        try:
            result = self.api.register(hardware_fingerprint=hardware_fingerprint())
        except ConflictError as exc:
            # P5（CR-20261106-OFFICIAL-SUPPLY-01 §3）：唯一键冲突 = 同一台机器/同一个硬件指纹
            # 已经注册过（云侧 40903，INV-01 防凭据接管）。这是「一机一节点」踩坑的第一现场，
            # 必须给可读指引而不是抽象 409，也**不进网络重试**（重试只会把冲突刷成噪声）。
            self._register_conflict(exc)
            return False
        except DjbError as exc:
            event("daemon.register_failed", level="warning", error=type(exc).__name__,
                  code=exc.code)
            if isinstance(exc, (NetworkError, RateLimitError)):
                self._consecutive_failures += 1
                return False
            self.machine.fail(next_step="注册失败：" + exc.message
                              + "；检查 djb config 的服务端地址，或用 djb auth --set-key 复用已有密钥")
            self._persist()
            return False
        node_id = str(result.get("node_id"))
        client_key = result.get("client_key")
        if isinstance(client_key, str) and client_key:
            self.credentials.set_node(node_id, client_key)
            event("daemon.registered", node_id=node_id, state=result.get("state"))
        else:
            event("daemon.registered_no_key", level="warning", node_id=node_id)
            self.machine.pause("credential_invalid",
                               next_step="注册响应没有一次性密钥：用 djb auth --set-key 填入密钥")
            self._persist()
            return False
        self.store.set_kv("node", {"node_id": node_id, "state": result.get("state"),
                                   "registered_at": iso(self._now())})
        return True

    def _register_conflict(self, exc: ConflictError) -> None:
        """唯一键冲突 → 可读指引（P5）；**只 fail，不重试、不清任何本地状态**。

        为什么单独一条路径：云侧 409 有多个来源（状态迁移、端口占用……），但注册端点上的
        409 只有一种业务含义 —— 这个硬件指纹已经有节点了。把它混进「注册失败」里，用户看到的
        是 HTTP 文案，不知道下一步该 `--set-key` 复用、`--bind` 归属，还是干脆加机器。
        """
        code = int(exc.code)
        server_said = str(exc.message or "").strip()
        hint = facts.REGISTER_CONFLICT_HINT
        next_step = (server_said + " · " + hint) if server_said else hint
        detail: dict[str, Any] = {"register_conflict": True, "code": code,
                                  "http_status": int(exc.details.get("status") or 409)}
        if code == facts.REGISTER_CONFLICT_CODE:
            detail["reason_code"] = "fingerprint_bound"
        event("daemon.register_conflict", level="warning", code=code,
              http_status=detail["http_status"])
        self.machine.fail(next_step=next_step, detail=detail)
        self._persist()

    def _register_conflict_pending(self) -> bool:
        """当前是否卡在「硬件指纹已注册」的永久冲突上（用于抑制自动重试）。"""
        detail = self.machine.snapshot.detail or {}
        return bool(detail.get("register_conflict"))

    def run(self, *, max_cycles: int | None = None) -> int:
        if not self._started:
            self.start()
        # F14：SIGHUP = 重载配置（与 `djb daemon reload` 同一条路径）
        self._install_reload_signal()
        cycles = 0
        try:
            while not self._stop_event.is_set():
                self.tick()
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                wait = self._seconds_until_next_due()
                if wait > 0:
                    self._stop_event.wait(min(wait, 1.0))
        finally:
            self.stop()
        return 0

    # ── runtime/daemon.pid：给 `djb stop --force` 的逃生门（(pid, 启动时刻) 双键）
    def _write_pid_file(self) -> None:
        """启动成功后原子写 pid 文件；写失败不阻断启动（逃生门是可选增强，不是启动前提）。"""
        if getattr(self, "paths", None) is None:               # Daemon.__new__ 构造的半成品
            return
        try:
            pidfile.write(self.paths)
        except OSError as exc:                                 # pragma: no cover - 只读/满盘
            event("daemon.pid_file_write_failed", level="warning", error=str(exc))

    def _remove_pid_file(self) -> None:
        """退出时删掉**自己的** pid 文件（幂等；不删别人的记录）。"""
        if getattr(self, "paths", None) is None:               # Daemon.__new__ 构造的半成品
            return
        try:
            pidfile.remove(self.paths, pid=os.getpid())
        except OSError as exc:                                 # pragma: no cover - 防御
            event("daemon.pid_file_remove_failed", level="warning", error=str(exc))

    def stop(self) -> None:
        if os.name == "nt" and not self._started and self._control is not None:
            raise UsageError("Windows 控制启动清理未完成；拒绝完整stop或重复操作")
        if not self._started and self._control is None:
            return
        event("daemon.stopping")
        self._stop_event.set()
        self._stop_channel()
        self._stop_model_sync()
        self._stop_notifications()
        self._stop_quotes()
        try:
            self.server.shutdown()
        except Exception:                                  # pragma: no cover - 防御
            pass
        snap = self.machine.snapshot
        if snap.state != "stopped":
            try:
                self.machine.stop()
            except DjbError:                               # pragma: no cover
                pass
        self._persist()
        self.store.set_kv("runtime", {})
        # 进程从这里起已经在退出路径上：先把 pid 记录撤掉。
        # **不能**放到 stop() 末尾 —— Windows 上"控制线程未回收"会在那之前 raise，
        # 结果进程退了、pid 文件却留下来（2026-11-05 真机 E2E 实测到）。
        self._remove_pid_file()
        if self._control is not None:
            self._control.join(timeout=2.0)
            if os.name == "nt" and self._control.is_alive():
                event("daemon.stop_cleanup_incomplete", level="warning")
                # Preserve ownership and prevent another start/stop claiming full cleanup.
                self._started = False
                raise UsageError("Windows 控制线程尚未回收；清理未完成")
            self._control = None
        socket_path = control_socket_file(self.paths) if os.name != "nt" else None
        if socket_path is not None and os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        self._started = False
        event("daemon.stopped")

    # ── F14 配置热重载（djb daemon reload / SIGHUP）
    #: 参与「是否变化」比较的字段（``extra`` 单独看，里面是子系统开关）
    RELOAD_FIELDS: tuple[str, ...] = ("gateway_url", "mirror_port", "update_channel",
                                      "update_source", "log_level", "yield_enabled", "extra")

    def _config_public(self, config: ClientConfig) -> dict[str, Any]:
        """重载回执里的配置快照：只含 config.json 的字段，凭据永不出现。"""
        data = config.to_dict()
        data["extra_keys"] = sorted(str(key) for key in (config.extra or {}))
        return data

    def _subsystem_running(self, name: str) -> bool:
        thread = {"channel": self._channel_thread,
                  "model_sync": self._model_sync_thread,
                  "notifications": self._notifications_thread,
                  "quotes": self._quotes_thread}.get(name)
        return bool(thread is not None and thread.is_alive())

    def _apply_subsystem_toggles(self, fresh: ClientConfig) -> list[str]:
        """开关变了就起停对应线程；**不碰**在途请求、状态机、收据缓冲。"""
        applied: list[str] = []
        # 通道的「该不该开」不只看本地开关：服务端心跳可以要求通道（P4 落点 B），
        # 但本地显式关闭优先（见 channel.channel_enabled 的优先级说明）。
        plan = (("channel", self._channel_flag_state, self._start_channel,
                 self._stop_channel),
                ("model_sync", model_sync_enabled, self._start_model_sync,
                 self._stop_model_sync),
                ("notifications", notifications_enabled, self._start_notifications,
                 self._stop_notifications),
                # 报价发布（CR-20261106-OFFICIAL-SUPPLY-01）：开关热切换，不碰在途请求
                ("quotes", quotes_enabled, self._start_quotes, self._stop_quotes))
        was = self.config
        for name, enabled, starter, stopper in plan:
            before, after = bool(enabled(was)), bool(enabled(fresh))
            if before == after:
                continue
            running = self._subsystem_running(name)
            if after and not running:
                starter()
                applied.append(name + ":started")
            elif not after and running:
                stopper()
                applied.append(name + ":stopped")
        return applied

    def reload_config(self) -> dict[str, Any]:
        """重读 ``~/.djb/config.json`` 与上游地址并生效；失败保留旧值。

        保证：

        * **不中断在途任务**：不动机器状态、不清 ``_inflight``、不动收据缓冲、
          不停镜像监听、不重启进程；
        * **坏配置回退**：解析/校验失败时 ``self.config`` 一个字段都不改，
          返回 ``ok=False`` + 可读原因（调用方必须把原因显示出来）；
        * **幂等**：同样的磁盘内容重载多次结果一致（``changed`` 为空、指纹相同）。
        """
        with self._reload_lock:
            return self._reload_locked()

    def _reload_locked(self) -> dict[str, Any]:
        previous = self.config
        if self._stop_event.is_set():
            return {"ok": False, "soft": True, "reason": "stopping",
                    "error": "daemon 正在退出，未重载",
                    "kept": self._config_public(previous)}
        try:
            fresh = load_config_strict(self.paths)
        except (ValueError, DjbError, OSError) as exc:
            message = str(exc).strip() or type(exc).__name__
            event("daemon.reload_rejected", level="warning", error=message)
            return {"ok": False, "soft": True, "reason": "invalid_config",
                    "error": message, "error_type": type(exc).__name__,
                    "kept": self._config_public(previous),
                    "message": "配置未生效：已保留旧配置（daemon 未中断）"}
        restart_required: list[dict[str, Any]] = []
        if int(fresh.mirror_port) != int(previous.mirror_port):
            restart_required.append({
                "field": "mirror_port", "from": int(previous.mirror_port),
                "to": int(fresh.mirror_port),
                "reason": "运行中的镜像端口不能热切换；旧端口继续服务，重启 daemon 后生效"})
            fresh.mirror_port = previous.mirror_port   # 不谎报：实际仍在用旧端口
        changed = [name for name in self.RELOAD_FIELDS
                   if getattr(previous, name, None) != getattr(fresh, name, None)]
        applied: list[str] = []
        notes: list[str] = []
        self.config = fresh
        if "gateway_url" in changed:
            # 下一次 tick 按新地址重建客户端；已建立的长连接不动（在途请求照常完成）
            self.api = None
            notes.append("服务端地址已切换；下次心跳起走新地址")
            applied.append("gateway_url")
        if "log_level" in changed:
            try:
                setup_logging(self.paths.log_file, level=fresh.log_level)
                applied.append("log_level")
            except (OSError, ValueError) as exc:            # pragma: no cover - 防御
                notes.append("日志级别未切换：" + (str(exc) or type(exc).__name__))
        for name in ("update_channel", "update_source"):
            if name in changed:
                applied.append(name)
        self.update.source = fresh.update_source
        self.update.channel = fresh.update_channel
        if "extra" in changed:
            # 镜像端点用 extra 做行为开关（如 tokens.compare）；直接换掉旧字典
            self.server.settings = dict(fresh.extra or {})
            applied.append("extra")
        # 上游地址：manager 每次都读 store，换实例只为把「已重载」这件事做实
        self.upstreams = UpstreamManager(self.store, self.credentials, http=self.http)
        upstream_count = len(self.upstreams.list())
        applied.extend(self._apply_subsystem_toggles(fresh))
        self._reload_count += 1
        fingerprint = sha256_hex(json.dumps(fresh.to_dict(), sort_keys=True,
                                            ensure_ascii=False))[:16]
        result: dict[str, Any] = {
            "ok": True,
            "changed": changed,
            "applied": applied,
            "unchanged": not changed,
            "restart_required": restart_required,
            "notes": notes,
            "fingerprint": fingerprint,
            "reload_count": self._reload_count,
            "upstreams": upstream_count,
            "inflight": self._inflight,
            "state": self.machine.snapshot.state,
            "config": self._config_public(fresh),
            "message": ("配置已重载（无变化）" if not changed
                        else "配置已重载并生效：" + ", ".join(changed)),
        }
        sample = self.store.policy()
        result["policy_version"] = getattr(sample, "policy_version", None)
        event("daemon.reloaded", changed=changed, applied=applied,
              restart_required=[item["field"] for item in restart_required],
              fingerprint=fingerprint, upstreams=upstream_count)
        self.last_reload = result
        return result

    def _consume_reload_flag(self) -> dict[str, Any] | None:
        """主循环消费一次 SIGHUP 请求；没请求时返回 None（幂等、不重复加载）。"""
        if not self._reload_requested:
            return None
        self._reload_requested = False
        result = self.reload_config()
        self._last_reload_signal_ok = bool(result.get("ok"))
        event("daemon.reload_signal", ok=self._last_reload_signal_ok,
              changed=result.get("changed"))
        return result

    def _on_sighup(self, _signum: int | None = None, _frame: Any = None) -> None:
        """SIGHUP 只置位：真正加载在主循环里做（信号上下文里不做 I/O、不抢锁）。"""
        self._reload_requested = True

    def _install_reload_signal(self) -> bool:
        """主线程才装 SIGHUP（Windows 无 SIGHUP：返回 False，走控制命令即可）。"""
        if not hasattr(signal, "SIGHUP"):
            return False
        if threading.current_thread() is not threading.main_thread():
            return False
        try:
            signal.signal(signal.SIGHUP, self._on_sighup)
        except (ValueError, OSError, RuntimeError):        # pragma: no cover - 受限环境
            return False
        return True

    # ── C10c 出站长轮询通道（0.2.4 起默认开；Kill switch 仍生效）
    def _load_channel_required(self) -> bool:
        """从 store kv 读服务端通道要求（P4：心跳协商结果跨重启保留）。"""
        raw = self.store.get_kv(facts.CHANNEL_REQUIRED_KV, default={}) or {}
        if isinstance(raw, dict):
            return raw.get("required") is True
        return False

    def _channel_flag_state(self, config: ClientConfig | None = None) -> bool:
        """通道「该不该工作」的唯一判定（本地开关 + 服务端协商）。

        签名接受一个 config 是为了与 ``_apply_subsystem_toggles`` 里其它开关同形；
        协商状态始终取自本实例（配置重载只换 config，不换协商结论）。
        """
        return channel_enabled(config if config is not None else self.config,
                               required=self._channel_required)

    def _channel_wanted(self) -> bool:
        return self._channel_flag_state(self.config)

    def _start_channel(self) -> None:
        """通道该工作时起线程（本地开关 or 服务端要求）；显式关闭时不创建任何线程。"""
        if not self._channel_wanted():
            return
        if self._channel_thread is not None and self._channel_thread.is_alive():
            return
        api = self._api_factory(self.config.gateway_url, self.credentials.client_key())
        self._channel_stop = threading.Event()
        self._channel = ChannelClient(self.paths, self.config, self.credentials,
                                      port=self.server.port, local_token=self.token,
                                      http=self.http, now=self._now, api=api)
        self._channel_thread = threading.Thread(target=self._channel_runner,
                                                name="djb-channel", daemon=True)
        self._channel_thread.start()
        event("channel.thread_started", port=self.server.port,
              wait_s=facts.CHANNEL_POLL_WAIT_S)

    def _channel_runner(self) -> None:
        """线程体：异常只记事件，绝不向上抛（不影响 daemon 主循环，M15 §7）。"""
        channel, stop = self._channel, self._channel_stop
        if channel is None or stop is None:                 # pragma: no cover - 防御
            return
        try:
            channel.run(stop)
        except Exception as exc:                            # pragma: no cover - 防御
            event("channel.thread_crashed", level="warning", error=type(exc).__name__)

    def _stop_channel(self) -> None:
        """置停止位并 join ≤ facts.CHANNEL_STOP_JOIN_S；已取 job 由通道自己尽力回传。"""
        if self._channel_stop is not None:
            self._channel_stop.set()
        thread = self._channel_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=facts.CHANNEL_STOP_JOIN_S)
        if thread is not None:
            event("channel.thread_joined", alive=thread.is_alive())
        self._channel_thread = None

    def _channel_status(self) -> dict[str, Any]:
        """status / 诊断用（只有状态与计数，无正文、无令牌）。

        ``enabled`` = 本地开关；``required`` = 服务端心跳要求；
        ``wanted`` = 「它现在应不应该在跑」（前两者的合成）。三者分开给，
        是为了让 CLI 的「通道：已连接/未开启/已断开」只按证据说话 ——
        拿不到证据时宁可说「已断开」，也不谎报「已连接」。
        """
        enabled = channel_enabled(self.config)
        snapshot = self._channel.snapshot() if self._channel is not None else {}
        running = bool(self._channel_thread is not None
                       and self._channel_thread.is_alive())
        return {"enabled": enabled, "running": running,
                "required": bool(self._channel_required),
                "wanted": self._channel_wanted(),
                "state": snapshot.get("state", "idle"),
                "port": self.server.port,
                "wait_s": facts.CHANNEL_POLL_WAIT_S,
                "jobs_done": int(snapshot.get("jobs_done", 0)),
                "jobs_failed": int(snapshot.get("jobs_failed", 0)),
                "last_job_id": snapshot.get("last_job_id"),
                "last_job_at": snapshot.get("last_job_at"),
                "last_error": snapshot.get("last_error")}

    def _apply_channel_required(self, result: Any, now: datetime) -> None:
        """心跳里的 ``channel_required``（P4 落点 B）：落盘 + 即时起停通道线程。

        只认真正的 bool —— 字段缺失 / 字符串 / null 一律忽略（契约是**加性字段**，
        老云端不发这个键时客户端行为必须逐字不变）。
        本地显式关闭仍然优先：这里只决定「要不要起来」，不覆盖 kill switch。
        """
        value = result.get("channel_required") if isinstance(result, dict) else None
        if not isinstance(value, bool) or value == self._channel_required:
            return
        self._channel_required = value
        self.store.set_kv(facts.CHANNEL_REQUIRED_KV,
                          {"required": value, "at": iso(now)})
        event("daemon.channel_required", required=value)
        running = self._subsystem_running("channel")
        if self._channel_wanted() and not running:
            self._start_channel()
        elif not self._channel_wanted() and running:
            self._stop_channel()

    # ── M18b 模型 / tokenizer 同步（默认关闭）
    def _start_model_sync(self) -> None:
        """开关 ``client.model_sync.enabled`` 打开时起同步线程；关闭时不拉取、不建线程。"""
        if not model_sync_enabled(self.config):
            return
        if self._model_sync_thread is not None and self._model_sync_thread.is_alive():
            return
        api = self._api_factory(self.config.gateway_url, self.credentials.client_key())
        self._model_sync_stop = threading.Event()
        self._model_sync = ModelSyncClient(self.paths, self.config, self.credentials,
                                           http=self.http, api=api, now=self._now,
                                           verifier=verifier_from_env())
        self._model_sync_thread = threading.Thread(target=self._model_sync_runner,
                                                   name="djb-model-sync", daemon=True)
        self._model_sync_thread.start()
        event("model_sync.thread_started_by_daemon",
              interval_s=facts.MODEL_SYNC_INTERVAL_S)

    def _model_sync_runner(self) -> None:
        """线程体：异常只记事件，不影响 daemon 主循环（M18 §5：同步失败不停接单）。"""
        client, stop = self._model_sync, self._model_sync_stop
        if client is None or stop is None:                  # pragma: no cover - 防御
            return
        try:
            client.run_periodic(stop)
        except Exception as exc:                            # pragma: no cover - 防御
            event("model_sync.thread_crashed", level="warning",
                  error=type(exc).__name__)

    def _stop_model_sync(self) -> None:
        """置停止位并 join ≤ facts.MODEL_SYNC_STOP_JOIN_S；已应用版本与本地文件保留。"""
        if self._model_sync_stop is not None:
            self._model_sync_stop.set()
        thread = self._model_sync_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=facts.MODEL_SYNC_STOP_JOIN_S)
        if thread is not None:
            event("model_sync.thread_joined", alive=thread.is_alive())
        self._model_sync_thread = None

    def _model_sync_status(self) -> dict[str, Any]:
        """status / 诊断用（只有版本号与计数，无正文、无凭据）。"""
        enabled = model_sync_enabled(self.config)
        running = bool(self._model_sync_thread is not None
                       and self._model_sync_thread.is_alive())
        if self._model_sync is not None:
            snapshot = self._model_sync.status()
        else:
            snapshot = {"current_version": 0, "applied_at": None, "models": 0,
                        "unsupported": [], "drift": [], "last_error": None,
                        "verified": False, "next_at": None}
        snapshot.update({"enabled": enabled, "running": running,
                         "interval_s": facts.MODEL_SYNC_INTERVAL_S})
        return snapshot

    # ── C16 消息中心（默认关闭）
    def _start_notifications(self) -> None:
        """开关开（或 DJB_NOTIFY=1）时起消息同步线程；关闭时不建线程、不动本地库。"""
        if not notifications_enabled(self.config):
            return
        if (self._notifications_thread is not None
                and self._notifications_thread.is_alive()):
            return
        api = self._api_factory(self.config.gateway_url, self.credentials.client_key())
        self._notifications_stop = threading.Event()
        self._notifications = NotificationStore(self.paths, config=self.config, api=api,
                                                http=self.http, now=self._now)
        self._notifications_thread = threading.Thread(
            target=self._notifications_runner, name="djb-notifications", daemon=True)
        self._notifications_thread.start()
        event("notifications.thread_started_by_daemon",
              interval_s=facts.NOTIFICATIONS_INTERVAL_S)

    def _notifications_runner(self) -> None:
        """线程体：异常只记事件（消息中心坏了不影响接单）。"""
        store, stop = self._notifications, self._notifications_stop
        if store is None or stop is None:                   # pragma: no cover - 防御
            return
        try:
            store.run_periodic(stop)
        except Exception as exc:                            # pragma: no cover - 防御
            event("notifications.thread_crashed", level="warning",
                  error=type(exc).__name__)
        finally:
            try:
                store.close()
            except Exception:                               # pragma: no cover - 防御
                pass

    def _stop_notifications(self) -> None:
        """置停止位并 join ≤ facts.NOTIFICATIONS_STOP_JOIN_S（本地消息与队列保留）。"""
        if self._notifications_stop is not None:
            self._notifications_stop.set()
        thread = self._notifications_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=facts.NOTIFICATIONS_STOP_JOIN_S)
        if thread is not None:
            event("notifications.thread_joined", alive=thread.is_alive())
        self._notifications_thread = None

    def _notifications_status(self) -> dict[str, Any]:
        """status / 诊断用：未读数 / 最近同步 / 待补发（不含 payload）。"""
        enabled = notifications_enabled(self.config)
        running = bool(self._notifications_thread is not None
                       and self._notifications_thread.is_alive())
        if self._notifications is not None:
            snapshot = self._notifications.status()
        else:
            snapshot = {"unread": 0, "total": 0, "pending": 0, "last_sync": None,
                        "last_error": None, "cursor": None, "syncs": 0}
        snapshot.update({"enabled": enabled, "running": running,
                         "interval_s": facts.NOTIFICATIONS_INTERVAL_S})
        return snapshot

    # ── CR-20261106-OFFICIAL-SUPPLY-01 报价发布（默认关闭；与 channel / model_sync 同构）
    def _quotes_period(self) -> int | None:
        """当前发布周期（秒）。配置非法时返回 ``None``（fail-closed：本轮不发）。"""
        try:
            return quotes_mod.resolve_period(self.config)
        except DjbError as exc:
            message = str(getattr(exc, "message", None) or exc)
            if message != self._quotes_error_logged:
                # 同一份坏配置只记一次，避免每轮刷日志
                self._quotes_error_logged = message
                event("quotes.period_invalid", level="warning", error=message)
            return None

    def _start_quotes(self) -> None:
        """开关打开时起报价线程（关闭 = 不建线程、不发任何请求）。

        周期配置非法时**也起线程**，但每一轮都 fail-closed 不发（`_quotes_period()`
        返回 ``None``）：这样（1）线程状态与「开关已开」一致，reload 回执不撒谎；
        （2）改好配置 `djb daemon reload` 后下一轮自动恢复，不用再开关一次。
        """
        if not quotes_enabled(self.config):
            return
        if self._quotes_thread is not None and self._quotes_thread.is_alive():
            return
        period = self._quotes_period()
        if period is None:
            self._quotes_period_s = facts.QUOTES_PERIOD_DEFAULT_S
            period = self._quotes_period_s
        self._quotes_period_s = period
        self._quotes_error_logged = ""
        self._quotes_stop = threading.Event()
        self._quotes_thread = threading.Thread(target=self._quotes_runner,
                                               name="djb-quotes", daemon=True)
        self._quotes_thread.start()
        event("quotes.thread_started", period_s=period,
              ttl_s=facts.QUOTE_TTL_MULTIPLE * period)

    def _quotes_runner(self) -> None:
        """线程体：**永不抛出**（一轮失败只记事件，不影响心跳主环与接单）。"""
        stop = self._quotes_stop
        if stop is None:                                       # pragma: no cover - 防御
            return
        while not stop.is_set():
            period = self._quotes_period()
            try:
                if period is not None:
                    self._publish_quotes(period, reason="periodic")
            except Exception as exc:                           # pragma: no cover - 防御
                event("quotes.publish_error", level="warning", error=type(exc).__name__)
            interval = period if period is not None else facts.QUOTES_PERIOD_DEFAULT_S
            if stop.wait(max(1.0, float(interval))):
                break

    def _publish_quotes(self, period_s: Any, *, reason: str = "manual") -> dict[str, Any]:
        """发布一轮报价：生成行 → 逐行发送 → 记**一行**计数日志。

        日志只有数字：本轮行数 / created / reused / failed / 跳过原因计数 / 失败分类。
        **不记价格明细，也不记模型清单**（模型名属于供方商业信息）。
        """
        # 走 resolve_period 而不是只看入参：配置里 `quotes.ttl_s` 与周期不自洽时
        # 必须 fail-closed（抛 UsageError，由调用方拦住这一轮），不能带病上行。
        period = quotes_mod.resolve_period(self.config, period_s=period_s)
        plan = quotes_mod.plan_quotes(self.upstreams, now=self._now(), period_s=period)
        self._quotes_period_s = period
        key = self.credentials.client_key()
        result = quotes_mod.PublishResult()
        withheld = ""
        if not key:
            # 未注册 = 还没有节点身份，报价一定 401：不刷请求，只记一次原因
            withheld = "no_client_key"
        else:
            api = (self.api if (self.api is not None and self.api.client_key == key)
                   else self._api_factory(self.config.gateway_url, key))
            result = quotes_mod.publish(api, plan.rows,
                                        node_id=self.credentials.node_id() or None)
        self._last_quotes = {
            "at": iso(self._now()), "reason": reason, "period_s": period,
            "ttl_s": facts.QUOTE_TTL_MULTIPLE * period, "rows": len(plan.rows),
            "created": result.created, "reused": result.reused, "failed": result.failed,
            "skipped_total": plan.skipped_total, "skipped": dict(plan.skipped),
            "error_classes": sorted({str(item.get("error")) for item in result.errors}),
        }
        if withheld:
            event("quotes.publish_skipped", reason=withheld, rows=len(plan.rows))
        else:
            event("quotes.publish", **self._last_quotes)
        return dict(self._last_quotes)

    def _stop_quotes(self) -> None:
        if self._quotes_stop is not None:
            self._quotes_stop.set()
        thread = self._quotes_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=facts.QUOTES_STOP_JOIN_S)
        if thread is not None:
            event("quotes.thread_joined", alive=thread.is_alive())
        self._quotes_thread = None

    def _quotes_status(self) -> dict[str, Any]:
        """status / 诊断用：开关 / 线程 / 周期 / 上一轮计数（不含价格明细）。"""
        enabled = quotes_enabled(self.config)
        running = bool(self._quotes_thread is not None and self._quotes_thread.is_alive())
        snapshot = dict(self._last_quotes or {"at": None, "rows": 0, "created": 0,
                                              "reused": 0, "failed": 0,
                                              "skipped_total": 0, "skipped": {},
                                              "error_classes": []})
        snapshot.update({"enabled": enabled, "running": running,
                         "period_s": self._quotes_period_s,
                         "ttl_s": facts.QUOTE_TTL_MULTIPLE * self._quotes_period_s})
        return snapshot

    # ── tick
    def _seconds_until_next_due(self) -> float:
        now = self._now()
        candidates = [(self._next_heartbeat_at - now).total_seconds(),
                      (self._next_receipt_flush_at - now).total_seconds(),
                      1.0]
        if self._periodic_probe_enabled():
            candidates.append((self._next_probe_at - now).total_seconds())
        return max(0.1, min(candidates))

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self._now()
        if self._stop_event.is_set():
            return {"stopped": True}
        self._consume_reload_flag()                 # F14：SIGHUP 置位 → 主循环里加载
        if self.api is None:
            self.api = self._api_factory(self.config.gateway_url,
                                         self.credentials.client_key())
        if not self.credentials.client_key():
            # 离线注册重试：只对网络/限流失败重试（未知状态不重建密钥，M15 §7）。
            # P5：「硬件指纹已被占用」是**永久性**错误（一机一节点），自动重试只会把日志
            # 刷成噪声、也不会自己变好 —— 必须先由人复用密钥 / 解绑 / 加机器，故不再重试。
            if moment >= self._next_heartbeat_at and not self._register_conflict_pending():
                if self._register():
                    if self.api is not None:
                        self.api.client_key = self.credentials.client_key()
                    self._next_heartbeat_at = moment
        # 周期复探在心跳之前：让本轮心跳带的是最新容量（C7）
        if self._periodic_probe_enabled() and moment >= self._next_probe_at:
            self._probe_upstreams(moment, reason="periodic")
        if moment >= self._next_heartbeat_at:
            self._heartbeat(moment)
        if not self._policy_updated and self._policy_refresh_due(moment):
            self._policy_updated = True
        if self._policy_updated:
            self._policy_updated = False
            self._fetch_policy(moment)
        if moment >= self._next_receipt_flush_at:
            self._flush_receipts(moment)
        decision = self._evaluate(moment)
        self._persist()
        return {"state": self.machine.snapshot.to_dict(), "decision": decision.to_dict(),
                "heartbeat_failures": self._consecutive_failures,
                "receipts": self.buffer.count(), "inflight": self._inflight}

    # ── 机器画像（M3；本地展示始终可用，上报受开关控制）
    def machine_eval(self, *, refresh: bool = False) -> dict[str, Any] | None:
        """本机 `djb.machine-eval/1` 文档（懒采集、进程内缓存）。采集失败 -> None。"""
        if self._machine_eval is not None and not refresh:
            return self._machine_eval
        try:
            self._machine_eval = machine_profile.build_machine_eval(
                home=getattr(self.paths, "home", None), benchmark=True)
        except Exception as exc:                        # 画像失败绝不影响 daemon
            event("daemon.machine_profile_failed", level="warning",
                  error=type(exc).__name__ + ": " + str(exc)[:160])
            self._machine_eval = None
        return self._machine_eval

    def _machine_payload(self) -> dict[str, Any] | None:
        """心跳里带的机器摘要；开关关闭 -> None（字段整体不出现，老服务端零感知）。"""
        if not feature_flag(self.config, facts.MACHINE_PROFILE_FLAG):
            return None
        doc = self.machine_eval()
        if not doc:
            return None
        return {"schema": doc.get("schema"), "collected_at": doc.get("collected_at"),
                "platform": doc.get("platform"), "cpu": doc.get("cpu"),
                "memory": doc.get("memory"), "gpus": doc.get("gpus"),
                "disks": doc.get("disks"), "load": doc.get("load"),
                "benchmark": doc.get("benchmark")}

    # ── 心跳
    def _relay_capacity_targets(self, now: datetime) -> dict[str, list[dict[str, Any]]]:
        """C9 接线：relay 发现结果 → 容量目标（开关关闭时返回空 dict，行为不变）。"""
        manager = RelayManager(self.store, self.credentials, config=self.config,
                               now=lambda: now)
        if not manager.enabled():
            return {}
        return capacity_targets(manager.list(), now=now)

    def _heartbeat(self, now: datetime, *, initial: bool = False) -> None:
        if self.api is None:
            self._next_heartbeat_at = now + timedelta(seconds=self._heartbeat_interval_s)
            return
        if not self.credentials.client_key():
            self._next_heartbeat_at = now + timedelta(seconds=facts.HEARTBEAT_DEFAULT_S)
            return
        snapshot = self.machine.snapshot
        profile = self.store.profile()
        current_policy = self.store.policy()
        payload = {
            "node_id": self.credentials.node_id() or "",
            "protocol_version": facts.PROTOCOL_VERSION,
            "policy_version": current_policy.policy_version if current_policy else "",
            "runtime_state": snapshot.runtime_state(),
            "capacity": self.upstreams.announce_capacity(
                profile, now=now, relay=self._relay_capacity_targets(now)),
            "load": {"inflight": self._inflight, "queue_seconds": 0.0},
        }
        # M3：加性字段（开关关闭时不出现；云端忽略未知键，老版本零感知）
        machine = self._machine_payload()
        if machine is not None:
            payload["machine"] = machine
        try:
            result = self.api.heartbeat(payload)
        except ForbiddenError as exc:
            self._on_heartbeat_forbidden(exc, now)
            return
        except RateLimitError as exc:
            self._on_failure(now, exc, kind="heartbeat")
            return
        except DjbError as exc:
            self._on_failure(now, exc, kind="heartbeat")
            return
        self._consecutive_failures = 0
        self._forbidden_failures = 0
        # P4 落点 B：服务端用心跳协商通道要求（加性字段，老云端不发 = 不改行为）
        self._apply_channel_required(result, now)
        interval = result.get("next_heartbeat_seconds")
        if not isinstance(interval, int) or isinstance(interval, bool):
            interval = facts.HEARTBEAT_DEFAULT_S
        interval = max(facts.HEARTBEAT_MIN_S, min(facts.HEARTBEAT_MAX_S, int(interval)))
        self._heartbeat_interval_s = interval
        self._next_heartbeat_at = now + timedelta(seconds=interval)
        if result.get("policy_updated") is True:
            self._policy_updated = True
        announcement = result.get("announcement")
        if isinstance(announcement, dict):
            seq = announcement.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.store.record_announcement(seq, announcement)
        self.store.set_kv("heartbeat", {"last_ok_at": iso(now),
                                        "interval_s": interval,
                                        "policy_version": result.get("policy_version"),
                                        "runtime_state": snapshot.runtime_state()})
        event("daemon.heartbeat_ok", interval_s=interval,
              policy_updated=bool(result.get("policy_updated")),
              runtime_state=snapshot.runtime_state())
        if initial:
            self._evaluate(now)

    def _on_heartbeat_forbidden(self, exc: ForbiddenError, now: datetime) -> None:
        """403（40305 等）：暂停 + **必须退避**，否则 tick 循环会以 ~110ms 间隔重发心跳。

        P0（2026-11-07）：原来这里只 ``machine.pause()``，不推进 ``_next_heartbeat_at``，
        而 ``_on_failure`` 会退避 —— 节点停在 ``pending`` 时日志以 ~110ms 间隔刷
        ``daemon.heartbeat_forbidden``（实测一分钟内 184 条）。现在复用同一套
        :class:`~djb_node.backoff.Backoff`，下限 ``facts.HEARTBEAT_MIN_S``（服务端 5s 限流）。
        """
        reason = self._forbidden_reason(exc)
        self.machine.pause(reason, next_step=NEXT_STEP_FOR(reason),
                           detail={"code": exc.code})
        delay = self._backoff_heartbeat_after_forbidden(now)
        self._persist()
        event("daemon.heartbeat_forbidden", level="warning", reason=reason,
              code=exc.code, retry_in_s=delay)

    def _backoff_heartbeat_after_forbidden(self, now: datetime) -> int:
        """403 后的下一次心跳时间：用独立计数走 Backoff，绝不「立刻重试」。"""
        from .backoff import Backoff
        self._forbidden_failures += 1
        decision = Backoff(now=self._now).after_failure(self._forbidden_failures)
        delay = decision.delay_s if decision.delay_s is not None else facts.RETRY_DELAYS_S[-1]
        delay = int(max(facts.HEARTBEAT_MIN_S, delay))
        self._next_heartbeat_at = now + timedelta(seconds=delay)
        return delay

    def _forbidden_reason(self, exc: ForbiddenError) -> str:
        """403 → 暂停原因：**先读服务端下发的 state**，再退回文案关键词。

        P0（2026-11-07）：云端 40305 的响应把节点当前状态平铺在 ``error.state``
        （``app/supply/router.py::_fail`` + ``NodeStateForbidden``）。以前只按异常文本猜，
        ``pending``（等审核）落到兜底 ``account_frozen``，界面显示
        「账户/节点被冻结：查看平台通知并申诉」——把「等审核」说成了「被封」。
        """
        state = str((exc.details or {}).get("state") or "").strip().lower()
        if state == "pending":
            return "account_pending"
        if state in ("frozen", "suspended"):
            return "account_frozen"
        if state == "retired":
            return "retired"
        text = (exc.message or "").lower()
        if "retired" in text or "清退" in text:
            return "retired"
        if "pending" in text or "审核" in text:
            return "account_pending"
        if "frozen" in text or "冻结" in text:
            return "account_frozen"
        if "license" in text or "credential" in text or "key" in text:
            return "credential_invalid"
        return "account_frozen"

    def _on_failure(self, now: datetime, exc: DjbError, *, kind: str) -> None:
        self._consecutive_failures += 1
        from .backoff import Backoff
        decision = Backoff(now=self._now).after_failure(self._consecutive_failures)
        delay = decision.delay_s if decision.delay_s is not None else facts.RETRY_DELAYS_S[-1]
        if kind == "heartbeat":
            self._next_heartbeat_at = now + timedelta(seconds=max(
                facts.HEARTBEAT_MIN_S, delay))
        event("daemon." + kind + "_failed", level="warning",
              error=type(exc).__name__, code=exc.code,
              failures=self._consecutive_failures, retry_in_s=delay)
        if isinstance(exc, AuthError):
            self.machine.pause("credential_invalid",
                               next_step="凭据失效：djb auth --set-key 更换密钥，或重新绑定")
            self._persist()
        elif self._consecutive_failures >= facts.HEARTBEAT_FAIL_CONSERVATIVE_AFTER:
            self.machine.pause("conservative",
                               next_step="连续失败进入保守模式（不接新单）；网络恢复后自动重试")
            self._persist()

    # ── 探针调度（C7：L1–L3 + 公告门控前置 + 周期复探）
    def _periodic_probe_enabled(self) -> bool:
        """开关 `client.probe.periodic.enabled`（**0.2.5 起默认开启**；见 facts.FEATURE_FLAGS）。

        关闭有三条等价路径（回滚规则）：配置 `feature_flags[...]=false`、
        `djb flags --disable client.probe.periodic.enabled`、
        环境变量 `DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=false`（压过配置）。
        三者都走 `config.feature_flag` 这一个入口，所以 status / flags / daemon 三处口径一致。
        """
        return feature_flag(self.config, facts.PROBE_FLAG_PERIODIC)

    def _probe_upstreams(self, now: datetime, *, reason: str) -> dict[str, Any]:
        """对启用中的上游做一轮探针（L1→L2/L3），结论写回本地模型。

        永不抛异常：`facts.PROBE_PASS_BUDGET_S` 是单轮预算，
        超预算的模型留到下一轮（`not_probed`，不算失败）。
        失败按指数退避拉长下一轮间隔（`probe.periodic_interval_s`）。
        """
        deadline = now + timedelta(seconds=facts.PROBE_PASS_BUDGET_S)
        verified = 0
        failed = 0
        models = 0
        for record in self.upstreams.list():
            if not record.enabled:
                continue
            if self._now() >= deadline:
                break
            try:
                payload = self.upstreams.probe(
                    record.id, timeout=facts.PROBE_TIMEOUT_S, now=now,
                    deadline=deadline, clock=self._now)
            except DjbError as exc:
                failed += 1
                event("upstream.probe_failed", level="warning", reason=reason,
                      upstream_id=record.id, error=type(exc).__name__, code=exc.code)
                continue
            except Exception as exc:                        # pragma: no cover - 防御
                failed += 1
                event("upstream.probe_error", level="warning", reason=reason,
                      upstream_id=record.id, error=type(exc).__name__)
                continue
            checks = payload.get("model_checks") or []
            models += len(checks)
            # 与服务端口径对齐（2026-11-04，D-PROBE-03）：云端 `_probe_verdict` 是
            # **任一检查失败即 rejected**（零检查 ⇒ rejected/no_model_check）。
            # 旧版这里用 `any(...)`：部分模型可用时节点自以为 verified、云端判 rejected，
            # 两边日志对不上，排障时容易误判。改为 all + 非空，逐字对齐。
            if checks and all(item.get("ok") for item in checks):
                verified += 1
                # D-PROBE-03：**周期探针的结论必须上报**，否则云端 verified_at 不动。
                # 旧行为：daemon 只写本地 last_probe_at ⇒ 本地算出正 QPS，但云端
                # `_gate_unverified(cutoff=now-900s)` 仍按旧的 verified_at 判过期 ⇒
                # 15 分钟后容量再次归零，且没有任何自动恢复路径（只能人工 `djb upstream probe`）。
                # 上报走与 CLI 完全相同的一条路（含服务端 upstream_id 映射），失败不影响本地结论。
                self._report_probe(record, payload, reason=reason)
            else:
                failed += 1
        self._probe_failures = min(self._probe_failures + 1, facts.PROBE_MAX_BACKOFF_SHIFT) \
            if failed else 0
        self._probe_interval_s = periodic_interval_s(self._upstream_model_count(),
                                                     self._probe_failures)
        self._next_probe_at = now + timedelta(seconds=self._probe_interval_s)
        self._last_probe = {"at": iso(now), "reason": reason, "verified": verified,
                            "failed": failed, "models": models,
                            "failures": self._probe_failures,
                            "interval_s": self._probe_interval_s}
        event("upstream.probe_pass", reason=reason, verified=verified, failed=failed,
              models=models, failures=self._probe_failures,
              interval_s=self._probe_interval_s)
        return self._last_probe

    def _upstream_model_count(self) -> int:
        return sum(len(item.model_ids()) for item in self.upstreams.list() if item.enabled)

    def _report_probe(self, record: Any, payload: dict[str, Any], *, reason: str) -> None:
        """把一次探针结论上报服务端（D-PROBE-03）。**永不抛异常**。

        为什么必须上报：云端 `verified_at` 是容量门控的另一半。只更新本地
        `last_probe_at` 时，节点自己的公告有正 QPS，云端却仍按旧的 `verified_at`
        判"未验证太久" ⇒ 15 分钟后容量归零，且**只有人工 probe 能刷新**。

        口径与 `cli.py` 的 `upstream probe` 完全一致：
        * 去掉本地字段 `upstream_id`（服务端 id 与本地 id 不同）；
        * 用 `upstream_server_ids` 映射换服务端 id，映射缺失时退回本地 id
          （与 CLI 同形，不额外发明规则）。
        """
        if self.api is None:
            event("upstream.probe_report_skipped", level="warning", reason=reason,
                  upstream_id=record.id, why="api_unavailable")
            return
        report = {k: v for k, v in dict(payload).items() if k != "upstream_id"}
        try:
            mapping = self.store.get_kv("upstream_server_ids", default={}) or {}
        except Exception:                                    # pragma: no cover - 防御
            mapping = {}
        server_id = mapping.get(str(record.id), record.id) if isinstance(mapping, dict) \
            else record.id
        try:
            self.api.report_probe({"upstream_id": server_id, **report})
        except DjbError as exc:
            event("upstream.probe_report_failed", level="warning", reason=reason,
                  upstream_id=record.id, error=type(exc).__name__, code=exc.code)
            return
        except Exception as exc:                             # pragma: no cover - 防御
            event("upstream.probe_report_error", level="warning", reason=reason,
                  upstream_id=record.id, error=type(exc).__name__)
            return
        event("upstream.probe_reported", reason=reason, upstream_id=record.id,
              server_upstream_id=server_id)

    def probe_now(self, *, reason: str = "manual") -> dict[str, Any]:
        """手动触发一轮探针（控制通道 / 测试用；不受周期开关影响）。"""
        return self._probe_upstreams(self._now(), reason=reason)

    # ── 策略
    # ── 策略租约（D-POLICY-01）
    def _policy_ttl_s(self, policy: Any) -> int:
        """本地租约时长（秒）：优先策略里的 ``policy_ttl_s``，否则用 facts 默认值。"""
        ttl = policy.settings.get("policy_ttl_s") if policy is not None else None
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
            ttl = facts.POLICY_DEFAULT_TTL_S
        return int(ttl)

    def _renew_local_policy(self, current: Any, now: datetime, *, via: str) -> Any:
        """内容不变时的**续租**：版本与 digest 不动，只把本地 ``expires_at`` 推到 now+TTL。

        spec ``NodePolicy`` 没有 ``expires_at`` 字段（CR-M15-05），租约完全由客户端按
        ``issued_at + TTL`` 自行推算；因此"服务端说没变"必须翻译成"续租"，否则本地会在
        内容不变的情况下自行过期。事件里带 ``via`` 以便区分 304 / 200 两条来路。
        """
        ttl = self._policy_ttl_s(current)
        renewed = policy_mod.parse_policy({
            "policy_version": current.policy_version,
            "protocol_version": current.protocol_version,
            "issued_at": current.issued_at,
            "digest": current.digest,
            "quotas": current.quotas,
            "directives": current.directives,
            "settings": {**current.settings, "policy_ttl_s": ttl},
            "expires_at": iso(now + timedelta(seconds=ttl)),
        }, now=now)
        self.store.save_policy(renewed)
        event("daemon.policy_renewed", policy_version=current.policy_version,
              expires_at=renewed.expires_at, via=via)
        return renewed

    def _policy_refresh_due(self, now: datetime) -> bool:
        """是否该主动拉一次策略：**没有策略 / 已过期 / 剩余不足 10 分钟**。

        修复前只有"服务端说 policy_updated"或启动时才拉策略，于是内容不变 ⇒ 永不拉 ⇒
        租约到期后永久 conservative。这里给一个限频（``POLICY_REFRESH_MIN_INTERVAL_S``）
        的前瞻刷新，让"内容不变"不再等于"永不续租"。
        """
        if now < self._next_policy_fetch_at:
            return False
        policy = self.store.policy()
        if policy is None:
            due = True
        else:
            # 剩余寿命以**记录里的 expires_at** 为准（续租只推 expires_at，不动
            # issued_at —— issued_at 是"服务端签发时刻"，不是"租约起点"）。
            expires = parse_iso(policy.expires_at)
            if expires is None:
                ttl = self._policy_ttl_s(policy)
                issued = parse_iso(policy.issued_at)
                expires = None if issued is None else issued + timedelta(seconds=ttl)
            due = True if expires is None else \
                (expires - now).total_seconds() <= POLICY_REFRESH_AHEAD_S
        if due:
            self._next_policy_fetch_at = now + timedelta(
                seconds=POLICY_REFRESH_MIN_INTERVAL_S)
        return due

    def _fetch_policy(self, now: datetime) -> None:
        if self.api is None or not self.credentials.client_key():
            return
        current = self.store.policy()
        try:
            result: PolicyFetch = self.api.policy(
                if_none_match=current.policy_version if current else None)
        except ForbiddenError as exc:
            self._on_heartbeat_forbidden(exc, now)
            return
        except DjbError as exc:
            self._on_failure(now, exc, kind="policy")
            return
        if result.not_modified:
            # 304 = 服务端确认 digest 仍是最新：按本地 TTL 续租（spec NodePolicy 无 expires_at，
            # 见 CR-M15-05），避免「服务端 304 但本地租约到期」永远保守
            if current is not None:
                self._policy_invalid = False
                self.store.set_kv("policy_invalid", False)
                self._renew_local_policy(current, now, via="304")
                return
            event("daemon.policy_not_modified", policy_version=None)
            return
        if result.payload is None:
            self.machine.pause("conservative", next_step="策略响应为空：保持保守模式")
            self._persist()
            return
        try:
            record = policy_mod.parse_policy(result.payload, now=now)
        except PolicyError as exc:
            self._policy_invalid = True
            self.store.set_kv("policy_invalid", True)
            self.machine.pause("conservative",
                               next_step="策略校验失败（" + exc.message
                               + "）：保持保守模式，等待服务端修复")
            self._persist()
            event("daemon.policy_invalid", level="warning", error=exc.message[:120])
            return
        self._policy_invalid = False
        self.store.set_kv("policy_invalid", False)
        # D-POLICY-01：**服务端返回 200 但版本与 digest 都没变**，等价于「确认你手上的策略仍是最新」
        # ——这与 304 是同一件事，必须同样按本地租约续租。
        #
        # 修复前这里只处理 304：条件拉取开关关闭时服务端一律 200（内容不变则沿用老
        # `issued_at`），客户端每次都用老 `issued_at` 覆盖本地，`expires_at = issued_at + TTL`
        # 于是**永远过期** ⇒ `_evaluate` 判定 conservative ⇒ `admit()` 拒绝一切 job
        # （`no_upstream`）⇒ 平台把节点"不接单"报成"上游异常"。
        if current is not None and record.policy_version == current.policy_version \
                and record.digest == current.digest:
            self._renew_local_policy(current, now, via="200_same_version")
            return
        if result.etag and result.etag != record.policy_version:
            record.settings = dict(record.settings)
            record.settings["etag"] = result.etag
        self.store.save_policy(record)
        event("daemon.policy_applied", policy_version=record.policy_version,
              verify_state=record.verify_state, quotas=len(record.quotas),
              directives=record.directives)

    # ── 收据
    def _flush_receipts(self, now: datetime) -> None:
        self._next_receipt_flush_at = now + timedelta(seconds=30)
        marked = self.buffer.mark_stale_expired(now=now)
        if marked:
            # Lead 2026-10-24 裁决（CR-2）：超 24 h 不丢弃，标 stale + 停接单 + 退避重推
            event("receipts.stale_marked", level="warning", count=len(marked),
                  stale_total=self.buffer.stale_count())
            self._backlog_block = True
            self.store.set_kv("receipt_backlog_block", True)
            self._transition_pause("receipt_backlog",
                                   next_step="收据超过 24 小时已标 stale（未丢弃）：停止接单，"
                                             "恢复网络后退避重推；诊断包可见 stale 计数")
        if self.buffer.must_stop_accepting(now=now):
            self._transition_pause("receipt_backlog")
        pending = self.buffer.pending(limit=facts.RECEIPT_BATCH_MAX, now=now)
        if not pending or self.api is None or not self.credentials.client_key():
            return
        batch = pending[:facts.RECEIPT_BATCH_MAX]
        ids = sorted(str(item.get("receipt_id")) for item in batch)
        idempotency_key = "djb-receipts-" + sha256_hex(",".join(ids))
        try:
            result = self.api.submit_receipts(batch, idempotency_key=idempotency_key)
        except ForbiddenError as exc:
            self._on_heartbeat_forbidden(exc, now)
            return
        except DjbError as exc:
            self._retry_receipts(batch, now, exc)
            return
        accepted = [str(item) for item in (result.get("accepted") or [])
                    if isinstance(item, (str, int))]
        duplicates = [str(item) for item in (result.get("duplicates") or [])
                      if isinstance(item, (str, int))]
        acked = self.buffer.ack(accepted + duplicates)
        rejected = result.get("rejected") or []
        for item in rejected:
            if isinstance(item, dict) and item.get("receipt_id"):
                self.store.delete_receipt(str(item["receipt_id"]))
                event("receipts.rejected", level="warning",
                      code=item.get("code"),
                      message=str(item.get("message") or "")[:120])
        if acked and not self.buffer.must_stop_accepting(now=now):
            self._backlog_block = False
            self.store.set_kv("receipt_backlog_block", False)
        event("receipts.flushed", accepted=len(accepted),
              duplicates=len(duplicates), rejected=len(rejected), acked=acked,
              remaining=self.buffer.count())
        if acked or rejected:
            self._next_receipt_flush_at = now

    def _retry_receipts(self, batch: list[dict[str, Any]], now: datetime,
                        exc: DjbError) -> None:
        from .backoff import Backoff
        for item in batch:
            receipt_id = str(item.get("receipt_id"))
            row = next((candidate for candidate in self.store.receipts(limit=10000)
                        if candidate.receipt_id == receipt_id), None)
            attempts = (row.attempts if row else 0) + 1
            decision = Backoff(now=self._now).after_failure(attempts, now=now)
            self.buffer.retry(receipt_id, attempts=attempts,
                              next_at=decision.next_at,
                              error=type(exc).__name__)
        event("receipts.flush_failed", level="warning", count=len(batch),
              error=type(exc).__name__)

    # ── 裁决
    def _evaluate(self, now: datetime) -> Decision:
        policy = self.store.policy()
        policy_ok = False
        policy_reason: str | None = None
        if policy is not None:
            policy_ok, policy_reason = policy_mod.verify(policy, now=now)
        conservative = (not policy_ok) or self._policy_invalid or \
            (self._consecutive_failures >= facts.HEARTBEAT_FAIL_CONSERVATIVE_AFTER)
        unbound = not bool(self.credentials.client_key())
        if policy is not None and policy_ok:
            paused, directive_reason = policy_mod.directives_pause(policy)
            if paused:
                self._transition_pause("conservative", next_step=directive_reason)
                return Decision(False, "conservative", "conservative",
                                directive_reason or "")
        if self._user_paused:
            return Decision(False, "user_paused", "user_paused",
                            "用户已暂停：djb resume 恢复接单")
        snapshot = self.machine.snapshot
        if snapshot.reason in ("account_frozen", "account_pending", "retired"):
            return Decision(False, str(snapshot.reason), str(snapshot.reason),
                            NEXT_STEP_FOR(str(snapshot.reason)))
        if self._backlog_block or self.buffer.must_stop_accepting(now=now):
            self._transition_pause("receipt_backlog")
            return Decision(False, "receipt_backlog", "receipt_backlog",
                            "收据积压超过 24 小时：已停止接单；确认恢复后 djb resume")
        decision = self.limiter.evaluate(profile=self.store.profile(), policy=policy,
                                         inflight=self._inflight, unbound=unbound,
                                         conservative=conservative, now=now)
        self._last_yield = decision.detail if decision.reason in (
            "yield_paused", "resource_limited") else {}
        if decision.accept:
            self._transition_online()
            self._next_receipt_flush_at = min(self._next_receipt_flush_at,
                                              now + timedelta(seconds=30))
            return decision
        if decision.pause_reason:
            self._transition_pause(decision.pause_reason,
                                   next_step=decision.next_step, detail=decision.detail)
        return decision

    # ── 服务端 Runtime 协议（本机回环 API）
    def admit(self, model: str) -> Admission:
        snapshot = self.machine.snapshot
        if self._user_paused:
            return Admission(False, 503, facts.LOCAL_ERROR_CODES["no_upstream"],
                             "policy_blocked", "用户已暂停接单：djb resume 恢复",
                             detail={"state": snapshot.state})
        if snapshot.state != "online":
            code = (facts.LOCAL_ERROR_CODES["quota_exceeded"]
                    if snapshot.reason == "quota_exhausted" else
                    facts.LOCAL_ERROR_CODES["no_upstream"])
            status = 402 if snapshot.reason == "quota_exhausted" else 503
            reason = "quota_exceeded" if snapshot.reason == "quota_exhausted" \
                else "policy_blocked"
            return Admission(False, status, code, reason,
                             "当前不接单（" + reason_label(snapshot.reason) + "）："
                             + (snapshot.next_step or "djb status 查看原因"),
                             detail={"state": snapshot.state,
                                     "reason": snapshot.reason})
        if not self._has_model(model):
            return Admission(False, 503, facts.LOCAL_ERROR_CODES["no_upstream"],
                             "policy_blocked", "该模型不在已启用的上游清单里",
                             detail={"model": model})
        if self._inflight >= max(1, self.store.profile().concurrent):
            return Admission(False, 429, facts.LOCAL_ERROR_CODES["rate_limited"],
                             "rate_limited", "并发已满：稍后重试",
                             retry_after=2,
                             detail={"inflight": self._inflight})
        return Admission(True)

    def _has_model(self, model: str) -> bool:
        return self.upstreams.route(model) is not None

    def route(self, model: str) -> RouteTarget | None:
        found = self.upstreams.route(model)
        if found is None:
            return None
        record, entry = found
        secret = self.credentials.upstream_secret(record.id)
        return RouteTarget(base_url=record.base_url, model=str(entry.get("id")),
                           access=record.access_type, upstream_id=record.id,
                           credential=secret,
                           protocol=getattr(record, "protocol", None) or "openai-chat")

    # ── plan 组（开关 plan.groups；默认关闭时下列方法不会被调用）
    def plan_groups(self) -> list[dict[str, Any]]:
        """plan 组快照（组 + 成员 + 已花预算），形状即 ``plan_router.pick_target`` 的入参。"""
        return self.store.plan_groups()

    def plan_route(self, upstream_id: str, model: str) -> RouteTarget | None:
        """把组内成员解析成 RouteTarget；该上游不服务此模型 / 已禁用 -> ``None``。

        凭据只从本机 0600 存储读出放进 ``RouteTarget.credential``，**不进日志**。
        """
        try:
            record = self.store.upstream(str(upstream_id))
        except UsageError:                              # 上游不存在 = 该成员不可用
            return None
        if not record.enabled:
            return None
        entry = next((item for item in record.models
                      if str(item.get("id")) == model and item.get("enabled", True)), None)
        if entry is None:
            return None
        return RouteTarget(base_url=record.base_url, model=str(entry.get("id")),
                           access=record.access_type, upstream_id=record.id,
                           credential=self.credentials.upstream_secret(record.id),
                           protocol=getattr(record, "protocol", None) or "openai-chat")

    def plan_charge(self, target: Any, model: str,
                    usage: tuple[int, int, int] | None) -> dict[str, Any]:
        """把一次**成功**请求记到组预算（C 段口径）。

        ``target`` 是 ``plan_router.PlanTarget``（``.group`` 是组名、``.model`` 是实际模型）。

        * 有 usage → 按「单价 × token」算金额（minor）；单价缺失 / 单位不认识 → 只记 token；
        * 没有 usage → 只把 ``unknown_usage_count`` +1（不记金额、不记 token）；
        * 记账是单条 SQL UPDATE，跨天 / 跨月窗口在同一句里归零。
        """
        group_key = str(getattr(target, "group", "") or "")      # 组名（PlanTarget.group）
        if not group_key:
            return {}
        tokens = None if usage is None else max(0, int(usage[2]))
        price: Any = None
        unit: str | None = None
        try:
            record = self.store.upstream(str(getattr(target, "upstream_id", "") or ""))
        except UsageError:                              # 上游已删：拿不到单价，只记 token
            record = None
        if record is not None:
            for item in record.models:
                if str(item.get("id")) == model:
                    price = item.get("min_price")
                    unit = item.get("min_price_unit")
                    break
        cost_minor = plan_router.estimate_cost_minor(tokens=tokens, price=price, unit=unit)
        group = self.store.add_plan_spend(group_key, tokens=tokens, cost_minor=cost_minor,
                                          now=self._now())
        return {"group": group["name"], "group_id": group["id"],
                "cost_minor": cost_minor, "tokens": tokens,
                "price_known": cost_minor is not None,
                "spent_usd_minor_day": group["spent_usd_minor_day"],
                "spent_usd_minor_month": group["spent_usd_minor_month"],
                "unknown_usage_count": group["unknown_usage_count"]}

    def begin_task(self) -> None:
        with self._inflight_lock:
            self._inflight += 1

    def end_task(self) -> None:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)

    def observe(self, *, model: str, prompt_tokens: int, completion_tokens: int,
                total_tokens: int, request_id: str) -> None:
        try:
            self.limiter.record_usage(total_tokens, profile=self.store.profile(),
                                      policy=self.store.policy())
        except Exception as exc:                            # pragma: no cover - 防御
            event("daemon.usage_record_failed", level="warning",
                  error=type(exc).__name__)
        event("task.completed", model=model, prompt_tokens=prompt_tokens,
              completion_tokens=completion_tokens, total_tokens=total_tokens,
              request_id=request_id)
        self._next_receipt_flush_at = self._now()

    def models(self) -> dict[str, Any]:
        policy = self.store.policy()
        price_version = policy.policy_version if policy is not None else "local"
        return {"object": "list", "data": self.upstreams.catalog(price_version=price_version)}

    def _policy_summary(self) -> dict[str, Any]:
        return policy_mod.summary(self.store.policy(), now=self._now())

    def health(self) -> dict[str, Any]:
        snapshot = self.machine.snapshot
        policy_summary = self._policy_summary()
        upstreams = self.upstreams.list()
        receipts_age = self.buffer.age_of_oldest_s(now=self._now())
        status = "ok"
        if snapshot.state != "online":
            status = "paused" if snapshot.state == "paused" else "degraded"
        elif not policy_summary.get("usable"):
            status = "degraded"
        payload = {
            "status": status,
            "daemon": {"state": snapshot.state, "reason": snapshot.reason,
                       "since": snapshot.since,
                       "next_step": snapshot.next_step},
            "upstream": {"count": len(upstreams),
                         "reachable": any(item.state in ("declared", "probing", "verified")
                                          for item in upstreams),
                         "models": len(self.upstreams.catalog()),
                         # C7 公告门控可见性（只加）：已验证 / 可公告 / 过期
                         "announced": len(self.upstreams.announceable_models(
                             self._now())),
                         "verified_models": sum(
                             1 for item in upstreams for raw in item.models
                             if ModelEntry.from_dict(raw).verified()),
                         "stale_models": sum(
                             1 for item in upstreams for raw in item.models
                             if ModelEntry.from_dict(raw).verified()
                             and not ModelEntry.from_dict(raw).fresh(self._now()))},
            "policy": {"present": policy_summary.get("present"),
                       "usable": policy_summary.get("usable"),
                       "policy_version": policy_summary.get("policy_version"),
                       "expires_at": policy_summary.get("expires_at"),
                       "verify_state": policy_summary.get("verify_state")},
            "receipts": {"count": self.buffer.count(),
                         "stale": self.buffer.stale_count(),
                         "oldest_age_s": receipts_age,
                         "backlog": receipts_age >= facts.RECEIPT_BUFFER_MAX_AGE_S},
            "inflight": self._inflight,
            "local_api": {"host": facts.MIRROR_HOST, "port": self.server.port,
                          "loopback_only": True},
            "version": facts.CLIENT_VERSION,
            "protocol_version": facts.PROTOCOL_VERSION,
            "server_time": iso(self._now()),
        }
        return payload

    def status(self) -> dict[str, Any]:
        snapshot = self.machine.snapshot
        install = self.store.install()
        profile = self.store.profile()
        return {
            "state": snapshot.state,
            "control": self.control_status(),
            "reason": snapshot.reason,
            "reason_label": reason_label(snapshot.reason),
            # E2E-06：只说「已暂停」不够，要说**为什么**（让路/资源/时段…各有不同恢复路径）。
            "pause_detail": dict(self._last_yield) if snapshot.state == "paused" else {},
            "next_step": snapshot.next_step,
            "since": snapshot.since,
            "accepting": snapshot.is_accepting(),
            "version": facts.CLIENT_VERSION,
            "protocol_version": facts.PROTOCOL_VERSION,
            "install": install.to_dict(),
            "node": {"node_id": self.credentials.node_id(),
                     "bound": bool(self.credentials.client_key())},
            "local_api": {"host": facts.MIRROR_HOST, "port": self.server.port,
                          "running": self._started, "loopback_only": True,
                          "token_fingerprint": self.server.token_fingerprint()},
            "profile": profile.to_dict(),
            "policy": self._policy_summary(),
            "limits": self.limiter.remaining(profile, self.store.policy(),
                                             now=self._now()),
            "upstreams": [{"id": item.id, "access_type": item.access_type,
                           "state": item.state, "enabled": item.enabled,
                           "models": len(item.models),
                           # P2：kind / protocol 也露出来（只加字段），否则中转站与
                           # 企业网关在 status 里长得一模一样，排障要先猜
                           "upstream_kind": item.upstream_kind,
                           "protocol": item.protocol,
                           "credential_ref": item.credential_ref}
                          for item in self.upstreams.list()],
            "receipts": {"count": self.buffer.count(),
                         "stale": self.buffer.stale_count(),
                         "oldest_age_s": self.buffer.age_of_oldest_s(now=self._now()),
                         "backlog": (self._backlog_block
                                     or self.buffer.must_stop_accepting(now=self._now()))},
            "heartbeat": {"failures": self._consecutive_failures,
                          "interval_s": self._heartbeat_interval_s,
                          "next_at": iso(self._next_heartbeat_at)},
            # C7：探针调度 + 特性开关（三字段齐全，见 facts.FEATURE_FLAGS）
            "probe": {"periodic_enabled": self._periodic_probe_enabled(),
                      "interval_s": self._probe_interval_s,
                      # ROUND3 / D-R3-02：把「上限」一并给渲染层，status 才能证明
                      # 「显示的周期 ≤ 上限」，而不是让用户自己猜 half-fresh 是多少。
                      "cap_s": facts.PROBE_PERIODIC_CAP_S,
                      "next_at": iso(self._next_probe_at),
                      "failures": self._probe_failures,
                      "last_pass": dict(self._last_probe),
                      "fresh_window_s": facts.PROBE_FRESH_S,
                      "models": [entry for item in self.upstreams.list()
                                 for entry in self.upstreams.probe_details(
                                     item.id, now=self._now())]},
            # C10c 出站长轮询通道（默认关闭；关闭时 running 恒为 False）
            "channel": self._channel_status(),
            # M18b 模型/tokenizer 同步（默认关闭；关闭时 running 恒为 False）
            "model_sync": self._model_sync_status(),
            # C16 消息中心（默认关闭；只暴露未读/同步/队列计数）
            "notifications": self._notifications_status(),
            # CR-20261106-OFFICIAL-SUPPLY-01 报价发布（默认关闭；只暴露计数，不含价格）
            "quotes": self._quotes_status(),
            "flags": {name: {"enabled": feature_flag(self.config, name), **spec}
                      for name, spec in facts.FEATURE_FLAGS.items()},
            "update": self.update.status(),
            # M3：机器画像（本地展示始终有；是否上报由 client.machine_profile.report 决定）
            "machine": self.machine_eval(),
            "machine_reported": bool(feature_flag(self.config, facts.MACHINE_PROFILE_FLAG)),
            "env": {"authorized_at": (self.store.env_state() or {}).get("authorized_at")},
            "yield": self._last_yield,
            "inflight": self._inflight,
            "credentials": self.credentials.describe(),
            "timestamp": iso(self._now()),
        }

    def control_status(self) -> dict[str, Any]:
        """Control transport availability is independent from daemon health."""
        reason = getattr(self, "_control_unavailable_reason", None)
        available = (reason is None and self._control is not None
                     and self._control.is_alive())
        return {"available": available,
                "reason": None if available else (reason or "control_not_running")}

    # ── 控制命令
    def _clear_yield_pause(self) -> bool:
        """手动 resume 时清掉「让路暂停」，返回是否真的动了让路设置（E2E-06）。

        为什么不能只把状态机推回 online：让路判据是**实时** GPU/CPU/焦点采样，
        下一个 tick 采样到「你在用机器」就立刻又 pause —— 用户看到的就是
        「djb resume 无效」（E2E-06 实测：resume 重启 daemon 后仍 paused(yield_paused)）。

        自动让路本身是有用的默认（别抢用户的机器），所以只在「当前确实因为让路而暂停」
        时才动它，并把恢复命令写进 status 的 resume_note；其它暂停原因一概不碰。
        """
        if self.machine.snapshot.reason != "yield_paused":
            return False
        self._last_yield = {}
        profile = self.store.profile()
        if not profile.yield_enabled:
            return False
        profile.yield_enabled = False
        self.store.save_profile(profile)
        event("daemon.yield_disabled_by_resume")
        return True

    def control(self, command: dict[str, Any]) -> dict[str, Any]:
        if getattr(self, "_control_unavailable_reason", None) is not None:
            from .ipc_windows import IpcUnavailable
            raise IpcUnavailable("Windows 控制通道不受支持；daemon独立运行，未执行控制命令")
        action = str(command.get("cmd") or "").strip()
        if action in ("ping", ""):
            return {"ok": True, "pid": os.getpid()}
        if action == "status":
            return {"ok": True, "status": self.status()}
        if action == "health":
            return {"ok": True, "health": self.health()}
        if action == "token":
            return {"ok": True, "local_token": self.token,
                    "fingerprint": self.server.token_fingerprint()}
        if action == "pause":
            self._user_paused = True
            self.store.set_kv("user_paused", True)
            try:
                self.machine.pause("user_paused")
            except DjbError:
                pass
            self._persist()
            event("daemon.user_paused")
            return {"ok": True, "status": self.status()}
        if action == "resume":
            self._user_paused = False
            self.store.set_kv("user_paused", False)
            self._backlog_block = False
            self.store.set_kv("receipt_backlog_block", False)
            if self._consecutive_failures >= facts.HEARTBEAT_FAIL_CONSERVATIVE_AFTER:
                self._consecutive_failures = 0
            self._forbidden_failures = 0
            self._next_heartbeat_at = self._now()
            # E2E-06：让路暂停必须能被手动 resume 清掉（见 _clear_yield_pause）
            yield_cleared = self._clear_yield_pause()
            self._evaluate(self._now())
            self._persist()
            event("daemon.user_resumed", yield_cleared=yield_cleared)
            status = self.status()
            if yield_cleared:
                status["resume_note"] = (
                    "已恢复接单；同时关闭了「自动让路」（它刚把你暂停了）。"
                    "想重新打开自动让路：djb limits --yield")
            return {"ok": True, "status": status, "yield_cleared": yield_cleared}
        if action in ("reload", "reload_config"):
            result = self.reload_config()
            if result.get("ok") is not True:
                # 失败也回 ok=False + 原因；调用方（CLI / GUI）必须显示出来
                return result
            result = dict(result)
            result["status"] = self.status()
            return result
        if action == "stop":
            threading.Thread(target=self.stop, name="djb-stop", daemon=True).start()
            return {"ok": True, "message": "daemon 正在退出"}
        if action == "limits":
            profile = self.store.profile()
            return {"ok": True, "profile": profile.to_dict(),
                    "remaining": self.limiter.remaining(profile, self.store.policy(),
                                                        now=self._now())}
        if action == "receipt":
            from .errors import ReceiptError
            receipt = command.get("receipt")
            if not isinstance(receipt, dict):
                raise UsageError("receipt 必须是 JSON 对象")
            try:
                stored = self.buffer.enqueue(receipt, provider_key=None)
            except ReceiptError as exc:
                raise UsageError(exc.message) from exc
            return {"ok": True, "stored": stored,
                    "receipt_id": receipt.get("receipt_id"),
                    "buffered": self.buffer.count()}
        raise UsageError("未知控制命令：" + action)


# ─────────────────────────────────────────────────────────── 进程入口
def hardware_fingerprint() -> str:
    """硬件指纹**哈希**（spec：平台只收哈希，不回传原始指纹）。"""
    parts: list[str] = []
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="ascii") as handle:
                parts.append(handle.read().strip())
            break
        except OSError:
            continue
    import platform
    parts.append(platform.node())
    parts.append(platform.machine())
    parts.append(str(os.cpu_count()))
    return sha256_hex("|".join(parts))


def NEXT_STEP_FOR(reason: str) -> str:
    from .state import NEXT_STEP
    return NEXT_STEP.get(reason, "djb status 查看原因")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="djb-daemon", description="DJB 联脑 M15 daemon")
    parser.add_argument("--home")
    parser.add_argument("--port", type=int)
    parser.add_argument("--token")
    parser.add_argument("--max-cycles", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from .config import resolve_home
    paths = Paths(resolve_home(args.home))
    token = args.token or os.environ.get(facts.LOCAL_TOKEN_ENV) or ""
    daemon = Daemon(paths, local_token=token or None,
                    port=args.port if args.port is not None else None)
    if not token:
        # 不在日志里打印令牌；只提示如何取
        event("daemon.token_generated", level="warning")
    def _handle(signum: int, frame: Any) -> None:
        daemon.stop()
        raise SystemExit(0)
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _handle)
        except (ValueError, OSError):                        # pragma: no cover - 非主线程
            pass
    try:
        return daemon.run(max_cycles=args.max_cycles)
    except DjbError as exc:
        from .logging_setup import event as log_event
        log_event("daemon.failed", level="error", error=exc.message,
                  exit_code=exc.exit_code)
        print("djb: " + exc.message, flush=True)
        return exc.exit_code


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
