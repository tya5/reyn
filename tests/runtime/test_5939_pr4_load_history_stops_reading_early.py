"""Tier 2: #5939 PR-4 — `Session.load_history()`'s own fast path wired
to `read_history_tail_with_byte_budget`, end to end. Real `Session` +
real `history.jsonl` files throughout — mirrors
`test_4387_bounded_history_hydration.py`'s own established pattern.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.chat import HistoryResidentConfig
from reyn.runtime.chat_message import ChatMessage, ResidentBytes
from reyn.runtime.session import _HISTORY_HYDRATE_MIN_LINES
from tests._support.agent_session import make_session
from tests._support.events import collect_events


def test_small_resident_budget_with_no_summary_sets_the_unsafe_flag(tmp_path: Path) -> None:
    """Tier 2: accept — many turns, NO summary anywhere, and a
    `history_resident.max_bytes` small enough that the backward hydrate
    read crosses it well before reaching BOF. The next session's own
    `load_history()` stops early, sets the public `history_hydration_
    stopped_reading_early_unsafe` flag, and emits the observable
    audit-event — never a silent truncation.

    Strip: comment out the `if truncated_unsafe: self._history_
    hydration_stopped_reading_early_unsafe = True; ...` block in
    `_load_history_body` — this goes RED (flag stays False, performed
    during review)."""
    s1 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    total = _HISTORY_HYDRATE_MIN_LINES + 50
    for i in range(total):
        s1._append_history(ChatMessage(role="user", content=f"turn {i}" + "x" * 500))
    on_disk = [ln for ln in s1.history_path.read_text().splitlines() if ln.strip()]
    assert len(on_disk) == total  # sanity: no summary anywhere on disk

    s2 = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path,
        history_resident_config=HistoryResidentConfig(max_bytes=ResidentBytes(2_000)),
    )
    events = collect_events(s2)

    s2.load_history()

    assert s2.history_hydration_stopped_reading_early_unsafe is True
    assert "turn 0x" not in [m.content for m in s2.history], (
        "the read must not have reached the OLDEST entry -- it stopped early"
    )
    assert s2.history, "partial content must still be loaded (it is real, usable content)"

    (unsafe_event,) = [
        e for e in events if e.type == "history_hydration_stopped_reading_early_unsafe"
    ]
    assert unsafe_event.data["lines_loaded"] == len(s2.history)
    assert unsafe_event.data["max_bytes"] == 2_000


def test_summary_within_budget_does_not_set_the_unsafe_flag(tmp_path: Path) -> None:
    """Tier 2: deny — a summary exists NEAR THE TAIL (the realistic
    "already compacted" shape) and is found before the byte budget is
    exhausted -> the flag stays False, matching the SAME safe shape
    `read_history_tail`'s own plain min_lines-after-summary cut already
    allows."""
    s1 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    for i in range(20):
        s1._append_history(ChatMessage(role="user", content=f"old turn {i}"))
    summary_seq = s1.history[-1].seq
    s1._append_history(ChatMessage(
        role="summary", content="summarised",
        meta={"structured": {}, "covers_through_seq": summary_seq},
    ))
    for i in range(10):
        s1._append_history(ChatMessage(role="user", content=f"new turn {i}"))

    s2 = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path,
        history_resident_config=HistoryResidentConfig(max_bytes=ResidentBytes(1_000_000)),
    )
    s2.load_history()

    assert s2.history_hydration_stopped_reading_early_unsafe is False
    assert any(m.role == "summary" for m in s2.history)


def test_generous_budget_never_triggers_the_early_stop(tmp_path: Path) -> None:
    """Tier 2: FP gate — a byte budget far larger than the whole file
    never triggers early-stop at all; the OLDEST entry is still present,
    same as #4387's own pre-PR-4 fast path."""
    s1 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    for i in range(20):
        s1._append_history(ChatMessage(role="user", content=f"turn {i}"))

    s2 = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path,
        history_resident_config=HistoryResidentConfig(max_bytes=ResidentBytes(1_000_000_000)),
    )
    s2.load_history()

    assert s2.history_hydration_stopped_reading_early_unsafe is False
    assert "turn 0" in [m.content for m in s2.history], (
        "with a generous budget, even the OLDEST entry must be reached"
    )
