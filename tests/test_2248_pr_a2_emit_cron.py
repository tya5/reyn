"""Tier 2: OS invariant — #2248 PR-A2 config-recovery emission for the cron registry.

The REAL cron tool handlers (``_handle_cron_register`` / ``_set_enabled`` /
``_handle_cron_unregister``) — handed a ``state_log`` via their ToolContext (the
production wiring: session → ToolContext) — emit a ``config_changed`` WAL event
carrying the FULL post-mutation cron registry content after they persist
``.reyn/cron.yaml``. The yaml is a derived projection; the WAL event is the
recovery truth.

Real StateLog + real handlers + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.tools.cron import (
    _handle_cron_register,
    _handle_cron_unregister,
    _set_enabled,
)
from reyn.tools.types import ToolContext


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


class _Workspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _ctx(base_dir: Path, state_log: StateLog) -> ToolContext:
    return ToolContext(
        events=_Events(),
        permission_resolver=None,  # unit-test context → _gate is a no-op
        workspace=_Workspace(base_dir=base_dir),
        caller_kind="router",
        state_log=state_log,
    )


def _config_changed(state_log: StateLog, after_seq: int) -> list[dict]:
    return [
        e
        for e in state_log.iter_from(after_seq + 1)
        if e.get("kind") == "config_changed"
    ]


@pytest.mark.asyncio
async def test_cron_register_emits_config_changed_full_content(tmp_path):
    """Tier 2: a REAL cron_register (state_log threaded into ToolContext) emits
    config_changed with the FULL post-register cron content keyed by the
    `.reyn`-relative path. RED if the handler didn't emit after persisting."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    ctx = _ctx(tmp_path, state_log)
    before = state_log.current_seq

    result = await _handle_cron_register(
        {
            "name": "morning_news",
            "to": "news_agent",
            "message": "今日のまとめ",
            "schedule": "0 9 * * *",
            "enabled": True,
        },
        ctx,
    )
    assert result["status"] == "ok"

    [ev] = _config_changed(state_log, before)
    assert ev["path"] == "cron.yaml"
    jobs = ev["content"]["cron"]["jobs"]
    [job] = [j for j in jobs if j.get("name") == "morning_news"]
    assert job["to"] == "news_agent"
    assert job["schedule"] == "0 9 * * *"
    # the yaml is a derived projection of the same content
    on_disk = yaml.safe_load(
        (tmp_path / ".reyn" / "cron.yaml").read_text(encoding="utf-8")
    )
    assert ev["content"] == on_disk


@pytest.mark.asyncio
async def test_cron_disable_emits_full_post_state(tmp_path):
    """Tier 2: a REAL cron disable emits config_changed carrying the FULL post-
    mutation registry (the disabled job present with enabled=False), not a delta."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    ctx = _ctx(tmp_path, state_log)
    await _handle_cron_register(
        {
            "name": "weekly",
            "to": "agent",
            "message": "report",
            "schedule": "0 9 * * MON",
            "enabled": True,
        },
        ctx,
    )
    before = state_log.current_seq

    await _set_enabled({"name": "weekly"}, ctx, enabled=False)

    [ev] = _config_changed(state_log, before)
    assert ev["path"] == "cron.yaml"
    [job] = [j for j in ev["content"]["cron"]["jobs"] if j["name"] == "weekly"]
    assert job["enabled"] is False


@pytest.mark.asyncio
async def test_cron_unregister_emits_full_post_state(tmp_path):
    """Tier 2: a REAL cron unregister emits config_changed carrying the FULL post-
    removal registry (the removed job absent from content)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    ctx = _ctx(tmp_path, state_log)
    await _handle_cron_register(
        {
            "name": "gone",
            "to": "agent",
            "message": "x",
            "schedule": "0 9 * * *",
            "enabled": True,
        },
        ctx,
    )
    before = state_log.current_seq

    await _handle_cron_unregister({"name": "gone"}, ctx)

    [ev] = _config_changed(state_log, before)
    assert ev["path"] == "cron.yaml"
    names = [j.get("name") for j in ev["content"]["cron"]["jobs"]]
    assert "gone" not in names
