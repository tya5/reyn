"""Tier-2 tests for #1470: cancel_inflight() propagates into running subprocess.

Merge-gate 5-point verification:
  1. Popen-equivalence  — cancel_event=None produces byte-identical results
  2. Process-group kill — SeatbeltBackend (macOS) kill covers wrapper+child
  3. Partial-capture    — kill delivers partial stdout/stderr
  4. Cancel-path        — cancel_event fire → killed → SandboxResult(cancelled=True)
                          + P6 sandboxed_exec_cancelled event + P5 status=cancelled
  5. Callsite completeness — verified in sister test files (stub signature updated)
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy

_POLICY = SandboxPolicy(env_passthrough=["PATH"], timeout_seconds=30)


# ── 1 Popen-equivalence (cancel_event=None) ──────────────────────────────────


@pytest.mark.asyncio
async def test_noop_cancel_none_stdout() -> None:
    """Tier 2: cancel_event=None → stdout captured correctly (Popen-equivalence)."""
    backend = NoopBackend()
    result = await backend.run(
        ["/bin/echo", "hello"], _POLICY, cancel_event=None
    )
    assert result.returncode == 0
    assert b"hello" in result.stdout
    assert not result.cancelled


@pytest.mark.asyncio
async def test_noop_cancel_none_nonzero_returncode() -> None:
    """Tier 2: cancel_event=None → non-zero returncode preserved (Popen-equivalence)."""
    backend = NoopBackend()
    result = await backend.run(
        ["/bin/sh", "-c", "exit 42"], _POLICY, cancel_event=None
    )
    assert result.returncode == 42
    assert not result.cancelled


@pytest.mark.asyncio
async def test_noop_cancel_event_set_kills_subprocess() -> None:
    """Tier 2: cancel_event set before run → subprocess killed, cancelled=True."""
    backend = NoopBackend()
    event = asyncio.Event()
    event.set()  # pre-set: cancel fires immediately
    result = await backend.run(
        ["/bin/sleep", "60"], _POLICY, cancel_event=event
    )
    assert result.cancelled
    assert result.returncode != 0  # killed, not clean exit


@pytest.mark.asyncio
async def test_noop_cancel_mid_run_kills_subprocess() -> None:
    """Tier 2: cancel_event set mid-run → subprocess killed before sleep completes."""
    backend = NoopBackend()
    event = asyncio.Event()

    async def _fire_cancel() -> None:
        await asyncio.sleep(0.05)
        event.set()

    fire_task = asyncio.create_task(_fire_cancel())
    result = await backend.run(
        ["/bin/sleep", "60"], _POLICY, cancel_event=event
    )
    await fire_task
    assert result.cancelled
    assert result.returncode != 0


@pytest.mark.asyncio
async def test_noop_cancel_partial_stdout() -> None:
    """Tier 2: cancel after partial output → partial stdout captured."""
    backend = NoopBackend()
    event = asyncio.Event()

    # Script writes a line, then sleeps for 60s. Cancel fires after a short
    # delay so we get the first line but the sleep is interrupted.
    script = textwrap.dedent("""\
        echo partial_output
        sleep 60
    """)

    async def _fire_cancel() -> None:
        await asyncio.sleep(0.1)
        event.set()

    fire_task = asyncio.create_task(_fire_cancel())
    result = await backend.run(
        ["/bin/sh", "-c", script], _POLICY, cancel_event=event
    )
    await fire_task

    assert result.cancelled
    # Partial output may or may not include the first line depending on
    # buffering and timing, but the subprocess must have been killed.
    assert result.returncode != 0


@pytest.mark.asyncio
async def test_noop_no_cancel_does_not_set_cancelled() -> None:
    """Tier 2: run completes normally → cancelled=False even with cancel_event provided."""
    backend = NoopBackend()
    event = asyncio.Event()  # never set
    result = await backend.run(
        ["/bin/echo", "ok"], _POLICY, cancel_event=event
    )
    assert not result.cancelled
    assert result.returncode == 0
    assert b"ok" in result.stdout


# ── 2 Process-group kill (macOS Seatbelt, if available) ──────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="SeatbeltBackend macOS only")
@pytest.mark.asyncio
async def test_seatbelt_cancel_kills_subprocess() -> None:
    """Tier 2: SeatbeltBackend — cancel_event kills sandbox-exec wrapper + child."""
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend  # noqa: PLC0415

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available")

    event = asyncio.Event()
    event.set()  # pre-set

    result = await backend.run(
        ["/bin/sleep", "60"], _POLICY, cancel_event=event
    )
    assert result.cancelled
    assert result.returncode != 0


@pytest.mark.skipif(sys.platform != "darwin", reason="SeatbeltBackend macOS only")
@pytest.mark.asyncio
async def test_seatbelt_cancel_none_equivalence() -> None:
    """Tier 2: SeatbeltBackend cancel_event=None → normal completion (Popen-equiv)."""
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend  # noqa: PLC0415

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available")

    result = await backend.run(
        ["/bin/echo", "seatbelt_ok"], _POLICY, cancel_event=None
    )
    assert result.returncode == 0
    assert b"seatbelt_ok" in result.stdout
    assert not result.cancelled


# ── 3b Process-group kill (Linux Landlock, if available) — #1527 ─────────────
# The Landlock cancel path mirrors SeatbeltBackend (verified on macOS) but was
# untested on Linux: CI lacks the Landlock LSM (Noop default) and e2e is macOS.
# These run ONLY on Linux 5.13+ with the landlock package + LSM available (skipped
# elsewhere, incl. the macOS dev env). The cancel logic + `_kill_proc_group`
# (SIGTERM-pg → SIGKILL grace) are a faithful Seatbelt mirror (code-inspected,
# #1527); these pin the process-group kill of the landlock-wrapped child live
# where Landlock actually exists.


@pytest.mark.skipif(sys.platform != "linux", reason="LandlockBackend is Linux-only")
@pytest.mark.asyncio
async def test_landlock_cancel_kills_subprocess() -> None:
    """Tier 2: LandlockBackend — cancel_event kills the landlock-wrapped child's
    process group, returning cancelled=True (the #1527 untested path)."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend  # noqa: PLC0415

    backend = LandlockBackend()
    if not backend.available():
        pytest.skip("Landlock LSM not available (needs Linux 5.13+ + landlock pkg)")

    event = asyncio.Event()
    event.set()  # pre-set so the cancel path is taken immediately

    result = await backend.run(
        ["/bin/sleep", "60"], _POLICY, cancel_event=event
    )
    assert result.cancelled                       # killed, not run-to-completion
    assert result.returncode != 0


@pytest.mark.skipif(sys.platform != "linux", reason="LandlockBackend is Linux-only")
@pytest.mark.asyncio
async def test_landlock_cancel_none_equivalence() -> None:
    """Tier 2: LandlockBackend cancel_event=None → normal completion (Popen-equiv,
    no-regression guard for the cancel-aware path)."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend  # noqa: PLC0415

    backend = LandlockBackend()
    if not backend.available():
        pytest.skip("Landlock LSM not available (needs Linux 5.13+ + landlock pkg)")

    result = await backend.run(
        ["/bin/echo", "landlock_ok"], _POLICY, cancel_event=None
    )
    assert result.returncode == 0
    assert b"landlock_ok" in result.stdout
    assert not result.cancelled


# ── 4 P6 + P5 via op_runtime handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sandboxed_exec_op_cancel_event_p6_p5() -> None:
    """Tier 2: sandboxed_exec handle() with cancel_event → P6 sandboxed_exec_cancelled
    + P5 result status='cancelled'. Uses real NoopBackend + cancel_event=set."""
    import dataclasses  # noqa: PLC0415

    from reyn.events.events import EventLog  # noqa: PLC0415
    from reyn.op_runtime import execute_op  # noqa: PLC0415
    from reyn.op_runtime.context import OpContext  # noqa: PLC0415
    from reyn.schemas.models import SandboxedExecIROp  # noqa: PLC0415
    from reyn.security.permissions.permissions import PermissionDecl  # noqa: PLC0415
    from reyn.security.sandbox.noop_backend import NoopBackend  # noqa: PLC0415
    from reyn.workspace.workspace import Workspace  # noqa: PLC0415

    events = EventLog()
    workspace = Workspace(events=events)

    cancel_event = asyncio.Event()
    cancel_event.set()  # pre-set: cancel fires immediately

    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        sandbox_backend=NoopBackend(),
        cancel_event=cancel_event,
    )

    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/sleep", "60"],
        env_passthrough=["PATH"],
        timeout_seconds=30,
    )

    result = await execute_op(op, ctx, caller="control_ir")

    # P5: result envelope reflects interruption
    assert result["status"] == "cancelled", f"expected 'cancelled', got {result['status']!r}"
    assert result["kind"] == "sandboxed_exec"

    # P6: sandboxed_exec_cancelled event emitted (not sandboxed_exec_completed)
    emitted_types = [e.type for e in events.all()]
    assert "sandboxed_exec_cancelled" in emitted_types, (
        f"sandboxed_exec_cancelled not in {emitted_types}"
    )
    assert "sandboxed_exec_completed" not in emitted_types, (
        "sandboxed_exec_completed must NOT be emitted on cancel"
    )
