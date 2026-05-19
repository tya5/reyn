"""Channel state tracking for outbound notification ack (issue #269).

Pre-#269 the webhook fire path (= ``reyn.web.notifications.post_webhook``)
was fire-and-forget: no HTTP 2xx check return value, no per-channel
retry, no "is this channel still alive?" inference. That left the
caller (= ``A2AInterventionBus.deliver``, ``_handle_async_mode._run``,
future expanded webhook triggers per #267 Gap 2) blind to delivery
failures and unable to drive stall detection for #268's origin-pinned
intervention routing.

This module defines the **data model** + **inference helpers**:

  - ``DeliveryResult`` — outcome of a single send attempt
    (= success / 4xx-permanent / 5xx-or-timeout-retryable / no-httpx)
  - ``RetryPolicy`` — declarative retry config (= max attempts +
    backoff schedule)
  - ``ChannelState`` — per-channel running state (= last ack timestamp,
    consecutive failure count, total attempt count, "is open"
    explicit register flag)

Concrete senders live in ``reyn.web.notifications`` (= HTTP webhook)
and channel-specific surfaces (= ``ChatSession`` listener for TUI,
``A2AInterventionBus.deliver`` for A2A peer). This module is the
shared vocabulary.

issue #269 — A2A spec range で組む (= HTTP 2xx + retry policy is
standard webhook convention、 custom protocol なし).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class DeliveryOutcome(str, Enum):
    """Categorisation of a single send attempt.

    The ``str``-Enum subclassing keeps the value JSON-serialisable so
    audit / debug payloads can carry the outcome without extra mapping.
    """

    SUCCESS = "success"
    """HTTP 2xx response received."""

    PERMANENT_FAILURE = "permanent_failure"
    """HTTP 4xx response — peer rejected the payload, retry won't help.
    Examples: 401 (auth bad), 404 (URL gone), 410 (resource removed)."""

    RETRYABLE_FAILURE = "retryable_failure"
    """HTTP 5xx, timeout, or transport error — peer might recover,
    retry policy may attempt again."""

    NO_TRANSPORT = "no_transport"
    """The optional ``httpx`` extra is not installed (= webhook can't
    be sent at all). Logged-and-degraded path; not a peer fault."""


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one ``post_webhook`` invocation (or equivalent
    channel-specific delivery attempt).

    Immutable so callers can store / forward without aliasing concerns.
    """

    outcome: DeliveryOutcome
    """The categorisation."""

    status_code: int | None = None
    """HTTP response code when applicable (= 2xx success or 4xx/5xx
    failure). ``None`` for transport errors or NO_TRANSPORT."""

    error: str | None = None
    """String repr of the exception when the attempt raised. ``None``
    on clean responses (including non-2xx HTTP)."""

    attempted_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    """Wall clock at attempt time, UTC."""

    @property
    def ok(self) -> bool:
        """True iff the attempt was delivered successfully."""
        return self.outcome is DeliveryOutcome.SUCCESS

    @property
    def should_retry(self) -> bool:
        """True iff the outcome is retryable per the RetryPolicy."""
        return self.outcome is DeliveryOutcome.RETRYABLE_FAILURE


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative retry config for outbound delivery attempts.

    The defaults are conservative — 3 attempts total, 0.5s / 2s
    backoff — so a transient 5xx / timeout doesn't kill a webhook
    notification, but a sustained outage doesn't block the caller
    for long.

    Set ``max_attempts=1`` for fire-and-forget callers that prefer
    pre-#269 semantics.
    """

    max_attempts: int = 3
    """Total attempts including the first try. ``1`` = no retry."""

    backoff_seconds: tuple[float, ...] = (0.5, 2.0)
    """Wait times between attempts. ``len(backoff_seconds)`` must be
    at least ``max_attempts - 1``; extras are ignored."""

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1, got {self.max_attempts!r}",
            )
        if len(self.backoff_seconds) < self.max_attempts - 1:
            raise ValueError(
                f"backoff_seconds must have at least max_attempts-1 entries "
                f"({self.max_attempts - 1}), got {len(self.backoff_seconds)}",
            )


# Module-level default — used by callers that don't specify per-call policy.
DEFAULT_RETRY_POLICY = RetryPolicy()

# Module-level no-retry — pre-#269 fire-and-forget compat for callers
# that want one attempt only.
NO_RETRY_POLICY = RetryPolicy(max_attempts=1, backoff_seconds=())


@dataclass
class ChannelState:
    """Per-channel running state for liveness inference (issue #269).

    Mutable; the owner (= ``RunRegistry`` per RunEntry for A2A peers,
    or ``ChatSession`` per attached listener for TUI / future
    channels) updates it on each delivery attempt.

    ``is_alive`` reads three signals together:
      1. explicit ``is_open`` flag (= owner-managed registration state)
      2. ``delivery_failures`` count vs ``failure_threshold``
      3. ``last_ack_at`` recency vs ``stale_after``

    Any of (2) or (3) tripping while ``is_open=True`` marks the channel
    dead (= caller treats it as closed for stall / redirect routing).
    """

    channel_id: str
    """Stable identifier (= ``"tui:<session>"`` / ``"a2a:<run_id>"`` /
    etc.). Used as the dictionary key in owning registries."""

    is_open: bool = True
    """Explicit register/unregister state. ``False`` = owner has
    declared the channel closed, regardless of recent ack history."""

    last_ack_at: datetime | None = None
    """Wall clock of the most recent successful delivery / heartbeat /
    polling observation. ``None`` = no observation yet."""

    delivery_failures: int = 0
    """Consecutive failure count (= reset on each success)."""

    delivery_attempts_total: int = 0
    """Cumulative attempts (= success + failure). Debug telemetry."""

    failure_threshold: int = 3
    """``delivery_failures >= failure_threshold`` → considered dead."""

    stale_after: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    """``now() - last_ack_at > stale_after`` → considered dead.
    ``None`` last_ack_at is treated as not-yet-stale (= channel hasn't
    been used)."""

    def record_attempt(self, result: DeliveryResult) -> None:
        """Update state from a delivery outcome.

        Idempotent over multiple results with the same attempt; the
        ``delivery_attempts_total`` counter increments regardless,
        ``delivery_failures`` resets on success and increments on
        failure, ``last_ack_at`` advances only on success.
        """
        self.delivery_attempts_total += 1
        if result.ok:
            self.delivery_failures = 0
            self.last_ack_at = result.attempted_at
        else:
            self.delivery_failures += 1

    def is_alive(self, *, now: datetime | None = None) -> bool:
        """Inference: is this channel currently viable?

        Returns False when:
          - explicit ``is_open=False`` (= owner declared dead)
          - ``delivery_failures >= failure_threshold`` (= sustained failure)
          - ``last_ack_at`` is older than ``stale_after`` (= no recent
            evidence the channel is live)
        """
        if not self.is_open:
            return False
        if self.delivery_failures >= self.failure_threshold:
            return False
        if self.last_ack_at is not None:
            current = now or datetime.now(timezone.utc)
            if current - self.last_ack_at > self.stale_after:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe serialization for persistence (= RunRegistry Phase 1
        snapshot integration)."""
        return {
            "channel_id": self.channel_id,
            "is_open": self.is_open,
            "last_ack_at": (
                self.last_ack_at.isoformat()
                if self.last_ack_at is not None
                else None
            ),
            "delivery_failures": self.delivery_failures,
            "delivery_attempts_total": self.delivery_attempts_total,
            "failure_threshold": self.failure_threshold,
            "stale_after_seconds": self.stale_after.total_seconds(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelState":
        """Inverse of ``to_dict``. Resilient to missing optional fields."""
        last_ack_raw = data.get("last_ack_at")
        last_ack: datetime | None = None
        if isinstance(last_ack_raw, str):
            try:
                last_ack = datetime.fromisoformat(last_ack_raw)
            except ValueError:
                last_ack = None
        stale_seconds = data.get("stale_after_seconds")
        try:
            stale_after = timedelta(seconds=float(stale_seconds))
        except (TypeError, ValueError):
            stale_after = timedelta(minutes=5)
        return cls(
            channel_id=str(data.get("channel_id", "")),
            is_open=bool(data.get("is_open", True)),
            last_ack_at=last_ack,
            delivery_failures=int(data.get("delivery_failures", 0) or 0),
            delivery_attempts_total=int(
                data.get("delivery_attempts_total", 0) or 0,
            ),
            failure_threshold=int(data.get("failure_threshold", 3) or 3),
            stale_after=stale_after,
        )


__all__ = [
    "DEFAULT_RETRY_POLICY",
    "NO_RETRY_POLICY",
    "ChannelState",
    "DeliveryOutcome",
    "DeliveryResult",
    "RetryPolicy",
]
