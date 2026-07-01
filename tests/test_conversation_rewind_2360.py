"""Tier 2: #2360 — time-travel rewinds the CONVERSATION, not just runtime state.

The append-only + branch/generation time-travel machinery existed only for the agent RUNTIME
state; the conversation (``self.history`` / ``history.jsonl``) was outside it, so after ``/rewind``
or fork-switch the LLM still saw post-cut turns. Fix (option 2, faithful to append-only): each turn
is anchored to the WAL seq at append (``meta['wal_seq']``), and the LLM-facing ``build_history``
source (``_active_branch_history``) filters turns to those whose anchor is on the ACTIVE branch
as-of the current GLOBAL cut — reusing the WAL branch-derivation (``is_active_seq``).

Rewind is GLOBAL (``checkout`` jumps the whole world's active cut; the reset-record has no
session_id), so the conversation follows the same global cut the runtime state does — coherent.
``history.jsonl`` stays append-only: futures/other-branches remain in the file, just outside the
visible prefix.

Real seam: real ``Session`` + real ``StateLog`` + the real ``checkout`` reset-record primitive (the
same one ``AgentRegistry.checkout`` appends) driving the real ``is_active_seq`` derivation — no proxy.
The WAL is advanced by a real ``step_completed`` append between turns so anchors differ (a bare
history append does not advance the WAL), mirroring a real turn's WAL activity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import checkout
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session


def _session(tmp_path: Path, name: str, state_log: StateLog) -> Session:
    s = Session(
        agent_name=name, state_log=state_log,
        snapshot_path=tmp_path / f"{name}_snapshot.json",
    )
    s.register_intervention_listener("test")
    return s


def _visible(session: Session) -> list[str]:
    """The user/text turns the LLM actually sees — via the public build_history (the wire output
    RouterLoop consumes), not the private filter, so the assertion rides the real end-to-end path."""
    return [
        m.get("content") for m in session._history_buffer.build_history()
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m.get("content")
    ]


def _dangling(wire: list[dict]) -> set:
    """Tool_call ids and tool_call_ids in the wire payload that have no partner — the provider
    BadRequest set (an assistant tool_calls turn without its results, or a tool result without its
    call). Empty = well-formed."""
    call_ids = {tc.get("id") for m in wire for tc in (m.get("tool_calls") or [])}
    result_ids = {m.get("tool_call_id") for m in wire if m.get("role") == "tool"}
    return call_ids ^ result_ids


async def _tool_cycle(session: Session, state_log: StateLog, tc_id: str) -> tuple[int, int]:
    """Append an assistant tool_calls turn then (after the WAL advances = the tool running) its
    tool result turn. Returns (call_anchor, result_anchor) with result_anchor > call_anchor, so a
    cut can land BETWEEN them (the mid-tool-cycle case)."""
    await state_log.append("step_completed")
    session._append_history(ChatMessage(
        role="assistant", content="",
        tool_calls=[{"id": tc_id, "type": "function", "function": {"name": "f", "arguments": "{}"}}],
    ))
    call_anchor = session.history[-1].meta["wal_seq"]
    await state_log.append("step_completed")  # the tool runs → WAL advances → later anchor
    session._append_history(ChatMessage(role="tool", tool_call_id=tc_id, content="tool output"))
    return call_anchor, session.history[-1].meta["wal_seq"]


async def _turn(session: Session, state_log: StateLog, text: str) -> int:
    """Simulate a real conversational turn: advance the WAL (turn processing), then append the
    turn (stamped with the current WAL seq). Returns the turn's anchor."""
    await state_log.append("step_completed")  # a real WAL kind → advances current_seq
    session._append_history(ChatMessage(role="user", content=text))
    return session.history[-1].meta["wal_seq"]


