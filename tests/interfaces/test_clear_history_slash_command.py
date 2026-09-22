"""Tier 2: ``/clear-history`` slash command (REGISTRY dispatch, on-disk wipe).

User dogfood 2026-05-25 asked for a slash that resets history + action_usage
to initial state without touching anything else. This file pins the
history half of that request end-to-end through the real slash REGISTRY:

1. Two-step confirmation (= bare ``/clear-history`` warns, requires
   ``confirm`` to actually wipe).
2. ``confirm`` form clears ``session.history`` (in-memory) AND removes the
   WHOLE ``session.history_dir`` (on-disk — #6240/#6248: every segment,
   active AND sealed, not just one file).
3. The slash does NOT touch the ``events/`` directory, the WAL, or the
   per-agent snapshot.

#6240/#6248 (architect ruling, PR #6257 issuecomment-5773552909): the
actual disk wipe now lives on ``Session.clear_history()``, a PUBLISHED
operation (not a private residue member the handler reaches across) —
this file drives it through a REAL ``Session`` (``make_session``), not a
hand-rolled stub, since the whole point of this file is the real
on-disk behavior that operation owns. ``tests/runtime/test_6240_6248_
history_segments.py`` covers ``Session.clear_history`` in isolation
(the 3 architect-required witnesses); this file covers the SLASH-LEVEL
contract — confirmation flow, reply text, untouched siblings — end to
end through a real session.

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

from pathlib import Path

import pytest

from reyn.interfaces.slash import REGISTRY
from reyn.runtime.chat_message import ChatMessage
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx


def _seeded_session(tmp_path: Path, *, n_turns: int = 3):
    """A real ``Session`` (real ``history/`` segment dir on disk) with
    *n_turns* real appended turns."""
    session = make_session(agent_name="alice", workspace_base_dir=tmp_path)
    for i in range(n_turns):
        session._append_history(ChatMessage(role="user", content=f"turn {i}"))
    return session


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
    session = _seeded_session(tmp_path, n_turns=2)
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    assert cmd is not None
    await cmd.handler(ctx, "")

    msgs = ctx.transport.displayed
    assert len(msgs) >= 1
    body = msgs[-1].text
    assert "confirm" in body.lower()
    # Data still intact.
    assert [m.content for m in session.history] == ["turn 0", "turn 1"]
    assert session.history_path.is_file()


@pytest.mark.asyncio
async def test_confirm_clears_history(tmp_path: Path):
    """Tier 2: ``/clear-history confirm`` wipes history in-memory and on
    disk — the WHOLE ``history/`` directory, not just the active file."""
    session = _seeded_session(tmp_path, n_turns=3)
    history_dir = session.history_dir
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")

    assert session.history == []
    # A fresh (empty) active segment exists — the dir itself is not gone.
    assert history_dir.is_dir()
    assert session.history_path.read_text() == ""
    msgs = ctx.transport.displayed
    success_lines = [m.text for m in msgs if "Cleared" in m.text]
    assert success_lines, f"expected a confirmation; got {[m.text for m in msgs]}"
    assert "3" in success_lines[0]


@pytest.mark.asyncio
async def test_confirm_removes_sealed_segments_too(tmp_path: Path):
    """Tier 2: #6248's own named defect — a pre-#6248 ``/clear`` removed
    only the active file, leaving a SEALED segment behind; a next-restart
    hydration would then read it right back in, so ``/clear`` would have
    looked like it worked and silently reverted. This is the deny-side
    witness for the fix, exercised at the slash-command level."""
    session = _seeded_session(tmp_path, n_turns=1)
    sealed = session.history_dir / "history-000000000001-000000000008-n.jsonl"
    sealed.write_text('{"role": "user", "seq": 1, "content": "sealed"}\n')

    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")

    assert not sealed.exists(), "a sealed segment must not survive /clear-history confirm"


@pytest.mark.asyncio
async def test_confirm_preserves_unrelated_files(tmp_path: Path):
    """Tier 2: the slash MUST NOT touch events/, the WAL, or snapshots —
    those live elsewhere on disk. Place a sentinel file in each and
    verify it survives."""
    session = _seeded_session(tmp_path, n_turns=1)

    # Sibling sentinels — these stand in for events/ / state/ etc.
    events_sentinel = tmp_path / "events.jsonl"
    events_sentinel.write_text("audit-log-entry\n")
    wal_sentinel = tmp_path / "wal.jsonl"
    wal_sentinel.write_text("wal-entry\n")
    snapshot_sentinel = tmp_path / "snapshot.json"
    snapshot_sentinel.write_text("{}\n")

    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")

    assert events_sentinel.read_text() == "audit-log-entry\n"
    assert wal_sentinel.read_text() == "wal-entry\n"
    assert snapshot_sentinel.read_text() == "{}\n"


@pytest.mark.asyncio
async def test_confirm_when_history_already_empty(tmp_path: Path):
    """Tier 2: empty history → success message stays informative, no crash."""
    session = make_session(agent_name="bob", workspace_base_dir=tmp_path)
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")
    msgs = ctx.transport.displayed
    assert msgs  # something was said


@pytest.mark.asyncio
async def test_an_append_after_clear_lands_in_a_fresh_segment(tmp_path: Path) -> None:
    """Tier 2: slash-level sibling of ``Session.clear_history``'s own
    witness ① — after ``/clear-history confirm``, a NEW turn through the
    real session append path lands in a genuinely FRESH active segment
    (its own on-disk content is only the post-clear turn — the pre-clear
    turns are gone, not silently still sitting ahead of it)."""
    session = _seeded_session(tmp_path, n_turns=5)
    ctx = slash_ctx(session)
    cmd = REGISTRY.get("clear-history")
    await cmd.handler(ctx, "confirm")

    session._append_history(ChatMessage(role="user", content="post-clear"))
    on_disk = [ln for ln in session.history_path.read_text().splitlines() if ln.strip()]
    assert [ln for ln in on_disk if '"content": "post-clear"' in ln], (
        "the post-clear append must be present in the active segment"
    )
    assert not any('"content": "turn ' in ln for ln in on_disk), (
        "a pre-clear turn must not still be present in the active segment"
    )
