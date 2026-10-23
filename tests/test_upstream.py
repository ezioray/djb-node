# SPDX-License-Identifier: Apache-2.0
import pytest

from djb_node import facts
from djb_node.errors import UsageError
from djb_node.upstream import (ModelEntry, UpstreamManager, a2_five_fields, a2_prompt,
                               chat_endpoint, models_endpoint, parse_models,
                               probe_upstream, validate_access_type, validate_base_url,
                               validate_min_price)
from tests.mock_m02 import MockServer


def test_access_type_and_base_url_validation():
    assert validate_access_type("A2") == "A2"
    with pytest.raises(UsageError):
        validate_access_type("A3")
    assert validate_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    for bad in ("ftp://x", "http://user:pass@host/v1", "http://host/v1#frag",
                "", "http://host/a b"):
        with pytest.raises(UsageError):
            validate_base_url(bad)


def test_parse_models_and_prices():
    entries = parse_models("a,b c")
    assert [item.id for item in entries] == ["a", "b", "c"]
    assert parse_models(["a", "a"])[0].id == "a"
    with pytest.raises(UsageError):
        parse_models("")
    with pytest.raises(UsageError):
        parse_models("bad model!")
    assert validate_min_price("0.35") == "0.35"
    with pytest.raises(UsageError):
        validate_min_price("abc")


def test_endpoint_normalisation():
    assert chat_endpoint("http://h:1/v1") == "http://h:1/v1/chat/completions"
    assert chat_endpoint("http://h:1") == "http://h:1/v1/chat/completions"
    assert models_endpoint("http://h:1/v1/") == "http://h:1/v1/models"
    assert models_endpoint("http://h:1") == "http://h:1/v1/models"


def test_a2_five_fields_and_prompt():
    fields = a2_five_fields(base_url="http://127.0.0.1:11434/v1", model="m1",
                            credential_ref="upstream:u1", concurrency_limit=3)
    assert set(fields) == set(facts.A2_FIVE_FIELDS)
    assert fields["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert fields["credential_ref"] == "upstream:u1"
    assert fields["receipt_sample"]["sig_provider"].startswith("hmac-sha256:")
    prompt = a2_prompt(base_url="http://127.0.0.1:11434/v1", model="m1",
                       credential_ref="upstream:u1", concurrency_limit=3)
    assert set(prompt["sections"]) == set(facts.A2_PROMPT_SECTIONS)
    for section in facts.A2_PROMPT_SECTIONS:
        assert "【" + section + "】" in prompt["text"]
    assert "凭据" in prompt["text"]


def test_manager_add_list_toggle_price_and_secret(paths, store, credentials, monkeypatch):
    manager = UpstreamManager(store, credentials)
    record = manager.add(access_type="A2", base_url="http://127.0.0.1:11434/v1",
                         models="m1,m2", secret="upstream-secret-value")
    assert record.access_type == "A2"
    assert record.credential_ref == "upstream:" + record.id
    assert credentials.upstream_secret(record.id) == "upstream-secret-value"
    payload = manager.declare_payload(record)
    assert payload["credential_handle"] == record.credential_ref
    assert "upstream-secret-value" not in str(payload)
    assert manager.set_model_enabled(record.id, "m1", False).models[0]["enabled"] is False
    assert manager.set_model_price(record.id, "m2", "0.5").models[1]["min_price"] == "0.5"
    catalog = manager.catalog()
    assert len(catalog) == 2
    assert any(item["price"] for item in catalog if item["id"] == "m2")
    assert manager.route("m2") is not None
    assert manager.route("nope") is None
    announced = manager.announce_capacity(store.profile())
    assert all(item["access"] == "A2" for item in announced)
    assert manager.remove(record.id) is True
    assert credentials.upstream_secret(record.id) is None


def test_probe_against_mock_upstream(store, credentials):
    server = MockServer().start()
    try:
        manager = UpstreamManager(store, credentials)
        record = manager.add(access_type="A1", base_url=server.upstream_base_url,
                             models="mock-model", secret="secret-value")
        result = manager.probe(record.id)
        assert result["reachable"] is True
        assert result["latency_ms"] >= 0
        assert result["model_checks"][0]["ok"] is True
        assert store.upstream(record.id).state == "verified"
        # 模型不在上游清单里 -> 不 ok，但不抛异常
        other = manager.add(access_type="A1", base_url=server.upstream_base_url,
                            models="other-model")
        result = manager.probe(other.id)
        assert result["model_checks"][0]["ok"] is False
    finally:
        server.stop()


def test_probe_unreachable_and_unauthorized():
    result = probe_upstream("http://127.0.0.1:1/v1", ["m"], credential=None,
                            timeout=0.2)
    assert result.reachable is False
    assert result.error_class in ("unreachable", "timeout")
    server = MockServer().start()
    try:
        result = probe_upstream(server.base_url + "/v1", ["mock-model"],
                                credential="wrong-key", timeout=2.0)
        # mock 的 /v1/models 不校验 key，这里只断言不崩溃
        assert result.reachable is True
    finally:
        server.stop()
