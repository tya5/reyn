"""Tier 2: #4534 PR-1 — ``ClientTransport.request_attach``/``request_session_switch``,
the ADD-ONLY named-operation seams retiring ``__attach_request__``/
``__session_switch_request__`` (a later PR removes the sentinel; this PR
only adds a second, parallel path to the SAME underlying registry
operations — the existing sentinel-driven tests are untouched and stay
the acceptance bar).

Real ``AgentRegistry`` + real ``Session`` (``make_session``) throughout,
mirroring ``tests/runtime/test_registry_multi_session_1726.py``'s own
established fixture — no mocks.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


def _registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name, agent_role=profile.role,
            output_language="en", snapshot_path=agent_dir / "state" / "snapshot.json",
        )
    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _transport(reg: AgentRegistry) -> InProcessTransport:
    return InProcessTransport(reg, intervention_channel="tui")


# ── request_attach ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_attach_switches_to_an_existing_agent(tmp_path):
    """Tier 2: the happy path, verbatim — request_attach reaches the SAME
    registry.attach() operation the __attach_request__ sentinel branch
    calls today."""
    reg = _registry(tmp_path)
    reg.create("alpha")
    reg.create("beta")
    transport = _transport(reg)
    try:
        ok = await transport.request_attach("beta")
        assert ok is True
        assert reg.attached_name == "beta"
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_request_attach_returns_false_for_an_unknown_agent(tmp_path):
    """Tier 2: (accept-side) an unknown agent name does not attach, does
    not raise — mirrors the sentinel branch's own existence check
    (registry._forwarder: ``if msg.text and self.exists(msg.text)``)."""
    reg = _registry(tmp_path)
    transport = _transport(reg)
    try:
        ok = await transport.request_attach("nonexistent")
        assert ok is False
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_request_attach_returns_false_for_empty_name(tmp_path):
    """Tier 2: (accept-side) an empty string is never a valid target."""
    reg = _registry(tmp_path)
    transport = _transport(reg)
    try:
        assert await transport.request_attach("") is False
    finally:
        for task in reg.running_tasks():
            task.cancel()


# ── request_session_switch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_session_switch_focuses_an_existing_session(tmp_path):
    """Tier 2: the happy path — reaches the SAME registry.attach_session()
    operation the __session_switch_request__ sentinel branch calls
    today."""
    reg = _registry(tmp_path)
    reg.get_or_load("default")
    sid = reg.spawn_session("default", presentation_consumer=None, intervention_bridge=None)
    transport = _transport(reg)
    try:
        await reg.attach("default")  # attach the agent first (transport needs one attached)
        ok = await transport.request_session_switch(sid)
        assert ok is True
        assert reg.attached_sid == sid
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_request_session_switch_returns_false_for_an_unknown_sid(tmp_path):
    """Tier 2: (accept-side) mirrors registry.attach_session's own
    KeyError-on-unknown-sid, caught and turned into False (graceful,
    matching the sentinel branch's own tolerance: "session vanished
    between validate + switch -- no-op")."""
    reg = _registry(tmp_path)
    transport = _transport(reg)
    try:
        await reg.attach("default")
        ok = await transport.request_session_switch("no-such-sid")
        assert ok is False
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_request_session_switch_returns_false_with_no_session_attached(tmp_path):
    """Tier 2: (accept-side) nothing attached yet -> False, not a crash."""
    reg = _registry(tmp_path)
    transport = _transport(reg)
    try:
        assert await transport.request_session_switch("whatever") is False
    finally:
        for task in reg.running_tasks():
            task.cancel()


# ── default (non-InProcess) ClientTransport keeps its old behavior ──────


@pytest.mark.asyncio
async def test_default_transport_implementation_returns_false():
    """Tier 2: (accept-side) the base ClientTransport default (used by
    narrow-purpose test stubs pre-dating this PR, mirroring
    run_slash_command/cancel_queued's own convention) is False, not
    abstract -- an existing stub subclass keeps working unmodified."""
    from reyn.interfaces.transport.client_transport import ClientTransport

    class _Stub(ClientTransport):
        def start(self) -> None: ...
        def close(self) -> None: ...
        async def frames(self):
            return
            yield  # pragma: no cover — makes this an async generator

        async def submit_user_text(self, text: str) -> str:
            return ""

        async def answer_intervention_text(self, text, *, intervention_id=None):
            return False

        async def answer_intervention_choice(self, choice_id, *, intervention_id=None):
            return False

        def has_session(self) -> bool:
            return False

        def pending_intervention_head(self):
            return None

        def put_display(self, msg) -> None: ...

        async def cancel_inflight(self) -> str:
            return ""

        async def shutdown(self) -> None: ...

    stub = _Stub()
    assert await stub.request_attach("anything") is False
    assert await stub.request_session_switch("anything") is False
