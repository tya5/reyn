"""Tier 2: __attach_request__ is forwarded to repl_outbox after attach swap (issue #191).

Pre-fix, ``AgentRegistry._forwarder`` consumed ``__attach_request__`` as a
control signal — it called ``self.attach(msg.text)`` and ``continue``'d,
swallowing the message before it reached ``repl_outbox``.  The TUI's
``_on_attach_request`` handler (= header label refresh) is wired but
never fires, so the header stays frozen at the previous agent name.

Fix: after ``self.attach(...)`` succeeds, the registry forwards the
same ``__attach_request__`` message to ``repl_outbox`` so downstream
consumers (= TUI app_outbox) see the switch and refresh the header.

This test pins:
  1. The forwarder is started by ``attach()`` and pumps the agent outbox.
  2. A ``__attach_request__("beta")`` on alpha's outbox causes the
     registry to swap the attached agent to beta AND to forward the
     same message to ``repl_outbox`` (= header-refresh signal).
  3. Bad target (= unknown agent name) is rejected: no attach swap and
     no repl_outbox forward (= silent drop, no spurious TUI refresh).
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
    """Minimal stand-in for Session that AgentRegistry can attach.

    Only exposes the attributes the registry's attach() + _forwarder()
    paths read (= .outbox, .is_attached, .run, ._interventions). The
    session.run() coroutine sleeps forever until cancelled — matches a
    real session that runs until shutdown.
    """

    def __init__(self) -> None:
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.is_attached: bool = False
        self._interventions = _FakeInterventions()

    async def run(self) -> None:
        # Sleep forever; registry cancels via shutdown which we don't
        # exercise here. The test's tmp_path teardown collects the task.
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass


def _build_registry(tmp_path: Path):
    """Real ``AgentRegistry`` with a session factory returning _FakeSession."""
    from reyn.runtime.registry import AgentRegistry

    sessions: dict[str, _FakeSession] = {}

    def _factory(profile) -> _FakeSession:
        # Return a cached session per agent name so attach() sees the
        # same outbox the test pushed messages onto.
        if profile.name not in sessions:
            sessions[profile.name] = _FakeSession()
        return sessions[profile.name]

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )
    # Persist alpha + beta profiles so registry.exists("alpha"/"beta") is True.
    registry.create("alpha")
    registry.create("beta")
    return registry, sessions


@pytest.mark.asyncio
async def test_attach_request_forwards_to_repl_outbox(tmp_path):
    """Tier 2: __attach_request__ on alpha's outbox swaps attach AND forwards.

    Issue #191: the TUI header refresh handler subscribes to
    ``__attach_request__`` on repl_outbox, but the registry was
    consuming the message without forwarding. After this fix the
    message reaches repl_outbox so the header refreshes correctly.
    """
    registry, sessions = _build_registry(tmp_path)

    # Attach alpha first — starts session.run() + _forwarder loop.
    await registry.attach("alpha")
    assert registry.attached_name == "alpha"

    # User types `/attach beta` — slash command emits __attach_request__
    # on alpha's outbox (= the currently attached agent's outbox).
    await sessions["alpha"].outbox.put(
        OutboxMessage(kind="__attach_request__", text="beta"),
    )

    # The forwarder is an asyncio.Task; yield to let it process the message.
    # 10 retries with short sleeps so the test is not racy on slow runners.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if registry.attached_name == "beta":
            break

    # Attach swap happened.
    assert registry.attached_name == "beta"

    # And the message reached repl_outbox so TUI's _on_attach_request fires.
    msg = registry.repl_outbox.get_nowait()
    assert msg.kind == "__attach_request__"
    assert msg.text == "beta"


@pytest.mark.asyncio
async def test_attach_request_unknown_target_drops_silently(tmp_path):
    """Tier 2: a __attach_request__ for an unknown agent does NOT forward.

    Avoids spurious TUI header refresh when the user typo'd a non-
    existent name — the slash layer surfaces the error separately;
    the forwarder must not echo a bogus signal.
    """
    registry, sessions = _build_registry(tmp_path)

    await registry.attach("alpha")

    # Bogus target — registry.exists("ghost") is False.
    await sessions["alpha"].outbox.put(
        OutboxMessage(kind="__attach_request__", text="ghost"),
    )

    # Give the forwarder time to process.
    for _ in range(20):
        await asyncio.sleep(0.01)

    # Attach did NOT swap.
    assert registry.attached_name == "alpha"

    # And repl_outbox got no forward (= bogus target dropped).
    assert registry.repl_outbox.empty()
