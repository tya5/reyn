"""Tier 2: #5828 (owner-hit, reyn-self brown, 2026-09-05) -- ``compaction_
failed`` now carries a ``failure_class`` field distinguishing the 3 states
it can mean, so ``CompactionEngine.compact()`` (the shared emit site for
BOTH the reactive shrink ladder and the operator's own ``/compact``)
actually emits the RIGHT value for each of them -- not just that the field
exists (``test_chat_lifecycle_forwarder.py`` already pins the render side
against hand-built event data alone).

Driven through a REAL Session/RouterLoopDriver/RecoveryLadder, the same
harness ``test_5633_ladder_compact_failure_closes_marker.py`` already
established (``_make_session_t_max``, many-small-turns overflow shape) --
this file adds the SUCCESS arm that file never needed (it only closes the
marker on failure) and a genuinely-exhausted-ladder arm using
``cause="context_overflow"`` instead of that file's own
``cause="rate_limit"`` (RETRYABLE bypasses the ladder on the FIRST
attempt -- #5828's own state C, not the ladder-terminal state B).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.dev.testing.llm_stub import LLMStub
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _make_session_t_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t_max: int,
    *, max_shrink_iterations: int = 25,
):
    """Mirrors test_5633's own builder (itself mirroring test_5531's) --
    compaction must actually be reachable (attempted), not merely skipped
    for lack of candidates. ``max_shrink_iterations`` defaults generous
    (matching test_5296's own choice for a scenario that needs the ladder
    to actually take more than one rung before this file's own arms
    resolve)."""
    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
        max_shrink_iterations=max_shrink_iterations,
    )
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _push_many_small_turns(session) -> None:
    """The SAME many-small-turns overflow shape test_5633 uses: nothing to
    spill onto individually, so retry_loop must actually reach the fold
    stage (and, for this file's own shrink-then-succeed arm, actually
    halve ``attempt_len``) to make any progress at all."""
    for i in range(1, 6):
        _push(session, "user" if i % 2 else "assistant", f"turn-{i}", seq=i)
    texts = [f"turn-{i}:" + ("X" * 320) for i in range(6, 36)]
    for i, text in enumerate(texts, start=6):
        _push(session, "user" if i % 2 else "assistant", text, seq=i)


class _AlwaysOverflowingLoop:
    """Same shape as test_5633's own fixture of the same name -- every
    attempt reports the wire as still oversized, so neither reduction axis
    can ever progress and the ladder genuinely exhausts."""

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        raise _FakeStatusError("request too large", status_code=413)


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _content_len(messages: "list[dict]") -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


@pytest.mark.asyncio
async def test_overflow_then_shrink_success_emits_overflow_failure_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept -- state A (recoverable intermediate). The FIRST
    compact() attempt raises a real litellm.ContextWindowExceededError
    (cause="context_overflow"); the ladder catches it as OVERFLOW, halves
    attempt_len, and retries with a SMALLER offered chunk -- LLMStub's
    content-driven ``raise_for`` (architect ruling, #5382) keeps raising
    only while this call's own content has not yet shrunk from the first
    attempt's, so it stops raising the moment a real halving occurs.

    Witness: the FAILED attempt's own compaction_failed event carries
    failure_class="overflow" (not "fatal"/"retryable" -- this exception IS
    shrink-ladder-eligible), and the run does not raise (the ladder
    genuinely recovered, unlike the terminal-arm test below)."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=2800)
    _push_many_small_turns(session)
    events = collect_events(session)

    _first_len: "list[int]" = []

    def _raise_until_shrunk(messages: "list[dict]") -> bool:
        length = _content_len(messages)
        if not _first_len:
            _first_len.append(length)
            return True  # first attempt: full-size, raise
        return length >= _first_len[0]  # keep raising until it has shrunk

    stub = LLMStub(raise_for=_raise_until_shrunk, cause="context_overflow")
    stub.install()
    try:
        loop = _AlwaysOverflowingLoop()
        with pytest.raises(Exception):
            # #4381 PR-4: the SAME wrapper test_5633 drives directly (no
            # router_context_overflow_unrecovered witness needed here --
            # this file's OWN subject is failure_class, already fully
            # witnessed by the events below; _AlwaysOverflowingLoop's own
            # main-call-side 413 still fires once the compact-side shrink
            # succeeds, so SOME exception is expected -- see the terminal
            # arm below for the ladder-exhaustion shape instead).
            await session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        await settle(session)
    finally:
        stub.restore()

    failed = [e for e in events if e.type == "compaction_failed"]
    assert failed, f"expected at least one compaction_failed -- got kinds: {[e.type for e in events]!r}"
    assert all(e.data.get("failure_class") == "overflow" for e in failed), (
        f"every compact() failure here is a real ContextWindowExceededError "
        f"-- expected failure_class='overflow' on all of them, got: "
        f"{[e.data.get('failure_class') for e in failed]!r}"
    )
    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    assert recovered, (
        "expected the ladder to have caught the overflow and shrunk at "
        f"least once -- got kinds: {[e.type for e in events]!r}"
    )
    # Non-vacuity vs the ladder-exhaustion test below: THIS scenario's own
    # predicate stops raising the moment content has shrunk once. Unpacking
    # to a single-element tuple IS the "exactly one failed attempt" check
    # -- it raises (rather than silently passing) on 0 or on 2+, unlike a
    # magic-number length comparison. A predicate bug that kept raising
    # indefinitely (making this scenario indistinguishable from ladder
    # exhaustion) would show up here as a tuple-unpack failure.
    (_only_failed,) = failed


@pytest.mark.asyncio
async def test_ladder_exhaustion_still_emits_overflow_failure_class_throughout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept -- state B (ladder terminal), the OTHER half of the
    "overflow" bucket the render layer must NOT distinguish from state A
    at compaction_failed's own level (see engine.py's own #5828 comment
    for why: this method genuinely cannot know in advance whether the
    caller's shrink will succeed or hit the floor). Every compact() call
    here raises unconditionally (cause="context_overflow", no raise_for
    predicate) -- attempt_len keeps halving until MID_FLOOR, at which
    point RecoveryLadder raises UnrecoveredError -- EVERY compaction_
    failed event along the way still carries failure_class="overflow",
    proving the field is not merely correct for the LAST attempt but for
    every one, including the ones that inch toward the floor."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=2800, max_shrink_iterations=25)
    _push_many_small_turns(session)
    events = collect_events(session)

    stub = LLMStub(raise_for="compaction", cause="context_overflow")
    stub.install()
    try:
        loop = _AlwaysOverflowingLoop()
        with pytest.raises(Exception):
            await session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        await settle(session)
    finally:
        stub.restore()

    failed = [e for e in events if e.type == "compaction_failed"]
    _first_failure, *_more_failures = failed
    assert _more_failures, (
        "expected the ladder to have actually retried (more than one "
        "compact() attempt) before exhausting -- an unconditional "
        "raise_for should keep failing every attempt, not stop after "
        f"one; got kinds: {[e.type for e in events]!r}"
    )
    assert all(e.data.get("failure_class") == "overflow" for e in failed), (
        f"a real ContextWindowExceededError, every single time -- expected "
        f"failure_class='overflow' on every occurrence, got: "
        f"{[e.data.get('failure_class') for e in failed]!r}"
    )


@pytest.mark.llm_stub(raise_for="compaction", cause="rate_limit")
def test_non_overflow_exception_emits_a_non_overflow_failure_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept -- state C (turn-killing non-overflow exception).
    cause="rate_limit" builds a real litellm.RateLimitError, classified
    RETRYABLE by classify_llm_failure -- classify_compact_overflow's own
    bare re-raise for RETRYABLE means the shrink ladder never even sees
    this exception (it is not wrapped as CompactionOverflowError), so it
    propagates on the VERY FIRST compact() failure -- unlike the overflow
    arms above, exactly ONE compaction_failed fires, and it must NOT read
    "overflow" (the render layer commits to "this turn is ending" on
    this value, so a wrong "overflow" here would show the FALSE
    provisional wording for an exception that is not going to retry)."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=2800)
    _push_many_small_turns(session)
    events = collect_events(session)
    loop = _AlwaysOverflowingLoop()

    with pytest.raises(Exception):
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        )
    asyncio.run(settle(session))

    failed = [e for e in events if e.type == "compaction_failed"]
    (only,) = failed
    assert only.data.get("failure_class") in ("fatal", "retryable"), (
        f"a real RateLimitError (RETRYABLE) must not read as 'overflow' -- "
        f"got failure_class={only.data.get('failure_class')!r}"
    )
    assert only.data.get("failure_class") != "overflow"
