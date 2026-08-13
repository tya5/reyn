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

from reyn.core.kernel.codeact_runner import CodeActRunner


@pytest.mark.asyncio
async def test_tool_call_round_trips_to_parent_dispatch() -> None:
    """Tier 2: snippet tool() → parent dispatch → result; dispatch sees (name, args)."""
    seen: list[tuple[str, dict]] = []

    async def dispatch(name: str, args: dict) -> dict:
        seen.append((name, args))
        return {"status": "ok", "data": {"echoed": args}}

    runner = CodeActRunner()
    code = "r = tool('read_file', path='a.txt')\nresult = r['echoed']['path']"
    out = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert out["ok"] is True, out
    assert out["result"] == "a.txt"
    assert seen == [("read_file", {"path": "a.txt"})]


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
    excluded = {"web_search"}

    async def dispatch(name: str, args: dict) -> dict:
        seen.append(name)
        # Mirror the OS pre-dispatch exclude gate on the resolved effective name.
        if name in excluded:
            return {"status": "error", "error": {"kind": "tool_excluded", "message": "excluded"}}
        return {"status": "ok", "data": "ok"}

    runner = CodeActRunner()
    # allowed, excluded (caught), allowed — the snippet try/excepts the ToolError.
    code = (
        "out = [tool('read_file', p=1)]\n"
        "try:\n"
        "    tool('web_search', q='x')\n"
        "except Exception as e:\n"
        "    out.append('blocked:' + type(e).__name__)\n"
        "out.append(tool('read_file', p=2))\n"
        "result = out"
    )
    res = await runner.run(code=code, dispatch=dispatch, allow_unsandboxed=True)
    assert res["ok"] is True, res
    # tool() returns the success envelope's `data` ("ok"); the excluded call raised
    # ToolError, caught by the snippet → "blocked:ToolError".
    assert res["result"] == ["ok", "blocked:ToolError", "ok"]
    # The gate was consulted for EACH call including the excluded one (per-call).
    assert seen == ["read_file", "web_search", "read_file"]


