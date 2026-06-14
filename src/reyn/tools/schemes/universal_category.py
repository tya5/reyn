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
"""
from __future__ import annotations

from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    Presentation,
    SchemeOps,
)


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
        return ops.present(available, layer_ctx)

    def interpret(self, llm_response, *, tool_catalog: dict, ops: SchemeOps) -> Interpretation:
        # ops.resolve = dedupe + salvage/unwrap → actions with effective names; the
        # OS exclude-gates these pre-execute. Universal always yields Execute.
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
        return ops.feedback(result.tool_results)


__all__ = ["UniversalCategoryScheme"]
