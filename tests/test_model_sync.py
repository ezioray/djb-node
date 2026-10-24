# SPDX-License-Identifier: Apache-2.0
"""M18b · 客户端模型/tokenizer 同步：版本单调、hash/TTL/签名校验、unsupported、合并保留、
原子写 0600、周期与退避、默认关闭、日志无凭据/正文。

不联网（本机回环 mock / 注入替身）、不真 sleep（注入 sleep 记录器）。
"""
from __future__ import annotations

import json
import logging
import os
import threading

import pytest

from djb_node import facts, logging_setup, model_sync as msync
from djb_node import tokens
from djb_node.api import NodeApi
from djb_node.config import ClientConfig
from djb_node.errors import NetworkError
from djb_node.httpclient import HttpResponse
from tests.mock_model_sync import MockModelSync

SECRET_TOKEN = "ck_" + "test_model_sync_key_0123456789abcdef"
NOW = "2026-10-25T00:00:00Z"


# ─────────────────────────────────────────────────────────── 夹具 / 替身
class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None) or {}
        merged = {"event": getattr(record, "event", record.getMessage())}
        merged.update(fields)
        self.items.append(merged)


@pytest.fixture
def events():
    logger = logging_setup.get_logger()
    handler = Capture()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.items
    logger.removeHandler(handler)
    logger.setLevel(previous)


class FakeApi:
    def __init__(self, catalog=None, *, fetch_error=None, state_error=None) -> None:
        self.catalog = catalog
        self.fetch_error = fetch_error
        self.state_error = state_error
        self.catalog_calls: list[object] = []
        self.state_payloads: list[dict] = []
        self.stop_event: threading.Event | None = None
        self.stop_after: int | None = None

    def model_catalog(self, since=None):
        self.catalog_calls.append(since)
        if (self.stop_event is not None and self.stop_after is not None
                and len(self.catalog_calls) >= self.stop_after):
            self.stop_event.set()
        if self.fetch_error is not None:
            raise self.fetch_error
        return dict(self.catalog) if isinstance(self.catalog, dict) else self.catalog

    def submit_model_sync_state(self, payload):
        self.state_payloads.append(dict(payload))
        if self.state_error is not None:
            raise self.state_error
        return {"accepted": True, "server_version": 9}


def bundle(version=1, *, models=None, expires_at="2026-10-26T00:00:00Z",
           signature="sig-v1", tamper_hash=False) -> dict:
    """构造一个 hash 自洽的 bundle。"""
    payload = {
        "version": version,
        "expires_at": expires_at,
        "signature": signature,
        "not_modified": False,
        "models": models if models is not None else [
            {"vendor": "openai", "canonical": "gpt-4o",
             "aliases": ["gpt-4o-2024-11-20"], "context_window": 128000,
             "tokenizer_family": "tiktoken", "tokenizer_version": "o200k_base",
             "tokenizer_hash": "a" * 64, "effective_at": "2026-10-20T00:00:00Z"},
        ],
    }
    payload["payload_hash"] = ("b" * 64) if tamper_hash else msync.payload_hash(payload)
    return payload


def make_client(paths, credentials, *, api=None, config=None, verifier=None,
                sleep=None, clock=None) -> msync.ModelSyncClient:
    credentials.set_node("node-1", SECRET_TOKEN)
    config = config or ClientConfig(gateway_url="http://127.0.0.1:1",
                                    mirror_port=1717,
                                    extra={facts.MODEL_SYNC_FLAG: True})
    return msync.ModelSyncClient(paths, config, credentials, api=api,
                                 verifier=verifier, sleep=sleep, now=clock)


def state_file(paths) -> str:
    return msync.bundle_file(paths)


# ─────────────────────────────────────────────────────────── 开关
def test_flag_default_off_with_three_fields():
    for config in (None, ClientConfig()):
        assert msync.model_sync_enabled(config) is False
    spec = facts.FEATURE_FLAGS[facts.MODEL_SYNC_FLAG]
    assert facts.MODEL_SYNC_FLAG == "client.model_sync.enabled"
    assert spec["default"] is False
    assert spec["owner"] == "client-seat"
    assert spec["expire_at"] == "2026-12-31"
    assert "停拉取" in str(spec["rollback_rule"])
    assert spec.get("description") and "desc" not in spec
    on = ClientConfig(extra={"feature_flags": {facts.MODEL_SYNC_FLAG: True}})
    assert msync.model_sync_enabled(on) is True


