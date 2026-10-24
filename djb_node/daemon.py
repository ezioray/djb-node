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

from . import facts, policy as policy_mod, protocol
from .api import NodeApi, PolicyFetch
from .channel import ChannelClient, channel_enabled
from .config import ClientConfig, Paths, feature_flag, load_config
from .errors import (AuthError, DjbError, ForbiddenError, NetworkError, NotInstalledError,
                     PolicyError, PortInUseError, RateLimitError, UsageError)
from .httpclient import HttpClient
from .limits import Decision, Limiter, YieldDetector, YieldResult
from .logging_setup import event, setup_logging
from .model_sync import ModelSyncClient, model_sync_enabled
from .notifications import NotificationStore, notifications_enabled
from .outbox import ReceiptFactory, SealedReceiptBuffer
from .probe import periodic_interval_s
from .secure import CredentialStore
from .server import Admission, LocalApiServer, RouteTarget
from .state import StateMachine, Snapshot, reason_label
from .store import Store, UpstreamRecord
from .update import UpdateManager
from .upstream import ModelEntry, UpstreamManager
from .util import (iso, new_request_id, new_token, parse_iso, sha256_hex, utcnow)


# ─────────────────────────────────────────────────────────── 控制套接字
def control_socket_file(paths: Paths) -> str:
    """Unix socket 路径上限 ~108 字节；太长时退到 /tmp 的确定性命中路径。"""
    primary = paths.socket_file
    if len(primary) <= 100:
        return primary
    import hashlib
    digest = hashlib.sha256(paths.home.encode("utf-8")).hexdigest()[:12]
    return os.path.join("/tmp", "djb-" + str(os.getuid()) + "-" + digest + ".sock")


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
            response = {"ok": False, "error": {"code": 50001,
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
        self.socket_path = control_socket_file(paths)

    def call(self, command: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
        from .errors import NotRunningError
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
        if response.get("ok") is False:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise UsageError(str(error.get("message") or "控制命令失败"))
        return response


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
                                    now=now)
        self.server = LocalApiServer(self, token=self.token, port=self.port, http=self.http,
                                        settings=self.config.extra)
        self._stop_event = threading.Event()
        self._control: ControlServer | None = None
        self._started = False
        # C10c 出站长轮询通道（默认关闭：开关关闭时不创建线程，行为与无通道版本一致）
        self._channel: ChannelClient | None = None
        self._channel_thread: threading.Thread | None = None
        self._channel_stop: threading.Event | None = None
        # M18b 模型/tokenizer 同步（默认关闭：关闭时不拉取、不建线程）
        self._model_sync: ModelSyncClient | None = None
        self._model_sync_thread: threading.Thread | None = None
        self._model_sync_stop: threading.Event | None = None
        # C16 消息中心（默认关闭：关闭时不拉取、不建线程；本地库只读保留）
        self._notifications: NotificationStore | None = None
        self._notifications_thread: threading.Thread | None = None
        self._notifications_stop: threading.Event | None = None
        # 运行态
        self._user_paused = bool(self.store.get_kv("user_paused", False))
        self._consecutive_failures = 0
        self._next_heartbeat_at = self._now()
        self._heartbeat_interval_s = facts.HEARTBEAT_DEFAULT_S
        self._policy_updated = False
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
        if self._started:
            return self.status()
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

    def run(self, *, max_cycles: int | None = None) -> int:
        if not self._started:
            self.start()
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

    def stop(self) -> None:
        if not self._started and self._control is None:
            return
        event("daemon.stopping")
        self._stop_event.set()
        self._stop_channel()
        self._stop_model_sync()
        self._stop_notifications()
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
        if self._control is not None:
            self._control.join(timeout=2.0)
            self._control = None
        socket_path = control_socket_file(self.paths)
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        self._started = False
        event("daemon.stopped")

    # ── C10c 出站长轮询通道（默认关闭）
    def _start_channel(self) -> None:
        """开关 ``client.channel.enabled`` 打开时起通道线程；关闭时不创建任何线程。"""
        if not channel_enabled(self.config):
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
        """status / 诊断用（只有状态与计数，无正文、无令牌）。"""
        enabled = channel_enabled(self.config)
        snapshot = self._channel.snapshot() if self._channel is not None else {}
        running = bool(self._channel_thread is not None
                       and self._channel_thread.is_alive())
        return {"enabled": enabled, "running": running,
                "state": snapshot.get("state", "idle"),
                "port": self.server.port,
                "wait_s": facts.CHANNEL_POLL_WAIT_S,
                "jobs_done": int(snapshot.get("jobs_done", 0)),
                "jobs_failed": int(snapshot.get("jobs_failed", 0)),
                "last_job_id": snapshot.get("last_job_id"),
                "last_error": snapshot.get("last_error")}

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
                                           http=self.http, api=api, now=self._now)
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
        if self.api is None:
            self.api = self._api_factory(self.config.gateway_url,
                                         self.credentials.client_key())
        if not self.credentials.client_key():
            # 离线注册重试：只对网络/限流失败重试（未知状态不重建密钥，M15 §7）
            if moment >= self._next_heartbeat_at:
                if self._register():
                    if self.api is not None:
                        self.api.client_key = self.credentials.client_key()
                    self._next_heartbeat_at = moment
        # 周期复探在心跳之前：让本轮心跳带的是最新容量（C7）
        if self._periodic_probe_enabled() and moment >= self._next_probe_at:
            self._probe_upstreams(moment, reason="periodic")
        if moment >= self._next_heartbeat_at:
            self._heartbeat(moment)
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

    # ── 心跳
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
            "capacity": self.upstreams.announce_capacity(profile, now=now),
            "load": {"inflight": self._inflight, "queue_seconds": 0.0},
        }
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
        reason = self._forbidden_reason(exc)
        self.machine.pause(reason, next_step=NEXT_STEP_FOR(reason),
                           detail={"code": exc.code})
        self._persist()
        event("daemon.heartbeat_forbidden", level="warning", reason=reason,
              code=exc.code)

    def _forbidden_reason(self, exc: ForbiddenError) -> str:
        text = (exc.message or "").lower()
        if "retired" in text or "清退" in text:
            return "retired"
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
        """开关 `client.probe.periodic.enabled`（默认关闭；见 facts.FEATURE_FLAGS）。"""
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
            if any(item.get("ok") for item in checks):
                verified += 1
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

    def probe_now(self, *, reason: str = "manual") -> dict[str, Any]:
        """手动触发一轮探针（控制通道 / 测试用；不受周期开关影响）。"""
        return self._probe_upstreams(self._now(), reason=reason)

    # ── 策略
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
                ttl = current.settings.get("policy_ttl_s")
                if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
                    ttl = facts.POLICY_DEFAULT_TTL_S
                renewed = policy_mod.parse_policy({
                    "policy_version": current.policy_version,
                    "protocol_version": current.protocol_version,
                    "issued_at": current.issued_at,
                    "digest": current.digest,
                    "quotas": current.quotas,
                    "directives": current.directives,
                    "settings": {**current.settings, "policy_ttl_s": ttl},
                    "expires_at": iso(now + timedelta(seconds=int(ttl))),
                }, now=now)
                self.store.save_policy(renewed)
                event("daemon.policy_renewed", policy_version=current.policy_version,
                      expires_at=renewed.expires_at)
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
        if snapshot.reason in ("account_frozen", "retired"):
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
                           credential=secret)

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
            "reason": snapshot.reason,
            "reason_label": reason_label(snapshot.reason),
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
            "flags": {name: {"enabled": feature_flag(self.config, name), **spec}
                      for name, spec in facts.FEATURE_FLAGS.items()},
            "update": self.update.status(),
            "env": {"authorized_at": (self.store.env_state() or {}).get("authorized_at")},
            "yield": self._last_yield,
            "inflight": self._inflight,
            "credentials": self.credentials.describe(),
            "timestamp": iso(self._now()),
        }

    # ── 控制命令
    def control(self, command: dict[str, Any]) -> dict[str, Any]:
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
            self._next_heartbeat_at = self._now()
            self._evaluate(self._now())
            self._persist()
            event("daemon.user_resumed")
            return {"ok": True, "status": self.status()}
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
