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
hypothetical. This test drives that EXACT model through the real
`compute_budgets` path (the same function `CompactionEngine` itself uses)
to prove the check fires for a genuinely reachable configuration, not a
synthetic one.

Real collaborators throughout, same idiom as
`tests/runtime/test_4381_pr1_resource_budget_invariant.py` (this check's
own sibling/precedent): `compute_budgets` builds a real `ComputedBudgets`;
only the tiny attribute-passthrough `_Engine`/`_Controller` containers are
hand-built (no logic of their own); `EventLog` + `collect_events` capture
the real subscriber path.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.context_builder import INLINE_CAP_BYTES_PER_TOKEN
from reyn.core.events.events import EventLog
from reyn.runtime.history_tail_reader import COMPACTION_BATCH_MAX_BYTES
from reyn.runtime.services import router_history_buffer as rhb
from reyn.services.compaction.engine import compute_budgets
from tests._support.events import collect_events

# A real, currently-cataloged model whose max_input_tokens (10,485,760)
# is large enough that the shipped component_weights default (25% combined
# head+tail) converts to MORE than COMPACTION_BATCH_MAX_BYTES (8 MiB) --
# confirmed via direct litellm.model_cost inspection, not assumed.
_HUGE_WINDOW_MODEL = "oci/meta.llama-4-scout-17b-16e-instruct"
# An ordinary model whose window is far too small to ever trip this check.
_NORMAL_MODEL = "openai/gpt-4o"


class _Engine:
    def __init__(self, budgets) -> None:
        self.budgets = budgets


class _Controller:
    def __init__(self, engine) -> None:
        self._engine = engine


def _budgets_for(model: str):
    # `get_max_input_tokens` (inside `compute_budgets`) resolves the real
    # litellm catalog value only once litellm itself is warm — deferring
    # otherwise, per `litellm_bootstrap.py`'s own non-blocking design
    # (#4395 PR-2). This test's whole premise depends on the REAL catalog
    # value (10,485,760 for the huge-window model), not the 128K
    # conservative fallback, so force the blocking warm-up first.
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    ensure_litellm_ready()
    return compute_budgets(CompactionConfig(), model, T_SP=2_000, T_comp_SP=500)


def _reset_warned():
    rhb._compaction_batch_budget_warned.clear()


def test_the_huge_window_models_own_budgets_actually_exceed_the_batch_cap():
    """Tier 2: sanity precondition — with the real, shipped defaults, the
    chosen model's own head_budget + tail_budget really does exceed
    COMPACTION_BATCH_MAX_BYTES once converted to bytes. Not assumed."""
    budgets = _budgets_for(_HUGE_WINDOW_MODEL)
    combined_bytes = (budgets.head_budget + budgets.tail_budget) * INLINE_CAP_BYTES_PER_TOKEN
    assert combined_bytes > COMPACTION_BATCH_MAX_BYTES, (
        f"test precondition failed: {_HUGE_WINDOW_MODEL}'s combined "
        f"head+tail budget ({combined_bytes} bytes) does not exceed the "
        f"batch cap ({COMPACTION_BATCH_MAX_BYTES}) -- either the model's "
        "own catalog entry changed, or component_weights' shipped default "
        "changed; this test needs a different model/weights to exercise "
        "the real condition"
    )


def test_a_reachable_large_window_model_emits_the_warning_once():
    """Tier 2: a genuinely reachable configuration (a real, currently-
    selectable model whose own window makes head+tail exceed the batch
    cap) emits compaction_batch_cap_below_head_tail_budget exactly once —
    repeated calls with the SAME (model, phase) do not re-warn (the SSoT
    is called on every trigger resolution)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_for(_HUGE_WINDOW_MODEL)
    controller = _Controller(_Engine(budgets))

    for _ in range(3):
        result = rhb.resolve_effective_trigger_and_budgets(
            controller, _HUGE_WINDOW_MODEL, events,
        )
        assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)

    warnings = [
        e for e in collected
        if e.type == "compaction_batch_cap_below_head_tail_budget"
    ]
    (only,) = warnings
    assert only.data["model"] == _HUGE_WINDOW_MODEL
    assert only.data["phase"] == ""
    assert only.data["head_budget"] == budgets.head_budget
    assert only.data["tail_budget"] == budgets.tail_budget
    assert only.data["combined_bytes"] > only.data["compaction_batch_max_bytes"]


def test_warns_again_for_a_different_phase_same_model():
    """Tier 2: warn-once granularity is per (model, phase), not per model
    alone — a DIFFERENT phase with the same model re-warns."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_for(_HUGE_WINDOW_MODEL)
    controller = _Controller(_Engine(budgets))

    rhb.resolve_effective_trigger_and_budgets(
        controller, _HUGE_WINDOW_MODEL, events, phase="alpha",
    )
    rhb.resolve_effective_trigger_and_budgets(
        controller, _HUGE_WINDOW_MODEL, events, phase="alpha",
    )
    rhb.resolve_effective_trigger_and_budgets(
        controller, _HUGE_WINDOW_MODEL, events, phase="beta",
    )

    warnings = [
        e for e in collected
        if e.type == "compaction_batch_cap_below_head_tail_budget"
    ]
    phases_warned = sorted(e.data["phase"] for e in warnings)
    assert phases_warned == ["alpha", "beta"]


def test_an_ordinary_model_emits_nothing():
    """Tier 2: accept-side twin — an ordinary model's head+tail budget
    stays comfortably under the batch cap; no event fires at all (not a
    present-but-benign one)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_for(_NORMAL_MODEL)
    combined_bytes = (budgets.head_budget + budgets.tail_budget) * INLINE_CAP_BYTES_PER_TOKEN
    assert combined_bytes <= COMPACTION_BATCH_MAX_BYTES, (
        "sanity: this test's own premise -- an ordinary model must NOT "
        "exceed the batch cap"
    )
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _NORMAL_MODEL, events)

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)
    assert not [
        e for e in collected
        if e.type == "compaction_batch_cap_below_head_tail_budget"
    ]


def test_no_events_sink_does_not_raise():
    """Tier 2: events=None (many test/estimation-path callers construct
    without one) — the check runs and detects the violation internally
    but never tries to emit, and never raises for lack of a sink."""
    _reset_warned()
    budgets = _budgets_for(_HUGE_WINDOW_MODEL)
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _HUGE_WINDOW_MODEL, None)

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)