@pytest.mark.asyncio
async def test_error_envelope_raises_tool_error_in_snippet() -> None:
    """Tier 2: a dispatch error envelope (permission_denied) surfaces as ToolError."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "error", "error": {"kind": "permission_denied", "message": "no-write"}}

    runner = CodeActRunner()
    code = "tool('write_file', path='x')"
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
        # #3901 PR-B ④: env is compat by default (env_deny_names empty), so
        # PATH needs no explicit passthrough declaration anymore.
        sandbox_policy={"network": False},
        timeout=30,
    )
    assert out["ok"] is True, out
    assert out["result"] == 42
    assert seen == [("m", {"n": 41})]


# ── #2628: single-abstraction — codeact delegates to SandboxBackend.wrap_command ──


def test_seatbelt_resolve_spawn_delegates_to_wrap_command(monkeypatch) -> None:
    """Tier 2: #2628 — CodeActRunner._resolve_sandbox_spawn no longer hand-rolls
    the Seatbelt wrap (importing ``_build_sbpl_profile`` + writing its own temp
    ``.sb`` directly); it now calls ``SandboxBackend.wrap_command(argv, policy)``
    — the SAME abstraction every other command-level launch route uses (#2626).
    The wrapped argv's shape (``sandbox-exec -f <profile> <base_argv>``) matches
    a direct ``backend.wrap_command()`` call on an equivalent policy, and the
    returned cleanup actually unlinks the temp profile (no leak).

    ``wrap_command`` builds a plain-text SBPL profile with local I/O only — it
    does not itself invoke ``sandbox-exec`` — so this pins the delegation on
    any host (the real ``SeatbeltBackend.available()`` platform gate is
    exercised separately by ``test_seatbelt_real_runner_round_trip`` above);
    only ``available()`` is monkeypatched here (a real instance's method, not a
    mock collaborator) so the delegation itself is tested off-macOS too."""
    import os

    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = SeatbeltBackend()
    monkeypatch.setattr(backend, "available", lambda: True)
    runner = CodeActRunner()
    base_argv = [runner.python_executable, "-m", "reyn.core.kernel._codeact_harness"]
    sandbox_policy = {"network": False}

    argv, cleanup, error, resolved_policy = runner._resolve_sandbox_spawn(
        base_argv, backend, sandbox_policy, 30.0, False,
    )
    assert error is None
    assert argv is not None
    assert cleanup is not None
    assert resolved_policy is not None
    # #3901 PR-B ④: PYTHONPATH is no longer forced into a policy field — it
    # reaches the harness subprocess via the env-compat default instead (see
    # _harness_subprocess_env's own tests). What's still pinned here is that
    # nothing DENIES it.
    assert "PYTHONPATH" not in resolved_policy.env_deny_names

    # Same shape as a direct wrap_command() call: sandbox-exec -f <profile> <base_argv>.
    # The profile PATH differs (#4434: each DISTINCT policy OBJECT gets its own
    # session-cache entry, even when two objects render identical SBPL text), so
    # compare everything else — the exact same code path codeact now runs.
    direct = backend.wrap_command(base_argv, SandboxPolicy(network=False, timeout_seconds=30.0))
    assert argv[0] == direct.argv[0] == "sandbox-exec"
    assert argv[1] == direct.argv[1] == "-f"
    assert argv[3:] == direct.argv[3:] == base_argv

    profile_path = argv[2]
    assert os.path.exists(profile_path)
    cleanup()
    # #4434: this policy has no write_paths, so it's safe to session-cache —
    # cleanup() on a cached profile is a no-op (a second caller reusing the
    # same policy object would still need the file); it does NOT unlink.
    assert os.path.exists(profile_path)
    os.unlink(profile_path)
    direct.cleanup()  # no-op too (direct is also cacheable) — tidy up by hand
    if os.path.exists(direct.argv[2]):
        os.unlink(direct.argv[2])


def test_landlock_resolve_spawn_now_wraps_via_abstraction(monkeypatch) -> None:
    """Tier 2: #2628 — Landlock is no longer a "S2c pending" stub that refuses
    to run. ``LandlockBackend.wrap_command`` builds the re-exec shim argv
    deterministically (no temp file, no randomness), so codeact's resolved argv
    is BYTE-IDENTICAL to a direct ``backend.wrap_command()`` call — this is the
    safety-preserving case: Landlock's wrap_command applies REAL isolation (the
    re-exec shim restricts itself via Landlock then execs the target), so
    delegating to it correctly ENABLES Landlock rather than weakening the prior
    fail-closed refusal. ``available()`` is monkeypatched on the real instance
    (its genuine platform/kernel-ABI gate is exercised separately by
    ``tests/security/test_sandbox_landlock.py``) so this delegation is pinned on any host,
    including this macOS dev box."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = LandlockBackend()
    monkeypatch.setattr(backend, "available", lambda: True)
    runner = CodeActRunner()
    base_argv = [runner.python_executable, "-m", "reyn.core.kernel._codeact_harness"]
    sandbox_policy = {"network": False}

    argv, cleanup, error, resolved_policy = runner._resolve_sandbox_spawn(
        base_argv, backend, sandbox_policy, 30.0, False,
    )
    assert error is None
    assert cleanup is None  # Landlock owns no cleanup resource (no temp file)
    assert resolved_policy is not None
    # #3901 PR-B ④: PYTHONPATH is no longer forced into a policy field — it
    # reaches the harness subprocess via the env-compat default instead.
    assert "PYTHONPATH" not in resolved_policy.env_deny_names

    # codeact's resolved policy is exactly what `_resolve_sandbox_spawn` built
    # from `sandbox_policy` above (no forced field), so the equivalent direct
    # call uses the SAME construction — this is comparing against the policy
    # codeact actually resolved, not a caller-embellished one.
    direct = backend.wrap_command(
        base_argv,
        SandboxPolicy(network=False, timeout_seconds=30.0),
    )
    assert argv == direct.argv  # byte-identical — same deterministic build


def test_unsandboxed_escape_still_resolves_a_policy_that_passes_pythonpath() -> None:
    """Tier 2: #3822/#3901 env — the ``allow_unsandboxed`` test-only escape (no
    real backend) still resolves a ``SandboxPolicy`` that, run through
    ``_harness_subprocess_env``, ends up with PYTHONPATH set — matching the
    sandboxed branches. #3901 PR-B ④: PYTHONPATH is no longer forced into a
    policy field (the env-compat default passes it through on its own); what
    is still pinned is that the bare-default policy this branch resolves
    denies nothing that would block it."""
    runner = CodeActRunner()
    base_argv = [runner.python_executable, "-m", "reyn.core.kernel._codeact_harness"]

    argv, cleanup, error, resolved_policy = runner._resolve_sandbox_spawn(
        base_argv, None, None, 30.0, True,
    )
    assert error is None
    assert argv == base_argv
    assert resolved_policy is not None
    assert "PYTHONPATH" not in resolved_policy.env_deny_names

    from reyn.core.kernel.codeact_runner import _harness_subprocess_env

    assert "PYTHONPATH" in _harness_subprocess_env(resolved_policy)


# ── #1609: harness subprocess PYTHONPATH propagation (multi-worktree drift) ───


def test_harness_subprocess_env_prepends_reyn_tree() -> None:
    """Tier 2: #1609 — the harness subprocess env prepends THIS process's reyn tree
    to PYTHONPATH, so `python -m reyn.core.kernel._codeact_harness` resolves the SAME tree
    (fixes the multi-worktree editable-install import-drift). Single-tree prod is
    unaffected (same path)."""
    import os
    from pathlib import Path

    import reyn
    from reyn.core.kernel.codeact_runner import _harness_subprocess_env
    from reyn.security.sandbox import SandboxPolicy

    tree = str(Path(reyn.__file__).resolve().parent.parent)
    env = _harness_subprocess_env(SandboxPolicy())
    assert env["PYTHONPATH"].split(os.pathsep)[0] == tree  # parent tree resolved first


