"""Tier 2: an UNSANDBOXED MCP stdio fallback leaves an audit-event, not only a
warning (#3821).

``_sandbox_wrap_stdio`` falls back to a raw, unwrapped launch when resolving or
probing the sandbox backend fails. Before #3821 that fallback was WARNING-only:
``warnings.warn`` reaches whoever is watching stderr at that instant and nothing
else, so a session that ran an MCP server outside the sandbox left no trace any
later reader could find — while the method's own docstring asserted the launch
was "never silently unsandboxed". The prose claimed an audit trail the mechanism
did not have.

What is pinned here:

  - the fallback emits ``sandbox_policy_not_applied`` (an EXISTING kind, shared
    with ``hooks/shell_runner`` — no new vocabulary), carrying enough to act on:
    which server, which command, and why the wrap failed;
  - ``scope="mcp_stdio"`` distinguishes this producer from the hook one, which
    reports a single refused axis via ``policy_field``. A subscriber reads the
    producer off a present field rather than inferring it from an absent one;
  - the sink is OPTIONAL and its absence is not an error — the ephemeral
    ``MCPClientPool`` path has no sink to give and stays WARNING-only;
  - the wiring: a REAL ``MCPConnectionService`` (the one production path that
    HAS a sink) opening a REAL stdio server delivers the event to that sink.
    Without this arm the emit could be perfectly correct and reach nobody.

No mocks. The backend failure is injected at ``get_default_backend`` — the same
seam ``test_mcp_client_sandbox_wrap.py`` already uses, and the actual shape of
the fault this branch exists for (a backend that will not resolve). Everything
else is a real instance: the client, the connection service, the stdio server
subprocess, and a list-appending sink (the emit-sink shape used throughout the
event tests).
"""
from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient
from reyn.mcp.connection_service import MCPConnectionService

_ECHO_SERVER = Path(__file__).parent / "_support" / "mcp_fastmcp_echo_server.py"


def _boom(config=None):
    raise RuntimeError("backend probe exploded")


def _stdio_cfg() -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}


def _not_applied(events: list) -> list[dict]:
    return [d for et, d in events if et == "sandbox_policy_not_applied"]


def test_unsandboxed_fallback_emits_audit_event(monkeypatch):
    """Tier 2: the fallback emits ``sandbox_policy_not_applied`` naming the
    server, the command and the failure — the trace the warning alone did not
    leave."""
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", _boom)
    events: list = []
    client = MCPClient(
        {"type": "stdio", "command": "my-mcp", "args": ["--flag"]},
        server_name="srv-a",
        emit_event=lambda et, **d: events.append((et, d)),
    )

    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])

    assert (cmd, args) == ("my-mcp", ["--flag"])  # still launches, unwrapped
    emitted = _not_applied(events)
    assert len(emitted) == 1
    payload = emitted[0]
    assert payload["scope"] == "mcp_stdio"
    assert payload["server"] == "srv-a"
    assert payload["command"] == "my-mcp"
    # The reason must carry the failure itself — "something failed" is not
    # actionable a week later, which is the whole point of recording it.
    assert "backend probe exploded" in payload["reason"]


def test_scope_field_distinguishes_this_producer_from_the_hook_one(monkeypatch):
    """Tier 2: the kind has two producers with different payloads. This one is
    identified by a field it HAS (``scope``), and carries no ``policy_field`` —
    the hook producer's per-axis key, which is meaningless here because the whole
    policy failed to apply rather than one axis being refused."""
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", _boom)
    events: list = []
    client = MCPClient(
        {"type": "stdio", "command": "my-mcp"},
        server_name="srv-a",
        emit_event=lambda et, **d: events.append((et, d)),
    )
    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        client._sandbox_wrap_stdio("my-mcp", [])

    payload = _not_applied(events)[0]
    assert "policy_field" not in payload


def test_missing_sink_still_warns_and_does_not_raise(monkeypatch):
    """Tier 2: no sink is a supported construction (the ephemeral pool path), not
    a degraded one — the warning still fires and the launch still proceeds."""
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", _boom)
    client = MCPClient({"type": "stdio", "command": "my-mcp", "args": ["--flag"]})

    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        cmd, args = client._sandbox_wrap_stdio("my-mcp", ["--flag"])

    assert (cmd, args) == ("my-mcp", ["--flag"])


def test_a_failing_sink_does_not_block_the_launch(monkeypatch):
    """Tier 2: telemetry is best-effort — a sink that raises must not turn a
    degraded-but-working launch into a dead one."""
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", _boom)

    def _bad_sink(_et, **_d):
        raise RuntimeError("sink is down")

    client = MCPClient({"type": "stdio", "command": "my-mcp"}, emit_event=_bad_sink)
    with pytest.warns(UserWarning, match="UNSANDBOXED"):
        cmd, args = client._sandbox_wrap_stdio("my-mcp", [])
    assert (cmd, args) == ("my-mcp", [])


def test_connection_service_delivers_the_event_to_its_sink(monkeypatch):
    """Tier 2: the wiring, end to end. A REAL ``MCPConnectionService`` with a sink
    opens a REAL stdio server while the backend refuses to resolve — and the
    event arrives. The emit above is correct in isolation; this is what makes it
    reach anybody."""
    monkeypatch.setattr("reyn.security.sandbox.get_default_backend", _boom)
    events: list = []
    service = MCPConnectionService(emit_sink=lambda et, **d: events.append((et, d)))

    async def _run_it():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # asserted directly above
                await service.get("echo", _stdio_cfg())
        finally:
            await service.aclose()

    asyncio.run(_run_it())

    emitted = _not_applied(events)
    assert len(emitted) == 1
    assert emitted[0]["scope"] == "mcp_stdio"
    assert emitted[0]["server"] == "echo"  # the service's own server name reached it
