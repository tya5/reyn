"""Tier 2: /agents + /attach slash — handler behavioural paths.

/agents: no-registry error, empty-agents unexpected note, normal listing
(names present → reply contains names).

/attach: no-name error, no-registry error, name-not-found error,
already-attached note, valid-name success (system reply + __attach_request__
sentinel with the name).
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.agents import agents_cmd, attach_cmd
from reyn.runtime.outbox import OutboxMessage

# ── stubs ──────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, *, registry=None) -> None:
        self._registry = registry
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def system_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")

    def outbox_kinds(self) -> list[str]:
        return [m.kind for m in self._outbox]

    def sentinel_text(self, kind: str) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == kind)


class _StubProfile:
    def __init__(self, role: str = "") -> None:
        self.role = role


class _FakeRegistry:
    def __init__(
        self,
        *,
        names: list[str] | None = None,
        attached: str = "default",
        loaded: list[str] | None = None,
        exists_result: bool = True,
    ) -> None:
        self._names = names or []
        self.attached_name = attached
        self._loaded = loaded or []
        self._exists = exists_result

    def list_active_names(self) -> list[str]:
        return list(self._names)

    def loaded_names(self) -> list[str]:
        return list(self._loaded)

    def exists(self, name: str) -> bool:
        return name in self._names if self._names else self._exists

    def load_profile(self, name: str) -> _StubProfile:
        return _StubProfile(role=f"role-of-{name}")

    def last_activity_at(self, name: str):
        return None


# ── agents_cmd paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agents_no_registry_sends_error() -> None:
    """Tier 2: /agents with no registry wired replies an error."""
    session = _FakeSession(registry=None)
    await agents_cmd(session, "")
    assert session.error_text(), "expected error reply when registry absent"
    assert not session.system_text()


@pytest.mark.asyncio
async def test_agents_empty_name_list_sends_system_note() -> None:
    """Tier 2: /agents with an empty name list sends a system note (unexpected state)."""
    session = _FakeSession(registry=_FakeRegistry(names=[]))
    await agents_cmd(session, "")
    assert session.system_text(), "expected system note for empty agents"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_agents_listing_includes_agent_names() -> None:
    """Tier 2: /agents with names present → each name appears in the reply."""
    session = _FakeSession(
        registry=_FakeRegistry(names=["alpha", "beta"], attached="alpha")
    )
    await agents_cmd(session, "")
    text = session.system_text()
    assert "alpha" in text
    assert "beta" in text


@pytest.mark.asyncio
async def test_agents_listing_marks_attached_agent() -> None:
    """Tier 2: /agents marks the attached agent with '*' in the reply."""
    session = _FakeSession(
        registry=_FakeRegistry(names=["alpha", "beta"], attached="alpha")
    )
    await agents_cmd(session, "")
    text = session.system_text()
    # The attached marker (* = attached) appears in the legend and next to the agent
    assert "*" in text


# ── attach_cmd paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_no_name_sends_error() -> None:
    """Tier 2: /attach with no name replies a usage error."""
    session = _FakeSession(registry=_FakeRegistry(names=["alpha"]))
    await attach_cmd(session, "")
    assert session.error_text()
    assert not session.system_text()


@pytest.mark.asyncio
async def test_attach_no_registry_sends_error() -> None:
    """Tier 2: /attach with no registry wired replies an error."""
    session = _FakeSession(registry=None)
    await attach_cmd(session, "alpha")
    assert session.error_text()


@pytest.mark.asyncio
async def test_attach_nonexistent_name_sends_error() -> None:
    """Tier 2: /attach <name> for a name that doesn't exist replies an error."""
    session = _FakeSession(registry=_FakeRegistry(names=["alpha"], attached="alpha"))
    await attach_cmd(session, "ghost")
    assert session.error_text()
    assert "ghost" in session.error_text()


@pytest.mark.asyncio
async def test_attach_already_attached_sends_system_note() -> None:
    """Tier 2: /attach to the already-attached agent replies an 'already attached' note."""
    session = _FakeSession(
        registry=_FakeRegistry(names=["alpha"], attached="alpha")
    )
    await attach_cmd(session, "alpha")
    assert "already" in session.system_text()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_attach_valid_name_sends_success_reply() -> None:
    """Tier 2: /attach <valid-name> sends a system success reply."""
    session = _FakeSession(
        registry=_FakeRegistry(names=["alpha", "beta"], attached="alpha")
    )
    await attach_cmd(session, "beta")
    assert session.system_text(), "expected success reply"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_attach_valid_name_emits_attach_request_sentinel() -> None:
    """Tier 2: /attach <valid-name> emits __attach_request__ OutboxMessage with the name."""
    session = _FakeSession(
        registry=_FakeRegistry(names=["alpha", "beta"], attached="alpha")
    )
    await attach_cmd(session, "beta")
    assert "__attach_request__" in session.outbox_kinds()
    assert session.sentinel_text("__attach_request__") == "beta"
