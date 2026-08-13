"""Tier 2: #4477 — the 4th instance of #4381 PR-1's resource/budget
comparison class: `resolve_effective_trigger_and_budgets`'s
`head_budget + tail_budget` (a BUDGET bound, model-context-window-derived)
vs `history_tail_reader.COMPACTION_BATCH_MAX_BYTES` (a RESOURCE bound,
#4472/#4475's compaction-batch byte cap).

Reachability confirmed LIVE before this check was written (lead-coder's
explicit dispatch: "the first task is measuring, not implementing a warn"):
with the shipped `component_weights` default (head=10, tail=15, of 100
total -> 25% of `main_pool` combined) and `INLINE_CAP_BYTES_PER_TOKEN=4`,
the worst-case `head_budget + tail_budget` in BYTES equals the model's own
`max_input_tokens` numerically (0.25 x 4 = 1). At least 5 models in this
repo's installed litellm catalog exceed `COMPACTION_BATCH_MAX_BYTES`
(8 MiB) at this weighting -- e.g. `oci/meta.llama-4-scout-17b-16e-instruct`
at 10,485,760 max_input_tokens, a real, currently-selectable model, not a
hypothetical.

**Split per lead-coder's PR review** (the precondition test below is the
ONLY one that touches the real litellm catalog): the reachability WITNESS
(does this actually happen in reality) is a separate concern from the
BEHAVIOR under test (reyn's own warn-once logic). The precondition test
IS #4477's entire justification and stays real-catalog-dependent on
purpose. The behavior tests (warn fires once, per-phase granularity,
accept-side silence, no-sink safety) are reyn's own logic, not litellm's
catalog content -- keeping them real-catalog-dependent would mean (a) a
third party changing that model's advertised window could turn reyn's own
correct logic red (the "third-party's property" hazard CLAUDE.md's test
review names), and (b) adding a second CI exposure to the exact blocking
litellm-warm-up class #4395 fixed earlier the same night. Rewritten to
drive synthetic, hand-built `ComputedBudgets` instead -- the subject under
test is reyn's own comparison + warn-once cache, which needs a
`head_budget`/`tail_budget` pair, not a real model resolution.

Real collaborators throughout for what's actually under test: `EventLog` +
`collect_events` capture the real subscriber path; `_Engine`/`_Controller`
are the same tiny attribute-passthrough containers
`test_4381_pr1_resource_budget_invariant.py` already established (no logic
of their own).
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.context_builder import INLINE_CAP_BYTES_PER_TOKEN
from reyn.core.events.events import EventLog
from reyn.runtime.history_tail_reader import COMPACTION_BATCH_MAX_BYTES
from reyn.runtime.services import router_history_buffer as rhb
from reyn.services.compaction.engine import ComputedBudgets, compute_budgets
from tests._support.events import collect_events

# A real, currently-cataloged model whose max_input_tokens (10,485,760) is
# large enough that the shipped component_weights default (25% combined
# head+tail) converts to MORE than COMPACTION_BATCH_MAX_BYTES (8 MiB) --
# confirmed via direct litellm.model_cost inspection, not assumed. Used
# ONLY by the precondition/reachability test below.
_HUGE_WINDOW_MODEL = "oci/meta.llama-4-scout-17b-16e-instruct"
_ORDINARY_MODEL = "openai/gpt-4o"

# Synthetic budgets for the behavior tests -- deliberately round numbers,
# not derived from any real model. Combined = 3,000,000 tokens *
# INLINE_CAP_BYTES_PER_TOKEN(4) = 12,000,000 bytes > COMPACTION_BATCH_MAX_
# BYTES (8,388,608) -- exceeds by construction, not by coincidence.
_EXCEEDING_BUDGETS = ComputedBudgets(
    main_pool=5_000_000, head_budget=2_000_000, body_budget=1_000_000,
    tail_budget=1_000_000, new_msg_budget=500_000, B_M=4_000_000,
    main_M_room=1_500_000, effective_trigger=1_500_000,
)
# Combined = 200 tokens * 4 = 800 bytes, comfortably under the cap.
_UNDER_BUDGETS = ComputedBudgets(
    main_pool=1_000, head_budget=100, body_budget=400,
    tail_budget=100, new_msg_budget=200, B_M=800,
    main_M_room=700, effective_trigger=700,
)


class _Engine:
    def __init__(self, budgets) -> None:
        self.budgets = budgets


class _Controller:
    def __init__(self, engine) -> None:
        self._engine = engine


def _reset_warned():
    rhb._compaction_batch_budget_warned.clear()


def test_the_huge_window_models_own_budgets_actually_exceed_the_batch_cap():
    """Tier 2: the SOLE real-litellm-catalog-dependent test in this file —
    the reachability WITNESS #4477's own existence depends on. Confirms,
    against the REAL installed catalog and the REAL compute_budgets path
    (the same function CompactionEngine itself uses), that a real,
    currently-selectable model's own head_budget + tail_budget genuinely
    exceeds COMPACTION_BATCH_MAX_BYTES once converted to bytes. Not
    assumed, and deliberately the ONLY place in this file that pays the
    real litellm-catalog-resolution cost."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    # get_max_input_tokens (inside compute_budgets) resolves the real
    # litellm catalog value only once litellm itself is warm -- deferring
    # otherwise, per litellm_bootstrap.py's own non-blocking design (#4395
    # PR-2). This precondition's whole point depends on the REAL catalog
    # value (10,485,760), not the 128K conservative fallback, so force the
    # blocking warm-up -- acceptable ONLY here, where paying that cost is
    # the entire point of the test.
    ensure_litellm_ready()
    budgets = compute_budgets(CompactionConfig(), _HUGE_WINDOW_MODEL, T_SP=2_000, T_comp_SP=500)
    combined_bytes = (budgets.head_budget + budgets.tail_budget) * INLINE_CAP_BYTES_PER_TOKEN
    assert combined_bytes > COMPACTION_BATCH_MAX_BYTES, (
        f"test precondition failed: {_HUGE_WINDOW_MODEL}'s combined "
        f"head+tail budget ({combined_bytes} bytes) does not exceed the "
        f"batch cap ({COMPACTION_BATCH_MAX_BYTES}) -- either the model's "
        "own catalog entry changed, or component_weights' shipped default "
        "changed; this test needs a different model/weights to exercise "
        "the real condition"
    )


