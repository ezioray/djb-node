# SPDX-License-Identifier: Apache-2.0
"""C12 模型身份证明（M17 §5.1–5.3 / §5.7 / §8.1）客户端侧用例。

全部离线：不调服务端、不联网；tokenizer 用注入的确定性计数器模拟「同 / 不同 tokenizer」。
重点覆盖：等级逐级推进与只升不降、失败降级、TTL 新鲜度、容差边界、不同 tokenizer 必检出、
nonce 重放拒绝、证据不含正文、开关默认关闭、客户端证据不自我宣称 verified。
"""
from __future__ import annotations

import sys
import types

import pytest

from djb_node import facts
from djb_node.attest import (AttestationManager, AttestError, Evidence, NonceLedger,
                             ReplayError, advance, assert_no_body, attest_key,
                             attestation_enabled, build_evidence, default_tokenizer_counter,
                             delete_record, fail, freshness, level_index, list_records,
                             load_record, new_record, reopen, save_record, service_gate,
                             tolerance_tokens, tokenizer_family, tokenizer_match, ttl_for)
from djb_node.config import ClientConfig
from djb_node.errors import ConflictError, FeatureDisabled
from djb_node.util import iso

PROMPT = " ".join("token" + str(index) for index in range(40))   # 40 个空白分隔 token


def make_config(**flags: bool) -> ClientConfig:
    config = ClientConfig()
    config.extra["feature_flags"] = dict(flags)
    return config


def whitespace_counter(model: str, text: str) -> int:
    """模拟 A tokenizer：按空白切分（40 个词的 prompt → 40）。"""
    return len([part for part in text.split() if part])


