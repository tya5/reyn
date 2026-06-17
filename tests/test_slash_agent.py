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
from reyn.interfaces.slash import REGISTRY


class _FakeSession:
    """Minimal session stub for the /agent flow.

    Exposes only what the slash handler reads (``_registry``,
    ``_put_outbox``, ``agent_name``, ``_agent_role``). Captures
    emitted outbox messages so the test can assert on the
    ``__attach_request__`` sentinel.
    """

    def __init__(self, registry, *, agent_name: str = "default", agent_role: str = "") -> None:
        self._registry = registry
        self.agent_name = agent_name
        # Mirror the real ChatSession's surface: ``_agent_role`` is the
        # mutable backing field (production code writes here), and
        # ``agent_role`` is the public read-only accessor (tests assert
        # through this). Keep the two-attribute shape so the slash
        # handler's ``session._agent_role = ...`` works against the fake
        # exactly as it does against ChatSession.
        self._agent_role = agent_role
        self.outbox_calls: list[OutboxMessage] = []

    @property
    def agent_role(self) -> str:
        return self._agent_role

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
    from reyn.interfaces.slash.agent import _create_agent

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
    from reyn.interfaces.slash.agent import _create_agent

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
    from reyn.interfaces.slash.agent import _create_agent

    registry = _build_real_registry(tmp_path)
    session = _FakeSession(registry)

    # Uppercase / starts-with-hyphen / too-long all fail the regex.
    await _create_agent(session, "BAD-Name-Mixed-Case")

    assert all(
        m.kind != "__attach_request__" for m in session.outbox_calls
    )
    error_msgs = [m for m in session.outbox_calls if m.kind == "error"]
    assert error_msgs


# ── /agent edit role ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_edit_role_persists_to_profile_and_session(tmp_path):
    """Tier 2: ``/agent edit role <text>`` writes the new role to disk
    and updates ``session.agent_role`` so the next turn sees it."""
    from reyn.chat.profile import AgentProfile
    from reyn.interfaces.slash.agent import _edit_role

    registry = _build_real_registry(tmp_path)
    registry.create("gamma", role="old role")
    session = _FakeSession(registry, agent_name="gamma", agent_role="old role")

    await _edit_role(session, "  new persona text  ")

    # Disk side: profile.yaml carries the new role (stripped).
    reloaded = AgentProfile.load(tmp_path / ".reyn" / "agents" / "gamma")
    assert reloaded.role == "new persona text"
    # In-memory: session attribute mutated for next-turn pickup.
    assert session.agent_role == "new persona text"
    # Confirmation message landed on outbox (system kind, not error).
    successes = [m for m in session.outbox_calls if m.kind == "system"]
    assert successes, f"expected system reply; got {session.outbox_calls}"


@pytest.mark.asyncio
async def test_agent_edit_role_preserves_other_profile_fields(tmp_path):
    """Tier 2: role edit MUST NOT clobber name / created_at / allowed_skills /
    allowed_mcp."""
    from reyn.chat.profile import PROFILE_FILENAME, AgentProfile
    from reyn.interfaces.slash.agent import _edit_role

    registry = _build_real_registry(tmp_path)
    registry.create("delta", role="initial")
    # Hand-write an allowed_skills + allowed_mcp config the create() flow
    # doesn't set, to verify the edit doesn't drop them.
    agent_dir = tmp_path / ".reyn" / "agents" / "delta"
    profile = AgentProfile.load(agent_dir)
    from dataclasses import replace
    enriched = replace(
        profile,
        allowed_skills=["skill_a", "skill_b"],
        allowed_mcp=["mcp_x"],
    )
    enriched.save(agent_dir)
    original_created_at = enriched.created_at

    session = _FakeSession(registry, agent_name="delta", agent_role="initial")
    await _edit_role(session, "edited persona")

    reloaded = AgentProfile.load(agent_dir)
    assert reloaded.role == "edited persona"
    assert reloaded.name == "delta"
    assert reloaded.created_at == original_created_at
    assert reloaded.allowed_skills == ["skill_a", "skill_b"]
    assert reloaded.allowed_mcp == ["mcp_x"]


@pytest.mark.asyncio
async def test_agent_edit_role_empty_value_errors(tmp_path):
    """Tier 2: empty / whitespace role → error message, no disk change."""
    from reyn.chat.profile import AgentProfile
    from reyn.interfaces.slash.agent import _edit_role

    registry = _build_real_registry(tmp_path)
    registry.create("eps", role="keep me")
    session = _FakeSession(registry, agent_name="eps", agent_role="keep me")

    await _edit_role(session, "   ")

    errors = [m for m in session.outbox_calls if m.kind == "error"]
    assert errors
    # On-disk role unchanged.
    assert AgentProfile.load(tmp_path / ".reyn" / "agents" / "eps").role == "keep me"
    # In-memory role unchanged.
    assert session.agent_role == "keep me"


@pytest.mark.asyncio
async def test_agent_edit_unknown_field_errors(tmp_path):
    """Tier 2: ``/agent edit <field> ...`` with field ≠ ``role`` errors.

    Drives the dispatcher (= ``_edit_agent``) so the sub-routing
    layer is covered, not only the leaf handler."""
    from reyn.interfaces.slash.agent import _edit_agent

    registry = _build_real_registry(tmp_path)
    session = _FakeSession(registry, agent_name="default", agent_role="r")

    await _edit_agent(session, "name newname")

    errors = [m for m in session.outbox_calls if m.kind == "error"]
    assert errors
    assert any("role" in m.text for m in errors)


@pytest.mark.asyncio
async def test_agent_edit_no_args_errors(tmp_path):
    """Tier 2: bare ``/agent edit`` (= no sub-field) errors."""
    from reyn.interfaces.slash.agent import _edit_agent

    registry = _build_real_registry(tmp_path)
    session = _FakeSession(registry, agent_name="default", agent_role="r")

    await _edit_agent(session, "")

    errors = [m for m in session.outbox_calls if m.kind == "error"]
    assert errors


@pytest.mark.asyncio
async def test_agent_dispatcher_routes_edit_to_handler(tmp_path):
    """Tier 2: the top-level ``agent_cmd`` dispatches ``edit role <text>``
    through to the leaf handler (= ``_edit_role``)."""
    from reyn.chat.profile import AgentProfile
    from reyn.interfaces.slash.agent import agent_cmd

    registry = _build_real_registry(tmp_path)
    registry.create("zeta", role="before")
    session = _FakeSession(registry, agent_name="zeta", agent_role="before")

    await agent_cmd(session, "edit role after")

    assert (
        AgentProfile.load(tmp_path / ".reyn" / "agents" / "zeta").role
        == "after"
    )
    assert session.agent_role == "after"
