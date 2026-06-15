"""Tier 2: #1593 PR-3 S3a — CodeActScheme (interpret + execute glue + feedback).

CodeAct is own-logic (not delegating). This pins:
  - interpret classifies the LLM output: a fenced code block ⇒ CodeBlock; no fence ⇒
    PlainText (terminal — #1618 root-3 #2 loop-unify "prose = done" contract). The
    fence label may be python / py / tool_code (#1618 root-3 #5 fence-label variation).
  - execute threads the OS per-call gate (exec_ctx.extra['dispatch']) + sandbox into
    the CodeActRunner and wraps the result — and REQUIRES the gate (no silent
    ungated run).
  - format_feedback shapes each runner envelope into a user-role observation message
    (the CodeBlock arm's append shape; the documented Execute/CodeBlock divergence).
  - build_presentation renders the actions as a code-API in tool_use_sp (#1618 root-3
    REPLACE channel, not the sp_fragment APPEND), no JSON tools=, with excluded
    actions omitted (presentation parity with JSON).

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


def test_interpret_no_fence_returns_plaintext_terminal() -> None:
    """Tier 2: #1618 root-3 (#2) — no recognized fence ⇒ PlainText (terminal), NOT a
    CodeBlock. A prose-only response is the model's final answer (loop-unify "prose =
    done" contract); the old "run the whole content as bare code" behavior no-op'd →
    the model never cleanly finished → loop/timeout. PlainText is dataless (the OS
    holds the content for the reply)."""
    resp = SimpleNamespace(content="The file contains a config for the build.")
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert isinstance(interp, PlainText)
    # Not misclassified as code (would run prose → no-op → loop).
    assert not isinstance(interp, CodeBlock)


def test_interpret_extracts_tool_code_fence() -> None:
    """Tier 2: #1618 root-3 (#5) — the Gemini-native ```tool_code fence label is
    recognized as a code block (fence-label variation), same as ```python."""
    resp = SimpleNamespace(content="```tool_code\nresult = tool('m')\n```")
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert isinstance(interp, CodeBlock)
    assert interp.code.strip() == "result = tool('m')"


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


# ── S3b: build_presentation (code-API render into tool_use_sp) ────────────────


class _CatalogOps:
    """A real Fake SchemeOps exposing the async ``catalog_entries`` adapter (#1599)."""

    async def catalog_entries(self) -> list[dict]:
        return [
            {"name": "file__read", "description": "Read a file.",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"name": "web__fetch", "description": "Fetch a URL.\nSecond line ignored.",
             "parameters": {"type": "object", "properties": {}}},
        ]


@pytest.mark.asyncio
async def test_build_presentation_renders_code_api_into_tool_use_sp() -> None:
    """Tier 2: #1618 root-3 — build_presentation renders the actions as a code-API in
    tool_use_sp (the REPLACE channel, not sp_fragment APPEND); no JSON tools=.
    Behavior-pinned (action names + tool() proxy + prose=terminal contract present),
    not format-pinned."""
    pres = await CodeActScheme().build_presentation({}, {}, _CatalogOps())
    # No JSON tools= — CodeAct presents via the SP, model writes a snippet.
    assert pres.llm_tools_payload == []
    # The actions surface in the code-API + the tool() proxy is instructed, via the
    # REPLACE channel (tool_use_sp), and the old APPEND channel is unused.
    assert "file__read" in pres.tool_use_sp
    assert "web__fetch" in pres.tool_use_sp
    assert "tool(" in pres.tool_use_sp
    assert not pres.sp_fragment  # root-3: replace channel, not append
    # The prose=terminal contract (#2) must be stated so the model knows how to finish.
    assert "plain prose" in pres.tool_use_sp
    # The named SP gates are off (CodeAct expresses tool-use via tool_use_sp).
    assert pres.sp_params.get("universal_wrappers_enabled") is False
    assert pres.sp_params.get("search_actions_enabled") is False


@pytest.mark.asyncio
async def test_build_presentation_includes_arg_names() -> None:
    """Tier 2: an action's schema arg names appear in its code-API signature."""
    pres = await CodeActScheme().build_presentation({}, {}, _CatalogOps())
    assert "path" in pres.tool_use_sp  # file__read's parameters.properties key


@pytest.mark.asyncio
async def test_code_api_has_no_bare_tool_call_for_flashlite() -> None:
    """Tier 2: #1638 — the rendered code-API carries NO bare quoted ``tool('<x>')``
    token. gemini-2.5-flash-lite returns ~100% empty-choices on a bare ``tool('<quoted>')``
    token (content-trigger, lead+sandbox_2 proxy-probe: bare 6/6 empty → backtick 0/6);
    the CodeAct code-API rendered ~50 such bare lines. Presentation-only — every rendered
    call is backtick-wrapped; the model still writes bare ``tool(...)`` in its python block."""
    import re

    pres = await CodeActScheme().build_presentation({}, {}, _CatalogOps())
    # No bare `tool('` (one not immediately preceded by a backtick) anywhere in the render.
    assert not re.search(r"(?<!`)tool\('", pres.tool_use_sp)
    # The call IS present, backtick-wrapped (the action is still discoverable/usable).
    assert "`tool('file__read'" in pres.tool_use_sp


@pytest.mark.asyncio
async def test_build_presentation_omits_excluded_actions() -> None:
    """Tier 2: presentation parity (#1400) — an excluded action is omitted from the
    code-API (CodeAct presentation not looser than JSON tools=). The OS supplies the
    exclude-set via available['exclude_tools']."""
    pres = await CodeActScheme().build_presentation(
        {"exclude_tools": frozenset({"web__fetch"})}, {}, _CatalogOps(),
    )
    assert "file__read" in pres.tool_use_sp   # kept
    assert "web__fetch" not in pres.tool_use_sp  # excluded → omitted


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