# ─────────────────────────────────────────────────────────── 应用 / 落盘
def test_sync_applies_bundle_and_writes_0600(paths, credentials, clock, events):
    api = FakeApi(bundle(version=3))
    client = make_client(paths, credentials, api=api, clock=clock)
    outcome = client.sync()
    assert outcome["changed"] is True
    assert outcome["applied_version"] == 3
    assert outcome["error_class"] is None
    assert outcome["models"] == 1
    path = state_file(paths)
    assert os.path.exists(path)
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    saved = json.loads(open(path, encoding="utf-8").read())
    assert saved["current_version"] == 3
    assert saved["applied_at"] == "2026-10-24T00:00:00Z"
    entry = saved["models"]["gpt-4o"]
    assert entry["tokenizer_family"] == "tiktoken"
    assert entry["tokenizer_version"] == "o200k_base"
    assert entry["context_window"] == 128000
    assert entry["aliases"] == ["gpt-4o-2024-11-20"]
    assert entry["supported"] is True
    assert api.state_payloads[-1]["applied_version"] == 3
    # 原子写：目录里没有临时残留
    assert [name for name in os.listdir(paths.home)
            if name.startswith(facts.MODEL_SYNC_FILE + ".")] == []


def test_tokenizer_implementation_is_not_changed(paths, credentials, clock):
    before = tokens.count_text("gpt-4o", "hello 世界").as_dict()
    client = make_client(paths, credentials, api=FakeApi(bundle(version=1)), clock=clock)
    client.sync()
    after = tokens.count_text("gpt-4o", "hello 世界").as_dict()
    assert after == before          # 同步只写元数据，不改分词实现


def test_status_shape_has_no_secret(paths, credentials, clock):
    client = make_client(paths, credentials, api=FakeApi(bundle(version=2)), clock=clock)
    client.sync()
    status = client.status()
    for key in ("enabled", "current_version", "applied_at", "models", "unsupported",
                "drift", "last_error"):
        assert key in status
    assert status["current_version"] == 2
    assert status["models"] == 1
    dumped = json.dumps(status, ensure_ascii=False)
    assert SECRET_TOKEN not in dumped
    assert "messages" not in status and "content" not in status


# ─────────────────────────────────────────────────────────── 校验：失败保留旧版
def _seed(paths, credentials, clock, version=5):
    client = make_client(paths, credentials, api=FakeApi(bundle(version=version)),
                         clock=clock)
    assert client.sync()["applied_version"] == version
    return client


def test_hash_tamper_is_rejected_and_old_version_kept(paths, credentials, clock, events):
    client = _seed(paths, credentials, clock, version=5)
    bad = FakeApi(bundle(version=6, tamper_hash=True))
    client.api = bad
    outcome = client.sync()
    assert outcome["error_class"] == "hash_mismatch"
    assert outcome["applied_version"] == 5           # 旧版保留
    assert client.status()["current_version"] == 5
    rejected = [item for item in events if item["event"] == "model_sync.rejected"]
    assert rejected and rejected[0]["error_class"] == "hash_mismatch"
    assert bad.state_payloads[-1]["error_class"] == "hash_mismatch"


def test_version_regression_is_rejected(paths, credentials, clock):
    client = _seed(paths, credentials, clock, version=5)
    client.api = FakeApi(bundle(version=4))
    outcome = client.sync()
    assert outcome["error_class"] == "version_regression"
    assert outcome["applied_version"] == 5
    assert client.status()["current_version"] == 5


def test_version_invalid_is_rejected(paths, credentials, clock):
    payload = bundle(version=7)
    payload["version"] = "abc"
    client = make_client(paths, credentials, api=FakeApi(payload), clock=clock)
    assert client.sync()["error_class"] == "version_invalid"


def test_equal_version_is_not_modified_and_not_reported(paths, credentials, clock):
    client = _seed(paths, credentials, clock, version=5)
    api = FakeApi(bundle(version=5))
    client.api = api
    outcome = client.sync()
    assert outcome["changed"] is False and outcome["error_class"] is None
    assert outcome["applied_version"] == 5
    assert api.state_payloads == []                  # 无变化不重复上报


