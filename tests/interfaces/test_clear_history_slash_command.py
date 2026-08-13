"""Tier 2: ``/clear-history`` slash command (REGISTRY dispatch, on-disk wipe).

User dogfood 2026-05-25 asked for a slash that resets history + action_usage
to initial state without touching anything else. This file pins the
history half of that request end-to-end through the real slash REGISTRY:

1. Two-step confirmation (= bare ``/clear-history`` warns, requires
   ``confirm`` to actually wipe).
2. ``confirm`` form clears ``session.history`` (in-memory) AND removes
   ``session.history_path`` (on-disk).
3. The slash does NOT touch the ``events/`` directory, the WAL, or the
   per-agent snapshot.

#4552: this file used to also pin ``session._action_usage_tracker.reset()``
being called on confirm — removed with the hot-list feature the tracker
existed for (owner directive: discarded, superseded by ``list_actions`` as
the canonical discovery path). The command no longer reads or clears any
tracker; only the history side of the original user request survives.
``ActionUsageTracker.reset()``'s own unit tests lived here too and are gone
along with the deleted ``reyn.tools.action_usage_tracker`` module.

These are Tier 2 (= OS invariant — wipe surface guarantees) rather
than Tier 1 because they involve the slash router + filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.interfaces.slash import REGISTRY
from tests._support.slash import slash_ctx


class _StubSession:
    """Minimal session-shaped object the slash handler reads from.

    The handler uses ``history`` and ``history_path``; its replies go
    through the client transport (#3595 S4), not through this object.
    Everything else can be absent.
    """

    def __init__(self, *, history: list, history_path: Path):
        self.history = history
        self.history_path = history_path


# ── /clear-history slash command ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_slash_registered():
    """Tier 2: the slash command is discoverable via the registry."""
    cmd = REGISTRY.get("clear-history")
    assert cmd is not None
    assert cmd.name == "clear-history"


@pytest.mark.asyncio
async def test_bare_slash_prints_warning_and_does_not_wipe(tmp_path: Path):
    """Tier 2: ``/clear-history`` (no confirm) preserves all data and
    prints a warning that asks for the confirm token."""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text("nonempty\n")

    session = _StubSession(history=["turn1", "turn2"], history_path=history_path)
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    assert cmd is not None
    await cmd.handler(ctx, "")

    msgs = ctx.transport.displayed
    assert len(msgs) >= 1
    body = msgs[-1].text
    assert "confirm" in body.lower()
    # Data still intact.
    assert session.history == ["turn1", "turn2"]
    assert history_path.exists()


@pytest.mark.asyncio
async def test_confirm_clears_history(tmp_path: Path):
    """Tier 2: ``/clear-history confirm`` wipes history in-memory and on disk."""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps({"role": "user", "content": "hi"}) + "\n",
    )

    session = _StubSession(history=["turn1", "turn2", "turn3"], history_path=history_path)
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")

    assert session.history == []
    assert not history_path.exists()
    msgs = ctx.transport.displayed
    success_lines = [m.text for m in msgs if "Cleared" in m.text]
    assert success_lines, f"expected a confirmation; got {[m.text for m in msgs]}"


@pytest.mark.asyncio
async def test_confirm_preserves_unrelated_files(tmp_path: Path):
    """Tier 2: the slash MUST NOT touch events/, the WAL, or snapshots —
    those live elsewhere on disk. Place a sentinel file in each and
    verify it survives."""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text("h\n")

    # Sibling sentinels — these stand in for events/ / state/ etc.
    events_sentinel = tmp_path / "events.jsonl"
    events_sentinel.write_text("audit-log-entry\n")
    wal_sentinel = tmp_path / "wal.jsonl"
    wal_sentinel.write_text("wal-entry\n")
    snapshot_sentinel = tmp_path / "snapshot.json"
    snapshot_sentinel.write_text("{}\n")

    session = _StubSession(history=["x"], history_path=history_path)
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")

    assert events_sentinel.read_text() == "audit-log-entry\n"
    assert wal_sentinel.read_text() == "wal-entry\n"
    assert snapshot_sentinel.read_text() == "{}\n"


@pytest.mark.asyncio
async def test_confirm_when_history_already_empty(tmp_path: Path):
    """Tier 2: empty history → success message stays informative, no crash."""
    session = _StubSession(history=[], history_path=tmp_path / "nonexistent.jsonl")
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")
    msgs = ctx.transport.displayed
    assert msgs  # something was said
