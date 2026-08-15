"""Tier 2: #4788 — Esc dismisses an open rewind picker regardless of what
currently holds focus.

Found investigating #4761 (headless repro of a reported TUI freeze — the
freeze itself did not reproduce, but this real, unrelated defect surfaced
along the way): :class:`~reyn.interfaces.inline.textual_chat.rewind_picker.
RewindPicker`'s own ``escape`` Binding only participates in Textual's
focused-widget-outward walk when the picker (or a descendant) is somewhere
in the current focus chain. Something moving focus away from the picker
while it stays open — originally observed via an arriving intervention —
steals focus, and Esc then resolves against the NEW chain instead, so the
picker's own binding never gets consulted at all. The picker was never
functionally stuck (clicking back into it, or Enter on a highlighted row,
both still worked) — only Esc's own path to it was unreachable, which is
the gap this test pins.

#4788 B (owner-approved, decided AFTER this file's first version landed):
an arriving intervention now closes the picker outright
(:meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
_present_intervention`), rather than merely stealing its focus — so an
intervention can no longer produce the "picker open, unfocused" state this
file's first test used to set up via that specific path. That test is
rewritten below to move focus away from the picker directly (a plain
``Composer.focus()``, e.g. the user clicking back into the composer while
the picker sits open) — a different, still-real way to reach the same
unfocused-but-open state the Esc catch exists for. #4788 B's OWN new
behavior (intervention closes the picker) is covered separately, in
``test_an_arriving_intervention_closes_an_open_rewind_picker`` below.

Real ``AgentRegistry``/``Session`` (the real ``session.set_pending_command_ui``
+ ``__rewind_list__`` sentinel path a typed ``/rewind`` uses) + a real,
mounted ``TextualChatApp`` — no mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
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
    throughout ``test_3310_n2_reset_hydrate.py``)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue" = asyncio.Queue()

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
async def test_escape_dismisses_rewind_picker_after_focus_moves_elsewhere(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: picker opens, THEN focus moves away from it (a direct
    ``Composer.focus()`` — the user clicking back into the composer, the
    same discriminator an arriving intervention used to exercise before
    #4788 B made intervention arrival close the picker outright instead)
    — Esc must still close the picker."""
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

            # 1) the rewind picker opens first, via the real command-UI path
            # a typed ``/rewind`` uses.
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
            assert app.focused is picker.query_one("#rewind-picker-options")

            # 2) focus moves away from the picker WITHOUT the picker itself
            # closing — the state Esc's app-level catch exists for.
            app.query_one(Composer).focus()
            await _settle(pilot)
            assert picker.display is True, (
                "the picker must still be open at this point — this test is "
                "about closing it via Esc, not about it disappearing on its own"
            )
            # focus moved off the picker's own OptionList — the discriminator
            # for this bug: without it, the picker's OWN Binding would still
            # answer Esc and this test would pass for the wrong reason.
            assert app.focused is not picker.query_one("#rewind-picker-options")

            # 3) Esc — the picker must close even though it holds no focus.
            await pilot.press("escape")
            await _settle(pilot)
            assert picker.display is False, (
                "Esc must dismiss an open rewind picker regardless of which "
                "widget currently holds focus (#4788)"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_an_arriving_intervention_closes_an_open_rewind_picker(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #4788 B (owner-approved, via lead-coder's recommendation) —
    an intervention arriving while the rewind picker is open closes the
    picker outright, rather than leaving both modals live behind each
    other. Covers the actual owner-decided behavior this issue's B half
    was about; the escape-reaches-an-unfocused-picker mechanism itself is
    covered by the sibling tests in this file."""
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

            # 1) the rewind picker opens first, via the real command-UI path
            # a typed ``/rewind`` uses.
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

            # 2) a pending free-text intervention (no "choices" key) arrives
            # SECOND, while the picker is still open.
            transport.push_display(OutboxMessage(
                kind="intervention", text="Type an answer",
                meta={"intervention_id": "iv-1", "prompt": "next step?"},
            ))
            await _settle(pilot)
            iv_panel = app.query_one(InterventionPanel)
            assert iv_panel.display is True
            assert picker.display is False, (
                "an arriving intervention must close an already-open rewind "
                "picker outright (#4788 B) — the two are not meant to coexist"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_escape_still_dismisses_rewind_picker_when_it_holds_focus(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: accept-side sibling — the ordinary case (picker open, picker
    holds focus, no intervention involved) must keep working exactly as
    before; the new app-level Esc catch must not double-fire or otherwise
    change the plain single-picker path."""
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
            assert app.focused is picker.query_one("#rewind-picker-options")

            await pilot.press("escape")
            await _settle(pilot)
            assert picker.display is False
    finally:
        await reg.shutdown()