def test_not_modified_flag_skips_apply(paths, credentials, clock):
    client = _seed(paths, credentials, clock, version=5)
    api = FakeApi({"not_modified": True, "version": 5})
    client.api = api
    outcome = client.sync()
    assert outcome["changed"] is False
    assert outcome["applied_version"] == 5
    assert api.state_payloads == []


def test_expired_bundle_is_rejected(paths, credentials, clock):
    client = make_client(paths, credentials,
                         api=FakeApi(bundle(version=1,
                                            expires_at="2026-10-24T00:00:00Z")),
                         clock=clock)
    assert client.sync()["error_class"] == "expired"     # clock 固定在 2026-10-24T00:00Z


def test_missing_models_is_payload_invalid(paths, credentials, clock):
    payload = bundle(version=1)
    payload["models"] = []
    payload["payload_hash"] = msync.payload_hash(payload)
    client = make_client(paths, credentials, api=FakeApi(payload), clock=clock)
    assert client.sync()["error_class"] == "payload_invalid"


# ─────────────────────────────────────────────────────────── 签名
def test_verifier_pass_marks_verified(paths, credentials, clock):
    seen: list[tuple] = []

    def verifier(payload, signature):
        seen.append((payload.get("version"), signature))
        return True

    client = make_client(paths, credentials, api=FakeApi(bundle(version=2)),
                         verifier=verifier, clock=clock)
    outcome = client.sync()
    assert outcome["verified"] is True
    assert client.status()["verified"] is True
    assert seen == [(2, "sig-v1")]


def test_verifier_fail_is_rejected(paths, credentials, clock, events):
    client = make_client(paths, credentials, api=FakeApi(bundle(version=2)),
                         verifier=lambda payload, signature: False, clock=clock)
    outcome = client.sync()
    assert outcome["error_class"] == "signature_invalid"
    assert outcome["changed"] is False
    assert not os.path.exists(state_file(paths))
    assert [item for item in events if item["event"] == "model_sync.rejected"]


def test_verifier_requires_signature_field(paths, credentials, clock):
    client = make_client(paths, credentials,
                         api=FakeApi(bundle(version=2, signature="")),
                         verifier=lambda payload, signature: True, clock=clock)
    assert client.sync()["error_class"] == "signature_invalid"


def test_no_verifier_warns_but_applies(paths, credentials, clock, events):
    client = make_client(paths, credentials, api=FakeApi(bundle(version=2)), clock=clock)
    outcome = client.sync()
    assert outcome["changed"] is True and outcome["verified"] is False
    warned = [item for item in events if item["event"] == "model_sync.unverified"]
    assert warned and warned[0]["reason"] == "no_verifier"


# ─────────────────────────────────────────────────────────── unsupported / 合并 / 漂移
def test_unknown_tokenizer_is_unsupported(paths, credentials, clock):
    models = [{"vendor": "acme", "canonical": "acme-1", "aliases": [],
               "context_window": 8000, "tokenizer_family": "sentencepiece",
               "tokenizer_version": "v9", "tokenizer_hash": "c" * 64,
               "effective_at": "2026-10-20T00:00:00Z"}]
    client = make_client(paths, credentials,
                         api=FakeApi(bundle(version=1, models=models)), clock=clock)
    outcome = client.sync()
    assert outcome["unsupported"] == ["acme-1"]
    saved = json.loads(open(state_file(paths), encoding="utf-8").read())
    assert saved["unsupported"] == ["acme-1"]
    assert saved["models"]["acme-1"]["supported"] is False     # 计数按 estimated 参考
    assert client.status()["unsupported"] == ["acme-1"]


