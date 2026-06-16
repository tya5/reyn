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

import sys

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
    out = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is True, out
    assert out["result"] == "a.txt"
    assert seen == [("file__read", {"path": "a.txt"})]


@pytest.mark.asyncio
async def test_gate_reenters_every_call_not_once() -> None:
    """Tier 2: the per-call gate re-entry invariant — N in-code tool() calls invoke
    the gate (exclude + dispatch) N times (EVERY call, not once/cached). This is the
    "CodeAct call >= JSON call" property: each proxied call is gated like a JSON one."""
    gate_calls: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        gate_calls.append((name, args))
        return {"status": "ok", "data": args.get("n", 0) * 2}

    runner = CodeActRunner()
    code = "result = [tool('m', n=i) for i in range(3)]"
    out = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is True, out
    assert out["result"] == [0, 2, 4]
    # The gate fired once PER call (3x), in order — not deduped, not cached.
    assert gate_calls == [("m", {"n": 0}), ("m", {"n": 1}), ("m", {"n": 2})]


@pytest.mark.asyncio
async def test_exclude_gate_blocks_per_call_mixed() -> None:
    """Tier 2: an excluded tool is rejected on EVERY call (mid a sequence of allowed
    calls), exactly as the JSON-path #1406/#187 pre-dispatch exclude gate would —
    the gate re-enters per call, so exclude is enforced per call (not once)."""
    seen: list[str] = []
    excluded = {"web__search"}

    async def dispatch(name: str, args: dict) -> dict:
        seen.append(name)
        # Mirror the OS pre-dispatch exclude gate on the resolved effective name.
        if name in excluded:
            return {"status": "error", "error": {"kind": "tool_excluded", "message": "excluded"}}
        return {"status": "ok", "data": "ok"}

    runner = CodeActRunner()
    # allowed, excluded (caught), allowed — the snippet try/excepts the ToolError.
    code = (
        "out = [tool('file__read', p=1)]\n"
        "try:\n"
        "    tool('web__search', q='x')\n"
        "except Exception as e:\n"
        "    out.append('blocked:' + type(e).__name__)\n"
        "out.append(tool('file__read', p=2))\n"
        "result = out"
    )
    res = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert res["ok"] is True, res
    # tool() returns the success envelope's `data` ("ok"); the excluded call raised
    # ToolError, caught by the snippet → "blocked:ToolError".
    assert res["result"] == ["ok", "blocked:ToolError", "ok"]
    # The gate was consulted for EACH call including the excluded one (per-call).
    assert seen == ["file__read", "web__search", "file__read"]


