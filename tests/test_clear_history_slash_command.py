"""Tier 2: ``/clear-history`` slash command + ``ActionUsageTracker.reset()``.

User dogfood 2026-05-25 asked for a slash that resets history +
action_usage to initial state without touching anything else. This
file pins:

1. Two-step confirmation (= bare ``/clear-history`` warns, requires
   ``confirm`` to actually wipe).
2. ``confirm`` form clears ``session.history`` (in-memory) AND removes
   ``session.history_path`` (on-disk).
3. ``confirm`` form calls ``tracker.reset()`` when tracker is wired.
4. ``tracker.reset()`` empties the in-memory table + removes the
   persist file but leaves the tracker instance reusable.
5. The slash does NOT touch the ``events/`` directory, the WAL, or the
   per-agent snapshot.

These are Tier 2 (= OS invariant — wipe surface guarantees) rather
than Tier 1 because they involve the slash router + filesystem.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import REGISTRY
from reyn.tools.action_usage_tracker import ActionUsageTracker


class _StubSession:
    """Minimal session-shaped object the slash handler reads from.

    The handler uses ``history``, ``history_path``, ``_action_usage_tracker``,
    and the same ``_put_outbox`` channel ``reply()`` / ``reply_error()``
    use. Everything else can be absent.
    """

    def __init__(self, *, history: list, history_path: Path, tracker):
        self.history = history
        self.history_path = history_path
        self._action_usage_tracker = tracker
        self.outbox: asyncio.Queue = asyncio.Queue()

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        await self.outbox.put(msg)


def _drain_outbox(session: _StubSession) -> list[OutboxMessage]:
    msgs: list[OutboxMessage] = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


# ── ActionUsageTracker.reset() ────────────────────────────────────────────


def test_tracker_reset_empties_in_memory_state(tmp_path: Path):
    """Tier 2: tracker.reset() clears the compacted table."""
    path = tmp_path / "action_usage.json"
    tracker = ActionUsageTracker(persist_path=path)
    tracker.merge_compacted([("file__read", 100.0), ("file__write", 101.0)])
    assert len(tracker._compacted) == 2

    tracker.reset()
    assert tracker._compacted == {}


def test_tracker_reset_removes_persist_file(tmp_path: Path):
    """Tier 2: tracker.reset() deletes the on-disk persist file."""
    path = tmp_path / "action_usage.json"
    tracker = ActionUsageTracker(persist_path=path)
    tracker.merge_compacted([("file__read", 100.0)])
    assert path.exists()

    tracker.reset()
    assert not path.exists()


def test_tracker_reset_safe_when_file_already_gone(tmp_path: Path):
    """Tier 2: reset() is idempotent — second call is a no-op, no exception."""
    path = tmp_path / "action_usage.json"
    tracker = ActionUsageTracker(persist_path=path)
    tracker.merge_compacted([("file__read", 100.0)])
    tracker.reset()
    # Second reset should not raise even though file is gone.
    tracker.reset()
    assert tracker._compacted == {}


def test_tracker_reset_preserves_instance_identity(tmp_path: Path):
    """Tier 2: tracker stays usable after reset — merge_compacted still
    appends from a clean slate so the caller's wiring keeps working."""
    path = tmp_path / "action_usage.json"
    tracker = ActionUsageTracker(persist_path=path)
    tracker.merge_compacted([("file__read", 50.0)])
    tracker.reset()
    tracker.merge_compacted([("file__write", 200.0)])
    assert "file__read" not in tracker._compacted
    assert "file__write" in tracker._compacted


def test_tracker_reset_with_no_persist_path():
    """Tier 2: tracker constructed without persist_path → reset() doesn't
    crash trying to unlink a None path."""
    tracker = ActionUsageTracker(persist_path=None)
    tracker.merge_compacted([("file__read", 1.0)])
    tracker.reset()
    assert tracker._compacted == {}


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
    tracker = ActionUsageTracker(persist_path=tmp_path / "action_usage.json")
    tracker.merge_compacted([("file__read", 100.0)])

    session = _StubSession(
        history=["turn1", "turn2"],
        history_path=history_path,
        tracker=tracker,
    )
    cmd = REGISTRY.get("clear-history")
    assert cmd is not None
    await cmd.handler(session, "")

    msgs = _drain_outbox(session)
    assert len(msgs) >= 1
    body = msgs[-1].text
    assert "confirm" in body.lower()
    # Data still intact.
    assert session.history == ["turn1", "turn2"]
    assert history_path.exists()
    assert "file__read" in tracker._compacted


@pytest.mark.asyncio
async def test_confirm_clears_history_and_tracker(tmp_path: Path):
    """Tier 2: ``/clear-history confirm`` wipes both."""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps({"role": "user", "content": "hi"}) + "\n",
    )
    tracker = ActionUsageTracker(persist_path=tmp_path / "action_usage.json")
    tracker.merge_compacted([("file__read", 100.0), ("file__write", 101.0)])

    session = _StubSession(
        history=["turn1", "turn2", "turn3"],
        history_path=history_path,
        tracker=tracker,
    )
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(session, "confirm")

    assert session.history == []
    assert not history_path.exists()
    assert tracker._compacted == {}
    msgs = _drain_outbox(session)
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

    tracker = ActionUsageTracker(persist_path=tmp_path / "action_usage.json")
    tracker.merge_compacted([("file__read", 1.0)])

    session = _StubSession(
        history=["x"], history_path=history_path, tracker=tracker,
    )
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(session, "confirm")

    assert events_sentinel.read_text() == "audit-log-entry\n"
    assert wal_sentinel.read_text() == "wal-entry\n"
    assert snapshot_sentinel.read_text() == "{}\n"


@pytest.mark.asyncio
async def test_confirm_when_tracker_missing(tmp_path: Path):
    """Tier 2: session without tracker (= test / minimal session) still
    succeeds — only the history side runs."""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text("h\n")
    session = _StubSession(
        history=["x"], history_path=history_path, tracker=None,
    )
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(session, "confirm")
    assert session.history == []
    assert not history_path.exists()


@pytest.mark.asyncio
async def test_confirm_when_history_already_empty(tmp_path: Path):
    """Tier 2: empty history + no tracker → success message stays
    informative, no crash."""
    session = _StubSession(
        history=[],
        history_path=tmp_path / "nonexistent.jsonl",
        tracker=None,
    )
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(session, "confirm")
    msgs = _drain_outbox(session)
    assert msgs  # something was said
