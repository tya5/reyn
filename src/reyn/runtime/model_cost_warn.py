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


async def maybe_block_high_cost_model(
    session: "Session",
    model_class: str,
    *,
    action: str,
) -> bool:
    """Return whether a high-cost model switch may proceed (#1867 / FP-0052 S4).

    Returns ``True`` when the switch should be applied:
      - ``cost_warn`` disabled, or ``block_on_high_cost`` off (warn-only,
        S1–S3 behaviour), or
      - the resolved model is not high-cost, or
      - the user approved the interactive confirm.

    Returns ``False`` to BLOCK the switch (the caller must NOT apply it):
      - block enabled + high-cost + the user declined the confirm, or
      - block enabled + high-cost + non-interactive session — fail-closed: a
        confirm cannot be shown, so a costly switch is denied rather than
        applied silently (#1867 Q2). Operators who run high-cost models
        head-less keep ``block_on_high_cost=False`` (warn-only).

    The confirm routes through the unified safety framework via
    ``session._handle_chat_limit_checkpoint`` (= the ``handle_limit_exceeded``
    wrapper) — no bespoke block path.

    Unexpected errors fail OPEN (return ``True``): the model-cost confirm is an
    advisory cost-UX convenience, not a security boundary (the budget caps
    remain the real spend backstop), so a gate bug must never wedge ``/model``.
    The *explicit* non-interactive case above is the one place this fails closed.
    """
    try:
        cost_warn_cfg = session._config.cost_warn
        if not cost_warn_cfg.enabled or not cost_warn_cfg.block_on_high_cost:
            return True

        from reyn.llm.model_cost_rate import (
            get_input_cost_per_1m_usd,
            is_high_cost_model,
        )

        resolved_model = session._resolver.resolve(model_class).model
        threshold = cost_warn_cfg.model_threshold_per_1m_input_usd
        if not is_high_cost_model(resolved_model, threshold):
            return True

        cost = get_input_cost_per_1m_usd(resolved_model)
        cost_str = f"${cost:.2f}/1M input tokens" if cost is not None else "high-cost"

        if getattr(session, "_non_interactive", False):
            session._chat_events.emit(
                "model_cost_block",
                model=resolved_model,
                model_class=model_class,
                cost_per_1m_input_usd=cost,
                action=action,
                reason="non_interactive_fail_closed",
            )
            return False

        decision = await session._handle_chat_limit_checkpoint(
            kind="cost.high_cost_model",
            prompt=f"Switch to high-cost model {resolved_model} ({cost_str})?",
            detail=f"model_class={model_class}",
            extension_amount=1.0,
        )
        allow = bool(getattr(decision, "allow_continue", False))
        session._chat_events.emit(
            "model_cost_block",
            model=resolved_model,
            model_class=model_class,
            cost_per_1m_input_usd=cost,
            action=action,
            reason="approved" if allow else "declined",
        )
        return allow
    except Exception:
        return True
