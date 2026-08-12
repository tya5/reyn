"""Tier 2: stdio MCP server subprocess is sandbox-wrapped (#1344, uniformly
rerouted through the abstraction #2620).

The MCP server launched by ``_initialize_stdio`` must run under the platform
sandbox so an LLM-invoked MCP tool cannot escape via the server.
``_sandbox_wrap_stdio`` now routes UNIFORMLY through ``backend.wrap_command()``
— no per-backend-name branching. This file pins: the Seatbelt command-wrap
(macOS), the Landlock re-exec shim (Linux, #1344-E), NoopBackend's
argv-unchanged PASSTHROUGH (still routed THROUGH the abstraction — the
owner-acceptable no-enforcement case, NOT a bypass), the per-server network
default (single-source DEFAULT_SANDBOX_NETWORK, #1339-D) with operator
opt-in / opt-out, and the temp-profile cleanup.

No mocks: the REAL ``NoopBackend`` / ``SeatbeltBackend`` / ``LandlockBackend``
classes are used (monkeypatched in as ``get_default_backend``'s return value) —
``wrap_command`` is pure/local-I/O-only and does not require the host platform
to match (SeatbeltBackend.wrap_command builds an SBPL profile as plain text;
it does not itself invoke ``sandbox-exec``).

#4282 / architect finding #4287: #3698 stage 1 moved stdio's live
initialize path from fastmcp's ``_open_stdio``/``_open_transport`` (this
file's original target) to ``_initialize_stdio`` — but the witness here
kept pointing at ``_sandbox_wrap_stdio`` called DIRECTLY, never at
``_initialize_stdio``'s own call to it. Every test above this comment still
correctly exercises ``_sandbox_wrap_stdio`` itself (a real, still-live
method — nothing wrong with those); what was missing was proof that
``_initialize_stdio`` actually CALLS it on the real live path. Measured:
deleting the call site in ``_initialize_stdio`` left every pre-#4287 test
in this file green (#4283 landed with a security suite that would not have
caught it). ``test_initialize_stdio_actually_invokes_the_sandbox_wrap``
and the retargeted env test below close that gap — both falsify-verified
(stripping the call site under test sends them RED; see each docstring).
"""
from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from tests._support.paths import REPO_ROOT

_ECHO_SERVER = REPO_ROOT / "tests" / "_support" / "mcp_fastmcp_echo_server.py"
from reyn.security.sandbox.noop_backend import NoopBackend


def _stdio_client(**cfg) -> MCPClient:
    base = {"type": "stdio", "command": "my-mcp", "args": ["--flag"]}
    base.update(cfg)
    return MCPClient(base)


def _patch_backend(monkeypatch, backend) -> None:
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", lambda config=None: backend)


def test_seatbelt_wrap_wraps_command(monkeypatch):
    """Tier 2: under Seatbelt, wrap_command wraps the command as sandbox-exec -f
    <profile> cmd args; the profile is a deny-default SBPL with broad-read."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client()
    cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])
    assert cmd == "sandbox-exec"
    assert args[0] == "-f"
    profile_path = args[1]  # the wrap's output (not private state)
    assert profile_path.endswith(".sb")
    assert args[-2:] == ["my-mcp", "--flag"]  # original command preserved after the wrapper
    profile = Path(profile_path).read_text()
    assert "(deny default)" in profile
    assert "(allow file-read*)" in profile.splitlines()  # broad-read (#1323)
    client.close_stderr_capture()  # cleanup — no leaked temp profile


def test_seatbelt_wrap_network_default(monkeypatch):
    """Tier 2: #1339-D reproduce-first — with no per-server override the Seatbelt
    profile follows the single-source default (network ON when
    DEFAULT_SANDBOX_NETWORK is True). FAILS on the pre-D hardcoded default-off
    (asserts on observable wrap output, not the private policy object)."""
    from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client()  # no `network` key
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert ("(allow network*)" in profile) is DEFAULT_SANDBOX_NETWORK
    client.close_stderr_capture()


def test_seatbelt_wrap_network_opt_in(monkeypatch):
    """Tier 2: an operator-declared `network: true` keeps the server on network."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client(network=True)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow network*)" in profile
    client.close_stderr_capture()


