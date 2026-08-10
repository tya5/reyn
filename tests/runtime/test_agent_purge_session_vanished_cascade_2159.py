"""Tier 2: OS invariant — #2159 agent-purge cascade emits per-sid session_vanished.

The gap: an agent-purge (``AgentRegistry.archive_agent(name, purge=True)`` and the
slice-2 WAL-window auto-purge, ``_purge_archived_below``) rmtree's the agent dir,
which subsumes every spawned session nested under it (``state/sessions/<sid>/``) —
but neither path emitted the per-session ``session_vanished`` destroy record. The
sessions vanished from the WAL's perspective with no destroy record, breaking the
create<->destroy symmetry #2154 established for ``remove_session`` (session_spawned
IS emitted at spawn; the purge cascade must mirror it at destroy).

Mirrors test_session_vanished_emit_2154.py's real AgentRegistry + StateLog + Session
setup (no mocks) and its ``_vanished_sids`` WAL-read helper.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> tuple[AgentRegistry, StateLog]:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        # #2159 test-hygiene: root snapshot_path under tmp_path (NOT the default
        # relative ``.reyn/agents/<name>/...``, which resolves against the real
        # process cwd and leaks a ``.reyn/`` dir into the repo working tree). This
        # also makes the per-session state dir land under tmp_path, so disk-based
        # session discovery (``_discover_session_ids``) — what the purge cascade
        # under test actually reads — is exercised for real, not just the
        # in-memory session map.
        snapshot_path = (
            tmp_path / ".reyn" / "agents" / profile.name / "state" / "snapshot.json"
        )
        s = make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            snapshot_path=snapshot_path,
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    # Hand back the CONCRETE StateLog alongside the registry: ``reg.state_log`` is
    # typed ``StateLog | None`` (a real Optional — a registry can be built without a
    # WAL for non-chat/test use), so reading the WAL through the property would make
    # every assertion here depend on an Optional the tests never actually exercise.
    # Returning the instance we constructed keeps the WAL reads honestly non-None.
    return reg, state_log


def _vanished_sids(log: StateLog, name: str) -> list[str]:
    """The sids of every session_vanished recorded for ``name`` — a PUBLIC WAL read
    (``iter_from``), the same read the #2154 test uses. ``str(...)`` because
    ``entry.get`` is ``Any | None``: the emit contract is a str sid, so a non-str
    would be a genuine defect, not something to silently pass through as None."""
    return [
        str(e.get("sid")) for e in log.iter_from(0)
        if e.get("kind") == "session_vanished" and e.get("name") == name
    ]


@pytest.mark.asyncio
async def test_purge_emits_session_vanished_for_every_spawned_session(tmp_path):
    """Tier 2: purging an agent with MULTIPLE spawned sessions emits session_vanished
    for EACH one (the bound test — a single-session case can't distinguish "one event
    per agent" from "one event per session"; this witnesses the per-sid boundary)."""
    reg, log = _make_registry(tmp_path)
    AgentProfile.new("victim", role="").save(tmp_path / ".reyn" / "agents" / "victim")
    reg.get_or_load("victim")

    sid_a = await reg.spawn_session_recorded(
        "victim", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )
    sid_b = await reg.spawn_session_recorded(
        "victim", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )
    sid_c = await reg.spawn_session_recorded(
        "victim", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )
    await log.flush()
    assert _vanished_sids(log, "victim") == []  # none yet — spawn only

    await reg.archive_agent("victim", purge=True)
    await log.flush()

    vanished = _vanished_sids(log, "victim")
    assert set(vanished) == {sid_a, sid_b, sid_c}  # ALL three, not just one
    assert not (tmp_path / ".reyn" / "agents" / "victim").exists()  # real hard-delete happened


@pytest.mark.asyncio
async def test_purge_of_agent_with_no_spawned_sessions_emits_none(tmp_path):
    """Tier 2: an agent purge with no spawned sessions (only its "main" primary
    session) emits zero session_vanished — "main" is the agent's own session
    (covered by agent_purged), not a spawned one."""
    reg, log = _make_registry(tmp_path)
    AgentProfile.new("solo", role="").save(tmp_path / ".reyn" / "agents" / "solo")
    reg.get_or_load("solo")

    await reg.archive_agent("solo", purge=True)
    await log.flush()

    assert _vanished_sids(log, "solo") == []


@pytest.mark.asyncio
async def test_archive_delete_does_not_emit_session_vanished(tmp_path):
    """Tier 2: the DEFAULT archive (soft-delete, not purge) preserves sessions on
    disk — no rmtree happens, so no session_vanished should fire."""
    reg, log = _make_registry(tmp_path)
    AgentProfile.new("victim", role="").save(tmp_path / ".reyn" / "agents" / "victim")
    reg.get_or_load("victim")
    await reg.spawn_session_recorded(
        "victim", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )
    await log.flush()

    await reg.archive_agent("victim", purge=False)
    await log.flush()

    assert _vanished_sids(log, "victim") == []
    # archive is a soft-delete — no rmtree, so nothing was actually subsumed.
    assert (tmp_path / ".reyn" / "agents" / "victim").is_dir()


@pytest.mark.asyncio
async def test_wal_window_auto_purge_emits_session_vanished_for_every_session(tmp_path):
    """Tier 2: the OTHER purge site — the slice-2 WAL-window GC auto-purge
    (``_purge_archived_below``) hard-deletes an archived agent once the retention
    floor passes its archival seq. Same cascade-emit gap, same bound-test shape
    (multiple sessions)."""
    reg, log = _make_registry(tmp_path)
    AgentProfile.new("victim", role="").save(tmp_path / ".reyn" / "agents" / "victim")
    reg.get_or_load("victim")
    sid_a = await reg.spawn_session_recorded(
        "victim", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )
    sid_b = await reg.spawn_session_recorded(
        "victim", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )

    await reg.archive_agent("victim", purge=False)   # archive (default) — no cascade yet
    archival_seq = reg._archived_seq("victim")        # the tombstone's WAL-window GC hinge
    assert archival_seq is not None
    assert _vanished_sids(log, "victim") == []

    # Floor at the archival seq -> still within the window -> not purged yet.
    await reg._purge_archived_below(archival_seq)
    assert (tmp_path / ".reyn" / "agents" / "victim").is_dir()
    assert _vanished_sids(log, "victim") == []

    # Floor past the archival seq -> soft-delete left the window -> hard-purged.
    await reg._purge_archived_below(archival_seq + 1)
    await log.flush()

    assert not (tmp_path / ".reyn" / "agents" / "victim").exists()
    assert set(_vanished_sids(log, "victim")) == {sid_a, sid_b}
