# SPDX-License-Identifier: Apache-2.0
"""出站 HTTP（标准库 urllib）+ SSE 流式转发。

只做三件事：发请求、把网络异常归一成 NetworkError、支持逐行读 SSE。
业务状态码（4xx/5xx）**不抛异常**，由调用方按 spec 错误信封处理（tolerant reader）。
超时、大小、重定向都收敛在客户端侧（04-api-contracts §1.5）。
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from . import facts
from .errors import NetworkError
from .util import first_line


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
        self.ssl_context = ssl_context
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
            raise _network_error(url, exc) from exc

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


def _network_error(url: str, exc: BaseException) -> NetworkError:
    """网络异常归一：只给「不可达 / 超时」这类粗粒度原因，不含 URL 凭据。"""
    reason = "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "unreachable"
    code = facts.LOCAL_ERROR_CODES["upstream_timeout"] if reason == "timeout" \
        else facts.LOCAL_ERROR_CODES["upstream_error"]
    host = host_of(url)
    return NetworkError(
        "无法连接服务端" + ("（" + host + "）" if host else "") + "：网络或服务不可用，稍后重试",
        code=code, reason=reason, details={"host": host})


def _monotonic() -> float:
    import time
    return time.monotonic()
