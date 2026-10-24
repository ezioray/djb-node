# SPDX-License-Identifier: Apache-2.0
"""C11 第三方中转站接入（M17 §4）客户端侧用例。

全部离线：HTTP 用注入的 FakeHttp，不连网、不调服务端。
重点覆盖：三种认证头 / URL 不带 key / TLS pin / 发现与归一 / 重复与冲突拒绝 /
未映射不公告不接单 / kv 持久化 / 开关默认关闭 / key 不进日志与异常文案。
"""
from __future__ import annotations

import json

import pytest

from djb_node import facts
from djb_node.config import ClientConfig
from djb_node.errors import FeatureDisabled, NetworkError
from djb_node.httpclient import HttpResponse
from djb_node.relay import (RelayError, RelayManager, RelayProfile, announceable_models,
                            apply_discovery, auth_headers, build_ssl_context,
                            canonical_slug, check_peer_pin, delete_profile,
                            discover_models, is_serviceable, list_aliases,
                            list_profiles, load_profile, map_alias,
                            normalize_tls_pin, parse_models_payload, pin_of_der,
                            relay_enabled, request_headers, resolve_model,
                            save_profile, set_model_enabled, unmap_alias,
                            validate_relay_base_url, validate_relay_profile)

SECRET = "sk-relay-0123456789abcdef0123456789"
REGISTRY = {"openai/gpt-4o", "openai/gpt-4o-mini", "anthropic/claude-3-7-sonnet",
            "deepseek/deepseek-chat"}


class FakeHttp:
    """注入用 HTTP 客户端：记录请求、返回固定响应，绝不联网。"""

    def __init__(self, status: int = 200, payload=None, *, raise_exc: BaseException | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.payload = payload
        self.raise_exc = raise_exc
        self.headers = dict(headers or {})
        self.calls: list[dict] = []

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                json_body=None, body=None, timeout=None) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        payload = b"" if self.payload is None else json.dumps(self.payload).encode("utf-8")
        return HttpResponse(status=self.status, headers=dict(self.headers), body=payload,
                            url=url)


def make_config(**flags: bool) -> ClientConfig:
    config = ClientConfig()
    config.extra["feature_flags"] = dict(flags)
    return config


def make_profile(**overrides) -> RelayProfile:
    values = {"upstream_id": "u-relay-1", "base_url": "https://relay.example.com/v1"}
    values.update(overrides)
    return RelayProfile(**values)


# ─────────────────────────────────────────────────────────── 认证头
@pytest.mark.parametrize("scheme,header,prefix", [
    ("bearer", "Authorization", "Bearer "),
    ("x-api-key", "X-Api-Key", ""),
    ("api-key", "Api-Key", ""),
])
def test_three_auth_schemes_build_expected_headers(scheme, header, prefix):
    profile = make_profile(auth_scheme=scheme)
    validate_relay_profile(profile, credential=SECRET)
    assert auth_headers(profile, SECRET) == {header: prefix + SECRET}
    assert profile.auth_header_name == header


def test_custom_scheme_requires_header_name_and_supports_prefix():
    profile = make_profile(auth_scheme="custom")
    with pytest.raises(RelayError):
        validate_relay_profile(profile, credential=SECRET)
    profile = make_profile(auth_scheme="custom", header_name="X-Relay-Key",
                           auth_prefix="Token ")
    validate_relay_profile(profile, credential=SECRET)
    assert auth_headers(profile, SECRET) == {"X-Relay-Key": "Token " + SECRET}


def test_non_custom_scheme_cannot_override_auth_header():
    with pytest.raises(RelayError):
        validate_relay_profile(make_profile(auth_scheme="x-api-key",
                                            header_name="Authorization"),
                               credential=SECRET)
    with pytest.raises(RelayError):
        validate_relay_profile(make_profile(header_name="X-Relay-Key"), credential=SECRET)
    profile = make_profile(header_name="authorization")     # 默认头名允许（大小写不敏感）
    validate_relay_profile(profile, credential=SECRET)
    assert profile.header_name is None


def test_auth_requires_credential():
    with pytest.raises(RelayError):
        auth_headers(make_profile(), None)


# ─────────────────────────────────────────────────────────── 头白名单
@pytest.mark.parametrize("bad_header", ["Host", "Authorization", "X-Forwarded-For"])
def test_extra_headers_reject_forbidden_or_unlisted(bad_header):
    profile = make_profile(extra_headers={bad_header: "x"})
    with pytest.raises(RelayError):
        validate_relay_profile(profile, credential=SECRET)


