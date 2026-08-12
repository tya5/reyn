"""#2978 — a write_deny_paths entry always wins over an overlapping write_paths grant.

Before the fix the Seatbelt backend emitted each `write_paths` allow-read AFTER
the deny rules (SBPL is last-match-wins), so a broad write grant (`$HOME`, `/`)
that engulfed a credential path silently re-opened it — the shipped
defense-in-depth deny-list was nullified with no signal. The fix emits the
deny-list AFTER the write grants so the deny ALWAYS wins (owner rule: "a deny
that loses to an allow is not a deny"), and emits a `sandbox_policy_narrowed`
audit-event so the narrowing is never silent.

#3901 PR-B ④: `read_deny_paths` and `write_deny_paths` are now separate,
explicit fields, each denying only its own axis — before PR-B, `read_deny_paths`
ALSO emitted an (undocumented) file-write* deny as a side-effect on Seatbelt
only (Landlock never replicated it, so the same policy meant different things
per OS). `deny_narrowed_write_grants` — the pure function this module's first
tests exercise — follows `write_deny_paths` accordingly: it is the write axis's
OWN deny-list now, not `read_deny_paths`'s side-effect.

Scope: Seatbelt only. Landlock has no deny primitive (allowlist-only), so this
hazard cannot exist there and the fix does not touch it.

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
    pol = SandboxPolicy(write_paths=[str(write)], write_deny_paths=[str(deny)])
    assert deny_narrowed_write_grants(pol) == [(str(write), str(deny))]


def test_narrowing_detected_when_write_engulfed_by_deny(tmp_path):
    """Tier 2: an explicit write grant fully inside a deny prefix is also reported
    (the deny nullifies the whole grant — the operator should know)."""
    deny = tmp_path
    write = tmp_path / "inside"
    pol = SandboxPolicy(write_paths=[str(write)], write_deny_paths=[str(deny)])
    assert deny_narrowed_write_grants(pol) == [(str(write), str(deny))]


def test_no_narrowing_when_disjoint(tmp_path):
    """Tier 2: disjoint write/deny paths produce no narrowing (no false positive)."""
    pol = SandboxPolicy(
        write_paths=[str(tmp_path / "a")], write_deny_paths=[str(tmp_path / "b")]
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

    #3901 PR-B ④: `read_deny_paths` and `write_deny_paths` are now separate
    fields, each denying only its own axis (the pre-PR-B behavior — a
    `read_deny_paths` entry ALSO denying writes — was an undocumented Seatbelt
    side-effect, #3901's own module docstring names it an accident). This test
    keeps its "both axes denied" claim by declaring the secret on BOTH fields —
    what an operator now does explicitly for full protection.
    """
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    sibling = tmp_path / "ok.txt"

    # Broad write grant over tmp_path, deny the secret subpath on BOTH axes —
    # the exact overlap shape (deny engulfed by a broad write grant) that
    # #2978 is about.
    policy = SandboxPolicy(
        write_paths=[str(tmp_path)],
        read_deny_paths=[str(secret)],
        write_deny_paths=[str(secret)],
        deny_subprocess=False,
    )

    def _run(argv: list[str]) -> int:
        wrapped = backend.wrap_command(argv, policy)
        try:
            return subprocess.run(wrapped.argv, capture_output=True).returncode  # #4397: no test-owned timeout
        finally:
            wrapped.cleanup()

    assert _run(["/bin/cat", str(secret)]) != 0, "deny lost to the write grant (read)"
    assert _run(["/usr/bin/touch", str(secret)]) != 0, "deny lost to the write grant (write)"
    # the deny is scoped, not over-broad: a sibling under the write grant works.
    assert _run(["/usr/bin/touch", str(sibling)]) == 0, "write grant broke for a non-denied sibling"
    assert sibling.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_read_deny_and_write_deny_are_independent_axes(tmp_path):
    """Tier 2: ★ #3901 PR-B ④'s own falsification — `read_deny_paths` alone
    denies the READ, and leaves the WRITE untouched (and symmetrically for
    `write_deny_paths` alone). Before PR-B a single `read_deny_paths` entry
    denied both axes (the accident #3901's docstring names); if that coupling
    silently came back, this is the test that would catch it."""
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    def _run(policy: SandboxPolicy, argv: list[str]) -> int:
        wrapped = backend.wrap_command(argv, policy)
        try:
            return subprocess.run(wrapped.argv, capture_output=True).returncode  # #4397: no test-owned timeout
        finally:
            wrapped.cleanup()

    read_only_deny = tmp_path / "read-denied.txt"
    read_only_deny.write_text("x")
    read_policy = SandboxPolicy(
        write_paths=[str(tmp_path)],
        read_deny_paths=[str(read_only_deny)],
        deny_subprocess=False,
    )
    assert _run(read_policy, ["/bin/cat", str(read_only_deny)]) != 0, (
        "read_deny_paths must deny the read"
    )
    assert _run(read_policy, ["/usr/bin/touch", str(read_only_deny)]) == 0, (
        "read_deny_paths alone must NOT deny the write — that coupling was the "
        "pre-#3901 accident, and this policy never declared write_deny_paths"
    )

    write_only_deny = tmp_path / "write-denied.txt"
    write_only_deny.write_text("x")
    write_policy = SandboxPolicy(
        write_paths=[str(tmp_path)],
        write_deny_paths=[str(write_only_deny)],
        deny_subprocess=False,
    )
    assert _run(write_policy, ["/usr/bin/touch", str(write_only_deny)]) != 0, (
        "write_deny_paths must deny the write"
    )
    assert _run(write_policy, ["/bin/cat", str(write_only_deny)]) == 0, (
        "write_deny_paths alone must NOT deny the read — reads stay broad "
        "(#1199) unless read_deny_paths says otherwise"
    )


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
    from tests._support.events import collect_events

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events)
    secret = tmp_path / "secret"
    secret.write_text("x")
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        # broad write grant over tmp_path, deny the secret subpath → narrowing.
        # #3901 PR-B ④: deny_narrowed_write_grants follows write_deny_paths
        # (the write axis's OWN deny-list), not read_deny_paths.
        default_sandbox_policy={
            "write_paths": [str(tmp_path)],
            "write_deny_paths": [str(secret)],
        },
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    narrowed = [e for e in collected if e.type == "sandbox_policy_narrowed"]
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
    from tests._support.events import collect_events

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={
            "write_paths": [str(tmp_path / "work")],
            "write_deny_paths": [str(tmp_path / "creds")],
        },
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])
    await handle(op, ctx)

    assert [e for e in collected if e.type == "sandbox_policy_narrowed"] == []
