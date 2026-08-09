"""Tier 2: #3901 §4③ — sandbox_axis_unenforced audit-event.

Landlock cannot express a read/write deny-list (LSM allowlist-only constraint,
``landlock.py``'s own module docstring: "you cannot carve a subpath out of an
allowed parent"), so a configured ``read_deny_paths``/``write_deny_paths``
silently does nothing there — a real backend capability gap that cannot be
fixed, only made visible. Doc-only visibility reads as "written but nobody
checks it" (the #3899 pattern this issue's own thread names), so the OS emits
``sandbox_axis_unenforced`` instead — the same precedent as
``sandbox_policy_narrowed`` (#2978).

Deliberately NOT wired into ``enforcement_self_test`` (CLAUDE.md hard rule:
that function is the PRODUCTION gate, blast radius every sandboxed op on
every host, deny-leg × write/spawn axes only) — this is audit visibility for
a DECLARED backend limitation, a different mechanism entirely.

The pure ``unenforced_axes()`` function is tested directly (no backend/events
needed); the audit-event wiring is driven through the REAL op handler + real
OpContext with a real (non-mock) backend stand-in named "landlock" (the
platform-dependent real LandlockBackend is exercised by
tests/test_sandbox_landlock.py; injecting a stand-in with the same ``.name``
lets this test run cross-platform without a real Linux kernel).
"""
from __future__ import annotations

import pytest

from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.policy import SandboxPolicy, unenforced_axes

# ── the pure classifier ───────────────────────────────────────────────────────


def test_landlock_with_read_deny_paths_is_unenforced():
    """Tier 2: a configured read_deny_paths on landlock is reported unenforced."""
    policy = SandboxPolicy(read_deny_paths=["~/.ssh"])
    assert unenforced_axes("landlock", policy) == ["read_deny_paths"]


def test_landlock_with_write_deny_paths_is_unenforced():
    """Tier 2: a configured write_deny_paths on landlock is reported unenforced."""
    policy = SandboxPolicy(write_deny_paths=["~/.aws"])
    assert unenforced_axes("landlock", policy) == ["write_deny_paths"]


def test_landlock_with_no_deny_lists_reports_nothing():
    """Tier 2: an empty (compat-default) policy has nothing to report — the
    event fires only when the operator actually configured an axis this
    backend cannot enforce, not unconditionally on every landlock dispatch."""
    assert unenforced_axes("landlock", SandboxPolicy()) == []


def test_seatbelt_with_deny_lists_is_not_unenforced():
    """Tier 2: ★ the backend-capability falsification — the SAME policy that
    is unenforced on landlock is fully enforced on seatbelt (SBPL
    deny-after-allow), so seatbelt never appears in the report. Proves the
    classifier keys off backend CAPABILITY, not merely "a deny-list is set"."""
    policy = SandboxPolicy(read_deny_paths=["~/.ssh"], write_deny_paths=["~/.aws"])
    assert unenforced_axes("seatbelt", policy) == []


def test_noop_with_deny_lists_is_not_reported_as_landlock_incapable():
    """Tier 2: noop is not in the deny-list-incapable set either — its
    non-enforcement is a documented, separate contract (the one-shot WARN),
    not this axis-capability gap."""
    policy = SandboxPolicy(read_deny_paths=["~/.ssh"])
    assert unenforced_axes("noop", policy) == []


# ── the audit-event, driven through the real op handler ──────────────────────


class _LandlockShapedBackend:
    """Real (non-mock) SandboxBackend stand-in named "landlock" — the platform
    real LandlockBackend is exercised by tests/test_sandbox_landlock.py; this
    lets the audit-event wiring run cross-platform without a Linux kernel."""

    name = "landlock"

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None):
        return SandboxResult(returncode=0, stdout=b"ok\n", stderr=b"")