def test_extra_headers_allowlisted_and_crlf_rejected():
    profile = make_profile(extra_headers={"X-Title": "djb-node", "anthropic-version":
                                          "2023-06-01"})
    validate_relay_profile(profile, credential=SECRET)
    headers = request_headers(profile, SECRET)
    assert headers["x-title"] == "djb-node"
    assert headers["Authorization"].startswith("Bearer ")
    with pytest.raises(RelayError):
        validate_relay_profile(make_profile(extra_headers={"X-Title": "a\r\nHost: evil"}),
                               credential=SECRET)


# ─────────────────────────────────────────────────────────── URL / key 卫生
@pytest.mark.parametrize("query", ["api_key=abc", "key=abc", "access_token=abc"])
def test_base_url_rejects_key_in_query(query):
    with pytest.raises(RelayError) as excinfo:
        validate_relay_base_url("https://relay.example.com/v1?" + query)
    message = excinfo.value.message
    assert "URL query" in message
    assert "abc" not in message                            # 不回显参数值


def test_base_url_allows_benign_query_and_loopback_http():
    assert validate_relay_base_url("https://relay.example.com/v1?api-version=2024-06-01")
    assert validate_relay_base_url("http://127.0.0.1:8080/v1").startswith("http://127.0.0.1")


def test_base_url_rejects_userinfo_fragment_and_remote_http():
    for bad in ("https://user:pw@relay.example.com/v1",
                "https://relay.example.com/v1#frag",
                "http://relay.example.com/v1"):
        with pytest.raises(RelayError):
            validate_relay_base_url(bad, secret=SECRET)


def test_base_url_rejects_embedded_secret():
    with pytest.raises(RelayError) as excinfo:
        validate_relay_base_url("https://relay.example.com/v1/" + SECRET, secret=SECRET)
    assert SECRET not in excinfo.value.message


# ─────────────────────────────────────────────────────────── TLS pin
def test_self_signed_tls_requires_explicit_pin():
    with pytest.raises(RelayError):
        validate_relay_profile(make_profile(tls_verify=False), credential=SECRET)
    profile = make_profile(tls_verify=False, tls_pin_sha256="AB:" * 31 + "CD")
    validate_relay_profile(profile, credential=SECRET)
    assert profile.tls_pin_sha256 == "ab" * 31 + "cd"
    assert profile.tls_mode == "pin_only"


def test_tls_pin_format_rejected():
    for bad in ("deadbeef", "z" * 64, "aa" * 63):
        with pytest.raises(RelayError):
            normalize_tls_pin(bad)
    assert normalize_tls_pin("AA:BB" + ":CC" * 30) == "aabb" + "cc" * 30


def test_pin_of_der_and_peer_pin_check():
    der = b"fake-der-bytes-for-pin"
    expected = pin_of_der(der)

    class Sock:
        def getpeercert(self, binary_form=False):
            return der if binary_form else {}

    assert check_peer_pin(Sock(), expected) == expected
    with pytest.raises(Exception) as excinfo:
        check_peer_pin(Sock(), "00" * 32)
    assert "pin" in str(excinfo.value.message)


def test_build_ssl_context_modes():
    assert build_ssl_context(make_profile()) is None
    pin_only = build_ssl_context(make_profile(tls_verify=False, tls_pin_sha256="aa" * 32))
    assert pin_only is not None and pin_only.verify_mode.name == "CERT_NONE"
    pinned = build_ssl_context(make_profile(tls_pin_sha256="bb" * 32))
    assert pinned is not None and pinned.verify_mode.name == "CERT_REQUIRED"


# ─────────────────────────────────────────────────────────── 超时
def test_timeouts_order_and_bounds():
    profile = make_profile(timeouts={"connect": 5, "first_byte": 30, "total": 120})
    validate_relay_profile(profile, credential=SECRET)
    assert profile.total_timeout_s == 120.0
    for bad in ({"connect": 10, "first_byte": 5, "total": 120},
                {"connect": 5, "first_byte": 30, "total": 9999},
                {"connect": 5, "unknown": 1},
                {"connect": -1}):
        with pytest.raises(RelayError):
            validate_relay_profile(make_profile(timeouts=bad), credential=SECRET)


