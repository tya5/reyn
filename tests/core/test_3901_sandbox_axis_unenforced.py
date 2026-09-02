"""Tier 2: #3901 §4③ / #4039 — sandbox_axis_unenforced audit-event.

#4039 generalised the predicate from "can this backend express a deny-list"
(which only ever caught Landlock's structural LSM constraint — a configured
``read_deny_paths``/``write_deny_paths`` silently doing nothing there) to
"does this backend enforce what you configured" — reading each backend's own
:class:`~reyn.security.sandbox.backend.AxisEnforcementDeclaration`
(``enforced_axes``, D1: the backend's own declaration, never probed) rather
than a hardcoded "deny-list-incapable" backend-name set. This closes the
founding bug #4039 named: Noop enforces NOTHING yet the OLD predicate
reported nothing either, so a quiet Noop run and a quiet Landlock run were
indistinguishable from the audit signal alone.

Deliberately NOT wired into ``enforcement_self_test`` (CLAUDE.md hard rule:
that function is the PRODUCTION gate, blast radius every sandboxed op on
every host, deny-leg × write/spawn axes only) — this is audit visibility for
a DECLARED gap, not a self-test probe.

The pure ``unenforced_axes()`` function is tested directly (no events
needed); the audit-event wiring is driven through the REAL op handler + real
OpContext with a real (non-mock) backend stand-in — the platform-dependent
real backends (LandlockBackend/SeatbeltBackend) are exercised by
tests/security/test_sandbox_landlock.py / test_sandbox_seatbelt.py; a
stand-in reusing each real class's own ``enforced_axes`` (not a hand-typed
duplicate — see :func:`_landlock_shaped`/:func:`_seatbelt_shaped`) lets this
run cross-platform without a real Linux kernel or macOS host, while staying
faithful to what the real backend actually declares.
"""
from __future__ import annotations

import pytest

from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy, unenforced_axes

# ── the pure classifier ───────────────────────────────────────────────────────


class _BackendStandIn:
    """A real (non-mock) SandboxBackend stand-in — ``name`` + ``enforced_axes``
    only (this classifier reads nothing else), reusing a REAL backend
    class's own ``enforced_axes`` value so the stand-in cannot drift from
    what that backend actually declares."""

    def __init__(self, name: str, enforced_axes) -> None:
        self.name = name
        self.enforced_axes = enforced_axes

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None, hook_process_context=None, sink=None):
        return SandboxResult(returncode=0, stdout=b"ok\n", stderr=b"")


def _landlock_shaped() -> _BackendStandIn:
    return _BackendStandIn("landlock", LandlockBackend.enforced_axes)


def _seatbelt_shaped() -> _BackendStandIn:
    return _BackendStandIn("seatbelt", SeatbeltBackend.enforced_axes)


def _noop_shaped() -> _BackendStandIn:
    return _BackendStandIn("noop", NoopBackend.enforced_axes)


def test_landlock_with_read_deny_paths_is_unenforced():
    """Tier 2: a configured read_deny_paths on landlock is reported unenforced
    (Landlock's structural LSM allowlist-only constraint)."""
    policy = SandboxPolicy(read_deny_paths=["~/.ssh"])
    assert unenforced_axes(_landlock_shaped(), policy) == ["read_deny_paths"]


def test_landlock_with_write_deny_paths_is_unenforced():
    """Tier 2: a configured write_deny_paths on landlock is reported unenforced."""
    policy = SandboxPolicy(write_deny_paths=["~/.aws"])
    assert unenforced_axes(_landlock_shaped(), policy) == ["write_deny_paths"]


def test_landlock_with_no_deny_lists_reports_nothing():
    """Tier 2: an empty (compat-default) policy has nothing to report on
    landlock — write_deny_paths/read_deny_paths are landlock's only
    DOES_NOT_ENFORCE axes, and neither is configured here. (write_paths IS
    always configured (the workspace floor) but landlock DOES enforce it,
    so it never reports.)"""
    assert unenforced_axes(_landlock_shaped(), SandboxPolicy()) == []


