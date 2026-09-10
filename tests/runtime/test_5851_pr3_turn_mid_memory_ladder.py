"""Tier 2: #5851 PR-3 — the turn-mid memory mini-ladder's own judge,
``Session._check_turn_mid_memory_ladder`` (①' fold now, excluding the
in-flight turn's own messages → ②' cache release [PR-1's shared step] →
③' stop at this boundary → host critical → ④ process exit).

Only the LADDER'S OWN logic is pinned here — see
``tests/llm/test_5851_pr3_turn_mid_memory_seam.py`` for the router_loop.py
iteration-boundary wiring (ordering, distinct recording).

Real ``Session`` (``tests._support.agent_session.make_session``), a real
injected ``ProcessMemoryGuard.reader`` (never a Mock — matches
``test_5939_pr2_memory_ladder_and_halt_remedies.py``'s own established
seam), a real ``CompactionController``/engine driven through an actual
fold (only ``litellm.acompletion`` monkeypatched to a scripted summary,
matching ``test_4472_compaction_reads_durable_store.py``'s own established
discipline). ``os._exit`` is intercepted (raising a sentinel instead) since
the real call would kill the test worker.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from reyn.config import CompactionConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.process_memory import ProcessMemoryGuard
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


def _fake_reader_sequence(values: "list[int | None]"):
    """A real, injectable callable — never a Mock — returning each of
    *values* in order, then repeating the last one."""
    it = iter(values)
    last = values[-1] if values else None

    def _read() -> "int | None":
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return _read


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(tmp_path: Path, *, guard: "ProcessMemoryGuard | None" = None) -> Session:
    return make_session(
        agent_name="pr3-agent",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path / "ws",
        process_memory_guard=guard,
        compaction_config=CompactionConfig(use_chars4_estimate=True, section_caps_spec_tokens=0),
    )


def _shrink_model_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches ``test_4472_compaction_reads_durable_store.py``'s own
    established discipline: without this, the real/fallback budget is
    large enough that head+tail protection alone covers every short pad
    turn this file uses, leaving zero unprotected candidates — a real
    fold would never actually run, and every test below would be
    vacuously green."""
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 2800)


def _script_compaction_llm(monkeypatch: pytest.MonkeyPatch) -> "list[str]":
    """Records every prompt the compactor's own LLM call actually saw (so
    a test can assert what was, or was not, folded in) and replies with a
    trivial, always-valid summary."""
    prompt_calls: "list[str]" = []

    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        prompt_calls.append(json.dumps(messages))
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({
                    "new_turn_seqs": [], "topic_arc": "compacted",
                    "decisions": [], "pending": [],
                    "session_user_facts": [], "artifacts_referenced": [],
                })),
            )],
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    return prompt_calls


class _ExitCalled(Exception):
    """Sentinel raised by the intercepted ``os._exit`` — never a real
    process termination inside a test."""


