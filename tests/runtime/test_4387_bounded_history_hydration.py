"""Tier 2: #4387 Phase B ① — ``Session.load_history()``'s bounded startup
read, and the two pure helpers it's built on (``read_history_tail`` /
``read_last_line`` in ``reyn.runtime.history_tail_reader``).

Owner's real environment: ``history.jsonl`` reached 500MB, and every
restart re-expanded the whole file into ``self.history`` (never shrinks,
never bounded). This bounds the STARTUP read: everything since the latest
compaction watermark, plus a minimum scrollback floor
(``_HISTORY_HYDRATE_MIN_LINES``), falling back to the original full-file
read when the file's last entry has no real ``seq`` (pre-#3704 data).

Real ``Session`` + real ``history.jsonl`` files throughout — no mocks. The
pure-function tests write raw JSON lines directly (this module's own
concern: line framing, not ``ChatMessage`` shape) and go through
``Session._append_history`` wherever the production write format matters.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.history_tail_reader import read_history_tail, read_last_line
from reyn.runtime.session import _HISTORY_HYDRATE_MIN_LINES
from tests._support.agent_session import make_session

# ---------------------------------------------------------------------------
# read_last_line / read_history_tail — pure functions, plain files
# ---------------------------------------------------------------------------


def test_read_last_line_missing_and_empty_file(tmp_path: Path) -> None:
    """Tier 2: missing or empty file → None, never an exception."""
    assert read_last_line(tmp_path / "nope.jsonl") is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert read_last_line(empty) is None


def test_read_last_line_returns_the_final_complete_line(tmp_path: Path) -> None:
    """Tier 2: the last line, trailing newline stripped."""
    p = tmp_path / "h.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    assert read_last_line(p) == '{"a": 3}'


def test_read_history_tail_without_any_summary_reads_the_whole_file(
    tmp_path: Path,
) -> None:
    """Tier 2: with NO summary anywhere in the file, the bounded read cannot
    safely stop at ``min_lines`` alone — it can't tell "no compaction has
    ever run" (everything is uncompacted, so watermark-bounded consumers
    need ALL of it) from "the summary is just further back" short of BOF.
    Deliberately conservative: reads to BOF, same as the pre-#4387 full
    read, for this specific (rare, once a session is old enough to have
    compacted at least once) shape."""
    p = tmp_path / "h.jsonl"
    total = _HISTORY_HYDRATE_MIN_LINES + 50
    p.write_text("\n".join(json.dumps({"i": i}) for i in range(total)) + "\n")

    tail = read_history_tail(p, min_lines=_HISTORY_HYDRATE_MIN_LINES)

    assert [json.loads(line)["i"] for line in tail] == list(range(total))


def test_read_history_tail_reads_past_the_floor_to_include_the_latest_summary(
    tmp_path: Path,
) -> None:
    """Tier 2: a summary further back than ``min_lines`` extends the read —
    the compaction-watermark invariant (everything since the latest
    compaction) always wins over the floor, never the other way around."""
    p = tmp_path / "h.jsonl"
    lines = [json.dumps({"i": i, "role": "user"}) for i in range(20)]
    lines[5] = json.dumps({"i": 5, "role": "summary", "covers_through_seq": 5})
    p.write_text("\n".join(lines) + "\n")

    # min_lines=3 is smaller than "distance back to the summary" (14 lines
    # from EOF) — the read must still reach the summary, not stop at 3.
    tail = read_history_tail(p, min_lines=3)

    parsed = [json.loads(line) for line in tail]
    assert parsed[0]["role"] == "summary" and parsed[0]["i"] == 5
    # Content equality (not just a count) — the summary line itself PLUS
    # everything after it, nothing more and nothing less.
    assert parsed == [json.loads(ln) for ln in lines[5:]]


def test_read_history_tail_short_file_returns_everything(tmp_path: Path) -> None:
    """Tier 2: a file with fewer than ``min_lines`` total lines is read in
    full — the same shape ``test_3137_...``'s small fixture relies on."""
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps({"i": i}) for i in range(5)) + "\n")

    tail = read_history_tail(p, min_lines=_HISTORY_HYDRATE_MIN_LINES)

    assert [json.loads(line)["i"] for line in tail] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Session.load_history() — the two real paths, end to end
