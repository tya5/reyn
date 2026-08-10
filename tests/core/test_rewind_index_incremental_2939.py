"""Tier 2: #2939 — deriving the active branch must not re-decode the WHOLE WAL on
every turn, and the incremental derivation must never outlive the entries it was
built from.

Residual after #2941: that fix hoisted the ``is_active_seq`` scan out of the
per-MESSAGE loop (O(N x M) -> O(N + M) per turn), but the surviving scan still
json-decoded every WAL line once per turn. So the cost of a turn (and of opening
the chat's context dropdown, which drives the same producer) grew with WAL SIZE
rather than with what had actually changed — #2940 measured that scan at ~99.7%
of a dropdown open.

**Why the #2941 seam could not see this bug.** ``test_active_branch_history_scan_
bound_2941.py`` counts ``iter_from`` CALLS, and that count was already constant
(2 per open) at every WAL size — the call count is structurally blind to the M
axis, which is exactly how this residual survived a green suite. These tests
therefore measure WAL lines DECODED, the quantity that actually grew, and pin it
against a *growing* WAL rather than a fixed one.

Real seam throughout: real ``StateLog`` on a real on-disk WAL, the real
``checkout`` reset-record primitive, the real ``truncate_below`` rewrite, and the
real ``build_active_predicate`` / ``is_active_seq`` derivations. Decodes are
counted by wrapping ``json.loads`` with a real counting function (not a Mock) —
the seam every WAL reader goes through, so a revert to a full ``iter_from(1)``
re-scan is caught rather than silently bypassing the counter.
"""
from __future__ import annotations

import json

import pytest

from reyn.core.events.snapshot_generations import (
    REWIND_KIND,
    build_active_predicate,
    checkout,
    is_active_seq,
)
from reyn.core.events.state_log import StateLog


def _count_decodes(monkeypatch):
    """Wrap json.loads with a real counting function; returns the live counter."""
    calls = {"n": 0}
    real_loads = json.loads

    def counting_loads(*args, **kwargs):
        calls["n"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting_loads)
    return calls


async def _grow_wal(state_log: StateLog, n: int) -> int:
    """Append n ordinary WAL entries; return the last seq."""
    seq = 0
    for _ in range(n):
        seq = await state_log.append("step_completed")
    return seq


