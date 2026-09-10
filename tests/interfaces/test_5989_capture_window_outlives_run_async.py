"""Tier 2: run_textual_chat's stray-output capture window outlives
app.run_async()'s own return, not just its await span (#5989 PR1).

The OLD scope (`with capture_stray_output(app): await app.run_async(...)`)
restored the real sys.stdout/sys.stderr the INSTANT run_async() returned.
A daemon thread `_setup_interactive_logging` starts (e.g.
`_litellm_warm_worker`) is never joined (lead-coder ruling, #5989: joining
would add a wait that collides with #6077's own "loop must not block"
concern), so it can call `logging` at ANY later moment with no lifetime tie
to run_async() at all -- and a `logging.Handler.handleError` firing in that
gap used to write straight past capture, to the real terminal (the owner's
own reported `_logger.warning(`/`--- Logging error ---` symptom).

This does not close that gap for good (a daemon thread that outlives
run_textual_chat's own RETURN, not merely run_async()'s, is still
unaddressed -- closing that needs a thread join, which #5989's own ruling
explicitly declines to add). What it DOES close: the specific, narrower gap
between run_async() returning and run_textual_chat's own remaining
teardown finishing -- proven here by observing capture is STILL active at
that exact point.

No timing/race between the daemon thread's own fire and the capture
window (CLAUDE.md: no floor/ceiling duration) -- the boundary is read as a
STATE at two fixed control-flow points instead: right when
run_textual_chat's own post-run_async teardown (`_disarm_stall_trace`)
runs, and right after run_textual_chat itself has fully returned. Neither
waits.

lead-coder BLOCKING, PR #6087: this test's `monkeypatch.setattr(...)` on
`TextualChatApp.run_async` and on `reyn.runtime.stall_trace`'s own
functions is a DELIBERATE departure from testing.md's "never fake a
collaborator when a real instance is cheaply constructible" rule -- named
here, not left for the next reader to rediscover as "wasn't this
forbidden?" ① the departure is intentional, not an oversight; ② it rests
on the SAME policy's own carve-out, "cheap to construct is not the same
as drivable": a real `TextualChatApp` IS cheap to construct, but a real
Textual boot through `run_async()` does not RETURN control back to this
test on any signal this test can drive -- it runs the app's own event
loop until something inside that loop (a `/quit`, a crash) ends it, and
nothing external can reach in and end it deterministically without a
timing-dependent hack (the exact CLAUDE.md floor/ceiling ban this file's
own paragraph above already invokes for a different reason); ③ with no
externally-drivable "return now" signal, there is no undriven way to
observe run_textual_chat's own control flow AROUND a real boot at all --
patching the ONE seam (`run_async` itself) that both IS the boundary
under test and has no other drive mechanism is the seam this test needs,
not a shortcut around a drivable one.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp, run_textual_chat
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.runtime import stall_trace


class _NullTransport(ClientTransportStub):
    """A transport that never has anything to say -- ``run_async`` is
    faked out below (this test is about ``run_textual_chat``'s own control
    flow, not a real Textual boot), so ``frames()`` is never actually
    driven."""

    async def frames(self) -> "AsyncIterator[object]":
        return
        yield  # pragma: no cover - makes this an async generator

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: object) -> None:  # pragma: no cover
        pass

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    async def cancel_inflight(self) -> str:  # pragma: no cover
        return ""

    async def shutdown(self) -> None:  # pragma: no cover
        pass


@pytest.mark.asyncio
async def test_capture_is_still_active_when_post_run_async_teardown_runs(
    monkeypatch,
) -> None:
    """Tier 2b: the strip-falsified regression. Forces the SAME
    ``_disarm_stall_trace()`` teardown ``run_textual_chat`` already calls
    (no new probe hook invented) to fire, and reads which ``sys.stderr``
    was live at that exact instant."""
    observed: "dict[str, object]" = {}

    async def _fake_run_async(self, *, inline: bool = False) -> None:
        # Stands in for a real Textual boot -- returns immediately, no
        # pump loop needed. What matters here is everything run_textual_
        # chat does AROUND this call, not Textual's own internals.
        return None

    def _probe_disarm() -> None:
        import sys

        observed["stderr_at_disarm"] = sys.stderr

    monkeypatch.setattr(TextualChatApp, "run_async", _fake_run_async)
    monkeypatch.setattr(stall_trace, "stall_trace_seconds_from_env", lambda: 5.0)
    monkeypatch.setattr(stall_trace, "arm", lambda *a, **kw: None)
    monkeypatch.setattr(stall_trace, "disarm", _probe_disarm)

    import sys

    real_stderr_before = sys.stderr

    await run_textual_chat(transport=_NullTransport(), agent_name="probe-agent")

    assert "stderr_at_disarm" in observed, (
        "the fake stall_trace.disarm() was never called -- run_textual_chat's "
        "own post-run_async teardown did not run, so this test proved nothing"
    )
    assert observed["stderr_at_disarm"] is not real_stderr_before, (
        "capture had ALREADY been torn down by the time post-run_async "
        "teardown ran -- this is the OLD, narrow-scope bug: a daemon "
        "thread emitting in this exact gap would write straight to the "
        "real terminal"
    )
    assert sys.stderr is real_stderr_before, (
        "capture must be fully restored once run_textual_chat itself has "
        "returned -- it must not stay swapped past this function's own "
        "scope (that would risk swallowing a genuinely fatal, unrelated "
        "crash the operator needs to see)"
    )
