"""Tier 2: #5329 — a quota exhaustion inside ``retry_loop``'s OWN
``engine.compact()`` call must terminate on the FIRST occurrence, never
enter the shrink ladder.

#5256 already guarded the OUTER gate (``RouterLoopDriver._run_with_shrink``,
router_loop_driver.py) so a quota-exhausted ``main_call`` never enters
``retry_loop`` at all. But retry_loop's OWN internal ``compact()`` call (the
compaction LLM call, triggered once a GENUINE context overflow is already
being recovered) had no equivalent gate — #3783 stage 3's own "EVERY
compact()-call exception recovers by default" rule wrapped a quota
exception the SAME as a transient 5xx, entering the shrink-escalation
ladder and burning ``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS`` (2) more calls
into the SAME exhausted quota window before finally giving up with
``UnrecoveredError`` — 3 wasted round-trips during an active outage
(owner's real-machine incident, reyn-self).

#5543 / #5531 §10 (2026-08-30) SUPERSEDES this file's own original
narrower scope: ``classify_llm_failure`` now groups rate-limit/5xx/
network/quota into ONE RETRYABLE class, all excluded from the shrink
ladder identically — not just the quota subtype #5329 originally carved
out. See ``test_transient_rate_limit_also_terminates_bare_on_first_
occurrence``'s own docstring for the updated claim.

Real ``retry_loop`` throughout, no mocks — only ``engine.compact()`` is a
scripted real async callable (matching this module's own established
harness, e.g. ``test_wire_byte_estimate_4944.py``'s ``_MinimalCompactionEngine``).
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
from reyn.services.compaction.engine import (
    CompactionOverflowError,
    ComputedBudgets,
    RetryPayload,
    UnrecoveredError,
    retry_loop,
)


class _QuotaExhaustedError(Exception):
    """Real, scripted stand-in for litellm's ``RateLimitError`` shape for a
    usage-window/plan quota exhaustion — the same structured ``.body``
    shape #5256's own fixture uses (a real observed incident field set,
    not invented for this test)."""

    def __init__(self) -> None:
        super().__init__("The usage limit has been reached")
        self.status_code = 429
        self.body = {
            "type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "resets_in_seconds": 12258,
        }


class _TransientRateLimitError(Exception):
    """A REAL, ordinary per-request 429 — no structured ``usage_limit_
    reached`` body. #3783 stage 3's own reasoning (shrinking can genuinely
    help this class) must stay unaffected by #5329's narrower carve-out."""

    def __init__(self) -> None:
        super().__init__("Rate limit exceeded, please retry")
        self.status_code = 429


class _MinimalCompactionEngine:
    """Smallest real collaborator retry_loop needs — mirrors
    test_wire_byte_estimate_4944.py's own ``_MinimalCompactionEngine``."""

    def __init__(self) -> None:
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=1_000, body_budget=500,
            tail_budget=1_500, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
            section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                          "session_user_facts": 50, "artifacts_referenced": 175},
        )
        self._events = EventLog()
        self._T_comp_SP = 100


def _cfg() -> CompactionConfig:
    return CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )


def _learner() -> TokenMultiplierLearner:
    return TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")


