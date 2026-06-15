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
    flat_catalog_entries,
    register_scheme,
)

# A fenced code block — how the CodeAct LLM emits its snippet in the message
# content. #1618 root-3 (#5): accept the Gemini-native ``tool_code`` fence label
# alongside ``python`` / ``py`` / bare ``` (fence-label variation — weak models
# vary the label; the snippet body is the same Python the runner executes).
_FENCE_RE = re.compile(r"```(?:python|py|tool_code)?\s*\n(.*?)```", re.DOTALL)


def _render_code_api(entries: list[dict]) -> str:
    """Render flat ``{name, description, parameters}`` action entries as a CodeAct
    *code-API*: a reference list of the actions the model invokes via the
    ``tool(...)`` proxy. Pure presentation — the model reads it; it is NOT executed
    Python. The arg names come from each entry's JSON-schema ``parameters.properties``
    (CodeAct's strictest-consumer schema-completeness floor guarantees a dict)."""
    # #1618 root-3 (②): this is the REPLACEMENT tool-use SP (Presentation.tool_use_sp)
    # — it is the SOLE tool-use instruction the model sees (the OS drops the universal
    # invoke_action / list_actions / ROUTING-RULE vocab for this region). So it must
    # carry the whole CodeAct contract: act = a single fenced python block; prose = the
    # terminal final answer (the #2 loop-unify contract — the model must KNOW prose ends
    # the turn, or it never cleanly finishes). Wording is the measured variable for the
    # fence-compliance oracle (dogfood-coder owns content finalization).
    lines = [
        "## Tool use — this agent acts by running Python, not JSON tool calls",
        "",
        "To DO anything (read a file, call an action, compute), respond with a "
        "SINGLE fenced ```python block and NOTHING else — no prose before or after it, "
        "no \"I am a Reyn agent\" preamble on an action turn. Inside the block, call any "
        "available action:",
        "",
        "    result = `tool('<name>', <arg>=<value>, ...)`",
        "",
        "`tool(...)` returns the action's result, or raises if the action is denied / "
        "excluded / unknown. Assign your final answer to `result`. The Python standard "
        "library is available; filesystem / network / subprocess are sandboxed — reach "
        "the outside world ONLY via `tool()`.",
        "",
        "When you are DONE (answer in hand, no more actions to run): reply in plain "
        "prose with NO code block — that ends the turn. A turn is EITHER one fenced "
        "```python block OR a plain-prose final answer, never both.",
        "",
        "Available actions:",
    ]
    for entry in entries:
        name = entry.get("name", "")
        params = entry.get("parameters") or {}
        arg_names = list((params.get("properties") or {}).keys())
        sig = ", ".join(arg_names)
        desc_raw = (entry.get("description") or "").strip()
        desc = desc_raw.splitlines()[0] if desc_raw else ""
        # #1638: backtick-wrap the rendered call so the SP carries NO bare quoted
        # `tool('<x>')` token — gemini-2.5-flash-lite returns ~100% empty-choices on a
        # bare `tool('<quoted>')` token (content-trigger; lead+sandbox_2 proxy-probe:
        # bare 6/6 empty → backtick 0/6). Presentation-only: the catalog is a reference
        # list the model READS; it still writes bare `tool(...)` inside its python block.
        lines.append(f"- `tool('{name}'{', ' + sig if sig else ''})` — {desc}".rstrip(" —"))
    return "\n".join(lines)


def _extract_fenced_code(llm_response: Any) -> "str | None":
    """Pull the snippet from the LLM response: the first fenced code block in the
    content, or ``None`` when there is NO recognized fence.

    #1618 root-3 (#2): returning ``None`` (instead of the old "else the whole
    content" bare-code fallback) is the loop-unify "prose = terminal" contract. The
    SP demands a fenced block for any action turn, so a no-fence response is the
    model's plain-prose final answer — NOT bare code to run. The old fallback ran
    prose as code (no-op → empty observation → the model retries forever → timeout,
    the oracle-baseline finding); ``interpret`` now maps ``None`` → ``PlainText``
    (terminal) so the loop cleanly exits."""
    content = getattr(llm_response, "content", None) or ""
    if not isinstance(content, str):
        return None
    match = _FENCE_RE.search(content)
    if match:
        return match.group(1)
    return None


