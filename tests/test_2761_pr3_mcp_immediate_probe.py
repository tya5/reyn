"""Tier 2: #2761 PR-3 — immediate mid-turn mcp install via probe-then-commit.

Extends the #2761 path-condition (PR-2) to mcp: a PURE ADDITION on a live per-session
reloader takes the IMMEDIATE mid-turn path — but mcp additionally PROBES the server
(spawn/connect + ``list_tools``) BEFORE writing config, and writes ONLY on a successful
probe (**probe-then-commit**: a failed/cancelled probe leaves nothing written — no
half-install, no rollback needed). A same-name overwrite (the documented ``reyn mcp
install`` re-install fix) or no per-session reloader keeps the existing DEFERRED
turn-boundary path, unchanged.

The probe is transport-uniform (one ``MCPGateway`` path; ``MCPClient.__aexit__`` owns
the transport-appropriate teardown — stdio subprocess kill / HTTP close) and rides the
cancellable turn task (Ctrl+C → ``CancelledError`` propagates after teardown; since the
write is strictly after the probe, a cancel commits nothing).

Coverage note: the completeness for the PRIMARY stdio use runs through
``mcp__install_local`` (``tools/mcp_verbs``), which writes ``.reyn/config/mcp.yaml``
DIRECTLY — a parallel path to the ``mcp_install`` op — so it carries the SAME contract.
These e2e tests drive that real path against a REAL stdio MCP server subprocess
(``tests/_support/mcp_tools_only_pid_server.py``).

Honesty (discovery vs resolution): asserts the installed server is *resolvable/usable*
(live roster + tool cache) the same turn, NOT the LLM's mid-turn discovery catalog.

No mocks. Real EventLog / HotReloader / Session / MCPGateway / a real stdio MCP server.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.mcp_install import (
    _read_yaml_config,
    _scope_to_path,
    probe_mcp_server,
)
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.hot_reload import HotReloader, set_active_hot_reloader
from reyn.runtime.session import Session
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.mcp_verbs import _handle_mcp_install_local
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session

_PID_SERVER = Path(__file__).parent / "_support" / "mcp_tools_only_pid_server.py"
_STDIO = {"command": sys.executable, "args": [str(_PID_SERVER)]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seam_recorder():
    ran: dict[str, int] = {}

    def make(name: str):
        async def _seam(in_set: dict) -> bool:
            ran[name] = ran.get(name, 0) + 1
            return True
        return _seam

    return ran, make


def _RS(factory) -> RouterCallerState:
    """The REAL RouterCallerState, carrying the op-context factory the handler reads.

    This used to be a hand-rolled stand-in that also declared a ``permission_resolver``
    attribute — a field ``RouterCallerState`` has never had. That invented field is what
    kept the install_local permission gate looking tested while it could never fire in
    production: the handler read the resolver off the router-state, which only the FAKE
    supplied. Constructing the real dataclass is what makes this suite able to witness
    that class of drift at all.
    """
    return RouterCallerState(op_context_factory=factory)


def _Ctx(root: Path, rs: "RouterCallerState | None") -> ToolContext:
    """The REAL ToolContext (a plain dataclass — a real instance, not a stand-in).

    ``permission_resolver=None`` keeps these probe/reload tests focused on their own
    invariant: the handler skips the gate without a resolver (the test / CLI contract).
    The gate itself is covered in test_3037_mcp_install_local_recovery_core_gate.py.
    """
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=SimpleNamespace(root=str(root)),
        caller_kind="router",
        router_state=rs,
    )


def _session(tmp: Path) -> Session:
    (tmp / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    return make_session(
        agent_name="mcp-pr3",
        state_log=StateLog(tmp / "s.wal"),
        snapshot_path=tmp / "snap.json",
    )


def _session_ctx(session: Session, tmp: Path) -> ToolContext:
    """A ctx whose op-context factory is the session's REAL make_router_op_context (so
    ctx.hot_reloader is the session's per-session reloader). resolver=None → the
    install_local file-write gate is skipped (not under test)."""
    return _Ctx(tmp, _RS(session._router_host.make_router_op_context))


def _reloader_ctx(
    reloader: HotReloader, tmp: Path, *, cancel_event: "asyncio.Event | None" = None,
) -> ToolContext:
    """A ctx whose factory returns a bare OpContext carrying ``reloader`` — for the
    probe-then-commit tests that only need the immediate path to fire (no full roster).

    ``cancel_event`` (#2813): threaded onto the OpContext so a probe started through
    this ctx races against it — used by the fast-cancel tests below, distinct from the
    plain-``task.cancel()`` tests above (which exercise the pre-#2813 propagation path,
    unaffected by this parameter when left ``None``)."""
    op_ctx = OpContext(
        workspace=Workspace(events=EventLog(), base_dir=tmp),
        events=EventLog(),
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        hot_reloader=reloader,
        cancel_event=cancel_event,
    )
    return _Ctx(tmp, _RS(lambda: op_ctx))


def _servers_on_disk(tmp: Path) -> dict:
    data = _read_yaml_config(_scope_to_path("local", tmp))
    return (data.get("mcp") or {}).get("servers") or {}


def _roster_names(session: Session) -> list[str]:
    return [s["name"] for s in session._router_host.get_mcp_servers()]


# ===========================================================================
# A. probe_mcp_server — real servers, transport-uniform
# ===========================================================================


@pytest.mark.asyncio
async def test_probe_reachable_stdio_returns_none() -> None:
    """Tier 2: probing a REACHABLE stdio server (spawn + list_tools) returns None —
    the commit gate opens."""
    err = await probe_mcp_server("pidsrv", {"type": "stdio", **_STDIO})
    assert err is None


@pytest.mark.asyncio
async def test_probe_unreachable_stdio_returns_error() -> None:
    """Tier 2: probing an UNREACHABLE stdio server (bad command) returns an error
    string (the MCPFault is contained) — the commit gate stays shut."""
    err = await probe_mcp_server(
        "bad", {"type": "stdio", "command": "/nonexistent/reyn-xyz", "args": []},
    )
    assert isinstance(err, str) and err


@pytest.mark.asyncio
async def test_probe_unreachable_remote_returns_error() -> None:
    """Tier 2: the probe is transport-uniform — an unreachable REMOTE (http) server
    returns an error string via the SAME path (http-transport branch, same rollback)."""
    err = await probe_mcp_server(
        "remote", {"type": "http", "url": "http://127.0.0.1:1/mcp", "timeout": 3},
    )
    assert isinstance(err, str) and err


# ===========================================================================
# B. apply_now — the mcp source maps to the mcp seam only
# ===========================================================================


@pytest.mark.asyncio
async def test_apply_now_mcp_install_targets_mcp_seam_only(tmp_path: Path) -> None:
    """Tier 2: apply_now(source=mcp_install) runs ONLY the "mcp" seam."""
    ran, make = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make("skills"))
    hr.register_seam("pipelines", make("pipelines"))
    hr.register_seam("mcp", make("mcp"))

    summary = await hr.apply_now(source="mcp_install")

    assert summary["applied"] == ["mcp"]
    assert ran == {"mcp": 1}


@pytest.mark.asyncio
async def test_apply_now_mcp_install_local_targets_mcp_seam_only(tmp_path: Path) -> None:
    """Tier 2: the parallel mcp__install_local source label also maps to the "mcp" seam."""
    ran, make = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("mcp", make("mcp"))
    hr.register_seam("skills", make("skills"))

    summary = await hr.apply_now(source="mcp__install_local")

    assert summary["applied"] == ["mcp"]
    assert ran == {"mcp": 1}


# ===========================================================================
# C. install_local probe-then-commit e2e (real Session + real stdio server)
# ===========================================================================


@pytest.mark.asyncio
async def test_local_install_new_reachable_is_live_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: installing a NEW reachable stdio server probes OK, commits, and applies
    IMMEDIATELY — the server is in the live roster + its tools are cached the SAME turn
    (no restart, no turn boundary). pending stays False (immediate, not deferred)."""
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path)
    assert "pidsrv" not in _roster_names(session)

    result = await _handle_mcp_install_local(
        {"name": "pidsrv", **_STDIO}, _session_ctx(session, tmp_path),
    )

    assert result["status"] == "ok"
    assert "pidsrv" in _roster_names(session), (
        "a NEW reachable server must be resolvable the same turn (immediate probe-commit)"
    )
    snap = session._router_host.mcp_tools_cache_snapshot
    assert snap is not None and "pidsrv" in snap, "its tools must be live-cached same turn"
    # Immediate path — nothing left pending for the turn boundary.
    residual = await session._hot_reloader.apply_pending()
    assert residual is None, "the immediate probe-commit must not ALSO defer a reload"


