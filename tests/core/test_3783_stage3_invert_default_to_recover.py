"""Tier 2: #3783 stage 3 (owner-ratified) — the compact()-call except clause
in ``retry_loop`` (``services/compaction/engine.py``) recovers by DEFAULT
instead of re-raising unknown exceptions raw.

Three witnesses, per lead-coder's design (issue #3783 comments):

(a) rescue arm — an exception the OLD keyword predicate did NOT recognise
    (no shared word with "context"/"token"/"length"/"limit"/"too long"/
    "too large") but which shrinking the input genuinely fixes (the
    motivating live incident: a response cut short by an output cap raises
    a bare ``JSONDecodeError``). Constructed, not reproduced: a small output
    cap + a large input, mirroring the incident's actual mechanism.
(b) discriminator arm — an exception that is NOT input-size-dependent (a
    Fake LLM call that raises unconditionally, regardless of how far the
    input has shrunk). Without this arm, every constructed witness would
    "recover by shrinking" trivially and never exercise the stage-2 cap —
    the direction that could be wrong (inverting a default that used to be
    correct) stays unverified.
(c) record arm — arm (b)'s terminal ``UnrecoveredError``, raised from INSIDE
    the compaction call (not the main router call), still reaches the router
    turn's F2b handoff loop and the loop emits
    ``router_context_overflow_unrecovered`` — the failure is not silently
    invisible in ``.reyn/events``. This is checked, not assumed: the code
    path from a compaction-side ``UnrecoveredError`` to this audit-event was
    measured BEFORE writing this test, specifically to check whether stage 3
    needed a NEW emit site here or only needed this witness to confirm the
    existing one already covers it.

    #4885 (architect finding, #4381's own late-stage remainder): the type
    this witness expects to propagate CHANGED. Originally,
    ``RouterLoopDriver._run_with_shrink`` caught BOTH
    ``_ContextOverflowError`` and ``_UnrecoveredError`` and re-raised both
    as a single ``_ContextOverflowError`` — which is exactly the reported
    defect (a misclassification: "shrinking recovered the same cause
    repeatedly" got relabelled as "the context window is too small", the
    literal shape #4885 fixes for real 413s). This arm's own RuntimeError
    IS such a case — not a real overflow at all — so it now correctly
    propagates as ``UnrecoveredError``, not ``ContextOverflowError``.
    ``run_turn`` in ``router_loop_driver.py`` widened its own except to
    catch BOTH types for the SAME audit event, so this arm's OWN claim
    (the event still fires) is unaffected — only the exception TYPE this
    test asserts on changed, to the more accurate one.

Real ``retry_loop``/``CompactionEngine`` collaborators throughout arms (a)/(b)
(only the LLM completion call is stubbed — the same one seam
``synthetic_t_max``-style tests already stub); mirrors
``tests/services/test_pr_n6_compaction_overflow_retry.py``'s existing
``_OverflowingEngine`` harness. Arm (c) drives a REAL ``Session``/
``RouterLoopDriver`` end to end (``tests/_support/session.py``), with only
``RouterLoop`` (a collaborator double, per
``test_force_close_chat_handoff_1092.py``'s existing ``_install_fake_loop``
pattern) and the compaction engine's own LLM completion call faked.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.events.events import EventLog
from reyn.llm.pricing import TokenUsage
from reyn.services.compaction.engine import (
    ChatSummary,
    ComputedBudgets,
    ContextOverflowError,
    HistoryChunkToCompact,
    UnrecoveredError,
    retry_loop,
)
from tests._support.events import collect_events, settle
from tests._support.session import make_session as _make_session
from tests._support.session import push as _push

# ── shared retry_loop harness (mirrors tests/services/test_pr_n6_compaction_overflow_retry.py) ──


class _StubEngineCompactRaises:
    """A minimal engine stub whose ``compact()`` is driven by a caller-supplied
    callable — real ``ComputedBudgets``/``EventLog``, only the LLM-shaped call
    is faked (same discipline as ``_OverflowingEngine`` in the stage-2 tests)."""

    def __init__(self, compact_fn) -> None:
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=1_000, body_budget=500,
            tail_budget=1_500, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
            section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                          "session_user_facts": 50, "artifacts_referenced": 175},
        )
        self._compact_fn = compact_fn
        self._events = EventLog()

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through=None,
    ) -> ChatSummary:
        return await self._compact_fn(input_chunk)


def _never_overflowing_main_call():
    """A main_call that always succeeds immediately — isolates the witness to
    the compact()-call classification, the ONE thing stage 3 changes."""
    from types import SimpleNamespace

    async def _main_call(**kwargs):
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])

    return _main_call


def _turns(n: int, *, seq_start: int = 1) -> list[dict]:
    return [
        {"role": "user", "content": f"turn {i}", "seq": seq_start + i}
        for i in range(n)
    ]


def _cfg():
    from reyn.config import CompactionConfig
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


def _learner(tmp_path):
    from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
    return TokenMultiplierLearner(
        storage_path=tmp_path / "mult.json", chars4_mode=True,
    )


# ── arm (a): rescue — an unrecognised exception that shrinking genuinely fixes ──


@pytest.mark.asyncio
async def test_unrecognised_exception_recovers_when_shrinking_fixes_it(tmp_path) -> None:
    """Tier 2: #3783 stage 3 arm (a) — a JSONDecodeError sharing NO keyword with
    the old overflow predicate (the motivating live incident's exact shape: a
    response cut off by an output cap) is now recovered by the shrink ladder
    instead of killing the turn — succeeding once raw_middle has shrunk enough
    that the (constructed) "output cap" the fake LLM enforces is not exceeded."""

    # Fake compaction LLM: raises a JSONDecodeError (unrecognised by the old
    # keyword predicate) when the input is "too large" (mimics an output cut
    # off by an output-token cap); succeeds once the input has shrunk below it.
    async def _compact_fn(input_chunk: HistoryChunkToCompact):
        if sum(1 for _ in input_chunk.messages) > 2:
            json.loads("{'unterminated")  # raises json.JSONDecodeError
        return ChatSummary(
            topic_arc="stub summary",
            covers_through_seq=max(
                (t.get("seq", 0) for t in input_chunk.messages if isinstance(t, dict)),
                default=0,
            ),
        )

    engine = _StubEngineCompactRaises(_compact_fn)

    result = await retry_loop(
        SP="system prompt",
        head=[],
        raw_middle=_turns(8),
        tail=[],
        new_msg={"role": "user", "content": "new"},
        cfg=_cfg(),
        model="fake-model",
        engine=engine,
        learner=_learner(tmp_path),
        main_call=_never_overflowing_main_call(),
    )
    assert result is not None  # recovered — did NOT propagate the JSONDecodeError raw


# ── arm (b): discriminator — an exception shrinking can NEVER fix ──────────


@pytest.mark.asyncio
async def test_input_independent_exception_hits_the_cap_not_an_infinite_loop(tmp_path) -> None:
    """Tier 2: #3783 stage 3 arm (b) — an exception that recurs regardless of
    how far the input has shrunk (a Fake LLM raising unconditionally, and
    NOT one of classify_llm_failure's FATAL/RETRYABLE members — falling
    through to OVERFLOW is #5543's own documented default, matching what
    this ladder already implicitly assumed pre-#5543) still hits a BOUNDED
    terminal, not an infinite shrink loop — proving the inverted default
    does not trade a loud crash for one.

    #5531 §10 (2026-08-30) SUPERSEDES the ORIGINAL mechanism this test
    pinned: T3 (the same-cause cap) is retired — the bound is now the
    ``_compact_attempt_len`` halving ladder's own mid=1-turn floor
    (raw_middle starts at 8 turns: 8→4→2→1, 4 compact() calls, each
    failing identically, then the floor raises). Still verifies stage 3's
    ② fix: the recorded cause on EVERY recover is the WRAPPED exception's
    type (``RuntimeError``), not the wrapper's constant
    (``CompactionOverflowError``) — that telemetry fix is independent of
    which mechanism the count/floor is now.

    Falsification (performed for real): reverting ② (back to
    ``_cause = type(_overflow_exc).__name__``, dropping the ``__cause__``
    unwrap) makes the ``causes`` assertion below fail with
    ``{"CompactionOverflowError"}`` — the SAME constant for every
    iteration regardless of what actually failed, confirming the
    cause-naming fix is load-bearing for the audit trail's correctness,
    not cosmetic.
    """
    call_count = [0]

    async def _compact_fn(input_chunk: HistoryChunkToCompact):
        call_count[0] += 1
        raise RuntimeError("boom: fails unconditionally regardless of input size")

    engine = _StubEngineCompactRaises(_compact_fn)
    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    with pytest.raises(UnrecoveredError):
        await retry_loop(
            SP="system prompt",
            head=[],
            raw_middle=_turns(8),
            tail=[],
            new_msg={"role": "user", "content": "new"},
            cfg=_cfg(),
            model="fake-model",
            engine=engine,
            learner=_learner(tmp_path),
            main_call=_never_overflowing_main_call(),
        )
    await settle(engine._events)

    # #5531 §10: no max_iterations to grind through any more — the
    # mid=1-turn floor bounds this instead, deterministically, via the
    # halving arithmetic on this fixture's own 8-turn raw_middle
    # (8→4→2→1, 4 compact() calls, no spill_fn injected so the floor
    # raises on the 4th).
    assert call_count[0] == 4

    recovered = [e for e in seen if e.type == "compaction_shrink_recovered"]
    # Unpacking to exactly 4 elements raises ValueError otherwise.
    first, second, third, fourth = recovered
    causes = {e.data.get("cause") for e in recovered}
    assert causes == {"RuntimeError"}, (
        f"expected the WRAPPED exception's type name, got {causes!r} — "
        "the ② fix reads type(exc.__cause__).__name__, not the wrapper's own type"
    )


# ── arm (c): record — a compaction-side UnrecoveredError is not invisible ──


class _AlwaysOverflowRouterLoop:
    """Collaborator double for RouterLoop (mirrors ``_FakeRouterLoop`` in
    ``test_force_close_chat_handoff_1092.py``) — ``run()`` always overflows,
    so ``RouterLoopDriver._run_with_shrink`` always enters the retry_loop
    branch (the ONLY way to reach the compaction-side failure this arm
    targets)."""

    def __init__(self, *args, **kwargs) -> None:
        self.router_model = "fake-model"
        self.last_call_usage = TokenUsage()

    async def run(self, *, user_text: str, history: list[dict]) -> TokenUsage:
        # The message must match ``is_context_overflow_error``'s keyword
        # fallback ("context"/"token"/"length"/"limit"/"too long"/"too
        # large") — the predicate does not special-case ``ContextOverflowError``
        # by TYPE, only litellm's own ``ContextWindowExceededError``; an
        # unmatched message here makes ``_run_with_shrink`` re-raise raw
        # WITHOUT ever entering retry_loop, silently missing this arm's
        # target code path entirely (caught during test development).
        raise ContextOverflowError("simulated: context length exceeded")


@pytest.mark.asyncio
async def test_compaction_side_unrecovered_error_emits_router_context_overflow_unrecovered(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #3783 stage 3 arm (c) — an ``UnrecoveredError`` raised INSIDE
    the compaction call (retry_loop's own cap, stage 2) still surfaces as
    ``router_context_overflow_unrecovered`` in the P6 audit log, driven
    through the REAL ``Session``/``RouterLoopDriver``/``CompactionController``
    stack (only ``RouterLoop`` and the compaction engine's own LLM completion
    call are faked).

    A small ``t_max`` (2800, matching ``test_wrap_up_sub_viable_raises``'s
    "sub-viable model" shape) makes ``wrap_up_output_reserve`` None, so the
    F2b handoff loop gives up on the FIRST overflow instead of attempting a
    force-close handoff first — isolating the witness to whether the record
    fires, not the handoff mechanics (already covered by
    ``test_force_close_chat_handoff_1092.py``).
    """
    monkeypatch.setattr(
        "reyn.runtime.router_loop.RouterLoop",
        lambda *a, **k: _AlwaysOverflowRouterLoop(),
    )

    session = _make_session(tmp_path, t_max=2800, monkeypatch=monkeypatch)
    for i in range(20):
        _push(session, "user" if i % 2 == 0 else "assistant", f"turn {i} " + "x" * 200)

    # raw_middle must be non-empty for retry_loop to call engine.compact() at
    # all (verified empirically before writing this test: with t_max=2800 and
    # the 20 pushed turns above, decompose_history_for_retry() yields a
    # non-empty raw_middle).
    _head, raw_middle, _tail, _summary, _ = session._history_buffer.decompose_history_for_retry()
    assert raw_middle, "test setup: raw_middle must be non-empty to exercise engine.compact()"

    # Force the compaction LLM to fail UNCONDITIONALLY (arm (b)'s shape) —
    # triggers the lazy engine build, then stubs its one LLM-shaped seam.
    engine = session._compaction_controller._engine
    async def _always_fail(*args, **kwargs):
        raise RuntimeError("boom: compaction LLM always fails, any input size")
    monkeypatch.setattr(engine, "_acompletion", _always_fail)

    events = collect_events(session)

    # #4885: UnrecoveredError, not ContextOverflowError — see the module
    # docstring's arm (c) note. This arm's RuntimeError is not a real
    # overflow; UnrecoveredError is the accurate diagnosis, and
    # `router_context_overflow_unrecovered` still fires for it (asserted
    # below) via `run_turn`'s own widened except, not a type-merging rewrap.
    with pytest.raises(UnrecoveredError):
        await session._run_router_loop("trigger a turn", "chain-3783-stage3-c")
    await settle(session)

    seen = [e.type for e in events]
    assert "router_context_overflow_unrecovered" in seen