@pytest.mark.asyncio
async def test_active_branch_derivation_decodes_do_not_grow_with_wal_size(tmp_path, monkeypatch):
    """Tier 2: re-deriving the active-branch predicate costs WAL lines decoded
    proportional to what was APPENDED since the last derivation, not to the WAL's
    total size. A full re-scan (the #2939 residual) makes the decode count grow
    with the WAL; the incremental index keeps it flat."""
    state_log = StateLog(tmp_path / "state.wal")
    anchor = await _grow_wal(state_log, 50)
    await checkout(state_log, target_seq=anchor // 2)
    build_active_predicate(state_log)  # warm

    counter = _count_decodes(monkeypatch)

    # Derive against a small WAL, then again against a much larger one. The WAL
    # grew ~20x between the two; a full re-scan would decode ~20x more lines.
    counter["n"] = 0
    build_active_predicate(state_log)
    small = counter["n"]

    await _grow_wal(state_log, 1000)
    build_active_predicate(state_log)  # absorb the delta once

    counter["n"] = 0
    build_active_predicate(state_log)
    large = counter["n"]

    assert large == small, (
        f"decodes per derivation must not grow with WAL size (small WAL: {small} "
        f"lines decoded, ~20x-larger WAL: {large}) — a full re-scan would make "
        "large >> small, which is the #2939 residual this pins"
    )


@pytest.mark.asyncio
async def test_repeated_derivation_over_unchanged_wal_decodes_nothing(tmp_path, monkeypatch):
    """Tier 2: when nothing was appended, re-deriving decodes no WAL lines at all
    — the dropdown-open / per-turn path pays for change, not for history size."""
    state_log = StateLog(tmp_path / "state.wal")
    anchor = await _grow_wal(state_log, 200)
    await checkout(state_log, target_seq=anchor // 2)
    build_active_predicate(state_log)  # warm

    counter = _count_decodes(monkeypatch)
    counter["n"] = 0
    build_active_predicate(state_log)

    assert counter["n"] == 0, (
        f"an unchanged WAL must cost zero decodes to re-derive (got {counter['n']}) "
        "— any non-zero count means the whole log is being re-read per turn"
    )


@pytest.mark.asyncio
async def test_rewind_appended_after_warm_index_takes_effect(tmp_path):
    """Tier 2: a reset-record appended AFTER the index warmed is honoured — the
    incremental cursor must pick up mid-session rewinds, not freeze the branch
    model at whatever was on disk when it was first built."""
    state_log = StateLog(tmp_path / "state.wal")
    anchor = await _grow_wal(state_log, 10)
    abandoned_seq = await _grow_wal(state_log, 5)

    assert is_active_seq(state_log, abandoned_seq), "sanity: active before any rewind"

    await checkout(state_log, target_seq=anchor)

    assert not is_active_seq(state_log, abandoned_seq), (
        "a rewind appended after the derivation was first built must abandon the "
        "seqs it rewound past — a frozen index would still call them active"
    )


@pytest.mark.asyncio
async def test_truncation_dropping_a_rewind_record_does_not_serve_a_stale_interval(tmp_path):
    """Tier 2: when retention's WAL rewrite drops a reset-record, the branch model
    must rebuild from the rewritten file — never keep serving the abandoned
    interval that record used to imply.

    This is the #2939 staleness hazard: an in-memory index of rewind records is
    not itself truncated, so a cache that merely appended would answer from a
    record the WAL no longer holds, and abandoned conversation turns would
    silently reappear in the LLM's context. ``truncate_below`` renames a fresh
    file into place, so the index's file-identity check must catch it.
    """
    state_log = StateLog(tmp_path / "state.wal")
    anchor = await _grow_wal(state_log, 10)
    abandoned_seq = await _grow_wal(state_log, 5)
    await checkout(state_log, target_seq=anchor)
    head = await _grow_wal(state_log, 5)

    # Warm the derivation while the reset-record is present.
    assert not is_active_seq(state_log, abandoned_seq), "sanity: abandoned pre-truncation"

    # Retention rewrite WITHOUT always_keep_kinds → the reset-record is dropped.
    await state_log.truncate_below(head)
    await state_log.flush()
    surviving_kinds = [e.get("kind") for e in state_log.iter_from(0)]
    assert REWIND_KIND not in surviving_kinds, (
        "test premise: this truncation must actually drop the reset-record, "
        "otherwise it cannot witness staleness"
    )

    assert is_active_seq(state_log, abandoned_seq), (
        "after the reset-record was truncated away, the branch model must reflect "
        "the WAL that now exists (nothing abandoned) — still reporting the seq as "
        "abandoned means a stale interval was served from a cache the truncation "
        "could not reach"
    )


@pytest.mark.asyncio
async def test_truncation_preserving_rewind_records_keeps_them_active(tmp_path):
    """Tier 2: the complement — a retention rewrite that PRESERVES reset-records
    (the production call, which passes always_keep_kinds) must leave the branch
    model intact. The rebuild-on-rewrite must re-read the kept records, not
    forget them."""
    state_log = StateLog(tmp_path / "state.wal")
    anchor = await _grow_wal(state_log, 10)
    abandoned_seq = await _grow_wal(state_log, 5)
    await checkout(state_log, target_seq=anchor)
    head = await _grow_wal(state_log, 5)

    assert not is_active_seq(state_log, abandoned_seq), "sanity: abandoned pre-truncation"

    await state_log.truncate_below(head, always_keep_kinds=frozenset({REWIND_KIND}))
    await state_log.flush()

    assert not is_active_seq(state_log, abandoned_seq), (
        "a truncation that keeps the reset-record must keep its abandoned interval "
        "— dropping it here would resurrect rewound-past turns into the LLM context"
    )
