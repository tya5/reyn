"""Tier 2: #1092 PR-B bar-2 — the converged path's make_router_op_context provisions a
REAL phase OpContext, so a phase op is GATED by the phase PermissionDecl.

This closes the #1248 advertise/wire-path silent-FAIL class for the converged op-loop at
unit level (the bar-2 dogfood bar, made sandbox_2-independent): when a phase drives the
shared RouterLoop, op dispatch reaches the registry handler, which builds its OpContext
from ``ctx.router_state.op_context_factory`` (= PhaseRouterLoopHost.make_router_op_context).
If that factory returned None / an empty PermissionDecl, the handler would fall back to an
empty decl and silently auto-permit (the #1248 trap). This test proves the factory yields
the real phase ``PermissionDecl`` + a real (non-None) ``PermissionResolver``, and that a
write the decl does NOT allow is actually DENIED — routed through the SAME registry handler
the converged run_loop dispatches to (``_handle_write`` → ``_build_legacy_op_context`` →
``op_context_factory()`` → ``execute_op`` → ``require_file_write``).

Mock-free: real Workspace + real PermissionResolver + real PhaseRouterLoopHost +
ControlIRExecutor + the real registry handler.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.phase_router_host import PhaseRouterLoopHost
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.tools.file import _handle_write
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.workspace.workspace import Workspace


def _converged_host(tmp_path: Path, *, decl: PermissionDecl) -> PhaseRouterLoopHost:
    """Build the converged-path host with a REAL resolver + the given phase decl."""
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    ws = Workspace(events, permission_resolver=resolver, base_dir=tmp_path)
    cie = ControlIRExecutor(
        workspace=ws, events=events, permission_resolver=resolver, skill_name="bar2",
    )
    return PhaseRouterLoopHost(
        control_ir_executor=cie,
        events=events,
        phase="draft",
        decl=decl,
        allowed_ops={"write_file"},
        default_sandbox_policy=None,
        agent_name="bar2",
        agent_role="draft",
        output_language=None,
        resolve_model_fn=lambda name: name,
    )


def _dispatch_write(host: PhaseRouterLoopHost, path: str) -> dict:
    """Dispatch write_file through the SAME registry path the converged run_loop uses:
    _handle_write → _build_legacy_op_context(ctx.router_state.op_context_factory).

    The ToolContext's own events/permission_resolver/workspace are placeholders — the
    registry handler resolves its OpContext from ``router_state.op_context_factory`` (=
    the phase host's make_router_op_context), which OVERRIDES them. That override IS the
    bar-2 path under test.
    """
    events = EventLog()
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=Path("/tmp"), interactive=False,
        ),
        workspace=Workspace(events),
        caller_kind="router",
        router_state=RouterCallerState(op_context_factory=host.make_router_op_context),
    )
    return asyncio.run(_handle_write({"path": path, "content": "x"}, tool_ctx))


def test_make_router_op_context_provisions_real_phase_context(tmp_path) -> None:
    """Tier 2: make_router_op_context returns a real OpContext carrying the phase decl +
    a non-None resolver (NOT None / empty-decl auto-permit — the #1248 silent-FAIL guard)."""
    decl = PermissionDecl()
    host = _converged_host(tmp_path, decl=decl)
    op_ctx = host.make_router_op_context()
    assert op_ctx is not None, "make_router_op_context must NOT return None (the #1248 trap)"
    assert op_ctx.permission_resolver is not None, (
        "the converged OpContext must carry a REAL PermissionResolver — None auto-permits"
    )
    assert op_ctx.permission_decl is decl, (
        "the converged OpContext must carry the PHASE PermissionDecl, not an empty fallback"
    )


def test_converged_dispatch_denies_write_outside_phase_decl(tmp_path) -> None:
    """Tier 2: a write the phase PermissionDecl does NOT allow is DENIED when dispatched
    through the converged path's registry handler (the bar-2 enforcement claim).

    Falsification: adding ``denied`` to ``decl.file_write`` (see the sibling
    ``..._allows...`` test) makes the SAME write succeed — so this assertion gates on the
    decl, not on an unrelated failure.
    """
    decl = PermissionDecl()  # empty write allowlist → nothing declared writable
    host = _converged_host(tmp_path, decl=decl)
    # A project-relative path OUTSIDE the default write zone (.reyn/) — denied unless the
    # phase decl declares it (so the DECL is the gating factor, not the absolute-path rule).
    denied = "data/bar2_gate.txt"

    result = _dispatch_write(host, denied)

    blob = json.dumps(result, default=str)
    assert ("denied" in blob) or ("permission" in blob), (
        f"the write must be permission-gated (denied), not silently allowed; got {result!r}"
    )
    assert not (tmp_path / denied).exists(), "the denied write must NOT have touched the filesystem"


def test_converged_dispatch_allows_write_declared_in_phase_decl(tmp_path) -> None:
    """Tier 2: the SAME write SUCCEEDS once the phase PermissionDecl declares the path
    writable — proving the previous denial gates on the decl (the falsification control),
    and that the converged provisioning is not deny-all."""
    target = "data/bar2_gate.txt"  # same path as the denial test
    decl = PermissionDecl(file_write=[{"path": target, "scope": "just_path"}])
    host = _converged_host(tmp_path, decl=decl)

    result = _dispatch_write(host, target)

    blob = json.dumps(result, default=str)
    assert "denied" not in blob and "permission" not in blob, (
        f"a write declared in the phase decl must NOT be denied; got {result!r}"
    )
    assert (tmp_path / target).exists(), "the allowed write should have been performed"
