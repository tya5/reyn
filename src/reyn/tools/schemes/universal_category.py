"""UniversalCategoryScheme — the current tool-use behaviour behind the protocol (#1593 PR-1).

The FP-0034 universal-category scheme (catalog wrappers → discover → call by
qualified name) is reyn's shipped tool-use. PR-1 moves it *behind* the
``ToolUseScheme`` protocol **without changing behaviour**: this scheme **delegates**
each method to the router-provided ``SchemeOps`` (which binds the existing
``build_tools`` / resolution / ``dispatch_tool`` / feedback logic). Delegation keeps
PR-1 byte-identical — zero logic is physically relocated — while establishing the
seam (``router_loop.run`` calls the four methods). PR-2 (enumerate-all) and PR-3
(CodeAct) implement their own scheme logic instead of delegating, which is what
exercises the abstraction.

The resolution (dedupe + salvage/unwrap → effective names) lands in ``interpret``
so the OS can exclude-gate **pre-dispatch** (preserving the #1406/#187 order);
``execute`` orchestrates the OS dispatch substrate; ``format_feedback`` produces the
basic tool_result messages (the op-specific plan / invoke_skill handling stays in
the OS loop, around it). Universal emits only ``Execute`` — never ``RePresent`` /
``CodeBlock`` — so the loop's other tag paths are unreached in PR-1.

#1627 Stage 1: ``build_presentation`` now owns its tool-use SP via the slot-map.
It calls ``build_universal_tool_use_slots`` with the 5 inputs derived from
``layer_ctx`` (the OS-supplied raw FACTS), and attaches the resulting slot-map to
the returned ``Presentation`` as ``tool_use_sp``. This relocates the tier→discovery-
mandate POLICY out of the OS and into the scheme layer (CHAR-IDENTICAL: Stage 0
proved the two paths produce the same SP bytes).
"""
from __future__ import annotations

import dataclasses

from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    PlainText,
    Presentation,
    SchemeOps,
    register_scheme,
)
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots


class UniversalCategoryScheme:
    """The shipped universal-category tool-use, behind the ``ToolUseScheme`` protocol.

    PR-1: a thin delegator over ``SchemeOps`` (byte-identical seam). The logic itself
    is the router's existing universal-category code, reached via ``ops`` — so every
    call produces identical bytes to the pre-refactor inline path.
    """

    name = "universal-category"

    async def build_presentation(self, available, layer_ctx, ops: SchemeOps) -> Presentation:
        # ops.present → today's build_tools (or the phase op-catalog) + SP params.
        # #1593 PR-2 seam: build_presentation is async (enumerate-all/PR-4 do I/O),
        # but universal's body is unchanged — ops.present stays sync and is NOT
        # awaited, so the tools=/sp_params bytes are byte-identical to PR-1.
        pres = ops.present(available, layer_ctx)

        # #1627 Stage 1: own the tool-use SP via the slot-map.
        # Derive the 5 builder inputs from the raw FACTS in layer_ctx (the OS
        # supplies facts; the scheme computes policy). The EXACT formulas below
        # must match what the OS computed for the None-path (router_loop.py):
        #
        #   universal_wrappers_enabled = layer_ctx["univ_enabled"]
        #   search_actions_enabled     = sv if univ else True   ← CRITICAL subtlety
        #   discovery_mandate          = tier_wants_discovery_mandate(router_model)
        #   has_hot_list_aliases       = bool(available["hot_list_aliases"])
        #   non_interactive            = layer_ctx["non_interactive"]
        univ: bool = bool(layer_ctx.get("univ_enabled", False))
        sv: bool = bool(layer_ctx.get("search_visible", True))
        sa: bool = sv if univ else True  # same formula as the prior sp_params["search_actions_enabled"]
        dm: bool = tier_wants_discovery_mandate(layer_ctx.get("router_model"))
        hl: bool = bool((available or {}).get("hot_list_aliases"))
        ni: bool = bool(layer_ctx.get("non_interactive", False))

        slots = build_universal_tool_use_slots(
            universal_wrappers_enabled=univ,
            search_actions_enabled=sa,
            discovery_mandate=dm,
            has_hot_list_aliases=hl,
            non_interactive=ni,
        )
        # #1627 Stage 4: sp_params removed from build_presentation (build_system_prompt
        # no longer reads it). tool_use_sp is now the sole SP channel.
        return dataclasses.replace(pres, tool_use_sp=slots)

    def interpret(self, llm_response, *, tool_catalog: dict, ops: SchemeOps) -> Interpretation:
        # ops.resolve = dedupe + salvage/unwrap → actions with effective names; the
        # OS exclude-gates these pre-execute. #1593 loop-unify: when the response has
        # NO tool calls it is a plain answer → PlainText (the OS routes it to the
        # terminal text-reply path) — byte-identical to the former empty-``tool_calls``
        # → text-reply branch. Otherwise Execute (the tool-round path).
        if not getattr(llm_response, "tool_calls", None):
            return PlainText()
        actions = ops.resolve(llm_response, tool_catalog)
        return Execute(actions=actions)

    async def execute(self, interp: Interpretation, exec_ctx: ExecContext, ops: SchemeOps) -> ExecutionResult:
        # Only Execute is emitted by this scheme; the OS loop never routes a
        # RePresent / CodeBlock here in PR-1. Dispatch via the OS substrate (ops),
        # which carries the DispatchContext / phase-memo / permission (P5) path.
        assert isinstance(interp, Execute), "universal-category emits only Execute"
        results = await ops.dispatch(interp.actions)
        return ExecutionResult(tool_results=results)

    def format_feedback(self, result: ExecutionResult, ops: SchemeOps) -> list[dict]:
        # #1608: delegate the full appendable-message build to the OS substrate
        # (ops.feedback now owns the relocated assistant+tool-message construction);
        # the OS loop appends what this returns. Byte-identical to the former inline
        # zip — the enriched ``result`` carries tool_calls + assistant_content.
        return ops.feedback(result)


__all__ = ["UniversalCategoryScheme"]

# #1608: self-register on import (the scheme bundle self-describes; the OS resolve
# no longer names built-in classes — P7). ``schemes/__init__`` imports all built-in
# modules so importing the package (or any submodule) registers the full set.
register_scheme(UniversalCategoryScheme())