def _format_codeact_observation(out: dict) -> str:
    """Render a ``CodeActRunner`` result envelope as the user-role observation text
    the model reads after its code turn (success result, or the error/kind on
    failure / timeout / sandbox-unavailable)."""
    if out.get("ok"):
        result = out.get("result")
        stdout = (out.get("stdout") or "").strip()
        if result is not None:
            body = json.dumps(result, default=str, ensure_ascii=False)
            obs = f"[codeact result]\n{body}"
        elif stdout:
            # #1618 root-2 (#6): the snippet print()d instead of binding ``result`` —
            # surface the captured stdout so the observation is not empty (the model
            # otherwise sees nothing and retries / gives up).
            obs = f"[codeact stdout]\n{stdout}"
        else:
            obs = f"[codeact result]\n{json.dumps(result, default=str)}"
        stderr = (out.get("stderr") or "").strip()
        if stderr:
            obs = f"{obs}\n[codeact stderr]\n{stderr}"
        return obs
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
        ``tool_use_sp`` (#1618 root-3 REPLACE channel — the code-API replaces the
        universal tool-use SP region, vs the #1601 ``sp_fragment`` APPEND that left the
        universal vocab in place) — each action a callable the model invokes via the
        ``tool(name, **args)`` proxy. No JSON ``tools=`` (``llm_tools_payload`` empty):
        the model writes a Python snippet in its content, not tool calls.

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
        entries = await ops.catalog_entries()  # canonical (OpenAI-nested) shape
        # #1618 root-1: project to the FLAT {name, params} shape the render reads (the
        # OS-owned projection — no hand-read of a nested dict at a guessed depth, which
        # was #1: tool('') ×50, and #3: the exclude filter silently never matched).
        flat = flat_catalog_entries(entries)
        # Presentation parity with the JSON path (#1400): omit excluded actions from
        # the rendered code-API (defense-in-depth — the real gate is per-call). The OS
        # supplies the session exclude-set via ``available``.
        exclude = (available or {}).get("exclude_tools") or frozenset()
        rendered = [e for e in flat if e["name"] not in exclude] if exclude else flat
        return Presentation(
            llm_tools_payload=[],
            # #1618 root-1: CodeAct advertises ∅ but dispatches the FULL catalog (the
            # model writes code). The dispatch gate's membership is sourced from this
            # (NOT the empty llm_tools_payload → #7 "not in catalog"). Excluded actions
            # stay IN the dispatchable set so an in-code call to one gets the clear
            # ``tool_excluded`` message (per-call gate), not ``unknown_tool``.
            dispatchable_catalog=entries,
            sp_params={
                "universal_wrappers_enabled": False,
                "search_actions_enabled": False,
            },
            # #1618 root-3 (②): REPLACE the universal tool-use SP region with the
            # code-API (not the old sp_fragment APPEND, which left the universal
            # invoke_action / list_actions / ROUTING-RULE vocab in place → leaks
            # 1/2/5/6). tool_use_sp ⇒ the OS injects this at the ## Capabilities
            # position + drops the universal tool-use construction, so the code-API is
            # the SOLE tool-use instruction the model sees.
            tool_use_sp=_render_code_api(rendered),
        )

    def interpret(
        self, llm_response: Any, *, tool_catalog: dict, ops: Any,
    ) -> "CodeBlock | PlainText":
        """Classify the LLM output: a fenced code snippet ⇒ ``CodeBlock`` (the OS-loop's
        CodeBlock arm runs ``execute``); no fence ⇒ ``PlainText`` (terminal — the model
        replied in prose = done, the loop exits to the text-reply path). No
        resolution/dedup here — CodeAct tool calls are resolved + gated per call inside
        ``execute`` (via the OS gate), not up front.

        #1618 root-3 (#2): the no-fence ⇒ PlainText branch is what lets a CodeAct turn
        cleanly TERMINATE. Without it (old: always CodeBlock), a prose final answer ran
        as bare code → no-op → the model never finishes → loop/timeout (oracle-baseline
        finding). ``interpret`` is a pure classifier (P-aligned): PlainText is dataless;
        the OS already holds ``llm_response.content`` for the reply."""
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
