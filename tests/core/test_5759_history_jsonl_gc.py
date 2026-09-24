"""Tier 2: OS invariant -- #5759 stage 2 / #6240 / #6248, history segment
GC end-to-end.

#6240/#6248 (architect design, issue #6248 comments 5772696821 +
5772712797 -- the second is the CURRENT ruling): ``history.jsonl`` is now
a per-session ``history/`` directory of SEGMENTS -- one ACTIVE segment
(``history.jsonl`` inside it) plus zero or more SEALED segments
(``history-<min_seq>-<max_seq>-<s|n>.jsonl``). GC now decides ENTIRELY
from a sealed segment's own FILENAME (never reading its content) whether
to ``unlink`` it outright:

  ① its own ``has_summary`` flag is False (a summary-carrying segment is
     NEVER unlinked, matching the pre-#6248 "a summary line is never
     dropped" invariant, now at whole-segment granularity)
  ② its ``max_seq`` is below BOTH the WAL's real, truncated floor AND the
     startup-hydration margin
  ③ its WHOLE ``[min_seq, max_seq]`` range falls inside the UNION of
     every recorded fold's ``[covers_from, covers_through]`` range
     (folds can accumulate across MULTIPLE non-overlapping summaries)

Real ``AgentRegistry`` + real ``StateLog`` + real on-disk segment files
throughout (no mocks). Drives the actual wired entry point
(``AgentRegistry._prune_generations_below``, the SAME throttled pass the
WAL truncation + generation prune already use -- no new trigger) rather
than probing the private helpers it composes directly, per this
codebase's own established convention for this class of GC test.

Fixtures write segment files DIRECTLY (bypassing real appends/sealing,
which ``test_6240_6248_history_segments.py`` covers on its own) --
matching this file's own pre-#6248 precedent of writing raw JSON lines
straight to disk for GC-specific tests, now shaped as segment files
instead of one flat file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.history_segments import (
    active_segment_path,
    history_dir_for,
    sealed_segment_name,
)
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import _HISTORY_HYDRATE_MIN_LINES
from tests._support.agent_session import make_session


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


def _turn(seq: int, role: str = "user") -> dict:
    return {"role": role, "seq": seq, "text": f"t{seq}"}


def _summary(seq: int, *, covers_from: "int | None", covers_through: int) -> dict:
    meta: dict = {"covers_through_seq": covers_through}
    if covers_from is not None:
        meta["covers_from_seq"] = covers_from
    return {"role": "summary", "seq": seq, "content": "summary", "meta": meta}


def _write_sealed(
    tmp_path: Path, name: str, lines: list[dict], *, has_summary: bool,
) -> Path:
    """Write *lines* as one SEALED segment under ``<name>``'s ``history/``
    dir, named from their own min/max seq (matching what the real
    appender's own seal would have produced)."""
    hist_dir = history_dir_for(tmp_path / ".reyn" / "agents" / name)
    hist_dir.mkdir(parents=True, exist_ok=True)
    seqs = [e["seq"] for e in lines]
    seg_name = sealed_segment_name(min(seqs), max(seqs), has_summary=has_summary)
    path = hist_dir / seg_name
    with path.open("w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def _write_active(tmp_path: Path, name: str, lines: list[dict]) -> Path:
    """Write *lines* as the ACTIVE segment (never a GC candidate)."""
    hist_dir = history_dir_for(tmp_path / ".reyn" / "agents" / name)
    hist_dir.mkdir(parents=True, exist_ok=True)
    path = active_segment_path(hist_dir)
    with path.open("w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def _record_gen(reg: AgentRegistry, name: str, seq: int) -> None:
    snap = AgentSnapshot.empty(name)
    snap.applied_seq = seq
    reg._store_for(name).record(snap)


def _pad_past_margin(start: int) -> list[dict]:
    """Enough trailing turns that the margin boundary sits at/after
    *start* -- keeps the margin condition out of the way for tests that
    aren't specifically about it. Written to the ACTIVE segment (the
    realistic shape: recent, still-growing content stays active)."""
    return [_turn(s) for s in range(start, start + _HISTORY_HYDRATE_MIN_LINES + 5)]


@pytest.mark.asyncio
async def test_a_non_summary_segment_wholly_covered_by_a_fold_is_unlinked(tmp_path):
    """Tier 2: strip-falsifier target. A sealed segment covering seq 4-6,
    with NO summary of its own, wholly covered by a fold recorded in a
    SEPARATE sealed summary-carrying segment, is unlinked outright once
    the WAL floor + margin both clear it."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "alpha", "x")

    fold_seg = _write_sealed(tmp_path, "alpha", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    _write_sealed(
        tmp_path, "alpha", [_summary(20, covers_from=4, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "alpha", _pad_past_margin(7))
    assert fold_seg.is_file()

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert not fold_seg.exists(), "fully-covered, below-floor, below-margin segment must be unlinked"


@pytest.mark.asyncio
async def test_a_non_summary_segment_not_covered_by_any_fold_survives(tmp_path):
    """Tier 2: deny-side sibling -- seq 1-3 sit in their own sealed
    segment, below the floor and outside the margin, but NO fold covers
    them (condition ③ fails) -- the segment must survive whole."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "beta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "beta", "x")

    head_seg = _write_sealed(tmp_path, "beta", [_turn(1), _turn(2), _turn(3)], has_summary=False)
    fold_seg = _write_sealed(tmp_path, "beta", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    _write_sealed(
        tmp_path, "beta", [_summary(20, covers_from=4, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "beta", _pad_past_margin(7))

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert head_seg.exists(), "an UNfolded segment must never be unlinked"
    assert not fold_seg.exists(), "the folded sibling segment IS still eligible"


@pytest.mark.asyncio
async def test_older_folds_range_is_also_collected_not_just_the_latest_summary(tmp_path):
    """Tier 2: architect's required acceptance point, at segment
    granularity. Two folds (an EARLIER summary covering seq 1-3, a LATER
    one covering seq 4-6) each live in their OWN summary-carrying sealed
    segment -- the earlier fold's own range must ALSO be honoured, not
    just the latest summary's. A GC reading only the latest summary would
    leave the seq-1-3 segment behind."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "gamma")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "gamma", "x")

    early_fold = _write_sealed(tmp_path, "gamma", [_turn(1), _turn(2), _turn(3)], has_summary=False)
    late_fold = _write_sealed(tmp_path, "gamma", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    _write_sealed(
        tmp_path, "gamma", [_summary(10, covers_from=1, covers_through=3)], has_summary=True,
    )
    _write_sealed(
        tmp_path, "gamma", [_summary(20, covers_from=4, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "gamma", _pad_past_margin(7))

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert not early_fold.exists(), "the EARLIER fold's own segment must also be GC-eligible"
    assert not late_fold.exists()


@pytest.mark.asyncio
async def test_a_summary_carrying_segment_is_never_unlinked(tmp_path):
    """Tier 2: condition ① -- even when a summary-carrying segment's own
    max_seq clears the floor and the margin, it is NEVER unlinked (the
    durable fold evidence must survive)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "delta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "delta", "x")

    summary_seg = _write_sealed(
        tmp_path, "delta",
        [_turn(4), _turn(5), _summary(6, covers_from=4, covers_through=5)],
        has_summary=True,
    )
    _write_active(tmp_path, "delta", _pad_past_margin(7))

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert summary_seg.exists(), "a summary-carrying segment must never be unlinked"


@pytest.mark.asyncio
async def test_nothing_removed_before_the_floor_advances(tmp_path):
    """Tier 2: time axis -- a folded, margin-eligible segment is NOT GC'd
    while the WAL floor has not yet advanced past it."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "epsilon")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "epsilon", "x")

    fold_seg = _write_sealed(tmp_path, "epsilon", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    _write_sealed(
        tmp_path, "epsilon", [_summary(20, covers_from=4, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "epsilon", _pad_past_margin(7))

    # No truncate_below call -- floor never advances past the fold.
    await reg._prune_generations_below(1)

    assert fold_seg.exists()


@pytest.mark.asyncio
async def test_startup_hydration_margin_protects_a_short_active_only_session(tmp_path):
    """Tier 2: a fold-covered, below-floor sealed segment is STILL
    protected if the session as a whole (active + sealed) is short enough
    that startup hydration would still read back the fold boundary
    itself -- the margin (condition ②) is not overridden by the other 2
    conditions."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "zeta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "zeta", "x")

    # Fold covers 4-6, but only a handful of lines total across active +
    # sealed -- the margin boundary (read_history_tail_segmented's own
    # BOF fallback) sits at/below seq 4.
    fold_seg = _write_sealed(tmp_path, "zeta", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    _write_sealed(
        tmp_path, "zeta", [_summary(6, covers_from=4, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "zeta", [_turn(7)])

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert fold_seg.exists(), "the margin must protect a short session's own fold boundary"


@pytest.mark.asyncio
async def test_fail_closed_on_summary_missing_covers_from_seq(tmp_path):
    """Tier 2: a pre-#5765 summary with ``covers_through_seq`` but no
    ``covers_from_seq`` contributes NO range (fail-closed) -- the
    otherwise-eligible sealed segment survives."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "eta")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "eta", "x")

    fold_seg = _write_sealed(tmp_path, "eta", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    _write_sealed(
        tmp_path, "eta", [_summary(20, covers_from=None, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "eta", _pad_past_margin(7))

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert fold_seg.exists()


@pytest.mark.asyncio
async def test_no_history_at_all_is_a_no_op(tmp_path):
    """Tier 2: an agent with no history at all (never sent a message)
    does not crash the GC pass, and no ``history/`` dir is created by GC
    itself."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "theta")
    log = reg.state_log
    for _ in range(5):
        await _put(log, "theta", "x")
    await _advance_floor_past(reg, 3)

    await reg._prune_generations_below(1)  # must not raise

    assert not history_dir_for(tmp_path / ".reyn" / "agents" / "theta").exists()


@pytest.mark.asyncio
async def test_gc_frees_real_disk_space(tmp_path):
    """Tier 2: the key differentiator from the rejected "discard-only"
    earlier design -- GC runs (and frees real bytes) purely from the
    throttled truncation pass, with zero `/rewind`/`checkout` calls ever
    made on this registry."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "iota")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "iota", "x")

    fold_seg = _write_sealed(tmp_path, "iota", [_turn(4), _turn(5), _turn(6)], has_summary=False)
    size_before = fold_seg.stat().st_size
    _write_sealed(
        tmp_path, "iota", [_summary(20, covers_from=4, covers_through=6)], has_summary=True,
    )
    _write_active(tmp_path, "iota", _pad_past_margin(7))
    agent_dir = tmp_path / ".reyn" / "agents" / "iota"
    total_before = sum(p.stat().st_size for p in agent_dir.rglob("*") if p.is_file())

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)  # no checkout()/rewind_to() call anywhere

    total_after = sum(p.stat().st_size for p in agent_dir.rglob("*") if p.is_file())
    assert total_after < total_before
    assert total_before - total_after >= size_before


# #6248: the liveness gate #6247 required (GC must never rewrite a LIVE
# session's own history.jsonl -- Session held a session-lifetime append
# handle onto that SAME path, so a rewrite would strand the handle on a
# detached inode) is REMOVED here, not narrowed: GC now only ever unlinks
# SEALED segments, whose filename the active segment can never carry (the
# active segment is ALWAYS literally ``history.jsonl``, never a sealed
# name) -- the two writers' path sets are disjoint BY CONSTRUCTION. The
# deny/present pair below is the REQUIRED witness for keeping the gate
# removed (architect instruction on the predecessor gate: neither alone
# is sufficient) -- both drive a REAL ``Session`` through REAL appends and
# a REAL seal, not synthetic segment files, so the appender's own
# ownership of sealing is exercised too.
def _seal_boundary_bytes() -> int:
    from reyn.runtime.history_segments import SEGMENT_MAX_BYTES

    return SEGMENT_MAX_BYTES


@pytest.mark.asyncio
async def test_a_live_sessions_own_sealed_segment_is_gcd(tmp_path):
    """Tier 2: #6248 present-side witness (gate REMOVAL, not narrowing) --
    a (name, sid) this registry holds a LIVE in-process ``Session`` for
    still has its OWN sealed segment unlinked by GC, once that segment's
    max_seq clears the floor and margin and is fully fold-covered.

    Strip-falsify (in-file Edit only): re-adding the pre-#6248 liveness
    gate (``if self.get_session(name, sid) is not None: continue`` at the
    top of ``AgentRegistry._gc_history_jsonl_below``'s per-session loop)
    turned this RED with::

        AssertionError: a live session's own fully-covered sealed segment
        must still be unlinked once #6247's liveness gate is removed --
        it still exists after GC. If this failed, the liveness gate was
        re-added or GC is skipping live sessions again.

    restored (Edit), confirmed GREEN again."""
    name = "kappa"
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, name)
    session = make_session(
        agent_name=name, state_log=StateLog(tmp_path / f"{name}-live.wal"),
        workspace_base_dir=tmp_path / ".reyn" / "agents",
    )
    seg_boundary = _seal_boundary_bytes()
    big = "x" * (seg_boundary // 4)
    while True:
        session._append_history(ChatMessage(role="user", content=big))
        # #6240 ③: the seal decision itself now runs off-loop, inside the
        # DurabilityWorker's own write job -- await it landing before
        # checking disk for a sealed segment each iteration.
        await session._flush_history_durability()
        sealed = [p for p in session.history_dir.iterdir() if p.name != "history.jsonl"]
        if sealed:
            break
    sealed_seg = sealed[0]
    # The sealed segment's own max_seq (parsed back from its own name) --
    # never read from private state here; the filename IS the public
    # contract GC itself reads.
    from reyn.runtime.history_segments import parse_sealed_segment_name

    parsed = parse_sealed_segment_name(sealed_seg.name)
    assert parsed is not None
    fold_covers = parsed.max_seq
    session._append_history(ChatMessage(
        role="summary", content="summarised",
        meta={"structured": {}, "covers_through_seq": fold_covers, "covers_from_seq": 1},
    ))
    for _ in range(_HISTORY_HYDRATE_MIN_LINES + 5):
        session._append_history(ChatMessage(role="user", content="pad"))
    # #6240 ③: GC's own fold-coverage scan (below, via _prune_generations_
    # below) reads the summary appended above from DISK -- await it
    # landing before GC runs, or GC sees no fold record yet and never
    # unlinks the sealed segment this test's own assertion checks.
    await session._flush_history_durability()

    reg._store_session(name, session)  # default sid="main"
    log = reg.state_log
    for _ in range(fold_covers + 5):
        await _put(log, name, "x")

    assert sealed_seg.exists()
    await _advance_floor_past(reg, fold_covers)
    await reg._prune_generations_below(1)

    assert not sealed_seg.exists(), (
        "a live session's own fully-covered sealed segment must still be "
        "unlinked once #6247's liveness gate is removed -- it still "
        "exists after GC. If this failed, the liveness gate was re-added "
        "or GC is skipping live sessions again."
    )


@pytest.mark.asyncio
async def test_the_active_segment_is_never_a_gc_candidate(tmp_path):
    """Tier 2: deny-side sibling -- the ACTIVE segment (still
    ``history.jsonl`` — never renamed) is never touched by GC even when
    every content-level eligibility condition would otherwise be met,
    because it is never returned by ``list_sealed_segments`` at all."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "lambda")
    log = reg.state_log
    for _ in range(20):
        await _put(log, "lambda", "x")

    _write_sealed(
        tmp_path, "lambda", [_summary(20, covers_from=1, covers_through=6)], has_summary=True,
    )
    active = _write_active(
        tmp_path, "lambda",
        [_turn(1), _turn(2), _turn(3)] + [_turn(4), _turn(5), _turn(6)] + _pad_past_margin(7),
    )
    before_ino = active.stat().st_ino

    await _advance_floor_past(reg, 6)
    await reg._prune_generations_below(1)

    assert active.exists()
    assert active.stat().st_ino == before_ino, "the active segment's inode must never change under GC"
