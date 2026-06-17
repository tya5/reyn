"""Event log verifier (FP-0036 Component C).

Assertions:
  - must_emit: list of {type, count, payload subset, status filter}
  - must_not_emit: list of {type, payload subset}
  - sequence: ordered subsequence of event types
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .types import VerifierResult

if TYPE_CHECKING:
    from reyn.dev.dogfood.scenarios import EventAssertion, ExpectedEvents


# ---------------------------------------------------------------------------
# Count comparator helpers
# ---------------------------------------------------------------------------

_COUNT_RE = re.compile(r'^(==|>=|<=|<|>)?(\d+)$')


def _parse_count(count_str: str) -> tuple[str, int]:
    """Return (op, n) from a count comparator string.

    Accepted forms: ``==N``, ``>=N``, ``<=N``, ``<N``, ``>N``, ``N``
    (bare integer treated as ``==N``).
    """
    m = _COUNT_RE.match(str(count_str).strip())
    if not m:
        raise ValueError(f"Invalid count comparator: {count_str!r}")
    op = m.group(1) or "=="
    n = int(m.group(2))
    return op, n


def _check_count(actual: int, count_str: str) -> bool:
    """Return True if ``actual`` satisfies the comparator in ``count_str``."""
    op, n = _parse_count(count_str)
    if op == "==":
        return actual == n
    if op == ">=":
        return actual >= n
    if op == "<=":
        return actual <= n
    if op == "<":
        return actual < n
    if op == ">":
        return actual > n
    return False  # unreachable


# ---------------------------------------------------------------------------
# Event matching helpers
# ---------------------------------------------------------------------------


def _event_payload(event: dict) -> dict:
    """Return the data dict from an event (= event['data'] or event itself)."""
    return event.get("data", event)


def _build_effective_payload(assertion: "EventAssertion") -> dict:
    """Merge assertion.payload + status shorthand into one expected dict."""
    effective: dict = dict(assertion.payload)
    if assertion.status is not None:
        effective["status"] = assertion.status
    return effective


def _event_matches_assertion(event: dict, assertion: "EventAssertion") -> bool:
    """Return True if the event satisfies the assertion's type + payload filter."""
    if event.get("type") != assertion.type:
        return False
    effective_payload = _build_effective_payload(assertion)
    if not effective_payload:
        return True
    event_data = _event_payload(event)
    return all(event_data.get(k) == v for k, v in effective_payload.items())


# ---------------------------------------------------------------------------
# Public verifier
# ---------------------------------------------------------------------------


def verify_events(
    expected: "ExpectedEvents | None",
    events: list[dict],
) -> VerifierResult:
    """Score the event tail against expected.

    Each event in ``events`` is a dict with at minimum {"type": str, "data": dict}.

    Parameters
    ----------
    expected:
        The ExpectedEvents declared in the scenario. ``None`` → blocked.
    events:
        List of event dicts from the scenario run.

    Returns
    -------
    VerifierResult with outcome:
      verified     — all must_emit pass + no must_not_emit triggered + sequence matches
      refuted      — any assertion failed (concrete miss recorded in detail)
      inconclusive — empty events list and assertions exist
      blocked      — no expected provided
    """
    if expected is None:
        return VerifierResult(outcome="blocked", detail={"reason": "no expected events declared"})

    has_assertions = (
        bool(expected.must_emit)
        or bool(expected.must_not_emit)
        or bool(expected.sequence)
        or bool(expected.must_emit_any)
    )

    if not events and has_assertions:
        return VerifierResult(
            outcome="inconclusive",
            detail={"reason": "event list is empty but assertions exist"},
        )

    failures: list[dict] = []

    # ── must_emit checks ────────────────────────────────────────────────────
    for assertion in expected.must_emit:
        matching = [e for e in events if _event_matches_assertion(e, assertion)]
        count = len(matching)
        if not _check_count(count, assertion.count):
            failures.append({
                "check": "must_emit",
                "type": assertion.type,
                "count_required": assertion.count,
                "count_found": count,
                "payload_filter": _build_effective_payload(assertion),
            })

    # ── must_not_emit checks ────────────────────────────────────────────────
    for assertion in expected.must_not_emit:
        matching = [e for e in events if _event_matches_assertion(e, assertion)]
        if matching:
            failures.append({
                "check": "must_not_emit",
                "type": assertion.type,
                "payload_filter": _build_effective_payload(assertion),
                "found_count": len(matching),
            })

    # ── must_emit_any check (B28-Q2: OR-of-listed semantics) ───────────────
    # Passes if at least one of the listed assertions matches at least one event.
    if expected.must_emit_any:
        any_matched = any(
            any(_event_matches_assertion(e, assertion) for e in events)
            for assertion in expected.must_emit_any
        )
        if not any_matched:
            failures.append({
                "check": "must_emit_any",
                "types": [a.type for a in expected.must_emit_any],
                "reason": "none of the listed event types was found in the event log",
            })

    # ── sequence check ──────────────────────────────────────────────────────
    if expected.sequence:
        seq = expected.sequence
        event_types = [e.get("type", "") for e in events]
        # Verify seq is an ordered subsequence of event_types
        seq_idx = 0
        for et in event_types:
            if seq_idx < len(seq) and et == seq[seq_idx]:
                seq_idx += 1
        if seq_idx < len(seq):
            # Not all sequence elements found in order
            failures.append({
                "check": "sequence",
                "required_sequence": seq,
                "matched_up_to_index": seq_idx,
                "reason": f"sequence element {seq[seq_idx]!r} not found in order",
            })

    if failures:
        return VerifierResult(outcome="refuted", detail={"failures": failures})

    return VerifierResult(
        outcome="verified",
        detail={
            "must_emit_count": len(expected.must_emit),
            "must_not_emit_count": len(expected.must_not_emit),
            "sequence_length": len(expected.sequence),
            "must_emit_any_count": len(expected.must_emit_any),
        },
    )
