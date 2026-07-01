"""Tier 2: #2374 — the hot-reload two-path contract for cron / hooks / skills.

The two-path principle (settled with owner, established by #2372 MCP): a config edit's reload
behavior depends on WHO edits it —
  - CLI edit (`reyn ...`) → writes the OUT-set (reyn.yaml) → restart to take effect.
  - LLM-tool / slash edit → writes the IN-set (.reyn/config/{cron,hooks}.yaml, or a skill file) →
    live within the same session (next turn), no restart.

The MCP roster-frozen gap (#2372) was MCP-SPECIFIC (the server roster was cached at ctor and gated
the enumeration). cron / hooks / skills satisfy the tool/slash-live intent via their OWN mechanisms
— so this is verify-first: each test proves the domain takes effect mid-session without restart
(GREEN = already live, the regression guard + two-path documentation). A RED would mark a genuine
MCP-class gap to fix. These tests upgrade the flow-trace inference to run-verified.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.events import EventLog
from reyn.runtime.cron.scheduler import CronScheduler, set_active_scheduler
from reyn.runtime.hot_reload import HotReloader, set_active_hot_reloader
from reyn.runtime.session import enumerate_available_skills
from reyn.tools.cron import _handle_cron_register
from reyn.tools.hooks import _handle_hooks_add
from reyn.tools.types import ToolContext


def _names(entries: list[dict]) -> set[str]:
    return {e["name"] for e in entries}


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(base_dir=tmp_path, root=tmp_path), caller_kind="router",
    )


def test_skills_are_filesystem_live_no_reload(tmp_path, monkeypatch):
    """Tier 2: a skill authored mid-session (a new skill.md under reyn/project) is enumerated on the
    NEXT enumeration WITHOUT any reload — skills resolve filesystem-live (enumerate_available_skills
    walks reyn/project → reyn/local → stdlib per call), like agent discovery. No config.yaml, no
    ctor-frozen roster → no MCP-class gap. GREEN = already live (the two-path 'tool-side' behavior)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn" / "project").mkdir(parents=True)
    assert "hotskill" not in _names(enumerate_available_skills(set()))  # absent before authoring

    d = tmp_path / "reyn" / "project" / "hotskill"
    d.mkdir()
    (d / "skill.md").write_text(
        "---\nname: hotskill\ndescription: a mid-session skill\n---\n# body\n", encoding="utf-8",
    )

    # no reload / no request_reload — the very next enumeration sees it (filesystem-live)
    assert "hotskill" in _names(enumerate_available_skills(set())), \
        "a new skill file must be visible next turn without a restart (filesystem-live)"


@pytest.mark.asyncio
async def test_cron_tool_add_is_live_via_scheduler_no_restart(tmp_path, monkeypatch):
    """Tier 2: a cron job added by the tool mid-session is registered LIVE on the active scheduler
    (next fire reflects it, no restart) AND persisted to the IN-set .reyn/config/cron.yaml. cron's
    own mechanism (direct get_active_scheduler().add_job) satisfies the tool/slash-live intent — no
    MCP-class roster-freeze. GREEN = already live. (CLI edits reyn.yaml → restart, unchanged.)"""
    monkeypatch.chdir(tmp_path)
    sched = CronScheduler([])
    set_active_scheduler(sched)
    try:
        res = await _handle_cron_register(
            {"name": "j1", "to": "alice", "message": "hi", "schedule": "* * * * *"}, _ctx(tmp_path),
        )
        assert (tmp_path / ".reyn" / "config" / "cron.yaml").exists(), "IN-set write"
        assert sched.get_job("j1") is not None, "the scheduler has the job LIVE (no restart)"
        assert res["live_update_applied"] is True
    finally:
        set_active_scheduler(None)


@pytest.mark.asyncio
async def test_hooks_tool_add_writes_inset_and_schedules_reload(tmp_path, monkeypatch):
    """Tier 2: a hook added by the tool mid-session is persisted to the IN-set .reyn/config/hooks.yaml
    (structurally cannot target reyn.yaml) AND schedules a hot-reload (request_reload → next-turn
    apply_pending → _reapply_hooks rebuilds the live registry). hooks already wires the MCP-style
    trigger. GREEN = already live. (CLI-equivalent startup hooks in reyn.yaml → restart, unchanged.)"""
    monkeypatch.chdir(tmp_path)
    reloader = HotReloader(project_root=tmp_path, events=None)
    set_active_hot_reloader(reloader)
    try:
        res = await _handle_hooks_add({"on": "turn_end", "message": "continue"}, _ctx(tmp_path))
        assert res.get("status") != "error", res
        assert (tmp_path / ".reyn" / "config" / "hooks.yaml").exists(), "IN-set write"
        assert reloader.pending is True, "the tool-edit scheduled a hot-reload (no restart)"
    finally:
        set_active_hot_reloader(None)
