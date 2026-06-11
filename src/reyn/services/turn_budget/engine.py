"""Cumulative-axis turn-budget engine (#1092 force-close + handoff, PR-A foundation).

Mirrors ``services/compaction/engine.py`` field-for-field where it can:

- a dedicated, axis-independent system prompt (the **wrap-up SP**) — sibling of
  the compaction summariser SP. Its token cost (``T_wrap_SP``) is measured once
  at engine init, the same way compaction measures ``T_comp_SP``.
- a pure budget derivation (``compute_turn_budget``) + a fail-fast bounds check
  (``assert_turn_budget_bounds``) — the same split as compaction's
  ``compute_budgets`` / ``assert_static_bounds``.

The headroom (§5 of #1092) is a two-layer design. This module provides **layer 1**
(the derived 目安): the largest accumulated current-turn *content* (history,
measured WITHOUT the system prompt — the SP is swapped for the wrap-up SP at
force-close time) that still leaves room for (a) one more normal increment
(bounded by the per-term ``offload_cap``), (b) the wrap-up SP, and (c) the
wrap-up call's own generated output (``output_reserve``)::

    force_close_threshold = T_max − T_wrap_SP − output_reserve − offload_cap

When the accumulated content reaches that threshold, the OS force-closes rather
than risk the next turn overflowing. **Layer 2** (the precision-independent
guarantee: a wrap-up call that still overflows → compaction shrink → retry,
monotonic) is NOT in this foundation PR — it lands with the force-close call
(PR-B). The subtraction here is a disposable estimate; the retry is the body.

NO wiring lives here: the per-turn trigger hook (PR-C), the force-close call
(PR-B), and the handoff persist/re-entry (PR-D) consume this service through the
shared ``RouterLoop`` so chat/plan/phase share one implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from reyn.services.compaction.engine import estimate_tokens

if TYPE_CHECKING:
    from reyn.llm.model_resolver import ModelResolver


# Axis-independent (P7-clean) + field-agnostic (P8-clean): the prompt switches
# the model's role to "consolidate and stop", but does NOT name any skill,
# phase, or artifact type, and does NOT enumerate the output schema or describe
# the Control IR format — the OS injects the finish-only candidate at call time
# (the same seam the rest of the OS uses), and the per-axis "what was done so
# far" context is injected separately at force-close time (§8: one SP + per-axis
# context injection). Sibling of compaction's summariser SP.
_WRAP_UP_SYSTEM_PROMPT = """\
You are being asked to WRAP UP the current unit of work. Do NOT continue the \
task and do NOT request or call any further tools or operations. Your only job \
now is to consolidate what has happened so far into a single, final hand-off \
so a fresh continuation can pick the work up without re-reading the raw history.

Cover, compactly:

- What is DONE — the essential conclusions, findings, and results produced so \
far, distilled as knowledge. Summarise large inputs you read down to what \
matters; do not paste their contents back.
- Where the OUTPUTS live — reference any files or stored artifacts by their \
location rather than inlining large data.
- What REMAINS — the next concrete step(s) still needed to finish.
- What must NOT be repeated — actions already completed that a continuation \
should not redo.

