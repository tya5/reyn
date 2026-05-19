"""Tier 2: ChannelState + DeliveryResult + RetryPolicy data model
(issue #269 outbound notification ack).

Pins the shared vocabulary used by ``reyn.web.notifications`` (=
HTTP webhook) + channel-specific senders (= ``A2AInterventionBus``,
future TUI / mobile listeners) for tracking per-channel liveness
and outbound delivery confirmation.

Pins:

  1. ``DeliveryResult`` immutability + properties (= ``ok`` /
     ``should_retry``).
  2. ``RetryPolicy`` validation (= ``max_attempts >= 1``,
     ``backoff_seconds`` length must cover the gaps).
  3. ``ChannelState.record_attempt`` mutates correctly on success +
     failure (= ``last_ack_at`` advances on success only,
     ``delivery_failures`` resets on success / increments on failure,
     ``delivery_attempts_total`` always increments).
  4. ``ChannelState.is_alive`` 3-way inference: explicit ``is_open=False``,
     ``delivery_failures >= failure_threshold``, stale ``last_ack_at``
     all return False; otherwise True.
  5. JSON round-trip preserves shape (= persistence into RunRegistry
     snapshot Phase 2 follow-up).
  6. Default values (= DEFAULT_RETRY_POLICY, NO_RETRY_POLICY) are the
     expected shapes.

No mocks. Pure data-model tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reyn.chat.channel_state import (
    DEFAULT_RETRY_POLICY,
    NO_RETRY_POLICY,
    ChannelState,
    DeliveryOutcome,
    DeliveryResult,
    RetryPolicy,
)

# ── 1. DeliveryResult ─────────────────────────────────────────────────


def test_delivery_result_ok_property_reflects_success_outcome() -> None:
    """Tier 2: ``DeliveryResult.ok`` is True iff outcome is SUCCESS."""
    assert DeliveryResult(outcome=DeliveryOutcome.SUCCESS).ok is True
    assert DeliveryResult(outcome=DeliveryOutcome.PERMANENT_FAILURE).ok is False
    assert DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE).ok is False
    assert DeliveryResult(outcome=DeliveryOutcome.NO_TRANSPORT).ok is False


def test_delivery_result_should_retry_only_for_retryable_failure() -> None:
    """Tier 2: ``should_retry`` is True only for RETRYABLE_FAILURE."""
    assert DeliveryResult(outcome=DeliveryOutcome.SUCCESS).should_retry is False
    assert (
        DeliveryResult(outcome=DeliveryOutcome.PERMANENT_FAILURE).should_retry
        is False
    )
    assert (
        DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE).should_retry
        is True
    )
    assert DeliveryResult(outcome=DeliveryOutcome.NO_TRANSPORT).should_retry is False


def test_delivery_result_immutable() -> None:
    """Tier 2: ``DeliveryResult`` is frozen — accidental mutation raises.

    Callers can safely store / forward the result without aliasing
    concerns.
    """
    result = DeliveryResult(outcome=DeliveryOutcome.SUCCESS, status_code=200)
    with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
        result.status_code = 500  # type: ignore[misc]


# ── 2. RetryPolicy validation ─────────────────────────────────────────


def test_retry_policy_default_is_three_attempts_with_backoff() -> None:
    """Tier 2: ``DEFAULT_RETRY_POLICY`` is the conservative shape
    (= 3 attempts, 0.5s + 2s backoff).
    """
    assert DEFAULT_RETRY_POLICY.max_attempts == 3
    assert DEFAULT_RETRY_POLICY.backoff_seconds == (0.5, 2.0)


def test_no_retry_policy_is_one_attempt() -> None:
    """Tier 2: ``NO_RETRY_POLICY`` is fire-and-forget shape (= 1 attempt).

    Pre-#269 callers that want the old "one attempt only" semantics
    pass this explicitly.
    """
    assert NO_RETRY_POLICY.max_attempts == 1
    assert NO_RETRY_POLICY.backoff_seconds == ()


def test_retry_policy_rejects_zero_or_negative_max_attempts() -> None:
    """Tier 2: ``max_attempts < 1`` is a programming error — raises
    ValueError so the caller learns at construction time, not at fire
    time.
    """
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        RetryPolicy(max_attempts=-1)


def test_retry_policy_rejects_insufficient_backoff_length() -> None:
    """Tier 2: ``backoff_seconds`` must have at least ``max_attempts - 1``
    entries so the policy can sleep between every retry pair.
    """
    with pytest.raises(ValueError, match="backoff_seconds must have at least"):
        RetryPolicy(max_attempts=3, backoff_seconds=(0.5,))  # only 1 entry for 2 gaps


def test_retry_policy_accepts_extra_backoff_entries() -> None:
    """Tier 2: extra backoff entries beyond ``max_attempts - 1`` are
    ignored (= harmless), not rejected.
    """
    # 3 attempts = 2 gaps; supplying 4 is fine.
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(0.1, 0.2, 0.3, 0.4))
    assert policy.max_attempts == 3


# ── 3. ChannelState.record_attempt ───────────────────────────────────


def test_channel_state_record_attempt_success_resets_failures() -> None:
    """Tier 2: a successful attempt resets ``delivery_failures`` to 0
    and advances ``last_ack_at`` to the attempt timestamp.
    """
    state = ChannelState(channel_id="a2a:test", delivery_failures=2)
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    result = DeliveryResult(
        outcome=DeliveryOutcome.SUCCESS,
        status_code=200,
        attempted_at=ts,
    )
    state.record_attempt(result)
    assert state.delivery_failures == 0
    assert state.last_ack_at == ts
    assert state.delivery_attempts_total == 1


def test_channel_state_record_attempt_failure_increments_failures() -> None:
    """Tier 2: a failed attempt increments ``delivery_failures`` and
    does NOT advance ``last_ack_at``.
    """
    state = ChannelState(channel_id="a2a:test", delivery_failures=1)
    ts_before = state.last_ack_at  # None
    result = DeliveryResult(
        outcome=DeliveryOutcome.RETRYABLE_FAILURE,
        status_code=503,
    )
    state.record_attempt(result)
    assert state.delivery_failures == 2
    assert state.last_ack_at == ts_before
    assert state.delivery_attempts_total == 1


def test_channel_state_attempts_total_always_increments() -> None:
    """Tier 2: ``delivery_attempts_total`` increments on every
    record_attempt call, regardless of outcome.
    """
    state = ChannelState(channel_id="a2a:test")
    state.record_attempt(DeliveryResult(outcome=DeliveryOutcome.SUCCESS))
    state.record_attempt(DeliveryResult(outcome=DeliveryOutcome.RETRYABLE_FAILURE))
    state.record_attempt(DeliveryResult(outcome=DeliveryOutcome.PERMANENT_FAILURE))
    state.record_attempt(DeliveryResult(outcome=DeliveryOutcome.NO_TRANSPORT))
    assert state.delivery_attempts_total == 4


# ── 4. ChannelState.is_alive ─────────────────────────────────────────


def test_channel_state_is_alive_true_when_open_and_no_failures() -> None:
    """Tier 2: default state (= open, no failures, no acks yet) is alive."""
    state = ChannelState(channel_id="a2a:test")
    assert state.is_alive() is True


def test_channel_state_is_alive_false_when_explicitly_closed() -> None:
    """Tier 2: ``is_open=False`` overrides everything else."""
    state = ChannelState(channel_id="a2a:test", is_open=False)
    assert state.is_alive() is False


def test_channel_state_is_alive_false_when_failures_at_threshold() -> None:
    """Tier 2: sustained failure (= ``delivery_failures >= failure_threshold``)
    marks the channel dead."""
    state = ChannelState(
        channel_id="a2a:test",
        delivery_failures=3,
        failure_threshold=3,
    )
    assert state.is_alive() is False


def test_channel_state_is_alive_false_when_last_ack_is_stale() -> None:
    """Tier 2: ``now() - last_ack_at > stale_after`` marks the channel dead.

    Uses an explicit ``now`` arg so the test is deterministic.
    """
    old_ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    later = old_ts + timedelta(minutes=10)  # > default 5 min stale_after
    state = ChannelState(channel_id="a2a:test", last_ack_at=old_ts)
    assert state.is_alive(now=later) is False


def test_channel_state_is_alive_true_when_last_ack_is_recent() -> None:
    """Tier 2: recent ack within ``stale_after`` keeps the channel alive."""
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    slightly_later = ts + timedelta(seconds=30)
    state = ChannelState(channel_id="a2a:test", last_ack_at=ts)
    assert state.is_alive(now=slightly_later) is True


# ── 5. JSON round-trip ──────────────────────────────────────────────


def test_channel_state_to_dict_from_dict_round_trip() -> None:
    """Tier 2: ``to_dict`` + ``from_dict`` preserves shape verbatim.

    Used by RunRegistry Phase 2 follow-up to persist channel state
    alongside RunEntry; this round-trip pin guards the contract.
    """
    ts = datetime(2026, 5, 20, 12, 30, 0, tzinfo=timezone.utc)
    state = ChannelState(
        channel_id="a2a:run-abc123",
        is_open=True,
        last_ack_at=ts,
        delivery_failures=1,
        delivery_attempts_total=5,
        failure_threshold=5,
        stale_after=timedelta(minutes=10),
    )
    restored = ChannelState.from_dict(state.to_dict())
    assert restored.channel_id == state.channel_id
    assert restored.is_open == state.is_open
    assert restored.last_ack_at == state.last_ack_at
    assert restored.delivery_failures == state.delivery_failures
    assert restored.delivery_attempts_total == state.delivery_attempts_total
    assert restored.failure_threshold == state.failure_threshold
    assert restored.stale_after == state.stale_after


def test_channel_state_from_dict_tolerates_missing_fields() -> None:
    """Tier 2: ``from_dict`` with a minimal payload returns a usable
    state (= defaults applied for missing optional fields).
    """
    state = ChannelState.from_dict({"channel_id": "a2a:test"})
    assert state.channel_id == "a2a:test"
    assert state.is_open is True
    assert state.last_ack_at is None
    assert state.delivery_failures == 0


def test_channel_state_from_dict_tolerates_malformed_timestamp() -> None:
    """Tier 2: a malformed ``last_ack_at`` ISO string yields ``None``
    rather than crashing on restore.
    """
    state = ChannelState.from_dict({
        "channel_id": "a2a:test",
        "last_ack_at": "not a real timestamp",
    })
    assert state.last_ack_at is None
