"""Tier 2: #3041 — the CodeAct shim must not eat a target tool's own ``name`` key.

The direct-function stub (#1658) marshals its qualified name through ``tool``'s first
parameter, which is itself called ``name``. Any tool whose OWN schema declares a
parameter named ``name`` therefore had its ``name`` kwarg collide with the already-
filled positional slot → ``TypeError: got multiple values for argument 'name'``,
raised in the SHIM before the parent gate ever ran. Deterministic Python argument
binding: no LLM-side calling convention could avoid it, so ~20 tools
(``mcp__install_local``, ``agent_spawn``, ``remember_shared``, ``pipeline__run`` …)
were simply uncallable under CodeAct. Fix: ``def tool(name, /, **args)`` — a
positional-only parameter cannot be filled by a keyword.

The affected set is derived FROM THE REGISTRY (every tool declaring a ``name``
property), not from a hand-listed subset, so a tool that grows a ``name`` parameter
later is covered on the day it is registered rather than the day someone remembers
to extend a literal list.

Real ``CodeActRunner`` + a real async dispatch double (no mocks); ``allow_unsandboxed``
(the test-only transport escape — the sandbox is orthogonal to the binding proof).
"""
from __future__ import annotations

import pytest

from reyn.core.kernel.codeact_runner import CodeActRunner
from reyn.tools import get_default_registry
from reyn.tools.schemes.codeact import _build_actions_map


def _tools_declaring_a_name_param() -> list[str]:
    """Every registered tool whose OWN parameter schema has a ``name`` property —
    the exact set at risk of the shim collision, read off the registry."""
    return sorted(
        t.name
        for t in get_default_registry()
        if "name" in ((t.parameters or {}).get("properties", {}) or {})
    )


@pytest.mark.asyncio
async def test_every_registry_tool_with_a_name_param_is_callable_under_codeact() -> None:
    """Tier 2: #3041 — for EVERY registered tool declaring its own ``name`` parameter,
    a CodeAct call passing ``name=`` reaches the parent gate with (a) the tool's real
    qualified name as the dispatch target and (b) ``name`` intact as an ARGUMENT.

    Both halves matter: the shim must keep routing on the qualified name while no
    longer consuming the caller's same-spelled argument."""
    qualified = _tools_declaring_a_name_param()
    assert qualified, "registry declares no tool with a 'name' param — the guard would be vacuous"
    actions = _build_actions_map(qualified)  # the REAL scheme identifier map
    ident_of = {q: i for i, q in actions.items()}

    seen: dict[str, dict] = {}

    async def dispatch(name: str, args: dict) -> dict:
        seen[name] = args
        return {"status": "ok", "data": name}

    # One snippet calling every at-risk stub — the pre-fix TypeError was raised by the
    # first one, so reaching the end at all is itself the regression signal.
    calls = "\n".join(
        f"{ident_of[q]}(name={q!r})" for q in qualified
    )
    out = await CodeActRunner().run(
        code=calls + "\nresult = 'done'",
        dispatch=dispatch,
        actions=actions,
        allow_unsandboxed=True,
    )

    assert out["ok"] is True, f"a name-declaring tool still fails under CodeAct: {out}"
    # Every tool dispatched, and each received its own 'name' as an argument.
    assert set(seen) == set(qualified)
    assert seen == {q: {"name": q} for q in qualified}


@pytest.mark.asyncio
async def test_target_name_arg_does_not_hijack_the_dispatch_target() -> None:
    """Tier 2: #3041 — a ``name`` ARGUMENT must not be able to redirect WHICH tool the
    gate runs. The qualified name rides a positional-only slot, so a snippet passing
    ``name='file__read'`` to one tool still dispatches the tool it named in code.

    This is the security-relevant half of the fix: were ``name`` keyword-reachable, a
    snippet could aim the marshalling primitive at a different tool than the stub the
    gate's exclude list was reasoning about."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": "ok"}

    out = await CodeActRunner().run(
        code="result = mcp__install_local(name='file__read', command='c', args=[])",
        dispatch=dispatch,
        actions={"mcp__install_local": "mcp__install_local"},
        allow_unsandboxed=True,
    )

    assert out["ok"] is True, out
    # Dispatched the stub the code named, NOT the value of the 'name' argument.
    assert seen == [
        ("mcp__install_local", {"name": "file__read", "command": "c", "args": []})
    ]


@pytest.mark.asyncio
async def test_live_reported_install_call_reaches_the_gate() -> None:
    """Tier 2: #3041 — the exact call the live LLM wrote in the reported trace
    (``mcp__install_local(name=..., command=..., args=[])``) now reaches the gate.

    Also pins the sibling non-collision: ``args`` is the shim's ``**kwargs`` catch-all,
    which no keyword can collide with, so a tool declaring its own ``args`` parameter
    (as this one does) rides through as a plain argument."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": {"installed": args.get("name")}}

    out = await CodeActRunner().run(
        code=(
            "result = mcp__install_local("
            "name='reyn_chunker', command='reyn-rag-chunker', args=[])"
        ),
        dispatch=dispatch,
        actions={"mcp__install_local": "mcp__install_local"},
        allow_unsandboxed=True,
    )

    assert out["ok"] is True, out
    assert out["result"] == {"installed": "reyn_chunker"}
    assert seen == [
        (
            "mcp__install_local",
            {"name": "reyn_chunker", "command": "reyn-rag-chunker", "args": []},
        )
    ]
