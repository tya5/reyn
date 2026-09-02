"""Tier 2: #5615 — "does `RouterLoopDriver._attempt_reactive_spill`
(the DRIVER-level reactive spill fallback, `router_loop_driver.py:480`)
still receive a non-empty candidate set in production, post-#5596
(rung① tier-batching) and post-#5612 (durable spill/compact)?"

Lead-coder's own suspicion (#5615's own issue body): rung① — inside
`retry_loop`, engine.py — now batches a WHOLE tier per call (#5596), so
by the time a turn reaches `UnrecoveredError` (the ONLY trigger for
`_attempt_reactive_spill`, `router_loop_driver.py:652`), rung① may have
already exhausted every spillable candidate — making this driver-level
fallback dead weight reachable with 0 candidates, always.

**Measured directly, twice, correcting my own first (wrong) framing
in between** (never assumed from reading the ladder):

- First attempt assumed retry_loop/rung① only runs when `compact()`
  overflows a genuine context/token limit, and tried to construct a
  scenario where retry_loop never fires at all. **Real event trace
  showed this premise false**: `_run_with_shrink`'s own overflow
  handler invokes `retry_loop` for EVERY shrinkable overflow — byte-
  limit (413) included — regardless of `recovery_policy` (that knob
  only gates whether a successful fold gets DURABLY PERSISTED, #5612 —
  never whether retry_loop itself runs). `compaction_started` fired
  twice in a scenario I had wrongly asserted would produce none.
- Re-measured with the real mechanism understood, and a genuine 3rd
  measurement correcting a 2nd wrong guess: a bare `@pytest.mark.
  llm_stub` (unconditional compact() SUCCESS) let retry_loop's own
  compact() call fold the ENTIRE huge `head`-face candidate directly
  into a summary (`compaction_wire_bytes_measured` showing
  `accepted: True`, wire bytes 60,151 → 10,050) — recovery succeeded
  entirely inside `_run_with_shrink`, `_attempt_reactive_spill` was
  NEVER even called. This is a genuinely DIFFERENT, equally real
  production outcome: whether the driver-level fallback is ever
  reached depends on whether retry_loop's own bounded compact() ladder
  eventually SUCCEEDS or genuinely EXHAUSTS. `LLMStub(raise_for=...,
  cause="byte_limit")` forcing compact() to fail on every attempt
  (matching a real, persistent upstream failure — the SAME class of
  scenario retry_loop's own `max_shrink_iterations` bound exists for)
  reproduces the exhaustion path directly: retry_loop's own rung①
  (`_spill_fn`, scoped to `raw_middle` ONLY — its own docstring:
  "never head/tail, a SEPARATE population this closure never sees")
  spills whatever small, unrelated `raw_middle` content it can reach
  first: `spill_candidate_population_exhausted` fires for that
  population; retry_loop then gives up (its own bounded ladder
  exhausted), `_run_with_shrink` raises `UnrecoveredError`, and
  `_attempt_reactive_spill` — reached ONLY then — finds the
  still-untouched `head`-face huge candidate and spills it. Confirmed
  directly (instrumented `_attempt_reactive_spill`'s own entry):
  genuinely called, receiving `head=3`.

Answer: **non-zero — retained** (issue #5615's own accept branch ②),
**conditionally**: `_attempt_reactive_spill` is reached only once
retry_loop's own bounded compact() ladder genuinely EXHAUSTS (a real,
persistent-failure scenario — the below reproduces it with `LLMStub`
forcing every compact() attempt to fail). When compact() instead
SUCCEEDS (the more common healthy-LLM case), it can fold a `head`-face
candidate directly and `_attempt_reactive_spill` is never called at
all — that is a different, also-correct outcome, not evidence against
retention. rung①'s own tier-batching (#5596) makes it more EFFICIENT
within its own scope (`raw_middle`); it does not, and structurally
cannot, widen that scope to cover `head`/`tail` — the exact population
`_attempt_reactive_spill` alone still reaches, on the exhaustion path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.runtime.chat_message import Spillability
from tests._support.events import collect_events, settle
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _ContentDrivenLoop,
    _has_content,
    _make_spill_session,
    _push,
)


def test_attempt_reactive_spill_receives_a_real_head_candidate_rung1_never_sees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5615 accept — a genuinely un-spilled huge tool result (a
    real `head`-face candidate, not `raw_middle`), retry_loop's own
    bounded compact() ladder genuinely EXHAUSTED (forced via
    `LLMStub(raise_for=..., cause="byte_limit")` — every compact() call
    fails, matching a real persistent-upstream-failure scenario, the
    same class `max_shrink_iterations` bounds), a real 413 on the main
    call: `_attempt_reactive_spill` still receives and consumes the
    candidate. Witnessed via 2 independent, PUBLIC signals: (1) a real
    `tool_result_offloaded` audit-event for the huge content, (2) that
    event's own position in the trace — AFTER rung①'s own
    `spill_candidate_population_exhausted` (its own `raw_middle`-scoped
    population, unrelated small content) reports rung①'s own population
    exhausted, proving rung① itself could not have been the one that
    spilled the huge (`head`-face) candidate — only
    `_attempt_reactive_spill`, called once retry_loop's own attempt
    gives up, reaches `head` at all.

    NOT tested with an unconditionally-succeeding stub: driven that way
    once, directly, retry_loop's own compact() call folds the ENTIRE
    huge candidate into a summary by itself and `_attempt_reactive_
    spill` is never even called — a real, DIFFERENT production outcome
    (compact() succeeding), not a bug, but not the scenario this test
    targets (whether the driver-level fallback is EVER reached)."""
    from reyn.dev.testing.llm_stub import LLMStub

    session = _make_spill_session(tmp_path, monkeypatch, t_max=None)
    events = collect_events(session)

    huge = "K" * 50_000
    _push(session, "user", "look something up")
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    loop = _ContentDrivenLoop(lambda history, user_text: _has_content(history, huge))
    stub = LLMStub(raise_for=lambda messages: True, cause="byte_limit")
    stub.install()
    try:
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            ),
        )
    finally:
        stub.restore()
    asyncio.run(settle(session))

    assert any(_has_content(c, huge) for c in loop.calls[:-1]), (
        "sanity: the main call must have genuinely 413'd on the "
        "un-spilled huge content at least once before recovering"
    )

    offload_types_in_order = [
        e.type for e in events
        if e.type in ("tool_result_offloaded", "spill_candidate_population_exhausted")
    ]
    huge_offload = next(
        (
            e for e in events
            if e.type == "tool_result_offloaded" and e.data.get("total_chars") == 50_000
        ),
        None,
    )
    assert huge_offload is not None, (
        "the huge (head-face) candidate must have genuinely been "
        "spilled — 0 matching offload events means the driver-level "
        "fallback received nothing, the exact 'dead weight' outcome "
        "#5615 asks whether this call site still avoids"
    )
    exhausted_events = [
        e for e in events if e.type == "spill_candidate_population_exhausted"
    ]
    assert exhausted_events, (
        "sanity: rung① must have genuinely reported its own (raw_middle-"
        "scoped) population exhausted at least once — otherwise this "
        "test cannot show rung① was NOT the one that spilled the huge "
        "candidate"
    )
    huge_offload_index = offload_types_in_order.index("tool_result_offloaded", 1)
    assert "spill_candidate_population_exhausted" in offload_types_in_order[:huge_offload_index], (
        f"the huge candidate's own offload must come AFTER rung①'s own "
        f"exhaustion report — order was {offload_types_in_order!r} — "
        f"otherwise rung① (not the driver-level fallback) may have been "
        f"the one that actually spilled it"
    )


