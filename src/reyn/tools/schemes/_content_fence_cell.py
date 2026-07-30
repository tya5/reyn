"""The ``content_fence`` transport's cell behaviour, shared by every cell on it.

``content_fence`` means: the model expresses a chosen action by writing a fenced
Python snippet that calls one of the code-API functions in its system prompt,
and the snippet runs in a sandboxed ``CodeActRunner`` whose stubs marshal each
call back to the OS per-call gate. That is a property of the **transport**, not
of any one presentation — it is identical whether the code-API lists the flat
``enumerate-all`` catalog or the ``category`` scheme's small wrapper set.

So the three transport-side methods (``interpret`` / ``execute`` /
``format_feedback``) and the ``Presentation`` assembly live here once, and a cell
supplies only the one thing that differs: its ``Exposure`` — *what* is shown,
already folded by the presentation layer. The encoder then renders whatever the
exposure carries; it never re-decides the exposed set. A per-cell composer here
would rebuild ``_VALID_SCHEME_TRANSPORT_PAIRS`` implicitly, one decision point
per cell (``reyn.tools.encoders``, "One encoder per transport, not one per
cell").
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
from reyn.tools.exposure import Exposure
from reyn.tools.scheme import CodeBlock, ExecContext, ExecutionResult, PlainText, Presentation
from reyn.tools.transport import Transport

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


class ContentFenceCellScheme:
    """Base for the cells of the ``content_fence`` transport.

    A subclass supplies ``name`` (its ``_SCHEMES`` registry key) and
    ``build_exposure``; everything below is the transport and is identical for
    every presentation carried over it."""

    #: The ``_SCHEMES`` key this cell registers under — set by the subclass.
    name: str = ""

    def __init__(self, runner: CodeActRunner | None = None) -> None:
        self._runner = runner or CodeActRunner()

    async def build_exposure(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> "tuple[Exposure, list[dict]]":
        """The cell's ``(exposure, dispatchable_entries)``.

        ``exposure`` is the presentation's answer to *what is shown*, already
        folded — the encoder renders it, it does not re-derive it.
        ``dispatchable_entries`` is the canonical-shape catalog the OS gate keys
        on for this cell (``Presentation.dispatchable_catalog``), which is
        deliberately NOT the advertised payload: this transport advertises no
        ``tools=`` at all, so an empty advertisement must not become an empty
        dispatch gate (#1618 root-1)."""
        raise NotImplementedError

    async def build_presentation(
        self, available: Any, layer_ctx: Any, ops: Any,
    ) -> Presentation:
        """Assemble the cell's ``Presentation`` from its exposure via the
        ``content_fence`` encoder.

        Both channels are encoder output. ``tools_channel`` is the encoder's
        ``NoToolsChannel`` — NOT "there are no tools" but "this transport has no
        ``tools=`` channel". Since #3421 that is the value's TYPE rather than a
        sentence about an empty list, so ``capability_visibility`` and the
        router read the distinction instead of being told it in a comment.
        ``dispatchable_catalog`` is mandatory on that arm (``Presentation``
        checks it): with nothing advertised there is no advertisement for the
        dispatch gate to fall back on (#1618 root-1). ``tool_use_sp`` is the rendered
        code-API: the SOLE tool-use instruction the model sees, injected at the
        ## Capabilities position with the universal tool-use construction
        dropped (#1618 root-3 ②)."""
        exposure, dispatchable_entries = await self.build_exposure(available, layer_ctx, ops)
        encoder = encoder_for_transport(Transport.CONTENT_FENCE)
        return Presentation(
            tools_channel=encoder.encode_tools(exposure),
            dispatchable_catalog=dispatchable_entries,
            tool_use_sp=encoder.encode_tool_use_sp(exposure),
        )

    def interpret(
        self, llm_response: Any, *, tool_catalog: dict, ops: Any,
    ) -> "CodeBlock | PlainText":
        """Classify the LLM output: a fenced code snippet ⇒ ``CodeBlock`` (the OS-loop's
        CodeBlock arm runs ``execute``); no fence ⇒ ``PlainText`` (terminal — the model
        replied in prose = done, the loop exits to the text-reply path). No
        resolution/dedup here — an in-code call is resolved + gated per call inside
        ``execute`` (via the OS gate), not up front.

        #1618 root-3 (#2): the no-fence ⇒ PlainText branch is what lets a turn on this
        transport cleanly TERMINATE. Without it (old: always CodeBlock), a prose final
        answer ran as bare code → no-op → the model never finishes → loop/timeout
        (oracle-baseline finding). ``interpret`` is a pure classifier (P-aligned):
        PlainText is dataless; the OS already holds ``llm_response.content`` for the
        reply."""
        code = _extract_fenced_code(llm_response)
        if code is None:
            return PlainText()
        return CodeBlock(code=code)

    async def execute(
        self, interp: CodeBlock, exec_ctx: ExecContext, ops: Any,
    ) -> ExecutionResult:
        """Run the snippet in the sandbox; proxy each in-code call through the OS
        per-call gate. ``exec_ctx.extra['dispatch']`` is the OS-provided gate
        (exclude + ``dispatch_tool`` + permission) — the scheme never builds it. The
        sandbox is ``exec_ctx.sandbox`` (fail-closed: no sandbox → the runner refuses
        unless a test sets the runner-level escape)."""
        dispatch = (exec_ctx.extra or {}).get("dispatch")
        if dispatch is None:
            raise ValueError(
                f"{type(self).__name__}.execute requires exec_ctx.extra['dispatch'] "
                "(the OS per-call exclude + dispatch_tool gate)"
            )
        extra = exec_ctx.extra or {}
        # #1658: build the {identifier: action_name} map over the full dispatchable
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
        """Shape the execution result(s) as loop-appendable feedback **messages** —
        a user-role 'observation' carrying the snippet's result / stdout / error (the
        ReAct-style observation turn). The OS loop's CodeBlock arm appends these
        verbatim after the [assistant: code] turn (it owns no CodeAct message shape —
        P7). NOTE the documented divergence: the Execute path's format_feedback
        returns tool_results (for the zip); this one returns messages (for direct
        append)."""
        return [
            {"role": "user", "content": _format_codeact_observation(out)}
            for out in exec_result.tool_results
        ]


__all__ = ["ContentFenceCellScheme"]
