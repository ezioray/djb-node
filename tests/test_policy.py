# SPDX-License-Identifier: Apache-2.0
import pytest

from djb_node import policy
from djb_node.errors import PolicyError


def _payload(**fields):
    payload = {
        "policy_version": "p-1",
        "protocol_version": "1.0.0",
        "issued_at": "2026-10-24T00:00:00Z",
        "digest": "c" * 64,
        "quotas": [{"name": "daily", "window_seconds": 86400, "value": 100},
                   {"name": "bad", "window_seconds": 0, "value": 1},
                   "junk"],
        "directives": ["pause_accepting", 7, "unknown_directive"],
        "settings": {"policy_ttl_s": 120},
    }
    payload.update(fields)
    return payload


def test_parse_policy_tolerates_unknown_and_filters_bad(clock):
    record = policy.parse_policy(_payload(), now=clock())
    assert record.policy_version == "p-1"
    assert record.verify_state == "digest_only"
    assert record.quotas == [{"name": "daily", "window_seconds": 86400, "value": 100}]
    assert record.directives == ["pause_accepting", "unknown_directive"]
    assert record.expires_at.startswith("2026-10-24T00:02:00")


def test_parse_policy_rejects_missing_or_bad_fields(clock):
    for broken in (None, {}, _payload(policy_version=""), _payload(digest="xyz"),
                   _payload(issued_at="nope"), _payload(protocol_version="2.0.0"),
                   _payload(issued_at="2030-01-01T00:00:00Z")):
        with pytest.raises(PolicyError):
            policy.parse_policy(broken, now=clock())


def test_verify_expired_and_future(clock):
    record = policy.parse_policy(_payload(), now=clock())
    ok, reason = policy.verify(record, now=clock())
    assert ok and reason is None
    later = clock.current.replace(year=2026, month=10, day=24, hour=2)
    ok, reason = policy.verify(record, now=later)
    assert not ok and reason == "expired"
    record.verify_state = "unverified"
    ok, reason = policy.verify(record, now=clock())
    assert not ok and reason == "unverified"
    record.verify_state = "digest_only"
    record.digest = "short"
    ok, reason = policy.verify(record, now=clock())
    assert not ok and reason == "bad_digest"


def test_parse_policy_uses_default_ttl_when_missing(clock):
    payload = _payload(settings={})
    record = policy.parse_policy(payload, now=clock())
    assert record.expires_at.startswith("2026-10-24T01:00:00")


def test_directives_pause_and_remaining_and_summary(clock):
    record = policy.parse_policy(_payload(), now=clock())
    paused, reason = policy.directives_pause(record)
    assert paused
    assert "暂停接单" in reason
    assert policy.remaining_seconds(record, now=clock()) > 0
    summary = policy.summary(record, now=clock())
    assert summary["usable"] is True
    assert summary["policy_version"] == "p-1"
    assert policy.summary(None)["present"] is False
