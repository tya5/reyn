"""Tier 2: #5192 — ``StateLog`` closes the same TOCTOU race
``ApprovalLedger``/``BudgetLedger`` had, in the third band it lives in
(crash-recovery/WAL, not permission or cost/bounding).

Architect ruling (issuecomment-5384637352, relayed by lead-coder) fixed
ALL THREE files (this one, ``approval_ledger.py``, ``budget.py``) in one PR,
with 5 shared acceptance criteria:
  ① the old check-then-write guard's identifier is COMPLETELY absent from
     the module — not merely unused (grep 0 hits; one file standing in for
     all three does not satisfy this)
  ② two real, separate writers appending concurrently to the same WAL file
     → line count == record count (no lost/corrupted entries)
  ③ WAL-specific truncate-falsify (the CLAUDE.md hard rule itself): set
     state with a WAL containing an embedded blank line → truncate the WAL
     past that point → reconstruct → assert derived state survives
  ④ the reader's tolerant blank-line skip is documented as an INVARIANT of
     the on-disk format (not incidental defensive code) at all 3 read sites
     in this module — see ``_parse_wal_line``'s, ``iter_from``'s, and
     ``_do_truncate``'s own docstrings
  ⑤ #5194's already-merged self-describing diagnostic stays (that fix lives
     in ``tests/security/test_5153_approval_ledger.py``, not here)

③'s WAL-specific angle (architect, not present in ①②'s approval_ledger/
budget precedent): truncation uses ``seq``, not byte offset, as its floor —
whether an embedded blank line could disturb that floor was explicitly
UNMEASURED before this test.
"""
from __future__ import annotations

import inspect
import json
import multiprocessing
from pathlib import Path

import pytest

import reyn.core.events.state_log as state_log_module
from reyn.core.events.state_log import StateLog

# Mirrors tests/security/test_5153_approval_ledger.py's own explicit-spawn
# fix (PR #5170 CI red on Linux+multi-threaded-parent) -- never the platform
# default, so this file's own correctness never depends on which platform
# happens to run it.
_mp = multiprocessing.get_context("spawn")


def test_no_lead_newline_guard_remains_in_the_module() -> None:
    """Tier 2: #5192 acceptance ① — the old TOCTOU-vulnerable
    check-then-write guard's identifier must be completely removed from
    ``state_log.py``, not merely unused. RED if the guard (or a
    reintroduced equivalent under the same name) comes back."""
    source = inspect.getsource(state_log_module)
    assert "_needs_lead_newline" not in source, (
        "the old TOCTOU-vulnerable guard's identifier must be completely "
        "removed -- see _do_wal_write's own docstring for why"
    )


def _worker_append_many(path_str: str, tag: str, n: int) -> None:
    """Module-level (picklable) worker: append *n* records with a distinct
    *tag* field -- run in a SEPARATE OS process, its own event loop."""
    import asyncio

    async def _run() -> None:
        sl = StateLog(Path(path_str))
        for i in range(n):
            await sl.append("inbox_put", text=f"{tag}-{i}")
        await sl.aclose()

    asyncio.run(_run())


def test_two_real_processes_appending_concurrently_line_count_equals_record_count(
    tmp_path: Path,
) -> None:
    """Tier 2: #5192 acceptance ② — two SEPARATE OS processes, each with its
    own event loop, appending to the SAME WAL file concurrently: every
    append must survive (no lost lines) and every surviving line must parse
    (no interleaved corruption from the removed guard's race). RED if the
    self-terminating write is reverted to a check-then-write guard."""
    wal_path = tmp_path / "wal.jsonl"
    n = 150

    p1 = _mp.Process(target=_worker_append_many, args=(str(wal_path), "p1", n))
    p2 = _mp.Process(target=_worker_append_many, args=(str(wal_path), "p2", n))
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    raw_content = wal_path.read_text(encoding="utf-8")
    lines = raw_content.splitlines()
    expected_total = 2 * n
    assert len(lines) == expected_total, (
        f"expected {expected_total} lines from two concurrent writers, got "
        f"{len(lines)} raw lines: {lines!r}"
    )
    records = [json.loads(line) for line in lines]  # every line must parse
    assert len(records) == expected_total
    tags = {r["text"].rsplit("-", 1)[0] for r in records}
    assert tags == {"p1", "p2"}
    # Each process's own texts must appear exactly n times, with none lost
    # or duplicated -- the property the removed guard's race could corrupt
    # (an interleaved/torn write, not seq assignment: each StateLog instance
    # owns its own in-memory counter, so cross-process seq UNIQUENESS is a
    # separate, out-of-scope property this test does not claim).
    for tag in ("p1", "p2"):
        texts = [r["text"] for r in records if r["text"].startswith(f"{tag}-")]
        assert sorted(texts) == sorted(f"{tag}-{i}" for i in range(n)), (
            f"{tag}'s {n} appends must all survive exactly once, got: {texts!r}"
        )


