"""Tier 2: OS invariant -- #5759 stage 2, history.jsonl GC end-to-end.

Real `AgentRegistry` + real `StateLog` + real on-disk `history.jsonl`
(no mocks). Drives the actual wired entry point
(`AgentRegistry._prune_generations_below`, the SAME throttled pass the
WAL truncation + generation prune already use -- no new trigger) rather
than probing the 4 private helpers it composes directly, per this
codebase's own established convention for this class of GC test
(`test_registry_rewind_to.py`, `test_2259_pr1b_agent_identity_truncation_
bug.py`, and `test_agent_archive_delete_1954.py` all call
`_prune_generations_below` directly -- an accepted semi-public GC seam in
this test suite, not a Tier-4 private-state probe).

A turn is GC-eligible only when ALL of: (1) below the WAL's real,
truncated floor, (2) outside the startup-hydration margin, (3) inside
SOME recorded fold's [covers_from, covers_through] range (union across
every summary the file has ever recorded, not just the latest one --
the architect correction this file's own `test_older_folds_range_is_
also_collected_not_just_the_latest_summary` exists to pin).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import _HISTORY_HYDRATE_MIN_LINES


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


async def _advance_floor_past(reg: AgentRegistry, seq: int) -> int:
    """Physically truncate the WAL so `_oldest_kept_seq()` reports a real
    floor strictly above *seq* (drop everything <= seq), and wait for the
    fire-and-forget rewrite worker to drain before returning."""
    assert reg.state_log is not None
    await reg.state_log.truncate_below(seq + 1)
    await reg.state_log.flush()
    oldest = reg._oldest_kept_seq()
    assert oldest is not None and oldest > seq, (oldest, seq)
    return oldest


def _write_history(tmp_path: Path, name: str, lines: list[dict]) -> Path:
    path = tmp_path / ".reyn" / "agents" / name / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def _turn(seq: int, role: str = "user") -> dict:
    return {"role": role, "seq": seq, "text": f"t{seq}"}


def _summary(seq: int, *, covers_from: "int | None", covers_through: int) -> dict:
    meta: dict = {"covers_through_seq": covers_through}
    if covers_from is not None:
        meta["covers_from_seq"] = covers_from
    return {"role": "summary", "seq": seq, "content": "summary", "meta": meta}


def _seqs(path: Path) -> list[int]:
    return [
        json.loads(line)["seq"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _pad_past_margin(start: int) -> list[dict]:
    """Enough trailing turns that the margin boundary sits at/after
    *start* -- keeps the margin condition out of the way for tests that
    aren't specifically about it."""
    return [_turn(s) for s in range(start, start + _HISTORY_HYDRATE_MIN_LINES + 5)]


def _raw_range(start: int, end: int) -> list[dict]:
    """The RAW turns a fold covers, as they actually sit on disk --
    history.jsonl is append-only, so folding a range never removes the
    raw entries from the file; they must be genuinely present for a GC
    test to exercise real removal rather than asserting over content
    that was never written in the first place."""
    return [_turn(s) for s in range(start, end + 1)]


@pytest.mark.asyncio
async def test_folded_middle_range_is_gcd_head_and_tail_survive(tmp_path):
    """Tier 2: strip-falsifier target. seq 1-3 (head, pre-fold, never
    folded), 4-6 (folded + below floor + below margin -> GC-eligible),
    7+ (post-fold, unfolded, padded past the margin) -- only the middle
    range is removed."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "alpha", "x")

    lines = [_turn(1), _turn(2), _turn(3)]
    lines += _raw_range(4, 6)
    lines.append(_summary(20, covers_from=4, covers_through=6))
    lines += _pad_past_margin(7)
    path = _write_history(tmp_path, "alpha", lines)

    before = _seqs(path)
    assert 4 in before and 5 in before and 6 in before  # genuinely present pre-GC

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    seqs = _seqs(path)
    assert seqs[:3] == [1, 2, 3]
    assert 4 not in seqs and 5 not in seqs and 6 not in seqs
    assert 20 in seqs  # the summary line itself always survives
    assert 7 in seqs


@pytest.mark.asyncio
async def test_older_folds_range_is_also_collected_not_just_the_latest_summary(tmp_path):
    """Tier 2: architect's required 6th acceptance point. Two folds have
    happened (seq 1-3 folded by an EARLIER summary, seq 4-6 by a LATER
    one) -- the earlier fold's own range must ALSO be GC-eligible, not
    just the latest summary's [covers_from, covers_through]. A GC that
    only reads the latest summary (copying the 2 existing
    `compaction_coverage_from_summary` consumers verbatim) would leave
    seq 1-3 behind -- this test goes RED under that implementation."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "beta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "beta", "x")

    lines = _raw_range(1, 3)
    lines.append(_summary(10, covers_from=1, covers_through=3))  # earlier fold
    lines += _raw_range(4, 6)
    lines.append(_summary(20, covers_from=4, covers_through=6))  # later fold
    lines += _pad_past_margin(7)
    path = _write_history(tmp_path, "beta", lines)

    before = _seqs(path)
    for s in (1, 2, 3, 4, 5, 6):
        assert s in before  # genuinely present pre-GC

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    seqs = _seqs(path)
    for s in (1, 2, 3, 4, 5, 6):
        assert s not in seqs, f"seq {s} should have been GC'd (older fold's own range)"
    assert 10 in seqs and 20 in seqs  # both summaries survive
    assert 7 in seqs


