# SPDX-License-Identifier: Apache-2.0
import sqlite3

import pytest

from djb_node.errors import StoreDowngradeError, StoreNotInitializedError, UsageError
from djb_node.store import (SCHEMA_VERSION, PolicyRecord, ProfileRecord, Store,
                            UpdateRecord, bucket_for, next_window_start,
                            utc_window_start, window_key)
from djb_node.util import iso
from datetime import datetime, timedelta, timezone


def test_create_is_idempotent_and_records_install(paths):
    first = Store.create(paths.db_file)
    record = first.install()
    assert record.version
    assert 0 <= record.bucket < 100
    assert record.arch
    first.close()
    second = Store.create(paths.db_file)
    assert second.install().install_id_hash == record.install_id_hash
    second.close()


def test_missing_db_raises(paths):
    store = Store(str(paths.home) + "/nope.db")
    with pytest.raises(StoreNotInitializedError):
        store.connect()


def test_downgrade_is_rejected(paths):
    Store.create(paths.db_file).close()
    connection = sqlite3.connect(paths.db_file)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()
    with pytest.raises(StoreDowngradeError):
        Store(paths.db_file).connect()


def test_upgrade_migrates_forward(paths):
    Store.create(paths.db_file).close()
    connection = sqlite3.connect(paths.db_file)
    connection.execute("PRAGMA user_version=0")
    connection.commit()
    connection.close()
    store = Store(paths.db_file)
    store.connect()
    assert store.is_initialized()
    store.close()


def test_profile_defaults_and_save(store):
    profile = store.profile()
    assert profile.concurrent >= 1
    assert profile.daily_tokens > 0
    profile.concurrent = 4
    profile.schedule = [{"days": [0], "start": "09:00", "end": "10:00"}]
    store.save_profile(profile)
    reloaded = store.profile()
    assert reloaded.concurrent == 4
    assert reloaded.schedule[0]["start"] == "09:00"


def test_upstreams_crud_and_model_toggles(store, credentials):
    record = store.add_upstream(upstream_id="u1", access_type="A2",
                                base_url="http://127.0.0.1:11434/v1",
                                models=[{"id": "m1", "enabled": True}],
                                credential_ref="upstream:u1")
    assert record.model_ids() == ["m1"]
    assert store.upstreams()[0].id == "u1"
    store.set_model_enabled("u1", "m1", False)
    assert store.upstream("u1").models[0]["enabled"] is False
    store.set_model_price("u1", "m1", "0.35")
    assert store.upstream("u1").models[0]["min_price"] == "0.35"
    with pytest.raises(UsageError):
        store.set_model_enabled("u1", "nope", True)
    with pytest.raises(UsageError):
        store.upstream("missing")
    assert store.delete_upstream("u1") is True
    assert store.delete_upstream("u1") is False


def test_policy_save_and_clear(store):
    record = PolicyRecord(policy_version="p1", protocol_version="1.0.0",
                          digest="a" * 64, issued_at=iso(datetime.now(timezone.utc)),
                          expires_at=iso(datetime.now(timezone.utc) + timedelta(hours=1)),
                          quotas=[{"name": "daily", "window_seconds": 86400, "value": 10}],
                          directives=["pause_accepting"], settings={"policy_ttl_s": 3600},
                          verify_state="digest_only",
                          fetched_at=iso(datetime.now(timezone.utc)))
    store.save_policy(record)
    saved = store.policy()
    assert saved.policy_version == "p1"
    assert saved.directives == ["pause_accepting"]
    assert saved.is_fresh()
    assert not saved.is_expired()
    store.clear_policy()
    assert store.policy() is None


def test_announcements_idempotent_by_seq(store):
    assert store.record_announcement(1, {"kind": "token_relay", "model": "m"}) is True
    assert store.record_announcement(1, {"kind": "token_relay", "model": "m"}) is False
    assert len(store.announcements()) == 1


def test_receipts_buffer_operations(store):
    assert store.add_receipt("r1", "sealed", buffered_at="2026-10-24T00:00:00Z")
    assert store.add_receipt("r1", "sealed") is False
    assert store.receipt_count() == 1
    assert store.oldest_receipt_at() == "2026-10-24T00:00:00Z"
    store.mark_receipt_retry("r1", attempts=2,
                             next_attempt_at="2026-10-24T00:05:00Z", last_error="boom")
    rows = store.receipts()
    assert rows[0].attempts == 2
    assert store.pending_receipts(
        datetime(2026, 10, 24, 0, 1, tzinfo=timezone.utc)) == []
    assert len(store.pending_receipts(
        datetime(2026, 10, 24, 0, 6, tzinfo=timezone.utc))) == 1
    assert store.purge_receipts_older_than(
        datetime(2026, 10, 25, tzinfo=timezone.utc)) == ["r1"]
    assert store.receipt_count() == 0


def test_usage_windows(store):
    start = iso(utc_window_start(86400, datetime(2026, 10, 24, 10, tzinfo=timezone.utc)))
    key = window_key("profile:daily", 86400)
    store.add_usage(key, start, 10, 1)
    store.add_usage(key, start, 5, 1)
    assert store.usage_total(key, since=start) == 15
    assert store.usage_window(key, start) == (15, 2)
    assert store.usage_total(key, since=iso(datetime(2026, 10, 25, tzinfo=timezone.utc))) == 0


def test_update_state_and_env_state_and_kv(store):
    record = store.save_update_state(UpdateRecord(current_version="0.1.0",
                                                  bucket=7, channel="stable"))
    assert record.current_version == "0.1.0"
    assert store.update_state().bucket == 7
    store.save_env_state(detected_at="t", authorized_at="t2",
                         plan=[{"id": "concurrent", "from": 3, "to": 1}],
                         report={"summary": {"ok": 1}})
    state = store.env_state()
    assert state["authorized_at"] == "t2"
    assert state["plan"][0]["to"] == 1
    store.set_kv("daemon_state", {"state": "online"})
    assert store.get_kv("daemon_state")["state"] == "online"
    store.delete_kv("daemon_state")
    assert store.get_kv("daemon_state", default={}) == {}
    assert store.counts()["receipts"] == 0


def test_bucket_is_deterministic():
    assert bucket_for("abc") == bucket_for("abc")
    assert 0 <= bucket_for("abc") < 100


def test_window_helpers():
    moment = datetime(2026, 10, 24, 12, 34, tzinfo=timezone.utc)
    start = utc_window_start(3600, moment)
    assert start == datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    assert next_window_start(3600, moment) == datetime(2026, 10, 24, 13, 0,
                                                      tzinfo=timezone.utc)
