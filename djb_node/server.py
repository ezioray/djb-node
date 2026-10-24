# SPDX-License-Identifier: Apache-2.0
"""本机回环镜像端点 127.0.0.1:1717/v1（M15 §4.1；不在 spec 开放契约内，CR-1）。

三个端点：

    GET  /v1/models             模型 + 价格 + 可用性（含 price_version）
    POST /v1/chat/completions   OpenAI 兼容转发；stream=true 走 SSE 透传
    GET  /v1/health             本机自检（daemon / 上游 / 策略新鲜度）——本机可免令牌

硬规则：

* INV-03：**只绑 127.0.0.1**；禁止 0.0.0.0 / ::；收到非回环来源也拒绝（纵深防御）。
* local_token：进程启动时一次性生成，只在内存里；进程退出即失效；不落盘、不外传。
  Authorization: Bearer <token>，常量时间比较；缺失/错误 -> 401 + 40101。
* 错误信封与 spec Error 一致（type / code / message / request_id）。
* **不落正文**：请求/响应 body 不写日志、不写数据库；只记元数据（模型、状态、tokens、耗时）。
* C8：开关 ``client.token.compare.enabled``（默认关闭）打开时，额外做「本地 token 计数 vs
  上游 usage」差异检测，>1% 记 ``token_mismatch``（只有数字与来源，不含正文）。
  **不改写上游 usage、不改收据金额**；开关关闭时行为和以前完全一致。
"""
from __future__ import annotations

import hmac
import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Protocol

from . import facts, protocol, tokens
from .errors import PortInUseError, UsageError
from .httpclient import HttpClient, InputStream, join_url
from .logging_setup import event
from .util import iso, new_request_id, redact_text, utcnow

MAX_BODY_BYTES = 8 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 120.0


# ─────────────────────────────────────────────────────────── 运行时接口（daemon 实现）
@dataclass
class RouteTarget:
    base_url: str
    model: str
    access: str
    upstream_id: str
    credential: str | None = None


@dataclass
class Admission:
    ok: bool
    http_status: int = 200
    code: int = 0
    reason: str | None = None
    message: str = ""
    retry_after: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class Runtime(Protocol):
    def health(self) -> dict[str, Any]: ...

    def models(self) -> dict[str, Any]: ...

    def admit(self, model: str) -> Admission: ...

    def route(self, model: str) -> RouteTarget | None: ...

    def observe(self, *, model: str, prompt_tokens: int, completion_tokens: int,
                total_tokens: int, request_id: str) -> None: ...


# ─────────────────────────────────────────────────────────── 绑定校验
def validate_bind_host(host: str) -> str:
    """只允许回环地址；0.0.0.0 / :: / 空 一律拒绝（INV-03）。"""
    value = (host or "").strip()
    if value in facts.FORBIDDEN_BIND_HOSTS:
        raise UsageError("本机镜像端点只允许绑定回环地址 127.0.0.1（禁止 "
                         + str(host or "空") + "）")
    if value == "localhost":
        value = "127.0.0.1"
    if value not in ("127.0.0.1", "::1"):
        raise UsageError("只允许回环地址 127.0.0.1（收到 " + value + "）")
    return value


