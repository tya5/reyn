"""Tier 2: #5179, real end-to-end connect path — does the reported drop of
the operator's OWN message actually reach through ``endpoint.py``'s real
connect code, or only through the hand-constructed SSE string the earlier
isolation test (``test_5179_remote_own_message_seq_gate_race.py``) used?

That earlier test built ``AgUiEmitter`` directly, with NO ``backlog``/
``backlog_provider`` at all, and hand-wrote ``status_provider``'s return
value and the ``user_submitted``/``turn_started`` ``EventFrame``s. It proves
the seq-gate mechanism CAN drop an already-reflected turn, but says nothing
about whether the real connect handler (``agui_events``, endpoint.py ~908-
1035) can actually produce that same shape.

This module drives the REAL pieces that function uses, in the REAL order,
with the ordering between two real reads CONTROLLED explicitly (not raced):

1. ``_SessionFrameSource(session, registry=..., agent_name=...)`` (the same
   class ``agui_events`` constructs at endpoint.py:945), started so its
   audit-event subscription is live BEFORE anything is submitted — matching
   ``agui_events``'s own ordering (``source.start()`` at line 965, before the
   backlog read at 1004).
2. ``session_backlog_page(registry, agent_name, _DEFAULT_SID)`` (endpoint.py
   :392, the exact function ``agui_events`` awaits at line 1004) — called
   FIRST, while ``session.history`` is still empty. This is the window's
   START boundary.
3. THEN, a REAL turn for the operator's own text is driven to completion
   through ``Session.submit_user_text`` (enqueue; emits the real
   ``user_submitted`` audit-event, session.py:4370) and
   ``Session.run_one_iteration`` (dispatch; emits the real ``turn_started``
   audit-event at session.py:6917, then reaches ``_handle_inbox_text``,
   whose ``Session._append_history`` call at session.py:7526 is the ACTUAL
   dispatch-commit this whole investigation is about — it runs strictly
   AFTER step 2's backlog snapshot was already taken). The turn is left to
   fail at the real litellm boundary afterward (no ``@pytest.mark.replay``
   fixture is installed, so ``reyn.dev.testing.network_gate`` raises
   ``UnpinnedNetworkReach`` the instant the router tries to call the model)
   — irrelevant to this test: everything it asserts on already happened
   before that point.
4. ``_snapshot_for_session(registry, source.current_session())`` (the exact
   call ``agui_events``'s own ``_status_provider`` closure makes at
   endpoint.py:984) is invoked ONLY once the real dispatch-commit above has
   already happened — this is the window's END boundary, and it is REAL:
   ``session.queue_seq`` genuinely reads 2 (bumped once by
   ``submit_user_text``'s ``user_submitted``, once by
   ``run_one_iteration``'s ``turn_started`` — ``Session._bump_queue_seq``,
   session.py:2389), not a hand-set fixture value.
5. A real ``AgUiEmitter`` is built with the STALE (pre-turn, empty) backlog
   from step 2, the real ``source.frames()`` as its frame source (so the
   real ``user_submitted``/``turn_started`` ``EventFrame``s it queued during
   step 3 are what get forwarded), and the status closure from step 4.
6. ``emitter.stream()`` is fed into a real ``AgUiTransport``, mounted into a
   real ``TextualChatApp`` — identical shape to
   ``test_5179_remote_own_message_seq_gate_race.py``'s own app-mounting
   pattern, reusing its ``_wait_until``/``_flow_user_entries`` helpers.

No mocks anywhere: real ``AgentRegistry`` + real ``Session``
(``tests._support.agent_session.make_session``), real audit-event bus, real
``AgUiEmitter``/``AgUiTransport``/``TextualChatApp``. The verdict is read off
PUBLIC widget state only (``FlowView.entries``, ``SentQueue.rendered_texts()``).

The two boundary points that determine how wide this window can practically
get in real operation (see the investigation's own report for the full
reasoning on what governs the width in between):

- **START**: ``endpoint.py:1004`` — ``await session_backlog_page(registry,
  agent_name, _DEFAULT_SID)``.
- **END**: ``emitter.py:135`` — ``self._model.snapshot(self._project())``
  inside ``_reconnect_snapshot_chunks``, which is the FIRST call
  ``AgUiEmitter.stream()`` ever makes to ``status_provider`` (line 140-143,
  the very top of ``stream()``, called from the ``StreamingResponse``'s
  ``gen()`` only once Starlette begins consuming the response body — i.e.
  strictly AFTER ``agui_events`` itself has already returned).
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
from reyn.interfaces.transport.agui.endpoint import _SessionFrameSource, session_backlog_page
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
async def test_own_message_dropped_via_real_connect_path(tmp_path) -> None:
    """Tier 2: reproduces (or falsifies) #5179 by driving the REAL
    ``endpoint.py`` connect-time pieces, in the REAL order, with the
    real-vs-stale backlog ordering deliberately controlled by this test
    (not raced) exactly as the module docstring lays out."""
    registry = _make_registry(tmp_path)
    AgentProfile.new(_AGENT_NAME, role="").save(
        tmp_path / ".reyn" / "agents" / _AGENT_NAME
    )
    session = await registry.ensure_running(_AGENT_NAME)

    # Real per-connection frame source (endpoint.py:945), started BEFORE
    # anything is submitted — matches agui_events's own ordering
    # (source.start() at line 965 precedes the backlog read at line 1004),
    # and is REQUIRED here: a subscription started AFTER the turn's audit-
    # events fire would simply never see them.
    source = _SessionFrameSource(session, registry=registry, agent_name=_AGENT_NAME)
    source.start()

    turn_task: "asyncio.Task | None" = None
    try:
        # ── Window START (endpoint.py:1004) ─────────────────────────────
        initial_backlog, initial_has_more, initial_next_cursor = await session_backlog_page(
            registry, _AGENT_NAME, _DEFAULT_SID,
        )
        assert initial_backlog == [], (
            "test construction error: expected an EMPTY initial backlog "
            "(nothing committed yet) — got a non-empty one, so this run "
            "does not exercise the ordering this test targets"
        )

        # ── The real dispatch-commit, driven to completion AFTER the
        # backlog snapshot above was already taken ──────────────────────
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

        # ── Window END (mirrors _status_provider, endpoint.py:967-984) ──
        def status_provider() -> dict:
            return _snapshot_for_session(registry, source.current_session())

        # Sanity: the dispatch genuinely already advanced queue_seq by the
        # time this connection's status read happens (real counter, not a
        # fixture value: session.py's Session._bump_queue_seq, one bump for
        # user_submitted, one for turn_started).
        assert session.queue_seq == 2, (
            f"test construction error: expected queue_seq==2 (user_submitted "
            f"+ turn_started already dispatched) at status-read time, got "
            f"{session.queue_seq!r}"
        )

        async def backlog_provider(name: str, sid: str):
            return await session_backlog_page(registry, name, sid)

        emitter = AgUiEmitter(
            source.frames(), status_provider,
            backlog=initial_backlog,
            backlog_has_more=initial_has_more,
            backlog_next_cursor=initial_next_cursor,
            backlog_provider=backlog_provider,
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

    assert rendered or still_queued, (
        "REPRODUCED #5179 via the REAL connect path: the operator's own "
        f"message ({_OWN_TEXT!r}) is present in NEITHER the flow "
        f"({[e.item.text for e in user_entries]!r}) NOR the sent-queue "
        f"region ({queued_texts!r}), even though the real dispatch-commit "
        "(Session._append_history) had already run before this connection's "
        "real backlog read AND its real status read reflects it as "
        "already-dispatched (queue_seq=2)."
    )
