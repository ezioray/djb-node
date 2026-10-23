# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timedelta, timezone

import pytest

from djb_node import crypto, facts
from djb_node.errors import ReceiptError
from djb_node.outbox import (ReceiptFactory, SealedReceiptBuffer, canonical_string,
                             provider_signature_valid, sign_receipt,
                             validate_receipt, well_formed_signature)

PROVIDER = b"p" * 32
CONSUMER = b"c" * 32


def _fields():
    return {"receipt_id": "r-1", "task_id": "t-1", "access": "A2",
            "usage_amount": "1000", "usage_unit": "token", "price_amount": "25"}


def test_canonical_string_order_and_raw_values():
    assert canonical_string(_fields()) == "r-1\nt-1\nA2\n1000\ntoken\n25"
    broken = dict(_fields())
    broken["usage_amount"] = 1000
    with pytest.raises(ReceiptError):
        canonical_string(broken)
    missing = dict(_fields())
    missing.pop("task_id")
    with pytest.raises(ReceiptError):
        canonical_string(missing)


def test_signature_format_and_verification():
    signatures = sign_receipt(_fields(), PROVIDER, CONSUMER)
    assert signatures["sig_provider"].startswith("hmac-sha256:")
    assert len(signatures["sig_provider"]) == len("hmac-sha256:") + 64
    assert well_formed_signature(signatures["sig_provider"])
    assert not well_formed_signature("hmac-sha256:zz")
    assert not well_formed_signature(None)


def _receipt(**overrides):
    receipt = {
        "receipt_id": "r-1", "task_id": "t-1", "access": "A2",
        "usage": {"amount": "1000", "unit": "token"},
        "price": {"amount": "25", "unit": "usd_minor"},
        "sig_provider": "", "sig_consumer": "",
        "usage_source": "client_reported",
    }
    fields = {"receipt_id": receipt["receipt_id"], "task_id": receipt["task_id"],
              "access": receipt["access"], "usage_amount": "1000",
              "usage_unit": "token", "price_amount": "25"}
    signatures = sign_receipt(fields, PROVIDER, CONSUMER)
    receipt.update(signatures)
    receipt.update(overrides)
    return receipt


def test_validate_receipt_rejects_single_signed():
    problems = validate_receipt(_receipt(sig_consumer=""))
    assert any("sig_consumer" in item for item in problems)
    problems = validate_receipt(_receipt(access="X9"))
    assert any("access" in item for item in problems)
    problems = validate_receipt(_receipt(usage={"amount": "abc", "unit": ""}))
    assert any("usage.amount" in item for item in problems)
    assert validate_receipt(_receipt()) == []


def test_factory_builds_double_signed_receipt():
    factory = ReceiptFactory(PROVIDER)
    receipt = factory.build(task_id="t-1", access="A2", usage_amount=1000,
                            price_amount=25, sig_consumer=sign_receipt(
                                _fields(), PROVIDER, CONSUMER)["sig_consumer"],
                            receipt_id="r-1")
    assert validate_receipt(receipt) == []
    assert provider_signature_valid(receipt, PROVIDER)
    assert not provider_signature_valid(receipt, CONSUMER)
    with pytest.raises(ReceiptError):
        factory.build(task_id="t-1", access="A2", usage_amount=1, price_amount=1,
                      sig_consumer="")


def test_sealed_buffer_roundtrip_and_ack(store, clock):
    buffer = SealedReceiptBuffer(store, crypto.new_key(), now=clock)
    receipt = _receipt()
    assert buffer.enqueue(receipt, provider_key=PROVIDER) is True
    assert buffer.count() == 1
    pending = buffer.pending(now=clock())
    assert pending[0]["receipt_id"] == "r-1"
    assert buffer.get("r-1") == receipt
    assert buffer.ack(["r-1"]) == 1
    assert buffer.count() == 0


def test_sealed_buffer_rejects_bad_and_tampered(store, clock):
    key = crypto.new_key()
    buffer = SealedReceiptBuffer(store, key, now=clock)
    with pytest.raises(ReceiptError):
        buffer.enqueue(_receipt(sig_consumer=""))
    with pytest.raises(ReceiptError):
        buffer.enqueue(_receipt(), provider_key=CONSUMER)
    store.add_receipt("bad", "s1:deadbeef:00:00", buffered_at=clock().isoformat())
    assert buffer.pending(now=clock()) == []
    assert store.receipt_count() == 0


def test_buffer_24h_rule(store, clock):
    key = crypto.new_key()
    buffer = SealedReceiptBuffer(store, key, now=clock)
    buffer.enqueue(_receipt())
    assert buffer.age_of_oldest_s(now=clock()) == 0
    assert not buffer.must_stop_accepting(now=clock())
    later = clock.current + timedelta(seconds=facts.RECEIPT_BUFFER_MAX_AGE_S + 1)
    assert buffer.must_stop_accepting(now=later)
    marked = buffer.mark_stale_expired(now=later)
    assert marked == ["r-1"]
    # Lead 裁决 CR-2：超 24 h 不丢弃，标 stale 且仍可重推
    assert buffer.count() == 1
    assert buffer.stale_count() == 1
    assert buffer.must_stop_accepting(now=later)
    assert buffer.mark_stale_expired(now=later) == []
    assert len(buffer.pending(now=later)) == 1
