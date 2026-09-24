"""Tier 2: /clear-history slash — _format_currently_line helper + handler paths.

`_format_currently_line` is a pure introspection helper; `clear_history_cmd`
has three behavioural paths: (1) no "confirm" token → warning, (2) confirm
with clearable state → clear + success reply, (3) confirm but nothing to clear.

#4552: this file used to also pin an ``ActionUsageTracker.reset()`` call the
handler made on confirm (the "hot list" tracker's own reset, distinct from
history clearing) — removed with the hot-list feature (owner directive:
discarded). ``_format_currently_line`` and the handler now only ever read
``session.history``.

#6240/#6248 (architect ruling, PR #6257 issuecomment-5773552909): the
actual disk wipe moved to a PUBLISHED ``Session.clear_history()``
operation — this file's own concern is the HANDLER's routing/reply-text
paths, so ``_FakeSession.clear_history()`` here is a minimal stand-in
(count + list-clear, or raise) rather than real segment-directory
semantics; the real on-disk behavior (and its 3 architect-required
witnesses) lives in ``tests/runtime/test_6240_6248_history_segments.py``,
and the slash-level end-to-end disk contract in
``tests/interfaces/test_clear_history_slash_command.py`` (real
``Session``, not a fake).
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.clear_history import (
    _format_currently_line,
    clear_history_cmd,
)
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx

# ── stubs ──────────────────────────────────────────────────────────────────


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.
    """
    return slash_ctx(session, recorder=session._outbox)


class _FakeSession:
    def __init__(
        self,
        *,
        history: list | None = None,
        fail_disk: bool = False,
    ) -> None:
        self.history = history
        self.history_dir = "<fake history dir>"
        self._fail_disk = fail_disk
        self._outbox: list[OutboxMessage] = []

    async def clear_history(self) -> int:
        """Minimal stand-in for ``Session.clear_history`` — same contract
        (raise before touching ``history`` on a disk failure; otherwise
        clear ``history`` and return the pre-clear count), no real
        filesystem involved. ``async def`` since #6240 ③ follow-up (PR
        #6260 comment 5808618887) made the real method ``async`` too
        (it awaits its own history-durability-worker flush first) —
        this fake carries no worker to flush, so the body stays
        synchronous logic under an ``async def`` signature, matching the
        real method's call-site contract without reintroducing one."""
        if self._fail_disk:
            raise OSError("permission denied")
        n_before = len(self.history) if isinstance(self.history, list) else 0
        if isinstance(self.history, list):
            self.history.clear()
        return n_before

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


# ── _format_currently_line pure helper ────────────────────────────────────


def test_format_currently_no_attrs_returns_empty() -> None:
    """Tier 2: session with no history attr → empty string."""
    session = object()  # has no .history
    assert _format_currently_line(session) == ""


def test_format_currently_history_only() -> None:
    """Tier 2: history wired → 'Currently: N history turns.'"""
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


# ── clear_history_cmd handler paths ───────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_history_no_confirm_sends_warning_not_error() -> None:
    """Tier 2: /clear-history without 'confirm' sends a warning, not an error."""
    session = _FakeSession()
    await clear_history_cmd(_ctx(session), "")
    assert not session.error_text(), "expected no error for missing confirm"
    # Must include some reply (the "type /clear-history confirm" warning)
    assert session.reply_text(), "expected at least one system reply"


@pytest.mark.asyncio
async def test_clear_history_no_confirm_warns_about_confirm_token() -> None:
    """Tier 2: the warning tells the user to type /clear-history confirm."""
    session = _FakeSession()
    await clear_history_cmd(_ctx(session), "not_confirm")
    text = session.reply_text()
    assert "confirm" in text.lower()


@pytest.mark.asyncio
async def test_clear_history_confirm_clears_history_list() -> None:
    """Tier 2: /clear-history confirm mutates the in-memory history list to empty."""
    history: list = ["turn1", "turn2"]
    session = _FakeSession(history=history)
    await clear_history_cmd(_ctx(session), "confirm")
    assert history == []


@pytest.mark.asyncio
async def test_clear_history_confirm_sends_success_reply() -> None:
    """Tier 2: /clear-history confirm sends a system (success) reply, not an error."""
    session = _FakeSession(history=["x"])
    await clear_history_cmd(_ctx(session), "confirm")
    kinds = [m.kind for m in session._outbox]
    assert "error" not in kinds
    assert "system" in kinds


@pytest.mark.asyncio
async def test_clear_history_confirm_nothing_to_clear() -> None:
    """Tier 2: /clear-history confirm with empty history → nothing-to-clear reply."""
    session = _FakeSession()  # no history
    await clear_history_cmd(_ctx(session), "confirm")
    text = session.reply_text()
    assert "nothing" in text.lower() or "empty" in text.lower()


def test_clear_alias_registered() -> None:
    """Tier 2: /clear is a registered alias for /clear-history so CC users don't hit
    'unknown command /clear'."""
    from reyn.interfaces.slash import REGISTRY
    cmd = REGISTRY.get("clear")
    assert cmd is not None, "/clear must resolve via the registry"
    assert cmd.name == "clear-history", "/clear must resolve to /clear-history handler"


@pytest.mark.asyncio
async def test_clear_history_disk_fail_leaves_memory_intact() -> None:
    """Tier 2: when the ``history/`` directory deletion fails, in-memory
    history must NOT be cleared.

    Falsification (old code): the old ordering cleared memory BEFORE the disk
    deletion attempt.  Under the old code this test would fail because history
    would be empty even though the disk write failed — a partial-clear that
    causes history to silently reload on next startup.
    """
    history: list = ["turn1", "turn2"]
    session = _FakeSession(history=history, fail_disk=True)
    await clear_history_cmd(_ctx(session), "confirm")

    # Memory must be unchanged — disk failed so nothing was committed.
    assert history == ["turn1", "turn2"], (
        "in-memory history was cleared even though disk deletion failed; "
        "old code cleared memory first then returned on OSError, leaving "
        "history empty in-memory but the history/ dir intact on disk — "
        "next startup would silently reload old turns"
    )
    # Must have emitted an error reply (not a success).
    assert session.error_text(), "expected an error reply when the directory removal raises OSError"