@pytest.mark.asyncio
async def test_error_envelope_raises_tool_error_in_snippet() -> None:
    """Tier 2: a dispatch error envelope (permission_denied) surfaces as ToolError."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "error", "error": {"kind": "permission_denied", "message": "no-write"}}

    runner = CodeActRunner()
    code = "tool('file__write', path='x')"
    out = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is False
    assert out["kind"] == "ToolError"
    assert "no-write" in out["error"]


@pytest.mark.asyncio
async def test_pure_compute_snippet_returns_without_dispatch() -> None:
    """Tier 2: a snippet with no tool() call returns its result; dispatch untouched."""

    async def dispatch(name: str, args: dict) -> dict:
        raise AssertionError("dispatch must not be called for a pure-compute snippet")

    runner = CodeActRunner()
    out = await runner.run(code="result = sum(range(5))", dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is True, out
    assert out["result"] == 10


@pytest.mark.asyncio
async def test_restricted_namespace_blocks_raw_open() -> None:
    """Tier 2: the reused safe-mode namespace rejects open() (defense-in-depth)."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    runner = CodeActRunner()
    out = await runner.run(code="result = open('/etc/passwd').read()", dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is False  # safe-mode AST/builtins blocks the open reference


# ── S2b: fail-closed gate + the real OS sandbox (Seatbelt) ───────────────────


@pytest.mark.asyncio
async def test_fail_closed_when_no_sandbox_backend() -> None:
    """Tier 2: no sandbox + not allow_unsandboxed → refused (sandbox_unavailable),
    NOT silently run unsandboxed (the owner-signed fail-closed posture)."""

    async def dispatch(name: str, args: dict) -> dict:
        raise AssertionError("must not run the snippet when fail-closed")

    runner = CodeActRunner()
    out = await runner.run(code="result = 1", dispatch=dispatch)  # no backend, no escape
    assert out["ok"] is False
    assert out["status"] == "sandbox_unavailable"
    assert out["kind"] == "SandboxUnavailable"


@pytest.mark.asyncio
async def test_noop_backend_is_fail_closed() -> None:
    """Tier 2: a noop backend (no real isolation) is treated as no-sandbox → refused."""
    from reyn.security.sandbox.noop_backend import NoopBackend  # noqa: PLC0415

    async def dispatch(name: str, args: dict) -> dict:
        raise AssertionError("must not run under noop")

    runner = CodeActRunner()
    out = await runner.run(code="result = 1", dispatch=dispatch, sandbox_backend=NoopBackend())
    assert out["status"] == "sandbox_unavailable"


@pytest.mark.skipif(sys.platform != "darwin", reason="SeatbeltBackend macOS only")
@pytest.mark.asyncio
async def test_seatbelt_real_runner_round_trip() -> None:
    """Tier 2: the permission-proxy round-trip works through the REAL CodeActRunner
    under an actual Seatbelt sandbox (fd-survival re-verified via the runner path,
    not a standalone probe). Network is denied by the default policy; the AF_UNIX
    control fd still round-trips."""
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend  # noqa: PLC0415

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available")

    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": {"n": args.get("n", 0) + 1}}

    runner = CodeActRunner()
    code = "result = tool('m', n=41)['n']"
    out = await runner.run(
        code=code,
        dispatch=dispatch,
        sandbox_backend=backend,
        sandbox_policy={"network": False, "env_passthrough": ["PATH"]},
        timeout=30,
    )
    assert out["ok"] is True, out
    assert out["result"] == 42
    assert seen == [("m", {"n": 41})]


# ── #1609: harness subprocess PYTHONPATH propagation (multi-worktree drift) ───


def test_harness_subprocess_env_prepends_reyn_tree() -> None:
    """Tier 2: #1609 — the harness subprocess env prepends THIS process's reyn tree
    to PYTHONPATH, so `python -m reyn.kernel._codeact_harness` resolves the SAME tree
    (fixes the multi-worktree editable-install import-drift). Single-tree prod is
    unaffected (same path)."""
    import os
    from pathlib import Path

    import reyn
    from reyn.kernel.codeact_runner import _harness_subprocess_env

    tree = str(Path(reyn.__file__).resolve().parent.parent)
    env = _harness_subprocess_env()
    assert env["PYTHONPATH"].split(os.pathsep)[0] == tree  # parent tree resolved first


def test_harness_subprocess_env_preserves_existing_pythonpath(monkeypatch) -> None:
    """Tier 2: #1609 — an existing PYTHONPATH is preserved (appended after the tree),
    not clobbered."""
    import os

    monkeypatch.setenv("PYTHONPATH", "/some/existing/path")
    from reyn.kernel.codeact_runner import _harness_subprocess_env

    parts = _harness_subprocess_env()["PYTHONPATH"].split(os.pathsep)
    assert "/some/existing/path" in parts
    assert parts[-1] == "/some/existing/path"  # appended after the prepended tree


@pytest.mark.asyncio
async def test_user_print_does_not_corrupt_result() -> None:
    """Tier 2: #1618 root-2 (#8) — user-code ``print()`` to stdout no longer corrupts
    the result. The result envelope now travels on the control channel (op="final"),
    so a snippet that prints AND binds ``result`` returns the correct result (was
    MalformedResponse when the print's repr landed on stdout ahead of the envelope)."""
    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": "PURPLE-OTTER-42"}

    runner = CodeActRunner()
    code = "print('noisy debug line'); print({'a': 1})\nresult = tool('file__read', path='x')"
    out = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is True, out
    assert out["result"] == "PURPLE-OTTER-42"          # result intact, not corrupted
    assert "noisy debug line" in (out.get("stdout") or "")  # user stdout captured as data


@pytest.mark.asyncio
async def test_print_only_snippet_surfaces_via_stdout() -> None:
    """Tier 2: #1618 root-2 (#6) — a snippet that print()s WITHOUT binding ``result``
    yields result=None but the captured stdout is returned as data, so the observation
    is not empty (the model otherwise sees nothing and retries / gives up)."""
    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    runner = CodeActRunner()
    out = await runner.run(
        code="print('the answer is 42')", dispatch=dispatch, allow_unsandboxed=True,
    )
    assert out["ok"] is True, out
    assert out.get("result") is None
    assert "the answer is 42" in (out.get("stdout") or "")