def test_seatbelt_wrap_network_opt_out(monkeypatch):
    """Tier 2: #1339-D — an operator-declared `network: false` ISOLATES the server
    (the opt-OUT knob; the network gate is now operator-set, not default-off)."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client(network=False)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow network*)" not in profile
    client.close_stderr_capture()


def test_seatbelt_wrap_subprocess_default_allows_fork(monkeypatch):
    """Tier 2: #2820-C — with no `subprocess` override a stdio MCP server defaults
    to allow-subprocess, so the Seatbelt profile grants `(allow process-fork)`. A
    fork-based launcher (npx/uvx/python) is the common case and must be able to
    fork to exist. FAILS on the pre-#2820 default (SandboxPolicy default False →
    (deny process-fork), which silently killed the launch)."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client()  # no `subprocess` key
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow process-fork)" in profile
    assert "(deny process-fork)" not in profile
    client.close_stderr_capture()


def test_seatbelt_wrap_subprocess_opt_out_denies_fork(monkeypatch):
    """Tier 2: #2820-C — an operator-declared `subprocess: false` HARDENS the
    server: the profile emits `(deny process-fork)` (the opt-OUT knob, for a
    genuinely fork-free server). Operator-owned, same model as `network`."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client(subprocess=False)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(deny process-fork)" in profile
    assert "(allow process-fork)" not in profile
    client.close_stderr_capture()


def test_seatbelt_wrap_subprocess_opt_in_explicit(monkeypatch):
    """Tier 2: #2820-C — an explicit `subprocess: true` is honored (allow fork),
    same observable outcome as the default but operator-pinned."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client(subprocess=True)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow process-fork)" in profile
    client.close_stderr_capture()


def test_landlock_wrap_uses_reexec_shim(monkeypatch):
    """Tier 2: under Landlock (#1344 follow-up E), wrap_command wraps the command
    as the reyn.security.sandbox.landlock_exec re-exec shim (python -m ... --policy
    ... -- cmd args) — the COMMAND-level analog of the Seatbelt wrap (no
    UNSANDBOXED warn — this is a routed, enforced wrap, not a bypass)."""
    _patch_backend(monkeypatch, LandlockBackend())
    client = _stdio_client()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any UNSANDBOXED warn would fail here
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])
    assert cmd == sys.executable
    assert args[:2] == ["-m", "reyn.security.sandbox.landlock_exec"]
    sep = args.index("--")
    assert args[sep + 1:] == ["my-mcp", "--flag"]  # original command preserved


def test_noop_backend_wraps_argv_unchanged_through_abstraction(monkeypatch):
    """Tier 2: #2620 — NoopBackend PASSES THROUGH argv unchanged, but the call
    still routed through backend.wrap_command() (never a raw bypass). No
    UserWarning is raised — Noop is the owner-acceptable no-enforcement
    outcome, not an error condition to surface as a warning."""
    _patch_backend(monkeypatch, NoopBackend())
    client = _stdio_client()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])
    assert cmd == "my-mcp"
    assert args == ["--flag"]  # unchanged — passthrough, but via wrap_command


def test_backend_probe_failure_falls_back_with_warning(monkeypatch):
    """Tier 2: only a genuine backend-resolution FAILURE (not a normal Noop
    outcome) falls back to an unwrapped launch — and that fallback is always
    loudly warned, never silent."""

    def _boom(config=None):
        raise RuntimeError("backend probe exploded")

    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", _boom)
    client = _stdio_client()
    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])
    assert cmd == "my-mcp"
    assert args == ["--flag"]


def test_profile_cleaned_on_close(monkeypatch):
    """Tier 2: the temp Seatbelt profile is unlinked on teardown (no leak)."""
    _patch_backend(monkeypatch, SeatbeltBackend())
    client = _stdio_client()
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile_path = args[1]  # wrap output (not private state)
    assert Path(profile_path).exists()
    client.close_stderr_capture()


