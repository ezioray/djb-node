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


class HttpClient:
    def __init__(self, *, timeout: float = 10.0,
                 user_agent: str = facts.USER_AGENT,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.ssl_context = ssl_context

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
            with urllib.request.urlopen(request, timeout=timeout or self.timeout,
                                        context=self.ssl_context) as response:
                payload = response.read()
                elapsed = int((_monotonic() - started) * 1000)
                return HttpResponse(status=int(response.status or 200),
                                    headers={key.lower(): value
                                             for key, value in response.headers.items()},
                                    body=payload, url=url, elapsed_ms=elapsed)
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
            response = urllib.request.urlopen(request, timeout=timeout or self.timeout,
                                              context=self.ssl_context)
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
