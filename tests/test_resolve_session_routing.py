"""Tier 2: FP-0043 S4b-1 — resolve_session routing-core primitive.

The routing-key seam (0043 §Routing-key, settled): an inbound transport message
maps to the right Session of one Agent. Real AgentRegistry + StateLog + on-disk
agents (no mocks). Covers the four settled rules + the S4b-1 path-safety encoding:

  1. default deterministic mapping  — same <transport>:<native_id> → same Session;
  2. explicit-join an EXISTING session;
  3. non-existent explicit id = ERROR (never auto-created);
  4. scope within one Agent;
  5. path round-trip — an arbitrary native_id (with ':' and '/') survives
     spawn → encoded dir → discover → restore as the SAME logical sid + state.

Each assertion is falsification-checked against the production mechanism
(see the companion comments) per feedback_falsify_acceptance_test_before_proof.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _make_registry(tmp_path: Path, wal: Path) -> AgentRegistry:
    state_log = StateLog(wal)

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _iv_dict(iv_id: str) -> dict:
    return {
        "kind": "ask_user", "prompt": "Q?", "detail": "", "choices": [],
        "suggestions": [], "run_id": "r", "skill_name": "demo", "id": iv_id,
    }


def test_resolve_session_default_mapping_is_deterministic(tmp_path):
    """Tier 2: same routing-key → same Session (get-or-spawn); distinct keys isolate."""
    reg = _make_registry(tmp_path, tmp_path / ".reyn" / "wal.jsonl")
    _seed(tmp_path, "alpha")

    s1 = reg.resolve_session("alpha", "slack", "T123")
    s2 = reg.resolve_session("alpha", "slack", "T123")
    assert s1 is s2                                   # resume, not re-spawn
    s3 = reg.resolve_session("alpha", "slack", "T999")
    assert s3 is not s1                               # distinct native_id isolates
    # the logical sid is the namespaced routing-key.
    assert "slack:T123" in reg.session_ids("alpha")
    assert "slack:T999" in reg.session_ids("alpha")


def test_resolve_session_explicit_join_existing(tmp_path):
    """Tier 2: explicit_sid joins an existing Session (cross-transport bridge)."""
    reg = _make_registry(tmp_path, tmp_path / ".reyn" / "wal.jsonl")
    _seed(tmp_path, "alpha")

    created = reg.resolve_session("alpha", "web", "tab1")   # sid = "web:tab1"
    joined = reg.resolve_session("alpha", "cron", "ignored", explicit_sid="web:tab1")
    assert joined is created                          # same Session, not a new one


def test_resolve_session_explicit_nonexistent_is_error(tmp_path):
    """Tier 2: a non-existent explicit id is rejected (never auto-created)."""
    reg = _make_registry(tmp_path, tmp_path / ".reyn" / "wal.jsonl")
    _seed(tmp_path, "alpha")
    reg.resolve_session("alpha", "slack", "T1")       # some existing session

    with pytest.raises(KeyError):
        reg.resolve_session("alpha", "x", "y", explicit_sid="typo:nope")
    # behavioral contract: the typo'd id was NOT auto-created (no silent new session).
    assert "typo:nope" not in reg.session_ids("alpha")


def test_resolve_session_scope_is_within_one_agent(tmp_path):
    """Tier 2: routing scope is per-Agent — a key on agent B is independent of A."""
    reg = _make_registry(tmp_path, tmp_path / ".reyn" / "wal.jsonl")
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")

    a = reg.resolve_session("alpha", "slack", "T1")
    # explicit-join of alpha's sid on beta is an error — scope does not cross agents.
    with pytest.raises(KeyError):
        reg.resolve_session("beta", "x", "y", explicit_sid="slack:T1")
    # a mapping resolve on beta creates beta's OWN distinct session under the key.
    b = reg.resolve_session("beta", "slack", "T1")
    assert b is not a
    assert reg.get_session("alpha", "slack:T1") is a
    assert reg.get_session("beta", "slack:T1") is b


@pytest.mark.asyncio
async def test_resolve_session_path_round_trip_arbitrary_native_id(tmp_path, monkeypatch):
    """Tier 2: an arbitrary native_id (':' and '/') round-trips spawn→dir→discover→restore.

    The logical sid stays "<transport>:<native_id>"; only the filesystem dir is
    bijective-encoded. A '/' in native_id would otherwise create a nested/garbled
    dir that discovery + restore could not recover.
    """
    monkeypatch.chdir(tmp_path)                       # base-align the session snapshot path
    _seed(tmp_path, "alpha")
    wal = tmp_path / ".reyn" / "wal.jsonl"
    reg = _make_registry(tmp_path, wal)

    sid = "webhook:a/b:c"                             # native_id "a/b:c" → ':' and '/'
    s = reg.resolve_session("alpha", "webhook", "a/b:c")
    assert s is reg.get_session("alpha", sid)
    await s.journal.record_intervention_dispatched(
        intervention_id="iv1", iv_dict=_iv_dict("iv1"),
    )

    # the '/' in native_id must NOT have created a nested dir tree (the failure mode
    # the encoding prevents) — verbatim it would be sessions/webhook:a/b:c/.
    sessions_root = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "sessions"
    assert not (sessions_root / "webhook:a" / "b:c").exists()
    # the live session is keyed by the LOGICAL sid (public surface), not the encoding.
    assert sid in reg.session_ids("alpha")

    # restart: a fresh registry must DISCOVER the encoded dir, decode it back to the
    # logical sid, and restore the session + its state under that sid (the round-trip
    # that exercises encode→dir→discover→decode→restore via the public surface).
    reg2 = _make_registry(tmp_path, wal)
    await reg2.restore_all()
    for _ in range(3):
        await asyncio.sleep(0)
    restored = reg2.get_session("alpha", sid)
    assert restored is not None, "encoded-dir session must restore under its logical sid"
    assert [iv.id for iv in restored.interventions.list_active()] == ["iv1"]
