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

#1627 Stage 1: ``build_presentation`` owns its tool-use SP rather than leaving it
to the OS — the tier→discovery-mandate POLICY is derived here, in the scheme
layer, from the raw FACTS the OS supplies in ``layer_ctx`` (CHAR-IDENTICAL:
Stage 0 proved the two paths produce the same SP bytes).

This is the ``(category, tool_calls)`` **cell**: those facts and the descriptors
the router's universal presentation produced become an ``Exposure``
(``reyn.tools.schemes._category_exposure``, shared with the ``content_fence``
cell since #3376 P2), and the ``tool_calls`` encoder (``reyn.tools.encoders``)
turns it into both channels of the ``Presentation``.
"""
from __future__ import annotations

from reyn.tools.encoders import encoder_for_transport
from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    PlainText,
    Presentation,
    SchemeOps,
    advertised_entries,
    register_scheme,
)
from reyn.tools.schemes._category_exposure import (
    TOOL_CALLS_EXPOSURE_DEVIATION,
    build_category_exposure,
)
from reyn.tools.transport import Transport


class UniversalCategoryScheme:
    """The shipped universal-category tool-use, behind the ``ToolUseScheme`` protocol.

    PR-1: a thin delegator over ``SchemeOps`` (byte-identical seam). The logic itself
    is the router's existing universal-category code, reached via ``ops`` — so every
    call produces identical bytes to the pre-refactor inline path.
    """

    name = "universal-category"

    async def build_presentation(self, available, layer_ctx, ops: SchemeOps) -> Presentation:
        # #1627 Stage 1: own the tool-use SP via the slot-map, now through the
        # seam: the exposure carries what is shown (the descriptors the router's
        # universal presentation produced — ``ops.present`` = today's build_tools
        # payload) plus the raw facts, and the tool_calls encoder writes both down.
        # #1593 PR-2 seam: build_presentation is async (enumerate-all/PR-4 do I/O),
        # but universal's body is unchanged — ops.present stays sync and is NOT
        # awaited, so the tools= bytes are byte-identical to PR-1.
        exposure = build_category_exposure(
            present_entries=advertised_entries(
                ops.present(available, layer_ctx).tools_channel
            ),
            available=available,
            layer_ctx=layer_ctx,
            deviation=TOOL_CALLS_EXPOSURE_DEVIATION,
        )
        encoder = encoder_for_transport(Transport.TOOL_CALLS)
        return Presentation(
            tools_channel=encoder.encode_tools(exposure),
            tool_use_sp=encoder.encode_tool_use_sp(exposure),
        )

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
        # which carries the DispatchContext / permission (P5) path.
        assert isinstance(interp, Execute), "universal-category emits only Execute"
        # #4691 Phase B ①(remainder): forward the round's call_id (threaded
        # via ExecContext.extra, the established per-round-context bag) so
        # every dispatched tool call carries the litellm call it belongs to.
        results = await ops.dispatch(
            interp.actions, call_id=(getattr(exec_ctx, "extra", None) or {}).get("call_id"),
        )
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
