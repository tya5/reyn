"""Tier 2: /agent new <name> creates an agent and triggers attach.

Pins the new command's registry membership + the create-then-attach
behaviour against a real ``AgentRegistry`` constructed on tmp_path.
No mocking of internal collaborators (per ``testing.ja.md`` "Use real
instances or the LLMReplay Fake").
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import REGISTRY


class _FakeSession:
    """Minimal session stub for the /agent flow.

    Exposes only what the slash handler reads (``_registry``,
    ``_put_outbox``). Captures emitted outbox messages so the test can
    assert on the ``__attach_request__`` sentinel.
    """

    def __init__(self, registry) -> None:
        self._registry = registry
        self.outbox_calls: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_calls.append(msg)


def _build_real_registry(tmp_path: Path):
    """Construct a real ``AgentRegistry`` rooted at tmp_path."""
    from reyn.chat.registry import AgentRegistry

    # Minimal session_factory — registry.create() doesn't invoke it,
    # it just persists the AgentProfile to disk. The factory is for
    # later session construction which we don't exercise here.
    def _factory(profile):
        return object()

    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )


@pytest.mark.asyncio
async def test_agent_slash_is_registered():
    """Tier 2: ``/agent`` is in the slash registry, summary mentions ``new``."""
    cmd = REGISTRY.get("agent")
    assert cmd is not None
    assert "new" in cmd.summary.lower()


@pytest.mark.asyncio
async def test_agent_new_creates_and_emits_attach_request(tmp_path):
    """Tier 2: ``/agent new <name>`` creates the agent and emits
    ``__attach_request__``.

    Drives the slash handler through a real registry on tmp_path so the
    create + outbox-emit chain is exercised end-to-end.
    """
    from reyn.chat.slash.agent import _create_agent

    registry = _build_real_registry(tmp_path)
    session = _FakeSession(registry)

    await _create_agent(session, "beta")

    # Profile file landed on disk via the real registry.
    assert registry.exists("beta"), "agent profile must persist on disk"
    # And the attach sentinel was emitted (plus a confirmation reply
    # before it — assert at least 1 attach_request was queued).
    kinds = [m.kind for m in session.outbox_calls]
    assert "__attach_request__" in kinds
    attach_msg = next(
        m for m in session.outbox_calls if m.kind == "__attach_request__"
    )
    assert attach_msg.text == "beta"


@pytest.mark.asyncio
async def test_agent_new_rejects_duplicate(tmp_path):
    """Tier 2: creating an existing agent surfaces a recoverable error,
    NOT a Python stack trace."""
    from reyn.chat.slash.agent import _create_agent

    registry = _build_real_registry(tmp_path)
    registry.create("dup")

    session = _FakeSession(registry)
    await _create_agent(session, "dup")

    # No attach should have been emitted on the failure path.
    assert all(
        m.kind != "__attach_request__" for m in session.outbox_calls
    )
    # An error outbox message should have been queued.
    error_msgs = [m for m in session.outbox_calls if m.kind == "error"]
    assert error_msgs, f"expected an error reply; got {session.outbox_calls}"


@pytest.mark.asyncio
async def test_agent_new_rejects_invalid_name(tmp_path):
    """Tier 2: invalid names (= regex violation) surface a clean error."""
    from reyn.chat.slash.agent import _create_agent

    registry = _build_real_registry(tmp_path)
    session = _FakeSession(registry)

    # Uppercase / starts-with-hyphen / too-long all fail the regex.
    await _create_agent(session, "BAD-Name-Mixed-Case")

    assert all(
        m.kind != "__attach_request__" for m in session.outbox_calls
    )
    error_msgs = [m for m in session.outbox_calls if m.kind == "error"]
    assert error_msgs
