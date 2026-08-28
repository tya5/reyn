"""Tier 2: #5179 — in ``--connect`` remote mode, the operator's OWN sent
message could silently never appear in the conversation, while the agent's
reply rendered fine.

**History of this file.** The first version hand-constructed ``AgUiEmitter``
directly with a deliberately-inconsistent ``status_provider`` (already
reflecting the post-dispatch ``queue_seq`` from its very first line) to
reproduce the race in isolation — real ``AgUiEmitter``/``AgUiTransport``/
``TextualChatApp``, but never calling through ``session_backlog_page``/
``endpoint.py`` at all. That was a valid demonstration of the seq-gate
mechanism (``RemoteQueueView``) dropping an already-reflected turn when
handed status inconsistent with the frames it is paired with — but it meant
this test could never be closed by the endpoint-level fix, so it stayed
permanently red even after the fix landed. CI correctly refused to merge a
PR with a red test (lead-coder, PR #5293 review) — the SAME fact architect's
review had already caught from the design side (its witness summed to zero
across every #5179 test at that point, this one included, since none of
them called ``agui_events`` itself).

**Rewritten (per lead-coder's own recommendation ①, matching the fix
already applied to ``test_5179_backlog_gap_end_to_end.py``'s own gap) to
call the REAL ``agui_events`` route handler directly** — same precedent
this directory already established
(``test_5116_connection_agent_owner_cross_attach.py``'s own
``test_a_real_agui_events_get_pushes_correct_status_after_cross_agent_
attach``: "This test calls the REAL agui_events route handler DIRECTLY").
This is now the THIRD, complementary angle on #5179's acceptance coverage,
each testing something the other two don't:

- ``test_5179_backlog_gap_end_to_end.py::test_own_message_renders_via_real_
  connect_path`` — hand-constructs ``AgUiEmitter`` mirroring
  ``agui_events``'s own wiring, mounts a full ``TextualChatApp``. Cheap,
  controllable, but (as PR #5293 review caught) does not itself execute
  the production call site.
- ``test_5179_backlog_gap_end_to_end.py::test_agui_events_route_pairs_
  connect_status_with_backlog`` — calls the REAL route, but reads the raw
  SSE wire directly (no app), checking only the first ``STATE_SNAPSHOT``'s
  ``queue_seq``.
- **This file** — calls the REAL route AND mounts a full
  ``TextualChatApp``, reading the verdict off PUBLIC widget state
  (``FlowView.entries``, ``SentQueue.rendered_texts()``) — the same
  end-to-end rendering check the very first version of this test made,
  now actually reached through ``agui_events`` itself.

Real chain (prior investigation, summarized in the issue thread, unchanged
by the rewrite):

- The operator's own message is NEVER folded into the flow directly.
  ``TextualChatApp._handle_user_submitted_event`` only STAGES it into
  ``RemoteQueueView`` (the sent-queue region); a LATER ``turn_started``
  whose ``chain_id`` matches is what actually PROMOTES it into a flow
  entry (``_handle_turn_started_event`` → ``_ingest_frame``). Both calls
  are gated by ``RemoteQueueView``'s own monotonic ``seq`` gate
  (``apply_user_submitted``/``apply_turn_started`` — state.py): a delta
  whose ``seq`` is ``<=`` the view's ``_last_seq`` is silently rejected.
- ``_last_seq`` is seeded exactly ONCE per connection, from a live read of
  ``transport.status.values`` (``RemoteReadModel.snapshot()``), at
  ``TextualChatApp._seed_queue_view``, itself called on the FIRST
  non-``BacklogBatch`` frame ``_pump_frames`` ever drains.
- The fix (``endpoint.py._session_backlog_page_and_status``) pairs the
  connect-time backlog and status in the SAME synchronous tick, so the
  seed this connection gets is never advanced ahead of frames it hasn't
  forwarded yet — see that function's own docstring for the full
  reasoning.

Real ``AgentRegistry``/``Session`` + a real ``agui_events`` call + real
``AgUiTransport`` (client-side decoder) + a real mounted ``TextualChatApp``
wired to a real ``RemoteReadModel`` — no mocks, no private-state pokes.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_OWN_TEXT = "hello from the operator"
_AGENT_NAME = "default"


def _make_registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(agent_name=profile.name, agent_role=profile.role)

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


async def _drain_lines(body_iter) -> AsyncIterator[str]:
    """Adapts a real ``StreamingResponse.body_iterator`` (whole SSE chunks,
    the exact thing Starlette itself would stream to a real client) into
    the line-at-a-time ``AsyncIterator[str]`` shape ``AgUiTransport``
    expects — mirrors ``test_5179_backlog_gap_end_to_end.py``'s own
    ``_live_sse_lines``. Blank lines are NOT filtered (unlike a
    manual-scan helper would) — they are the SSE block delimiter
    ``AgUiTransport``'s own parser relies on."""
    async for chunk in body_iter:
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


