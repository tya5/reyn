"""Tier 2: #4387 Phase B ② — ``Session._active_branch_history`` extends
``self.history`` backward on demand when a rewind's cut point is older than
what's currently in memory.

Phase B ① (#4400) made ``load_history()`` load only a BOUNDED tail at
startup rather than the whole file. ``_active_branch_history`` (#2360's
rewind-visibility filter) only ever looked at ``self.history`` — before
Phase B ①, that was always the complete file, so a rewind past the
compaction watermark still worked by accident. Once ``self.history`` can
be a genuine prefix, a rewind whose cut point references content older
than that prefix would (without this fix) silently show FEWER turns than
the active branch actually contains, or none at all — not a crash, just a
wrong answer, the same "silent, not loud" failure shape architect's
Phase B ② review flagged for ``mcp/server.py``'s position index.

Real ``Session`` + real ``StateLog`` + the real ``checkout`` reset-record
primitive throughout — same seam ``test_conversation_rewind_2360.py``
already established. The "bounded load" precondition is simulated by
truncating ``self.history`` directly after real turns are appended (the
same technique ``test_3380_...``'s sibling tests already use to simulate
"this content is not currently in memory" — see that file's own
``s.history = [...]`` precedent), NOT by re-deriving Phase B ①'s own
tail-selection logic, which is already covered by
``test_4387_bounded_history_hydration.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, REWIND_KIND, checkout
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _session(tmp_path: Path, state_log: StateLog) -> Session:
    s = make_session(
        agent_name="alice", state_log=state_log,
        snapshot_path=tmp_path / "alice_snapshot.json",
    )
    s.register_intervention_listener("test")
    return s


async def _turn(session: Session, state_log: StateLog, text: str) -> int:
    """Mirrors test_conversation_rewind_2360.py's own helper: advance the
    WAL (real turn processing), then append the turn, real coordinates
    throughout."""
    await state_log.append("step_completed")
    session._append_history(ChatMessage(role="user", content=text))
    return session.history[-1].meta["wal_seq"]


def _visible_texts(session: Session) -> "list[str | list[dict]]":
    return [m.content for m in session._active_branch_history()]


@pytest.mark.asyncio
async def test_rewind_past_a_bounded_prefix_extends_backward(tmp_path, monkeypatch):
    """Tier 2: 10 real turns appended, self.history then truncated to only
    the last 3 (simulating a bounded startup load) — a real rewind to
    BEFORE that truncated prefix must still show the correct active turns,
    not an empty/wrong result."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 11)]

    on_disk = s.history_path.read_text()
    for i in range(1, 11):
        assert f"turn {i}" in on_disk, f"sanity: turn {i} must be durable on disk"

    # Simulate a bounded startup load: only the last 3 turns are in memory.
    s.history = s.history[-3:]
    assert [m.content for m in s.history] == ["turn 8", "turn 9", "turn 10"]

    # Rewind to after turn 3 — entirely OLDER than what's currently loaded.
    await checkout(state_log, target_seq=anchors[2], scope=GLOBAL_SCOPE)

    assert _visible_texts(s) == ["turn 1", "turn 2", "turn 3"], (
        "the active branch (turns 1-3) must be visible even though none of "
        "them were in the bounded self.history before the rewind"
    )
    # And the entries that got pulled back in are now genuinely IN self.history
    # (not just returned transiently) — _load_older_entries prepends, doesn't
    # hand back a throwaway view.
    assert [m.content for m in s.history[:3]] == ["turn 1", "turn 2", "turn 3"]


