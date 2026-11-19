# SPDX-License-Identifier: Apache-2.0
"""兜底传输 + 网络错误可诊断（2026-11-19 客户现场，`CR-20261119-CLIENT-TLS-DIAG`）。

现场事实：某台 Windows 上 ``curl``（Schannel + 系统信任库）能连网关，客户端
（CPython/OpenSSL）每次握手都失败，而两套证书存储里都装着有效的 ISRG Root X1；
客户已无法继续提供信息，所以按兜底修。本文件钉死四条边界：

1. 传输失败后**用系统 curl 再试一次**，成功就照常返回响应（带 ``x-djb-fallback`` 标记）；
2. 兜底**不做任何降级校验**：配置里不出现 ``insecure``/``-k``，凭据只走 stdin，
   绝不进 argv（进程列表可见）；
3. ``DJB_HTTP_FALLBACK=off`` 逐字节回到旧行为；
4. 失败时 ``details`` 必须带原始异常（类型/消息/是否证书校验），否则日志里永远只有 50903。
"""
from __future__ import annotations

import stat
import ssl
import urllib.error

import pytest

from djb_node import httpclient, trust_store
from djb_node.errors import NetworkError


class _FailingOpener:
    """替身 opener：一被调用就抛，用来把 request() 逼进兜底分支。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def open(self, request, timeout=None):        # noqa: D401 - 替身
        raise self.exc


def _verify_error() -> urllib.error.URLError:
    inner = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "unable to get local issuer certificate (_ssl.c:1006)")
    inner.verify_message = "unable to get local issuer certificate"
    return urllib.error.URLError(inner)


def _fake_curl(tmp_path, monkeypatch, *, stdout: str = '{"ok": true}\n__DJB_STATUS__200'):
    """假 curl：把 stdin 配置与 argv 各存一份，方便断言「凭据没进 argv」。"""
    cfg = tmp_path / "curl.cfg"
    argv = tmp_path / "curl.argv"
    script = tmp_path / "curl"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s" "$*" > "' + str(argv) + '"\n'
        'cat > "' + str(cfg) + '"\n'
        "printf '%s' '" + stdout.replace("'", "'\\''") + "'\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv(httpclient.CURL_BIN_ENV, str(script))
    monkeypatch.delenv(httpclient.FALLBACK_ENV, raising=False)
    return cfg, argv


def _client() -> httpclient.HttpClient:
    return httpclient.HttpClient(timeout=2.0)


def test_request_falls_back_to_curl_after_tls_failure(tmp_path, monkeypatch):
    _fake_curl(tmp_path, monkeypatch)
    client = _client()
    client._opener = _FailingOpener(_verify_error())
    response = client.request("GET", "https://tkbank.ai/api/v1/protocol/capabilities")
    assert response.status == 200
    assert response.json() == {"ok": True}
    assert response.headers.get("x-djb-fallback") == "curl"


def test_credentials_go_through_stdin_not_argv(tmp_path, monkeypatch):
    cfg, argv = _fake_curl(tmp_path, monkeypatch)
    client = _client()
    client._opener = _FailingOpener(_verify_error())
    client.request("POST", "https://tkbank.ai/api/v1/nodes/register",
                   headers={"Authorization": "Bearer SECRET-TOKEN-123"},
                   json_body={"client_key": "SECRET-CLIENT-KEY"})
    argv_text = argv.read_text(encoding="utf-8")
    cfg_text = cfg.read_text(encoding="utf-8")
    assert "SECRET-TOKEN-123" not in argv_text        # argv 会出现在进程列表里
    assert "SECRET-CLIENT-KEY" not in argv_text
    assert "SECRET-TOKEN-123" in cfg_text             # 凭据只走 stdin 配置
    assert "SECRET-CLIENT-KEY" in cfg_text


def test_curl_config_never_disables_verification(tmp_path, monkeypatch):
    cfg, _argv = _fake_curl(tmp_path, monkeypatch)
    client = _client()
    client._opener = _FailingOpener(_verify_error())
    client.request("GET", "https://tkbank.ai/healthz")
    text = cfg.read_text(encoding="utf-8")
    assert "insecure" not in text and "--insecure" not in text
    assert "silent" in text and "show-error" in text


def test_fallback_off_returns_to_old_behaviour(tmp_path, monkeypatch):
    _fake_curl(tmp_path, monkeypatch)
    monkeypatch.setenv(httpclient.FALLBACK_ENV, "off")
    client = _client()
    client._opener = _FailingOpener(_verify_error())
    with pytest.raises(NetworkError) as caught:
        client.request("GET", "https://tkbank.ai/healthz")
    details = caught.value.details or {}
    assert details["fallback"] == "off"
    assert details["exc_type"] == "SSLCertVerificationError"
    assert details["verify_failed"] is True


def test_failure_details_explain_certificate_problem(monkeypatch):
    """没有 curl 时也要说清楚「是证书校验」而不是笼统的「网络不可用」。"""
    monkeypatch.delenv(httpclient.FALLBACK_ENV, raising=False)
    monkeypatch.setenv(httpclient.CURL_BIN_ENV, "/nonexistent/curl")
    client = _client()
    client._opener = _FailingOpener(_verify_error())
    with pytest.raises(NetworkError) as caught:
        client.request("GET", "https://tkbank.ai/healthz")
    assert caught.value.details["fallback"] == "no-curl"
    assert "证书校验" in caught.value.message
    assert "unable to get local issuer certificate" in caught.value.details["verify_msg"]


def test_timeout_keeps_timeout_reason(monkeypatch):
    monkeypatch.setenv(httpclient.FALLBACK_ENV, "off")
    client = _client()
    client._opener = _FailingOpener(TimeoutError("timed out"))
    with pytest.raises(NetworkError) as caught:
        client.request("GET", "https://tkbank.ai/healthz")
    assert caught.value.reason == "timeout"


def test_trust_store_embeds_isrg_roots():
    context = trust_store.build_ssl_context()
    subjects = [str(cert.get("subject")) for cert in context.get_ca_certs()]
    assert any("ISRG Root X1" in item for item in subjects)
    assert any("ISRG Root X2" in item for item in subjects)


def test_httpclient_uses_hardened_context_by_default():
    context = httpclient._default_ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED          # 只加信任，不关校验
    assert context.check_hostname is True


def test_explicit_context_is_not_replaced():
    custom = ssl.create_default_context()
    assert httpclient.HttpClient(ssl_context=custom).ssl_context is custom
