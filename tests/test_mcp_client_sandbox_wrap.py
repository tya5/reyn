"""Tier 2: stdio MCP server subprocess is sandbox-wrapped (#1344, uniformly
rerouted through the abstraction #2620).

The MCP server launched by ``_open_stdio`` must run under the platform sandbox
so an LLM-invoked MCP tool cannot escape via the server. ``_sandbox_wrap_stdio``
now routes UNIFORMLY through ``backend.wrap_command()`` — no per-backend-name
branching. This file pins: the Seatbelt command-wrap (macOS), the Landlock
re-exec shim (Linux, #1344-E), NoopBackend's argv-unchanged PASSTHROUGH (still
routed THROUGH the abstraction — the owner-acceptable no-enforcement case, NOT
a bypass), the per-server network default (single-source
DEFAULT_SANDBOX_NETWORK, #1339-D) with operator opt-in / opt-out, and the
temp-profile cleanup.

No mocks: the REAL ``NoopBackend`` / ``SeatbeltBackend`` / ``LandlockBackend``
classes are used (monkeypatched in as ``get_default_backend``'s return value) —
``wrap_command`` is pure/local-I/O-only and does not require the host platform
to match (SeatbeltBackend.wrap_command builds an SBPL profile as plain text;
it does not itself invoke ``sandbox-exec``).
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
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
    assert not Path(profile_path).exists()  # teardown unlinked it