def test_quota_exhausted_compact_terminates_on_first_occurrence_bare() -> None:
    """Tier 2: THE core #5329 proof. ``engine.compact()`` always raises the
    quota-exhausted shape — retry_loop must call ``compact()`` exactly
    ONCE (not the shrink ladder's multiple attempts), and the exception
    that propagates OUT of retry_loop must be the BARE quota exception
    itself — never wrapped in ``CompactionOverflowError``/
    ``UnrecoveredError`` (the SAME "re-raise bare, let the existing
    generic catch-all handle it" shape #5256's outer gate already uses;
    a caller catching retry_loop's own normal failure vocabulary must NOT
    catch this).

    #5292-style six-questions note (architect review on this PR): a
    prior version of this file had the count check and the TYPE check as
    two separate tests — the second could only ever go red in exactly
    the situation the first already does (nothing distinguishes them),
    so they are one test now, both invariants proven from the SAME
    single drive."""
    engine = _MinimalCompactionEngine()
    compact_calls = 0

    async def _compact(input_chunk, *, covers_through=None):
        nonlocal compact_calls
        compact_calls += 1
        raise _QuotaExhaustedError()

    engine.compact = _compact

    async def _never_called_main_call(**kwargs):
        raise AssertionError("main_call must never run — compact() never succeeds")

    async def _drive():
        try:
            await retry_loop(
                SP="sp", payload=RetryPayload(
            head=[], raw_middle=[{"role": "user", "content": "x", "seq": 1}],
            tail=[], new_msg={"role": "user", "content": "q", "seq": 2},
            seq_by_id={},
        ),
                cfg=_cfg(), model="test-model", engine=engine,  # type: ignore[arg-type]
                learner=_learner(), main_call=_never_called_main_call,
            )
        except (CompactionOverflowError, UnrecoveredError):
            raise AssertionError(
                "a quota exhaustion must propagate BARE — retry_loop's "
                "own wrapper types would be caught by callers as an "
                "ordinary overflow, not a quota-specific terminal cause"
            )

    with pytest.raises(_QuotaExhaustedError):
        asyncio.run(_drive())

    assert compact_calls == 1, (
        f"expected exactly 1 compact() call (no shrink retry into the SAME "
        f"exhausted quota) — got {compact_calls}"
    )


def test_transient_rate_limit_also_terminates_bare_on_first_occurrence() -> None:
    """Tier 2: #5543 / #5531 §10 SUPERSEDES this test's own original claim
    (#3783 stage 3's narrower rule: only a QUOTA exhaustion is excluded
    from the shrink ladder, an ordinary per-request 429 still enters it).
    ``classify_llm_failure`` groups rate-limit/5xx/network/quota into ONE
    RETRYABLE class — shrinking cannot fix an infra condition or a
    wait-bound rate limit any better than it fixes a quota window, so
    both are now excluded identically: ``engine.compact()``'s own
    exception handler re-raises bare the moment classification is
    anything but OVERFLOW, before the shrink ladder (and the now-retired
    same-cause cap, #5531 §10) ever sees it.

    Mirrors ``test_quota_exhausted_compact_terminates_on_first_occurrence_
    bare`` exactly (same harness, same 2 invariants: exactly 1 compact()
    call, the bare exception type propagates) — the two causes are no
    longer distinguishable by this loop's own behaviour, only by which
    exception type the caller catches."""
    engine = _MinimalCompactionEngine()
    compact_calls = 0

    async def _compact(input_chunk, *, covers_through=None):
        nonlocal compact_calls
        compact_calls += 1
        raise _TransientRateLimitError()

    engine.compact = _compact

    async def _never_called_main_call(**kwargs):
        raise AssertionError("main_call must never run — compact() never succeeds")

    async def _drive():
        try:
            await retry_loop(
                SP="sp", payload=RetryPayload(
            head=[], raw_middle=[{"role": "user", "content": "x", "seq": 1}],
            tail=[], new_msg={"role": "user", "content": "q", "seq": 2},
            seq_by_id={},
        ),
                cfg=_cfg(), model="test-model", engine=engine,  # type: ignore[arg-type]
                learner=_learner(), main_call=_never_called_main_call,
            )
        except (CompactionOverflowError, UnrecoveredError):
            raise AssertionError(
                "a RETRYABLE (rate-limit) exception must propagate BARE — "
                "retry_loop's own wrapper types would be caught by "
                "callers as an ordinary overflow, not a retryable cause"
            )

    with pytest.raises(_TransientRateLimitError):
        asyncio.run(_drive())

    assert compact_calls == 1, (
        f"expected exactly 1 compact() call (RETRYABLE never enters the "
        f"shrink ladder — #5543) — got {compact_calls}"
    )
