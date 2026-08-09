"""Tier 1/2: #3868 PR-1 — EventLog's folded ``ingested`` derived state.

``present``'s "was this ref already read this session?" question used to be
answered by scanning ``EventLog._events`` (source.py's old ``compute_ingested``)
— O(session length) on every present call. #3868 PR-1 moves the fold into
``EventLog.emit()`` itself: a dict keyed on the read's ``path``, updated
incrementally as reads happen, so ``EventLog.compute_ingested`` becomes an
O(1) lookup.

The growth-class claim (architect's design, #3868): this is NOT bounded to a
fixed size — no ``deque(maxlen=N)``. A cap would make an old path's entry
silently vanish, and re-presenting that ref would then report ``none``
instead of ``full`` — a false "you haven't read this" for a ref that WAS
fully read (compute_ingested is an audit annotation, not a permission mode,
but a wrong one is still a lie). What changed is the CLASS of growth:

    old   O(every event ever emitted)
    new   O(distinct paths ever read)

Still unbounded in principle (read enough distinct paths, this grows
forever) — the bound is on WORK done (each entry costs a real file read +
permission gate), not on how much an agent can emit. The tests below make
this falsifiable rather than asserted:

  1. Non-read events, however many, leave the derived state at size 0.
  2. Repeated reads of the SAME path leave it at size 1 (not N).

Real ``EventLog`` throughout (no mocks) — ``ingested_path_count`` is the
public witness (CLAUDE.md: no private-state assertions), not a read of the
private ``_ingested`` dict.

A fourth test, ``test_removing_the_fold_breaks_both_size_claims``, was
removed (#3908, architect co-vet on #3868 comment 5229519141): it built a
hand-written ``_NoFoldEventLog`` subclass whose ``emit()`` skips the fold,
then asserted that stub's own behavior back — a transcribed implementation
(CLAUDE.md's six-question review, Q1 "none": reyn's own trivia; Q2 "yes":
same expression both sides), and a real strip of the production fold block
in ``events.py`` leaves it GREEN (it asserts what the hand-written stub
does, not what production does with the fold removed) — the opposite of
what "STRIP-FALSIFY" in its name claimed. The real strip-falsify for tests
1 and 2 above is executed and recorded in the PR body, not encoded as a
test: deleting the fold block in ``EventLog.emit()`` turns both RED
(confirmed via a positive control — ``grep -c '_ingested\\[path\\]'
src/reyn/core/events/events.py`` → 0 after the strip), then the tree is
restored.
"""
from __future__ import annotations

from reyn.core.events.events import EventLog


def _emit_read(log: EventLog, path: str, *, truncated: bool = False) -> None:
    log.emit("tool_executed", op="read_file", path=path, truncated=truncated)


def test_non_read_events_leave_ingested_state_at_zero() -> None:
    """Tier 1: emitting events that are not a read (any type, any op) never
    grows the ingested derived state — it tracks read PATHS, not events."""
    log = EventLog()
    for i in range(50):
        log.emit("tool_executed", op="write_file", path=f"/tmp/f{i}.txt")
        log.emit("llm_request", model="x")
        log.emit("tool_executed", op="read_file")  # no path — must not count

    assert log.ingested_path_count == 0, (
        f"non-read events (or a pathless read) grew the derived state: "
        f"{log.ingested_path_count}"
    )
    assert log.compute_ingested("/tmp/f0.txt", "/tmp/f0.txt") == "none"


def test_repeated_reads_of_the_same_path_stay_at_size_one() -> None:
    """Tier 1: N reads of the SAME path grow the derived state by 0 after the
    first — this is what makes the growth class O(distinct paths), not
    O(events): a chatty agent re-reading one file 1000 times costs the same
    memory as reading it once."""
    log = EventLog()
    for _ in range(200):
        _emit_read(log, "/repo/README.md")

    assert log.ingested_path_count == 1, (
        f"200 reads of ONE path grew the derived state to "
        f"{log.ingested_path_count}, not 1 — growth is tracking events, not paths"
    )
    assert log.compute_ingested("/repo/README.md", "/repo/README.md") == "full"


def test_sticky_full_survives_a_later_truncated_read() -> None:
    """Tier 2: a full read followed by a truncated read on the SAME path
    stays ``full`` — the operator (or the earlier read) already has the
    whole thing; a later truncated read of the same path must not read as
    a downgrade to partial."""
    log = EventLog()
    _emit_read(log, "/repo/big.txt", truncated=False)
    _emit_read(log, "/repo/big.txt", truncated=True)

    assert log.compute_ingested("/repo/big.txt", "/repo/big.txt") == "full"
    assert log.ingested_path_count == 1
