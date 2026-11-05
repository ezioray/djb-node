# SPDX-License-Identifier: Apache-2.0
"""P2 · 探针协议感知 + `upstream_kind` 可见（CR-20261106 客户端）。

背景（2026-11-06）
------------------
客户端早已支持 ``anthropic-messages``（请求链接走 ``server.upstream_request`` 的转换），
但 **探针**只认 OpenAI：硬编码 ``Authorization: Bearer`` + ``/chat/completions``。
一条 ``protocol=anthropic-messages`` 的上游因此模型级探针必失败 → ``verified_at`` 不刷新
→ 云端 ``_gate_unverified`` 把 ``max_qps`` 清零 → 报价被 ``upstream_gated`` 吃掉，
**还连累同一上游的其它模型**（任一检查失败即 rejected）。

这里守三件事：

1. **向后兼容**：缺省（不传 ``protocol``）时端点 / 鉴权头 / 请求体与改前逐字一致；
2. **anthropic 正确**：端点 ``/messages``、头 ``x-api-key`` + ``anthropic-version``、
   体走协议转换，L2/L3 都能判成功，错误包装也不抛；
3. **接线**：`UpstreamManager.probe()` 把 ``record.protocol`` 传下去，
   两个模型的 anthropic 上游能整体 verified 且 ``max_qps > 0``（不再连累）。

全部离线：`tests.mock_probe.UpstreamStub`（单元）+ `tests.mock_m02.MockServer`（接线）。
"""
from __future__ import annotations

import json

import pytest

from djb_node import facts, probe as probe_mod
from djb_node import upstream_protocols as up
from djb_node.cli import main
from djb_node.config import Paths
from djb_node.probe import chat_body, probe_model, probe_models
from djb_node.upstream import UpstreamManager, probe_upstream
from tests.mock_probe import UpstreamStub

ANTHROPIC = "anthropic-messages"


@pytest.fixture
def stub():
    server = UpstreamStub(models=("probe-model", "claude-x")).start()
    yield server
    server.stop()


def _lower_headers(item: dict) -> dict[str, str]:
    return {str(key).lower(): value for key, value in (item.get("headers") or {}).items()}


def _last(requests: list[dict]) -> dict:
    assert requests, "上游没收到任何请求"
    return requests[-1]


# ─────────────────────────────────────────── 1. 向后兼容（缺省 = 改前行为）
def test_default_protocol_keeps_openai_endpoint_and_bearer(stub):
    outcome = probe_model(stub.upstream_base_url, "probe-model", credential="k-1",
                          level="L2", timeout=2.0)
    assert outcome.inference_ok is True
    request = _last(stub.chat_requests())
    assert request["path"] == "/v1/chat/completions"
    headers = _lower_headers(request)
    assert headers["authorization"] == "Bearer k-1"
    assert "x-api-key" not in headers and "anthropic-version" not in headers
    # 请求体与 `chat_body()` 逐字一致（老口径）
    assert request["body"] == chat_body("probe-model", stream=False)


def test_explicit_openai_protocol_matches_default(stub):
    probe_model(stub.upstream_base_url, "probe-model", level="L2", timeout=2.0,
                protocol="openai-chat")
    assert _last(stub.chat_requests())["path"] == "/v1/chat/completions"


def test_unknown_protocol_falls_back_to_openai(stub):
    """tolerant reader：未知协议名不崩、按 OpenAI 发（与 `normalize_protocol` 同口径）。"""
    outcome = probe_model(stub.upstream_base_url, "probe-model", level="L2",
                          timeout=2.0, protocol="some-new-protocol")
    assert outcome.inference_ok is True
    assert _last(stub.chat_requests())["path"] == "/v1/chat/completions"
    assert stub.messages_requests() == []


def test_probe_models_without_protocol_keeps_openai(stub):
    outcomes = probe_models(stub.upstream_base_url, ["probe-model"], level="L2",
                            timeout=2.0)
    assert outcomes["probe-model"].inference_ok is True
    assert len(stub.chat_requests()) == 1 and stub.messages_requests() == []


