"""Tier 2: #5179, real end-to-end connect path — the operator's OWN message,
submitted AFTER a real connection's backlog+status were already captured,
must still render once its own ``turn_started`` frame is forwarded live.

**History of this file** (kept for record, not re-litigated every read):
the first version of this test drove the connect-time backlog read and the
connect-time status read at two SEPARATELY-CONSTRUCTED times (mirroring
what ``endpoint.py``'s ``agui_events`` used to do), and reproduced #5179:
the operator's message rendered in neither ``FlowView`` nor ``SentQueue``.
Architect review of the FIX design for that bug (issuecomment-5434642964,
relayed by lead-coder) caught that the first fix candidate ("capture status
right after ``session_backlog_page`` returns") still left the SAME-direction
race open at that function's own ``extended <= 0`` exit — see
``test_5179_exit_b_status_race_discriminator.py``, which keeps demonstrating
that exact flawed candidate's own failure directly (architect's own
instruction: keep that shape unchanged post-fix, or it stops testing
anything). The adopted fix instead reads backlog and status in the SAME
synchronous tick, every loop iteration, inside
``endpoint.py._session_backlog_page_and_status`` — no separate-time read
exists to race at all, at either exit.

This module now exercises THAT real, fixed pairing: ``_SessionFrameSource``
+ ``_session_backlog_page_and_status`` (the exact call ``agui_events`` now
makes) + ``AgUiEmitter(..., initial_status=...)`` (the exact wiring
``agui_events`` now does) + real ``AgUiTransport``/``TextualChatApp`` — no
mocks. The connect's own backlog+status pairing is captured BEFORE the
operator's turn is ever submitted (there is nothing to dispatch yet at real
connect time — the general case, not a special-cased "empty" scenario);
the turn is submitted and dispatched to completion AFTER that pairing was
taken, exactly as a real operator typing after connecting would. Its own
``user_submitted``/``turn_started`` events arrive afterward via
``source.frames()`` (the SAME real subscription the connect started before
anything was submitted) with seq values ABOVE the low, pre-dispatch
``_last_seq`` this connection's own pairing seeded — so they pass the
seq-gate and promote normally. The verdict is read off PUBLIC widget state
only (``FlowView.entries``, ``SentQueue.rendered_texts()``), as before.

**Second gap, caught in PR #5293 review (architect, relayed by lead-coder,
same-day):** the test above (and every #5179 test at that point) all
hand-construct ``AgUiEmitter`` themselves, mirroring ``agui_events``'s own
wiring (the docstring above literally says "the exact wiring `agui_events`
now does") rather than ever CALLING ``agui_events`` itself. So the fix's
actual production line — ``initial_status=initial_status`` inside
``agui_events`` (endpoint.py) — could be deleted entirely and every #5179
test stayed green: the fix's witness summed to zero. Same gap #5116 already
named once in this same directory
(``test_5116_connection_agent_owner_cross_attach.py``'s own
``test_a_real_agui_events_get_pushes_correct_status_after_cross_agent_
attach``, and its own docstring's account of catching an identical "tests
mirror the wiring, never call it" gap). Same fix here:
``test_agui_events_route_pairs_connect_status_with_backlog`` below calls
the REAL route handler directly.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import (
    _session_backlog_page_and_status,
    _SessionFrameSource,
    session_backlog_page,
)
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_OWN_TEXT = "hello from the real connect path"
_AGENT_NAME = "default"


def _make_registry(tmp_path) -> AgentRegistry:
    """A real ``AgentRegistry`` whose factory builds real ``Session``s on
    demand — same pattern ``test_5094_status_provider_never_none_on_connect.
    py``'s own ``_make_registry`` uses."""

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


async def _live_sse_lines(emitter: AgUiEmitter) -> AsyncIterator[str]:
    """Adapts the REAL ``AgUiEmitter.stream()`` (an async generator of SSE
    text chunks, exactly what ``endpoint.py``'s own ``gen()`` yields to
    Starlette) into the line-at-a-time ``AsyncIterator[str]`` shape
    ``AgUiTransport`` expects — the same relationship
    ``StreamingResponse(gen())`` has to the real ASGI body. Because
    ``source.frames()`` (the emitter's frame source here) never raises
    ``StopAsyncIteration`` on its own (it only ends on a synthetic
    ``__end__`` frame, never produced in this test), this hangs open after
    the currently-queued frames drain — a genuinely open connection, not an
    exhausted stream, matching
    ``test_5179_remote_own_message_seq_gate_race.py``'s own
    ``_sse_lines``/``_sse_lines_then_hang`` precedent."""
    async for chunk in emitter.stream():
        for line in chunk.split("\n"):
            yield line
            await asyncio.sleep(0)