Keep it concise and self-contained, and prefer references over large inline \
content. This consolidation replaces the raw working history for the next step, \
so anything omitted here is lost: capture the essence, not the volume."""


def wrap_up_system_prompt() -> str:
    """The axis-independent wrap-up system prompt (the single SP of §8).

    Exposed as a function (not just the constant) so callers depend on a stable
    surface and a future templated variant stays source-compatible.
    """
    return _WRAP_UP_SYSTEM_PROMPT


@dataclass(frozen=True)
class TurnBudget:
    """Derived layer-1 headroom for one (model, config) context.

    Computed once per engine init. All token counts are in the model's tokens.
    """

    max_input: int            # T_max — model input budget (excludes output)
    T_wrap_SP: int            # wrap-up SP cost (the binding SP at wrap-up time)
    output_reserve: int       # tokens reserved for the wrap-up call's OUTPUT
    offload_cap: int          # max tokens one post-offload increment can add
    force_close_threshold: int  # max accumulated turn-content tokens before close

    @property
    def progress_margin(self) -> int:
        """#1092 PR-E: tokens of NEW-work head-room a force-close re-entry has —
        i.e. ``force_close_threshold − output_reserve − offload_cap`` (the
        consolidation is hard-capped ≤ output_reserve; offload_cap is one working
        increment). The by-construction termination guarantee is exactly
        ``progress_margin > 0``: each re-entry makes a full increment of new
        progress below threshold, so a finite-work phase converges in FEW
        re-entries. Enforced at construction by :func:`assert_turn_budget_bounds`."""
        return self.force_close_threshold - self.output_reserve - self.offload_cap


def compute_turn_budget(
    model: str,
    *,
    T_wrap_SP: int,
    output_reserve: int,
    offload_cap: int,
    resolver: "ModelResolver | None" = None,
) -> TurnBudget:
    """Derive the layer-1 force-close threshold (§5) — pure, no I/O beyond the
    model catalog lookup.

    Parameters
    ----------
    model:
        Model class ("standard"/"light"/...) or a literal LiteLLM string.
        Resolved to the LiteLLM string before the catalog lookup (#1172: an
        unresolved class makes ``get_max_input_tokens`` fall back to the 128K
        default and silently mis-budget — the same trap compaction hit).
    T_wrap_SP:
        Token cost of the wrap-up system prompt (measured by the engine via
        ``estimate_tokens``). It is the SP present during the wrap-up call, so
        it is the binding SP term in the threshold.
    output_reserve:
        Tokens reserved for the wrap-up call's generated consolidation.
    offload_cap:
        Upper bound on the tokens a single post-offload increment (one more
        normal turn's new content) can add — this is what makes "one more turn"
        a finite, known quantity (size axis = #1093 / ``services/offload/``).

    Returns
    -------
    TurnBudget with ``force_close_threshold = T_max − T_wrap_SP −
    output_reserve − offload_cap``. The threshold is NOT clamped here (callers
    validate via :func:`assert_turn_budget_bounds`); a non-positive threshold
    signals a misconfiguration, not a runtime state.
    """
    from reyn.llm.model_budget import get_max_input_tokens

    if resolver is None:
        from reyn.llm.model_resolver import ModelResolver as _MR

        resolver = _MR({})
    resolved_model = resolver.resolve(model).model

    max_input = get_max_input_tokens(resolved_model)
    threshold = max_input - T_wrap_SP - output_reserve - offload_cap
    return TurnBudget(
        max_input=max_input,
        T_wrap_SP=T_wrap_SP,
        output_reserve=output_reserve,
        offload_cap=offload_cap,
        force_close_threshold=threshold,
    )


def assert_turn_budget_bounds(tb: TurnBudget) -> None:
    """Fail-fast invariants on a derived TurnBudget (sibling of compaction's
    ``assert_static_bounds``): the threshold must leave real working room, so a
    misconfigured reserve/offload_cap/model fails at construction rather than by
    force-closing on every single turn.
    """
    assert tb.T_wrap_SP > 0, (
        f"TurnBudget.T_wrap_SP must be > 0 (the wrap-up SP was not measured); "
        f"got {tb.T_wrap_SP}"
    )
    assert tb.output_reserve >= 0 and tb.offload_cap >= 0, (
        f"TurnBudget reserves must be non-negative; got output_reserve="
        f"{tb.output_reserve}, offload_cap={tb.offload_cap}"
    )
    # #1092 PR-E (by-construction termination — LOCKED #1092 issuecomment-4618222625):
    # the threshold must leave room for the re-injected consolidation (hard-capped
    # ≤ output_reserve by the wrap-up call's max_tokens) PLUS a full working
    # increment (offload_cap) below it. Then every force-close re-entry makes a
    # full increment of NEW progress (not just the minimal ≥1 op that
    # output_reserve<threshold alone gives — that is fragile, re-entering close to
    # the visit cap), so a finite-work phase converges to a genuine finish in FEW
    # re-entries — making the max_phase_visits abort UNREACHABLE for a
    # well-configured / finite-work phase (a pathological infinite-work phase that
    # never completes is still backstopped by the visit cap). Strictly implies
    # threshold > 0 AND no wrap-up overflow (consolidation + T_wrap_SP +
    # output_reserve ≤ T_max). A config where it fails is rejected at construction
    # (degenerate → fail-fast).
    assert tb.force_close_threshold > tb.output_reserve + tb.offload_cap, (
        f"TurnBudget.force_close_threshold ({tb.force_close_threshold}) must exceed "
        f"output_reserve ({tb.output_reserve}) + offload_cap ({tb.offload_cap}) = "
        f"{tb.output_reserve + tb.offload_cap} so a force-close re-entry makes a full "
        f"increment of progress (the consolidation, hard-capped ≤ output_reserve, "
        f"plus one increment fit below the threshold). Otherwise the re-entry cannot "
        f"reliably progress and would force-close-loop toward max_phase_visits — a "
        f"by-construction termination gap. Lower the reserves or use a larger-context "
        f"model."
    )


class TurnBudgetEngine:
    """Holds the wrap-up SP + the derived layer-1 budget for one (model, config).

    Mirrors ``CompactionEngine``'s init shape: resolve the model class once,
    measure the dedicated-SP token cost (``T_wrap_SP``) once, derive the budget,
    and assert its bounds fail-fast. The per-turn trigger (PR-C) calls
    :meth:`should_force_close` with the current accumulated turn-content size.
    """

    def __init__(
        self,
        model: str,
        *,
        output_reserve: int,
        offload_cap: int,
        resolver: "ModelResolver | None" = None,
        use_chars4: bool = False,
    ) -> None:
        if resolver is None:
            from reyn.llm.model_resolver import ModelResolver as _MR

            resolver = _MR({})
        self._model = resolver.resolve(model).model
        # Measure the wrap-up SP cost once, on the RESOLVED model (the #1172
        # discipline) — the same way CompactionEngine measures T_comp_SP.
        self._T_wrap_SP: int = estimate_tokens(
            _WRAP_UP_SYSTEM_PROMPT, self._model, use_chars4=use_chars4
        )
        self._budget: TurnBudget = compute_turn_budget(
            self._model,
            T_wrap_SP=self._T_wrap_SP,
            output_reserve=output_reserve,
            offload_cap=offload_cap,
            resolver=resolver,
        )
        assert_turn_budget_bounds(self._budget)

    @property
    def budget(self) -> TurnBudget:
        """The derived layer-1 TurnBudget (read-only)."""
        return self._budget

    @property
    def wrap_up_sp(self) -> str:
        """The wrap-up system prompt this engine measured."""
        return _WRAP_UP_SYSTEM_PROMPT

    def should_force_close(self, content_tokens: int) -> bool:
        """True when the accumulated current-turn *content* (history measured
        WITHOUT the system prompt) has reached the layer-1 threshold, i.e. one
        more normal increment plus the wrap-up call would risk overflow.
        """
        return content_tokens >= self._budget.force_close_threshold


# Cross-axis default reserves for the layer-1 force-close threshold (#1092 C2).
# Shared by ALL axes (phase now; chat/plan in PR-F) so the threshold shape never
# diverges per-axis. ``output_reserve`` = tokens kept for the wrap-up call's
# generated consolidation; a wrap-up hand-off is short (essence, not volume —
# see the wrap-up SP), so a small fixed reserve suffices. A config field can
# override this later if a model needs it.
DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS = 2048


def build_default_turn_budget_engine(
    model: str,
    *,
    resolver: "ModelResolver | None" = None,
    use_chars4: bool = False,
) -> TurnBudgetEngine:
    """Construct a TurnBudgetEngine with the shared cross-axis default reserves.

    The two reserves are:
    - ``offload_cap`` — the post-offload per-result inline ceiling (#1093,
      ``context_builder.MAX_OFFLOADED_INLINE_BYTES``) converted to tokens: the
      largest a single tool_result can re-add as the "one more turn" increment
      once offload has capped it.
    - ``output_reserve`` — :data:`DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS`.

    Building every axis through THIS helper keeps the threshold shape identical
    across chat/plan/phase (no per-axis drift); PR-F reuses it verbatim. The
    import of the offload ceiling is local to avoid a module-load cycle.
    """
    from reyn.context_builder import MAX_OFFLOADED_INLINE_BYTES

    # The offload ceiling is a BYTE bound; convert to the model's tokens so it is
    # comparable with the (token-denominated) threshold. Measured once at build.
    offload_cap = estimate_tokens(
        "x" * MAX_OFFLOADED_INLINE_BYTES, model, use_chars4=use_chars4
    )
    return TurnBudgetEngine(
        model,
        output_reserve=DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS,
        offload_cap=offload_cap,
        resolver=resolver,
        use_chars4=use_chars4,
    )


def try_build_default_turn_budget_engine(
    model: str,
    *,
    resolver: "ModelResolver | None" = None,
    use_chars4: bool = False,
) -> "TurnBudgetEngine | None":
    """:func:`build_default_turn_budget_engine`, but returns ``None`` instead of
    raising when the model's context is too small to satisfy the by-construction
    force-close floor (``output_reserve + offload_cap < force_close_threshold``).

    Such a model genuinely CANNOT support force-close termination by construction
    (there is no room for a capped consolidation plus a working increment below
    the threshold) — that is a property of the model's small context, NOT a
    misconfiguration. So a caller that activates force-close opportunistically
    (e.g. a chat/plan session that may run on any model) degrades to its
    pre-force-close path (no wrap-up cap, no handoff — the existing retry_loop
    terminal) rather than failing to construct. Where a non-viable config IS a bug
    (the reserves were mis-set for a known large model), call
    ``build_default_turn_budget_engine`` directly so the assert still fires.

    The viability gate is exactly the engine's own construction-time
    ``assert_turn_budget_bounds`` — caught here so the result is None, not a raise;
    no bounds logic is duplicated (which would drift from the engine's measure)."""
    try:
        return build_default_turn_budget_engine(
            model, resolver=resolver, use_chars4=use_chars4
        )
    except AssertionError:
        return None
