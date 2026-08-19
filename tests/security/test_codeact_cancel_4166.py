"""Tier 2: #4166 — CodeActRunner.run(cancel_event=...) actually kills a
running snippet's subprocess, mirroring the cancel-aware race
``noop_backend``/``landlock`` already carry for the sibling (non-CodeAct)
sandboxed_exec op since #1470.

Real subprocess, real asyncio.Event, real wall-clock — no fakes of the
process or the race. The falsifying witness is elapsed time: the snippet
sleeps far longer than the test waits before cancelling, so if the kill
did NOT happen the run would take (and this test would observe) the full
sleep duration instead of returning promptly.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from reyn.core.kernel.codeact_runner import CodeActRunner


async def _dispatch(name: str, args: dict) -> dict:
    return {"status": "ok", "data": None}


@pytest.mark.asyncio
async def test_cancel_event_kills_a_running_snippet_promptly() -> None:
    """Tier 2: cancel_event, pre-set before ``run()`` is even called, returns
    status='cancelled' well before the snippet's own sleep would finish —
    the process was actually killed, not merely marked cancelled while
    left running to completion.

    Pre-set (mirrors ``test_subprocess_cancel_1470.py``'s own idiom, e.g.
    ``test_noop_cancel_event_set_kills_subprocess``), not a background task
    racing a fixed sleep against the run: the subprocess is already spawned
    and running the snippet by the time ``run()``'s internal
    ``asyncio.wait({comm_future, cancel_task}, ...)`` race begins
    (``codeact_runner.py``), so the event being pre-set doesn't make this
    vacuous — it still proves an ALREADY-RUNNING process gets killed, just
    without a sleep-based sender racing against it.

    #4847 (lead-coder review): the FLOOR duration is gone, but the CEILING
    (``elapsed < 5.0``, below) is still a duration standing in for an
    observation nothing public exposes — ``run()``'s cancelled-result dict
    (``{"ok": False, "status": "cancelled", ...}``) carries no pid /
    returncode / kill-confirmation field, so there is no non-timing seam
    to assert on from outside the function today. Elapsed-time is a
    disclosed proxy for that missing seam, not a hidden one — tracked as
    #4924, not silently left (real seam: expose the killed process's
    returncode in the result so a future version of this test can assert
    on that instead of timing)."""
    runner = CodeActRunner()
    cancel_event = asyncio.Event()
    cancel_event.set()  # pre-set: cancel fires as soon as the run's own race begins
    code = "import time\ntime.sleep(30)\nresult = 'never gets here'"

    start = time.monotonic()
    out = await runner.run(
        code=code, dispatch=_dispatch, allow_unsandboxed=True,
        timeout=30.0, cancel_event=cancel_event,
    )
    elapsed = time.monotonic() - start

    assert out["status"] == "cancelled", out
    assert out["ok"] is False, out
    # The falsifying witness: if kill_process_tree did NOT actually run,
    # the 30s sleep would have to complete (or the 30s timeout would fire)
    # before this returns. A pre-set cancel_event must not cost anywhere
    # near that — generous margin, not pinning exact timing.
    assert elapsed < 5.0, (
        f"took {elapsed:.1f}s with a pre-set cancel_event — the subprocess "
        "was not actually killed, only marked cancelled while left running"
    )


@pytest.mark.asyncio
async def test_cancel_event_never_set_runs_to_completion_unaffected() -> None:
    """Tier 2: accept-side sibling — cancel_event is provided (not None) but
    never fires, so the run must complete normally exactly as if no
    cancel_event had been passed at all. Proves the new race arm doesn't
    change behaviour for the ordinary (uncancelled) case."""
    runner = CodeActRunner()
    cancel_event = asyncio.Event()
    code = "result = 1 + 1"
    out = await runner.run(
        code=code, dispatch=_dispatch, allow_unsandboxed=True,
        cancel_event=cancel_event,
    )
    assert out["ok"] is True, out
    assert out["result"] == 2
    assert cancel_event.is_set() is False


@pytest.mark.asyncio
async def test_cancel_event_omitted_still_runs_the_pre_4166_call_shape() -> None:
    """Tier 2: the default (cancel_event=None, the call shape every existing
    caller used before #4166) still works — the new parameter is additive."""
    runner = CodeActRunner()
    out = await runner.run(
        code="result = 'ok'", dispatch=_dispatch, allow_unsandboxed=True,
    )
    assert out["ok"] is True, out
    assert out["result"] == "ok"
