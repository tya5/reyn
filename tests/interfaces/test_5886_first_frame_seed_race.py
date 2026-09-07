"""Tier 2: #5886 — in ``--connect`` remote mode, a message submitted AFTER
attach (the session idle at attach time) can be silently dropped: it never
appears as a user row, and its sent-queue placeholder is never cleared.

This is the FIRST-FRAME instance of #5179's class. #5179 closed the
*connect-time* half (``endpoint.py::_session_backlog_page_and_status`` pairs
the backlog page and the status snapshot in one synchronous tick). The
*lazy seed* half is untouched: ``TextualChatApp._seed_queue_view`` takes the
seq-gate baseline from a LIVE read of the read-model at the moment the pump
processes its FIRST non-status frame — and a ``StatusApplied``
(STATE_SNAPSHOT/STATE_DELTA) deliberately does NOT trigger that seed
(``app.py``'s own status-only branch). So a delta that advanced
``queue_seq`` between attach and the first real frame silently becomes the
gate's baseline, and the very frames the gate exists to admit
(``user_submitted`` seq 1, ``turn_started`` seq 2) are rejected as
"already reflected" — ``logger.debug`` only, invisible in a shipped config.

**Why a status delta can outrun the frames it must not outrun** (architect
ruling on #5886, 2026-09-06): ``submit_user_text`` emits the
``user_submitted`` audit-event, whose subscriber chain reaches
``endpoint._on_status_changed`` → ``_SessionFrameSource.push_status_ping()``
→ ``_q.put_nowait(StatusPingFrame())`` — **directly onto the connection's
own ordered queue**. The matching ``EventFrame`` reaches that same queue the
long way, through the session outbox hub. The emitter answers a ping with a
LIVE ``_project()`` read, so its ``STATE_DELTA`` can carry a ``queue_seq``
already past the deltas still behind it in the queue. This test builds that
sequence out of the production path only — no delivery gate, no injected
ordering: attach, then submit through the client, then let the session's own
run loop dispatch.

Harness shape is ``test_5179_remote_own_message_seq_gate_race.py``'s (real
``agui_events`` route + real ``AgUiTransport`` + a real mounted
``TextualChatApp`` on a real ``RemoteReadModel``), with the two differences
#5886 turns on: the submit happens AFTER attach, and it goes through the
CLIENT (``on_composer_submitted`` → ``_submit`` →
``transport.submit_user_text(client_ref=…)``, so the local sent-queue
placeholder is staged the way a real operator's Enter stages it), with
``_send`` bridging back into the real session exactly as ``endpoint.py``'s
own ``user_message`` handler does.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_OWN_TEXT = "the message the operator typed after attaching"
_AGENT_NAME = "default"


def _make_registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(agent_name=profile.name, agent_role=profile.role)

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _flow_user_texts(app: TextualChatApp) -> "list[str]":
    return [
        str(e.item.text) for e in app.query_one(FlowView).entries
        if e.item.kind == "user"
    ]


async def _drain_lines(
    body_iter, seen: "list[str]", gate: "asyncio.Event | None" = None,
) -> AsyncIterator[str]:
    """Adapts a real ``StreamingResponse.body_iterator`` (whole SSE chunks)
    into the line-at-a-time shape ``AgUiTransport`` expects — copied from
    ``test_5179_remote_own_message_seq_gate_race.py``'s own helper, blank
    lines included (they are the SSE block delimiter that parser relies
    on).

    ``seen`` records every line in ARRIVAL ORDER. It is a pure observer of
    the wire (append-only, never gates or reorders), and it is what lets
    this test tell its two possible greens apart: "the racing order
    occurred and the client survived it" vs "the racing order never
    occurred, so this run measured nothing" — question 4 of this repo's
    own test review, made checkable instead of assumed."""
    async for chunk in body_iter:
        for line in chunk.split("\n"):
            if gate is not None:
                await gate.wait()
            seen.append(line)
            yield line
            # NO `await asyncio.sleep(0)` here (the #5179 helper this was
            # copied from has one). That yield hands control back to the app
            # pump between every single line, which keeps the pump level with
            # its own decoder and makes the state #5886 lives in
            # unreachable. A real socket delivers many SSE lines in one read;
            # awaiting an async generator that returns immediately does not
            # yield to the loop, so `_pump_sse` gets to do what its own
            # comment says it does — decode a whole burst without yielding.


def _wire_shape(lines: "list[str]") -> "list[str]":
    """A compact, ordered census of what actually crossed the wire — the
    SSE ``event:`` names, plus a ``queue_seq=N`` marker for any data line
    carrying one. Reported by the precondition assertion below so a run
    that failed to build the racing order says what it built instead,
    rather than leaving the next reader to re-measure."""
    out: "list[str]" = []
    for line in lines:
        if line.startswith("event:"):
            out.append(line.split(":", 1)[1].strip())
        elif "queue_seq" in line:
            marker = "queue_seq=0" if ('"queue_seq": 0' in line or '"queue_seq":0' in line) else "queue_seq>0"
            out.append(f"  [{marker}]")
        elif "user_submitted" in line:
            out.append("  [user_submitted]")
        elif "turn_started" in line:
            out.append("  [turn_started]")
    return out


def _first_index(lines: "list[str]", predicate) -> "int | None":
    for i, line in enumerate(lines):
        if predicate(line):
            return i
    return None


def _advanced_queue_seq(line: str) -> bool:
    """A wire line carrying a ``queue_seq`` that is NOT 0 — i.e. a status
    projection that has already seen at least the enqueue bump."""
    if "queue_seq" not in line:
        return False
    return '"queue_seq": 0' not in line and '"queue_seq":0' not in line


async def _wait_until(pilot, condition) -> None:
    """Poll ``pilot.pause()`` unboundedly until ``condition()`` is true —
    CLAUDE.md's Ceiling rule: wait on the real condition, never a fixed
    pause count. CI's own ``--timeout`` is the kill switch."""
    while not condition():
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_a_message_submitted_after_attach_reaches_the_conversation(
    tmp_path, monkeypatch, caplog,
) -> None:
    """Tier 2: #5886's acceptance — attach to an IDLE session, then submit
    through the client. The message must be promoted into the conversation
    as a user row, and its sent-queue placeholder must be gone.

    Both halves are asserted because either alone is satisfiable by the
    wrong behaviour: "rendered" alone passes a build that also leaves a
    duplicate stuck in the queue; "not stranded" alone passes a build that
    dropped the placeholder without ever rendering anything.

    The verdict is waited for as the observable itself — the operator's own
    message in the flow — unconditionally (CI's ``--timeout`` is the red
    when the gate rejects the echo; a hang, disclosed at the wait site).
    The turn behind it runs against ``@pytest.mark.llm_stub`` so no real
    network call is on the wire.

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    registry = _make_registry(tmp_path)
    AgentProfile.new(_AGENT_NAME, role="").save(
        tmp_path / ".reyn" / "agents" / _AGENT_NAME
    )
    session = await registry.ensure_running(_AGENT_NAME)

    api = FastAPI()
    api.include_router(router)
    api.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: registry)

    scope = {
        "type": "http", "method": "GET", "path": f"/agui/chat/{_AGENT_NAME}/events",
        "query_string": b"token=s3cret&connection_id=conn-5886-first-frame-seed",
        "headers": [], "client": ("127.0.0.1", 12345), "app": api,
        "path_params": {"agent_name": _AGENT_NAME},
    }
    req = Request(scope)
    caplog.set_level(
        "DEBUG", logger="reyn.interfaces.inline.textual_chat.app",
    )

    try:
        resp = await endpoint_mod.agui_events(req)
        assert isinstance(resp, StreamingResponse), (
            f"expected a real StreamingResponse (auth/agent-exists must both "
            f"pass for this test's own setup); got {resp!r}"
        )

        async def _send(payload: dict) -> "dict | None":
            """The client→server half of the real POST route, inlined: the
            same call ``endpoint.py``'s own ``user_message`` branch makes
            (``session.submit_user_text(text, client_ref=…)``), the same
            echoed ``msg_id`` shape."""
            if payload.get("type") != "user_message":
                return None
            client_ref = payload.get("client_ref")
            msg_id = await session.submit_user_text(
                str(payload.get("text", "")),
                client_ref=client_ref if isinstance(client_ref, str) else None,
            )
            return {"status": "ok", "msg_id": msg_id}

        wire_lines: "list[str]" = []
        gate = asyncio.Event()
        gate.set()  # open: the connect-time snapshot must reach the app
        transport = AgUiTransport(
            _drain_lines(resp.body_iterator, wire_lines, gate), _send,
        )
        read_model = RemoteReadModel(transport)
        app_ = TextualChatApp(transport=transport, read_model=read_model)

        async with app_.run_test(size=(100, 30)) as pilot:
            # Attach: the connect-time STATE_SNAPSHOT (queue_seq 0 — this
            # session is idle) reaches the app. It does NOT seed the
            # seq-gate (status-only frames never do), which is the standing
            # precondition this whole class rests on.
            await _wait_until(pilot, lambda: transport.has_session())

            # Hold DELIVERY (never production): the server keeps running
            # at full speed; this only decides when bytes it has already
            # produced reach the client, which is the one variable this race
            # turns on. Released below, so the whole burst is decoded in one
            # go.
            gate.clear()

            # The operator's own Enter, through the client path, so the
            # local placeholder is staged exactly as in production.
            await app_.on_composer_submitted(Composer.Submitted(_OWN_TEXT))

            # The session's OWN run loop (started by ``ensure_running``,
            # which the route requires) dispatches the queued message —
            # nothing here drives it, exactly as in production. Wait on the
            # SERVER's own counter reaching the dispatch bump, pumping the
            # app while we wait.
            # ``Session.queue_seq`` is the public read-only accessor for
            # the same counter the status projection publishes (#3300 P2a);
            # 2 = the enqueue bump plus the dispatch bump.
            await _wait_until(pilot, lambda: session.queue_seq >= 2)

            # Release. `_pump_sse` (AgUiTransport's OWN task, client.py:507)
            # decodes the whole burst WITHOUT yielding between frames — its
            # own comment says so — applying every STATE_* to the read-model
            # as it goes, while `frames()` hands the app one frame at a time
            # through `suspend_between_frames()`. So by the time the app pump
            # reaches `user_submitted`, the read-model the lazy seed reads is
            # ALREADY at the post-dispatch `queue_seq`. That gap is the bug,
            # and it needs no wire reordering at all — only a pump that lags
            # its own decoder, which a real TUI doing real rendering work
            # does by default.
            gate.set()

            # The barrier IS the verdict's own observable, waited for
            # unconditionally: the operator's message appearing in the flow
            # (the ``user_submitted`` echo, drained by the pump). Under the
            # defect the echo is rejected by the gate and this loop never
            # ends — the red is a HANG that CI's ``--timeout`` kills, not an
            # assertion; said here because a hang wears no colour of its
            # own. (Two earlier forms waited for an ordering marker instead
            # — the turn's ``kind="error"`` reply from a REAL litellm
            # failure, then ``turn_active`` rising and falling — and both
            # were timing-shaped: the failure took the retry path in CI and
            # out-waited ``--timeout``; the flag is up too briefly for a
            # ``pilot.pause()`` poll to ever see it. The stub turn
            # (``@pytest.mark.llm_stub``) keeps the wire free of a real
            # network call either way.) The precondition below is still read
            # first, so a run that never built the racing state says so.
            await _wait_until(pilot, lambda: _OWN_TEXT in _flow_user_texts(app_))

            # ── Did the run reach the state the bug lives in? ───────────
            # NOT the wire order (measured, and it does NOT invert here:
            # `user_submitted` precedes the advancing delta on the wire).
            # The precondition that matters is that the client's READ MODEL
            # was already carrying the post-dispatch `queue_seq` — that is
            # what the lazy seed reads. `_pump_sse` decodes the released
            # burst without yielding, so it is; asserted here rather than
            # assumed, since a run where the decoder had NOT advanced would
            # make every verdict below meaningless.
            # The read model this test itself constructed and handed to
            # the app — its own public `snapshot()`, never a private
            # attribute reached back through the app.
            advanced = (read_model.snapshot() or {}).get("queue_seq", 0)
            assert advanced >= 2, (
                "this run never reached #5886's own precondition — the "
                "client's read model must already carry the post-dispatch "
                f"queue_seq when the pump seeds (saw {advanced!r}). Wire, in "
                f"arrival order: {_wire_shape(wire_lines)!r}"
            )
            # ruling ⑥, the accept side: with a correct baseline this
            # client's own submission is never rejected, so the WARNING
            # that names that fingerprint must not appear. Its RED side —
            # the warning firing when the baseline IS wrong — is witnessed
            # in ``test_5886_own_client_ref_rejection_warns.py`` (the
            # discriminator driven directly), and was also observed live on
            # the strip run that restored the lazy seed.
            own_ref_warnings = [
                r.getMessage() for r in caplog.records
                if r.levelname == "WARNING" and "baseline is wrong" in r.getMessage()
            ]
            assert not own_ref_warnings, own_ref_warnings

            user_texts = _flow_user_texts(app_)
            queued_texts = list(app_.query_one(SentQueue).rendered_texts())

            assert any(_OWN_TEXT in t for t in user_texts), (
                "#5886: the operator's own message never reached the "
                f"conversation — flow user rows: {user_texts!r}, sent-queue: "
                f"{queued_texts!r}"
            )
            assert not any(_OWN_TEXT in t for t in queued_texts), (
                "#5886: the message was left stranded in the sent-queue "
                f"region — sent-queue: {queued_texts!r}"
            )
    finally:
        await registry.shutdown()