# ─────────────────────────────────────────── 2. anthropic：端点 / 头 / 体 / 判定
def test_anthropic_l2_request_shape(stub):
    outcome = probe_model(stub.upstream_base_url, "claude-x", credential="sk-ant-1",
                          level="L2", timeout=2.0, protocol=ANTHROPIC)
    assert outcome.reachable is True and outcome.auth_ok is True
    assert outcome.inference_ok is True and outcome.error_class is None
    assert outcome.usage_present is True
    assert outcome.token_usage == {"prompt_tokens": 3, "completion_tokens": 1,
                                   "total_tokens": 4}
    request = _last(stub.messages_requests())
    assert request["path"] == "/v1/messages"
    headers = _lower_headers(request)
    assert headers["x-api-key"] == "sk-ant-1"
    assert headers["anthropic-version"] == up.ANTHROPIC_VERSION
    assert "authorization" not in headers            # 不两套鉴权一起发
    body = request["body"]
    assert body["model"] == "claude-x"
    assert body["max_tokens"] == facts.PROBE_MAX_TOKENS
    assert body["messages"] == [{"role": "user",
                                 "content": [{"type": "text",
                                              "text": facts.PROBE_PROMPT}]}]
    assert "stream" not in body                      # L2 不带 stream（Anthropic 语义）
    assert stub.chat_requests() == []                # 一个字节都不打 OpenAI 端点


def test_anthropic_base_without_v1_still_gets_v1_messages(stub):
    """base 不带 `/v1` 时补 `/v1/messages`（`messages_path` 同一口径）。"""
    probe_model(stub.base_url, "claude-x", level="L2", timeout=2.0, protocol=ANTHROPIC)
    assert _last(stub.messages_requests())["path"] == "/v1/messages"


def test_anthropic_401_is_classified_not_raised(stub):
    stub.messages_status = 401
    outcome = probe_model(stub.upstream_base_url, "claude-x", credential="bad",
                          level="L2", timeout=2.0, protocol=ANTHROPIC)
    assert outcome.reachable is True
    assert outcome.auth_ok is False and outcome.inference_ok is False
    assert outcome.error_class == "unauthorized"


def test_anthropic_error_envelope_does_not_raise(stub):
    stub.anthropic_mode = "error_envelope"
    outcome = probe_model(stub.upstream_base_url, "claude-x", level="L2",
                          timeout=2.0, protocol=ANTHROPIC)
    assert outcome.auth_ok is True
    assert outcome.inference_ok is False
    assert outcome.error_class == "inference_failed"


def test_anthropic_missing_stop_reason_but_text_is_ok(stub):
    """宽容：只回正文不回 stop_reason 的兼容端点也算能推理。"""
    stub.anthropic_mode = "no_stop_reason"
    outcome = probe_model(stub.upstream_base_url, "claude-x", level="L2",
                          timeout=2.0, protocol=ANTHROPIC)
    assert outcome.inference_ok is True and outcome.error_class is None


def test_anthropic_missing_usage_still_counts_as_inference(stub):
    stub.anthropic_mode = "no_usage"
    outcome = probe_model(stub.upstream_base_url, "claude-x", level="L2",
                          timeout=2.0, protocol=ANTHROPIC)
    assert outcome.inference_ok is True
    assert outcome.usage_present is False and outcome.token_usage == {}


def test_anthropic_l3_stream_ok(stub):
    outcome = probe_model(stub.upstream_base_url, "claude-x", level="L3",
                          timeout=2.0, protocol=ANTHROPIC)
    assert outcome.level == "L3"
    assert outcome.stream_ok is True and outcome.inference_ok is True
    assert outcome.token_usage.get("prompt_tokens") == 3      # message_start
    assert outcome.token_usage.get("completion_tokens") == 1  # message_delta
    assert _last(stub.messages_requests())["body"]["stream"] is True