async def _wait_until(pilot, condition) -> None:
    """Poll ``pilot.pause()`` unboundedly until ``condition()`` is true —
    CLAUDE.md's Ceiling rule: wait on the real condition, never a fixed
    pause count. CI's own ``--timeout`` is the kill switch if it never
    resolves."""
    while not condition():
        await pilot.pause()


def _own_text_committed(session) -> bool:
    """Whether the REAL dispatch-commit (``Session._append_history``,
    session.py:7526) has actually appended the operator's own turn to
    ``session.history`` yet — the public, in-memory SSoT
    ``session_backlog_frames``/``session_backlog_page`` themselves read
    (endpoint.py:388/432), not a private attribute invented for this test."""
    return any(
        getattr(m, "role", None) == "user" and _OWN_TEXT in str(getattr(m, "content", ""))
        for m in session.history
    )


@pytest.mark.asyncio
async def test_own_message_renders_via_real_connect_path(tmp_path) -> None:
    """Tier 2: acceptance — drives the REAL, FIXED ``endpoint.py`` connect
    pairing (``_session_backlog_page_and_status``), submits the operator's
    own turn AFTER that pairing is taken (the ordinary case: nothing to
    submit yet at real connect time), and confirms it renders once its
    own ``turn_started`` frame is forwarded live."""
    registry = _make_registry(tmp_path)
    AgentProfile.new(_AGENT_NAME, role="").save(
        tmp_path / ".reyn" / "agents" / _AGENT_NAME
    )
    session = await registry.ensure_running(_AGENT_NAME)

    # Real per-connection frame source (endpoint.py:945), started BEFORE
    # anything is submitted — matches agui_events's own ordering.
    source = _SessionFrameSource(session, registry=registry, agent_name=_AGENT_NAME)
    source.start()

    turn_task: "asyncio.Task | None" = None
    try:
        # ── The real, FIXED connect-time pairing (endpoint.py's own
        # agui_events call) — backlog and status captured in the SAME
        # synchronous tick, before the operator has submitted anything. ──
        initial_backlog, initial_has_more, initial_next_cursor, initial_status = (
            await _session_backlog_page_and_status(registry, _AGENT_NAME, _DEFAULT_SID)
        )
        assert initial_backlog == [], (
            "test construction error: expected an EMPTY initial backlog "
            "(nothing submitted yet) — got a non-empty one, so this run "
            "does not exercise the ordering this test targets"
        )
        assert initial_status is not None and initial_status.get("queue_seq") == 0, (
            f"test construction error: expected the connect-time pairing's "
            f"own queue_seq to be 0 (nothing dispatched yet) — got "
            f"{initial_status!r}"
        )

        def status_provider() -> dict:
            return _snapshot_for_session(registry, source.current_session())

        # ── AFTER the connect pairing above, the operator submits and the
        # real dispatch-commit runs — exactly as a real operator typing
        # after connecting would. ──────────────────────────────────────
        async def _drive_turn() -> None:
            try:
                await session.run_one_iteration()
            except Exception:
                # Expected: no @pytest.mark.replay fixture is installed, so
                # reyn.dev.testing.network_gate's UnpinnedNetworkReach (or
                # any other failure at the real litellm boundary) fires
                # once the router tries to call the model — strictly AFTER
                # the history append/turn_started emit this test cares
                # about. Irrelevant to what this test asserts.
                pass

        await session.submit_user_text(_OWN_TEXT)  # real user_submitted audit-event
        turn_task = asyncio.create_task(_drive_turn())
        while not _own_text_committed(session):
            await asyncio.sleep(0)

        async def backlog_provider(name: str, sid: str):
            return await session_backlog_page(registry, name, sid)

        emitter = AgUiEmitter(
            source.frames(), status_provider,
            backlog=initial_backlog,
            backlog_has_more=initial_has_more,
            backlog_next_cursor=initial_next_cursor,
            backlog_provider=backlog_provider,
            initial_status=initial_status,
        )

        async def _send(_payload: dict) -> bool:
            return False

        transport = AgUiTransport(_live_sse_lines(emitter), _send)
        app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

        async with app.run_test(size=(100, 30)) as pilot:
            # The turn was already driven all the way to its (expected)
            # litellm-boundary failure BEFORE this app ever mounted (see
            # above) — every frame this connection will ever emit for this
            # turn is already queued on ``source._q`` by the time
            # ``emitter.stream()`` starts, so ``ActivityRow`` never sits in
            # a non-idle state long enough for a mount-time observer to
            # catch it (`state` goes straight Thinking → idle before the
            # first ``pilot.pause()`` returns). The real, public signal that
            # THIS turn's own lifecycle fully drained to the client instead
            # is the router-failure line landing in the flow — a downstream
            # effect of the SAME turn_settled/turn_failed audit-events the
            # turn_started one (the thing under test) rode alongside, so
            # waiting for it never presupposes the outcome being asserted
            # below.
            await _wait_until(
                pilot,
                lambda: any(
                    "litellm.acompletion" in getattr(e.item, "text", "")
                    for e in app.query_one(FlowView).entries
                ),
            )
            await pilot.pause()
            await pilot.pause()

            user_entries = _flow_user_entries(app)
            sent_queue = app.query_one(SentQueue)
            queued_texts = sent_queue.rendered_texts()

            rendered = any(_OWN_TEXT in e.item.text for e in user_entries)
            still_queued = any(_OWN_TEXT in t for t in queued_texts)
    finally:
        source.close()
        if turn_task is not None:
            # Cancelled rather than awaited to natural completion: this
            # background task's own router-loop failure path (real retry/
            # backoff + WAL/audit-event cleanup, none of it under test here)
            # otherwise entangles with ``app.run_test()``'s own teardown in
            # a way that hangs test-process shutdown — irrelevant to what
            # this test asserts, which already happened (the public widget
            # read above) before this cleanup ever runs.
            turn_task.cancel()
            try:
                await turn_task
            except (Exception, asyncio.CancelledError):
                pass
        # Real Session.run_one_iteration() above spawns the full router-loop
        # machinery (EventLog subscribers included); a bare turn_task cancel
        # leaves that background state dangling past this test's own event
        # loop, surfacing as a cross-test "Event loop is closed" warning in
        # a large sweep. registry.shutdown() is the established production
        # cleanup path (registry.py's own docstring: "stop all loaded
        # sessions, then await/cancel their tasks").
        await registry.shutdown()

    assert rendered or still_queued, (
        "#5179 REGRESSION via the REAL connect path: the operator's own "
        f"message ({_OWN_TEXT!r}) is present in NEITHER the flow "
        f"({[e.item.text for e in user_entries]!r}) NOR the sent-queue "
        f"region ({queued_texts!r}), even though it was submitted AFTER "
        "this connection's own backlog+status pairing was captured, so "
        "its own turn_started frame should promote normally once forwarded."
    )


