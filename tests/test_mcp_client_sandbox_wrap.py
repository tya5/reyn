"""Tier 2: stdio MCP server subprocess is sandbox-wrapped (#1344).

The MCP server launched by ``_open_stdio`` must run under the platform sandbox
so an LLM-invoked MCP tool cannot escape via the server. Pins the Seatbelt
command-wrap (the implemented backend), the per-server network opt-in
(default OFF = secure-by-default), the unsandboxed warning for other backends,
and the temp-profile cleanup.

No mocks: a real ``_FakeBackend`` (name + available()) injected via monkeypatch
of ``reyn.sandbox.get_default_backend``; the real ``_build_sbpl_profile`` builds
the asserted SBPL.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.mcp_client import MCPClient


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
    monkeypatch.setattr("reyn.sandbox.get_default_backend", lambda config=None: backend)


def test_seatbelt_wrap_wraps_command(monkeypatch):
    """Tier 2: under Seatbelt, the command is wrapped as sandbox-exec -f <profile>
    cmd args; the profile is a deny-default SBPL with broad-read, network OFF."""
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
    assert "(allow network*)" not in profile  # network OFF by default


def test_seatbelt_wrap_network_opt_in(monkeypatch):
    """Tier 2: an operator-declared `network: true` opts the server into network
    (the per-server knob; default is off = secure-by-default)."""
    _patch_backend(monkeypatch, _FakeBackend("seatbelt"))
    client = _stdio_client(network=True)
    _cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    profile = Path(args[1]).read_text()
    assert "(allow network*)" in profile


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
