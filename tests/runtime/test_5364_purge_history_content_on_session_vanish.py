"""Tier 2: #5364 §1.6 "Q" — a spawned session's own spilled tool-result
content (``.reyn/memory/history-content/<agent>/<sid>/``) must not be
ORPHANED once ``registry.remove_session`` tears the session down.
Before #5364 §1.6 nothing ever purged this directory: the GC cap
(§1.6 "C") bounds a LIVE session's own content, it has no trigger for a
session that no longer exists at all.

Real ``AgentRegistry`` + real on-disk content written via
:func:`history_content_dir_for` (the SAME path a live ``MediaStore``
would use) — the (name, sid) key-space is agent-scoped (#5364's own
key-space fix), so purging it here is safe only because it matches
``_session_state_dir``'s own key shape exactly (pre-fix, this directory
was shared across every agent's same-named session — see this file's
sibling ``test_5364_history_content_agent_scoped_keyspace.py``).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.data.workspace.media_store import history_content_dir_for
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _no_factory(_profile):
        raise AssertionError("session factory must not be called in this test")

    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _seed_spilled_content(tmp_path: Path, agent: str, sid: str) -> Path:
    """Real on-disk content at the SAME path a live MediaStore would
    have written it to — via the standalone function, not a hand-rolled
    path (the exact 1-source-of-truth reason it exists)."""
    content_dir = history_content_dir_for(tmp_path, agent, sid)
    content_dir.mkdir(parents=True, exist_ok=True)
    (content_dir / "spilled.txt").write_text("this session's own content", encoding="utf-8")
    return content_dir


@pytest.mark.asyncio
async def test_remove_session_purges_the_sessions_own_spilled_content(tmp_path) -> None:
    """Tier 2: the headline — tearing down a spawned session via
    ``remove_session`` must ALSO remove its own history-content
    directory, not just its state dir."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "worker")
    reg._sessions.setdefault("worker", {})["task1"] = SimpleNamespace()
    content_dir = _seed_spilled_content(tmp_path, "worker", "task1")
    assert content_dir.is_dir()

    assert await reg.remove_session("worker", "task1") is True

    assert not content_dir.exists()


@pytest.mark.asyncio
async def test_remove_session_does_not_touch_a_different_agents_same_sid_content(
    tmp_path,
) -> None:
    """Tier 2: the attribution-axis check for Q itself — removing
    ``worker``'s ``task1`` session must NOT purge ``other``'s own
    ``task1`` content (same sid, different agent — exactly the
    cross-agent key-space #5364's own fix separated)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "worker")
    reg._sessions.setdefault("worker", {})["task1"] = SimpleNamespace()
    victim_dir = _seed_spilled_content(tmp_path, "worker", "task1")
    survivor_dir = _seed_spilled_content(tmp_path, "other", "task1")

    await reg.remove_session("worker", "task1")

    assert not victim_dir.exists()
    assert survivor_dir.is_dir()


@pytest.mark.asyncio
async def test_remove_session_with_no_spilled_content_is_still_a_clean_noop(
    tmp_path,
) -> None:
    """Tier 2: a session that never wrote any tool-result content (the
    common case) must not error just because Q's own directory never
    existed."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "worker")
    reg._sessions.setdefault("worker", {})["task1"] = SimpleNamespace()

    assert await reg.remove_session("worker", "task1") is True
