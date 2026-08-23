"""Tier 2: #5149 — the client's "current location" (which agent, which of
that agent's sessions) is ONE value (:class:`_Destination`), not two
separately-writable fields.

Architect ruling (issue #5149, part of #5116): the #5148-flagged hazard —
``_agent_name`` and ``_session_id`` could be written independently, and a
divergence silently discards correct remote backlog (``batch.agent !=
self._agent_name or batch.sid != self._session_id``) — is closed
STRUCTURALLY, not by a test: ``_Destination`` is a frozen dataclass, so
``dest.agent = x`` is a ``dataclasses.FrozenInstanceError`` at the write
site, not a convention a future editor has to remember. The acceptance
itself ("two fields cannot be updated independently") is a type/structure
claim (architect's own words, "test では示せません — 型／構造で示すもの"),
so the FIRST test below witnesses that directly; the rest witness the
actual #5148 bug scenario this closes (a partial ``session_attached``
announce) now landing as one atomic, internally-consistent value.

Real ``AgentRegistry`` + real ``TextualChatApp`` + a real queue-backed
``ClientTransport`` (mirrors ``tests/interfaces/test_3310_n2_reset_
hydrate.py``'s own ``QueueTransport`` exactly) — no mocks, per the testing
policy.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat.app import TextualChatApp, _Destination
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame, EventFrame
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def test_destination_is_frozen_writing_one_field_alone_raises() -> None:
    """Tier 2: #5149's own acceptance, stated verbatim by architect —
    "two fields cannot be updated independently" is a type/structure claim.
    This is that claim, witnessed directly: assigning to ONE field of an
    already-constructed ``_Destination`` raises, so the only way to change
    either half is a whole new ``_Destination(...)`` — one assignment,
    both fields, always together. RED (a plain, non-frozen dataclass) if
    ``frozen=True`` is ever dropped from the class."""
    dest = _Destination(agent="alpha", session_id=_DEFAULT_SID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dest.agent = "beta"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        dest.session_id = "some-other-sid"  # type: ignore[misc]
    # Neither raise mutated it — a FrozenInstanceError leaves the instance
    # exactly as constructed, not partially written.
    assert dest == _Destination(agent="alpha", session_id=_DEFAULT_SID)


class QueueTransport(ClientTransportStub):
    """Mirrors ``test_3310_n2_reset_hydrate.py``'s own class of the same
    name exactly — a real, minimal ``ClientTransport`` a test drives
    frame-by-frame via an ``asyncio.Queue``."""

    def __init__(self) -> None:
        import asyncio

        self._queue: "asyncio.Queue" = asyncio.Queue()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    def push_event(self, etype: str, data: dict) -> None:
        self._queue.put_nowait(EventFrame(Event(type=etype, data=data)))

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg) -> None:
        self._queue.put_nowait(DisplayFrame(msg))

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

    async def cancel_inflight(self) -> None:
        pass

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


async def _settle(pilot, n: int = 2) -> None:
    for _ in range(n):
        await pilot.pause()


def _matches_destination(app: TextualChatApp, agent: str, sid: str) -> bool:
    """Public-surface oracle for "is the App's current destination exactly
    (agent, sid)?" — never reads ``app._destination`` directly (the testing
    policy forbids a private-state assertion). ``_apply_backlog_batch`` is
    ALREADY the production code that compares a batch's destination against
    ``self._destination`` in full (both halves) and exposes the outcome via
    the public :meth:`TextualChatApp.remote_backlog_discard_count` counter
    — reusing it here means this oracle and the real discard check can
    never silently drift apart from each other."""
    before = app.remote_backlog_discard_count()
    app._apply_backlog_batch(BacklogBatch(agent=agent, sid=sid, frames=[]))
    return app.remote_backlog_discard_count() == before


@pytest.mark.asyncio
async def test_agent_only_announce_preserves_the_current_session_id(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the actual #5148 bug scenario — a ``session_attached``
    announce naming ONLY ``agent`` (``session_id`` absent from ``data``,
    e.g. a caller that has not learned the session id yet) must not corrupt
    or blank the session half; it must fall back to whatever the current
    session_id already was, in the SAME atomic assignment that updates
    ``agent``. RED if a stray ``session_id=None`` ever wins over the
    current value (the old independently-guarded ``if session_id: ...``
    shape could not express this wrongly since it just skipped the write,
    but a rewrite that constructs unconditionally with ``data.get
    ("session_id")`` verbatim WOULD silently blank it — this test is the
    guard against that regression). The well-known default session id
    (``_DEFAULT_SID``) is public knowledge (``registry.py``'s own constant,
    also what ``__init__`` seeds — see that constructor's own comment),
    not a private-state read."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    transport = QueueTransport()
    app = TextualChatApp(
        transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        assert _matches_destination(app, "alpha", _DEFAULT_SID), (
            "sanity: a fresh App starts at (alpha, _DEFAULT_SID)"
        )

        transport.push_event("session_attached", {"agent": "alpha"})  # no session_id
        await _settle(pilot)

        assert _matches_destination(app, "alpha", _DEFAULT_SID), (
            "an agent-only announce must preserve the existing session_id "
            "(_DEFAULT_SID), not blank it"
        )


@pytest.mark.asyncio
async def test_session_only_announce_preserves_the_current_agent(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the mirror case — a ``session_attached`` announce naming
    ONLY ``session_id`` (``agent`` absent) must not corrupt or blank the
    agent half."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    transport = QueueTransport()
    app = TextualChatApp(
        transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        assert app.agent_name == "alpha"  # the public property (#5131)

        transport.push_event("session_attached", {"session_id": "some-other-sid"})
        await _settle(pilot)

        assert app.agent_name == "alpha", (
            "a session-only announce must preserve the existing agent"
        )
        assert _matches_destination(app, "alpha", "some-other-sid"), (
            "a session-only announce must land the new session_id"
        )


@pytest.mark.asyncio
async def test_full_announce_updates_both_atomically(tmp_path, monkeypatch) -> None:
    """Tier 2: falsification contrast — a FULL announce (both fields
    present) updates both, in the same one assignment (never two writes an
    observer could catch mid-update — not directly observable from outside
    without instrumenting the reactive descriptor, but the end state must
    be the new pair, not a stale mix)."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    reg.create("beta")
    await reg.attach("alpha")
    transport = QueueTransport()
    app = TextualChatApp(
        transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        await reg.attach("beta")
        transport.push_event(
            "session_attached", {"agent": "beta", "session_id": "beta-sid"},
        )
        await _settle(pilot)

        assert app.agent_name == "beta"
        assert _matches_destination(app, "beta", "beta-sid")


@pytest.mark.asyncio
async def test_remote_backlog_discard_compares_the_whole_destination(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: witnesses the #5148-named discard site
    (``_apply_backlog_batch``'s ``(batch.agent, batch.sid) !=
    (self._destination.agent, self._destination.session_id)`` check) still
    discriminates correctly post-#5149 — a backlog batch destined for the
    CURRENT (agent, session_id) pair is not discarded, one destined for a
    DIFFERENT pair (either half differing) is."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    transport = QueueTransport()
    app = TextualChatApp(
        transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        before = app.remote_backlog_discard_count()

        # Matches the current destination exactly -- not discarded.
        app._apply_backlog_batch(
            BacklogBatch(agent=app._destination.agent, sid=app._destination.session_id, frames=[]),
        )
        assert app.remote_backlog_discard_count() == before

        # Session half differs -- discarded.
        app._apply_backlog_batch(
            BacklogBatch(agent=app._destination.agent, sid="some-other-sid", frames=[]),
        )
        assert app.remote_backlog_discard_count() == before + 1

        # Agent half differs -- discarded.
        app._apply_backlog_batch(
            BacklogBatch(agent="some-other-agent", sid=app._destination.session_id, frames=[]),
        )
        assert app.remote_backlog_discard_count() == before + 2
