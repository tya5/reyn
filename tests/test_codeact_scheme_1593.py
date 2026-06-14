"""Tier 2: #1593 PR-3 S3a — CodeActScheme (interpret + execute glue + feedback).

CodeAct is own-logic (not delegating). This pins:
  - interpret returns a CodeBlock for a fenced ```python block, else PlainText (a
    response with no fence is a terminal natural-language reply, not code to exec).
  - execute threads the OS per-call gate (exec_ctx.extra['dispatch']) + sandbox into
    the CodeActRunner and wraps the result — and REQUIRES the gate (no silent
    ungated run).
  - format_feedback shapes each runner envelope into a user-role observation message
    (the CodeBlock arm's append shape; the documented Execute/CodeBlock divergence).
  - build_presentation renders the actions as a code-API in sp_fragment (no JSON
    tools=), with excluded actions omitted (presentation parity with JSON).

The per-call gate RE-ENTRY invariant (N calls → N gate invocations, exclude
per-call) is pinned at the runner level in test_codeact_runner_1593.py (the gate is
the dispatch callback execute forwards). Real Fake runner / Fake SchemeOps (record
what's forwarded) — no mocks.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.tools.scheme import CodeBlock, ExecContext, ExecutionResult, PlainText
from reyn.tools.schemes.codeact import CodeActScheme


class _FakeRunner:
    """Records the args execute forwards; returns a canned envelope. A real Fake
    (implements ``run``), not a mock."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"ok": True, "status": "ok", "result": "ran"}


def test_interpret_extracts_fenced_code() -> None:
    """Tier 2: interpret pulls the fenced ```python block as the CodeBlock."""
    resp = SimpleNamespace(content="here:\n```python\nresult = tool('m')\n```\nthanks")
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert isinstance(interp, CodeBlock)
    assert interp.code.strip() == "result = tool('m')"


def test_interpret_extracts_tool_code_fence() -> None:
    """Tier 2: interpret accepts ANY fence language label — flash-lite fences with
    Gemini's native ```tool_code (not ```python); a python-only pattern dropped it
    and the snippet never ran (#1593 live-verify)."""
    resp = SimpleNamespace(
        content="I will read it.\n```tool_code\nresult = tool('file__read', path='x')\n```",
    )
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert isinstance(interp, CodeBlock)
    assert interp.code.strip() == "result = tool('file__read', path='x')"


def test_interpret_plaintext_when_no_fence() -> None:
    """Tier 2: a response with no fenced block is a terminal natural-language reply
    → PlainText (NOT bare code to execute). The SP instructs the model to fence its
    snippet; treating un-fenced prose as code made the final answer turn raise a
    spurious SyntaxError and loop without terminating (#1593 live-verify)."""
    resp = SimpleNamespace(content="The file contains PURPLE-OTTER-42.")
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert isinstance(interp, PlainText)


