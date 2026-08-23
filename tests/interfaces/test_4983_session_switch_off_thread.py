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
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper
from tests._support.agent_session import make_session


class QueueTransport(ClientTransportStub):
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
    reg.create("gamma")
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
    itself runs on.

    #5159 (census finding): waiting for the switch's read via a flat
    ``_settle(pilot)`` (``pilot.pause()`` — CPU idle time) instead of the
    real condition ("has the read call actually been made yet") used to
    risk a false ``assert switch_calls`` failure: `pilot.pause()`
    determines idle from CPU time, and the switch's read is off-loop
    I/O (``asyncio.to_thread``) that a busy machine may not have even
    STARTED by the time the loop looks idle. Waits on the actual
    condition instead — a public read (`len(call_threads)`, this test's
    own recording list, not reyn's private state), unboundedly (CI's
    own --timeout=120 is the kill switch)."""
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
            await wait_until(lambda: len(call_threads) > calls_at_mount)

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
    back to alpha.

    #5159 (census finding): the final switch-back used to be settled via
    a flat ``_settle(pilot)`` — a CPU-idle heuristic that can declare
    "idle" before the switch's off-thread history read has landed and
    been applied (same risk as the sibling test above, here for the
    APPLIED content rather than just the call having been made). Waits
    on the actual condition instead: the rendered rows matching what
    this test asserts, unboundedly (CI's own --timeout=120 is the kill
    switch) — this is the SAME condition the final `assert` already
    checks, just waited on instead of asserted-after-a-duration."""
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

            def _rows() -> "list[tuple[str, str]]":
                return [
                    (e.item.kind, e.item.text)
                    for e in app.query_one(FlowView).entries
                    if e.item.kind != "system"
                ]

            # Wait for the switch-back to have applied SOMETHING (the real
            # condition — not a duration) before checking WHAT it applied,
            # so a genuine content mismatch still fails with a real diff
            # instead of a bare CI-timeout with no message.
            await wait_until(lambda: bool(_rows()))

            rows = _rows()
            assert rows == [("user", "turn while away")]
    finally:
        pass


@pytest.mark.asyncio
async def test_session_switch_supersede_guard_lets_the_later_switch_win(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #4983 supersede guard (architect co-vet on #4994, self-
    flagged as their own design's residue).

    Moving step ①'s read off the event loop opens a window an ``await``
    didn't have before: switch A's read can still be in flight when switch
    B arrives, and if B finishes first, A returning later must NOT
    overwrite B's already-applied hydrate. Witness ① from the co-vet:
    2 switches in a row, the FIRST one's read held open — final display
    is the SECOND switch's history; reverting the guard (always applying)
    turns this red because A's stale read lands last.

    No duration anywhere (CLAUDE.md floor/ceiling rule): ordering is
    forced with a ``threading.Event`` gate — a controllable seam the test
    releases explicitly — never a sleep. Calls
    ``_handle_session_attached_event`` directly (real method, real app,
    real registry/session — no mock) rather than through the transport
    frame pump, because ``_pump_frames``'s ``async for`` only ever awaits
    one frame's handler to completion before requesting the next: the
    pump itself cannot interleave two ``session_attached`` frames, so
    exercising the guard's own contract needs to invoke the coroutine the
    way a second concurrent caller would.

    Witness ② (a single switch still hydrates normally, so an "always
    discard" implementation cannot pass vacuously) is
    ``test_session_switch_still_hydrates_the_new_sessions_history`` above
    — both are required together per the co-vet note."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        from reyn.runtime.chat_message import ChatMessage

        await reg.attach("beta")
        reg.get_session("beta")._append_history(
            ChatMessage(role="user", content="beta's own turn"),
        )
        await reg.attach("gamma")
        reg.get_session("gamma")._append_history(
            ChatMessage(role="user", content="gamma's own turn"),
        )
        await reg.attach("alpha")

        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            loop = asyncio.get_running_loop()
            release_beta_read = threading.Event()
            beta_read_started = asyncio.Event()
            real_read = app._read_conversation_history

            def _gated_read(*, agent=None, session_id=None):
                if agent == "beta":
                    loop.call_soon_threadsafe(beta_read_started.set)
                    release_beta_read.wait()
                return real_read(agent=agent, session_id=session_id)

            monkeypatch.setattr(app, "_read_conversation_history", _gated_read)

            await reg.attach("beta")
            beta_task = asyncio.create_task(
                app._handle_session_attached_event(
                    Event(
                        type="session_attached",
                        data={"agent": "beta", "session_id": _DEFAULT_SID},
                    )
                )
            )
            await beta_read_started.wait()  # beta's read is now held open

            # gamma's switch starts AFTER beta's and, being ungated, finishes
            # (including its own apply) BEFORE beta's held-open read returns.
            await reg.attach("gamma")
            await app._handle_session_attached_event(
                Event(
                    type="session_attached",
                    data={"agent": "gamma", "session_id": _DEFAULT_SID},
                )
            )

            release_beta_read.set()
            await beta_task
            await _settle(pilot)

            rows = [
                (e.item.kind, e.item.text)
                for e in app.query_one(FlowView).entries
                if e.item.kind != "system"
            ]
            assert rows == [("user", "gamma's own turn")], (
                "the LATER switch (gamma) must win — beta's stale, "
                "later-arriving read must be a no-op, not an overwrite"
            )
    finally:
        pass
