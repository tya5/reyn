"""Tier 2: #2073 S2 — per-component reapply seams + validate-before-apply.

S2 registers 4 reapply seams on the HotReloader (cron / MCP / per-agent-capability /
new-agent), each reapplying one IN-set component live at the turn boundary, plus the
validate-before-apply that rejects a malformed IN-set atomically (no seam runs, live
config unchanged = rollback). Hooks are S2b.

No mocks: the validate is a pure function; the reject path uses a real HotReloader +
the real loader + a recording seam; the real seams run on a real (minimal) Session.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.hot_reload import HotReloader, validate_in_set
from reyn.runtime.session import Session

# ── validate-before-apply (the structural IN-set check) ─────────────────────


def test_validate_accepts_valid_and_absent() -> None:
    """Tier 2: a well-formed or empty IN-set validates (None = ok)."""
    assert validate_in_set({}) is None
    assert validate_in_set({"mcp": {"servers": {}}}) is None
    assert validate_in_set(
        {"cron": {"jobs": [{"name": "j", "schedule": "* * * * *"}]}}
    ) is None


@pytest.mark.parametrize(
    "bad, needle",
    [
        ([], "mapping"),                                       # not a dict
        ({"cron": "nope"}, "cron section"),                    # cron not a mapping
        ({"cron": {"jobs": "nope"}}, "cron.jobs must be a list"),
        ({"cron": {"jobs": [{"message": "x"}]}}, "name + schedule"),  # job missing name/schedule
        ({"mcp": "nope"}, "mcp section"),                      # mcp not a mapping
    ],
)
def test_validate_rejects_malformed(bad, needle) -> None:
    """Tier 2: a malformed IN-set returns a decision-enabling reason (→ the reload
    is rejected before any seam runs)."""
    reason = validate_in_set(bad)
    assert reason is not None and needle in reason


@pytest.mark.asyncio
async def test_bad_in_set_is_rejected_no_seam_runs(tmp_path: Path) -> None:
    """Tier 2: validate-before-apply rollback — a malformed .reyn/cron.yaml is
    rejected whole: the registered seam is NOT called and no config_reloaded is
    emitted (the live config is unchanged)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    # valid YAML, structurally bad (a named cron job missing its schedule — survives
    # the loader's job-list merge but fails validate)
    (reyn_dir / "cron.yaml").write_text("cron:\n  jobs:\n    - name: j1\n", encoding="utf-8")
    events = EventLog()
    hr = HotReloader(project_root=tmp_path, events=events)
    calls: list = []

    async def seam(in_set: dict) -> bool:
        calls.append(in_set)
        return True

    hr.register_seam("cron", seam)
    hr.request_reload(source="operator")
    summary = await hr.apply_pending()

    assert summary["rejected"]                 # the reload was rejected
    assert calls == []                         # no seam ran (rollback)
    assert [e.type for e in events.all() if e.type == "config_reloaded"] == []


# ── the real reapply seams (real minimal Session) ──────────────────────────


def _make_session(tmp_path: Path, *, agent_name: str = "test-agent", allowed_skills=None) -> Session:
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        allowed_skills=allowed_skills,
    )


def test_seams_registered_on_the_reloader(tmp_path: Path) -> None:
    """Tier 2: the Session registers its 4 reapply seams on the HotReloader."""
    session = _make_session(tmp_path)
    names = [name for (name, _fn) in session._hot_reloader._seams]
    assert names == ["cron", "mcp", "per_agent_capability", "new_agent"]


@pytest.mark.asyncio
async def test_cron_seam_reapplies_jobs_live(tmp_path: Path) -> None:
    """Tier 2: the cron seam applies .reyn/cron.yaml jobs to the live scheduler."""
    from reyn.runtime.cron import CronScheduler, set_active_scheduler

    session = _make_session(tmp_path)
    sched = CronScheduler([])
    set_active_scheduler(sched)
    try:
        changed = await session._reapply_cron(
            {"cron": {"jobs": [{"name": "j1", "schedule": "* * * * *",
                                "to": "demo", "message": "hi"}]}},
        )
        assert changed is True
        assert sched.get_job("j1") is not None  # applied live
    finally:
        set_active_scheduler(None)


@pytest.mark.asyncio
async def test_per_agent_capability_seam_enforces_new_profile(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the Session-orchestrated per-agent seam re-reads profile.yaml so the
    SKILL gate enforces the new allowlist — BEHAVIOR, via the real spawn gate: after
    the reapply (profile allows only skill_a), spawning an un-listed skill is refused.
    This exercises the skill_runner holder the Session swapped (a holder the seam
    missed would leave the old decision)."""
    monkeypatch.chdir(tmp_path)  # project_root = cwd for the seam's profile re-read
    agent = "test-agent"
    prof_dir = tmp_path / ".reyn" / "agents" / agent
    prof_dir.mkdir(parents=True)
    (prof_dir / "profile.yaml").write_text(
        "name: test-agent\nallowed_skills: [skill_a]\n", encoding="utf-8",
    )
    # before: unrestricted (allowed_skills None) → the gate would allow any skill
    session = _make_session(tmp_path, agent_name=agent, allowed_skills=None)

    changed = await session._reapply_per_agent_capability({})
    assert changed is True

    # behavior through the public spawn gate: an un-listed skill is now refused
    # (the allowlist gate fires before skill-load, so skill_b need not exist).
    result = await session._skill_runner.run_skill_awaitable(
        {"skill": "skill_b", "input": {}}, chain_id="c1",
    )
    assert result["status"] == "error"
    assert "not in allowed_skills" in result["data"]["error"]


@pytest.mark.asyncio
async def test_per_agent_seam_noop_when_no_profile(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: no profile.yaml (single-agent) → the per-agent seam is a no-op."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, agent_name="solo")
    changed = await session._reapply_per_agent_capability({})
    assert changed is False


@pytest.mark.asyncio
async def test_new_agent_seam_is_confirming_noop(tmp_path: Path) -> None:
    """Tier 2: new-agent discovery is filesystem-live → the seam is a confirming
    no-op (returns False; nothing to actively reapply)."""
    session = _make_session(tmp_path)
    changed = await session._reapply_new_agent({})
    assert changed is False
