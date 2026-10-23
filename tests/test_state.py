# SPDX-License-Identifier: Apache-2.0
import pytest

from djb_node import facts
from djb_node.errors import StateTransitionError, UsageError
from djb_node.state import NEXT_STEP, Snapshot, StateMachine, reason_label, retry_at_iso


def test_initial_state_is_stopped(clock):
    machine = StateMachine(now=clock)
    assert machine.snapshot.state == "stopped"
    assert machine.snapshot.runtime_state() == "unbound"
    assert not machine.snapshot.is_accepting()


def test_lifecycle_transitions(clock):
    machine = StateMachine(now=clock)
    assert machine.start().state == "starting"
    assert machine.online().state == "online"
    assert machine.snapshot.is_accepting()
    snapshot = machine.pause("yield_paused")
    assert snapshot.state == "paused"
    assert snapshot.reason == "yield_paused"
    assert snapshot.next_step and "自动恢复" in snapshot.next_step
    assert snapshot.runtime_state() == "yield_paused"
    assert machine.resume().state == "online"
    assert machine.begin_update().state == "updating"
    assert machine.online().state == "online"
    assert machine.fail().state == "error"
    assert machine.stop().state == "stopped"


def test_illegal_transition_rejected(clock):
    machine = StateMachine(now=clock)
    with pytest.raises(StateTransitionError):
        machine.online()
    machine.start()
    with pytest.raises(StateTransitionError):
        machine.begin_update()
    machine.online()
    with pytest.raises(StateTransitionError):
        machine.start()


def test_pause_requires_registered_reason(clock):
    machine = StateMachine(now=clock)
    machine.start()
    machine.online()
    with pytest.raises(UsageError):
        machine.pause("not-a-reason")


def test_unknown_state_rejected():
    with pytest.raises(UsageError):
        Snapshot(state="running")


def test_repeat_pause_keeps_since_and_updates_detail(clock):
    machine = StateMachine(now=clock)
    machine.start()
    machine.online()
    first = machine.pause("yield_paused", detail={"source": "load"})
    clock.advance(30)
    second = machine.pause("yield_paused", detail={"source": "thermal"})
    assert second.since == first.since
    assert second.detail["source"] == "thermal"


def test_snapshot_serialization_roundtrip_and_restore(clock):
    machine = StateMachine(now=clock)
    machine.start()
    machine.online()
    machine.pause("user_paused")
    payload = machine.snapshot.to_dict()
    restored = Snapshot.from_dict(payload)
    assert restored == machine.snapshot
    other = StateMachine(now=clock)
    other.restore(restored)
    assert other.snapshot.state == "paused"
    assert Snapshot.from_dict({"state": "weird"}).state == "stopped"
    assert Snapshot.from_dict(None).state == "stopped"


def test_runtime_state_mapping_covers_pause_reasons(clock):
    machine = StateMachine(now=clock)
    machine.start()
    machine.online()
    for reason in facts.PAUSE_REASONS:
        machine.resume() if machine.snapshot.state == "paused" else None
        snapshot = machine.pause(reason)
        assert snapshot.runtime_state() in (
            "unbound", "online", "quota_exhausted", "yield_paused", "user_paused", "error")
        assert snapshot.next_step
        machine.resume()


def test_describe_and_helpers(clock):
    machine = StateMachine(now=clock)
    machine.start()
    machine.online()
    described = machine.describe()
    assert described["accepting"] is True
    assert described["runtime_state"] == "online"
    machine.pause("quota_exhausted")
    described = machine.describe()
    assert described["accepting"] is False
    assert described["next_step"] == NEXT_STEP["quota_exhausted"]
    assert reason_label("yield_paused") == "让路（用户在用）"
    assert reason_label(None) == "-"
    assert retry_at_iso(60, clock()).endswith("Z")
