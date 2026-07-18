"""Tier 2: #2548 PR-B — skill hot-reload + per-session enable/disable toggle.

Three invariants:

1. **Hot-reload e2e** (Tier 2c): writing a new skill to .reyn/config/skills.yaml and
   applying the hot-reload seam updates the LIVE ``get_available_skills()`` on the
   RouterHostAdapter — not a local copy.  The next turn's system prompt reflects the
   change.

2. **Toggle e2e** (Tier 2b): ``set_capability_visible("skill", name, False)`` removes
   the named skill from the list ``get_available_skills()`` returns; re-enabling it with
   ``True`` restores it.  Disabling an unregistered skill name is a no-op (restrict-only
   invariant).

3. **Persistence** (Tier 2b): the skill toggle persists to ``visibility.yaml`` (in the
   per-session state dir) and is restored by ``load_persisted_toggles()``, which
   re-applies the filter on the live host.

No mocks.  Real Session + real RouterHostAdapter + real HotReloader throughout.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.data.skills.registry import SkillEntry
from reyn.runtime.session import Session
from reyn.runtime.session_params import CapabilityScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Path, *, agent_name: str = "test-agent") -> Session:
    """Minimal real Session in *tmp_path*.  Monkeypatch cwd before calling so
    ``load_config`` resolves ``.reyn/config/skills.yaml`` correctly.

    Builds ``available_skills`` from ``load_config`` so that skills declared in
    ``.reyn/config/skills.yaml`` (written before this call) are included — mirroring
    what ``SessionFactoryConfig.from_config`` does in production."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    from reyn.config.loader import load_config
    from reyn.data.skills.registry import build_skill_registry
    cfg = load_config()
    available_skills = build_skill_registry(cfg.skills) or None
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=available_skills),
    )


def _skills_yaml_path(tmp_path: Path) -> Path:
    p = tmp_path / ".reyn" / "config" / "skills.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_skills(tmp_path: Path, entries: dict) -> None:
    """Write an entries dict to .reyn/config/skills.yaml."""
    _skills_yaml_path(tmp_path).write_text(
        yaml.safe_dump({"skills": {"entries": entries}}),
        encoding="utf-8",
    )


def _skill_names(session: Session) -> "list[str]":
    """Read skill names via the LIVE public surface (RouterHostAdapter)."""
    skills = session._router_host.get_available_skills()
    if not skills:
        return []
    return [s.name for s in skills]


# ---------------------------------------------------------------------------
# Hot-reload e2e: changing .reyn/config/skills.yaml updates LIVE available_skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hotreload_adds_skill_to_live_available_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: hot-reload seam updates the LIVE get_available_skills() on the
    RouterHostAdapter — not a dead local variable — when a skill is added to
    .reyn/config/skills.yaml."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    # No skills before the file is written.
    assert "hot-fix" not in _skill_names(session)

    # Write a skill to the IN-set.
    _write_skills(tmp_path, {
        "hot-fix": {"path": "skills/hot-fix/SKILL.md", "description": "quick hot-fix workflow"},
    })

    # Apply the seam — the same path the HotReloader fires at the turn boundary.
    changed = await session._reapply_skills({})

    assert changed is True
    assert "hot-fix" in _skill_names(session), (
        "get_available_skills() must reflect the newly written skill after _reapply_skills"
    )


@pytest.mark.asyncio
async def test_hotreload_removes_skill_from_live_available_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: hot-reload seam removes a skill from get_available_skills() when it
    is removed from .reyn/config/skills.yaml (handles deletion, not just addition)."""
    monkeypatch.chdir(tmp_path)
    # Start with a skill present.
    _write_skills(tmp_path, {
        "old-skill": {"path": "skills/old/SKILL.md", "description": "old"},
    })
    session = _make_session(tmp_path)
    assert "old-skill" in _skill_names(session)

    # Remove the skill from the IN-set.
    _skills_yaml_path(tmp_path).write_text(
        yaml.safe_dump({"skills": {"entries": {}}}), encoding="utf-8",
    )

    changed = await session._reapply_skills({})

    assert changed is True
    assert "old-skill" not in _skill_names(session), (
        "get_available_skills() must no longer include the removed skill after _reapply_skills"
    )


@pytest.mark.asyncio
async def test_hotreload_noop_when_skills_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: _reapply_skills returns False (no change) when .reyn/config/skills.yaml
    has not changed between two reload calls."""
    monkeypatch.chdir(tmp_path)
    _write_skills(tmp_path, {
        "stable": {"path": "skills/stable/SKILL.md", "description": "stable"},
    })
    session = _make_session(tmp_path)

    # First apply picks up the change from startup state.
    await session._reapply_skills({})
    # Second apply with identical file → no change.
    changed = await session._reapply_skills({})

    assert changed is False


