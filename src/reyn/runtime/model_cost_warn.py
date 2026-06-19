"""Shared helper: high-cost model pre-selection warning (#1830 / FP-0052).

Called from two injection points:
  - ``interfaces/slash/model.py``: on ``/model <class>`` override.
  - ``runtime/session.py``: at session startup (``run()``).

Both sites pass a ``Session`` instance + the model class string. The helper
resolves the class → litellm key, looks up the per-1M-token input cost, and
emits ``model_cost_warn`` when the rate exceeds the configured threshold.

Session-scoped de-dup: ``session._cost_warned_models`` (a ``set[str]``) is
checked and updated so the same model class is warned at most once per session
regardless of how many times the injection points fire.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.session import Session


def maybe_emit_model_cost_warn(
    session: "Session",
    model_class: str,
    *,
    action: str,
) -> None:
    """Emit ``model_cost_warn`` if the resolved model is above threshold.

    Pure warn — never raises, never blocks the caller. Falls back silently on
    any error (missing litellm entry, resolver failure, emit failure) so the
    session startup / model switch always completes.

    ``action`` is surfaced in the event payload so consumers can distinguish
    the trigger context (``"session_start"`` vs ``"model_override"``).
    """
    try:
        cost_warn_cfg = session._config.cost_warn
        if not cost_warn_cfg.enabled:
            return

        warned: set | None = getattr(session, "_cost_warned_models", None)
        if warned is None:
            session._cost_warned_models: set[str] = set()
            warned = session._cost_warned_models
        if model_class in warned:
            return

        resolved_model = session._resolver.resolve(model_class).model

        from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd, is_high_cost_model
        threshold = cost_warn_cfg.model_threshold_per_1m_input_usd
        if not is_high_cost_model(resolved_model, threshold):
            return

        warned.add(model_class)
        cost = get_input_cost_per_1m_usd(resolved_model)
        session._chat_events.emit(
            "model_cost_warn",
            model=resolved_model,
            model_class=model_class,
            cost_per_1m_input_usd=cost,
            threshold_per_1m_input_usd=threshold,
            action=action,
        )
    except Exception:
        pass