def test_seatbelt_with_deny_lists_is_not_unenforced():
    """Tier 2: ★ the backend-capability falsification — the SAME policy that
    is unenforced on landlock is fully enforced on seatbelt (SBPL
    deny-after-allow, every axis ENFORCES), so seatbelt never appears in the
    report. Proves the classifier keys off the backend's OWN declaration,
    not merely "a deny-list is set"."""
    policy = SandboxPolicy(read_deny_paths=["~/.ssh"], write_deny_paths=["~/.aws"])
    assert unenforced_axes(_seatbelt_shaped(), policy) == []


def test_noop_enforces_nothing_it_is_configured_to_restrict():
    """Tier 2: #4039's founding bug, now fixed — Noop enforces NOTHING (bar
    the two env fields), so a policy that configures write/network/subprocess
    restrictions is reported in full. Under the OLD predicate this returned
    [] silently (Noop was never in the deny-list-incapable set)."""
    policy = SandboxPolicy(
        write_deny_paths=["~/.aws"],
        network=False,
        deny_subprocess=True,
    )
    assert unenforced_axes(_noop_shaped(), policy) == [
        "write_deny_paths", "network", "deny_subprocess",
    ]


def test_noop_enforces_env_deny_names():
    """Tier 2: Noop's ONE real enforcement mechanism (resolve_passthrough_env,
    shared with every other backend) — env_deny_names/allow_env_names do NOT
    appear in the report even though every other configured axis does."""
    policy = SandboxPolicy(env_deny_names=["SECRET"], allow_env_names=["PATH"])
    assert unenforced_axes(_noop_shaped(), policy) == []


def test_write_paths_configured_and_unenforced_is_reported():
    """Tier 2: #4039 — write_paths (the grant, not a deny-list) is now part
    of the reported domain. Noop enforces nothing on write, so a
    non-default write_paths grant is reported unenforced — the SandboxPolicy
    floor always sets write_paths to something, so this is the common case
    on a real Noop dispatch, not an edge case."""
    policy = SandboxPolicy(write_paths=["/repo"])
    assert unenforced_axes(_noop_shaped(), policy) == ["write_paths"]


# ── the audit-event, driven through the real op handler ──────────────────────


class _LandlockShapedBackend:
    """Real (non-mock) SandboxBackend stand-in named "landlock" — the platform
    real LandlockBackend is exercised by tests/security/test_sandbox_landlock.py; this
    lets the audit-event wiring run cross-platform without a Linux kernel."""

    name = "landlock"
    enforced_axes = LandlockBackend.enforced_axes

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None, hook_process_context=None, sink=None):
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
    from tests._support.events import collect_events, settle

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

    await settle(events)
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
    unenforced state gets its own text (#4039: Docker's real entry is the
    landed instance of exactly this — "the container's own launch-time
    boundary decides, not the policy field", a different reason than
    Landlock's LSM constraint for the SAME 2-value classification). Falls
    back to a generic statement for an unlisted backend rather than
    raising, matching unenforced_axes' own defensive posture."""
    from reyn.security.sandbox.policy import unenforced_axis_reason

    landlock_reason = unenforced_axis_reason("landlock")
    assert "Landlock" in landlock_reason or "landlock" in landlock_reason
    assert "allowlist" in landlock_reason

    docker_reason = unenforced_axis_reason("docker")
    assert "container" in docker_reason

    fallback_reason = unenforced_axis_reason("some-future-backend")
    assert "some-future-backend" in fallback_reason


@pytest.mark.asyncio
async def test_enforced_axis_emits_no_unenforced_event(tmp_path):
    """Tier 2: ★∩-falsification pair — the SAME policy on a backend that CAN
    express the deny-list (a stand-in named "seatbelt") emits no event.
    Proves the emission is keyed on the backend's OWN declaration, not
    merely on the policy carrying a deny-list."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from tests._support.events import collect_events, settle

    class _SeatbeltShapedBackend(_LandlockShapedBackend):
        name = "seatbelt"
        enforced_axes = SeatbeltBackend.enforced_axes

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

    await settle(events)
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
    from tests._support.events import collect_events, settle

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

    await settle(events)
    assert [e for e in collected if e.type == "sandbox_axis_unenforced"] == []
