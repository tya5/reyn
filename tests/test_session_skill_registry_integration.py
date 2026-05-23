"""Tier 2: OS invariant — ChatSession lazily constructs and configures a per-agent SkillRegistry.

The session is the layer that owns the production wiring:
  - state_log → SkillRegistry (so step events land in the right WAL)
  - registry.truncate_wal_if_eligible → truncate_eligible_hook
    (so semantic boundary truncation triggers in production but not in
    tests with no AgentRegistry)

These invariants matter because the runtime (OSRuntime) and resume entry
(future PR-skill-resume part D) both rely on the session having handed
them a correctly-wired SkillRegistry. A wiring regression would silently
disable resume.

Observation flows through:
  - ChatSession._get_skill_registry() return value type
  - The returned registry's behavior when its lifecycle methods are called
    (does start() append to the WAL? does the hook fire?)
No mocks — real ChatSession with no LLM (we never call _run_skill_awaitable),
real StateLog.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog
from reyn.skill.skill_registry import SkillRegistry


def _make_session(tmp_path: Path, *, with_state_log: bool, with_registry: bool):
    """Construct a ChatSession with optional state_log + registry back-ref."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl") if with_state_log else None
    registry = None
    if with_registry:
        # Minimal AgentRegistry — we only need .truncate_wal_if_eligible
        from reyn.chat.registry import AgentRegistry

        def _no_factory(_profile):
            raise AssertionError("session factory must not be called")

        registry = AgentRegistry(
            project_root=tmp_path,
            session_factory=_no_factory,
            state_log=state_log,
        )
    return ChatSession(
        agent_name="alpha",
        state_log=state_log,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Lazy construction
# ---------------------------------------------------------------------------


def test_get_skill_registry_returns_none_without_state_log(tmp_path, monkeypatch):
    """Tier 2: with state_log=None (test/standalone), no SkillRegistry is constructed.

    Resume only makes sense with a durable WAL; standalone CLI runs and
    test fixtures default to None and should see a None back from the
    getter.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, with_state_log=False, with_registry=False)
    assert session._get_skill_registry() is None


def test_get_skill_registry_lazy_constructs_with_state_log(tmp_path, monkeypatch):
    """Tier 2: with a state_log wired, _get_skill_registry returns a SkillRegistry on first call and the same instance on subsequent calls (lazy init).

    Same-instance check matters: a fresh registry per call would lose
    the in-memory cache of active runs.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, with_state_log=True, with_registry=False)

    reg1 = session._get_skill_registry()
    assert isinstance(reg1, SkillRegistry)
    reg2 = session._get_skill_registry()
    assert reg1 is reg2  # cached


def test_skill_registry_writes_to_session_state_log(tmp_path, monkeypatch):
    """Tier 2: lifecycle events from the lazily-constructed registry land in the same WAL as the session's other persistence.

    Verified by calling registry.start() and re-reading the WAL.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, with_state_log=True, with_registry=False)
    reg = session._get_skill_registry()

    async def go():
        await reg.start(
            run_id="r1", skill_name="demo", skill_input={"x": 1},
        )

    asyncio.run(go())

    # WAL should contain skill_started — read directly from the session's StateLog
    seen = [e for e in session._state_log.iter_from(0)]
    started = [e for e in seen if e.get("kind") == "skill_started"]
    assert len(started) > 0
    assert started[0]["run_id"] == "r1"
    assert started[0]["target"] == "alpha"


# ---------------------------------------------------------------------------
# Truncate hook wiring (the production-vs-test distinction)
# ---------------------------------------------------------------------------


def test_truncate_hook_unwired_without_registry(tmp_path, monkeypatch):
    """Tier 2: with registry=None (test fixtures), the SkillRegistry's truncate hook is None — no truncation triggered.

    Production tests that need truncation triggered must instantiate an
    AgentRegistry; bare ChatSession unit tests get a no-trigger registry.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, with_state_log=True, with_registry=False)
    reg = session._get_skill_registry()
    assert reg._truncate_hook is None


def test_truncate_hook_wired_with_registry(tmp_path, monkeypatch):
    """Tier 2: with registry set, the hook bridges to AgentRegistry.truncate_wal_if_eligible.

    Verified end-to-end: seed alpha's profile (so AgentRegistry's
    ``list_names`` sees it) and snapshot with a non-zero applied_seq
    (so the floor calc returns a real value), call ``advance_phase``
    → hook fires → AgentRegistry executes a real truncation pass.
    Observable signal is the throttle stamp, which is set only when
    truncation actually attempts a rewrite (floor > 0).
    """
    from reyn.chat.profile import AgentProfile
    from reyn.events.agent_snapshot import AgentSnapshot

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, with_state_log=True, with_registry=True)

    # Seed alpha as a real on-disk agent so AgentRegistry.list_names
    # discovers it (it scans the agents/ directory for entries with a
    # profile.yaml). Without this, alpha is invisible to the truncation
    # floor calc and floor stays at 0.
    AgentProfile.new("alpha", role="").save(
        Path(".reyn") / "agents" / "alpha",
    )
    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = 5
    snap.save(
        Path(".reyn") / "agents" / "alpha" / "state" / "snapshot.json",
    )

    reg = session._get_skill_registry()
    assert reg._truncate_hook is not None

    async def go():
        await reg.start(run_id="r", skill_name="s", skill_input={})
        # advance_phase fires the hook → AgentRegistry truncates → stamp set
        await reg.advance_phase(run_id="r", next_phase="draft")

    asyncio.run(go())

    # Throttle stamp set proves the hook reached AgentRegistry's truncation
    # path AND truncation actually attempted a rewrite (floor>0).
    assert session._registry.last_truncation_ts is not None