def test_harness_subprocess_env_preserves_existing_pythonpath(monkeypatch) -> None:
    """Tier 2: #1609 — an existing PYTHONPATH is preserved (appended after the tree),
    not clobbered."""
    import os

    monkeypatch.setenv("PYTHONPATH", "/some/existing/path")
    from reyn.core.kernel.codeact_runner import _harness_subprocess_env
    from reyn.security.sandbox import SandboxPolicy

    parts = _harness_subprocess_env(SandboxPolicy())["PYTHONPATH"].split(os.pathsep)
    assert "/some/existing/path" in parts
    assert parts[-1] == "/some/existing/path"  # appended after the prepended tree


def test_harness_subprocess_env_goes_through_the_shared_chokepoint(monkeypatch) -> None:
    """Tier 2: #3822 env — the harness subprocess env is built by
    ``resolve_passthrough_env`` (the SAME chokepoint every other sandboxed
    launch route uses), not a bespoke copy.

    #3901 PR-B ④ (owner ruling B, full compat) changed WHAT that chokepoint
    does (deny-list, empty = pass everything) but not THAT
    ``_harness_subprocess_env`` goes through it — before #3822's fix,
    ``_harness_subprocess_env`` did a bespoke ``dict(os.environ)`` that bypassed
    the chokepoint entirely; strip the ``resolve_passthrough_env`` call and
    this test's positive-and-negative pair below stops moving together (the
    denied name would leak too, since a bespoke copy has no deny mechanism at
    all)."""

    from reyn.core.kernel.codeact_runner import _harness_subprocess_env
    from reyn.security.sandbox import SandboxPolicy

    monkeypatch.setenv("REYN_3901_TEST_PASSES_THROUGH", "compat-default")
    monkeypatch.setenv("REYN_3901_TEST_DENIED", "should-not-leak")
    env = _harness_subprocess_env(
        SandboxPolicy(env_deny_names=["REYN_3901_TEST_DENIED"])
    )
    assert env.get("REYN_3901_TEST_PASSES_THROUGH") == "compat-default", (
        "an undeclared parent env var did not reach the CodeAct harness "
        "subprocess under the compat default — the chokepoint's own default "
        "changed to a deny-list under #3901"
    )
    assert "REYN_3901_TEST_DENIED" not in env, (
        "an explicitly-denied parent env var reached the CodeAct harness "
        "subprocess — the allowlist chokepoint was bypassed"
    )
    assert "PYTHONPATH" in env  # #1609 still holds through the allowlist


@pytest.mark.asyncio
async def test_codeact_snippet_env_goes_through_the_real_run_call_path(monkeypatch) -> None:
    """Tier 2: #3822 env — end-to-end, through the REAL ``run()`` call path
    (not just the ``_harness_subprocess_env`` unit above): a real snippet's
    ``os.environ`` reflects the SAME chokepoint the unit test pins, not a
    bespoke copy that could drift from it on this path specifically.

    This is the seam lead-coder flagged (#3834/#3835/#3837 pattern): the
    IMPLEMENTED side of a fix needs its own witness, not just the incidental
    side. #3901 PR-B ④ (owner ruling B, full compat) flipped what the
    chokepoint does BY DEFAULT (an undeclared var now passes through) but not
    THAT this path uses it — ``run()`` has no ``sandbox_policy`` parameter to
    thread an ``env_deny_names`` entry through in the ``allow_unsandboxed``
    escape, so this witnesses the positive (passes-through) side; the deny
    leg is covered by the unit test above, which calls
    ``_harness_subprocess_env`` directly with an explicit deny.
    """
    async def dispatch(name: str, args: dict) -> dict:  # pragma: no cover - unused
        return {"status": "ok", "data": {}}

    monkeypatch.setenv("REYN_3822_TEST_MARKER", "compat-default")
    code = "import os\nresult = os.environ.get('REYN_3822_TEST_MARKER', 'ABSENT')"
    out = await CodeActRunner().run(
        code=code, dispatch=dispatch, allow_unsandboxed=True,
        allowed_modules=["os"], timeout=30.0,
    )
    assert out["ok"] is True, out
    assert out["result"] == "compat-default", (
        "the CodeAct snippet did not see an undeclared parent env var under "
        "the compat default — the real run() call path diverged from the "
        "_harness_subprocess_env unit above"
    )


@pytest.mark.asyncio
async def test_user_print_does_not_corrupt_result() -> None:
    """Tier 2: #1618 root-2 (#8) — user-code ``print()`` to stdout no longer corrupts
    the result. The result envelope now travels on the control channel (op="final"),
    so a snippet that prints AND binds ``result`` returns the correct result (was
    MalformedResponse when the print's repr landed on stdout ahead of the envelope)."""
    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": "PURPLE-OTTER-42"}

    runner = CodeActRunner()
    code = "print('noisy debug line'); print({'a': 1})\nresult = tool('read_file', path='x')"
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