# ─────────────────────────────────────────────────────────── 发现
def test_discover_models_parses_maps_and_uses_auth_header():
    http = FakeHttp(payload={"data": [{"id": "gpt-4o"},
                                      {"id": "claude-3-7-sonnet"},
                                      {"id": "mystery-model"}]})
    profile = make_profile()
    result = discover_models(profile, SECRET, http=http, registry=REGISTRY)
    assert result.reachable and result.http_status == 200
    assert http.calls[0]["url"] == "https://relay.example.com/v1/models"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer " + SECRET
    assert http.calls[0]["timeout"] == 120.0
    states = {item.upstream_model_id: (item.state, item.canonical) for item in result.models}
    assert states["gpt-4o"] == ("mapped", "openai/gpt-4o")
    assert states["claude-3-7-sonnet"] == ("mapped", "anthropic/claude-3-7-sonnet")
    assert states["mystery-model"] == ("unmapped", None)


def test_discover_models_unauthorized_and_http_error():
    unauthorized = discover_models(make_profile(), SECRET, http=FakeHttp(status=401))
    assert unauthorized.error_class == "unauthorized" and not unauthorized.reachable
    failed = discover_models(make_profile(), SECRET, http=FakeHttp(status=503))
    assert failed.error_class == "http_503" and not failed.reachable


def test_discover_models_invalid_payload_and_network_error():
    invalid = discover_models(make_profile(), SECRET, http=FakeHttp(payload=["nope"]))
    assert invalid.error_class == "invalid_payload"
    error = NetworkError("无法连接", code=50903, reason="timeout")
    unreachable = discover_models(make_profile(), SECRET, http=FakeHttp(raise_exc=error))
    assert unreachable.error_class == "timeout" and not unreachable.reachable


def test_duplicate_and_conflicting_model_ids_rejected():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o"}, {"id": "GPT-4O"},
                        {"id": "bad model!"}, 42]}
    models, rejected = parse_models_payload(payload, registry=REGISTRY)
    assert [item.upstream_model_id for item in models] == ["gpt-4o"]
    reasons = {entry["reason"] for entry in rejected}
    assert {"duplicate_id", "id_conflict", "invalid_id", "invalid_item"} <= reasons


def test_unmapped_models_are_not_announced_or_serviced():
    profile = make_profile()
    result = discover_models(profile, SECRET,
                             http=FakeHttp(payload={"data": [{"id": "gpt-4o"},
                                                             {"id": "mystery"}]}),
                             registry=REGISTRY)
    apply_discovery(profile, result)
    assert announceable_models(profile) == ["openai/gpt-4o"]
    assert is_serviceable(profile, "openai/gpt-4o") is True
    assert is_serviceable(profile, "mystery") is False
    assert resolve_model(profile, "mystery") is None


def test_unmapped_model_lists_registry_candidates():
    canonical, candidates = canonical_slug("claude-3-7", registry=REGISTRY)
    assert canonical is None
    assert "anthropic/claude-3-7-sonnet" in candidates


def test_canonical_slug_offline_heuristics():
    assert canonical_slug("gpt-4o")[0] == "openai/gpt-4o"
    assert canonical_slug("openai/gpt-4o")[0] == "openai/gpt-4o"
    assert canonical_slug("kimi-k2-instruct")[0] == "moonshot/kimi-k2-instruct"
    assert canonical_slug("totally-unknown")[0] is None
    assert canonical_slug("bad model") == (None, [])


def test_alias_mapping_conflict_and_unmap():
    profile = make_profile()
    apply_discovery(profile, discover_models(
        profile, SECRET, http=FakeHttp(payload={"data": [{"id": "claude-3-7"}]}),
        registry=REGISTRY))
    map_alias(profile, "claude-3-7", "anthropic/claude-3-7-sonnet")
    assert resolve_model(profile, "claude-3-7") == "anthropic/claude-3-7-sonnet"
    assert announceable_models(profile) == ["anthropic/claude-3-7-sonnet"]
    assert list_aliases(profile) == {"claude-3-7": "anthropic/claude-3-7-sonnet"}
    with pytest.raises(RelayError):
        map_alias(profile, "claude-3-7", "openai/gpt-4o")
    map_alias(profile, "claude-3-7", "openai/gpt-4o", replace=True)
    assert resolve_model(profile, "claude-3-7") == "openai/gpt-4o"
    assert unmap_alias(profile, "claude-3-7") is True
    assert resolve_model(profile, "claude-3-7") is None