def test_anthropic_l3_without_message_stop_is_still_ok(stub):
    """没有 message_stop 但有 usage：不因收尾事件缺失就判失败。"""
    stub.anthropic_stream_mode = "no_stop"
    outcome = probe_model(stub.upstream_base_url, "claude-x", level="L3",
                          timeout=2.0, protocol=ANTHROPIC)
    assert outcome.stream_ok is True and outcome.error_class is None


def test_anthropic_l3_json_upstream_falls_back_to_l2(stub):
    """不支持 SSE 的 Anthropic 兼容端点：L3 判失败 → 降级 L2，且降级也用同一协议。"""
    stub.anthropic_stream_mode = "json"
    outcomes = probe_models(stub.upstream_base_url, ["claude-x"], level="L3",
                            timeout=2.0, protocol=ANTHROPIC)
    outcome = outcomes["claude-x"]
    assert outcome.inference_ok is True
    assert outcome.level == "L2"                 # 结论来自 L2 降级
    assert outcome.stream_ok is False            # 流式确实不通（保留 L3 的结论）
    assert outcome.error_class is None
    assert len(stub.messages_requests()) == 2                 # L3 一次 + L2 一次
    assert stub.chat_requests() == []


# ─────────────────────────────────────────── 3. L1（/models）鉴权头
def test_probe_upstream_anthropic_uses_x_api_key(stub):
    result = probe_upstream(stub.upstream_base_url, ["probe-model"],
                            credential="sk-ant-2", timeout=2.0, protocol=ANTHROPIC)
    assert result.reachable is True and result.error_class is None
    assert [item["model"] for item in result.model_checks] == ["probe-model"]
    assert result.model_checks[0]["ok"] is True
    headers = _lower_headers(_last(stub.models_requests()))
    assert headers["x-api-key"] == "sk-ant-2"
    assert headers["anthropic-version"] == up.ANTHROPIC_VERSION
    assert "authorization" not in headers


def test_probe_upstream_default_keeps_bearer(stub):
    probe_upstream(stub.upstream_base_url, ["probe-model"], credential="k-9", timeout=2.0)
    headers = _lower_headers(_last(stub.models_requests()))
    assert headers["authorization"] == "Bearer k-9"
    assert "x-api-key" not in headers


# ─────────────────────────────────────────── 4. 接线：record.protocol 传下去
def _manager(store, credentials) -> UpstreamManager:
    return UpstreamManager(store, credentials)


def test_manager_probe_passes_record_protocol(store, credentials, mock):
    """两个 claude 模型都走 anthropic 探针 → 整条上游 verified（不再互相连累）。"""
    mock.models = ["claude-a", "claude-b"]
    manager = _manager(store, credentials)
    record = manager.add(access_type="B", base_url=mock.upstream_base_url,
                        models="claude-a,claude-b", secret="sk-ant-3",
                        protocol=ANTHROPIC, upstream_kind="third_party_relay")
    payload = manager.probe(record.id, timeout=2.0)
    assert [item["ok"] for item in payload["model_checks"]] == [True, True]
    assert store.upstream(record.id).state == "verified"
    messages = [item for item in mock.requests if item["path"] == "/v1/messages"]
    chats = [item for item in mock.requests if item["path"] == "/v1/chat/completions"]
    assert len(messages) == 2 and chats == []
    assert all(_lower_headers(item)["x-api-key"] == "sk-ant-3" for item in messages)
    # 公告侧：两个模型都拿到正 QPS（此前被判 rejected ⇒ max_qps=0）
    entries = manager.announce_capacity(store.profile())
    assert {entry["model"] for entry in entries} == {"claude-a", "claude-b"}
    assert all(entry["max_qps"] > 0 for entry in entries)
    assert all(entry["unit"] == facts.CAPACITY_UNIT for entry in entries)


def test_manager_probe_openai_upstream_unchanged(store, credentials, mock):
    mock.models = ["mock-model"]
    manager = _manager(store, credentials)
    record = manager.add(access_type="A1", base_url=mock.upstream_base_url,
                         models="mock-model", secret="sk-openai")
    payload = manager.probe(record.id, timeout=2.0)
    assert [item["ok"] for item in payload["model_checks"]] == [True]
    assert store.upstream(record.id).state == "verified"
    chats = [item for item in mock.requests if item["path"] == "/v1/chat/completions"]
    assert chats and all("/v1/messages" not in item["path"] for item in mock.requests)
    assert _lower_headers(chats[0])["authorization"] == "Bearer sk-openai"


