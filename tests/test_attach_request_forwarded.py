"""Tier 2: __attach_request__ swaps the attached agent but does not re-post to repl_outbox.

``AgentRegistry._forwarder`` processes ``__attach_request__`` as a
control signal: ``attach()`` swaps the active agent, then ``continue``
discards the message without forwarding it to ``repl_outbox``.

This file pins:
  1. A ``__attach_request__("beta")`` on alpha's outbox swaps the attached
     agent to beta (control path intact).
  2. ``repl_outbox`` is empty after the swap — no re-post occurs.
  3. An unknown target (= registry.exists() is False) does not swap and
     does not forward — unchanged behavior proves the control path is not
     broken by the removal.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.runtime.outbox import OutboxMessage


class _FakeInterventions:
    def list_active(self) -> list:
        return []


class _FakeSession:
    """Minimal Session stand-in for AgentRegistry attach() + _forwarder() paths."""

    def __init__(self) -> None:
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.is_attached: bool = False
        self._interventions = _FakeInterventions()

    async def run(self) -> None:
        try:
            await asyncio.sleep(3600)
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
    """Tier 2: __attach_request__ swaps attach AND leaves repl_outbox empty.

    The control path (attach swap + continue) is intact. repl_outbox gets
    no copy of the signal — there is no live downstream consumer for it.
    If a re-post is silently re-added, this test goes RED, preventing
    a bare-text leak through _output_loop.
    """
    registry, sessions = _build_registry(tmp_path)

    await registry.attach("alpha")
    assert registry.attached_name == "alpha"

    await sessions["alpha"].outbox.put(
        OutboxMessage(kind="__attach_request__", text="beta"),
    )

    # Yield to the forwarder task; break as soon as the swap is detected.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if registry.attached_name == "beta":
            break

    # Control path intact: swap happened.
    assert registry.attached_name == "beta"
    # Re-post removal proven: outbox got nothing.
    assert registry.repl_outbox.empty()


@pytest.mark.asyncio
async def test_attach_request_unknown_target_drops_silently(tmp_path):
    """Tier 2: __attach_request__ for an unknown agent does not swap or forward."""
    registry, sessions = _build_registry(tmp_path)

    await registry.attach("alpha")

    await sessions["alpha"].outbox.put(
        OutboxMessage(kind="__attach_request__", text="ghost"),
    )

    for _ in range(20):
        await asyncio.sleep(0.01)

    assert registry.attached_name == "alpha"
    assert registry.repl_outbox.empty()
