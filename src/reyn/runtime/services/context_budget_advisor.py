"""ContextBudgetAdvisor — per-turn token budget computation for Session.

Owns the five budget-arithmetic methods:

  - cap_tool_result       — truncate oversized tool-result text (#1128 size axis)
  - per_turn_cap_tokens   — derive B_M-relative per-turn token ceiling
  - media_followup_budget — tokens left for media after capped text (#272)
  - context_window_status — {free_window, effective_trigger} for SP header
  - maybe_force_compact   — pre-frame overflow guard (#1128)

Plus the shared helper:

  - _free_window_now      — (effective_trigger, estimated_history_tokens)

All public methods are pure or only cause contained async side effects on
the compaction controller.  Session holds an instance and forwards each
method as a callback to RouterHostAdapter.

history_fn dependency: a zero-arg callable that returns the current
router-view history (list of dicts).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass


class ContextBudgetAdvisor:
    """Per-turn token budget advisor for the chat router loop.

    Constructed once per Session; passed as callbacks to RouterHostAdapter.
    """

    def __init__(
        self,
        *,
        compaction: Any,             # CompactionConfig — use_chars4_estimate
        compaction_controller: Any,  # for engine.budgets + force_compact_now
        media_store: Any,            # MediaStore | None — for cap_tool_result
        model_fn: Callable[[], str],  # zero-arg → CURRENT resolved model (#1752)
        events: Any,                 # EventLog — for fallback budget + emit
        history_fn: Callable[[], list],  # zero-arg → current router-view history
    ) -> None:
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._media_store = media_store
        self._model_fn = model_fn
        self._events = events
        self._history_fn = history_fn

    @property
    def _model(self) -> str:
        # #1752: resolve the model live each call so a /model override (which can
        # change the context window) is reflected in budgeting. The session-side
        # fn resolves the class → litellm string; without this the advisor would
        # budget against the construction-time model after a /model switch.
        return self._model_fn()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_effective_trigger(self) -> int:
        """Derive effective_trigger from engine budgets or fallback."""
        controller = self._compaction_controller
        engine = getattr(controller, "_engine", None) if controller is not None else None
        budgets = getattr(engine, "budgets", None)
        if budgets is not None:
            return budgets.effective_trigger
        from reyn.llm.model_budget import get_max_input_tokens
        return get_max_input_tokens(self._model, events=self._events)

    def _free_window_now(self) -> tuple[int, int]:
        """Return (effective_trigger, estimated_history_tokens).

        Used by context_window_status and maybe_force_compact.
        """
        import json as _json

        from reyn.services.compaction.engine import estimate_tokens

        effective_trigger = self._get_effective_trigger()
        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
        try:
            combined = _json.dumps(self._history_fn(), ensure_ascii=False)
            estimated = estimate_tokens(combined, self._model, use_chars4=use_chars4)
        except Exception:  # noqa: BLE001 — estimation best-effort
            estimated = 0
        return effective_trigger, estimated

    # ── Public API ────────────────────────────────────────────────────────────

    def per_turn_cap_tokens(self) -> int:
        """B_M-relative per-turn cap (#1128/#272).

        Derived from engine budgets; falls back to max_input_tokens.
        """
        from reyn.runtime.services.tool_result_cap import compute_cap_tokens
        return compute_cap_tokens(self._get_effective_trigger())

    def cap_tool_result(self, content_str: str) -> str:
        """Cap an oversized chat tool result (#1128 size axis).

        No-op when no media_store is configured.
        """
        store = self._media_store
        if store is None:
            return content_str
        from reyn.runtime.services.tool_result_cap import cap_tool_result_content

        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
        return cap_tool_result_content(
            content_str,
            cap_tokens=self.per_turn_cap_tokens(),
            model=self._model,
            save_fn=store.save_tool_result,
            use_chars4=use_chars4,
            events=self._events,
        )

    def media_followup_budget(self, tool_content: str) -> int:
        """Tokens left for media after capped tool text (#272 media axis)."""
        from reyn.services.compaction.engine import estimate_tokens

        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
        text_tokens = estimate_tokens(tool_content, self._model, use_chars4=use_chars4)
        return max(0, self.per_turn_cap_tokens() - text_tokens)

    def context_window_status(self) -> dict:
        """Live exact-token context budget for the SP context-size signal.

        Returns {free_window, effective_trigger}.
        """
        effective_trigger, used = self._free_window_now()
        return {
            "free_window": max(0, effective_trigger - used),
            "effective_trigger": effective_trigger,
        }

    async def maybe_force_compact(
        self,
        *,
        new_msg_text: str | None = None,
    ) -> None:
        """Pre-frame context-overflow guard (PR-N3).

        Recomputes budgets, checks new_msg_budget (axis 11), and runs
        force_compact_now() if the current history exceeds effective_trigger.
        """
        import json as _json

        from reyn.services.compaction.engine import (
            NewMsgExceedsBudgetError,
            estimate_tokens,
            estimate_tokens_for_turn,
        )

        # ISSUE #4: re-measure T_SP dynamically.
        engine = self._compaction_controller._engine
        try:
            engine.recompute_budgets()
        except Exception:
            pass

        engine = self._compaction_controller._engine
        budgets = getattr(engine, "budgets", None)
        if budgets is not None:
            effective_trigger = budgets.effective_trigger
            new_msg_budget = budgets.new_msg_budget
        else:
            from reyn.llm.model_budget import get_max_input_tokens
            effective_trigger = get_max_input_tokens(self._model, events=self._events)
            new_msg_budget = effective_trigger

        # Axis 11 (ISSUE #5): check new_msg_budget before estimating history.
        if new_msg_text is not None:
            new_msg_turn = {"role": "user", "content": new_msg_text}
            use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
            new_msg_tokens = estimate_tokens_for_turn(
                new_msg_turn, self._model, use_chars4=use_chars4
            )
            if new_msg_tokens > new_msg_budget:
                self._events.emit(
                    "new_msg_exceeds_budget",
                    new_msg_tokens=new_msg_tokens,
                    new_msg_budget=new_msg_budget,
                )
                raise NewMsgExceedsBudgetError(
                    new_msg_tokens=new_msg_tokens,
                    new_msg_budget=new_msg_budget,
                )

        # Quick estimate of the current history slice.
        try:
            history_msgs = self._history_fn()
            combined = _json.dumps(history_msgs, ensure_ascii=False)
            use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
            estimated = estimate_tokens(combined, self._model, use_chars4=use_chars4)
        except Exception:
            return

        if estimated > effective_trigger:
            self._events.emit(
                "compaction_check",
                outcome="pre_frame_overflow",
                estimated_tokens=estimated,
                effective_trigger=effective_trigger,
            )
            await self._compaction_controller.force_compact_now()