def test_merge_keeps_previous_models(paths, credentials, clock):
    first = [{"vendor": "openai", "canonical": "gpt-4o", "aliases": [],
              "context_window": 128000, "tokenizer_family": "tiktoken",
              "tokenizer_version": "o200k_base", "tokenizer_hash": "a" * 64,
              "effective_at": "2026-10-20T00:00:00Z"},
             {"vendor": "openai", "canonical": "gpt-4.1", "aliases": [],
              "context_window": 1000000, "tokenizer_family": "tiktoken",
              "tokenizer_version": "o200k_base", "tokenizer_hash": "a" * 64,
              "effective_at": "2026-10-20T00:00:00Z"}]
    client = make_client(paths, credentials,
                         api=FakeApi(bundle(version=1, models=first)), clock=clock)
    client.sync()
    # 第二版只带 gpt-4o：gpt-4.1 必须保留（不删旧模型）
    second = [{"vendor": "openai", "canonical": "gpt-4o", "aliases": [],
               "context_window": 128000, "tokenizer_family": "tiktoken",
               "tokenizer_version": "o200k_base", "tokenizer_hash": "a" * 64,
               "effective_at": "2026-10-21T00:00:00Z"}]
    client.api = FakeApi(bundle(version=2, models=second))
    outcome = client.sync()
    assert outcome["models"] == 2
    saved = json.loads(open(state_file(paths), encoding="utf-8").read())
    assert set(saved["models"]) == {"gpt-4o", "gpt-4.1"}
    assert saved["models"]["gpt-4o"]["effective_at"] == "2026-10-21T00:00:00Z"


def test_drift_is_recorded_and_reported(paths, credentials, clock, events):
    client = _seed(paths, credentials, clock, version=1)
    changed = [{"vendor": "openai", "canonical": "gpt-4o", "aliases": [],
                "context_window": 200000, "tokenizer_family": "tiktoken",
                "tokenizer_version": "o200k_v2", "tokenizer_hash": "d" * 64,
                "effective_at": "2026-10-22T00:00:00Z"}]
    api = FakeApi(bundle(version=2, models=changed))
    client.api = api
    outcome = client.sync()
    fields = {(item["field"], item["from"], item["to"]) for item in outcome["drift"]}
    assert ("tokenizer_version", "o200k_base", "o200k_v2") in fields
    assert ("context_window", 128000, 200000) in fields
    assert api.state_payloads[-1]["drift"] == outcome["drift"]
    assert client.status()["drift"] == outcome["drift"]


# ─────────────────────────────────────────────────────────── 周期 / 退避
def test_periodic_syncs_then_waits_interval(paths, credentials, clock):
    sleeps: list[float] = []
    api = FakeApi(bundle(version=1))
    client = make_client(paths, credentials, api=api, clock=clock, sleep=sleeps.append)
    stop = threading.Event()
    api.stop_event = stop
    api.stop_after = 2
    client.run_periodic(stop)
    assert len(api.catalog_calls) == 2
    assert sleeps == [float(facts.MODEL_SYNC_INTERVAL_S)] * 2


def test_periodic_backoff_on_fetch_failure(paths, credentials, clock):
    sleeps: list[float] = []
    api = FakeApi(fetch_error=NetworkError("net", code=50903))
    client = make_client(paths, credentials, api=api, clock=clock, sleep=sleeps.append)
    stop = threading.Event()
    api.stop_event = stop
    api.stop_after = 3
    client.run_periodic(stop)
    assert sleeps == [60.0, 300.0, 900.0]
    assert client.status()["last_error"] == "fetch_failed"


def test_periodic_stops_immediately_when_event_already_set(paths, credentials, clock):
    sleeps: list[float] = []
    api = FakeApi(bundle(version=1))
    client = make_client(paths, credentials, api=api, clock=clock, sleep=sleeps.append)
    stop = threading.Event()
    stop.set()
    client.run_periodic(stop)
    assert api.catalog_calls == [] and sleeps == []


def test_fetch_failure_reports_error_class(paths, credentials, clock, events):
    api = FakeApi(fetch_error=NetworkError("net", code=50903))
    client = make_client(paths, credentials, api=api, clock=clock)
    outcome = client.sync()
    assert outcome["error_class"] == "fetch_failed"
    assert api.state_payloads[-1]["error_class"] == "fetch_failed"
    assert [item for item in events if item["event"] == "model_sync.fetch_failed"]


def test_state_report_failure_does_not_raise(paths, credentials, clock, events):
    api = FakeApi(bundle(version=1), state_error=NetworkError("boom", code=50001))
    client = make_client(paths, credentials, api=api, clock=clock)
    outcome = client.sync()
    assert outcome["changed"] is True          # 应用成功
    assert outcome["reported"] is False        # 上报失败只记事件
    assert [item for item in events if item["event"] == "model_sync.state_report_failed"]