@pytest.mark.asyncio
async def test_local_install_probe_failure_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a NEW server that FAILS its probe (bad command) returns an error and
    writes NOTHING — probe-then-commit means no half-install (config unchanged)."""
    monkeypatch.chdir(tmp_path)
    reloader = HotReloader(project_root=tmp_path, events=EventLog())

    result = await _handle_mcp_install_local(
        {"name": "badsrv", "command": "/nonexistent/reyn-xyz", "args": []},
        _reloader_ctx(reloader, tmp_path),
    )

    assert result["status"] == "error"
    assert "badsrv" not in _servers_on_disk(tmp_path), (
        "a failed probe must leave NOTHING written (no half-install)"
    )


@pytest.mark.asyncio
async def test_local_install_same_name_overwrite_defers_and_skips_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: re-installing an EXISTING server (the documented re-install fix) is NOT
    probe-gated and NOT applied mid-turn — it keeps the deferred turn-boundary path
    (write + schedule). Proven by re-installing over a good server with a BAD command:
    an overwrite still succeeds (no probe) and schedules a deferred reload."""
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path)

    # First install (addition) → probed + immediately live.
    await _handle_mcp_install_local(
        {"name": "srv", **_STDIO}, _session_ctx(session, tmp_path),
    )
    assert "srv" in _roster_names(session)

    # Re-install SAME name with a BAD command — an overwrite skips the probe, so it
    # does NOT error; it writes + defers (documented re-install workflow preserved).
    result = await _handle_mcp_install_local(
        {"name": "srv", "command": "/nonexistent/reyn-xyz", "args": []},
        _session_ctx(session, tmp_path),
    )

    assert result["status"] == "ok", "an overwrite must NOT be probe-gated (re-install preserved)"
    pending = session._hot_reloader.pending
    assert pending is True, "an overwrite schedules the deferred turn-boundary reload"
    assert _servers_on_disk(tmp_path)["srv"]["command"] == "/nonexistent/reyn-xyz", (
        "the overwrite is written to config (clobber-update), applied at the turn boundary"
    )