def _intercept_exit(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    calls: "list[int]" = []

    def _fake_exit(code: int) -> None:
        calls.append(code)
        raise _ExitCalled()

    import os
    monkeypatch.setattr(os, "_exit", _fake_exit)
    return calls


def _append_turn(s: Session, text: str) -> int:
    s._append_history(ChatMessage(role="user", content=text, ts=_now()))
    return s.history[-1].seq


# ── inert default ─────────────────────────────────────────────────────


def test_ladder_is_inert_when_not_enforced(tmp_path: Path):
    """Tier 2: matches PR-2's own steady-state ladder inert-default
    contract — ``enforce=False``/``cap_bytes=None`` means 0 lines run,
    ``guard.read()`` never even called."""
    calls: "list[int]" = []

    def _read() -> int:
        calls.append(1)
        return 999_999_999_999

    guard = ProcessMemoryGuard(reader=_read, cap_bytes=None, enforce=False)
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    result = asyncio.run(s._check_turn_mid_memory_ladder())

    assert result is None
    assert calls == [], "an inert (not enforced) guard must never be read at all"
    assert events == []


# ── ④ host-critical, independent of this session's own cap ─────────────


def test_host_critical_alone_exits_immediately_without_folding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: host-critical (architect ruling, #5939: the HOST's
    operability, not this process's own size alone) exits immediately —
    no fold, no release — even though THIS session's own footprint is
    comfortably under its own cap."""
    prompt_calls = _script_compaction_llm(monkeypatch)
    exits = _intercept_exit(monkeypatch)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([10]),  # WAY under cap=1_000_000
        cap_bytes=1_000_000, enforce=True, metric="phys_footprint",
        host_swap_critical_bytes=500, host_swap_reader=lambda: 100,  # <= critical
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    async def _drive() -> None:
        try:
            await s._check_turn_mid_memory_ladder()
        finally:
            # The intercepted os._exit raises SYNCHRONOUSLY, before the
            # background event-dispatch consumer task ever gets a turn —
            # settle here (inside the SAME coroutine, before the
            # exception propagates out of asyncio.run) so the read below
            # is not racing that consumer.
            await settle(s)

    with pytest.raises(_ExitCalled):
        asyncio.run(_drive())

    assert exits == [1]
    assert prompt_calls == [], "host-critical-alone must never trigger a fold"
    assert not any(e.type == "process_memory_forensics" for e in events), (
        "host-critical-alone must never trigger a cache release either"
    )
    assert any(e.type == "session_halted" for e in events)
    assert s.halted_reason == "process_memory"


# ── ①' fold excludes the in-flight turn's own messages ──────────────────


def test_fold_now_never_covers_the_in_flight_turns_own_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: architect's own required condition — do NOT rely on the
    tail token budget. Every message belonging to the in-flight turn
    stays uncovered by the fold, even though a real compaction genuinely
    ran (sanity: something strictly OLDER was folded)."""
    _shrink_model_budget(monkeypatch)
    prompt_calls = _script_compaction_llm(monkeypatch)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([2_000, 10]),  # over cap, then fold fixes it
        cap_bytes=1_000, enforce=True, metric="phys_footprint",
    )
    s = _session(tmp_path, guard=guard)

    pad = "x" * 4000
    for i in range(1, 6):
        _append_turn(s, f"older-turn-{i} {pad}")
    # Freeze the boundary exactly like _run_router_loop does at turn
    # entry, then append this "in-flight" turn's own messages. Captured
    # into a LOCAL here (rather than re-read off the private field at
    # assertion time below) so the eventual assertion compares against
    # this snapshot, not private session state.
    turn_start_seq = s._next_seq
    s._current_turn_start_seq = turn_start_seq
    in_flight_seqs = [_append_turn(s, f"in-flight-turn-{i} {pad}") for i in range(1, 4)]

    result = asyncio.run(s._check_turn_mid_memory_ladder())

    assert result is None, "fold alone must have brought the footprint back under cap"
    assert prompt_calls, "sanity: a real compaction must have run"
    summary = s._latest_summary()
    assert summary is not None
    covers_through_seq = int((summary.meta or {}).get("covers_through_seq", 0))
    assert covers_through_seq < turn_start_seq, (
        f"the fold covered through seq {covers_through_seq}, which reaches into "
        f"the in-flight turn (started at seq {turn_start_seq}) — the model's own "
        "just-written reasoning from THIS turn must never be folded out from "
        "under it mid-turn"
    )
    for seq in in_flight_seqs:
        assert seq > covers_through_seq, (
            f"in-flight message seq={seq} must be strictly past what the fold covered"
        )


# ── ②'/③' escalation when the fold alone is not enough ──────────────────


def test_ladder_escalates_to_release_then_stops_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: fold genuinely ran but did not bring the footprint back
    under cap, release genuinely ran but ALSO did not — the ladder
    reaches ③' and returns the stop reason, emitting
    ``turn_stopped_memory`` with the measured fields."""
    _shrink_model_budget(monkeypatch)
    _script_compaction_llm(monkeypatch)
    guard = ProcessMemoryGuard(
        # 1: initial (over) 2: post-fold (still over)
        # 3: release's own before-read (irrelevant) 4: release's own after-read (still over)
        reader=_fake_reader_sequence([2_000, 1_500, 1_500, 1_200]),
        cap_bytes=1_000, enforce=True, metric="phys_footprint",
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)
    pad = "x" * 4000
    for i in range(1, 4):
        _append_turn(s, f"older-turn-{i} {pad}")
    s._current_turn_start_seq = s._next_seq
    _append_turn(s, f"in-flight {pad}")

    result = asyncio.run(s._check_turn_mid_memory_ladder())

    assert result == "turn_stopped_memory"
    assert any(e.type == "process_memory_forensics" for e in events), (
        "②' cache release must have run"
    )
    (stop_ev,) = [e for e in events if e.type == "turn_stopped_memory"]
    assert stop_ev.data["footprint_bytes"] == 1_200
    assert stop_ev.data["cap_bytes"] == 1_000
    # ③' is a safety net, not a halt — the session itself stays healthy.
    assert s.halted_reason is None
