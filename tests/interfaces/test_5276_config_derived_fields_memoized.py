"""Tier 2: #5276 — cron_jobs/mcp_servers/hooks/skills are computed at most
ONCE per (session, config) pair, not recomputed every render frame.

Root cause: these 4 status-panel fields are pure functions of ``config``,
and ``config`` is a frozen reference assigned exactly once at construction
(``TextualChatApp.__init__``'s ``self._config = config`` is the only
assignment site in that class; ``Session`` never assigns ``self._config``
at all — grep-confirmed). So every pre-#5276 render frame recomputed the
IDENTICAL result from the same unchanging input — pure waste, not a
correctness issue. Memoizing them here causes ZERO behavior change either
way, since they never changed input to begin with — see
``_CONFIG_DERIVED_CACHE``'s own comment (status.py) for the full "why
caching here is safe" reasoning. This file tests ONLY the "computed at
most once" claim about THESE 4 CACHED fields themselves — it does not,
and must not, assert anything about whether the STATUS PANEL as a whole
reflects a hot-reload (a different, renderer-level question — see
#5278's own resolution below).

**#5278 update (filed from this PR's own review, since resolved for 3 of
the 4):** investigation at #5278 fix time found ``mcp_servers``/``hooks``/
``skills`` were NEVER actually stuck showing stale data for a real running
session — chrome.py's own renderer (``_hook_pane_entries``/
``_visibility_pane_rows``) prefers a LIVE sibling (``hook_items``/
``visibility_items``) that was already correctly invalidated at the hot-
reload mutation site (``_reapply_hooks``/``_reapply_mcp``/
``_reapply_skills`` — #5276②/#5285, this same investigation arc), falling
back to these cached, config-derived fields only when the live sibling is
empty (a remote connection that never wires it, or genuinely zero
configured — not a real local staleness case). Only ``cron_jobs`` had NO
live sibling at all; #5278's own fix added one (``cron_items``, reading the
real running ``CronScheduler`` — see ``_extract_live_cron_jobs``'s own
docstring, status.py). So: the 4 fields THIS file caches still never
reflect a hot-reload on their own (unchanged, correctly memoized-once) —
but the PANEL itself now does, for all 4, via each field's own live
sibling. Do not read this file's own claim as "the panel is still broken
for all 4" — that was #5278's original, correctly-disclosed-at-the-time
finding, since narrowed and resolved.

Real ``AgentRegistry``/``Session`` — no mocks. Uses the #4403 counting
technique (wrap the real extractor, count real calls) — the SAME technique
``test_5267_elide_cache_ownership.py`` uses for its own cache-hit witness.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.interfaces.repl.status import _snapshot_for_session
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


def _counting_wrapper(monkeypatch) -> dict:
    """Mirrors #4403's own counting technique: wraps the real
    ``_extract_cron_jobs`` and counts real calls from this point on."""
    import reyn.interfaces.repl.status as status_module

    real_fn = status_module._extract_cron_jobs
    call_count = {"n": 0}

    def _counting(config):
        call_count["n"] += 1
        return real_fn(config)

    monkeypatch.setattr(status_module, "_extract_cron_jobs", _counting)
    return call_count


@pytest.mark.asyncio
async def test_config_derived_fields_computed_once_across_repeated_snapshots(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — 3 ``_snapshot_for_session`` calls with the SAME
    session + config cost exactly 1 real ``_extract_cron_jobs`` call, not
    3 (the pre-#5276 shape)."""
    reg = _make_registry(tmp_path)
    s = await reg.ensure_running("alpha")
    config = SimpleNamespace(cron=SimpleNamespace(jobs=[
        SimpleNamespace(name="job1", schedule="@daily", enabled=True),
    ]))
    call_count = _counting_wrapper(monkeypatch)

    snap1 = _snapshot_for_session(reg, s, config)
    snap2 = _snapshot_for_session(reg, s, config)
    snap3 = _snapshot_for_session(reg, s, config)

    assert call_count["n"] == 1, (
        f"expected exactly 1 real _extract_cron_jobs call across 3 snapshots "
        f"(memoized per session+config), got {call_count['n']}"
    )
    assert snap1["cron_jobs"] == snap2["cron_jobs"] == snap3["cron_jobs"] == [
        {"name": "job1", "schedule": "@daily", "enabled": True},
    ]


@pytest.mark.asyncio
async def test_config_derived_fields_recompute_for_a_different_config_object(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast — a genuinely DIFFERENT config object
    (even for the same session) is NOT silently served the first config's
    stale cache entry; it recomputes."""
    reg = _make_registry(tmp_path)
    s = await reg.ensure_running("alpha")
    config_a = SimpleNamespace(cron=SimpleNamespace(
        jobs=[SimpleNamespace(name="a", schedule="@daily", enabled=True)],
    ))
    config_b = SimpleNamespace(cron=SimpleNamespace(
        jobs=[SimpleNamespace(name="b", schedule="@daily", enabled=True)],
    ))
    call_count = _counting_wrapper(monkeypatch)

    snap_a = _snapshot_for_session(reg, s, config_a)
    snap_b = _snapshot_for_session(reg, s, config_b)

    assert call_count["n"] == 2, (
        f"a different config object must recompute rather than reuse the "
        f"other config's cached entry, got {call_count['n']} real calls"
    )
    assert snap_a["cron_jobs"] == [{"name": "a", "schedule": "@daily", "enabled": True}]
    assert snap_b["cron_jobs"] == [{"name": "b", "schedule": "@daily", "enabled": True}]