# ---------------------------------------------------------------------------


def test_load_history_fast_path_bounds_a_long_compacted_session(tmp_path) -> None:
    """Tier 2: a session that HAS compacted (the realistic shape once a
    session is old/large enough to matter — compaction keeps re-firing as
    the token budget refills) with turns after the latest summary exceeding
    the hydrate floor — cold start loads only since that summary, not the
    whole file, and ``_next_seq`` is still correct (derived from the last
    entry's real seq, not a full scan)."""
    s1 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    # A pre-summary region (would NOT be loaded by the bounded read) ...
    for i in range(50):
        s1._append_history(ChatMessage(role="user", content=f"old turn {i}"))
    summary_seq = s1.history[-1].seq
    s1._append_history(ChatMessage(
        role="summary", content="summarised",
        meta={"structured": {}, "covers_through_seq": summary_seq},
    ))
    # ... then more turns than the hydrate floor AFTER that summary.
    post_total = _HISTORY_HYDRATE_MIN_LINES + 30
    for i in range(post_total):
        s1._append_history(ChatMessage(role="user", content=f"turn {i}"))
    total_on_disk = 50 + 1 + post_total
    on_disk = [ln for ln in s1.history_path.read_text().splitlines() if ln.strip()]
    assert len(on_disk) == total_on_disk, "sanity: every entry is durable on disk"

    s2 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    s2.load_history()

    assert len(s2.history) < total_on_disk, (
        "the bounded startup read must not have loaded the whole file"
    )
    assert s2.history[0].role == "summary", (
        "the loaded tail must start at (include) the latest summary — the "
        "watermark every bounded consumer depends on"
    )
    assert [m.content for m in s2.history[1:]] == [f"turn {i}" for i in range(post_total)], (
        "everything after the summary must be present, in order"
    )
    assert "old turn 0" not in [m.content for m in s2.history], (
        "the pre-summary region must NOT be loaded by the bounded read"
    )
    # _next_seq correctness: an appended turn continues the coordinate space.
    s2._append_history(ChatMessage(role="user", content="new turn"))
    assert s2.history[-1].seq == total_on_disk + 1


def test_load_history_falls_back_to_full_scan_when_last_entry_has_no_seq(
    tmp_path,
) -> None:
    """Tier 2: a file whose LAST written entry has ``seq == 0`` (simulating
    data that predates #3704, or any file the fast-path peek can't trust)
    falls back to the original full forward read — every entry loaded,
    ``_next_seq`` derived from a full scan, not just the peeked line."""
    s1 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    for i in range(_HISTORY_HYDRATE_MIN_LINES + 10):
        s1._append_history(ChatMessage(role="user", content=f"turn {i}"))
    # Append one more line by hand with seq stamped to 0 — the exact legacy
    # shape #3704's own fixture uses, at the position that matters for the
    # fast-path's peek (the file's last line).
    with s1.history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "legacy tail", "seq": 0}) + "\n")

    s2 = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    s2.load_history()

    total_on_disk = _HISTORY_HYDRATE_MIN_LINES + 11
    assert len(s2.history) == total_on_disk, (
        "the fallback path must load the ENTIRE file, not just a bounded tail"
    )
    assert s2.history[-1].content == "legacy tail"
    assert s2.history[-1].seq == 0
    # _next_seq must still be derived correctly (max real seq + 1, ignoring
    # the trailing seq==0 entry) — the same #3704 invariant, now reached via
    # the fallback branch rather than the only branch.
    s2._append_history(ChatMessage(role="user", content="new turn"))
    assert s2.history[-1].seq == _HISTORY_HYDRATE_MIN_LINES + 10 + 1
