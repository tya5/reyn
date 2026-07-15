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
        offload_config: Any = None,  # OffloadConfig — tool-result-schema-redesign §5
    ) -> None:
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._media_store = media_store
        self._model_fn = model_fn
        self._events = events
        self._history_fn = history_fn
        from reyn.config.chat import OffloadConfig
        self._offload_config = offload_config if offload_config is not None else OffloadConfig()
        # #2940: incremental token-estimate cache. Re-dumping the full router-view
        # history on every call (dropdown open, pre-frame overflow check) is
        # O(history size) each time and grows unbounded over a long session.
        # Cache is (history length, cumulative estimated tokens) keyed by
        # (model, use_chars4); a length decrease (compaction/rewind truncated
        # history) or a model/config change invalidates it (full recompute),
        # a length increase only dumps+estimates the NEW tail slice.
        # "boundary" is a hash of the last cached message's json dump — the
        # cache is an append-only-PREFIX assumption, but history_fn is
        # typically a derived, recomputed view (e.g. Session._active_branch_
        # history — the same function #2938 hoisted), not a real array: a
        # rewind can change WHICH messages are active such that len returns
        # to a previously-cached value while the actual content at that
        # boundary differs. Checking length alone would then silently return
        # a stale cached total (never re-synced until a later length
        # decrease). The boundary hash makes this checked, not assumed.
        self._history_token_cache: dict[str, Any] = {
            "len": 0, "tokens": 0, "model": None, "use_chars4": None, "boundary": None,
        }

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

    def _incremental_history_tokens(self) -> int:
        """Estimated token count of the full router-view history (#2940).

        Incremental: only the slice of history NEWER than the last call is
        json.dumps'd + estimated; a cache hit (no new messages since last
        call) is O(1). A shrink, a model/use_chars4 change, OR the cached
        PREFIX's content actually differing (checked via a boundary hash,
        not assumed from length alone — history_fn is often a recomputed
        derived view, e.g. a rewind-aware active-branch filter, where the
        same length can recur with different content) invalidates the cache
        for one full recompute.
        """
        import json as _json

        from reyn.services.compaction.engine import estimate_tokens

        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
        cache = self._history_token_cache
        try:
            history = self._history_fn()
            n = len(history)
            cached_len = cache["len"]
            # The boundary is the json dump of the LAST message that was in
            # the cached prefix (index cached_len - 1). If it no longer
            # matches the message currently at that index, the prefix isn't
            # append-only-stable and the cache cannot be trusted, even at an
            # unchanged or grown length.
            boundary = (
                _json.dumps(history[cached_len - 1], ensure_ascii=False)
                if 0 < cached_len <= n
                else None
            )
            prefix_unchanged = cached_len == 0 or (cached_len <= n and boundary == cache["boundary"])
            if (
                cached_len > n
                or cache["model"] != self._model
                or cache["use_chars4"] != use_chars4
                or not prefix_unchanged
            ):
                combined = _json.dumps(history, ensure_ascii=False)
                tokens = estimate_tokens(combined, self._model, use_chars4=use_chars4)
            elif cached_len == n:
                return int(cache["tokens"])
            else:
                # Dump each new message individually and join with the same
                # comma separator json.dumps(list) would use BETWEEN elements
                # (not wrapped in its own "[...]") — an array-slice dump would
                # otherwise add a spurious pair of bracket tokens on every
                # single call, drifting the cumulative estimate upward as the
                # history grows across many dropdown-open/pre-frame checks.
                delta = history[cached_len:]
                combined = ",".join(_json.dumps(m, ensure_ascii=False) for m in delta)
                tokens = int(cache["tokens"]) + estimate_tokens(combined, self._model, use_chars4=use_chars4)
            new_boundary = _json.dumps(history[-1], ensure_ascii=False) if n > 0 else None
            cache.update(
                len=n, tokens=tokens, model=self._model, use_chars4=use_chars4, boundary=new_boundary,
            )
            return tokens
        except Exception:  # noqa: BLE001 — estimation best-effort
            return 0

    def _free_window_now(self) -> tuple[int, int]:
        """Return (effective_trigger, estimated_history_tokens).

        Used by context_window_status and maybe_force_compact.
        """
        effective_trigger = self._get_effective_trigger()
        estimated = self._incremental_history_tokens()
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

        No-op when no media_store is configured, or when ``offload.enabled: false``
        (tool-result-schema-redesign §5 debug lever — never truncate). ``content_str``
        is the canonical ``text`` body (#2425 案B) — already the clean payload, so the
        stored body is it as-is and the inline is a bounded plain-text preview.
        """
        if not self._offload_config.enabled:
            return content_str
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

    def media_followup_budget(self, tool_content: str) -> "int | None":
        """Tokens left for media after capped tool text (#272 media axis).

        ``None`` (unbounded) when ``offload.enabled: false`` (tool-result-schema-
        redesign §5) — the media gate is one of the three size gates the debug
        lever disables, so the opt-out isn't confounded by media starvation.
        """
        if not self._offload_config.enabled:
            return None
        from reyn.services.compaction.engine import estimate_tokens

        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
        text_tokens = estimate_tokens(tool_content, self._model, use_chars4=use_chars4)
        return max(0, self.per_turn_cap_tokens() - text_tokens)

    def _effective_trigger_source(self) -> str:
        """Where effective_trigger's underlying window size came from (status-bar
        ctx chip detail). The engine-budget path subdivides T_max by configured
        component weights, but T_max itself is always resolved via
        get_max_input_tokens — so the root source is the same two-way split
        either way (litellm catalog vs reyn's fallback default)."""
        from reyn.llm.model_budget import get_max_input_tokens_source
        return get_max_input_tokens_source(self._model)

    def context_window_status(self) -> dict:
        """Live exact-token context budget for the SP context-size signal.

        Returns {free_window, effective_trigger, source}.
        """
        effective_trigger, used = self._free_window_now()
        return {
            "free_window": max(0, effective_trigger - used),
            "effective_trigger": effective_trigger,
            "source": self._effective_trigger_source(),
        }

    def raw_context_window(self) -> dict:
        """The model's ACTUAL context window (get_max_input_tokens), distinct
        from ``context_window_status``'s ``effective_trigger`` — that value is
        already reduced by SP/head/tail/component-weight budgeting (an
        internal compaction-trigger threshold, not the model's real limit).
        For a user-facing "how close to the model's hard limit" display
        (status-bar ctx chip), the denominator should be this raw figure.

        Returns {window, source}.
        """
        from reyn.llm.model_budget import get_max_input_tokens, get_max_input_tokens_source
        model = self._model
        return {
            "window": get_max_input_tokens(model, events=self._events),
            "source": get_max_input_tokens_source(model),
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
        from reyn.services.compaction.engine import (
            NewMsgExceedsBudgetError,
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

        # Quick estimate of the current history slice (#2940: incremental, not a
        # full re-dump every call — shares the cache with context_window_status).
        estimated = self._incremental_history_tokens()

        if estimated > effective_trigger:
            self._events.emit(
                "compaction_check",
                outcome="pre_frame_overflow",
                estimated_tokens=estimated,
                effective_trigger=effective_trigger,
            )
            await self._compaction_controller.force_compact_now()
