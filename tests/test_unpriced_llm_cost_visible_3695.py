"""#3695 — an unpriced model's calls stay distinguishable from free ones.

The owner reported the status bar's cost figure never moving: it showed an old
hydrated value all day and never grew. Measured cause, in ``record_llm``:

    estimate_cost("gpt-5.6-luna", usage) -> (None, None)     # unpriced
    estimate_cost("gpt-4o",       usage) -> (0.0075, {...})  # priced

``cost_usd = cost_usd or 0.0`` folded that None to zero, so every call added
exactly nothing to a total that still presented itself as the amount spent.
Two lines further down the SAME function skips the cache breakdown for an
unpriced model rather than treating unknown as free — the same fact handled
two ways within one function. ``pricing.py`` states the rule outright
("unknown != free"), and the embedding path already applies the mechanism
(``EmbeddingCost.unpriced_calls``, "visible, not a silent $0.00"). The LLM
path did not.

Every gate here pairs the unpriced case with a PRICED CONTROL. Without one,
"the cost did not grow" passes for a harness that never reached the
accumulation at all — which is exactly how the first attempt at measuring
this went (a stub above ``record_llm`` made both models look frozen).
"""
from __future__ import annotations

import logging

from reyn.llm.pricing import TokenUsage, estimate_cost
from reyn.runtime.budget.budget import BudgetTracker, CostConfig

_PRICED = "gpt-4o"
_UNPRICED = "reyn-test-model-with-no-price"
_USAGE = TokenUsage(prompt_tokens=1000, completion_tokens=500)


def _tracker() -> BudgetTracker:
    return BudgetTracker(CostConfig())


def test_the_two_models_this_module_relies_on_really_are_priced_and_not() -> None:
    """Tier 2: the premise every other gate here rests on, asserted directly.

    If the "unpriced" model ever gained a price, or the priced one lost its
    entry, the rest of this module would keep passing while testing nothing.
    """
    assert estimate_cost(_PRICED, _USAGE)[0] is not None
    assert estimate_cost(_UNPRICED, _USAGE)[0] is None


def test_an_unpriced_call_is_counted_while_a_priced_one_is_not() -> None:
    """Tier 2: the unpriced counter tracks exactly the calls with no price."""
    priced, unpriced = _tracker(), _tracker()

    for _ in range(3):
        priced.record_llm(model=_PRICED, agent="a", usage=_USAGE)
        unpriced.record_llm(model=_UNPRICED, agent="a", usage=_USAGE)

    assert unpriced.agent_unpriced_calls("a") == 3
    assert priced.agent_unpriced_calls("a") == 0, (
        "a priced call must not be reported as one whose cost is unknown"
    )


def test_the_unpriced_total_is_a_lower_bound_the_reader_can_detect() -> None:
    """Tier 2: the owner's symptom, and the signal that now distinguishes it.

    Cost frozen at zero while tokens climb is INDISTINGUISHABLE from a genuinely
    free model until something says the price was unknown. Both halves are
    asserted: the frozen cost (the symptom, still true — this change does not
    invent a price) and the counter (the part that makes it legible).
    """
    tracker = _tracker()

    for _ in range(3):
        tracker.record_llm(model=_UNPRICED, agent="a", usage=_USAGE)

    assert tracker.agent_tokens("a") > 0, "tokens are independent of pricing"
    assert tracker.agent_cost_usd("a") == 0.0
    assert tracker.agent_unpriced_calls("a") > 0, (
        "cost 0.00 with no unpriced signal reads as 'these calls were free'"
    )


def test_a_priced_agent_reports_a_cost_that_grows() -> None:
    """Tier 2: the control — the same harness, on a priced model, accumulates.

    This is what makes the assertions above non-vacuous: if the harness never
    reached the accumulation, this test fails rather than the others quietly
    passing.
    """
    tracker = _tracker()

    tracker.record_llm(model=_PRICED, agent="a", usage=_USAGE)
    after_one = tracker.agent_cost_usd("a")
    tracker.record_llm(model=_PRICED, agent="a", usage=_USAGE)

    assert after_one > 0.0
    assert tracker.agent_cost_usd("a") > after_one


def test_mixing_priced_and_unpriced_keeps_both_facts() -> None:
    """Tier 2: a real cost AND a count of what is missing from it.

    The realistic case is an agent that used a priced model and then switched.
    Reporting only one of the two would either hide the spend or hide the gap.
    """
    tracker = _tracker()

    tracker.record_llm(model=_PRICED, agent="a", usage=_USAGE)
    tracker.record_llm(model=_UNPRICED, agent="a", usage=_USAGE)

    assert tracker.agent_cost_usd("a") > 0.0
    assert tracker.agent_unpriced_calls("a") == 1


def test_agents_do_not_share_an_unpriced_count() -> None:
    """Tier 2: the counter is per agent, like the cost it qualifies.

    A count filed under the wrong key would attach the caveat to an agent whose
    figure is complete, and drop it from the one whose figure is not.
    """
    tracker = _tracker()

    tracker.record_llm(model=_UNPRICED, agent="a", usage=_USAGE)
    tracker.record_llm(model=_PRICED, agent="b", usage=_USAGE)

    assert tracker.agent_unpriced_calls("a") == 1
    assert tracker.agent_unpriced_calls("b") == 0


def test_an_unpriced_model_is_reported_where_a_reader_who_did_not_ask_sees_it(
    caplog,
) -> None:
    """Tier 2: the warning fires for an unpriced model, and not for a priced one.

    The counter serves a reader that knows to ask. The embedding path's
    equivalent counter is read by nothing under ``interfaces/``, so a counter
    alone would repeat that silence.
    """
    import reyn.runtime.budget.budget as budget_mod

    budget_mod._UNPRICED_MODELS_WARNED.discard(_UNPRICED)
    with caplog.at_level(logging.WARNING, logger=budget_mod.__name__):
        _tracker().record_llm(model=_UNPRICED, agent="a", usage=_USAGE)
        unpriced_text = caplog.text
        caplog.clear()
        _tracker().record_llm(model=_PRICED, agent="a", usage=_USAGE)
        priced_text = caplog.text

    assert _UNPRICED in unpriced_text
    assert _PRICED not in priced_text, (
        "a priced model must not be reported as having an unknown price"
    )