@pytest.mark.asyncio
async def test_own_message_renders_via_real_agui_events_route(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — calls the REAL ``agui_events`` route handler,
    submits+dispatches the operator's own turn AFTER it returns (mirrors
    ``test_agui_events_route_pairs_connect_status_with_backlog``'s own
    ordering), and confirms the message renders in a REAL mounted
    ``TextualChatApp`` fed off the route's own real ``StreamingResponse``.

    Strip-falsifier: removing ``initial_status=initial_status`` from
    ``agui_events``'s own ``AgUiEmitter(...)`` construction (endpoint.py)
    turns this test red — the operator's own message renders in neither
    the flow nor the sent-queue region. Verified locally (same strip
    already used for ``test_agui_events_route_pairs_connect_status_with_
    backlog``)."""
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
        "query_string": b"token=s3cret&connection_id=conn-5179-full-app-route-test",
        "headers": [], "client": ("127.0.0.1", 12345), "app": app,
        "path_params": {"agent_name": _AGENT_NAME},
    }
    req = Request(scope)

    turn_task: "asyncio.Task | None" = None
    try:
        resp = await endpoint_mod.agui_events(req)
        assert isinstance(resp, StreamingResponse), (
            f"expected a real StreamingResponse (auth/agent-exists must "
            f"both pass for this test's own setup); got {resp!r}"
        )

        async def _drive_turn() -> None:
            try:
                await session.run_one_iteration()
            except Exception:
                # Expected: no @pytest.mark.replay fixture is installed —
                # the real litellm boundary raises once the router reaches
                # it, strictly after the history append this test cares
                # about.
                pass

        await session.submit_user_text(_OWN_TEXT)
        turn_task = asyncio.create_task(_drive_turn())
        while not any(
            getattr(m, "role", None) == "user" and _OWN_TEXT in str(getattr(m, "content", ""))
            for m in session.history
        ):
            await asyncio.sleep(0)

        async def _send(_payload: dict) -> bool:
            return False

        transport = AgUiTransport(_drain_lines(resp.body_iterator), _send)
        app_ = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

        async with app_.run_test(size=(100, 30)) as pilot:
            # The turn was already driven all the way to its (expected)
            # litellm-boundary failure BEFORE this app ever mounted (see
            # above) — every frame this connection will ever emit for this
            # turn is already queued by the time ``emitter.stream()``
            # starts, so ``ActivityRow`` never sits in a non-idle state
            # long enough for a mount-time observer to catch it (mirrors
            # ``test_5179_backlog_gap_end_to_end.py``'s own reasoning). The
            # real, public signal that THIS turn's own lifecycle fully
            # drained to the client instead is the router-failure line
            # landing in the flow.
            await _wait_until(
                pilot,
                lambda: any(
                    "litellm.acompletion" in getattr(e.item, "text", "")
                    for e in app_.query_one(FlowView).entries
                ),
            )
            await pilot.pause()
            await pilot.pause()

            user_entries = _flow_user_entries(app_)
            sent_queue = app_.query_one(SentQueue)
            queued_texts = sent_queue.rendered_texts()

            rendered = any(_OWN_TEXT in e.item.text for e in user_entries)
            still_queued = any(_OWN_TEXT in t for t in queued_texts)

            assert rendered or still_queued, (
                "#5179 REGRESSION via the REAL agui_events route: the "
                f"operator's own message ({_OWN_TEXT!r}) is present in "
                f"NEITHER the flow ({[e.item.text for e in user_entries]!r}) "
                f"NOR the sent-queue region ({queued_texts!r})."
            )
    finally:
        if turn_task is not None:
            turn_task.cancel()
            try:
                await turn_task
            except (Exception, asyncio.CancelledError):
                pass
        await registry.shutdown()
