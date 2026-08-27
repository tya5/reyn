"""Tier 2: #5278 — the status panel's ``cron_jobs`` row (chrome.py's
``cron_pane_lines``) never reflected a cron hot-reload: it read only
``config.cron.jobs``, a reference frozen at construction and never
reassigned, while the ACTUAL hot-reload machinery (``Session._reapply_cron``)
mutates a completely separate live object — the running
:class:`~reyn.runtime.cron.scheduler.CronScheduler` — via
``sched.add_job``/``sched.remove_job``, never touching ``config`` at all.

Root cause confirmed by a dedicated investigation before this fix (issue
#5278's own comment thread): of the 4 fields named in the issue
(``cron_jobs``/``mcp_servers``/``hooks``/``skills``), only ``cron_jobs`` had
NO live sibling at all. ``mcp_servers``/``skills`` already flow through
``visibility_items`` (backed by ``capability_visibility_state()``'s memoized
census, invalidated at ``_reapply_mcp``/``_reapply_skills`` — #5276/#5284/
#5285's own recent work) and ``hooks`` already flows through ``hook_items``
(backed by ``session._cached_hook_items``, invalidated at
``_reapply_hooks`` — also #5284) — both correctly reflect a hot-reload
already, via chrome.py's own live-then-fallback pattern
(``_hook_pane_entries``/``_visibility_pane_rows``). Only cron lacked the
live half of that pair; this fix adds it, following the SAME shape.

Fix: ``status._extract_live_cron_jobs()`` reads
``get_active_scheduler().jobs()`` (the actual running scheduler
``_reapply_cron`` mutates) fresh on every call — no cache needed, since
``CronScheduler.jobs()`` is a cheap ``list(self._jobs.values())`` read.
Exposed as the snapshot's new ``cron_items`` key; ``cron_pane_lines``
prefers it, falling back to the stale ``cron_jobs`` only when no scheduler
is registered for this process at all (mirrors ``_hook_pane_entries``'s own
``hook_items``-then-``hooks`` fallback exactly).

Real ``AgentRegistry``/``Session`` + a real ``CronScheduler`` (registered via
``set_active_scheduler``, following ``test_2073_s2_reapply_seams.py``'s own
established idiom) — no mocks. The real ``Session._reapply_cron`` seam
drives the live mutation; the verdict is read off the real
``_snapshot_for_session`` + ``cron_pane_lines`` render path."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.interfaces.inline.textual_chat.chrome import cron_pane_lines
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.runtime.cron.scheduler import CronScheduler, set_active_scheduler
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


_STALE_CONFIG = SimpleNamespace(cron=SimpleNamespace(jobs=[
    SimpleNamespace(name="boot-time-job", schedule="@daily", enabled=True),
]))


@pytest.mark.asyncio
async def test_cron_panel_reflects_a_live_reload_the_boot_time_config_never_saw(
    tmp_path,
) -> None:
    """Tier 2: acceptance — after a real ``_reapply_cron`` hot-reload adds a
    job the boot-time ``config`` never had, the snapshot's ``cron_items``
    (and the rendered cron pane) show the NEW job; the stale ``cron_jobs``
    field still shows only the old, boot-time one — proving the panel now
    reads the live sibling, not the frozen config."""
    reg = _make_registry(tmp_path)
    s = await reg.ensure_running("alpha")

    sched = CronScheduler([])
    set_active_scheduler(sched)
    try:
        changed = await s._reapply_cron({
            "cron": {"jobs": [
                {"name": "hot-reloaded-job", "schedule": "*/5 * * * *",
                 "to": "alpha", "message": "hi"},
            ]},
        })
        assert changed is True  # sanity: the real seam reports it applied

        snap = _snapshot_for_session(reg, s, config=_STALE_CONFIG)

        assert snap["cron_jobs"] == [
            {"name": "boot-time-job", "schedule": "@daily", "enabled": True},
        ], "test construction error: the stale field should still show only the boot-time job"

        assert snap["cron_items"] == [
            {"name": "hot-reloaded-job", "schedule": "*/5 * * * *", "enabled": True},
        ], (
            "#5278 REGRESSION: cron_items did not reflect the real "
            f"_reapply_cron hot-reload — got {snap['cron_items']!r}"
        )

        rendered = cron_pane_lines(snap)
        assert any("hot-reloaded-job" in line for line in rendered), (
            f"#5278 REGRESSION: the rendered cron pane does not show the "
            f"hot-reloaded job — got {rendered!r}"
        )
        assert not any("boot-time-job" in line for line in rendered), (
            "the rendered pane should show the LIVE scheduler's jobs, not "
            "a mix of live and stale — got a boot-time-job line too: "
            f"{rendered!r}"
        )
    finally:
        set_active_scheduler(None)


@pytest.mark.asyncio
async def test_cron_panel_falls_back_to_stale_config_when_no_scheduler_is_active(
    tmp_path,
) -> None:
    """Tier 2: falsification contrast — with NO active scheduler (the
    ordinary bare local CUI case, no AG-UI web gateway running — the
    ONLY site that calls ``set_active_scheduler`` today, grep-confirmed;
    a standalone ``reyn cron`` CLI invocation constructs its own
    scheduler but never registers it as active), ``cron_items`` is
    ``None`` (not-wired, distinct from a real empty scheduler) and the
    panel correctly falls back to the config-derived ``cron_jobs`` —
    unchanged from before this fix, no regression for the no-scheduler
    case."""
    reg = _make_registry(tmp_path)
    s = await reg.ensure_running("alpha")

    set_active_scheduler(None)  # explicit: no scheduler registered
    snap = _snapshot_for_session(reg, s, config=_STALE_CONFIG)

    assert snap["cron_items"] is None, (
        "test construction error: expected no active scheduler to report "
        f"None, got {snap['cron_items']!r}"
    )
    rendered = cron_pane_lines(snap)
    assert any("boot-time-job" in line for line in rendered), (
        f"expected the stale-config fallback when no scheduler is active, "
        f"got {rendered!r}"
    )