@pytest.mark.asyncio
async def test_execute_threads_gate_and_sandbox_into_runner() -> None:
    """Tier 2: execute forwards the OS gate (exec_ctx.extra['dispatch']) + the
    sandbox + code into the runner, and wraps the envelope in ExecutionResult."""
    runner = _FakeRunner()
    scheme = CodeActScheme(runner=runner)

    async def gate(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    sentinel_sandbox = object()
    exec_ctx = ExecContext(
        sandbox=sentinel_sandbox,
        extra={"dispatch": gate, "sandbox_policy": {"network": False}, "timeout": 12},
    )
    res = await scheme.execute(CodeBlock(code="result = tool('m')"), exec_ctx, ops=None)

    assert isinstance(res, ExecutionResult)
    assert res.tool_results == [{"ok": True, "status": "ok", "result": "ran"}]
    forwarded = runner.calls[0]
    assert forwarded["dispatch"] is gate           # the OS gate is threaded through
    assert forwarded["sandbox_backend"] is sentinel_sandbox
    assert forwarded["code"] == "result = tool('m')"
    assert forwarded["sandbox_policy"] == {"network": False}
    assert forwarded["timeout"] == 12


@pytest.mark.asyncio
async def test_execute_requires_the_os_gate() -> None:
    """Tier 2: execute refuses to run without the OS gate (no silent ungated run)."""
    scheme = CodeActScheme(runner=_FakeRunner())
    exec_ctx = ExecContext(sandbox=object(), extra={})  # no 'dispatch'
    with pytest.raises(ValueError, match="dispatch"):
        await scheme.execute(CodeBlock(code="x = 1"), exec_ctx, ops=None)


def test_format_feedback_shapes_observation_message() -> None:
    """Tier 2: format_feedback shapes each runner envelope into a user-role
    observation message (the CodeAct ReAct observation turn) — result on success,
    error/kind on failure — for the loop's CodeBlock arm to append (not raw
    tool_results; the documented Execute/CodeBlock divergence)."""
    scheme = CodeActScheme(runner=_FakeRunner())
    ok = scheme.format_feedback(
        ExecutionResult(tool_results=[{"ok": True, "result": 42}]), ops=None,
    )
    assert ok[0]["role"] == "user"
    assert "42" in ok[0]["content"]
    err = scheme.format_feedback(
        ExecutionResult(tool_results=[{"ok": False, "kind": "ToolError", "error": "denied"}]),
        ops=None,
    )
    assert err[0]["role"] == "user"
    assert "denied" in err[0]["content"]


# ── S3b: build_presentation (code-API render into sp_fragment) ────────────────


class _CatalogOps:
    """A real Fake SchemeOps exposing the async ``catalog_entries`` adapter (#1599).

    Returns the **OpenAI tool-schema shape** (``{type, function: {name, description,
    parameters}}``) the LIVE adapter emits (router_loop ``catalog_entries``, uniform
    with ``base_tools`` / enumerate-all / retrieval). #1593 live-verify: an earlier
    flat-shape Fake here false-passed while the nested live shape rendered ``tool('')``
    for every action ([[feedback_fake_backend_unit_misses_real_integration]])."""

    @staticmethod
    def _fn(name: str, description: str, properties: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": properties},
            },
        }

    async def catalog_entries(self) -> list[dict]:
        return [
            self._fn("file__read", "Read a file.", {"path": {"type": "string"}}),
            self._fn("web__fetch", "Fetch a URL.\nSecond line ignored.", {}),
        ]


@pytest.mark.asyncio
async def test_build_presentation_renders_code_api_into_sp_fragment() -> None:
    """Tier 2: build_presentation renders the actions as a code-API in sp_fragment
    (no JSON tools=); behavior-pinned (action names + tool() proxy instruction
    present), not format-pinned."""
    pres = await CodeActScheme().build_presentation({}, {}, _CatalogOps())
    # No JSON tools= — CodeAct presents via the SP fragment, model writes a snippet.
    assert pres.llm_tools_payload == []
    # The actions surface in the code-API + the tool() proxy is instructed.
    assert "file__read" in pres.sp_fragment
    assert "web__fetch" in pres.sp_fragment
    assert "tool(" in pres.sp_fragment
    # The named SP gates are off (CodeAct expresses tool-use via the fragment).
    assert pres.sp_params.get("universal_wrappers_enabled") is False
    assert pres.sp_params.get("search_actions_enabled") is False


@pytest.mark.asyncio
async def test_build_presentation_includes_arg_names() -> None:
    """Tier 2: an action's schema arg names appear in its code-API signature."""
    pres = await CodeActScheme().build_presentation({}, {}, _CatalogOps())
    assert "path" in pres.sp_fragment  # file__read's parameters.properties key


@pytest.mark.asyncio
async def test_build_presentation_omits_excluded_actions() -> None:
    """Tier 2: presentation parity (#1400) — an excluded action is omitted from the
    code-API (CodeAct presentation not looser than JSON tools=). The OS supplies the
    exclude-set via available['exclude_tools']."""
    pres = await CodeActScheme().build_presentation(
        {"exclude_tools": frozenset({"web__fetch"})}, {}, _CatalogOps(),
    )
    assert "file__read" in pres.sp_fragment   # kept
    assert "web__fetch" not in pres.sp_fragment  # excluded → omitted


# ── S4: registration + selectability ─────────────────────────────────────────


def test_codeact_scheme_registered_and_selectable() -> None:
    """Tier 2: CodeActScheme is registered under 'codeact' and resolves by name
    (selectable via tool_use=codeact); universal stays the default (not codeact)."""
    from reyn.chat.router_loop import _resolve_tool_use_scheme

    selected = _resolve_tool_use_scheme("codeact")
    assert selected.name == "codeact"
    assert isinstance(selected, CodeActScheme)
    # Default is unchanged (universal-category), not codeact.
    assert _resolve_tool_use_scheme(None).name == "universal-category"
