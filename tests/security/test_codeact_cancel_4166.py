"""Tier 2: #4166 — CodeActRunner.run(cancel_event=...) invokes the kill path
on a running snippet's subprocess, mirroring the cancel-aware race
``noop_backend``/``landlock`` already carry for the sibling (non-CodeAct)
sandboxed_exec op since #1470.

Real subprocess, real asyncio.Event — no fakes of the process or the race.
The witness is ``out["killed"]`` (#4924), not elapsed time: #4923 removed
the sleep-based FLOOR this file used to have and disclosed that the
CEILING (an ``elapsed < N`` assertion) was still standing in for an
observation nothing public exposed. #4924 closed that by adding a real
non-timing signal to ``run()``'s cancelled-result envelope — see
``codeact_runner.py``'s own comment on the ``killed`` field for the
discriminated-union design (``killed`` is data attached to the existing
``status == "cancelled"`` branch, never a second discriminator a consumer
would need to probe for presence).

Scope, precisely (lead-coder review — the test docstring below originally
overclaimed this): ``killed=True`` witnesses that ``kill_process_tree(proc)``
was CALLED and RETURNED for this cancellation — not that the OS process is
CONFIRMED dead. ``kill_process_tree()`` has no return value today (#4924's
own reasoning for choosing this field over a real ``returncode``), so
whether the graceful (SIGTERM) or forced (SIGKILL-after-grace) path fired,
and whether the process actually exited, is not observable from this seam.
The elapsed-time witness this replaces used to ALSO indirectly cover "the
process was not left running to completion" (an unrelated snippet, if left
alive past this call, would still be consuming CPU/holding resources even
though ``run()`` itself returned) — this new witness does not cover that
either. Both gaps are ``kill_process_tree()``'s own responsibility to close
(a future OS-level confirmation, #4924's disclosed "option ① later" path),
not something a caller-side test can currently verify.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.kernel.codeact_runner import CodeActRunner


async def _dispatch(name: str, args: dict) -> dict:
    return {"status": "ok", "data": None}


@pytest.mark.asyncio
async def test_cancel_event_kills_a_running_snippet_promptly() -> None:
    """Tier 2: cancel_event, pre-set before ``run()`` is even called, returns
    status='cancelled' with ``killed=True`` — ``kill_process_tree`` was
    genuinely invoked (and returned) for this cancellation, not merely
    marked cancelled while the subprocess was left running from this
    call's own perspective. See the module docstring's own "Scope,
    precisely" section for what this does NOT claim (OS-level death
    confirmation, or that the process wasn't left running past this
    call — both are ``kill_process_tree()``'s own unclosed gap, not this
    test's).

    Pre-set (mirrors ``test_subprocess_cancel_1470.py``'s own idiom, e.g.
    ``test_noop_cancel_event_set_kills_subprocess``), not a background task
    racing a fixed sleep against the run: the subprocess is already spawned
    and running the snippet by the time ``run()``'s internal
    ``asyncio.wait({comm_future, cancel_task}, ...)`` race begins
    (``codeact_runner.py``), so the event being pre-set doesn't make this
    vacuous — it still proves the kill path fires against an
    ALREADY-RUNNING process, just without a sleep-based sender racing
    against it.

    ``killed`` is tied to the LINE right after ``kill_process_tree(proc)``
    returns (``codeact_runner.py``'s own comment on the ``killed`` local),
    not a literal baked into the return dict — a hardcoded ``True`` there
    would be tautological with ``status == "cancelled"`` (true on every
    path reaching that return, including a hypothetical future regression
    that reaches it WITHOUT ever calling ``kill_process_tree`` at all),
    closing none of the gap #4923 disclosed. This PR's own strip-falsify
    (removing the ``kill_process_tree(proc)`` call entirely — see the PR
    body, not a permanent sibling test) confirmed a naive literal would
    NOT have caught that regression, which is why the field is wired to
    real execution instead."""
    runner = CodeActRunner()
    cancel_event = asyncio.Event()
    cancel_event.set()  # pre-set: cancel fires as soon as the run's own race begins
    code = "import time\ntime.sleep(30)\nresult = 'never gets here'"

    out = await runner.run(
        code=code, dispatch=_dispatch, allow_unsandboxed=True,
        timeout=30.0, cancel_event=cancel_event,
    )

    assert out["status"] == "cancelled", out
    assert out["ok"] is False, out
    assert out["killed"] is True, (
        "cancelled but killed=False — kill_process_tree was not invoked "
        "for this cancellation (the subprocess was marked cancelled without "
        "the kill path ever firing)"
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
