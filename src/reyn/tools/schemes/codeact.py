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

import json
import re
from typing import Any

from reyn.kernel.codeact_runner import CodeActRunner
from reyn.tools.scheme import (
    CodeBlock,
    ExecContext,
    ExecutionResult,
    PlainText,
    Presentation,
    register_scheme,
)

# A fenced code block — how the CodeAct LLM emits its snippet in the message content.
# Accept the real code labels (``python`` / ``py`` / Gemini's native ``tool_code``) or
# a bare fence — but NOT a data label like ``json``: #1593 live-verify showed flash
# -lite both (a) fences with ```tool_code (a python-only pattern silently dropped it →
# misclassified as a terminal PlainText turn) and (b) sometimes wraps a JSON tool-call
# envelope in ```json (matching that exec'd ``{"tool_code": "..."}`` as a no-op dict →
# ``result`` unset → a misleading "null" observation). A ```json block is the model
# mis-formatting (an SP-channel pull, #1608②), not a CodeAct snippet — leave it to the
# no-fence PlainText path rather than exec a data literal.
_FENCE_RE = re.compile(r"```(?:python|py|tool_code)?[ \t]*\n(.*?)```", re.DOTALL)


def _render_code_api(entries: list[dict]) -> str:
    """Render flat ``{name, description, parameters}`` action entries as a CodeAct
    *code-API*: a reference list of the actions the model invokes via the
    ``tool(...)`` proxy. Pure presentation — the model reads it; it is NOT executed
    Python. The arg names come from each entry's JSON-schema ``parameters.properties``
    (CodeAct's strictest-consumer schema-completeness floor guarantees a dict)."""
    lines = [
        "## CodeAct — write a Python script (not JSON tool calls)",
        "",
        "To act, respond with a SINGLE fenced ```python code block and nothing "
        "else — no prose before or after it, no \"I am a Reyn agent\" preamble. Call "
        "any available action with ``tool('<name>', <arg>=<value>, ...)`` — it returns "
        "the action's result, or raises if the action is denied / excluded / unknown. "
        "Assign your final answer to a variable named ``result``. The standard library "
        "is available; filesystem / network / subprocess are sandboxed. When you are "
        "done — you have the answer and need no more actions — reply in plain prose "
        "with NO code block (that ends the turn).",
        "",
        "Available actions:",
    ]
    for entry in entries:
        # The live ``SchemeOps.catalog_entries`` adapter returns the OpenAI
        # tool-schema shape (``{type, function: {name, description, parameters}}``,
        # uniform with ``base_tools`` so enumerate-all / retrieval concat into one
        # ``tools=`` payload). CodeAct is the lone consumer that renders names into a
        # code-API, so it unwraps the ``function`` envelope here (tolerating a flat
        # ``{name, ...}`` entry too). #1593 live-verify caught this: reading the
        # top-level ``name`` yielded ``tool('')`` for every action (empty catalog →
        # the model's correct ``tool('file__read', ...)`` hit "not in catalog").
        fn = entry.get("function", entry)
        name = fn.get("name", "")
        params = fn.get("parameters") or {}
        arg_names = list((params.get("properties") or {}).keys())
        sig = ", ".join(arg_names)
        desc_raw = (fn.get("description") or "").strip()
        desc = desc_raw.splitlines()[0] if desc_raw else ""
        lines.append(f"- tool('{name}'{', ' + sig if sig else ''}) — {desc}".rstrip(" —"))
    return "\n".join(lines)


def _extract_fenced_code(llm_response: Any) -> str | None:
    """The first fenced ```python block in the response content, or ``None`` when
    there is no fenced block. A response with no fence is a terminal natural-language
    reply (→ ``PlainText``), NOT bare code to execute: the SP instructs the model to
    fence its snippet, and treating un-fenced prose as code made the model's final
    answer turn raise a spurious ``SyntaxError`` and loop without terminating
    (#1593 live-verify)."""
    content = getattr(llm_response, "content", None) or ""
    if not isinstance(content, str):
        return None
    match = _FENCE_RE.search(content)
    return match.group(1) if match else None