@pytest.mark.asyncio
async def test_rewind_hides_post_cut_turns_exactly(tmp_path, monkeypatch):
    """Tier 2: rewind to after turn 3 → build_history's source shows EXACTLY turns 1,2,3 (4,5 hidden;
    3 not over-hidden, 6 n/a). Anchor precision verified against the real is_active derivation."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 6)]

    assert _visible(s) == [f"turn {i}" for i in range(1, 6)]  # all visible pre-rewind

    await checkout(state_log, target_seq=anchors[2])  # keep through turn 3

    assert _visible(s) == ["turn 1", "turn 2", "turn 3"], "exact prefix, 4/5 hidden"


@pytest.mark.asyncio
async def test_history_file_stays_append_only(tmp_path, monkeypatch):
    """Tier 2: the hidden (post-cut) turns remain in history.jsonl — the rewind hides them from the
    LLM but never truncates the append-only file (P6 audit + no-discard)."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 6)]
    await checkout(state_log, target_seq=anchors[2])

    raw = s.history_path.read_text()
    assert "turn 4" in raw and "turn 5" in raw, "append-only: hidden turns still on disk"


@pytest.mark.asyncio
async def test_fork_switch_and_no_discard(tmp_path, monkeypatch):
    """Tier 2: fork-switch + no-discard. Rewind to turn 3, add a new branch (6,7) → active shows
    1,2,3,6,7. Switch BACK to the old tip (turn 5, on the now-abandoned branch → checkout revives
    it) → active shows 1..5, and the 6,7 branch survives in the file (no discard)."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 6)]

    await checkout(state_log, target_seq=anchors[2])  # rewind to after turn 3
    await _turn(s, state_log, "turn 6")
    await _turn(s, state_log, "turn 7")
    assert _visible(s) == ["turn 1", "turn 2", "turn 3", "turn 6", "turn 7"]

    await checkout(state_log, target_seq=anchors[4])  # switch back to the old tip (turn 5)
    assert _visible(s) == [f"turn {i}" for i in range(1, 6)], "old future revived"
    raw = s.history_path.read_text()
    assert "turn 6" in raw and "turn 7" in raw, "no discard: the alternate branch survives in the file"


@pytest.mark.asyncio
async def test_global_cut_filters_each_session_own_view(tmp_path, monkeypatch):
    """Tier 2: reframed (1) — rewind is GLOBAL, so a single world-cut filters EACH session's own
    per-session history by that session's own anchors. Two sessions interleave turns; a global
    checkout hides each session's post-cut turns (no session's history escapes the world-cut, and
    each shows exactly its own anchored ≤ cut)."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    a = _session(tmp_path, "alice", state_log)
    b = _session(tmp_path, "bob", state_log)

    await _turn(a, state_log, "A1")
    b1 = await _turn(b, state_log, "B1")
    await _turn(a, state_log, "A2")
    await _turn(b, state_log, "B2")

    await checkout(state_log, target_seq=b1)  # world-cut just after B1 (A1 < B1 < A2 < B2)

    assert _visible(a) == ["A1"], "alice: only her ≤-cut turn"
    assert _visible(b) == ["B1"], "bob: only his ≤-cut turn"


@pytest.mark.asyncio
async def test_unanchored_turns_always_visible(tmp_path, monkeypatch):
    """Tier 2: backward-compat — a turn with no wal_seq anchor (pre-#2360 entry, or no state_log) is
    always visible, so existing history is unaffected and no migration is needed."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)
    a1 = await _turn(s, state_log, "anchored-1")
    s.history.append(ChatMessage(role="user", content="legacy-no-anchor"))  # no wal_seq in meta
    await _turn(s, state_log, "anchored-2")

    await checkout(state_log, target_seq=a1)  # hides anchored-2

    visible = _visible(s)
    assert "legacy-no-anchor" in visible, "unanchored turns stay visible (no migration)"
    assert "anchored-1" in visible and "anchored-2" not in visible


@pytest.mark.asyncio
async def test_mid_tool_cycle_cut_leaves_no_dangling(tmp_path, monkeypatch):
    """Tier 2: #2360 tool-cycle-aware — a GLOBAL cut landing MID-tool-cycle (call anchor ≤ cut <
    result anchor) must NOT emit a dangling tool_calls-without-results (provider BadRequest, the
    #2290 class). The cycle is atomic: governed by the assistant tool_calls anchor, so an active
    call pulls its later-anchored result into the visible payload. Real checkout + real build_history
    wire output."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)
    u1 = await _turn(s, state_log, "please call the tool")
    call_anchor, result_anchor = await _tool_cycle(s, state_log, "tc1")
    assert result_anchor > call_anchor  # the cut can fall strictly between call and result

    # cut lands mid-cycle (at the call anchor; the result's anchor is beyond it)
    await checkout(state_log, target_seq=call_anchor)
    wire = s._history_buffer.build_history()
    assert _dangling(wire) == set(), "mid-cycle cut must leave a well-formed (dangling-free) payload"
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "tc1" for m in wire), \
        "the active call pulls its later-anchored result into the visible payload (cycle atomic)"