def test_state_payload_only_has_contract_keys(paths, credentials, clock):
    api = FakeApi(bundle(version=2))
    client = make_client(paths, credentials, api=api, clock=clock)
    client.sync()
    payload = api.state_payloads[-1]
    assert set(payload) == {"applied_version", "drift", "error_class"}
    assert payload["applied_version"] == 2 and payload["error_class"] is None


def test_events_have_no_token_or_prompt_body(paths, credentials, clock, events):
    client = make_client(paths, credentials, api=FakeApi(bundle(version=1)), clock=clock)
    client.sync()
    dumped = json.dumps(events, ensure_ascii=False)
    assert SECRET_TOKEN not in dumped
    keys = {key for item in events for key in item}
    assert not keys & {"messages", "content", "prompt", "token", "client_key"}


# ─────────────────────────────────────────────────────────── api.py 层
class FakeHttp:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, json_body=None, body=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "json_body": json_body, "timeout": timeout})
        return self.response


def test_api_model_catalog_sends_key_and_since():
    body = json.dumps({"version": 3, "models": []}).encode()
    http = FakeHttp(HttpResponse(status=200, headers={}, body=body, url="", elapsed_ms=1))
    api = NodeApi("https://gateway.test", client_key=SECRET_TOKEN, http=http)
    payload = api.model_catalog(since=2)
    assert payload["version"] == 3
    call = http.calls[0]
    assert facts.MODEL_CATALOG_PATH in call["url"] and call["url"].endswith("since=2")
    assert call["timeout"] == facts.MODEL_SYNC_TIMEOUT_S
    from djb_node import protocol
    assert call["headers"][protocol.NODE_KEY_HEADER] == SECRET_TOKEN


def test_api_model_catalog_304_returns_not_modified():
    http = FakeHttp(HttpResponse(status=304, headers={}, body=b"", url="", elapsed_ms=1))
    api = NodeApi("https://gateway.test", client_key=SECRET_TOKEN, http=http)
    assert api.model_catalog(since=3)["not_modified"] is True


def test_api_submit_state_requires_accepted():
    http = FakeHttp(HttpResponse(status=200, headers={}, body=b"{}", url="", elapsed_ms=1))
    api = NodeApi("https://gateway.test", client_key=SECRET_TOKEN, http=http)
    with pytest.raises(NetworkError):
        api.submit_model_sync_state({"applied_version": 1, "drift": [],
                                     "error_class": None})


# ─────────────────────────────────────────────────────────── 真实回环集成
def test_integration_pull_apply_and_report(paths, credentials):
    server = MockModelSync().start()
    try:
        payload = bundle(version=4)
        server.catalog = payload
        server.server_version = 4
        config = ClientConfig(gateway_url=server.base_url, mirror_port=1717,
                              extra={facts.MODEL_SYNC_FLAG: True})
        credentials.set_node("node-1", SECRET_TOKEN)
        client = msync.ModelSyncClient(paths, config, credentials)
        outcome = client.sync()
        assert outcome["changed"] is True and outcome["applied_version"] == 4
        assert server.catalog_requests and "since=" not in server.catalog_requests[0]
        assert server.state_requests[-1]["applied_version"] == 4
        from djb_node import protocol
        assert server.catalog_headers[0][protocol.NODE_KEY_HEADER.lower()] == SECRET_TOKEN
        # 第二次：同版本 → 304 路径（mock 回 not_modified），不再上报
        server.catalog_status = 304
        again = client.sync()
        assert again["changed"] is False
        assert len(server.state_requests) == 1
    finally:
        server.stop()


# ─────────────────────────────────────────────────────────── daemon 线程起停
class StubModelSync:
    instances: list["StubModelSync"] = []

    def __init__(self, paths, config, credentials, **kwargs) -> None:
        self.kwargs = kwargs
        self.stop_event: threading.Event | None = None
        StubModelSync.instances.append(self)

    def status(self):
        return {"current_version": 0, "applied_at": None, "models": 0,
                "unsupported": [], "drift": [], "last_error": None,
                "verified": False, "next_at": None}

    def run_periodic(self, stop_event):
        self.stop_event = stop_event
        stop_event.wait(30)


