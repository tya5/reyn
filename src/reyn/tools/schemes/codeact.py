"""CodeActScheme — the CodeAct tool-use scheme (#1593 PR-3).

Unlike universal-category (which delegates to the router's existing JSON tool
logic), CodeAct implements its own scheme logic: the LLM writes a Python snippet
and tool calls happen as **in-code ``tool()`` calls**, each round-tripping through
the sandboxed ``CodeActRunner`` to the OS per-call gate (exclude + ``dispatch_tool``
+ permission, P5). A CodeAct call is therefore gated **>=** a JSON call (same gate
+ sandbox containment).

The 4 ToolUseScheme methods:
  - ``build_presentation`` → render the permission-eligible actions as a *code-API*
    (function signatures from ``ops.catalog_entries()``, excluded omitted). **S3b**
    — depends on the ``SchemeOps.catalog_entries`` adapter (e2e); stubbed here.
  - ``interpret`` → extract the ``CodeBlock`` from the LLM response.
  - ``execute`` → run the snippet via ``CodeActRunner`` with the OS-provided per-call
    gate (``exec_ctx``) under ``exec_ctx.sandbox`` (fail-closed).
  - ``format_feedback`` → the runner result envelope back to the loop.

The OS gate + sandbox are provided via ``ExecContext`` (the OS assembles them in the
router's CodeBlock arm); the scheme never assembles a DispatchContext or reaches
permission internals — it orchestrates, the OS gates (P3/P7).
"""
from __future__ import annotations

import re
from typing import Any

from reyn.kernel.codeact_runner import CodeActRunner
from reyn.tools.scheme import CodeBlock, ExecContext, ExecutionResult

# A fenced ```python ... ``` (or bare ``` ... ```) block — how the CodeAct LLM
# emits its snippet in the message content.
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def _extract_code(llm_response: Any) -> str:
    """Pull the snippet from the LLM response: the first fenced code block in the
    content, else the whole content (the model may emit bare code). Empty when
    there is nothing to run (``execute`` then no-ops)."""
    content = getattr(llm_response, "content", None) or ""
    if not isinstance(content, str):
        return ""
    match = _FENCE_RE.search(content)
    if match:
        return match.group(1)
    return content.strip()


class CodeActScheme:
    """CodeAct scheme (#1593 PR-3). Own logic (not delegating)."""

    name: str = "codeact"

    def __init__(self, runner: CodeActRunner | None = None) -> None:
        self._runner = runner or CodeActRunner()

    async def build_presentation(self, available: Any, layer_ctx: Any, ops: Any):
        # S3b: render ops.catalog_entries() as a code-API (excluded omitted) + the
        # "write a script calling these functions" SP; llm_tools_payload native-
        # minimal. ASYNC per the e2e seam decision (#1593 issuecomment-4700815694):
        # catalog_entries is async for rag/source completeness + PR-4 dynamic search.
        # Blocked on the SchemeOps.catalog_entries async adapter (tui PR-2 / e2e);
        # not wired until CodeAct is a selectable scheme (tui's per-layer selection).
        raise NotImplementedError(
            "CodeActScheme.build_presentation — S3b (needs async SchemeOps.catalog_entries)"
        )

    def interpret(self, llm_response: Any, *, tool_catalog: dict, ops: Any) -> CodeBlock:
        """Extract the snippet as a ``CodeBlock`` (the OS-loop's CodeBlock arm runs
        ``execute``). No resolution/dedup here — CodeAct tool calls are resolved +
        gated per call inside ``execute`` (via the OS gate), not up front."""
        return CodeBlock(code=_extract_code(llm_response))

    async def execute(
        self, interp: CodeBlock, exec_ctx: ExecContext, ops: Any,
    ) -> ExecutionResult:
        """Run the snippet in the sandbox; proxy each in-code ``tool()`` call through
        the OS per-call gate. ``exec_ctx.extra['dispatch']`` is the OS-provided gate
        (exclude + ``dispatch_tool`` + permission) — the scheme never builds it. The
        sandbox is ``exec_ctx.sandbox`` (fail-closed: no sandbox → the runner refuses
        unless a test sets the runner-level escape)."""
        dispatch = (exec_ctx.extra or {}).get("dispatch")
        if dispatch is None:
            raise ValueError(
                "CodeActScheme.execute requires exec_ctx.extra['dispatch'] "
                "(the OS per-call exclude + dispatch_tool gate)"
            )
        extra = exec_ctx.extra or {}
        out = await self._runner.run(
            code=interp.code,
            dispatch=dispatch,
            sandbox_backend=exec_ctx.sandbox,
            sandbox_policy=extra.get("sandbox_policy"),
            allowed_modules=extra.get("allowed_modules"),
            timeout=extra.get("timeout", 30.0),
            cwd=extra.get("cwd"),
            allow_unsandboxed=extra.get("allow_unsandboxed", False),
        )
        return ExecutionResult(tool_results=[out])

    def format_feedback(self, exec_result: ExecutionResult, ops: Any) -> list[dict]:
        """The runner result envelope(s) become the loop's tool_results unchanged;
        the CodeBlock arm shapes them into the LLM feedback message (S3 wiring)."""
        return exec_result.tool_results
