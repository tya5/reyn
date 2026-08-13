"""Tier 2: #4494 design C — ``ClientTransport.request_artifact_list``, the
durable artifact-ref table fallback a remote client's Artifacts pane
consults when its live conversation view carries nothing. Mirrors
``test_4534_pr1_request_attach_switch.py``'s own fixture shape (real
``AgentRegistry`` + real ``Session`` — no mocks).

**#4601**: the method now returns ``(entries, total)`` — entries capped
(newest-first) at ``config.artifacts.remote_fallback_limit``, total the
pre-cap count, so a caller can disclose "newest N of M".
"""
from __future__ import annotations

import pytest

from reyn.data.workspace.artifact_ref import mint_ref
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _registry(tmp_path) -> AgentRegistry:
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")

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


@pytest.mark.asyncio
async def test_returns_the_attached_agents_own_ref_table_entries(tmp_path):
    """Tier 2: the happy path — reaches the SAME table
    ``list_refs_for_agent`` reads, scoped to the attached agent."""
    reg = _registry(tmp_path)
    reg.create("alpha")
    await reg.attach("alpha")
    f = tmp_path / "report.pdf"
    f.write_text("x")
    ref = mint_ref(tmp_path, "alpha", f)
    transport = _transport(reg)
    try:
        entries, total = await transport.request_artifact_list(agent="alpha")
        assert entries == [{"ref": ref, "path": str(f)}]
        assert total == 1
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_caps_entries_at_the_configured_remote_fallback_limit(tmp_path):
    """Tier 2: (#4601) more artifacts than ``remote_fallback_limit`` are
    minted — the returned entries are capped (newest-first), but
    ``total`` still names the full count."""
    reg = _registry(tmp_path)
    # _registry() itself writes the plain MINIMAL_REYN_YAML — overwrite it
    # AFTER, with the artifacts: section this test actually needs.
    (tmp_path / "reyn.yaml").write_text(
        MINIMAL_REYN_YAML + "\nartifacts:\n  remote_fallback_limit: 2\n",
        encoding="utf-8",
    )
    reg.create("alpha")
    await reg.attach("alpha")
    refs = []
    for i in range(5):
        f = tmp_path / f"f{i}.pdf"
        f.write_text("x")
        refs.append(mint_ref(tmp_path, "alpha", f))
    transport = _transport(reg)
    try:
        entries, total = await transport.request_artifact_list(agent="alpha")
        assert [e["ref"] for e in entries] == [refs[4], refs[3]]
        assert total == 5
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_returns_empty_with_no_session_attached(tmp_path):
    """Tier 2: (accept-side) nothing attached -> no project root to read,
    same graceful-empty contract every other unattached transport call
    already gives (mirrors ``request_session_switch``'s own)."""
    reg = _registry(tmp_path)
    transport = _transport(reg)
    try:
        assert await transport.request_artifact_list(agent="alpha") == ([], 0)
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_default_transport_implementation_returns_empty():
    """Tier 2: (accept-side) the base ClientTransport default — a narrow
    test stub pre-dating this method keeps working unmodified, same
    convention as ``request_attach``'s own default."""
    from reyn.interfaces.transport.client_transport import ClientTransport

    class _Stub(ClientTransport):
        def start(self) -> None: ...
        def close(self) -> None: ...
        async def frames(self):
            return
            yield  # pragma: no cover

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
    assert await stub.request_artifact_list(agent="alpha") == ([], 0)
