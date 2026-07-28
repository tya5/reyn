"""CodeActScheme — the (enumerate-all, content_fence) transport-cell
implementation (#1593 PR-3; FP-0066 P4c, #3247).

Unlike universal-category (which delegates to the router's existing JSON tool
logic), CodeAct implements its own scheme logic: the LLM writes a Python snippet
and tool calls happen as **in-code ``tool()`` calls**, each round-tripping through
the sandboxed ``CodeActRunner`` to the OS per-call gate (exclude + ``dispatch_tool``
+ permission, P5). A CodeAct call is therefore gated **>=** a JSON call (same gate
+ sandbox containment).

FP-0066 P4c clean-break: this class is no longer registered under the bare
name ``"codeact"`` (which read as if it were a 4th sibling scheme alongside
``category`` / ``enumerate-all`` / ``retrieval``). It self-registers under
``reyn.tools.transport.CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME`` — reachable
ONLY by resolving ``(scheme="enumerate-all", transport=Transport.CONTENT_FENCE)``
through the P4a valid-pair registry (``tool_use.scheme: codeact`` was never a
valid config value; this closes the matching ``_SCHEMES``-level naming gap).
The class body — presentation over the enum-all catalog, the CodeBlock
interpret-branch, and ``CodeActRunner`` execution — is byte-identical to
pre-P4c. Presentation itself now runs on the Exposure/Encoder seam: the shared
``enumerate-all`` exposure (``reyn.tools.schemes._enumerate_exposure``) decides
what is shown, and the ``content_fence`` encoder (``reyn.tools.encoders``)
renders the code-API and owns the identifier map ``execute`` shares.

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

from reyn.core.kernel.codeact_runner import CodeActRunner
from reyn.prompt.codeact import (
    CODEACT_RESULT_LABEL,
    CODEACT_STDERR_LABEL,
    CODEACT_STDOUT_LABEL,
)
from reyn.tools.encoders import build_actions_map, encoder_for_transport
from reyn.tools.scheme import (
    CodeBlock,
    ExecContext,
    ExecutionResult,
    PlainText,
    Presentation,
    register_scheme,
)
from reyn.tools.schemes._enumerate_exposure import (
    CONTENT_FENCE_EXPOSURE_DEVIATION,
    build_enumerate_all_exposure,
)
from reyn.tools.transport import CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME, Transport

# A fenced code block — how the CodeAct LLM emits its snippet in the message
# content. #1618 root-3 (#5): accept the Gemini-native ``tool_code`` fence label
# alongside ``python`` / ``py`` / bare ``` (fence-label variation — weak models
# vary the label; the snippet body is the same Python the runner executes).
_FENCE_RE = re.compile(r"```(?:python|py|tool_code)?\s*\n(.*?)```", re.DOTALL)


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
            obs = f"{CODEACT_RESULT_LABEL}\n{body}"
        elif stdout:
            # #1618 root-2 (#6): the snippet print()d instead of binding ``result`` —
            # surface the captured stdout so the observation is not empty (the model
            # otherwise sees nothing and retries / gives up).
            obs = f"{CODEACT_STDOUT_LABEL}\n{stdout}"
        else:
            obs = f"{CODEACT_RESULT_LABEL}\n{json.dumps(result, default=str)}"
        stderr = (out.get("stderr") or "").strip()
        if stderr:
            obs = f"{obs}\n{CODEACT_STDERR_LABEL}\n{stderr}"
        return obs
    kind = out.get("kind", "Error")
    return f"[codeact {kind}]\n{out.get('error', '')}"


class CodeActScheme:
    """CodeAct scheme (#1593 PR-3). Own logic (not delegating).

    ``name`` is the P4c-relocated ``_SCHEMES`` key (see module docstring) —
    not the literal ``"codeact"`` string; that name no longer exists in the
    registry."""

    name: str = CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME

    def __init__(self, runner: CodeActRunner | None = None) -> None:
        self._runner = runner or CodeActRunner()

    async def build_presentation(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> Presentation:
        """Render the permission-eligible actions as a CodeAct *code-API* in the
        ``tool_use_sp`` (#1618 root-3 REPLACE channel — the code-API replaces the
        universal tool-use SP region, rather than appending to it and leaving the
        universal vocab in place) — each action a callable the model invokes via the
        ``tool(name, /, **args)`` proxy. No JSON ``tools=`` (``llm_tools_payload`` empty):
        the model writes a Python snippet in its content, not tool calls.

        ``ops.catalog_entries()`` is async (the SchemeOps adapter ensures the
        rag/source-populated context — e2e Option A: adapter owns the rs-ensure
        await; my ``universal_catalog.catalog_entries`` substrate stays sync).

        This is the ``(enumerate-all, content_fence)`` **cell**: the shared
        ``enumerate-all`` exposure decides *what* is shown (catalog only, per the
        deviation this cell declares — see ``_enumerate_exposure``), and the
        ``content_fence`` encoder decides *how* (no ``tools=`` channel at all; the
        whole surface is the rendered code-API).

        Excluded-tool *omission from the code-API* is defense-in-depth, NOT the safety
        boundary: the real gate is the per-call exclude + ``dispatch_tool`` re-entry
        in ``execute`` (a code call to an excluded action is rejected at dispatch).
        #3378: the omission reads the session's EFFECTIVE contextual narrowing
        (``available['contextual_permission']``) — the same source the live gate
        enforces — instead of the ``exclude_tools`` name set, which could not express a
        topology / delegate / ephemeral narrowing (so a denied action stayed rendered in
        the code-API) nor an allow-list."""
        entries = await ops.catalog_entries()  # canonical (OpenAI-nested) shape
        exposure = build_enumerate_all_exposure(
            catalog_entries=entries,
            available=available,
            layer_ctx=layer_ctx,
            ops=ops,
            deviation=CONTENT_FENCE_EXPOSURE_DEVIATION,
        )
        encoder = encoder_for_transport(Transport.CONTENT_FENCE)
        return Presentation(
            # NOT "no tools": this transport has no ``tools=`` channel. The
            # encoder owns that answer, which is why the empty list is its
            # return value rather than a literal written here.
            llm_tools_payload=encoder.encode_tools(exposure),
            # #1618 root-1: CodeAct advertises ∅ but dispatches the FULL catalog (the
            # model writes code). The dispatch gate's membership is sourced from this
            # (NOT the empty llm_tools_payload → #7 "not in catalog"). Excluded actions
            # stay IN the dispatchable set so an in-code call to one gets the clear
            # ``tool_excluded`` message (per-call gate), not ``unknown_tool``.
            dispatchable_catalog=entries,
            # #1618 root-3 (②): REPLACE the universal tool-use SP region with the
            # code-API. tool_use_sp ⇒ the OS injects this at the ## Capabilities
            # position + drops the universal tool-use construction, so the code-API is
            # the SOLE tool-use instruction the model sees.
            tool_use_sp=encoder.encode_tool_use_sp(exposure),
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
        # ``build_actions_map`` the content_fence encoder runs → identical identifiers
        # as the SP. Sharing that one function is why the map is an encoder concern.
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
        actions_map = build_actions_map([n for n in _names if n])
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