# ─────────────────────────────────────────── 5. upstream_kind 可见
def test_upstream_record_to_dict_includes_kind(store, credentials):
    manager = _manager(store, credentials)
    relay = manager.add(access_type="B", base_url="https://agg.example.com/v1",
                        models="gpt-4o", upstream_kind="third_party_relay")
    plain = manager.add(access_type="A2", base_url="http://127.0.0.1:11434/v1",
                        models="local-model")
    assert store.upstream(relay.id).to_dict()["upstream_kind"] == "third_party_relay"
    assert store.upstream(plain.id).to_dict()["upstream_kind"] is None
    # 既有键一个都不少（只加不改）
    assert set(store.upstream(relay.id).to_dict()) >= {
        "id", "access_type", "base_url", "models", "credential_ref", "state",
        "last_probe_at", "protocol", "created_at", "enabled"}


@pytest.fixture
def ready(home):
    assert main(["init", "--no-env"]) == 0
    return Paths(str(home))


def test_cli_upstream_list_shows_kind(ready, store, credentials, capsys):
    manager = _manager(store, credentials)
    manager.add(access_type="B", base_url="https://agg.example.com/v1",
                models="gpt-4o", upstream_kind="third_party_relay")
    manager.add(access_type="A2", base_url="http://127.0.0.1:11434/v1",
                models="local-model")
    assert main(["upstream", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    kinds = {item["id"]: item["upstream_kind"] for item in payload["upstreams"]}
    assert sorted(kinds.values(), key=str) == [None, "third_party_relay"]
    # 文本面：中转站那行多印 kind，旧上游那行逐字不变
    assert main(["upstream", "list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert any("third_party_relay" in line for line in lines)
    plain = [line for line in lines if "local-model" in line][0]
    assert "third_party_relay" not in plain
    assert plain.count(" · ") == 4                    # id·access·base·state·模型


def test_diagnose_summary_exposes_kind_and_protocol(paths, store, credentials):
    """诊断包（脱敏）里也带 kind / protocol：排障不用先猜「这是不是中转站」。"""
    from djb_node.config import ClientConfig
    from djb_node.diagnose import DiagnoseBuilder

    manager = _manager(store, credentials)
    manager.add(access_type="B", base_url="https://agg.example.com/v1",
                models="gpt-4o", protocol=ANTHROPIC,
                upstream_kind="third_party_relay")
    summary = DiagnoseBuilder(paths, store, credentials, ClientConfig()).db_summary()
    row = summary["upstreams"][0]
    assert row["upstream_kind"] == "third_party_relay"
    assert row["protocol"] == ANTHROPIC
    assert "credential_ref_set" in row and "secret" not in json.dumps(row)


def test_daemon_status_exposes_kind_and_protocol(paths, store, credentials, mock):
    """`djb status --json` 的上游段也带 kind / protocol（只加字段）。"""
    from tests.test_daemon import _daemon

    manager = _manager(store, credentials)
    manager.add(access_type="B", base_url=mock.upstream_base_url, models="mock-model",
                secret="sk-ant-4", protocol=ANTHROPIC,
                upstream_kind="third_party_relay")
    daemon = _daemon(paths, store, credentials, mock)
    row = daemon.status()["upstreams"][0]
    assert row["upstream_kind"] == "third_party_relay"
    assert row["protocol"] == ANTHROPIC


def test_probe_outcome_dict_shape_unchanged():
    """回归：`ProbeOutcome.to_dict()` 的键集合不许因为本卡变化（只加不改）。"""
    assert set(probe_mod.ProbeOutcome().to_dict()) == {
        "level", "reachable", "auth_ok", "inference_ok", "stream_ok",
        "usage_present", "latency_ms", "error_class", "token_usage"}