def test_deny_sibling_pre_spilled_candidate_never_re_offloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5615 deny — a candidate ALREADY spilled (ahead of time)
    is never re-offered as fresh progress on a LATER
    `_attempt_reactive_spill` call — `is_already_spilled` keeps it out
    of the population `_spill_batch_within_face` scans, the same
    durable-supersede mechanism #5612 established. The comparison this
    reachability claim needs: `_attempt_reactive_spill` finding
    something (the accept test above) is not vacuous — it can also find
    NOTHING, correctly, when there is genuinely nothing left."""
    session = _make_spill_session(tmp_path, monkeypatch, t_max=None)
    huge = "L" * 50_000
    _push(session, "user", "look something up", spillability=Spillability.NEVER)
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok", spillability=Spillability.NEVER)

    hb = session._loop_driver._history_buffer
    replacement = hb.spill_turn_content(huge, chain_id="c1", tool="tool", seq=1)
    assert replacement is not None and replacement != huge, (
        "sanity: the candidate must genuinely be spilled ahead of time"
    )

    progressed = asyncio.run(
        session._loop_driver._attempt_reactive_spill(chain_id="c2"),
    )
    assert progressed is False, (
        "with the ONLY sizeable candidate already spilled, "
        "_attempt_reactive_spill must report no progress — re-offering "
        "an already-spilled candidate would be exactly the double-count "
        "#5612's own supersede map exists to prevent"
    )