def bigram_counter(model: str, text: str) -> int:
    """模拟 B tokenizer：字符二元组（同一 prompt 的计数远大于 A）。"""
    compact = "".join(text.split())
    return max(1, (len(compact) + 1) // 2)


def fixed_counter(value: int):
    return lambda model, text: value


def make_evidence(**overrides) -> Evidence:
    values = dict(challenge_id="ch-0001", model="gpt-4o", method="l3_tokenizer",
                  level="L3", nonce="nonce-000001", result="match", tokens_in=40,
                  tokens_out=3, usage={"prompt_tokens": 40}, response_hash="a" * 64,
                  signature="hmac-sha256:" + "b" * 64, confidence=0.9)
    values.update(overrides)
    return build_evidence(**values)


# ─────────────────────────────────────────────────────────── 等级 / TTL
def test_level_table_and_ttl_per_spec():
    assert facts.ATTEST_LEVELS == ("L0", "L1", "L2", "L3", "L4", "L5")
    assert facts.ATTEST_LEVEL_NAMES["L3"] == "tokenizer"
    assert ttl_for("L1") == 24 * 3600
    assert ttl_for("L2") == 24 * 3600
    assert ttl_for("L3") == 7 * 86400
    assert ttl_for("L4") == 30 * 86400
    assert ttl_for("L5") == 90 * 86400
    assert ttl_for("L3", {"L3": 60}) == 60
    assert ttl_for("L5", {"L3": 60}) == 90 * 86400
    assert level_index("L0") == 0 and level_index("L5") == 5


def test_advance_is_stepwise_and_sets_state_and_expiry(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    assert record.level == "L0" and record.state == "declared"
    advance(record, "L1", now=clock())
    assert record.state == "reachable"
    assert record.expires_at == iso(clock().replace(day=25))
    advance(record, "L2", now=clock())
    advance(record, "L3", now=clock())
    assert record.level == "L3" and record.state == "tokenizer"
    assert record.best_level == "L3"
    assert record.confidence == 0.0 or record.confidence <= 1.0


def test_advance_rejects_downgrade_and_skipping(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    advance(record, "L1", now=clock())
    advance(record, "L2", now=clock())
    with pytest.raises(ConflictError):
        advance(record, "L1", now=clock())
    assert record.level == "L2"                             # 拒绝后状态不变
    with pytest.raises(ConflictError):
        advance(record, "L4", now=clock())
    assert record.level == "L2"


def test_advance_blocked_when_suspended(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    fail(record, reason_code="client_tamper", state="suspended", now=clock())
    with pytest.raises(ConflictError):
        advance(record, "L1", now=clock())


def test_fail_downgrades_l3_to_l2_and_keeps_best_level(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    for level in ("L1", "L2", "L3"):
        advance(record, level, now=clock())
    fail(record, reason_code="tokenizer_mismatch", downgrade_to="L2", now=clock())
    assert record.level == "L2" and record.state == "failed"
    assert record.best_level == "L3"
    assert service_gate(record, now=clock())["serviceable"] is False


def test_fail_rejects_invalid_state_and_upward_downgrade(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    advance(record, "L1", now=clock())
    advance(record, "L2", now=clock())
    with pytest.raises(AttestError):
        fail(record, reason_code="x", state="declared", now=clock())
    with pytest.raises(ConflictError):
        fail(record, reason_code="x", downgrade_to="L3", now=clock())


def test_ttl_expiry_blocks_gate_then_reopen(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    for level in ("L1", "L2", "L3"):
        advance(record, level, now=clock())
    assert service_gate(record, now=clock())["serviceable"] is True
    clock.advance(7 * 86400 + 60)
    gate = service_gate(record, now=clock())
    assert gate["serviceable"] is False and gate["reason"] == "state_expired"
    assert record.state == "expired"
    reopen(record, now=clock())
    assert record.level == "L0" and record.state == "declared"
    assert record.best_level == "L3"
    assert freshness(record, now=clock())["fresh"] is False


def test_l1_ttl_boundary_and_freshness(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    advance(record, "L1", now=clock())
    clock.advance(24 * 3600 - 1)
    info = freshness(record, now=clock())
    assert info["fresh"] is True and info["expired"] is False
    assert info["seconds_left"] == 1
    assert service_gate(record, now=clock())["serviceable"] is False   # L1 < L3
    clock.advance(2)                                                   # 越过 24h
    assert freshness(record, now=clock())["expired"] is True


def test_service_gate_requires_l3(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    advance(record, "L1", now=clock())
    advance(record, "L2", now=clock())
    gate = service_gate(record, now=clock())
    assert gate["serviceable"] is False and gate["reason"] == "level_below_L3"
    advance(record, "L3", now=clock())
    assert service_gate(record, now=clock())["serviceable"] is True


# ─────────────────────────────────────────────────────────── tokenizer
def test_tolerance_table_and_boundaries():
    assert tolerance_tokens(1000) == 10
    assert tolerance_tokens(150) == 2
    assert tolerance_tokens(50) == 1
    assert tolerance_tokens(1) == 1
    assert tolerance_tokens(1000, pct=2.0) == 20
    assert tolerance_tokens(1, tokens=3) == 3


def test_same_tokenizer_matches_and_tolerance_boundary():
    assert tokenizer_match(PROMPT, "gpt-4o", 40, counter=whitespace_counter).result == "match"
    at_edge = tokenizer_match(PROMPT, "gpt-4o", 41, counter=whitespace_counter)
    assert at_edge.result == "match" and at_edge.diff == 1 and at_edge.tolerance == 1
    over_edge = tokenizer_match(PROMPT, "gpt-4o", 42, counter=whitespace_counter)
    assert over_edge.result == "mismatch" and over_edge.diff == 2


def test_tolerance_boundary_large_count():
    counter = fixed_counter(1000)
    assert tokenizer_match(PROMPT, "gpt-4o", 1010, counter=counter).result == "match"
    assert tokenizer_match(PROMPT, "gpt-4o", 990, counter=counter).result == "match"
    over = tokenizer_match(PROMPT, "gpt-4o", 1011, counter=counter)
    assert over.result == "mismatch" and over.tolerance == 10


@pytest.mark.parametrize("usage", [None, 0, -5, "not-a-number"])
def test_missing_usage_is_unknown(usage):
    check = tokenizer_match(PROMPT, "gpt-4o", usage, counter=whitespace_counter)
    assert check.result == "unknown"
    assert check.reason_code == "usage_missing"


def test_counter_unavailable_is_unknown():
    check = tokenizer_match(PROMPT, "gpt-4o", 40, counter=lambda model, text: None)
    assert check.result == "unknown" and check.reason_code == "tokenizer_unavailable"
    check = tokenizer_match(PROMPT, "gpt-4o", 40, counter=lambda model, text: 1 / 0)
    assert check.result == "unknown"


def test_different_tokenizer_always_detected():
    """换不同 tokenizer 的低端模型：同一 prompt 计数差异远超容差 → 必 detective。"""
    claimed = whitespace_counter("gpt-4o", PROMPT)
    served = bigram_counter("gpt-4o", PROMPT)
    assert served > claimed * 2
    check = tokenizer_match(PROMPT, "gpt-4o", served, counter=whitespace_counter)
    assert check.result == "mismatch"
    assert check.expected == claimed and check.actual == served
    assert check.reason_code == "tokenizer_mismatch"
    assert check.confidence == 0.0


def test_tokenizer_family_hints():
    assert tokenizer_family("gpt-4o-mini") == "openai:o200k"
    assert tokenizer_family("gpt-4-turbo") == "openai:cl100k"
    assert tokenizer_family("claude-3-7-sonnet") == "anthropic:claude"
    assert tokenizer_family("unknown-model-xyz") is None


def test_default_counter_uses_lazy_tokens_module(monkeypatch):
    stub = types.ModuleType("djb_node.tokens")
    stub.count_prompt_tokens = lambda model, text: len(text)
    monkeypatch.setitem(sys.modules, "djb_node.tokens", stub)
    assert default_tokenizer_counter("gpt-4o", "abcdef") == 6
    check = tokenizer_match("abcdef", "gpt-4o", 6)
    assert check.result == "match"


def test_default_counter_positional_signature_fallback(monkeypatch):
    stub = types.ModuleType("djb_node.tokens")

    def count_prompt_tokens(m, t):                          # 只接受位置参数
        return len(t.split())

    stub.count_prompt_tokens = count_prompt_tokens
    monkeypatch.setitem(sys.modules, "djb_node.tokens", stub)
    assert default_tokenizer_counter("gpt-4o", "a b c") == 3


def test_default_counter_degrades_without_tokens_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "djb_node.tokens", raising=False)
    assert default_tokenizer_counter("gpt-4o", "abc") is None
    check = tokenizer_match("abc", "gpt-4o", 1)
    assert check.result == "unknown" and check.reason_code == "tokenizer_unavailable"


# ─────────────────────────────────────────────────────────── 证据
def test_evidence_shape_and_no_body():
    evidence = make_evidence()
    payload = evidence.to_dict()
    assert_no_body(payload)
    assert payload["response_hash"] == "a" * 64
    assert "prompt" not in payload and "response" not in payload
    assert set(payload) <= {"schema", "challenge_id", "model", "method", "level", "nonce",
                            "result", "confidence", "created_at", "tokens_in",
                            "tokens_out", "usage", "response_hash", "signature",
                            "reason_code"}


@pytest.mark.parametrize("overrides", [
    {"response_hash": "not-a-hash"},
    {"signature": "x" * 600},
    {"challenge_id": "line1\nline2"},
    {"nonce": "bad"},
    {"result": "definitely"},
    {"model": "bad model!"},
    {"tokens_in": -1},
    {"usage": {"prompt_tokens": -2}},
])
def test_evidence_rejects_bad_or_body_like_fields(overrides):
    with pytest.raises(AttestError):
        make_evidence(**overrides)


def test_assert_no_body_rejects_prompt_text():
    with pytest.raises(AttestError):
        assert_no_body({"prompt": "用户正文"})
    with pytest.raises(AttestError):
        assert_no_body({"note": "line1\nline2"})


def test_evidence_roundtrip():
    evidence = make_evidence()
    clone = Evidence.from_dict(evidence.to_dict())
    assert clone.to_dict() == evidence.to_dict()


# ─────────────────────────────────────────────────────────── 记录 / kv
def test_record_roundtrip_and_kv(store, clock):
    record = new_record("u1", "openai/gpt-4o", now=clock())
    advance(record, "L1", now=clock())
    save_record(store, record)
    assert store.get_kv(attest_key("u1", "openai/gpt-4o"))["level"] == "L1"
    loaded = load_record(store, "u1", "openai/gpt-4o")
    assert loaded is not None and loaded.to_dict() == record.to_dict()
    assert [item.model for item in list_records(store)] == ["openai/gpt-4o"]
    assert delete_record(store, "u1", "openai/gpt-4o") is True
    assert list_records(store) == []


def test_describe_never_claims_verified(clock):
    record = new_record("u1", "gpt-4o", now=clock())
    for level in ("L1", "L2", "L3"):
        advance(record, level, now=clock())
    payload = record.describe(now=clock())
    assert payload["verified"] is False and payload["server_verified"] is False
    assert payload["trust"] == facts.ATTEST_TRUST
    assert payload["level_label"] == "tokenizer"
    assert "服务端" in payload["note"]


# ─────────────────────────────────────────────────────────── nonce / 重放
def test_nonce_replay_rejected(store, clock):
    ledger = NonceLedger(store, now=clock)
    ledger.consume("nonce-aaaa1111", now=clock())
    assert ledger.seen("nonce-aaaa1111", now=clock()) is True
    with pytest.raises(ReplayError):
        ledger.consume("nonce-aaaa1111", now=clock())


def test_same_challenge_with_new_nonce_rejected(store, clock):
    ledger = NonceLedger(store, now=clock)
    ledger.consume("nonce-aaaa1111", challenge_id="ch-1", now=clock())
    with pytest.raises(ReplayError):
        ledger.consume("nonce-bbbb2222", challenge_id="ch-1", now=clock())


def test_nonce_expiry_allows_reuse(store, clock):
    ledger = NonceLedger(store, now=clock, ttl_s=60)
    ledger.consume("nonce-aaaa1111", now=clock())
    clock.advance(61)
    assert ledger.seen("nonce-aaaa1111", now=clock()) is False
    ledger.consume("nonce-aaaa1111", now=clock())        # 过期后可再用（不再重放）


# ─────────────────────────────────────────────────────────── 管理器 / 开关
def test_manager_write_paths_disabled_by_default(store, clock):
    manager = AttestationManager(store, config=make_config(), now=clock)
    assert attestation_enabled(manager.config) is False
    with pytest.raises(FeatureDisabled):
        manager.open_record("u1", "gpt-4o")
    status = manager.status("u1", "gpt-4o")              # 读路径可用
    assert status["verified"] is False and status["serviceable"] is False
    assert status["reason"] == "not_attested"


def test_manager_enabled_flow_advances_to_l3(store, clock):
    manager = AttestationManager(store, config=make_config(**{facts.ATTEST_FLAG: True}),
                                 now=clock)
    assert manager.enabled() is True
    record = manager.open_record("u1", "gpt-4o")
    assert record.level == "L0"
    check = tokenizer_match(PROMPT, "gpt-4o", 40, counter=whitespace_counter)
    assert check.result == "match"
    for level in ("L1", "L2"):
        manager.apply_evidence("u1", "gpt-4o", make_evidence(
            challenge_id="ch-" + level, nonce="nonce-" + level.lower() + "-0001",
            method=facts.ATTEST_LEVEL_METHOD[level], level=level, result="ok"))
    result = manager.record_tokenizer_check(
        "u1", "gpt-4o", check, challenge_id="ch-l3", nonce="nonce-l3-000001",
        response_hash="c" * 64, tokens_out=3)
    assert result["record"]["level"] == "L3"
    assert result["record"]["serviceable"] is True
    status = manager.status("u1", "gpt-4o")
    assert status["verified"] is False and status["trust"] == facts.ATTEST_TRUST
    assert manager.list()[0]["model"] == "gpt-4o"


def test_manager_mismatch_downgrades_and_blocks(store, clock):
    manager = AttestationManager(store, config=make_config(**{facts.ATTEST_FLAG: True}),
                                 now=clock)
    manager.open_record("u1", "gpt-4o")
    for level in ("L1", "L2", "L3"):
        manager.apply_evidence("u1", "gpt-4o", make_evidence(
            challenge_id="ch-" + level, nonce="nonce-" + level.lower() + "-0001",
            method=facts.ATTEST_LEVEL_METHOD[level], level=level, result="ok"))
    check = tokenizer_match(PROMPT, "gpt-4o", bigram_counter("gpt-4o", PROMPT),
                            counter=whitespace_counter)
    assert check.result == "mismatch"
    payload = manager.record_tokenizer_check("u1", "gpt-4o", check,
                                             challenge_id="ch-bad", nonce="nonce-bad-0001")
    assert payload["record"]["level"] == "L2"
    assert payload["record"]["state"] == "failed"
    assert payload["record"]["best_level"] == "L3"
    assert payload["record"]["serviceable"] is False


def test_manager_unknown_evidence_keeps_level(store, clock):
    manager = AttestationManager(store, config=make_config(**{facts.ATTEST_FLAG: True}),
                                 now=clock)
    manager.open_record("u1", "gpt-4o")
    manager.apply_evidence("u1", "gpt-4o", make_evidence(
        challenge_id="ch-l1", nonce="nonce-l1-000001", method="l1_models", level="L1",
        result="ok"))
    manager.apply_evidence("u1", "gpt-4o", make_evidence(
        challenge_id="ch-l3-unknown", nonce="nonce-u-0000001", method="l3_tokenizer",
        level="L3", result="unknown", reason_code="usage_missing"))
    record = manager.get("u1", "gpt-4o")
    assert record is not None and record.level == "L1"
    assert len(record.evidence) == 2


def test_manager_replay_does_not_mutate_record(store, clock):
    manager = AttestationManager(store, config=make_config(**{facts.ATTEST_FLAG: True}),
                                 now=clock)
    manager.open_record("u1", "gpt-4o")
    evidence = make_evidence(challenge_id="ch-l1", nonce="nonce-l1-000001",
                             method="l1_models", level="L1", result="ok")
    manager.apply_evidence("u1", "gpt-4o", evidence)
    before = manager.get("u1", "gpt-4o").to_dict()
    with pytest.raises(ReplayError):
        manager.apply_evidence("u1", "gpt-4o", evidence)
    assert manager.get("u1", "gpt-4o").to_dict() == before


def test_report_payload_matches_spec_fields_without_body(store, clock):
    manager = AttestationManager(store, config=make_config(**{facts.ATTEST_FLAG: True}),
                                 now=clock)
    record = manager.open_record("u1", "gpt-4o")
    evidence = make_evidence()
    payload = manager.report_payload(record, evidence)
    for key in ("challenge_id", "model", "tokens_in", "tokens_out", "usage",
                "response_hash", "nonce", "signature", "attestation_id"):
        assert key in payload
    for forbidden in ("prompt", "response", "messages", "content", "body", "text"):
        assert forbidden not in payload
    assert payload["attestation_id"] == record.attestation_id
    assert payload["model"] == "gpt-4o"
    assert_no_body(payload)
