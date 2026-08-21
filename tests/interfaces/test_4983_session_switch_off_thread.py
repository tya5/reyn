"""Tier 2: #4983 — the session-switch rehydrate no longer makes its
synchronous ``history.jsonl`` disk read on the event loop.

Owner ruling (2026-08-21, "セッション切り替えの見た目許容"): a brief
blank-then-refill on switch is the ACCEPTED trade — NOT "history never
arrives" or "order changes" (both still forbidden), and NOT a change to
mount's own first-paint behavior (#4985 already keeps that unchanged).

Architect's design: split by WORK, not by function — ``_hydrate_from_
history`` itself stays synchronous (mount, #4985's own untouched
fallback path, still uses it byte-identically); ONLY
``_handle_session_attached_event`` (the live switch barrier) is now
``async def`` and runs step ① (``_read_conversation_history``, the I/O)
via ``asyncio.to_thread`` before applying step ② (``_apply_hydrated_
messages``, pure in-memory) on the loop, exactly as it always has.

Witness is "does the I/O run off the event loop" (real thread-identity
check, mirroring ``test_4983_mount_hydrate_off_loop.py``'s own
technique for the mount side) — no duration anywhere (CLAUDE.md's
floor/ceiling rule).

Real ``AgentRegistry`` + real ``Session`` + real ``RegistryReadModel`` +
the real mounted ``TextualChatApp``, driven through a real queue-backed
``ClientTransport`` — no mocks.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` whose ``frames()`` drains
    an ``asyncio.Queue`` the test pushes onto (mirrors ``test_3310_n2_
    reset_hydrate.py``'s own helper of the same name/shape)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue" = asyncio.Queue()

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    def push_event(self, etype: str, data: dict) -> None:
        self._queue.put_nowait(EventFrame(Event(type=etype, data=data)))

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self):
        return None

    def put_display(self, msg: OutboxMessage) -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
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
    reg.create("beta")
    return reg


async def _settle(pilot, n: int = 2) -> None:
    for _ in range(n):
        await pilot.pause()


@pytest.mark.asyncio
async def test_session_switch_reads_conversation_history_off_the_event_loop(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the measured defect's own falsifier. The switch barrier's
    ``conversation_history()`` call must run on a DIFFERENT thread than
    the event loop's own main thread — reverting the fix (calling
    ``_hydrate_from_history`` synchronously again, as it did before this
    PR) turns this red: the call lands on the SAME thread the test
    itself runs on."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        read_model = RegistryReadModel(reg)
        main_thread = threading.current_thread()
        call_threads: "list[threading.Thread]" = []
        real_conversation_history = read_model.conversation_history

        def _recording(*args, **kwargs):
            call_threads.append(threading.current_thread())
            return real_conversation_history(*args, **kwargs)

        monkeypatch.setattr(read_model, "conversation_history", _recording)

        transport = QueueTransport()
        app = TextualChatApp(transport=transport, read_model=read_model, agent_name="alpha")
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            calls_at_mount = len(call_threads)

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID},
            )
            await _settle(pilot)

        switch_calls = call_threads[calls_at_mount:]
        assert switch_calls, "the switch must have read conversation_history"
        assert all(t is not main_thread for t in switch_calls), (
            "the session-switch read must run off the event-loop thread "
            "(asyncio.to_thread), not inline on the test's own thread"
        )
    finally:
        pass


@pytest.mark.asyncio
async def test_session_switch_still_hydrates_the_new_sessions_history(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: accept-side — moving the read off-thread must not change
    WHAT ends up on screen. Same staleness-gate shape ``test_3310_n2_
    reset_hydrate.py`` already established, reconfirmed after the split:
    a turn produced on alpha while this client is on beta (durably
    persisted only, never delivered live) is present after switching
    back to alpha."""
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

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID},
            )
            await _settle(pilot)

            from reyn.runtime.chat_message import ChatMessage
            alpha._append_history(ChatMessage(role="user", content="turn while away"))

            await reg.attach("alpha")
            transport.push_event(
                "session_attached", {"agent": "alpha", "session_id": _DEFAULT_SID},
            )
            await _settle(pilot)

            rows = [
                (e.item.kind, e.item.text)
                for e in app.query_one(FlowView).entries
                if e.item.kind != "system"
            ]
            assert rows == [("user", "turn while away")]
    finally:
        pass
