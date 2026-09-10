"""Tier 2: #5986 (lead-coder review) — ``NoopBackend.run``'s post-kill drain
read (both the cancel branch and the policy-timeout branch) had 3 defects
in ``src/reyn/security/sandbox/noop_backend.py``:

1. ``timeout=3.0`` — a bare literal with no stated rationale, byte-identical
   to the already-named :data:`~reyn.security.sandbox.policy.
   POST_KILL_DRAIN_GRACE_SECONDS`.
2. ``except (asyncio.TimeoutError, Exception):`` — ``asyncio.TimeoutError``
   IS an ``Exception`` subclass, so this caught EVERYTHING (the #5990-class
   "nobody reports this failure" shape) and silently swapped the result for
   empty output.
3. The cancel branch's own comment ("kill process group + return partial
   output") was FALSE for the except path specifically — it returns EMPTY
   output there, not partial.

Fixed: the except narrowed to ``(asyncio.TimeoutError, subprocess.
TimeoutExpired)`` — the two outcomes ``communicate_capped``'s own docstring
and this ``wait_for`` call can actually produce — with a ``_logger.warning``
on the empty-output path (a real, operator-visible fact: partial output
existed and was lost, not silence).

Real ``NoopBackend`` + a real subprocess throughout (the established idiom
from ``test_subprocess_cancel_1470.py``'s own cancel tests) — no mocks.
Forcing the drain-grace ``asyncio.TimeoutError`` deterministically (not by
RACING a small timeout against a real drain that usually finishes near-
instantly once the killed process's own pipe closes — that raced and lost,
observed by hand) needs the SAME real hang this module's own #3862
reference describes: a DETACHED grandchild (``setsid``, its own process
group) that inherits the pipe's write end and outlives the killed process
tree, so ``communicate_capped``'s blocking read never sees EOF on its own
— a real, unbounded hang this test's own ``asyncio.wait_for`` genuinely
has to interrupt, not a race against how fast a normal drain happens to
finish. ``POST_KILL_DRAIN_GRACE_SECONDS`` is monkeypatched only to keep
this fast (the grandchild would otherwise make the DEFAULT 3s grace, not
a chosen shorter one, the thing under test — the property doesn't change
with the value, since the read never completes regardless)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import pytest

from reyn.security.sandbox import noop_backend as noop_backend_module
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy

_POLICY = SandboxPolicy(timeout_seconds=30)
# #6020: the narrowed except's own `_logger.warning` call moved out of
# noop_backend.py and into the shared `_subprocess_io.drain_after_kill()`
# helper (8 byte-identical copies across 4 backend files collapsed into
# one) -- the PROPERTY this file's own tests guard (a drain that cannot
# complete in time is logged, not silently swapped for empty output) is
# unchanged; only the module that emits the warning moved, so this
# constant follows that move rather than the tests re-asserting a stale
# emission site.
_LOGGER = "reyn.security.sandbox._subprocess_io"


async def _wait_for_file(path) -> None:
    """Same unbounded-poll idiom as ``test_subprocess_cancel_1470.py``'s
    own helper — no attempt cap, CI's own kill switch is the ceiling."""
    while not path.exists():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_drain_that_cannot_complete_in_time_warns_and_returns_empty(
    tmp_path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the cancel branch's own except — real subprocess, real
    cancel, a real drain that genuinely never sees EOF (a detached
    grandchild survives the kill, per this module's own #3862 reference).
    The empty-output result must be logged, not silent.

    Strip-falsifier (verified by hand: the except reverted to
    ``except (asyncio.TimeoutError, Exception):`` with no ``_logger.warning``
    call inside it): this test goes red — no ``WARNING`` record, and the
    #5990-class silent-except shape is back."""
    monkeypatch.setattr(noop_backend_module, "POST_KILL_DRAIN_GRACE_SECONDS", 0.2)
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    backend = NoopBackend()
    event = asyncio.Event()
    ready = tmp_path / "ready"
    detached = tmp_path / "detached"
    # `setsid`(1) isn't shipped on macOS, so detaching into a NEW process
    # group (the property under test — killing the OUTER script's own
    # process group must not touch this) goes through `os.setsid()`
    # directly: the backgrounded python3 process calls it, touches its
    # OWN ready marker (an observable condition to wait on — cancelling
    # before this genuinely-async setsid()+exec has landed would leave
    # the grandchild still in the ORIGINAL group, killed along with it,
    # confirmed by hand), then `exec`s into `sleep 60` in place — one
    # process, a NEW session/pgid, still holding the inherited stdout
    # write end the ORIGINAL pipe reads from, so communicate_capped's
    # blocking read never reaches EOF on its own.
    # The grandchild writes ITS OWN pid into the ready marker — its
    # detached fd keeps `communicate_capped`'s own background executor
    # thread (a non-daemon thread, by this module's own docstring)
    # blocked reading forever, so this test kills it explicitly in
    # `finally` rather than leaving it (and pytest's own process exit)
    # hanging on a real 60s sleep.
    script = (
        f"touch {ready}\n"
        "(python3 -c \"import os; os.setsid(); "
        f"open('{detached}', 'w').write(str(os.getpid())); "
        "os.execvp('sleep', ['sleep', '60'])\" &)\n"
        "sleep 60\n"
    )

    async def _fire_cancel() -> None:
        await _wait_for_file(ready)
        await _wait_for_file(detached)
        event.set()

    fire_task = asyncio.create_task(_fire_cancel())
    try:
        result = await backend.run(["/bin/sh", "-c", script], _POLICY, cancel_event=event, env_path=None)
        await fire_task

        assert result.cancelled is True
        assert result.stdout == b"" and result.stderr == b"", (
            "the detached grandchild keeps the pipe open past the drain grace "
            "-- empty output is the correct, honest result here"
        )
        drain_warnings = [
            r.getMessage() for r in caplog.records
            if r.name == _LOGGER and r.levelname == "WARNING"
            and "could not capture partial output" in r.getMessage()
        ]
        assert drain_warnings != [], (
            f"#5986 REGRESSION: a drain that could not complete in time must be "
            f"logged, not silently swapped for empty output — got "
            f"{[(r.name, r.getMessage()) for r in caplog.records]!r}"
        )
    finally:
        grandchild_pid = int(detached.read_text())
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_a_normal_cancel_with_time_to_drain_needs_no_warning(tmp_path, caplog) -> None:
    """Tier 2: regression guard — the ordinary case (real grace, real
    drain completes) must stay exactly as quiet as before; the fix adds a
    signal on the failure path, not noise on the routine one. Reuses
    ``test_subprocess_cancel_1470.py``'s own established cancel-with-
    partial-output shape."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    backend = NoopBackend()
    event = asyncio.Event()
    ready = tmp_path / "ready"
    script = "echo partial_output\ntouch {ready}\nsleep 60\n".format(ready=ready)

    async def _fire_cancel() -> None:
        await _wait_for_file(ready)
        event.set()

    fire_task = asyncio.create_task(_fire_cancel())
    result = await backend.run(["/bin/sh", "-c", script], _POLICY, cancel_event=event, env_path=None)
    await fire_task

    assert result.cancelled is True
    drain_warnings = [
        r.getMessage() for r in caplog.records
        if r.name == _LOGGER and r.levelname == "WARNING"
        and "could not capture partial output" in r.getMessage()
    ]
    assert drain_warnings == [], (
        f"#5986 REGRESSION: a drain that completed normally must not warn — "
        f"got {drain_warnings!r}"
    )
