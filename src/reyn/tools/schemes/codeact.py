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
import keyword
import re
from typing import Any

from reyn.kernel.codeact_runner import CodeActRunner


def _sanitize_identifier(name: str) -> str:
    """#1658: a qualified action name → a valid Python identifier. Most names
    (``file__read``, ``exec__sandboxed_exec``) already are; MCP / skill names with
    hyphens or dots (``web-search__search``) are not — non-identifier chars become
    ``_``, a leading digit is prefixed, and a Python keyword is suffixed. The REAL
    qualified name is preserved in the actions map and is what the parent gate
    receives — the identifier is only the LLM-facing Python name."""
    s = re.sub(r"\W", "_", name)
    if not s or s[0].isdigit():
        s = "_" + s
    if keyword.iskeyword(s):
        s = s + "_"
    return s


def _build_actions_map(qualified_names: "list[str]") -> "dict[str, str]":
    """#1658: ``{python_identifier: qualified_name}`` for the direct-function code-API.

    DETERMINISTIC (sorted) with collision-disambiguation (``_2`` / ``_3`` …) so that
    ``build_presentation`` (renders the SP signatures) and ``execute`` (builds the
    harness stubs) compute the **identical** map when both run it over the same full
    dispatchable name set — the model's identifier call always matches a stub, and
    the stub marshals the real qualified name to the parent gate."""
    out: "dict[str, str]" = {}
    used: set[str] = set()
    for qn in sorted(qualified_names):
        base = _sanitize_identifier(qn)
        ident, n = base, 2
        while ident in used:
            ident, n = f"{base}_{n}", n + 1
        used.add(ident)
        out[ident] = qn
    return out
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


def _render_code_api(entries: list[dict], ident_by_qn: "dict[str, str]") -> str:
    """#1658: render flat ``{name, description, parameters}`` entries as a CodeAct
    *code-API* — DIRECT function signatures the model calls by name
    (``file__read(path=...)``), NOT the old ``tool('name', ...)`` string-proxy. The
    function name is a selected Python identifier (``ident_by_qn[qualified]``), so the
    action name can never be a hallucinated produced string. Pure presentation — the
    model reads these signatures and writes the calls; the OS injects gated stubs of
    the same names into the sandbox namespace (each marshals to the parent gate).

    This is the SOLE tool-use instruction the model sees (Presentation.tool_use_sp —
    the OS drops the universal invoke_action / list_actions vocab for this region), so
    it carries the whole CodeAct contract: act = a single fenced python block; prose =
    the terminal final answer (the loop-unify contract — prose ends the turn)."""
    lines = [
        "## Tool use — this agent acts by running Python, not JSON tool calls",
        "",
        "To DO anything (read a file, call an action, compute), respond with a "
        "SINGLE fenced ```python block and NOTHING else — no prose before or after it, "
        "no \"I am a Reyn agent\" preamble on an action turn. Inside the block, call the "
        "available functions DIRECTLY by name and assign your final value to `result`:",
        "",
        "    result = file__read(path=\"README.md\")",
        "",
        "Each function returns the action's result, or raises if the action is denied / "
        "excluded / unknown. The Python standard library is available; filesystem / "
        "network / subprocess are sandboxed — reach the outside world ONLY by calling "
        "these functions.",
        "",
        "When you are DONE (answer in hand, no more actions to run): reply in plain "
        "prose with NO code block — that ends the turn. A turn is EITHER one fenced "
        "```python block OR a plain-prose final answer, never both.",
        "",
        "Available functions:",
    ]
    for entry in entries:
        name = entry.get("name", "")
        ident = ident_by_qn.get(name, _sanitize_identifier(name))
        params = entry.get("parameters") or {}
        arg_names = list((params.get("properties") or {}).keys())
        sig = ", ".join(arg_names)
        desc_raw = (entry.get("description") or "").strip()
        desc = desc_raw.splitlines()[0] if desc_raw else ""
        # A direct function signature — the model calls `ident(args)`. No quoted
        # `tool('<x>')` token anywhere in the SP (#1638: that bare token caused
        # ~100% empty choices on gemini-2.5-flash-lite; the direct-call form removes
        # it entirely — the model writes an identifier call, not a quoted string).
        line = f"- `def {ident}({sig})`"
        lines.append(f"{line} — {desc}" if desc else line)
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
        # #1658: the identifier map over the FULL dispatchable name set (deterministic
        # sort + collision-disambig) so it is IDENTICAL to the map execute builds for
        # the harness stubs → the model's identifier call always matches a stub.
        ident_by_qn = {
            qn: ident for ident, qn in _build_actions_map([e["name"] for e in flat]).items()
        }
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
            tool_use_sp=_render_code_api(rendered, ident_by_qn),
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
        # #1658: build the {identifier: qualified_name} map over the full dispatchable
        # catalog the OS threads in (the gate's membership) using the SAME deterministic
        # _build_actions_map as build_presentation → identical identifiers as the SP.
        # The harness injects a gated stub per identifier that marshals the REAL
        # qualified name to `dispatch` (the parent gate) — gating identical to the old
        # tool('name') proxy (denied/excluded/unknown → same raise).
        _dispatchable = extra.get("dispatchable_catalog") or exec_ctx.tool_catalog or {}
        if isinstance(_dispatchable, dict):
            _names = list(_dispatchable.keys())
        else:
            _names = [
                (e.get("function") if isinstance(e.get("function"), dict) else e).get("name", "")
                for e in _dispatchable
            ]
        actions_map = _build_actions_map([n for n in _names if n])
        out = await self._runner.run(
            code=interp.code,
            dispatch=dispatch,
            actions=actions_map,
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
