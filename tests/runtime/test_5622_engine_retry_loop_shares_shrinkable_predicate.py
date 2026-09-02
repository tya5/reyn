"""Tier 2: #5622 — `retry_loop`'s own internal `compact()`-call except
clause (`engine.py`, the 3rd of 3 call sites across the codebase that
decide "wrap and offer to the shrink ladder") had drifted onto
`classify_llm_failure` ALONE, missing the narrower
`is_context_overflow_error` requirement the other 2 call sites
(`router_loop_driver.py`, #5577/#5593's own `_is_shrinkable_overflow`)
already carry — the exact "2 discriminators for one question" shape
#5577 already closed once, recurring at a 3rd site (lead-coder's own
#5621 D4 finding).

Fix (this PR): `_is_shrinkable_overflow` relocated into `engine.py`
itself as the public `is_shrinkable_overflow` (breaking the would-be
circular import — `router_loop_driver.py` already imports 2 of its own
building blocks FROM `engine.py`) — all 3 call sites now share the ONE
predicate, imported/called, never duplicated.

This file exercises the 3rd site directly, driving a REAL overflow
recovery through `retry_loop`'s own internal `compact()` call (real
`CompactionEngine`, real `Session`, no fake collaborator) with that
ONE call's own return value monkeypatched to raise — the standard,
narrow seam for forcing a specific exception TYPE at a real call site
without a full litellm-level stub (`LLMStub`'s own `cause=` vocabulary
covers 4 REAL litellm exception shapes, none of which is "genuinely
unclassifiable" — the shape this test needs).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.events import collect_events, settle
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _ContentDrivenLoop,
    _make_spill_session,
    _push,
)


class _UnclassifiableCompactionError(Exception):
    """Mirrors `test_5577_unify_overflow_classification_arms.py`'s own
    `_UnclassifiableModelConfigError` — neither in `FATAL_EXC_TYPES`,
    nor a rate-limit/timeout/5xx/quota shape, nor carrying a real
    overflow signal (`is_context_overflow_error` reads it False: no
    typed `ContextWindowExceededError`, no 413 status_code, no overflow
    keyword in the message) — genuinely unclassifiable by
    `classify_llm_failure`'s own 3-way split, which is exactly why its
    bare fallthrough (pre-#5622, at THIS 3rd call site only) would have
    misdiagnosed it as OVERFLOW and entered the shrink ladder."""


def test_engine_compact_call_site_does_not_enter_ladder_on_unclassifiable_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5622 deny — retry_loop's own internal `engine.compact()`
    call raising a genuinely unclassifiable exception must propagate
    BARE (never wrapped as `CompactionOverflowError`, never entering the
    shrink ladder) — the exact gap the 3rd call site's own pre-#5622
    `classify_llm_failure`-only check would have missed (that
    fallthrough is unconditionally OVERFLOW for anything neither FATAL
    nor RETRYABLE)."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, recovery_policy="never",
    )
    budgets = session._compaction_controller._engine.budgets
    # A real, driven overflow: enough content that raw_middle is
    # genuinely non-empty (retry_loop's own compact() is only called
    # when raw_middle has candidates to offer) — mirrors #5498's own
    # established sizing (test_5498_retry_loop_covers_zero_never_
    # persisted.py).
    head_tokens = budgets.effective_trigger + budgets.tail_budget + 1_000
    _push(session, "user", "H" * (head_tokens * 4))
    for _i in range(4):
        _push(session, "user", "F" * (max(1, budgets.tail_budget // 4) * 4))

    engine = session._compaction_controller._engine
    compact_calls = {"n": 0}

    async def _raise_unclassifiable(*args, **kwargs):
        compact_calls["n"] += 1
        raise _UnclassifiableCompactionError("model-x rejected this compact() call shape")

    monkeypatch.setattr(engine, "compact", _raise_unclassifiable)

    events = collect_events(session)

    def _fail_once(history: list, user_text: str) -> bool:
        return len([e for e in history]) > 0  # any real call fails, forcing retry_loop

    loop = _ContentDrivenLoop(_fail_once)

    async def _drive() -> None:
        with pytest.raises(_UnclassifiableCompactionError):
            await session._loop_driver._run_with_shrink(
                loop, "continue please", chain_id="c1",
            )
        await settle(session)

    import asyncio
    asyncio.run(_drive())

    assert compact_calls["n"] >= 1, (
        "sanity: engine.compact() must have genuinely been called at "
        "least once — otherwise this test never reached the call site "
        "#5622 fixes, and the assertion above proves nothing"
    )
    # The OUTER main-call's own overflow (a real, overflow-shaped
    # _FakeStatusError) legitimately enters retry_loop first — that is
    # expected, correct arm① behavior, not what this test checks. The
    # claim is narrower: the UNCLASSIFIABLE exception engine.compact()
    # itself raises must never show up as a shrink-ladder CAUSE — if it
    # did, the pre-#5622 gap (classify_llm_failure alone at this 3rd
    # call site) would have wrapped it as CompactionOverflowError and
    # fed it back into the SAME ladder.
    shrink_causes = [
        e.data.get("cause") for e in events if e.type == "compaction_shrink_recovered"
    ]
    assert "_UnclassifiableCompactionError" not in shrink_causes, (
        f"the unclassifiable exception must never appear as a shrink-"
        f"ladder cause (it should propagate bare out of retry_loop's "
        f"own compact() call site, never re-enter the ladder as a "
        f"CompactionOverflowError) — got causes {shrink_causes!r}"
    )