def test_user_mapping_wins_after_rediscovery():
    profile = make_profile()
    apply_discovery(profile, discover_models(
        profile, SECRET, http=FakeHttp(payload={"data": [{"id": "mystery"}]}),
        registry=REGISTRY))
    map_alias(profile, "mystery", "openai/gpt-4o-mini")
    apply_discovery(profile, discover_models(
        profile, SECRET, http=FakeHttp(payload={"data": [{"id": "mystery"}]}),
        registry=REGISTRY))
    assert resolve_model(profile, "mystery") == "openai/gpt-4o-mini"
    set_model_enabled(profile, "mystery", False)
    assert announceable_models(profile) == []


# ─────────────────────────────────────────────────────────── kv 持久化
def test_profile_roundtrip_uses_kv_and_never_touches_upstreams(store):
    profile = make_profile(extra_headers={"X-Title": "djb"})
    validate_relay_profile(profile, credential=SECRET)
    save_profile(store, profile)
    raw = store.get_kv("relay:u-relay-1")
    assert isinstance(raw, dict) and raw["base_url"] == "https://relay.example.com/v1"
    assert SECRET not in json.dumps(raw, ensure_ascii=False)
    loaded = load_profile(store, "u-relay-1")
    assert loaded is not None and loaded.auth_scheme == "bearer"
    assert [item.upstream_id for item in list_profiles(store)] == ["u-relay-1"]
    assert store.upstreams() == []                          # 不改 upstreams 表
    assert delete_profile(store, "u-relay-1") is True
    assert list_profiles(store) == []


# ─────────────────────────────────────────────────────────── 管理器 + 开关
def test_relay_manager_write_paths_disabled_by_default(store, credentials):
    manager = RelayManager(store, credentials, config=make_config(), http=FakeHttp())
    assert relay_enabled(manager.config) is False
    with pytest.raises(FeatureDisabled):
        manager.add(base_url="https://relay.example.com/v1", upstream_id="u1",
                    secret=SECRET)
    with pytest.raises(FeatureDisabled):
        manager.discover("u1")
    with pytest.raises(FeatureDisabled):
        manager.map("u1", "a", "openai/gpt-4o")


def test_relay_manager_enabled_flow_and_readonly_when_disabled(store, credentials):
    config = make_config(**{facts.RELAY_FLAG: True})
    http = FakeHttp(payload={"data": [{"id": "gpt-4o"}, {"id": "mystery"}]})
    manager = RelayManager(store, credentials, config=config, http=http)
    assert manager.enabled() is True
    profile = manager.add(base_url="https://relay.example.com/v1", upstream_id="u1",
                          secret=SECRET, extra_headers={"X-Title": "djb"})
    assert profile.upstream_id == "u1"
    assert credentials.upstream_secret("u1") == SECRET
    assert manager.credential_ref("u1") == "upstream:u1"
    result = manager.discover("u1", registry=REGISTRY)
    assert result.reachable and result.mapped
    assert manager.announceable("u1") == ["openai/gpt-4o"]
    assert manager.serviceable("u1", "openai/gpt-4o") is True
    status = manager.status("u1")
    assert SECRET not in json.dumps(status, ensure_ascii=False)
    assert status["switch"]["enabled"] is True

    # 关闭开关：写路径拒绝，读路径仍可读（§9 rollback_rule：已有配置只读、不删除）
    config.extra["feature_flags"] = {facts.RELAY_FLAG: False}
    with pytest.raises(FeatureDisabled):
        manager.discover("u1")
    assert manager.load("u1") is not None
    assert manager.announceable("u1") == ["openai/gpt-4o"]
    status = manager.status("u1")
    assert status["switch"]["enabled"] is False


def test_secret_never_leaks_into_profile_status_or_errors(store, credentials):
    config = make_config(**{facts.RELAY_FLAG: True})
    manager = RelayManager(store, credentials, config=config, http=FakeHttp())
    manager.add(base_url="https://relay.example.com/v1", upstream_id="u1", secret=SECRET)
    payloads = [json.dumps(manager.status("u1"), ensure_ascii=False),
                json.dumps(store.get_kv("relay:u1"), ensure_ascii=False),
                json.dumps(manager.load("u1").to_dict(), ensure_ascii=False)]
    for payload in payloads:
        assert SECRET not in payload
        assert SECRET[-8:] not in payload
    with pytest.raises(RelayError) as excinfo:
        manager.add(base_url="https://relay.example.com/v1?key=" + SECRET,
                    upstream_id="u2", secret=SECRET)
    assert SECRET not in excinfo.value.message