@pytest.mark.asyncio
async def test_no_rewind_never_touches_disk_beyond_what_is_loaded(tmp_path, monkeypatch):
    """Tier 2: accept-side — a session that has NEVER rewound (the
    overwhelming common case) must not trigger any backward extension, even
    with a truncated self.history. earliest_relevant_wal_seq returning None
    is what keeps this cheap for every ordinary turn."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log)
    for i in range(1, 11):
        await _turn(s, state_log, f"turn {i}")

    s.history = s.history[-3:]  # bounded load, but no rewind ever happened

    assert _visible_texts(s) == ["turn 8", "turn 9", "turn 10"], (
        "with no rewind, the bounded prefix IS the correct active view — "
        "no backward extension should have been triggered"
    )
    assert [m.content for m in s.history] == ["turn 8", "turn 9", "turn 10"], (
        "self.history must stay untouched (no extension fired)"
    )


@pytest.mark.asyncio
async def test_extend_backward_survives_wal_truncation_below_the_rewind_record(
    tmp_path, monkeypatch,
):
    """Tier 2: CLAUDE.md recovery-feature PR gate (truncate-falsify).
    ``_load_older_entries``/``earliest_relevant_wal_seq`` (this PR's new
    consumer-level code) read WAL-derived state through the SAME
    ``_abandoned_intervals``/``_rewind_records`` primitive
    ``test_rewind_index_incremental_2939.py`` already truncate-falsifies at
    the ``StateLog`` level — this test proves the NEW consumer built on top
    of it (``Session._active_branch_history``'s extend-backward wiring)
    doesn't break under the production truncation call
    (``always_keep_kinds=frozenset({REWIND_KIND})``, which protects the
    reset-record — the documented correct usage, per
    ``StateLog.truncate_below``'s own docstring on why ``REWIND_KIND`` must
    survive: dropping it "causes abandoned conversation turns to reappear
    in the LLM context").

    Set X (a rewind hiding turns 4-10) → truncate the WAL below the
    reset-record's own seq, WITH the record protected → reconstruct
    (``_active_branch_history``, which now has to extend self.history
    backward past the bounded 3-turn prefix) → X survives (turns 4-10 stay
    hidden, turns 1-3 correctly visible from the extended prefix).
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 11)]
    s.history = s.history[-3:]  # bounded load: only turns 8-10 in memory

    await checkout(state_log, target_seq=anchors[2], scope=GLOBAL_SCOPE)  # hide turns 4-10
    # Grow the WAL further so there's real content past the rewind to
    # truncate BELOW (truncate_below's min_keep_seq must be > the rewind
    # record's own seq for this to be a meaningful drop, not a no-op).
    for i in range(11, 14):
        await _turn(s, state_log, f"turn {i}")

    assert _visible_texts(s) == [f"turn {i}" for i in (1, 2, 3, 11, 12, 13)], (
        "sanity: turns 4-10 abandoned, 1-3 + 11-13 active, before truncation"
    )

    kept_kinds_before = {e.get("kind") for e in state_log.iter_from(0)}
    assert REWIND_KIND in kept_kinds_before, "sanity: a reset-record exists to protect"

    # Truncate below a point PAST the reset-record's own seq, protecting it —
    # the real production call (registry.py's own retention path).
    await state_log.truncate_below(anchors[-1], always_keep_kinds=frozenset({REWIND_KIND}))
    await state_log.flush()

    surviving_kinds = [e.get("kind") for e in state_log.iter_from(0)]
    assert REWIND_KIND in surviving_kinds, (
        "test premise: the reset-record must have survived truncation, "
        "otherwise this isn't testing what it claims to"
    )

    # Reconstruct: a FRESH bounded self.history again (as if the process
    # just restarted and load_history() gave only the newest turns), then
    # ask _active_branch_history to answer from the (now-truncated) WAL.
    # (self.history already grew via the extend-backward call above, so
    # re-truncate explicitly by content rather than assuming a slice.)
    s.history = [m for m in s.history if m.content in ("turn 11", "turn 12", "turn 13")]
    assert [m.content for m in s.history] == ["turn 11", "turn 12", "turn 13"], (
        "sanity: the reconstructed bounded prefix is exactly the newest 3 turns"
    )

    assert _visible_texts(s) == ["turn 1", "turn 2", "turn 3", "turn 11", "turn 12", "turn 13"], (
        "X survives truncation: turns 4-10 must STILL be hidden and 1-3 "
        "STILL correctly recovered by extend-backward, using the "
        "truncated-but-REWIND_KIND-protected WAL"
    )


@pytest.mark.asyncio
async def test_rewind_scoped_to_this_session_alone_still_extends_backward(
    tmp_path, monkeypatch,
):
    """Tier 2: #5789/#5795 regression -- architect caught this direction
    error in review (the fix's own round-1 draft passed `GLOBAL_SCOPE` to
    `earliest_relevant_wal_seq`, reasoning it was a safe, merely-accidental
    conservative bound; the reasoning had the direction backwards). A
    session using ONLY its own session-scoped rewind (the common case
    since #5785 made `/rewind` default to session-local) has ZERO
    global-scope reset-records -- `earliest_relevant_wal_seq(scope=
    GLOBAL_SCOPE)` would see an EMPTY `abandoned` set and return `None`,
    skipping the backward-load-extension loop ENTIRELY, even though this
    session's own scoped rewind genuinely hid older turns that the
    properly-scoped `is_active` filter (run right after) needs the
    extended prefix to correctly classify. Scoping the bound to THIS
    session's own `(agent_name, session_id)` -- the same scope the
    sibling `is_active` filter already uses -- fixes it: same interval
    set, so the bound is conservative by construction."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 11)]
    s.history = s.history[-3:]  # bounded load: only turns 8-10 in memory

    # SESSION-SCOPED rewind only -- zero global-scope reset-records exist.
    await checkout(
        state_log, target_seq=anchors[2], scope=(s.agent_name, s.session_id),
    )  # hide turns 4-10, scoped to this session alone

    assert _visible_texts(s) == ["turn 1", "turn 2", "turn 3"], (
        "the active branch (turns 1-3) must be visible even though none of "
        "them were in the bounded self.history before the rewind, AND the "
        "rewind was scoped (not global) -- a regression here would show "
        "['turn 8', 'turn 9', 'turn 10'] (the stale bounded prefix, "
        "backward extension never triggered)"
    )
    assert [m.content for m in s.history[:3]] == ["turn 1", "turn 2", "turn 3"]
