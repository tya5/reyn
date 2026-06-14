"""Tier 2: #1593 PR-3 S2a — CodeActRunner duplex permission-proxy round-trip.

The CodeAct snippet's ``tool(name, **args)`` calls must round-trip to the parent
``dispatch`` (the OS exclude + dispatch_tool + permission gate), error envelopes
must surface inside the snippet as ToolError, and the final ``result`` returns. The
reused restricted namespace blocks raw builtins (defense-in-depth on top of the
sandbox).

Real subprocess + real AF_UNIX socketpair + a real (non-mock) ``dispatch`` callback
— no fakes of the channel. The sandbox wrap is S2b (Seatbelt) / S2c (Landlock);
this pins the transport + proxy core that survives inside the sandbox.
"""
from __future__ import annotations

import pytest

from reyn.kernel.codeact_runner import CodeActRunner


@pytest.mark.asyncio
async def test_tool_call_round_trips_to_parent_dispatch() -> None:
    """Tier 2: snippet tool() → parent dispatch → result; dispatch sees (name, args)."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": {"echoed": args}}

    runner = CodeActRunner()
    code = "r = tool('file__read', path='a.txt')\nresult = r['echoed']['path']"
    out = await runner.run(code=code, dispatch=dispatch)
    assert out["ok"] is True, out
    assert out["result"] == "a.txt"
    assert seen == [("file__read", {"path": "a.txt"})]


@pytest.mark.asyncio
async def test_multiple_sequential_tool_calls() -> None:
    """Tier 2: several mid-execution tool() calls each round-trip, in order."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": args.get("n", 0) * 2}

    runner = CodeActRunner()
    code = "result = [tool('m', n=i) for i in range(3)]"
    out = await runner.run(code=code, dispatch=dispatch)
    assert out["ok"] is True, out
    assert out["result"] == [0, 2, 4]


@pytest.mark.asyncio
async def test_error_envelope_raises_tool_error_in_snippet() -> None:
    """Tier 2: a dispatch error envelope (permission_denied) surfaces as ToolError."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "error", "error": {"kind": "permission_denied", "message": "no-write"}}

    runner = CodeActRunner()
    code = "tool('file__write', path='x')"
    out = await runner.run(code=code, dispatch=dispatch)
    assert out["ok"] is False
    assert out["kind"] == "ToolError"
    assert "no-write" in out["error"]


@pytest.mark.asyncio
async def test_pure_compute_snippet_returns_without_dispatch() -> None:
    """Tier 2: a snippet with no tool() call returns its result; dispatch untouched."""

    async def dispatch(name: str, args: dict) -> dict:
        raise AssertionError("dispatch must not be called for a pure-compute snippet")

    runner = CodeActRunner()
    out = await runner.run(code="result = sum(range(5))", dispatch=dispatch)
    assert out["ok"] is True, out
    assert out["result"] == 10


@pytest.mark.asyncio
async def test_restricted_namespace_blocks_raw_open() -> None:
    """Tier 2: the reused safe-mode namespace rejects open() (defense-in-depth)."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    runner = CodeActRunner()
    out = await runner.run(code="result = open('/etc/passwd').read()", dispatch=dispatch)
    assert out["ok"] is False  # safe-mode AST/builtins blocks the open reference
