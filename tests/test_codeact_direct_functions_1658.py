"""Tier 2: #1658 — CodeAct direct-function stubs gate EXACTLY like the old
tool('name') proxy, including the hyphenated-alias path.

The redesign replaces the tool('<name>', args) string-proxy with gated direct
functions (read_file(...)) injected into the sandbox namespace. Each stub is a
thin renamed wrapper of the same marshalling primitive — it sends the REAL
qualified name over the SAME control channel to the SAME parent dispatch gate. So:
(a) the gate runs on the REAL qualified name (not the Python identifier), even
    when the identifier was sanitized from a hyphenated MCP name; and
(b) a denied action raises in the snippet, exactly as the proxy did.

Real CodeActRunner + a real async dispatch double (no mocks); allow_unsandboxed
(the test-only transport escape — the sandbox is orthogonal to the gating proof).
"""
from __future__ import annotations

import pytest

from reyn.core.kernel.codeact_runner import CodeActRunner


@pytest.mark.asyncio
async def test_direct_function_marshals_real_qualified_name_through_gate() -> None:
    """Tier 2: #1658 — calling the sanitized identifier ``web_search__search(...)``
    marshals the REAL qualified name ``web-search__search`` to the parent gate (the
    gate runs on the real name, not the Python identifier). The aliasing-gate path."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": {"hits": 1}}

    runner = CodeActRunner()
    out = await runner.run(
        code="result = web_search__search(query='reyn')",
        dispatch=dispatch,
        actions={"web_search__search": "web-search__search"},
        allow_unsandboxed=True,
    )
    assert out["ok"] is True, out
    # The gate received the REAL hyphenated qualified name, not the identifier.
    assert seen == [("web-search__search", {"query": "reyn"})]
    assert out["result"] == {"hits": 1}


@pytest.mark.asyncio
async def test_direct_function_denied_raises_in_snippet() -> None:
    """Tier 2: #1658 — a denied action (gate returns an error envelope) raises inside
    the snippet exactly as the old tool() proxy did (gating ENFORCED: denied→raise).

    The identifier is DERIVED via ``build_actions_map`` rather than written out,
    which is load-bearing for this particular action: #3429 renamed it to
    ``exec``, and safe mode refuses the AST Name ``exec`` outright (dynamic
    code). ``sanitize_identifier`` therefore suffixes it, and a hand-written
    ``exec`` here would fail on the safe-mode refusal instead of on the
    permission denial this test is about."""
    from reyn.tools.encoders import build_actions_map

    actions = build_actions_map(["exec"])
    (identifier,) = actions
    assert actions[identifier] == "exec"

    async def dispatch(name: str, args: dict) -> dict:
        return {
            "status": "error",
            "error": {"message": "permission denied", "kind": "permission_denied"},
        }

    runner = CodeActRunner()
    out = await runner.run(
        code=f"result = {identifier}(cmd='rm -rf /')",
        dispatch=dispatch,
        actions=actions,
        allow_unsandboxed=True,
    )
    # The denied stub call raised ToolError → uncaught → snippet failed.
    assert out["ok"] is False
    assert "permission denied" in (out.get("error") or "")


@pytest.mark.asyncio
async def test_simple_identifier_direct_call_gates_same_name() -> None:
    """Tier 2: #1658 — a plain-identifier action (read_file) gates the same name."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": "CONTENT"}

    runner = CodeActRunner()
    out = await runner.run(
        code="result = read_file(path='README.md')",
        dispatch=dispatch,
        actions={"read_file": "read_file"},
        allow_unsandboxed=True,
    )
    assert out["ok"] is True and out["result"] == "CONTENT"
    assert seen == [("read_file", {"path": "README.md"})]


@pytest.mark.asyncio
async def test_internal_tool_primitive_still_callable() -> None:
    """Tier 2: #1658 — the internal tool() primitive the stubs wrap stays callable
    (back-compat / dynamic-name escape), even though the SP advertises only the direct
    functions. Confirms the redesign ADDS the direct surface without removing the
    underlying gated primitive."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": 42}

    runner = CodeActRunner()
    out = await runner.run(
        code="result = tool('read_file', path='x')",
        dispatch=dispatch,
        actions={"read_file": "read_file"},
        allow_unsandboxed=True,
    )
    assert out["ok"] is True and out["result"] == 42
    assert seen == [("read_file", {"path": "x"})]
