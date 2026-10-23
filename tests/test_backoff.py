# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timezone

from djb_node import facts
from djb_node.backoff import Backoff


def test_delay_schedule_and_exhaustion(clock):
    backoff = Backoff(now=clock)
    assert backoff.delay_for(1) == 1
    assert backoff.delay_for(2) == 5
    assert backoff.delay_for(3) == 30
    assert backoff.delay_for(4) == 300
    assert backoff.delay_for(5) == 1800
    assert backoff.delay_for(8) == 1800
    decision = backoff.after_failure(1)
    assert decision.delay_s == 1
    assert decision.exhausted is False
    assert decision.next_at
    last = backoff.after_failure(facts.RETRY_MAX_ATTEMPTS)
    assert last.exhausted is True
    assert last.delay_s is None
    assert last.next_at is None


def test_max_attempts_and_jitter_bounds():
    backoff = Backoff(jitter=0.0, max_attempts=3)
    assert backoff.exhausted(3)
    assert not backoff.exhausted(2)
    jittered = Backoff(jitter=0.5, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert jittered.after_failure(5).delay_s is not None
    assert jittered.describe()["max_attempts"] == facts.RETRY_MAX_ATTEMPTS
