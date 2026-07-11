"""Tier 2: an operator can drive an intervention end-to-end over the wire (P3).

Reachability-for-purpose (the arc is COMPLETE only when an actor can reach + use
it in a real run): a real intervention announced by a real ``Session`` is emitted
to AG-UI SSE by the real ``AgUiEmitter``, the real ``AgUiTransport`` decodes it and
learns the pending intervention BY ID off the frontend-tool, and the operator's
answer round-trips back through the transport's ``send`` seam to
``answer_intervention_by_id`` — resolving the exact intervention and resuming the
run. This is the whole HITL loop with real instances end to end (no HTTP flake).

Also asserts the ``reyn chat --connect`` command surface exists (the CLI is the
operator's entry point to this loop).

Real ``Session`` + real ``AgUiEmitter`` + real ``AgUiTransport`` — no mocks.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from reyn.user_intervention import UserIntervention


def _make_session(tmp_path: Path) -> Session:
    session = Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.register_intervention_listener("tui")
    return session


async def _frames(items):
    for it in items:
        yield it


async def _sse_lines(text: str):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_operator_drives_intervention_over_the_wire(tmp_path, monkeypatch) -> None:
    """Tier 2: announce → SSE → client learns pending id → answer → run resumes."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    iv = UserIntervention(kind="ask_user", prompt="Deploy to prod?", run_id="r1")
    iv.future = asyncio.get_running_loop().create_future()
    dispatch_task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await wait_until(lambda: bool(session.interventions.list_active()))

    # The real announce landed an intervention OutboxMessage on the session outbox.
    announce_msg = await asyncio.wait_for(session.outbox.get(), timeout=2.0)
    assert announce_msg.kind == "intervention"
    assert announce_msg.meta.get("intervention_id") == iv.id

    # Server side: emit the announce frame (+ terminator) to AG-UI SSE.
    server_frames = [
        DisplayFrame(announce_msg),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    emitter = AgUiEmitter(_frames(server_frames), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])

    # Client side: the send seam delivers an answer BY ID to the real session
    # (the endpoint's post-auth delivery, in-process for a flake-free e2e).
    async def send(payload: dict) -> bool:
        if payload.get("type") == "TOOL_CALL_RESULT":
            return await session.answer_intervention_by_id(
                str(payload.get("toolCallId")), str(payload.get("text", ""))
            )
        return False

    transport = AgUiTransport(_sse_lines(sse), send)

    # Consume the wire: the client learns the pending intervention id off the
    # frontend-tool (not rendered — answer-correlation only).
    seen_intervention = False
    async for frame in transport.frames():
        if isinstance(frame, DisplayFrame) and frame.message.kind == "intervention":
            seen_intervention = True
    assert seen_intervention  # the operator SAW the prompt over the wire
    assert transport.pending_intervention_head() == iv.id  # tracked BY ID

    # The operator answers; it round-trips to the exact intervention and resumes.
    delivered = await transport.answer_intervention_text("yes")
    assert delivered is True
    answer = await asyncio.wait_for(dispatch_task, timeout=2.0)
    assert answer.text == "yes"
    # Terminal cleared the pending correlation.
    assert transport.pending_intervention_head() is None


def test_chat_connect_command_surface_exists() -> None:
    """Tier 2: `reyn chat --connect <url> [--token]` is a registered command
    surface (the operator's entry point to the remote loop)."""
    from reyn.interfaces.cli.commands import chat as chat_cmd

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    chat_cmd.register(sub)
    ns = parser.parse_args(["chat", "myagent", "--connect", "http://h:8080", "--token", "s"])
    assert ns.connect == "http://h:8080"
    assert ns.token == "s"
    assert ns.agent_name == "myagent"