def is_loopback_peer(address: Any) -> bool:
    try:
        host = str(address[0])
    except (TypeError, IndexError):
        return False
    return host in ("127.0.0.1", "::1") or host.startswith("127.")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:                            # 跳过 socket.getfqdn 反查
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class LocalApiServer:
    def __init__(self, runtime: Runtime, *, token: str, host: str = facts.MIRROR_HOST,
                 port: int = facts.MIRROR_PORT, http: HttpClient | None = None,
                 settings: dict[str, Any] | None = None) -> None:
        if not token or len(token) < facts.LOCAL_TOKEN_MIN_BYTES:
            raise UsageError("local_token 长度不足（至少 " + str(facts.LOCAL_TOKEN_MIN_BYTES)
                             + " 字节）")
        self.runtime = runtime
        self.token = token
        self.host = validate_bind_host(host)
        self.port = int(port)
        self.http = http or HttpClient(timeout=UPSTREAM_TIMEOUT_S)
        # C8：开关宿主（client.* 配置树 / extra）；缺省 None = 按 facts 默认（关闭）
        self.settings = dict(settings or {})
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # ── 生命周期
    def bind(self) -> None:
        handler = _make_handler(self)
        try:
            self._server = _Server((self.host, self.port), handler)
        except OSError as exc:
            raise PortInUseError(
                "端口 " + str(self.port) + " 绑定失败（" + str(exc) + "）："
                "改端口 djb init --port <新端口> 或 djb start --port <新端口>；"
                "先确认没有其它进程占用 127.0.0.1:" + str(self.port),
                code=40919) from exc
        bound_host, bound_port = self._server.server_address[:2]
        if str(bound_host) != self.host:
            self._server.server_close()
            raise UsageError("实际绑定地址不是回环（" + str(bound_host) + "）：拒绝启动")
        self.port = int(bound_port)

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    @property
    def base_url(self) -> str:
        return "http://" + self.host + ":" + str(self.port)

    def start(self) -> None:
        """后台线程跑 serve_forever（daemon 用）。"""
        if self._server is None:
            self.bind()
        assert self._server is not None
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        name="djb-local-api", daemon=True)
        self._thread.start()
        event("local_api.started", host=self.host, port=self.port,
              loopback_only=True)

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ── 鉴权
    def authorized(self, header_value: str | None) -> bool:
        if not header_value:
            return False
        prefix = "bearer "
        if not header_value.lower().startswith(prefix):
            return False
        presented = header_value[len(prefix):].strip()
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self.token.encode("utf-8"))

    def token_fingerprint(self) -> str:
        from .util import sha256_hex
        return sha256_hex("djb-local-token:" + self.token)[:12]

    # ── C8：本地 token 计数 vs 上游 usage（开关默认关闭）
    def token_compare_enabled(self) -> bool:
        """开关 ``client.token.compare.enabled``（默认关闭 = 只记录上游 usage）。"""
        return tokens.compare_enabled(self.settings)


