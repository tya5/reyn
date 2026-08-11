"""Tier 2: __attach_request__ swaps the attached agent but does not re-post to repl_outbox.

``AgentRegistry._forwarder`` processes ``__attach_request__`` as a
control signal: ``attach()`` swaps the active agent, then ``continue``
discards the message without forwarding it to ``repl_outbox``.

This file pins:
  1. A ``__attach_request__("beta")`` on alpha's outbox swaps the attached
     agent to beta (control path intact).
  2. ``repl_outbox`` gets no RE-POST of the raw sentinel — the only thing a
     swap contributes is the #3310 N1 ``session_attached`` announce
     ``attach()`` itself now emits (the switch-barrier), never a bare-text
     leak of the control message.
  3. An unknown target (= registry.exists() is False) does not swap and
     does not forward — unchanged behavior proves the control path is not
     broken by the removal.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import OutboxHub


def _drain_all(q: "asyncio.Queue") -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class _FakeInterventions:
    def list_active(self) -> list:
        return []


class _FakeSession:
    """Minimal Session stand-in for AgentRegistry attach() + _forwarder() paths."""

    def __init__(self) -> None:
        self.outbox: asyncio.Queue = asyncio.Queue()
        # ADR-0039 P6b: the forwarder subscribes to the session's outbox hub
        # (the sole outbox.get() consumer) rather than draining outbox directly.
        # A real OutboxHub over this fake's real outbox preserves the test's
        # producer→forwarder path (no mock).
        self.outbox_hub = OutboxHub(self.outbox)
        self._interventions = _FakeInterventions()

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass


def _build_registry(tmp_path: Path):
    """Real AgentRegistry with a session factory returning _FakeSession."""
    from reyn.runtime.registry import AgentRegistry

    sessions: dict[str, _FakeSession] = {}

    def _factory(profile) -> _FakeSession:
        if profile.name not in sessions:
            sessions[profile.name] = _FakeSession()
        return sessions[profile.name]

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )
    registry.create("alpha")
    registry.create("beta")
    return registry, sessions


@pytest.mark.asyncio
async def test_attach_request_swaps_but_does_not_repost(tmp_path):
    """Tier 2: __attach_request__ swaps attach; repl_outbox gets the #3310 N1
    ``session_attached`` announce for the swap and NOTHING else — no re-post
    of the raw sentinel.

    The control path (attach swap + continue) is intact. repl_outbox gets no
    COPY OF THE SENTINEL — there is no live downstream consumer for that raw
    text. If a re-post is silently re-added, this test goes RED, preventing
    a bare-text leak through _output_loop.
    """
    registry, sessions = _build_registry(tmp_path)

    await registry.attach("alpha")
    _drain_all(registry.repl_outbox)  # discard the first-attach announce

    await sessions["alpha"].outbox.put(
        OutboxMessage(kind="__attach_request__", text="beta"),
    )

    # #3748: unbounded wait for the swap (owner policy) -- was a "yield 50
    # times, break early" pump. No terminating assert: the loop condition
    # IS the control-path-intact check, so an assert restating it can never
    # fire; a hang here surfaces via the kill stack showing this exact
    # `while`.
    while registry.attached_name != "beta":
        await asyncio.sleep(0.01)

    # Re-post removal proven: outbox got ONLY the switch-barrier announce,
    # never a copy of the raw "__attach_request__"/"beta" sentinel.
    # Unpacking into a single-element tuple IS the "exactly one, nothing
    # else" assertion (raises ValueError on 0 or 2+ items in the queue).
    (frame,) = _drain_all(registry.repl_outbox)
    assert isinstance(frame, EventFrame) and frame.event.type == "session_attached"
    assert frame.event.data == {"agent": "beta", "session_id": "main"}


@pytest.mark.asyncio
async def test_attach_request_unknown_target_drops_silently(tmp_path):
    """Tier 2: __attach_request__ for an unknown agent does not swap or forward."""
    registry, sessions = _build_registry(tmp_path)

    await registry.attach("alpha")
    _drain_all(registry.repl_outbox)  # discard the first-attach announce

    await sessions["alpha"].outbox.put(
        OutboxMessage(kind="__attach_request__", text="ghost"),
    )

    for _ in range(20):
        await asyncio.sleep(0.01)

    assert registry.attached_name == "alpha"
    # No swap happened (unknown target) => no NEW announce, and no re-post.
    assert registry.repl_outbox.empty()
