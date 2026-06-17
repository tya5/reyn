"""Tier 2: stdio MCP server subprocess is sandbox-wrapped (#1344).

The MCP server launched by ``_open_stdio`` must run under the platform sandbox
so an LLM-invoked MCP tool cannot escape via the server. Pins the Seatbelt
command-wrap (macOS) + the Landlock re-exec shim (Linux, #1344-E), the
per-server network default (single-source DEFAULT_SANDBOX_NETWORK, #1339-D)
with operator opt-in / opt-out, the unsandboxed warning for other backends,
and the temp-profile cleanup.

No mocks: a real ``_FakeBackend`` (name + available()) injected via monkeypatch
of ``reyn.security.sandbox.get_default_backend``; the real ``_build_sbpl_profile`` builds
the asserted SBPL.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient


class _FakeBackend:
    """A real (non-mock) sandbox-backend stand-in: just name + available()."""

    def __init__(self, name: str, available: bool = True) -> None:
        self.name = name
        self._available = available

    def available(self) -> bool:
        return self._available


def _stdio_client(**cfg) -> MCPClient:
    base = {"type": "stdio", "command": "my-mcp", "args": ["--flag"]}
    base.update(cfg)
    return MCPClient(base)


def _patch_backend(monkeypatch, backend) -> None:
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", lambda config=None: backend)


def test_seatbelt_wrap_wraps_command(monkeypatch):
    """Tier 2: under Seatbelt, the command is wrapped as sandbox-exec -f <profile>
    cmd args; the profile is a deny-default SBPL with broad-read."""
    _patch_backend(monkeypatch, _FakeBackend("seatbelt"))
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


def test_seatbelt_wrap_network_default(monkeypatch):
    """Tier 2: #1339-D reproduce-first — with no per-server override the Seatbelt
    profile follows the single-source default (network ON when
    DEFAULT_SANDBOX_NETWORK is True). FAILS on the pre-D hardcoded default-off
    (asserts on observable wrap output, not the private policy object)."""
    from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

    _patch_backend(monkeypatch, _FakeBackend("seatbelt"))
    client = _stdio_client()  # no `network` key
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert ("(allow network*)" in profile) is DEFAULT_SANDBOX_NETWORK


def test_seatbelt_wrap_network_opt_in(monkeypatch):
    """Tier 2: an operator-declared `network: true` keeps the server on network."""
    _patch_backend(monkeypatch, _FakeBackend("seatbelt"))
    client = _stdio_client(network=True)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow network*)" in profile


def test_seatbelt_wrap_network_opt_out(monkeypatch):
    """Tier 2: #1339-D — an operator-declared `network: false` ISOLATES the server
    (the opt-OUT knob; the network gate is now operator-set, not default-off)."""
    _patch_backend(monkeypatch, _FakeBackend("seatbelt"))
    client = _stdio_client(network=False)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow network*)" not in profile


def test_landlock_wrap_uses_reexec_shim(monkeypatch):
    """Tier 2: under Landlock (#1344 follow-up E), the command is wrapped as the
    reyn.security.sandbox.landlock_exec re-exec shim (python -m ... --policy ... -- cmd
    args) — the COMMAND-level analog of the Seatbelt wrap (no UNSANDBOXED warn)."""
    import sys
    import warnings

    _patch_backend(monkeypatch, _FakeBackend("landlock"))
    client = _stdio_client()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any UNSANDBOXED warn would fail here
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])
    assert cmd == sys.executable
    assert args[:2] == ["-m", "reyn.security.sandbox.landlock_exec"]
    sep = args.index("--")
    assert args[sep + 1:] == ["my-mcp", "--flag"]  # original command preserved


def test_non_seatbelt_warns_unsandboxed(monkeypatch):
    """Tier 2: a backend without a Seatbelt wrap (e.g. noop) leaves the command
    unchanged and WARNS that the server runs unsandboxed (never silent)."""
    _patch_backend(monkeypatch, _FakeBackend("noop"))
    client = _stdio_client()
    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])
    assert cmd == "my-mcp"
    assert args == ["--flag"]  # unchanged → no wrap applied


def test_unavailable_seatbelt_warns(monkeypatch):
    """Tier 2: Seatbelt present but available()=False → unsandboxed + warn (not wrapped)."""
    _patch_backend(monkeypatch, _FakeBackend("seatbelt", available=False))
    client = _stdio_client()
    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        cmd, _ = client._sandbox_wrap_stdio("my-mcp", [])
    assert cmd == "my-mcp"


def test_profile_cleaned_on_close(monkeypatch):
    """Tier 2: the temp Seatbelt profile is unlinked on teardown (no leak)."""
    _patch_backend(monkeypatch, _FakeBackend("seatbelt"))
    client = _stdio_client()
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile_path = args[1]  # wrap output (not private state)
    assert Path(profile_path).exists()
    client.close_stderr_capture()
    assert not Path(profile_path).exists()  # teardown unlinked it