# ─────────────────────────────────────────────────────────── HTTP handler
def _make_handler(server: LocalApiServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "djb-node"
        sys_version = ""

        # ── 工具
        def log_message(self, fmt: str, *args: Any) -> None:   # noqa: A003
            # 不落默认访问日志（会带路径；正文从不记录，路径也不进文件）
            return

        def _request_id(self) -> str:
            incoming = (self.headers.get("X-Request-Id") or "").strip()
            if incoming and len(incoming) <= 64 and "\n" not in incoming:
                return incoming
            return new_request_id()

        def _peer_ok(self) -> bool:
            return is_loopback_peer(self.client_address)

        def _send_json(self, status: int, payload: Any, request_id: str,
                       extra_headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", request_id)
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _error(self, status: int, code: int, message: str, request_id: str,
                   *, reason: str | None = None, retry_after: int | None = None,
                   extra_headers: dict[str, str] | None = None) -> None:
            from .errors import envelope_for_status
            payload = envelope_for_status(status, code, message, request_id,
                                          reason=reason, retry_after=retry_after)
            headers = dict(extra_headers or {})
            if retry_after is not None:
                headers["Retry-After"] = str(int(retry_after))
            self._send_json(status, payload, request_id, headers)

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                raise UsageError("Content-Length 不合法")
            if length < 0 or length > MAX_BODY_BYTES:
                raise UsageError("请求体过大（上限 " + str(MAX_BODY_BYTES) + " 字节）")
            return self.rfile.read(length) if length else b""

        # ── 路由
        def do_GET(self) -> None:                              # noqa: N802
            request_id = self._request_id()
            if not self._peer_ok():
                self._error(403, 40101, "只接受本机回环请求", request_id)
                return
            path = self.path.split("?", 1)[0]
            if path == protocol.PATHS["local_health"]:
                self._handle_health(request_id)
                return
            if not self._require_token(request_id):
                return
            if path == protocol.PATHS["local_models"]:
                self._handle_models(request_id)
                return
            self._error(404, 40401, "未知路径：" + path, request_id)

        def do_POST(self) -> None:                             # noqa: N802
            request_id = self._request_id()
            if not self._peer_ok():
                self._error(403, 40101, "只接受本机回环请求", request_id)
                return
            path = self.path.split("?", 1)[0]
            if path != protocol.PATHS["local_chat"]:
                self._error(404, 40401, "未知路径：" + path, request_id)
                return
            if not self._require_token(request_id):
                return
            self._handle_chat(request_id)

        def do_HEAD(self) -> None:                             # noqa: N802
            self._error(405, 40001, "不支持 HEAD", self._request_id())

        # ── 鉴权 / 端点
        def _require_token(self, request_id: str) -> bool:
            if server.authorized(self.headers.get("Authorization")):
                return True
            self._error(401, facts.LOCAL_ERROR_CODES["unauthorized"],
                        "本机令牌无效：从 djb status --show-token 获取（进程退出即失效）",
                        request_id)
            return False

        def _handle_health(self, request_id: str) -> None:
            try:
                payload = server.runtime.health()
            except Exception as exc:                           # pragma: no cover - 防御
                self._error(500, 50001, "健康检查失败", request_id)
                event("local_api.health_failed", level="error", error=type(exc).__name__)
                return
            payload = dict(payload)
            payload.setdefault("server_time", iso(utcnow()))
            payload["token_fingerprint"] = server.token_fingerprint()
            self._send_json(200, payload, request_id)

        def _handle_models(self, request_id: str) -> None:
            try:
                payload = server.runtime.models()
            except Exception as exc:                           # pragma: no cover - 防御
                self._error(500, 50001, "模型目录不可用", request_id)
                event("local_api.models_failed", level="error", error=type(exc).__name__)
                return
            self._send_json(200, payload, request_id)

        def _handle_chat(self, request_id: str) -> None:
            try:
                raw = self._read_body()
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (UsageError, ValueError, UnicodeDecodeError):
                self._error(400, facts.LOCAL_ERROR_CODES["invalid_request"],
                            "请求体必须是合法 JSON", request_id)
                return
            if not isinstance(payload, dict):
                self._error(400, facts.LOCAL_ERROR_CODES["invalid_request"],
                            "请求体必须是 JSON 对象", request_id)
                return
            model = payload.get("model")
            messages = payload.get("messages")
            if not isinstance(model, str) or not model.strip():
                self._error(400, facts.LOCAL_ERROR_CODES["invalid_request"],
                            "model 必填", request_id)
                return
            if not isinstance(messages, list) or not messages:
                self._error(400, facts.LOCAL_ERROR_CODES["invalid_request"],
                            "messages 必须是非空数组", request_id)
                return
            stream = bool(payload.get("stream"))
            admission = server.runtime.admit(model)
            if not admission.ok:
                extra = {"Retry-After": str(admission.retry_after)} \
                    if admission.retry_after else None
                self._error(admission.http_status, admission.code,
                            admission.message or "当前不接单", request_id,
                            reason=admission.reason, retry_after=admission.retry_after,
                            extra_headers=extra)
                return
            target = server.runtime.route(model)
            if target is None:
                self._error(503, facts.LOCAL_ERROR_CODES["no_upstream"],
                            "没有可用的上游能服务该模型", request_id)
                return
            upstream_headers: dict[str, str] = {}
            if target.credential:
                upstream_headers["Authorization"] = "Bearer " + target.credential
            url = join_url(target.base_url.rstrip("/"),
                           "/chat/completions" if target.base_url.rstrip("/").endswith("/v1")
                           else "/v1/chat/completions")
            begin = getattr(server.runtime, "begin_task", None)
            end = getattr(server.runtime, "end_task", None)
            if callable(begin):
                begin()
            try:
                if stream:
                    self._forward_stream(url, payload, upstream_headers, model,
                                         request_id, target)
                else:
                    self._forward_json(url, payload, upstream_headers, model, request_id,
                                       target)
            finally:
                if callable(end):
                    end()

        # ── 转发：非流式
        def _forward_json(self, url: str, payload: dict[str, Any],
                          headers: dict[str, str], model: str, request_id: str,
                          target: RouteTarget) -> None:
            from .errors import NetworkError
            try:
                response = server.http.request("POST", url, headers=headers,
                                               json_body=payload,
                                               timeout=UPSTREAM_TIMEOUT_S)
            except NetworkError as exc:
                status = 504 if exc.reason == "timeout" else 502
                code = (facts.LOCAL_ERROR_CODES["upstream_timeout"]
                        if exc.reason == "timeout"
                        else facts.LOCAL_ERROR_CODES["upstream_error"])
                self._error(status, code, "上游" + ("超时" if exc.reason == "timeout"
                                                   else "不可达"), request_id)
                event("local_api.upstream_failed", level="warning", model=model,
                      error_class=str(exc.reason), request_id=request_id)
                return
            self._echo_upstream_status(response.status, response, model, request_id)
            body = response.body or b"{}"
            usage = _usage_from_json(response.json())
            if 200 <= response.status < 300 and usage is not None:
                server.runtime.observe(model=model, prompt_tokens=usage[0],
                                       completion_tokens=usage[1], total_tokens=usage[2],
                                       request_id=request_id)
                self._compare_tokens(model, payload, usage, stream=False,
                                     request_id=request_id)
            self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get(
                "content-type", "application/json; charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", request_id)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            event("local_api.chat", model=model, status=response.status,
                  access=target.access, duration_ms=response.elapsed_ms,
                  tokens=(usage[2] if usage else None), request_id=request_id)

        def _echo_upstream_status(self, status: int, response: Any, model: str,
                                  request_id: str) -> None:
            if 200 <= status < 300:
                return
            event("local_api.upstream_status", level="warning", model=model,
                  status=status, request_id=request_id)

        def _compare_tokens(self, model: str, payload: dict[str, Any],
                            usage: tuple[int, int, int], *, stream: bool,
                            request_id: str) -> None:
            """C8 差异检测：本地估算 vs 上游 prompt tokens，>1% 记一条事件。

            * 开关关闭（默认）→ 直接返回，行为与接入前一致；
            * **只读**：不改 `usage`、不改转发 body、不动 `observe()` 入参、不碰收据金额；
            * 事件只含 model / 本地 / 上游 / 差值 / 来源 / tokenizer_id，**不含正文**；
            * 计数异常不得影响转发（捕获后只记一条 warning）。
            """
            if not server.token_compare_enabled():
                return
            try:
                local = tokens.count_messages(model, payload.get("messages"),
                                              tools=payload.get("tools"))
                mismatch = tokens.compare_counts(local.tokens, usage[0])
                if mismatch is None:
                    return
                event("token_mismatch", level="warning", model=model,
                      source=("stream" if stream else "json"),
                      local_tokens=mismatch.local_tokens,
                      upstream_tokens=mismatch.upstream_tokens,
                      delta_tokens=mismatch.delta_tokens,
                      delta_pct=mismatch.delta_pct,
                      tokenizer_id=local.tokenizer_id, estimated=local.estimated,
                      request_id=request_id)
            except Exception as exc:                           # 计数失败不阻断转发
                event("local_api.token_compare_failed", level="warning", model=model,
                      error=type(exc).__name__, request_id=request_id)

        # ── 转发：SSE
        def _forward_stream(self, url: str, payload: dict[str, Any],
                            headers: dict[str, str], model: str, request_id: str,
                            target: RouteTarget) -> None:
            from .errors import NetworkError
            try:
                stream = server.http.open_stream("POST", url, headers=headers,
                                                 json_body=payload,
                                                 timeout=UPSTREAM_TIMEOUT_S)
            except NetworkError as exc:
                status = 504 if exc.reason == "timeout" else 502
                code = (facts.LOCAL_ERROR_CODES["upstream_timeout"]
                        if exc.reason == "timeout" else facts.LOCAL_ERROR_CODES["upstream_error"])
                self._error(status, code, "上游不可达", request_id)
                return
            with stream:
                if stream.status >= 400:
                    body = b""
                    try:
                        body = stream._response.read()
                    except Exception:                          # pragma: no cover
                        body = b""
                    self._error(502, facts.LOCAL_ERROR_CODES["upstream_error"],
                                "上游拒绝请求（HTTP " + str(stream.status) + "）", request_id)
                    event("local_api.stream_rejected", level="warning", model=model,
                          status=stream.status, request_id=request_id)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Request-Id", request_id)
                self.send_header("Connection", "close")
                self.end_headers()
                usage: tuple[int, int, int] | None = None
                try:
                    for line in stream.lines():
                        if not line:
                            break
                        self.wfile.write(line)
                        self.wfile.flush()
                        parsed = _usage_from_sse_line(line)
                        if parsed is not None:
                            usage = parsed
                except (BrokenPipeError, ConnectionResetError):
                    event("local_api.stream_client_gone", level="warning",
                          model=model, request_id=request_id)
                except OSError:
                    event("local_api.stream_error", level="warning", model=model,
                          request_id=request_id)
                finally:
                    self.close_connection = True
                if usage is not None:
                    server.runtime.observe(model=model, prompt_tokens=usage[0],
                                           completion_tokens=usage[1], total_tokens=usage[2],
                                           request_id=request_id)
                    self._compare_tokens(model, payload, usage, stream=True,
                                         request_id=request_id)
                event("local_api.chat_stream", model=model, status=200,
                      access=target.access, tokens=(usage[2] if usage else None),
                      request_id=request_id)

    return Handler


def _usage_from_json(payload: Any) -> tuple[int, int, int] | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if not all(isinstance(item, int) and not isinstance(item, bool)
               for item in (prompt, completion, total)):
        return None
    if prompt < 0 or completion < 0 or total < 0:
        return None
    return prompt, completion, total


def _usage_from_sse_line(line: bytes) -> tuple[int, int, int] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return _usage_from_json(json.loads(data))
    except ValueError:
        return None
