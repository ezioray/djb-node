# SPDX-License-Identifier: Apache-2.0
"""S2 P1 CLI 接线回归（C2/C7/C8/C11/C12 的 Lead 接线面）。

覆盖：token 计数、flags 开关读写、attest 只读、relay 开关门控。
不联网、不依赖数据库端口；全部用 conftest 的临时 home。
"""
from __future__ import annotations

import json

import pytest

from djb_node import facts
from djb_node.cli import main
from djb_node.config import Paths, feature_flag, load_config
from djb_node.store import Store


@pytest.fixture
def ready(home):
    assert main(["init", "--no-env"]) == 0
    return Paths(str(home))


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip())


def test_token_count_text(ready, capsys):
    assert main(["token", "count", "--model", "gpt-4o", "--text", "hello world",
                 "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["model"] == "gpt-4o"
    assert payload["source"] == "text"
    assert payload["tokens"] > 0
    assert payload["tokenizer_id"] == "heuristic:v1"
    assert payload["estimated"] is True


def test_token_count_messages_includes_template_overhead(ready, capsys):
    messages = json.dumps([{"role": "user", "content": "hi"}])
    assert main(["token", "count", "--model", "gpt-4o", "--messages", messages,
                 "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["source"] == "messages"
    assert payload["tokens"] >= 4


def test_flags_list_defaults_all_off(ready, capsys):
    assert main(["flags", "--json"]) == 0
    payload = _json_out(capsys)
    # 计数与名单都以登记表为唯一来源（U1：原硬编码 count == 7 在新增开关后陈旧失败）
    assert payload["count"] == len(facts.FEATURE_FLAGS)
    assert {item["name"] for item in payload["flags"]} == set(facts.FEATURE_FLAGS)
    # M17/P1 核心开关必须在列（名字本身的断言留在 test_feature_flags.py）
    assert {item["name"] for item in payload["flags"]} >= {
        "client.probe.periodic.enabled", "client.token.compare.enabled",
        "client.relay.enabled", "supply.attestation.enabled", "client.channel.enabled",
        "client.model_sync.enabled", "client.notifications.enabled"}
    assert all(item["enabled"] is False for item in payload["flags"])
    assert all(item["owner"] and item["expire_at"] and item["rollback_rule"]
               for item in payload["flags"])


def test_flags_enable_and_disable_roundtrip(ready, capsys):
    assert main(["flags", "--enable", "client.relay.enabled", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["changed"] == {"name": "client.relay.enabled", "value": True}
    config = load_config(ready)
    assert feature_flag(config, "client.relay.enabled") is True
    assert feature_flag(config, "client.token.compare.enabled") is False

    assert main(["flags", "--disable", "client.relay.enabled", "--json"]) == 0
    _json_out(capsys)
    assert feature_flag(load_config(ready), "client.relay.enabled") is False


def test_flags_unknown_rejected(ready):
    assert main(["flags", "--enable", "no.such.flag"]) == 2


def test_flags_enable_disable_are_exclusive(ready):
    assert main(["flags", "--enable", "client.relay.enabled",
                 "--disable", "client.relay.enabled"]) == 2


def test_attest_list_when_switch_off(ready, capsys):
    assert main(["attest", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["enabled"] is False
    assert payload["records"] == []
    assert payload["count"] == 0


def test_relay_add_requires_switch_and_creates_nothing(ready):
    assert main(["upstream", "add", "--access", "A1",
                 "--base-url", "https://relay.example.com/v1",
                 "--models", "gpt-4o", "--kind", "third_party_relay",
                 "--secret", "sk-test-not-real"]) == 2
    store = Store(ready.db_file)
    try:
        assert store.upstreams() == []
    finally:
        store.close()


def test_relay_list_with_switch_off_is_readonly(ready, capsys):
    assert main(["upstream", "relay", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["enabled"] is False
    assert payload["profiles"] == []