@pytest.mark.asyncio
async def test_truncate_falsify_survives_an_embedded_blank_line(tmp_path: Path) -> None:
    """Tier 2: #5192 acceptance ③ — the CLAUDE.md hard rule itself
    ("Recovery-feature PRs need a truncate-falsify test: set X → truncate
    the WAL past X's events → reconstruct → assert X survives").

    Sets up a WAL with a real durable entry, hand-inserts an embedded blank
    line (the on-disk shape a benign write race can produce -- see
    ``_parse_wal_line``'s docstring), appends more entries past it, then
    truncates below a seq that is AFTER the blank line. Reconstruction
    (a fresh StateLog's ``iter_from``) must see exactly the kept entries --
    the blank line must not have thrown off the seq-based truncation floor
    (architect's explicitly-unmeasured concern) or corrupted anything past
    it. RED if the blank line disturbs truncation's seq accounting or if
    the reconstructed state loses an entry that should have survived."""
    wal_path = tmp_path / "wal.jsonl"
    sl = StateLog(wal_path)
    seq1 = await sl.append("inbox_put", text="kept-below-floor-but-highest")
    seq2 = await sl.append("inbox_put", text="dropped")
    assert (seq1, seq2) == (1, 2)
    await sl.aclose()

    # Hand-insert an embedded blank line between durable entries -- the
    # shape a benign concurrent-write race produces (two adjacent "\n"s),
    # now tolerated by construction rather than prevented by a guard.
    with wal_path.open("a", encoding="utf-8") as f:
        f.write("\n")

    sl = StateLog(wal_path)  # re-opens; counter recovers past the blank line
    seq3 = await sl.append("inbox_put", text="also-dropped")
    seq4 = await sl.append("inbox_put", text="survives-truncation")
    assert (seq3, seq4) == (3, 4)

    # Truncate below seq4: everything strictly below seq4 is dropped, EXCEPT
    # the highest seq present is always kept (the counter watermark) -- so
    # seq2/seq3 are dropped, seq4 survives. seq1 is below the highest-seq
    # exemption's reach only because it isn't the highest -- also dropped.
    await sl.truncate_below(seq4)
    await sl.flush()
    stats = sl.last_truncate_stats
    assert stats["dropped"] == 3, f"expected 3 dropped entries, got: {stats!r}"
    assert stats["kept"] == 1, f"expected 1 kept entry, got: {stats!r}"
    await sl.aclose()

    # Reconstruction: a FRESH StateLog on the truncated file must recover
    # correctly -- the seq counter watermark must not have been corrupted
    # by the blank line that sat in the pre-truncation file.
    recovered = StateLog(wal_path)
    entries = list(recovered.iter_from(1))
    assert [e["seq"] for e in entries] == [4], (
        f"derived state after truncate+reconstruct must contain exactly "
        f"seq 4 (the only survivor), got: {entries!r}"
    )
    assert entries[0]["text"] == "survives-truncation"
    # The next append must continue from seq 5, not re-issue 1-4 -- proof
    # the counter watermark itself survived the blank-line-adjacent rewrite.
    seq5 = await recovered.append("inbox_put", text="next-after-recovery")
    assert seq5 == 5, (
        f"counter must resume at 5 after truncate+reconstruct, got {seq5} "
        f"-- a reset here would re-issue a seq already used before the blank "
        f"line, corrupting the audit trail's monotonic ordering"
    )
    await recovered.aclose()


@pytest.mark.asyncio
async def test_iter_from_tolerates_an_embedded_blank_line_as_an_invariant(
    tmp_path: Path,
) -> None:
    """Tier 2: #5192 acceptance ④ — the blank-line skip is exercised as a
    real behavior (not merely asserted-present in a docstring): a WAL with
    an embedded blank line between two durable entries must still yield
    both entries via ``iter_from``, in seq order, with nothing lost or
    duplicated. RED if the skip is ever narrowed to "only a trailing blank
    line" or removed."""
    wal_path = tmp_path / "wal.jsonl"
    sl = StateLog(wal_path)
    await sl.append("inbox_put", text="before-blank")
    await sl.aclose()

    with wal_path.open("a", encoding="utf-8") as f:
        f.write("\n")  # embedded blank line -- the tolerated on-disk shape

    sl = StateLog(wal_path)
    await sl.append("inbox_put", text="after-blank")

    entries = list(sl.iter_from(1))
    assert [e["text"] for e in entries] == ["before-blank", "after-blank"], (
        f"a blank line between two durable entries must not lose or "
        f"duplicate either one: {entries!r}"
    )
    await sl.aclose()
