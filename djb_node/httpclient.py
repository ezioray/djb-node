# SPDX-License-Identifier: Apache-2.0
"""出站 HTTP（标准库 urllib）+ SSE 流式转发。

只做三件事：发请求、把网络异常归一成 NetworkError、支持逐行读 SSE。
业务状态码（4xx/5xx）**不抛异常**，由调用方按 spec 错误信封处理（tolerant reader）。
超时、大小、重定向都收敛在客户端侧（04-api-contracts §1.5）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from . import facts
from .errors import NetworkError
from .platform.no_window import subprocess_window_kwargs
from .util import first_line

#: 兜底传输开关（2026-11-19 客户现场）：urllib 的 TLS/传输失败后，用**系统 curl** 再试一次。
#: ``off`` 即回旧行为；未设/其它值 = 只在失败后兜底（成功路径一个字节都不变）。
FALLBACK_ENV = "DJB_HTTP_FALLBACK"
#: 指定 curl 可执行文件（运维/单测用；不设则用系统自带的那个）。
CURL_BIN_ENV = "DJB_CURL_BIN"
_STATUS_MARK = "__DJB_STATUS__"
_DEFAULT_CONTEXT: ssl.SSLContext | None = None
_CONTEXT_TRIED = False


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""
    elapsed_ms: int = 0

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def error_code(self) -> int | None:
        payload = self.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                if isinstance(code, int) and not isinstance(code, bool):
                    return code
        return None

    def error_message(self) -> str:
        payload = self.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return first_line(str(error.get("message") or ""))
        return first_line(self.text())


class InputStream:
    """SSE / 流式响应：逐行读，close 幂等。"""

    def __init__(self, response: Any, url: str) -> None:
        self._response = response
        self.url = url
        self.status = int(getattr(response, "status", 0) or 0)
        raw_headers = getattr(response, "headers", None)
        self.headers: dict[str, str] = {}
        if raw_headers is not None:
            try:
                self.headers = {key.lower(): value for key, value in raw_headers.items()}
            except Exception:                                  # pragma: no cover
                self.headers = {}

    def lines(self) -> Iterator[bytes]:
        while True:
            line = self._response.readline()
            if not line:
                break
            yield line

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:                                      # pragma: no cover
            pass

    def __enter__(self) -> "InputStream":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


#: 重定向时**只允许同源**携带的凭据头（跨源必须剥掉，2026-11-15 审计 P1）。
CREDENTIAL_HEADERS: frozenset[str] = frozenset({
    "authorization", "x-client-key", "x-api-key", "cookie", "set-cookie",
    "proxy-authorization",
})


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """重定向护栏：限跳数 + 跨源剥凭据 + 拒绝 https→http 降级。

    为什么需要：``urllib`` 默认 ``HTTPRedirectHandler`` 会把**所有请求头**带到新 URL
    （只去掉 Content-Length / Content-Type）。而客户端的 ``X-Client-Key``（节点密钥）与
    ``Authorization``（上游密钥）都在请求头里 —— 只要网关或上游回一次 302，
    密钥就会被送到重定向目标（可能是另一台主机，也可能是明文 http）。

    处置口径：
    * 同源（scheme/host/port 全同）重定向：原样保留头（兼容 CDN 尾斜杠等正常场景）；
    * 跨源：保留请求方法与 body，剥掉 :data:`CREDENTIAL_HEADERS`；
    * ``https`` → 非 ``https``：返回 ``None``（urllib 不跳，调用方拿到 3xx 自行判失败）；
    * 最多 3 跳（默认 10 跳对凭据暴露面太大）。
    """

    max_redirections = 3

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None
        origin = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if origin.scheme == "https" and target.scheme != "https":
            return None                                    # 拒绝降级；3xx 原样交回调用方
        same_origin = ((origin.scheme, origin.hostname, origin.port)
                       == (target.scheme, target.hostname, target.port))
        if not same_origin:
            for name in list(new_request.headers):
                if name.lower() in CREDENTIAL_HEADERS:
                    del new_request.headers[name]
        return new_request


class HttpClient:
    def __init__(self, *, timeout: float = 10.0,
                 user_agent: str = facts.USER_AGENT,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.ssl_context = ssl_context if ssl_context is not None else _default_ssl_context()
        self._opener: urllib.request.OpenerDirector | None = None

    def _open(self) -> urllib.request.OpenerDirector:
        """带重定向护栏的 opener（惰性构建，线程内复用；ssl_context 只影响 HTTPS）。"""
        if self._opener is None:
            handlers: list[Any] = [_RedirectGuard()]
            if self.ssl_context is not None:
                handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context))
            self._opener = urllib.request.build_opener(*handlers)
        return self._opener

    # ── 基础请求
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                json_body: Any = None, body: bytes | None = None,
                timeout: float | None = None) -> HttpResponse:
        if json_body is not None and body is not None:
            raise ValueError("json_body 与 body 只能二选一")
        data: bytes | None = body
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update({key: value for key, value in headers.items()
                                    if value is not None})
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, method=method.upper(),
                                         headers=request_headers)
        started = _monotonic()
        try:
            with self._open().open(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
                elapsed = int((_monotonic() - started) * 1000)
                final_url = getattr(response, "geturl", lambda: url)() or url
                return HttpResponse(status=int(response.status or 200),
                                    headers={key.lower(): value
                                             for key, value in response.headers.items()},
                                    body=payload, url=final_url, elapsed_ms=elapsed)
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read()
            except Exception:                                  # pragma: no cover
                payload = b""
            elapsed = int((_monotonic() - started) * 1000)
            return HttpResponse(status=int(exc.code or 500),
                                headers={key.lower(): value
                                         for key, value in (exc.headers or {}).items()},
                                body=payload, url=url, elapsed_ms=elapsed)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError,
                ConnectionError, OSError) as exc:
            fallback, note = _curl_request(method, url, request_headers, data,
                                           timeout or self.timeout)
            if fallback is not None:
                return fallback
            raise _network_error(url, exc, fallback=note) from exc

    def get_json(self, url: str, *, headers: dict[str, str] | None = None,
                 timeout: float | None = None) -> HttpResponse:
        return self.request("GET", url, headers=headers, timeout=timeout)

    def post_json(self, url: str, payload: Any, *,
                  headers: dict[str, str] | None = None,
                  timeout: float | None = None) -> HttpResponse:
        return self.request("POST", url, headers=headers, json_body=payload,
                            timeout=timeout)

    # ── SSE / 流式
    def open_stream(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                    json_body: Any = None, timeout: float | None = None) -> InputStream:
        request_headers = {"User-Agent": self.user_agent,
                           "Accept": "text/event-stream",
                           "Cache-Control": "no-cache"}
        if headers:
            request_headers.update({key: value for key, value in headers.items()
                                    if value is not None})
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method.upper(),
                                         headers=request_headers)
        try:
            response = self._open().open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            # 上游错误也要能读 body（SSE 之外的错误封装成 InputStream 由调用方处理）
            return InputStream(exc, url)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError,
                ConnectionError, OSError) as exc:
            raise _network_error(url, exc) from exc
        return InputStream(response, url)


def join_url(base: str, path: str) -> str:
    if not base:
        return path
    if not path:
        return base
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def host_of(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or ""


def _network_error(url: str, exc: BaseException, *, fallback: str = "") -> NetworkError:
    """网络异常归一：对外仍是「不可达 / 超时」，但**细节里带上原始异常**。

    T1（`changes/CR-20261119-CLIENT-TLS-DIAG.md`）：旧实现把 ``URLError`` /
    ``SSLCertVerificationError`` / 连接被重置全部压成同一句话，现场只能看到
    ``code 50903``，连「证书校验失败」和「端口不通」都分不开。这里只加
    ``details``（日志/``diagnose`` 可见），错误码与对外文案不变。
    """
    reason = "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "unreachable"
    code = facts.LOCAL_ERROR_CODES["upstream_timeout"] if reason == "timeout" \
        else facts.LOCAL_ERROR_CODES["upstream_error"]
    host = host_of(url)
    # urllib 把真正的异常塞在 URLError.reason 里：不解包就永远只看得到 "URLError"
    inner = getattr(exc, "reason", None)
    if not isinstance(inner, BaseException):
        inner = exc
    verify_failed = isinstance(inner, ssl.SSLCertVerificationError) \
        or isinstance(exc, ssl.SSLCertVerificationError)
    details: dict[str, Any] = {
        "host": host,
        "exc_type": type(inner).__name__,
        "exc_msg": first_line(str(inner))[:200],
        "fallback": fallback or "off",
    }
    if verify_failed:
        details["verify_failed"] = True
        details["verify_msg"] = first_line(
            str(getattr(inner, "verify_message", "") or inner))[:200]
    message = ("无法连接服务端" + ("（" + host + "）" if host else "")
               + "：网络或服务不可用，稍后重试")
    if verify_failed:
        message += "（本机证书校验未通过）"
    return NetworkError(message, code=code, reason=reason, details=details)


def _default_ssl_context() -> ssl.SSLContext | None:
    """出站 HTTPS 默认 context：系统默认 + Windows 存储枚举 + 内置根（见 trust_store）。"""
    global _DEFAULT_CONTEXT, _CONTEXT_TRIED
    if not _CONTEXT_TRIED:
        _CONTEXT_TRIED = True
        try:
            from .trust_store import build_ssl_context
            _DEFAULT_CONTEXT = build_ssl_context()
        except Exception:                       # 信任面出问题不许让客户端起不来
            _DEFAULT_CONTEXT = None
    return _DEFAULT_CONTEXT


def _fallback_enabled() -> bool:
    raw = (os.environ.get(FALLBACK_ENV) or "").strip().lower()
    return raw not in ("0", "off", "false", "no", "none")


def curl_binary() -> str | None:
    """系统 curl 的绝对路径（Windows 只用 System32，避免 PATH 劫持）。"""
    override = (os.environ.get(CURL_BIN_ENV) or "").strip()
    if override:
        return override if os.path.isfile(override) else None
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        candidate = os.path.join(root, "System32", "curl.exe")
        return candidate if os.path.isfile(candidate) else None
    return shutil.which("curl")


def _config_line(key: str, value: str) -> str:
    """curl ``-K`` 配置行：双引号 + 反斜杠转义（**凭据只走 stdin，不进 argv**）。"""
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\r", "\\r").replace("\n", "\\n"))
    return key + ' = "' + escaped + '"'


def _curl_request(method: str, url: str, headers: dict[str, str],
                  data: bytes | None, timeout: float) -> tuple[HttpResponse | None, str]:
    """用系统 curl 发一次请求；返回 ``(响应 | None, 说明)``。

    为什么要它：客户那台 Windows 上 ``curl``（Schannel + 系统信任库）能连上网关，
    而 CPython/OpenSSL 在握手阶段必败，客户又无法再提供现场信息 —— 于是把「**系统 TLS
    栈**」当成兜底通道。仍然做证书校验（用的是 OS 信任库），不提供跳过校验的选项。

    凭据面：header 与 body 全部写进 ``-K -`` 的 **stdin 配置**，不落 argv、不落临时文件。
    """
    if not _fallback_enabled():
        return None, "off"
    binary = curl_binary()
    if not binary:
        return None, "no-curl"
    text: str | None = None
    if data is not None:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, "binary-body"                # 非 UTF-8 体不走配置通道
    seconds = int(max(1, round(float(timeout))))
    lines = ["silent", "show-error", "location", "max-redirs = 3",
             "max-time = " + str(seconds),
             _config_line("url", url),
             _config_line("request", method.upper()),
             _config_line("write-out", "\n" + _STATUS_MARK + "%{http_code}")]
    for name, value in headers.items():
        lines.append(_config_line("header", str(name) + ": " + str(value)))
    if text is not None:
        lines.append(_config_line("data-binary", text))
    started = _monotonic()
    try:
        proc = subprocess.run([binary, "-K", "-"], input="\n".join(lines) + "\n",
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=float(timeout) + 5.0,
                              **subprocess_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None, "curl-error"
    if proc.returncode != 0:
        return None, "curl-exit-" + str(proc.returncode)
    out = proc.stdout or ""
    index = out.rfind(_STATUS_MARK)
    if index < 0:
        return None, "curl-no-status"
    status_text = out[index + len(_STATUS_MARK):].strip()[:3]
    payload = out[:index]
    if payload.endswith("\n"):
        payload = payload[:-1]                        # 去掉 write-out 自带的分隔换行
    try:
        status = int(status_text)
    except ValueError:
        return None, "curl-bad-status"
    return HttpResponse(status=status,
                        headers={"x-djb-fallback": "curl"},
                        body=payload.encode("utf-8"), url=url,
                        elapsed_ms=int((_monotonic() - started) * 1000)), "ok"


def _monotonic() -> float:
    import time
    return time.monotonic()
