"""Tier 2: #5276 — cron_jobs/mcp_servers/hooks/skills are computed at most
ONCE per (session, config) pair, not recomputed every render frame.

Root cause: these 4 status-panel fields are pure functions of ``config``,
and ``config`` is a frozen reference assigned exactly once at construction
(``TextualChatApp.__init__``'s ``self._config = config`` is the only
assignment site in that class; ``Session`` never assigns ``self._config``
at all — grep-confirmed). So every pre-#5276 render frame recomputed the
IDENTICAL result from the same unchanging input — pure waste, not a
correctness issue. #5278 (filed separately, NOT fixed here) is the real,
disclosed gap this leaves untouched: the actual hot-reload machinery
mutates SEPARATE live objects (the cron scheduler, the hook dispatcher's
registry, ``_available_skills``, the MCP tool cache), never ``config``
itself — so these 4 fields never reflected a hot-reload before this PR
either, and still don't after it. This file tests ONLY the "computed at
most once" claim — it does not, and must not, assert anything about
hot-reload reflection (that would be asserting #5278's own absence).

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