@pytest.mark.asyncio
async def test_nothing_removed_before_the_floor_advances(tmp_path):
    """Tier 2: time axis -- a folded, margin-eligible range is NOT GC'd
    while the WAL floor has not yet advanced past it (0 lines removed)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "gamma")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "gamma", "x")

    lines = [_turn(1), _turn(2), _turn(3)]
    lines += _raw_range(4, 6)
    lines.append(_summary(20, covers_from=4, covers_through=6))
    lines += _pad_past_margin(7)
    path = _write_history(tmp_path, "gamma", lines)

    # No truncate_below call -- floor never advances past the fold.
    before = _seqs(path)
    await reg._prune_generations_below(1)
    after = _seqs(path)

    assert before == after


@pytest.mark.asyncio
async def test_startup_hydration_margin_is_never_gcd_even_if_folded_and_below_floor(tmp_path):
    """Tier 2: a range that is folded AND below the WAL floor is STILL
    protected if it falls inside the startup-hydration margin -- the
    margin (condition (4)) is not overridden by the other 2 conditions."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "delta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "delta", "x")

    # Fold covers 1..6, but only a handful of lines total -- the whole
    # file sits inside read_history_tail's own BOF fallback margin.
    lines = [_turn(1), _turn(2), _turn(3)]
    lines += _raw_range(4, 6)
    lines.append(_summary(6, covers_from=4, covers_through=6))
    lines.append(_turn(7))
    path = _write_history(tmp_path, "delta", lines)

    await _advance_floor_past(reg, 6)
    before = _seqs(path)
    await reg._prune_generations_below(1)
    after = _seqs(path)

    assert before == after  # margin protects the whole (short) file


@pytest.mark.asyncio
async def test_fail_closed_on_summary_missing_covers_from_seq(tmp_path):
    """Tier 2: manually-verified point (2)'s public-path witness --
    lead-coder-30's explicit requirement. A pre-#5765 summary with
    `covers_through_seq` but no `covers_from_seq` contributes NO range
    (fail-closed: never guess, never hide) -- nothing is removed even
    though the WAL floor has advanced past covers_through."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "epsilon")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "epsilon", "x")

    lines = [_turn(1), _turn(2), _turn(3)]
    lines += _raw_range(4, 6)
    lines.append(_summary(20, covers_from=None, covers_through=6))
    lines += _pad_past_margin(7)
    path = _write_history(tmp_path, "epsilon", lines)

    await _advance_floor_past(reg, 6)
    before = _seqs(path)
    await reg._prune_generations_below(1)
    after = _seqs(path)

    assert before == after


@pytest.mark.asyncio
async def test_missing_history_file_is_a_no_op(tmp_path):
    """Tier 2: manually-verified point (1)'s public-path witness -- an
    agent with no history.jsonl at all (never sent a message) does not
    crash the GC pass."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "zeta")
    log = reg.state_log
    for _ in range(5):
        await _put(log, "zeta", "x")
    await _advance_floor_past(reg, 3)

    await reg._prune_generations_below(1)  # must not raise

    assert not (tmp_path / ".reyn" / "agents" / "zeta" / "history.jsonl").exists()


@pytest.mark.asyncio
async def test_gc_frees_space_even_when_rewind_was_never_used(tmp_path):
    """Tier 2: the key differentiator from the rejected "discard-only"
    earlier design -- GC runs (and frees real bytes) purely from the
    throttled truncation pass, with zero `/rewind`/`checkout` calls ever
    made on this registry."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "eta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "eta", "x")

    lines = [_turn(1), _turn(2), _turn(3)]
    lines += _raw_range(4, 6)
    lines.append(_summary(20, covers_from=4, covers_through=6))
    lines += _pad_past_margin(7)
    path = _write_history(tmp_path, "eta", lines)
    size_before = path.stat().st_size

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)  # no checkout()/rewind_to() call anywhere

    assert path.stat().st_size < size_before


# Disclosure (CLAUDE.md six-questions #4): manually-verified point (3) --
# "a summary further back than the margin does not move the boundary
# early" -- has no independent test in THIS file. It is a property of
# `read_history_tail` itself, already covered directly by
# `tests/runtime/test_4676_history_tail_reader_toctou.py` and by
# `read_history_tail`'s own module-docstring-referenced test suite; the
# GC wiring here only ever CALLS that function unchanged, so re-asserting
# its internal stop condition through the GC's own public path would be
# the "same expression on both sides" shape CLAUDE.md's test-review
# question 2 rejects.