@pytest.mark.asyncio
async def test_hotreload_seam_registered(tmp_path: Path) -> None:
    """Tier 2: the Session registers the skills seam on the HotReloader."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    seam_names = [name for (name, _fn) in session._hot_reloader._seams]
    assert "skills" in seam_names


# ---------------------------------------------------------------------------
# Toggle e2e: set_capability_visible("skill", ...) filters get_available_skills()
# ---------------------------------------------------------------------------

def test_toggle_disable_skill_removes_from_live_list(tmp_path: Path) -> None:
    """Tier 2: set_capability_visible("skill", name, False) removes the skill from
    get_available_skills() immediately (live next turn — no restart needed)."""
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="deploy", description="deploy", path="skills/deploy/SKILL.md"),
            SkillEntry(name="review", description="review", path="skills/review/SKILL.md"),
        ]),
    )
    assert "deploy" in _skill_names(session)

    session.set_capability_visible("skill", "deploy", False)

    assert "deploy" not in _skill_names(session), (
        "disabled skill must be absent from get_available_skills()"
    )
    assert "review" in _skill_names(session), (
        "non-disabled skill must remain in get_available_skills()"
    )


def test_toggle_reenable_skill_restores_to_live_list(tmp_path: Path) -> None:
    """Tier 2: set_capability_visible("skill", name, True) restores the skill to
    get_available_skills() — toggle-ON reverses a prior toggle-OFF."""
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="deploy", description="deploy", path="skills/deploy/SKILL.md"),
        ]),
    )
    session.set_capability_visible("skill", "deploy", False)
    assert "deploy" not in _skill_names(session)

    session.set_capability_visible("skill", "deploy", True)

    assert "deploy" in _skill_names(session), (
        "re-enabled skill must reappear in get_available_skills()"
    )


def test_toggle_disable_unregistered_skill_is_noop(tmp_path: Path) -> None:
    """Tier 2: disabling a skill name not in the registered set is a no-op — the
    restrict-only invariant: toggle can only hide within the registered set."""
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="real-skill", description="real", path="skills/real/SKILL.md"),
        ]),
    )
    # Disable a name that is NOT in the registered set.
    session.set_capability_visible("skill", "nonexistent-skill", False)

    # The registered skill must still be present.
    assert "real-skill" in _skill_names(session), (
        "disabling an unregistered skill must not affect the registered set"
    )


def test_toggle_unknown_kind_raises(tmp_path: Path) -> None:
    """Tier 2: set_capability_visible with an unknown kind raises ValueError."""
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    with pytest.raises(ValueError, match="unknown capability kind"):
        session.set_capability_visible("bogus", "something", False)


def test_capability_visibility_state_includes_skill_kind(tmp_path: Path) -> None:
    """Tier 2: capability_visibility_state() reports skill kind in authorized + hidden."""
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="review", description="review", path="skills/review/SKILL.md"),
        ]),
    )
    state = session.capability_visibility_state()
    authorized_skills = [e for e in state["authorized"] if e["kind"] == "skill"]
    assert {"kind": "skill", "name": "review"} in authorized_skills, (
        "registered skill must appear in capability_visibility_state authorized list"
    )

    session.set_capability_visible("skill", "review", False)

    state2 = session.capability_visibility_state()
    hidden_skills = [e for e in state2["hidden_by_session"] if e["kind"] == "skill"]
    assert {"kind": "skill", "name": "review"} in hidden_skills, (
        "disabled skill must appear in hidden_by_session"
    )


# ---------------------------------------------------------------------------
# Persistence: toggle survives persist + restore via load_persisted_toggles
# ---------------------------------------------------------------------------

def test_skill_toggle_persists_to_visibility_yaml(tmp_path: Path) -> None:
    """Tier 2: set_capability_visible("skill", ...) persists the disabled name to
    visibility.yaml in the per-session state dir."""
    snap = tmp_path / "snap.json"
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=snap,
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="linter", description="lint", path="skills/linter/SKILL.md"),
        ]),
    )
    session.set_capability_visible("skill", "linter", False)

    vpath = snap.parent / "visibility.yaml"
    assert vpath.is_file(), "visibility.yaml must be written after a skill toggle"
    data = yaml.safe_load(vpath.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "linter" in (data.get("skill") or []), (
        "disabled skill name must be stored in visibility.yaml under 'skill' key"
    )


def test_skill_toggle_restored_by_load_persisted_toggles(tmp_path: Path) -> None:
    """Tier 2: load_persisted_toggles() restores a persisted skill toggle and
    re-applies the filter on the live RouterHostAdapter — the skill is absent from
    get_available_skills() after restore, exactly as after the original toggle."""
    snap = tmp_path / "snap.json"
    # Session A: disable the skill and persist.
    session_a = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s_a.wal"),
        snapshot_path=snap,
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="deploy", description="deploy", path="skills/deploy/SKILL.md"),
            SkillEntry(name="review", description="review", path="skills/review/SKILL.md"),
        ]),
    )
    session_a.set_capability_visible("skill", "deploy", False)

    # Session B: same state dir, same available_skills — simulate a restart.
    session_b = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s_b.wal"),
        snapshot_path=snap,
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="deploy", description="deploy", path="skills/deploy/SKILL.md"),
            SkillEntry(name="review", description="review", path="skills/review/SKILL.md"),
        ]),
    )
    # Before restore the full set is visible (persisted toggle not yet applied).
    assert "deploy" in _skill_names(session_b)

    session_b.load_persisted_toggles()

    # After restore, the disabled skill must be absent.
    assert "deploy" not in _skill_names(session_b), (
        "load_persisted_toggles must restore the skill disable filter on the live host"
    )
    assert "review" in _skill_names(session_b), (
        "non-disabled skill must remain after load_persisted_toggles"
    )


def test_skill_toggle_persist_clears_when_reenabled(tmp_path: Path) -> None:
    """Tier 2: re-enabling a skill removes the name from visibility.yaml (or removes
    the file when no overrides remain) — the stored state matches the current toggle."""
    snap = tmp_path / "snap.json"
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=snap,
        capability_scope=CapabilityScope(available_skills=[
            SkillEntry(name="linter", description="lint", path="skills/linter/SKILL.md"),
        ]),
    )
    session.set_capability_visible("skill", "linter", False)
    vpath = snap.parent / "visibility.yaml"
    assert vpath.is_file()

    session.set_capability_visible("skill", "linter", True)

    # The visibility.yaml must no longer contain the skill name.
    if vpath.is_file():
        data = yaml.safe_load(vpath.read_text(encoding="utf-8")) or {}
        assert "linter" not in (data.get("skill") or []), (
            "re-enabled skill must be removed from visibility.yaml"
        )


# ---------------------------------------------------------------------------
# validate_in_set: skills section validation
# ---------------------------------------------------------------------------

def test_validate_in_set_accepts_valid_skills_section() -> None:
    """Tier 2: validate_in_set accepts a well-formed skills section."""
    from reyn.runtime.hot_reload import validate_in_set
    assert validate_in_set({}) is None
    assert validate_in_set({"skills": {}}) is None
    assert validate_in_set({"skills": {"entries": {}}}) is None
    assert validate_in_set({"skills": {"entries": {
        "my-skill": {"path": "skills/my/SKILL.md", "description": "ok"},
    }}}) is None


def test_validate_in_set_rejects_malformed_skills_section() -> None:
    """Tier 2: validate_in_set rejects a skills section that is not a mapping."""
    from reyn.runtime.hot_reload import validate_in_set
    reason = validate_in_set({"skills": "not-a-dict"})
    assert reason is not None and "mapping" in reason

    reason2 = validate_in_set({"skills": {"entries": "not-a-dict"}})
    assert reason2 is not None and "mapping" in reason2