def test_a_reachable_configuration_emits_the_warning_once():
    """Tier 2: synthetic budgets whose combined head+tail footprint
    exceeds the batch cap by construction — the SAME shape the
    precondition test above proved is reachable, but driven through
    reyn's own logic without touching the real litellm catalog (the
    subject under test here is the warn-once cache, not litellm's
    catalog content). Emits compaction_batch_cap_below_head_tail_budget
    exactly once — repeated calls with the SAME (model, phase) do not
    re-warn (the SSoT is called on every trigger resolution)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    controller = _Controller(_Engine(_EXCEEDING_BUDGETS))

    for _ in range(3):
        result = rhb.resolve_effective_trigger_and_budgets(
            controller, _ORDINARY_MODEL, events,
        )
        assert result == (
            _EXCEEDING_BUDGETS.effective_trigger,
            _EXCEEDING_BUDGETS.head_budget,
            _EXCEEDING_BUDGETS.tail_budget,
        )

    warnings = [
        e for e in collected
        if e.type == "compaction_batch_cap_below_head_tail_budget"
    ]
    (only,) = warnings
    assert only.data["model"] == _ORDINARY_MODEL
    assert only.data["phase"] == ""
    assert only.data["head_budget"] == _EXCEEDING_BUDGETS.head_budget
    assert only.data["tail_budget"] == _EXCEEDING_BUDGETS.tail_budget
    assert only.data["combined_bytes"] > only.data["compaction_batch_max_bytes"]


def test_warns_again_for_a_different_phase_same_model():
    """Tier 2: warn-once granularity is per (model, phase), not per model
    alone — a DIFFERENT phase with the same synthetic exceeding budgets
    re-warns."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    controller = _Controller(_Engine(_EXCEEDING_BUDGETS))

    rhb.resolve_effective_trigger_and_budgets(
        controller, _ORDINARY_MODEL, events, phase="alpha",
    )
    rhb.resolve_effective_trigger_and_budgets(
        controller, _ORDINARY_MODEL, events, phase="alpha",
    )
    rhb.resolve_effective_trigger_and_budgets(
        controller, _ORDINARY_MODEL, events, phase="beta",
    )

    warnings = [
        e for e in collected
        if e.type == "compaction_batch_cap_below_head_tail_budget"
    ]
    phases_warned = sorted(e.data["phase"] for e in warnings)
    assert phases_warned == ["alpha", "beta"]


def test_budgets_under_the_cap_emit_nothing():
    """Tier 2: accept-side twin — synthetic budgets whose combined
    head+tail footprint stays comfortably under the batch cap; no event
    fires at all (not a present-but-benign one)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    controller = _Controller(_Engine(_UNDER_BUDGETS))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _ORDINARY_MODEL, events)

    assert result == (
        _UNDER_BUDGETS.effective_trigger,
        _UNDER_BUDGETS.head_budget,
        _UNDER_BUDGETS.tail_budget,
    )
    assert not [
        e for e in collected
        if e.type == "compaction_batch_cap_below_head_tail_budget"
    ]


def test_no_events_sink_does_not_raise():
    """Tier 2: events=None (many test/estimation-path callers construct
    without one) — the check runs and detects the violation internally
    but never tries to emit, and never raises for lack of a sink."""
    _reset_warned()
    controller = _Controller(_Engine(_EXCEEDING_BUDGETS))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _ORDINARY_MODEL, None)

    assert result == (
        _EXCEEDING_BUDGETS.effective_trigger,
        _EXCEEDING_BUDGETS.head_budget,
        _EXCEEDING_BUDGETS.tail_budget,
    )
