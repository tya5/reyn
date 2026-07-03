"""Tier 2: /clear-history slash — _format_currently_line helper + handler paths.

`_format_currently_line` is a pure introspection helper; `clear_history_cmd`
has three behavioural paths: (1) no "confirm" token → warning, (2) confirm
with clearable state → clear + success reply, (3) confirm but nothing to clear.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.clear_history import (
    _format_currently_line,
    clear_history_cmd,
)
from reyn.runtime.outbox import OutboxMessage

# ── stubs ──────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(
        self,
        *,
        history: list | None = None,
        history_path=None,
        tracker=None,
    ) -> None:
        self.history = history
        self.history_path = history_path
        self._action_usage_tracker = tracker
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


class _FakeTracker:
    def __init__(self, n: int) -> None:
        self._n = n
        self.reset_called = False

    def __len__(self) -> int:
        return self._n

    def reset(self) -> None:
        self.reset_called = True


# ── _format_currently_line pure helper ────────────────────────────────────


def test_format_currently_no_attrs_returns_empty() -> None:
    """Tier 2: session with no history/tracker attrs → empty string."""
    session = object()  # has neither .history nor ._action_usage_tracker
    assert _format_currently_line(session) == ""


def test_format_currently_history_only() -> None:
    """Tier 2: only history wired → 'Currently: N history turns.'"""
    session = _FakeSession(history=["a", "b", "c"])
    out = _format_currently_line(session)
    assert out.startswith("Currently:")
    assert "3 history turns" in out


def test_format_currently_history_singular() -> None:
    """Tier 2: single history turn uses singular 'turn' not 'turns'."""
    session = _FakeSession(history=["only"])
    out = _format_currently_line(session)
    assert "1 history turn" in out
    assert "turns" not in out


def test_format_currently_tracker_only() -> None:
    """Tier 2: only tracker wired → 'Currently: N tracked tools.'"""
    session = _FakeSession(tracker=_FakeTracker(5))
    out = _format_currently_line(session)
    assert "5 tracked tools" in out


def test_format_currently_both_attrs() -> None:
    """Tier 2: history + tracker → both counts in the 'Currently:' line."""
    session = _FakeSession(history=["x", "y"], tracker=_FakeTracker(3))
    out = _format_currently_line(session)
    assert "Currently:" in out
    assert "2 history turns" in out
    assert "3 tracked tools" in out


# ── clear_history_cmd handler paths ───────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_history_no_confirm_sends_warning_not_error() -> None:
    """Tier 2: /clear-history without 'confirm' sends a warning, not an error."""
    session = _FakeSession()
    await clear_history_cmd(session, "")
    assert not session.error_text(), "expected no error for missing confirm"
    # Must include some reply (the "type /clear-history confirm" warning)
    assert session.reply_text(), "expected at least one system reply"


@pytest.mark.asyncio
async def test_clear_history_no_confirm_warns_about_confirm_token() -> None:
    """Tier 2: the warning tells the user to type /clear-history confirm."""
    session = _FakeSession()
    await clear_history_cmd(session, "not_confirm")
    text = session.reply_text()
    assert "confirm" in text.lower()


@pytest.mark.asyncio
async def test_clear_history_confirm_clears_history_list() -> None:
    """Tier 2: /clear-history confirm mutates the in-memory history list to empty."""
    history: list = ["turn1", "turn2"]
    session = _FakeSession(history=history)
    await clear_history_cmd(session, "confirm")
    assert history == []


@pytest.mark.asyncio
async def test_clear_history_confirm_calls_tracker_reset() -> None:
    """Tier 2: /clear-history confirm calls tracker.reset()."""
    tracker = _FakeTracker(4)
    session = _FakeSession(tracker=tracker)
    await clear_history_cmd(session, "confirm")
    assert tracker.reset_called


@pytest.mark.asyncio
async def test_clear_history_confirm_sends_success_reply() -> None:
    """Tier 2: /clear-history confirm sends a system (success) reply, not an error."""
    session = _FakeSession(history=["x"], tracker=_FakeTracker(2))
    await clear_history_cmd(session, "confirm")
    kinds = [m.kind for m in session._outbox]
    assert "error" not in kinds
    assert "system" in kinds


@pytest.mark.asyncio
async def test_clear_history_confirm_nothing_to_clear() -> None:
    """Tier 2: /clear-history confirm with empty history + no tracker → nothing-to-clear reply."""
    session = _FakeSession()  # no history, no tracker
    await clear_history_cmd(session, "confirm")
    text = session.reply_text()
    assert "nothing" in text.lower() or "empty" in text.lower()


class _FailingPath:
    """Stub Path that raises OSError on unlink — simulates a write-protected file."""

    def unlink(self, missing_ok: bool = False) -> None:
        raise OSError("permission denied")


def test_clear_alias_registered() -> None:
    """Tier 2: /clear is a registered alias for /clear-history so CC users don't hit
    'unknown command /clear'."""
    from reyn.interfaces.slash import REGISTRY
    cmd = REGISTRY.get("clear")
    assert cmd is not None, "/clear must resolve via the registry"
    assert cmd.name == "clear-history", "/clear must resolve to /clear-history handler"


@pytest.mark.asyncio
async def test_clear_history_disk_fail_leaves_memory_intact() -> None:
    """Tier 2: when history_path.unlink() fails, in-memory history must NOT be cleared.

    Falsification (old code): the old ordering cleared memory BEFORE the disk
    deletion attempt.  Under the old code this test would fail because history
    would be empty even though the disk write failed — a partial-clear that
    causes history to silently reload on next startup.
    """
    history: list = ["turn1", "turn2"]
    session = _FakeSession(history=history, history_path=_FailingPath())
    await clear_history_cmd(session, "confirm")

    # Memory must be unchanged — disk failed so nothing was committed.
    assert history == ["turn1", "turn2"], (
        "in-memory history was cleared even though disk deletion failed; "
        "old code cleared memory first then returned on OSError, leaving "
        "history empty in-memory but history.jsonl intact on disk — "
        "next startup would silently reload old turns"
    )
    # Must have emitted an error reply (not a success).
    assert session.error_text(), "expected an error reply when unlink raises OSError"
