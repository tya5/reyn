"""Tier 2: #4817 — the rewind picker YIELDS (does not open) while the
intervention panel is already showing, rather than the two coexisting.

The untouched reverse direction from #4788 B: B closes an already-open
picker when an intervention ARRIVES; this issue is the opposite order — the
intervention panel is ALREADY showing, and the user (or a replayed/remote
``__rewind_list__`` sentinel) then asks to open the picker. Owner's B
ruling applies unchanged (lead-coder, without re-asking the owner — same
axis already decided): an intervention is the agent BLOCKED and waiting
(urgent); the picker is a look-only browsing surface. Priority does not
flip depending on which one happened to open second, so the picker yields.

Lead-coder's one explicit condition: silently doing nothing is NOT
acceptable — a typed ``/rewind`` producing no observable effect is the
exact "ran, but no observable effect" class #4801 closed elsewhere the
same night (a mechanism exists; its result is invisible). The refusal must
be visible and must say what to do instead. Closing the intervention panel
is explicitly off the table — that would break B's own "an intervention
must be answered" property.

Real ``AgentRegistry``/``Session`` (the real ``session.set_pending_command_ui``
+ ``__rewind_list__`` sentinel path a typed ``/rewind`` uses) + a real,
mounted ``TextualChatApp`` — no mocks, per the testing policy. Harness
mirrors ``test_4788_rewind_picker_escape_dismiss.py``'s own shape.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` whose ``frames()`` drains an
    ``asyncio.Queue`` a test pushes onto (mirrors the same harness shape used
    throughout ``test_4788_rewind_picker_escape_dismiss.py``)."""

    def __init__(self, session: "object | None" = None) -> None:
        self._queue: "asyncio.Queue" = asyncio.Queue()
        #: #5045: the real write side moved from ChatReadModel onto
        #: ClientTransport — this fixture needs the real Session to
        #: perform it, same as InProcessTransport's own override does.
        self._session = session

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    def push_display(self, msg: OutboxMessage) -> None:
        self._queue.put_nowait(DisplayFrame(msg))

    def push_event(self, etype: str, data: dict) -> None:
        self._queue.put_nowait(EventFrame(Event(type=etype, data=data)))

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: OutboxMessage) -> None:
        self.push_display(msg)

    async def clear_pending_command_ui(self) -> None:
        if self._session is not None:
            self._session.set_pending_command_ui(None)

    async def cancel_inflight(self) -> None:
        pass

    async def cancel_queued(self, msg_id: str) -> bool:
        return False

    async def shutdown(self) -> None:
        pass


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


async def _settle(pilot, n: int = 3) -> None:
    for _ in range(n):
        await pilot.pause()


@pytest.mark.asyncio
async def test_rewind_does_not_open_while_an_intervention_is_showing(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #4817 — the intervention panel arrives FIRST; a subsequent
    ``/rewind`` (via the real command-UI path) must not open the picker on
    top of it, must not close the panel, and must not be a silent no-op."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        alpha = reg.get_session("alpha")
        transport = QueueTransport(session=alpha)
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            # 1) an intervention arrives first.
            transport.push_display(OutboxMessage(
                kind="intervention", text="Type an answer",
                meta={"intervention_id": "iv-1", "prompt": "next step?"},
            ))
            await _settle(pilot)
            iv_panel = app.query_one(InterventionPanel)
            assert iv_panel.display is True

            # 2) THEN a typed /rewind, via the real command-UI path.
            alpha.set_pending_command_ui({
                "kind": "rewind",
                "points": [{"seq": 1, "ts": "t1", "kind": "checkpoint"}],
            })
            transport.push_display(OutboxMessage(
                kind="__rewind_list__", text="rewind points", meta={},
            ))
            await _settle(pilot)

            picker = app.query_one(RewindPicker)
            assert picker.display is False, (
                "the picker must yield — not open on top of an already-"
                "showing intervention panel (#4817, owner's B ruling "
                "applied to the reverse arrival order)"
            )
            assert iv_panel.display is True, (
                "refusing to open the picker must not close the "
                "intervention panel — an intervention must still be "
                "answerable (#4788 B's own property)"
            )

            # 3) NOT a silent no-op: a visible reason must have landed
            # somewhere in the conversation (#4801's own "ran, but no
            # observable effect" class, closed here for /rewind too).
            flow_text = "\n".join(
                entry.item.text or "" for entry in app.query_one(FlowView).entries
            )
            assert "rewind" in flow_text.lower(), (
                f"the refusal must be visible, not silent: {flow_text!r}"
            )
            assert "intervention" in flow_text.lower(), (
                "the visible refusal must say WHY (an intervention is "
                f"pending), not just that nothing happened: {flow_text!r}"
            )

            # 4) the consumed read-model request must not replay onto a
            # later, unrelated picker interaction.
            assert alpha.pending_command_ui is None, (
                "a refused rewind request must still be consumed — left "
                "pending, it would wrongly attach to a LATER /rewind"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_rewind_still_opens_normally_with_no_intervention_pending(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: accept-side sibling — the ordinary case (no intervention
    pending) must keep opening the picker exactly as before; the new guard
    must not over-fire on the common path."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        alpha = reg.get_session("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            alpha.set_pending_command_ui({
                "kind": "rewind",
                "points": [{"seq": 1, "ts": "t1", "kind": "checkpoint"}],
            })
            transport.push_display(OutboxMessage(
                kind="__rewind_list__", text="rewind points", meta={},
            ))
            await _settle(pilot)
            picker = app.query_one(RewindPicker)
            assert picker.display is True
    finally:
        await reg.shutdown()