@pytest.mark.asyncio
async def test_rewind_record_survives_wal_truncation(tmp_path, monkeypatch):
    """Tier 2: truncate-falsify — abandoned turns stay hidden after WAL truncation below
    the rewind reset-record (CLAUDE.md recovery PR gate: set X → truncate past X's events
    → reconstruct → assert X survives).

    Scenario: rewind hides turns 2 and 3. Many new turns advance applied_seq well past the
    rewind reset-record seq R. A WAL truncation with floor > R would drop the rewind record
    without always_keep_kinds protection, causing _active_branch_history / is_active_seq to
    treat (N, R) as active — abandoned turns 2 and 3 reappear in the LLM context.
    With always_keep_kinds=frozenset({"rewind"}) the reset-record survives and the filter holds.
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)

    # Three turns; rewind to after turn 1 (abandons turns 2 and 3).
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 4)]
    reset_seq = await checkout(state_log, target_seq=anchors[0])

    # New turns on the active branch — these advance applied_seq past the reset record.
    for i in range(4, 10):
        await _turn(s, state_log, f"turn {i}")

    # Truncate with floor > reset_seq so the rewind record is below the floor.
    # always_keep_kinds=frozenset({"rewind"}) (what AgentRegistry.truncate_wal_if_eligible
    # passes) must keep the reset-record despite it being below floor.
    floor = reset_seq + 5  # well past the reset record
    await state_log.truncate_below(floor, always_keep_kinds=frozenset({"rewind"}))
    await state_log.flush()

    # Rewind record must survive in the WAL (directly verifiable).
    surviving_kinds = [e.get("kind") for e in state_log.iter_from(1)]
    assert "rewind" in surviving_kinds, (
        "reset-record must survive WAL truncation so is_active_seq can still classify "
        "abandoned-branch wal_seq values from history.jsonl"
    )

    # Abandoned turns must stay hidden from the LLM.
    visible = _visible(s)
    assert "turn 1" in visible, "turn 1 (before rewind target) must be visible"
    assert "turn 2" not in visible, "abandoned turn must stay hidden after WAL truncation"
    assert "turn 3" not in visible, "abandoned turn must stay hidden after WAL truncation"
    assert "turn 4" in visible, "first post-rewind turn must be visible"


@pytest.mark.asyncio
async def test_cut_before_tool_cycle_hides_whole_cycle_no_dangling(tmp_path, monkeypatch):
    """Tier 2: #2360 tool-cycle-aware — a cut BEFORE the assistant tool_calls turn hides the WHOLE
    cycle (call + result), never a dangling tool result. The other direction of the same atomicity."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, "alice", state_log)
    u1 = await _turn(s, state_log, "hello")
    await _tool_cycle(s, state_log, "tc1")

    await checkout(state_log, target_seq=u1)  # cut before the cycle
    wire = s._history_buffer.build_history()
    assert _dangling(wire) == set(), "cut before the cycle must leave no dangling tool result"
    assert not any(m.get("role") == "tool" for m in wire), "the whole cycle is hidden (call+result)"