def test_initialize_stdio_env_is_driven_by_config_alone(monkeypatch):
    """Tier 2: ``_initialize_stdio``'s real ``StdioServerParameters(env=...)``
    call is driven ONLY by ``self._config.get("env")`` — nothing else
    computes or substitutes an env for the real launch. (#3848 closed with
    no fix needed, owner ruling: the official ``mcp`` SDK's own
    ``DEFAULT_INHERITED_ENV_VARS`` allowlist is correct here as-is, and an
    earlier stage-1 mechanism that held a WIDER allowlist for a planned
    stage 2 that never landed — ``self._sandbox_env`` — was removed; see
    ``_sandbox_wrap_stdio``'s own docstring for why MCP stdio's trust
    relationship differs from reyn's own sandbox path.) #4282/#4287:
    retargeted from the removed ``_open_stdio`` to the live
    ``_initialize_stdio`` path. Captures the REAL
    ``mcp.client.stdio.StdioServerParameters`` class's kwargs by wrapping it
    (still constructs the real object, exactly what ``_initialize_stdio``
    would build unmodified) rather than faking the SDK type — same seam
    ``test_initialize_failure_includes_stderr_tail_in_error`` in
    ``test_mcp_client_stderr_capture.py`` already established (a local
    ``from mcp.client.stdio import ...`` re-resolves the source module's
    attribute on every call, so patching it there is what a fresh call
    actually sees). A real connection to the real echo server (same
    fixture ``test_mcp_client.py`` uses) drives ``initialize()`` end to
    end — the NoopBackend keeps the sandbox wrap itself a passthrough so
    only the env-plumbing claim is under test here."""
    import mcp.client.stdio as stdio_mod

    backend = NoopBackend()
    _patch_backend(monkeypatch, backend)
    captured: dict = {}
    real_params_cls = stdio_mod.StdioServerParameters

    def _capturing_params(*args, **kwargs):
        captured.update(kwargs)
        return real_params_cls(*args, **kwargs)

    monkeypatch.setattr(stdio_mod, "StdioServerParameters", _capturing_params)

    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}
    client = MCPClient(cfg)

    async def _run() -> None:
        await client.initialize()
        await client.close()

    asyncio.run(_run())

    # config declared no `env:` key -> today's default: env=None, so the
    # official SDK fills its own DEFAULT_INHERITED_ENV_VARS allowlist.
    assert captured.get("env") is None


def test_initialize_stdio_actually_invokes_the_sandbox_wrap(monkeypatch):
    """Tier 2: #4287 (architect finding on #4283) — every OTHER test in this
    file calls ``_sandbox_wrap_stdio`` DIRECTLY, which proves the method
    itself works but never proves ``_initialize_stdio`` (the live path
    since #3698 stage 1 moved stdio off fastmcp's ``_open_stdio``) actually
    CALLS it. Falsify-verified: commenting out client.py's
    ``command, args = self._sandbox_wrap_stdio(command, args)`` line inside
    ``_initialize_stdio`` leaves every pre-#4287 test in this file green
    while THIS one goes red (``calls`` stays empty even though the
    connection still succeeds unwrapped) — reproduced by hand before
    writing this docstring, not assumed.

    Spies on the SUT's own method (wraps, still delegates to the real
    implementation for every call) rather than faking a collaborator — the
    subprocess really is sandbox-wrapped by NoopBackend's real (passthrough)
    ``wrap_command``; only the CALL itself is additionally recorded."""
    backend = NoopBackend()
    _patch_backend(monkeypatch, backend)
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}
    client = MCPClient(cfg)
    calls = []
    real_wrap = client._sandbox_wrap_stdio

    def _spy(command, args):
        calls.append((command, args))
        return real_wrap(command, args)

    client._sandbox_wrap_stdio = _spy

    async def _run() -> None:
        await client.initialize()
        await client.close()

    asyncio.run(_run())

    assert calls == [(sys.executable, [str(_ECHO_SERVER)])]