@pytest.fixture
def daemon_env(paths, store, credentials):
    from tests.mock_m02 import MockServer
    from tests.test_daemon import _daemon

    server = MockServer().start()
    yield paths, store, credentials, server, _daemon
    server.stop()


def test_daemon_has_no_model_sync_by_default(daemon_env, monkeypatch):
    from djb_node import daemon as daemon_module

    paths, store, credentials, mock, make_daemon = daemon_env
    StubModelSync.instances = []
    monkeypatch.setattr(daemon_module, "ModelSyncClient", StubModelSync)
    daemon = make_daemon(paths, store, credentials, mock)
    daemon.start()
    try:
        assert StubModelSync.instances == []
        assert daemon._model_sync is None and daemon._model_sync_thread is None
        status = daemon.status()
        assert status["model_sync"]["enabled"] is False
        assert status["model_sync"]["running"] is False
        assert status["flags"][facts.MODEL_SYNC_FLAG]["enabled"] is False
    finally:
        daemon.stop()


def test_daemon_starts_and_stops_model_sync_thread(daemon_env, monkeypatch):
    from djb_node import daemon as daemon_module

    paths, store, credentials, mock, make_daemon = daemon_env
    StubModelSync.instances = []
    monkeypatch.setattr(daemon_module, "ModelSyncClient", StubModelSync)
    config = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                          extra={facts.MODEL_SYNC_FLAG: True})
    daemon = make_daemon(paths, store, credentials, mock, config=config)
    daemon.start()
    thread = daemon._model_sync_thread
    try:
        assert len(StubModelSync.instances) == 1
        assert thread is not None and thread.is_alive()
        status = daemon.status()
        assert status["model_sync"]["enabled"] is True
        assert status["model_sync"]["running"] is True
        assert status["model_sync"]["interval_s"] == facts.MODEL_SYNC_INTERVAL_S
        assert status["flags"][facts.MODEL_SYNC_FLAG]["enabled"] is True
    finally:
        daemon.stop()
    assert thread is not None and not thread.is_alive()
    assert daemon._model_sync_thread is None
    assert daemon.status()["model_sync"]["running"] is False


# ─────────────────────────────────────────────────────────── CLI / 卸载残留
def test_cli_model_sync_status_and_sync(paths, store, credentials, home, capsys):
    from djb_node.cli import main
    from djb_node.config import save_config

    server = MockModelSync().start()
    try:
        server.catalog = bundle(version=6)
        config = ClientConfig(gateway_url=server.base_url, mirror_port=1717)
        save_config(paths, config)
        credentials.set_node("node-1", SECRET_TOKEN)
        assert main(["model-sync", "status", "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["current_version"] == 0 and status["enabled"] is False
        assert main(["model-sync", "sync", "--json"]) == 0
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["changed"] is True and result["applied_version"] == 6
        assert result["status"]["models"] == 1
        assert SECRET_TOKEN not in out            # 不打印 key
        assert server.state_requests[-1]["applied_version"] == 6
    finally:
        server.stop()


def test_uninstall_residual_includes_model_sync_file(paths, credentials, clock):
    from djb_node.cli import _residual_hits

    client = make_client(paths, credentials, api=FakeApi(bundle(version=1)), clock=clock)
    client.sync()
    assert os.path.exists(state_file(paths))
    assert state_file(paths) in _residual_hits(paths, keep_logs=True)


def test_hmac_verifier_roundtrip_and_tamper():
    import hashlib
    import hmac

    from djb_node.model_sync import hmac_verifier, payload_hash

    payload = {"version": 3, "expires_at": "2026-10-25T00:00:00.000Z",
               "models": [{"vendor": "openai", "canonical": "openai/gpt-4o-mini",
                           "aliases": [], "context_window": 128000,
                           "tokenizer_family": "o200k_base", "tokenizer_version": "2026-10",
                           "tokenizer_hash": None, "effective_at": None}]}
    key = b"test-secret"
    sig = "hmac-sha256:" + hmac.new(key, payload_hash(payload).encode("ascii"),
                                    hashlib.sha256).hexdigest()
    verify = hmac_verifier(key)
    assert verify(payload, sig) is True
    assert verify(payload, sig[:-1] + "0") is False
    assert verify(payload, "ed25519:deadbeef") is False
