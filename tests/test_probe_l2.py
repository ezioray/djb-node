# SPDX-License-Identifier: Apache-2.0
"""C7 · L2/L3 探针单元用例（合成 prompt、无外网、不 sleep）。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from djb_node import facts, probe as probe_mod
from djb_node.probe import (ProbeOutcome, chat_body, normalize_level, probe_model,
                            probe_models, usage_from_payload, usage_from_sse_line)
from tests.mock_probe import UpstreamStub


@pytest.fixture
def stub():
    server = UpstreamStub().start()
    yield server
    server.stop()


def _probe(stub, **kwargs) -> ProbeOutcome:
    kwargs.setdefault("timeout", 2.0)
    return probe_model(stub.upstream_base_url, "probe-model", **kwargs)


# ─────────────────────────────────────────────────────────── L2（非流式）
def test_l2_success_reads_finish_reason_and_usage(stub):
    outcome = _probe(stub, level="L2")
    assert outcome.level == "L2"
    assert outcome.reachable is True
    assert outcome.auth_ok is True
    assert outcome.inference_ok is True
    assert outcome.usage_present is True
    assert outcome.token_usage == {"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2}
    assert outcome.error_class is None
    assert outcome.latency_ms >= 0
    assert outcome.stream_ok is None              # L2 不算流式


def test_l2_unauthorized_is_classified_not_raised(stub):
    stub.chat_status = 401
    outcome = _probe(stub, level="L2", credential="wrong-key")
    assert outcome.reachable is True
    assert outcome.auth_ok is False
    assert outcome.inference_ok is False
    assert outcome.error_class == "unauthorized"


def test_l2_http_error_class(stub):
    stub.chat_status = 500
    outcome = _probe(stub, level="L2")
    assert outcome.reachable is True
    assert outcome.inference_ok is False
    assert outcome.error_class == "http_500"


def test_l2_unreachable_endpoint_does_not_raise():
    outcome = probe_model("http://127.0.0.1:1/v1", "probe-model", level="L2",
                          timeout=0.3)
    assert outcome.reachable is False
    assert outcome.inference_ok is False
    assert outcome.error_class in ("unreachable", "timeout")


def test_l2_missing_usage_still_counts_as_inference(stub):
    stub.chat_mode = "no_usage"
    outcome = _probe(stub, level="L2")
    assert outcome.inference_ok is True
    assert outcome.usage_present is False
    assert outcome.token_usage == {}


def test_l2_bad_json_is_flagged(stub):
    stub.chat_mode = "bad_json"
    outcome = _probe(stub, level="L2")
    assert outcome.reachable is True
    assert outcome.inference_ok is False
    assert outcome.error_class == "bad_json"


def test_l2_without_finish_reason_fails(stub):
    stub.chat_mode = "no_finish"
    outcome = _probe(stub, level="L2")
    assert outcome.inference_ok is False
    assert outcome.error_class == "inference_failed"


def test_l2_error_envelope_is_not_inference(stub):
    stub.chat_mode = "error_envelope"
    outcome = _probe(stub, level="L2")
    assert outcome.inference_ok is False
    assert outcome.error_class == "inference_failed"


# ─────────────────────────────────────────────────────────── L3（流式）
def test_l3_stream_ok_takes_usage_from_last_chunk(stub):
    outcome = _probe(stub, level="L3")
    assert outcome.level == "L3"
    assert outcome.stream_ok is True
    assert outcome.inference_ok is True
    assert outcome.usage_present is True
    assert outcome.token_usage["total_tokens"] == 2
    assert stub.chat_requests()[-1]["body"]["stream"] is True


def test_l3_stream_without_usage(stub):
    stub.stream_mode = "sse_no_usage"
    outcome = _probe(stub, level="L3")
    assert outcome.stream_ok is True
    assert outcome.usage_present is False
    assert outcome.error_class is None


def test_l3_non_sse_upstream_fails_then_falls_back_to_l2(stub):
    stub.stream_mode = "json"                      # 不支持 SSE 的端点
    outcomes = probe_models(stub.upstream_base_url, ["probe-model"], level="L3",
                            timeout=2.0)
    outcome = outcomes["probe-model"]
    assert outcome.inference_ok is True            # L2 兜底成功
    assert outcome.stream_ok is False              # 但流式确实不可用
    assert outcome.usage_present is True
    # 一次 L3 + 一次 L2 兜底
    assert len(stub.chat_requests()) == 2
    assert stub.chat_requests()[0]["body"]["stream"] is True
    assert stub.chat_requests()[-1]["body"]["stream"] is False


def test_l3_broken_stream_is_stream_failed(stub):
    stub.stream_mode = "bad_chunk"
    outcome = probe_model(stub.upstream_base_url, "probe-model", level="L3",
                          timeout=2.0, http=None)
    assert outcome.stream_ok is False
    assert outcome.inference_ok is False
    assert outcome.error_class == "stream_failed"


# ─────────────────────────────────────────────────────────── 合成请求 / 隐私
def test_probe_request_is_synthetic_and_minimal(stub):
    _probe(stub, level="L2")
    body = stub.chat_requests()[-1]["body"]
    assert body["model"] == "probe-model"
    assert body["max_tokens"] == 1
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": facts.PROBE_PROMPT}]
    # 合成 prompt：不含任何用户正文占位
    assert "prompt" not in facts.PROBE_PROMPT.lower()
    assert chat_body("m", stream=True)["stream"] is True


def test_credential_never_leaves_authorization_header(stub):
    secret = "sk-test-probe-credential-value"
    outcome = _probe(stub, level="L2", credential=secret)
    request = stub.chat_requests()[-1]
    assert request["headers"]["Authorization"] == "Bearer " + secret
    assert secret not in str(request["body"])
    assert secret not in str(outcome.to_dict())
    assert secret not in str(outcome.error_class)


def test_outcome_dict_is_local_only_detail():
    outcome = ProbeOutcome(level="L3", reachable=True, auth_ok=True, inference_ok=True,
                           stream_ok=True, usage_present=True, latency_ms=3,
                           token_usage={"total_tokens": 2})
    assert set(outcome.to_dict()) == {"level", "reachable", "auth_ok", "inference_ok",
                                      "stream_ok", "usage_present", "latency_ms",
                                      "error_class", "token_usage"}


# ─────────────────────────────────────────────────────────── 解析 / 调度纯函数
def test_usage_parsing_is_tolerant():
    assert usage_from_payload({"usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                         "total_tokens": 3}})
    assert usage_from_payload({"usage": {"prompt_tokens": True}}) == {}
    assert usage_from_payload({"usage": {"prompt_tokens": -1}}) == {}
    assert usage_from_payload({"usage": "nope"}) == {}
    assert usage_from_payload(None) == {}
    assert usage_from_sse_line(b'data: {"usage": {"total_tokens": 4}}') == {
        "total_tokens": 4}
    assert usage_from_sse_line(b"data: [DONE]") == {}
    assert usage_from_sse_line(b"data: {oops") == {}
    assert usage_from_sse_line(b": keep-alive") == {}


def test_normalize_level_defaults_to_l2():
    assert normalize_level("L3") == "L3"
    assert normalize_level("l3") == "L3"
    assert normalize_level("L2") == "L2"
    assert normalize_level("L9") == "L2"
    assert normalize_level(None) == "L2"


def test_probe_models_stops_at_deadline(clock):
    stub = UpstreamStub().start()
    try:
        outcomes = probe_models(stub.upstream_base_url, ["probe-model"], level="L2",
                                timeout=2.0, deadline=clock(), clock=clock)
        assert outcomes == {}
        assert stub.chat_requests() == []          # 到点即停，不发请求
    finally:
        stub.stop()


def test_periodic_interval_shape_before_cap(monkeypatch):
    """退避曲线的**形状**没变（模型数分档 + 失败翻倍 + 6 h 上限），只是外面多了钳制。

    把钳制上限临时放到 6 h，验的仍然是修前的公式；钳制本身由下一条用例钉。
    """
    monkeypatch.setattr(facts, "PROBE_PERIODIC_CAP_S", facts.PROBE_PERIODIC_MAX_S)
    assert probe_mod.periodic_interval_s(0, 0) == facts.PROBE_PERIODIC_DEFAULT_S
    assert probe_mod.periodic_interval_s(1, 0) == facts.PROBE_PERIODIC_DEFAULT_S
    # 模型越多越稀疏（每 8 个一档）
    assert probe_mod.periodic_interval_s(8, 0) == 2 * facts.PROBE_PERIODIC_DEFAULT_S
    assert probe_mod.periodic_interval_s(20, 0) == 3 * facts.PROBE_PERIODIC_DEFAULT_S
    # 失败指数退避
    assert probe_mod.periodic_interval_s(1, 1) == 2 * facts.PROBE_PERIODIC_DEFAULT_S
    assert probe_mod.periodic_interval_s(1, 2) == 4 * facts.PROBE_PERIODIC_DEFAULT_S
    # 上限截断
    assert probe_mod.periodic_interval_s(100, 9) == facts.PROBE_PERIODIC_MAX_S
    assert probe_mod.periodic_interval_s(-3, -1) == facts.PROBE_PERIODIC_DEFAULT_S


def test_periodic_interval_never_crosses_half_fresh_window():
    """P6 / E2E-12 不变式：任何组合都不得把下一轮推到新鲜度窗口的一半之外。

    修前实测：一次失败 → 1200 s > 窗口 900 s ⇒ 每周期固定掉 5 分钟容量；
    两次失败 → 2400 s ⇒ 永久 0 容量，而客户端与云端都不报错（E2E-12）。
    """
    cap = facts.PROBE_FRESH_S // 2
    assert facts.PROBE_PERIODIC_CAP_S == cap
    combos = ((0, 0), (1, 0), (8, 0), (20, 0), (1, 1), (1, 2), (100, 9), (-3, -1))
    for models, failures in combos:
        interval = probe_mod.periodic_interval_s(models, failures)
        assert 60 <= interval <= cap, (models, failures, interval)
    # 基础周期(600 s)本身就比 half-fresh(450 s) 长 ⇒ 默认配置下被钳到 450
    assert probe_mod.periodic_interval_s(0, 0) == cap
    assert probe_mod.periodic_interval_s(1, 2) == cap
    assert probe_mod.periodic_interval_s(100, 9) < facts.PROBE_PERIODIC_MAX_S


def test_probe_deadline_is_clock_injected(clock):
    """截止时间用注入时钟判定（不 sleep、不看真实时间）。"""
    stub = UpstreamStub().start()
    try:
        outcomes = probe_models(stub.upstream_base_url, ["probe-model"], level="L2",
                                timeout=2.0, deadline=clock() + timedelta(seconds=5),
                                clock=clock)
        assert outcomes["probe-model"].inference_ok is True
    finally:
        stub.stop()