@pytest.mark.asyncio
async def test_local_install_no_per_session_reloader_defers_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: with no per-session reloader (CLI separate process), a NEW install is NOT
    probe-gated and takes the deferred path — proven by a BAD command succeeding
    (written) because no probe runs."""
    monkeypatch.chdir(tmp_path)
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))
    try:
        result = await _handle_mcp_install_local(
            {"name": "clisrv", "command": "/nonexistent/reyn-xyz", "args": []},
            _Ctx(tmp_path, _RS(factory=None)),  # no op-context factory
        )
    finally:
        set_active_hot_reloader(None)

    assert result["status"] == "ok", "no per-session reloader → no probe → not error"
    assert "clisrv" in _servers_on_disk(tmp_path), "deferred path still writes config"


@pytest.mark.asyncio
async def test_local_install_probe_cancel_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: Ctrl+C during a HUNG probe → CancelledError propagates and NOTHING is
    written (the write is strictly after the probe → cancel commits nothing). The
    subprocess is spawned by a real python that never speaks MCP, so the initialize
    handshake hangs until cancelled."""
    monkeypatch.chdir(tmp_path)
    reloader = HotReloader(project_root=tmp_path, events=EventLog())
    ctx = _reloader_ctx(reloader, tmp_path)

    task = asyncio.ensure_future(_handle_mcp_install_local(
        {"name": "hung", "command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]},
        ctx,
    ))
    await asyncio.sleep(0.6)  # let the probe start + the subprocess spawn
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "hung" not in _servers_on_disk(tmp_path), (
        "a cancelled probe must leave NOTHING written (write is strictly after the probe)"
    )


@pytest.mark.asyncio
async def test_local_install_probe_cancel_event_interrupts_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #2813 — the LIVE incident this fixes. Before this fix, Ctrl-C during
    ``mcp__install_local``'s probe did NOT interrupt it: the probe ran to its own full
    ``call_timeout_seconds`` (120s default) regardless, and only the SURROUNDING turn's
    cooperative cancel flag was set (checked between tool-iteration boundaries, per
    ``Session.cancel_inflight``'s documented V1 boundary) — so the user's Ctrl-C
    appeared to hang for up to 2 minutes.

    This drives the REAL production path (``_handle_mcp_install_local``, the exact
    ``mcp__install_local`` tool the incident used — NOT the parallel ``mcp_install``
    op path, which has its own equivalent test above) with a genuinely hung stdio
    subprocess, sets ``cancel_event`` shortly after the probe starts (mirroring
    ``Session.cancel_inflight()``), and asserts the call returns FAST — well under the
    120s timeout — proving genuine early interruption, not eventual timeout expiry.
    Distinct from ``test_local_install_probe_cancel_writes_nothing`` above, which
    exercises the OTHER (still-valid, unaffected) cancellation path: an external
    ``task.cancel()`` on the whole call chain, with no ``cancel_event`` involved at
    all — this test is RED before the #2813 fix (would take ~120s to return, this
    assertion's deadline would fail) and GREEN after."""
    monkeypatch.chdir(tmp_path)
    reloader = HotReloader(project_root=tmp_path, events=EventLog())
    cancel_event = asyncio.Event()
    ctx = _reloader_ctx(reloader, tmp_path, cancel_event=cancel_event)

    task = asyncio.ensure_future(_handle_mcp_install_local(
        {"name": "hung", "command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]},
        ctx,
    ))
    await asyncio.sleep(0.6)  # let the probe start + the subprocess spawn
    cancel_event.set()

    try:
        result = await asyncio.wait_for(task, timeout=10.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "cancel_event must interrupt the in-flight probe within a few seconds — "
            "NOT wait out its own ~120s call_timeout_seconds (#2813)"
        )

    # #2813: uniform status:"cancelled" (matches the mcp/resource op cancel surface),
    # not a generic probe-error string.
    assert result["status"] == "cancelled"
    assert "hung" not in _servers_on_disk(tmp_path), (
        "a cancel_event-cancelled probe must leave NOTHING written too"
    )


@pytest.mark.asyncio
async def test_session_cancel_inflight_interrupts_a_real_hung_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #2813 production-wiring test — drives the REAL Session.cancel_inflight()
    (the actual method the TUI's Ctrl-C handler calls), NOT a hand-built OpContext with
    cancel_event set directly. test_local_install_probe_cancel_event_interrupts_immediately
    above proves the mechanism works when cancel_event is threaded in; THIS test proves
    the threading itself is wired end to end: Session construction → RouterLoopDriver's
    _set_cancel_event onto the router_host → make_router_op_context → OpContext.cancel_event
    → probe_mcp_server → MCPGateway → race_cancellable.

    A wiring-only test that hand-constructs an OpContext with cancel_event= would pass
    even if THIS production chain were entirely disconnected (a real regression class —
    see the #2788/#2801/#2802 co-vet precedent for exactly this failure mode: a
    mechanism-test green while the production call-site is silently unwired)."""
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path)

    task = asyncio.ensure_future(_handle_mcp_install_local(
        {"name": "hung", "command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]},
        _session_ctx(session, tmp_path),
    ))
    await asyncio.sleep(0.6)  # let the probe start + the subprocess spawn
    await session.cancel_inflight()  # the REAL production seam (TUI Ctrl-C → this call)

    try:
        result = await asyncio.wait_for(task, timeout=10.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "Session.cancel_inflight() must interrupt the in-flight probe within a few "
            "seconds via the real production wiring chain — NOT wait out the probe's own "
            "~120s call_timeout_seconds (#2813)"
        )

    assert result["status"] == "cancelled"
    assert "hung" not in _servers_on_disk(tmp_path)