@pytest.mark.asyncio
async def test_unenforced_axis_emits_audit_event_through_real_op_dispatch(tmp_path):
    """Tier 2: when the resolved backend cannot enforce a configured deny-list
    axis, the sandboxed_exec op handler emits sandbox_axis_unenforced — the
    gap is observable, not silent. Driven through the REAL op handler + real
    OpContext (no mocks)."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from tests._support.events import collect_events

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        sandbox_backend=_LandlockShapedBackend(),
        default_sandbox_policy={"read_deny_paths": [str(tmp_path / "secret")]},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    unenforced = [e for e in collected if e.type == "sandbox_axis_unenforced"]
    assert unenforced, "expected a sandbox_axis_unenforced audit-event"
    assert unenforced[0].data["backend"] == "landlock"
    assert unenforced[0].data["axes"] == ["read_deny_paths"]
    # #3823: the audit-event names WHY, not just WHICH axes — the report is
    # "policy X was given, backend Y did Z with it", not a bare axis list.
    assert "cannot express a deny-list" in unenforced[0].data["reason"]


@pytest.mark.asyncio
async def test_unenforced_axis_also_emits_a_warn_log_line(tmp_path, caplog):
    """Tier 2: #3823 — the audit-event alone lands in .reyn/events, a surface
    nobody re-reads without cause (the same "written but nobody checks it"
    shape #3899 named). A WARN log line is the paired, at-the-moment
    visibility — the same channel sandbox.on_unsupported's own WARN already
    uses for a wholly-absent backend, reused here for the narrower "backend
    present but this axis unenforceable" case."""
    import logging

    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        sandbox_backend=_LandlockShapedBackend(),
        default_sandbox_policy={"read_deny_paths": [str(tmp_path / "secret")]},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    with caplog.at_level(logging.WARNING, logger="reyn.core.op_runtime.sandboxed_exec"):
        await handle(op, ctx)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARN log line when an axis is unenforced"
    assert "read_deny_paths" in warnings[0].getMessage()
    assert "landlock" in warnings[0].getMessage()


def test_unenforced_axis_reason_names_the_structural_cause() -> None:
    """Tier 1: #3823 — the reason string is per-backend prose, not a bare
    axis-name echo; a future backend with a DIFFERENT reason for the same
    unenforced state gets its own text (owner: a Docker-shaped backend
    "not enforcing env" is the SAME 2-value state as Landlock's deny-list
    gap, but a DIFFERENT reason — the image decides, not a kernel
    constraint). Falls back to a generic statement for an unlisted backend
    rather than raising, matching unenforced_axes' own defensive posture."""
    from reyn.security.sandbox.policy import unenforced_axis_reason

    landlock_reason = unenforced_axis_reason("landlock")
    assert "Landlock" in landlock_reason or "landlock" in landlock_reason
    assert "allowlist" in landlock_reason

    fallback_reason = unenforced_axis_reason("some-future-backend")
    assert "some-future-backend" in fallback_reason


@pytest.mark.asyncio
async def test_enforced_axis_emits_no_unenforced_event(tmp_path):
    """Tier 2: ★∩-falsification pair — the SAME policy on a backend that CAN
    express the deny-list (a stand-in named "seatbelt") emits no event.
    Proves the emission is keyed on backend capability, not merely on the
    policy carrying a deny-list."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from tests._support.events import collect_events

    class _SeatbeltShapedBackend(_LandlockShapedBackend):
        name = "seatbelt"

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        sandbox_backend=_SeatbeltShapedBackend(),
        default_sandbox_policy={"read_deny_paths": [str(tmp_path / "secret")]},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    assert [e for e in collected if e.type == "sandbox_axis_unenforced"] == []


@pytest.mark.asyncio
async def test_landlock_with_no_deny_lists_emits_no_unenforced_event(tmp_path):
    """Tier 2: landlock with a compat-default policy (no deny-lists configured)
    emits no event — the gap is only reported when the operator actually
    configured an axis this backend cannot enforce."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from tests._support.events import collect_events

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        sandbox_backend=_LandlockShapedBackend(),
        default_sandbox_policy={},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    assert [e for e in collected if e.type == "sandbox_axis_unenforced"] == []
