"""Tier 2: #4966 — the collect-events-without-settle gate.

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files) — the function under test reads real file content and parses real
ASTs, so faking the filesystem would test nothing real. Mirrors
`tests/scripts/test_check_fastmcp_import_boundary_3698.py`'s own shape
(reject variants / accept variants / a final "real tree is currently
clean" check against the gate's own live scope).

#4990: this gate's own starting population was zero (all 31 found
instances were fixed in the same PR that added it, #4966) but had NO
covering test of its own — "0 hits" cannot distinguish "nothing to find"
from "the gate never actually runs its detection logic" (CLAUDE.md's
pre-conclusion checklist: an absence needs a positive witness, not a
green run). Every fixture below asserts a SHAPE the gate must detect (or
must NOT), never a bare hit-count.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_collect_events_settle import offending_files


def test_a_read_with_no_settle_before_it_is_flagged(tmp_path: Path) -> None:
    """Tier 2: the exact class #4965/#4966 exists to close — an `async
    def` test binds `collect_events(log)`'s result and reads it back with
    no yield point (settle/drain/polling helper) anywhere earlier in the
    same function, so the read can race the background consumer."""
    (tmp_path / "test_x.py").write_text(
        "async def test_something(log):\n"
        "    collected = collect_events(log)\n"
        "    await trigger(log)\n"
        "    assert not any(e.type == 'denied' for e in collected)\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert [p for p, _hits in offenders] == [tmp_path / "test_x.py"]
    assert offenders[0][1] == [(4, "collected")]


def test_a_one_level_derived_name_is_also_flagged(tmp_path: Path) -> None:
    """Tier 2: a name derived one level from an already-tracked name
    (a comprehension filtering `collected`) is tracked too — the real
    shape most of the 31-site audit's instances used (`blocked =
    [e for e in collected if ...]`, not a bare re-read of `collected`
    itself)."""
    (tmp_path / "test_y.py").write_text(
        "async def test_something(log):\n"
        "    collected = collect_events(log)\n"
        "    await trigger(log)\n"
        "    denied = [e for e in collected if e.type == 'denied']\n"
        "    assert denied == []\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert [p for p, _hits in offenders] == [tmp_path / "test_y.py"]


def test_a_hand_rolled_add_subscriber_read_is_also_flagged(tmp_path: Path) -> None:
    """Tier 2: the SECOND discriminator this gate closes (found the same
    night, #4966's own docstring) — a hand-rolled
    `log.add_subscriber(collected.append)` list, never touching
    `collect_events()` at all, needs the identical settle()-before-read
    treatment and must be tracked the same way."""
    (tmp_path / "test_z.py").write_text(
        "async def test_something(log):\n"
        "    collected = []\n"
        "    log.add_subscriber(collected.append)\n"
        "    await trigger(log)\n"
        "    assert not any(e.type == 'denied' for e in collected)\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert [p for p, _hits in offenders] == [tmp_path / "test_z.py"]


def test_a_settle_before_the_read_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: accept side — the actual fix (`await settle(log)`
    immediately before the read) must not false-positive. This is the
    consumer #4990 itself names: an author who did the fix correctly
    must never see this gate fire on them (six-questions ③)."""
    (tmp_path / "test_ok.py").write_text(
        "async def test_something(log):\n"
        "    collected = collect_events(log)\n"
        "    await trigger(log)\n"
        "    await settle(log)\n"
        "    assert not any(e.type == 'denied' for e in collected)\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_unrelated_code_with_no_tracked_name_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — an ordinary async test with no
    `collect_events()`/`add_subscriber()` anywhere must not be touched by
    this gate at all."""
    (tmp_path / "test_unrelated.py").write_text(
        "async def test_something():\n"
        "    result = await do_a_thing()\n"
        "    assert result == 'ok'\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current `tests/` tree (not assumed), matching the sibling
    gates' own "run it before shipping it" discipline. #4966 fixed every
    found instance in the same PR that added this gate, so this asserts
    it stayed at zero, not that it started there."""
    from scripts.check_collect_events_settle import _TESTS_DIR

    offenders = offending_files(_TESTS_DIR)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