def _format_codeact_observation(out: dict) -> str:
    """Render a ``CodeActRunner`` result envelope as the user-role observation text
    the model reads after its code turn (success result, or the error/kind on
    failure / timeout / sandbox-unavailable)."""
    if out.get("ok"):
        body = json.dumps(out.get("result"), default=str, ensure_ascii=False)
        return f"[codeact result]\n{body}"
    kind = out.get("kind", "Error")
    return f"[codeact {kind}]\n{out.get('error', '')}"


class CodeActScheme:
    """CodeAct scheme (#1593 PR-3). Own logic (not delegating)."""

    name: str = "codeact"

    def __init__(self, runner: CodeActRunner | None = None) -> None:
        self._runner = runner or CodeActRunner()

    async def build_presentation(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> Presentation:
        """Render the permission-eligible actions as a CodeAct *code-API* in the
        ``sp_fragment`` (#1601 channel) — each action a callable the model invokes via
        the ``tool(name, **args)`` proxy. No JSON ``tools=`` (``llm_tools_payload``
        empty): the model writes a Python snippet in its content, not tool calls.

        ``ops.catalog_entries()`` is async (the SchemeOps adapter ensures the
        rag/source-populated context — e2e Option A: adapter owns the rs-ensure
        await; my ``universal_catalog.catalog_entries`` substrate stays sync). The
        ``sp_params`` named gates are both off (CodeAct expresses its whole tool-use
        SP through the free-form fragment, not the universal named gates).

        Excluded-tool *omission from the code-API* is defense-in-depth, NOT the safety
        boundary: the real gate is the per-call exclude + ``dispatch_tool`` re-entry
        in ``execute`` (a code call to an excluded action is rejected at dispatch).
        Presentation-level omission is deferred to a follow-up (it needs the exclude
        set, which the OS applies post-presentation to ``tools=`` today)."""
        entries = await ops.catalog_entries()
        # Presentation parity with the JSON path (#1400): omit excluded actions from
        # the code-API too, so CodeAct's presentation is not looser than JSON tools=
        # ("CodeAct >= JSON" on the presentation face, not just the per-call gate).
        # The OS supplies the session exclude-set via ``available``.
        exclude = (available or {}).get("exclude_tools") or frozenset()
        if exclude:
            # Name lives under the OpenAI ``function`` envelope (live adapter shape);
            # unwrap it here too — reading the top-level ``name`` made the filter a
            # silent no-op (excluded actions still rendered = parity/permission leak),
            # the same nested-shape trap as ``_render_code_api`` (#1593 live-verify).
            entries = [
                e for e in entries
                if (e.get("function", e)).get("name") not in exclude
            ]
        return Presentation(
            llm_tools_payload=[],
            sp_params={
                "universal_wrappers_enabled": False,
                "search_actions_enabled": False,
            },
            sp_fragment=_render_code_api(entries),
        )

    def interpret(
        self, llm_response: Any, *, tool_catalog: dict, ops: Any,
    ) -> CodeBlock | PlainText:
        """Classify the response: a fenced ```python block → ``CodeBlock`` (the
        OS-loop's CodeBlock arm runs ``execute``); no fenced block → ``PlainText``
        (the terminal text-reply path emits the final answer — the model's natural
        -language answer turn is NOT code to execute). No resolution/dedup here —
        CodeAct tool calls are resolved + gated per call inside ``execute`` (via the
        OS gate), not up front."""
        code = _extract_fenced_code(llm_response)
        if code is None:
            return PlainText()
        return CodeBlock(code=code)

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
        """Shape the CodeAct execution result(s) as loop-appendable feedback
        **messages** — a user-role 'observation' carrying the snippet's result /
        stdout / error (the CodeAct ReAct-style observation turn). The OS loop's
        CodeBlock arm appends these verbatim after the [assistant: code] turn (it owns
        no CodeAct message shape — P7). NOTE the documented divergence: the Execute
        path's format_feedback returns tool_results (for the zip); CodeAct returns
        messages (for direct append)."""
        return [
            {"role": "user", "content": _format_codeact_observation(out)}
            for out in exec_result.tool_results
        ]


# #1608: self-register on import (P7 — the OS resolve no longer names this class).
register_scheme(CodeActScheme())