@pytest.mark.asyncio
async def test_agui_events_route_pairs_connect_status_with_backlog(tmp_path, monkeypatch) -> None:
    """Tier 2: architect finding (PR #5293 review, relayed by lead-coder) —
    no #5179 test called the REAL ``agui_events`` route handler directly;
    every one (including the test above) hand-constructs ``AgUiEmitter``
    itself, mirroring the route's own wiring rather than executing it. So
    deleting the fix's actual production line
    (``initial_status=initial_status`` inside ``agui_events``, endpoint.py)
    left every #5179 test green — the fix's witness summed to zero. Same
    gap #5116 already named once in this directory
    (``test_5116_connection_agent_owner_cross_attach.py``'s own
    ``test_a_real_agui_events_get_pushes_correct_status_after_cross_agent_
    attach`` — its own docstring records catching an identical "tests
    mirror the wiring, never call it" gap). Same fix here: call the route
    directly (a real, minimal ``starlette.requests.Request``, not a
    stand-in), obtain its own real ``StreamingResponse``, and read what the
    PRODUCTION closure actually put on the wire.

    Constructs the exact interleaving the fix closes: the operator's turn
    is submitted and dispatched to completion AFTER ``agui_events`` already
    returned (so its own ``_session_backlog_page_and_status`` call captured
    a PRE-dispatch pairing — ``queue_seq`` 0), but BEFORE this test drains
    the response body. Without the fix, a lazy ``status_provider()`` read
    at ``stream()``-start (which only runs once something starts draining
    ``body_iterator``, i.e. after the dispatch above already committed)
    would see the ALREADY-dispatched, POST-turn value instead — 2. The
    first ``STATE_SNAPSHOT`` on the real wire must carry the PRE-dispatch
    value.

    Strip-falsifier: removing ``initial_status=initial_status`` from
    ``agui_events``'s own ``AgUiEmitter(...)`` construction (endpoint.py)
    turns this test red — the first ``STATE_SNAPSHOT`` then reports
    ``queue_seq=2`` instead of ``0``. Verified locally."""
    import json as _json

    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    registry = _make_registry(tmp_path)
    AgentProfile.new(_AGENT_NAME, role="").save(
        tmp_path / ".reyn" / "agents" / _AGENT_NAME
    )
    session = await registry.ensure_running(_AGENT_NAME)

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: registry)

    # #5130: agui_events no longer accepts a bare agent_name: str
    # parameter — the real router populates scope["path_params"] on a
    # match, so a direct (non-routed) call here must do the same.
    scope = {
        "type": "http", "method": "GET", "path": f"/agui/chat/{_AGENT_NAME}/events",
        "query_string": b"token=s3cret&connection_id=conn-5179-route-test",
        "headers": [], "client": ("127.0.0.1", 12345), "app": app,
        "path_params": {"agent_name": _AGENT_NAME},
    }
    req = Request(scope)

    turn_task: "asyncio.Task | None" = None
    try:
        # ── Call the REAL route handler — its own real, fixed connect-time
        # pairing runs here, before anything is submitted. ────────────────
        resp = await endpoint_mod.agui_events(req)
        assert isinstance(resp, StreamingResponse), (
            f"expected a real StreamingResponse (auth/agent-exists must "
            f"both pass for this test's own setup); got {resp!r}"
        )

        # ── AFTER agui_events already returned, submit + dispatch the
        # operator's real turn — BEFORE this test ever drains the body. ──
        async def _drive_turn() -> None:
            try:
                await session.run_one_iteration()
            except Exception:
                pass

        await session.submit_user_text(_OWN_TEXT)
        turn_task = asyncio.create_task(_drive_turn())
        while not _own_text_committed(session):
            await asyncio.sleep(0)

        body_iter = resp.body_iterator.__aiter__()

        async def _iter_lines():
            async for chunk in body_iter:
                for line in chunk.split("\n"):
                    if line:
                        yield line

        _lines = _iter_lines()

        async def _read_one_event():
            event_type = None
            async for line in _lines:
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    return event_type, line[len("data:"):].strip()
            raise StopAsyncIteration

        # 1st reconnect event: MESSAGES_SNAPSHOT (backlog). 2nd: STATE_
        # SNAPSHOT (status) — the one this fix is about.
        first_type, _ = await _read_one_event()
        assert first_type == "MESSAGES_SNAPSHOT", (
            f"test construction error: expected MESSAGES_SNAPSHOT as the "
            f"1st reconnect event, got {first_type!r}"
        )
        state_type, state_data = await _read_one_event()
        assert state_type == "STATE_SNAPSHOT", (
            f"test construction error: expected STATE_SNAPSHOT as the 2nd "
            f"reconnect event, got {state_type!r}"
        )
        snapshot = _json.loads(state_data).get("snapshot") or {}

        assert snapshot.get("queue_seq") == 0, (
            "#5179 REGRESSION at the REAL agui_events route: the first "
            f"STATE_SNAPSHOT reports queue_seq={snapshot.get('queue_seq')!r}, "
            "not the PRE-dispatch value (0) captured at real connect time — "
            "this means the wire is carrying a LIVE status re-read taken "
            "at stream()-start rather than the paired connect-time "
            "snapshot (the exact bug this fix closes)."
        )
    finally:
        if turn_task is not None:
            turn_task.cancel()
            try:
                await turn_task
            except (Exception, asyncio.CancelledError):
                pass
        await registry.shutdown()
