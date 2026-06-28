"""Tier 2: OS invariant — #2259 config-recovery emission for the cron registry.

The REAL cron tool handlers (``_handle_cron_register`` / ``_set_enabled`` /
``_handle_cron_unregister``) — handed a ``state_log`` via their ToolContext (the
production wiring: session → ToolContext) — record a full-state config GENERATION
carrying the FULL post-mutation cron registry content after they persist
``.reyn/config/cron.yaml``. The yaml is a derived projection; the generation is the
recovery truth (it reconstructs the registry as-of-cut and survives WAL truncation).

Real StateLog + real handlers + real AgentRegistry + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
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


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _ctx(base_dir: Path, state_log: StateLog) -> ToolContext:
    return ToolContext(
        events=_Events(),
        permission_resolver=None,  # unit-test context → _gate is a no-op
        workspace=_Workspace(base_dir=base_dir),
        caller_kind="router",
        state_log=state_log,
    )


def _reconstructed_cron(tmp_path: Path, state_log: StateLog) -> dict:
    """Reconstruct the cron registry from its generation as-of the current WAL head — the
    recovery truth re-materialised onto disk (the post-mutation full state)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(state_log.current_seq)
    return yaml.safe_load(
        (tmp_path / ".reyn" / "config" / "cron.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.asyncio
async def test_cron_register_records_generation_full_content(tmp_path):
    """Tier 2: a REAL cron_register (state_log threaded into ToolContext) records a generation
    with the FULL post-register cron content — reconstructable as-of-cut. RED if the handler
    didn't record after persisting."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    ctx = _ctx(tmp_path, state_log)

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

    content = _reconstructed_cron(tmp_path, state_log)
    [job] = [j for j in content["cron"]["jobs"] if j.get("name") == "morning_news"]
    assert job["to"] == "news_agent"
    assert job["schedule"] == "0 9 * * *"
    # the live yaml is a derived projection of the same content
    on_disk = yaml.safe_load(
        (tmp_path / ".reyn" / "config" / "cron.yaml").read_text(encoding="utf-8")
    )
    assert content == on_disk


@pytest.mark.asyncio
async def test_cron_disable_records_full_post_state(tmp_path):
    """Tier 2: a REAL cron disable records a generation carrying the FULL post-mutation registry
    (the disabled job present with enabled=False), not a delta."""
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

    await _set_enabled({"name": "weekly"}, ctx, enabled=False)

    content = _reconstructed_cron(tmp_path, state_log)
    [job] = [j for j in content["cron"]["jobs"] if j["name"] == "weekly"]
    assert job["enabled"] is False


@pytest.mark.asyncio
async def test_cron_unregister_records_full_post_state(tmp_path):
    """Tier 2: a REAL cron unregister records a generation carrying the FULL post-removal
    registry (the removed job absent from content)."""
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

    await _handle_cron_unregister({"name": "gone"}, ctx)

    content = _reconstructed_cron(tmp_path, state_log)
    names = [j.get("name") for j in content["cron"]["jobs"]]
    assert "gone" not in names
