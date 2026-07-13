"""Model cost-rate utilities for high-cost model pre-selection warning (#1830).

Provides a pure, side-effect-free lookup of a model's estimated input cost
per 1M tokens from litellm's pricing database (the same DB used by
``pricing.estimate_cost``).

Used by the ``/model`` slash command and session startup to surface a
``model_cost_warn`` event when the chosen model exceeds a configured threshold
— giving the user cost awareness *before* a high-cost model is committed,
rather than after spend has accumulated (which is ``BudgetTracker``'s axis).

Design note (non-duplication):
  - ``BudgetTracker.check_pre_llm`` gates on *cumulative spend* exceeding a
    hard cap.  This module gates on *per-token rate* at *model selection time*.
    The two are orthogonal; this module does not replace or extend BudgetTracker.
"""
from __future__ import annotations


def get_input_cost_per_1m_usd(model: str) -> float | None:
    """Return the estimated input cost per 1M tokens for ``model``.

    Looks up ``litellm.model_cost[model]["input_cost_per_token"]`` and scales
    to USD/1M.  Returns ``None`` when litellm does not carry pricing data for
    the model (unknown / very new model, or a local proxy alias with no entry).

    The function never raises — failures return ``None`` so callers can treat
    an unknown cost as "no warning needed" rather than crashing the session.
    """
    try:
        # perf: route through the single litellm-first-touch chokepoint so the
        # #2929 console-log routing is active regardless of whether this
        # session-start check or a later LLM call is the first real litellm
        # touch in the process (see litellm_bootstrap module docstring).
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready
        ensure_litellm_ready()
        import litellm
        entry = litellm.model_cost.get(model, {})
        per_token = entry.get("input_cost_per_token")
        if per_token is None:
            return None
        return float(per_token) * 1_000_000
    except Exception:
        return None


def is_high_cost_model(model: str, threshold_per_1m_usd: float) -> bool:
    """Return True if ``model``'s input rate exceeds ``threshold_per_1m_usd``.

    Returns False when the rate is unknown (litellm has no entry) — unknown
    cost is not treated as high cost, preserving the current user experience
    for custom or proxy models.
    """
    cost = get_input_cost_per_1m_usd(model)
    return cost is not None and cost > threshold_per_1m_usd
