"""#2978 — a read_deny_paths entry always wins over an overlapping write_paths grant.

Before the fix the Seatbelt backend emitted each `write_paths` allow-read AFTER
the `read_deny_paths` deny rules (SBPL is last-match-wins), so a broad write
grant (`$HOME`, `/`) that engulfed a credential path silently re-opened it for
both read and write — the shipped `read_deny_paths` defense-in-depth was
nullified with no signal. The fix emits the deny-list AFTER the write grants so
the deny ALWAYS wins (owner rule: "a deny that loses to an allow is not a
deny"), denies BOTH read and write on the deny path (a write grant otherwise
leaves an engulfed credential path WRITABLE even once reads are denied), and
emits a `sandbox_policy_narrowed` audit-event so the narrowing is never silent.

Scope: Seatbelt only. Landlock has no read-deny primitive (allowlist-only), so
this hazard cannot exist there and the fix does not touch it.

The behavioral tests drive the REAL SeatbeltBackend against a REAL process with a
REAL SBPL profile — a hand-built SandboxPolicy proves the mechanism but a real
sandbox-exec run proves the wiring (a profile that "looks right" can still
permit the read). They are hermetic: the deny target is a temp file the test
creates, so the assertion is never vacuous on a machine without `~/.ssh`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from reyn.security.sandbox.policy import (
    SandboxPolicy,
    deny_narrowed_write_grants,
)

# ── the narrowing detector (pure) ─────────────────────────────────────────────


def test_narrowing_detected_when_deny_engulfed_by_write(tmp_path):
    """Tier 2: a deny path under a broad write grant is reported as narrowed."""
    write = tmp_path
    deny = tmp_path / "secret"
    pol = SandboxPolicy(write_paths=[str(write)], read_deny_paths=[str(deny)])
    assert deny_narrowed_write_grants(pol) == [(str(write), str(deny))]


def test_narrowing_detected_when_write_engulfed_by_deny(tmp_path):
    """Tier 2: an explicit write grant fully inside a deny prefix is also reported
    (the deny nullifies the whole grant — the operator should know)."""
    deny = tmp_path
    write = tmp_path / "inside"
    pol = SandboxPolicy(write_paths=[str(write)], read_deny_paths=[str(deny)])
    assert deny_narrowed_write_grants(pol) == [(str(write), str(deny))]


def test_no_narrowing_when_disjoint(tmp_path):
    """Tier 2: disjoint write/deny paths produce no narrowing (no false positive)."""
    pol = SandboxPolicy(
        write_paths=[str(tmp_path / "a")], read_deny_paths=[str(tmp_path / "b")]
    )
    assert deny_narrowed_write_grants(pol) == []


# ── the enforcement (real backend, real process) ─────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_deny_wins_over_overlapping_write_grant_read_and_write(tmp_path):
    """Tier 2: a deny path ENGULFED by a broad write grant stays denied for BOTH
    read and write — the #2978 hazard, hermetic.

    Strip-falsify: with the pre-fix ordering (write allow AFTER deny) the secret
    is readable AND writable (rc=0) — observed on the unmodified production code.
    With the fix (deny AFTER write) both are denied (rc!=0). A SIBLING file under
    the same write grant stays writable, proving the deny is not over-broad.
    """
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    sibling = tmp_path / "ok.txt"

    # Broad write grant over tmp_path, deny the secret subpath — the exact
    # overlap shape (deny engulfed by a broad write grant) that #2978 is about.
    policy = SandboxPolicy(
        write_paths=[str(tmp_path)],
        read_deny_paths=[str(secret)],
        allow_subprocess=True,
    )

    def _run(argv: list[str]) -> int:
        wrapped = backend.wrap_command(argv, policy)
        try:
            return subprocess.run(wrapped.argv, capture_output=True, timeout=30).returncode
        finally:
            wrapped.cleanup()

    assert _run(["/bin/cat", str(secret)]) != 0, "deny lost to the write grant (read)"
    assert _run(["/usr/bin/touch", str(secret)]) != 0, "deny lost to the write grant (write)"
    # the deny is scoped, not over-broad: a sibling under the write grant works.
    assert _run(["/usr/bin/touch", str(sibling)]) == 0, "write grant broke for a non-denied sibling"
    assert sibling.exists()


# ── the audit-event (never silent) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_narrowing_emits_audit_event_through_real_op_dispatch(tmp_path):
    """Tier 2: when a deny narrows a write grant, the sandboxed_exec op handler
    emits a `sandbox_policy_narrowed` audit-event — the narrowing is observable,
    not silent. Driven through the REAL op handler + real OpContext (no mocks)."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    ws = Workspace(events=events)
    secret = tmp_path / "secret"
    secret.write_text("x")
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        # broad write grant over tmp_path, deny the secret subpath → narrowing.
        default_sandbox_policy={
            "write_paths": [str(tmp_path)],
            "read_deny_paths": [str(secret)],
        },
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    narrowed = [e for e in events.all() if e.type == "sandbox_policy_narrowed"]
    assert narrowed, "expected a sandbox_policy_narrowed audit-event when a deny wins"
    pairs = [p for e in narrowed for p in e.data["narrowed"]]
    assert {"write_path": str(tmp_path), "deny_path": str(secret)} in pairs


@pytest.mark.asyncio
async def test_no_narrowing_no_audit_event(tmp_path):
    """Tier 2: a clean policy (write/deny disjoint) emits NO narrowing event —
    the event fires only when a deny actually wins over a grant."""
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
        default_sandbox_policy={
            "write_paths": [str(tmp_path / "work")],
            "read_deny_paths": [str(tmp_path / "creds")],
        },
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    assert [e for e in events.all() if e.type == "sandbox_policy_narrowed"] == []
