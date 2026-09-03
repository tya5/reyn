"""Tier 2: #5699 — the no-hardcoded-compaction-role-tuple enforcement gate.

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files) — the function under test reads real file content, so faking the
filesystem would test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_no_hardcoded_compaction_role_tuple import offending_files


def test_a_hardcoded_tuple_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE case #5699 exists to catch — a filter re-typing the
    role tuple instead of importing the named predicate."""
    (tmp_path / "some_filter.py").write_text(
        'turns = [m for m in history if m.role in ("user", "assistant", "tool", "agent")]\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "some_filter.py"]


def test_a_hardcoded_tuple_with_summary_appended_is_also_flagged(tmp_path: Path) -> None:
    """Tier 2: the decompose_history_for_retry-shaped variant (base roles
    + a trailing summary role) must be caught too, not just the bare
    4-role form."""
    (tmp_path / "some_filter.py").write_text(
        'if m.role in ("user", "assistant", "tool", "agent", SUMMARY_MESSAGE_ROLE):\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "some_filter.py"]


def test_single_quotes_and_extra_whitespace_are_still_matched(tmp_path: Path) -> None:
    """Tier 2: the pattern must not be defeated by a cosmetic reformat —
    single quotes or extra spacing around the commas."""
    (tmp_path / "some_filter.py").write_text(
        "roles = ('user' ,  'assistant', 'tool',  'agent')\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [tmp_path / "some_filter.py"]


def test_calling_the_named_predicate_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: deny-side — the FIX shape (importing and calling the named
    predicate) must never itself trip this gate."""
    (tmp_path / "some_filter.py").write_text(
        "from reyn.runtime.chat_message import is_compaction_eligible\n"
        "turns = [m for m in history if is_compaction_eligible(m)]\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_an_unrelated_four_tuple_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — a plain 4-string tuple unrelated to this
    role vocabulary (different words) must not false-positive."""
    (tmp_path / "unrelated.py").write_text(
        'colors = ("red", "green", "blue", "yellow")\n',
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current tree (not assumed), matching the sibling gates' own
    "run it before shipping it" discipline. #5699 closed every existing
    hand-typed copy (router_history_buffer.py's two filters were already
    routed through the named predicate; compaction_controller.py's and
    session.py's own copies were the two the owner's incident found).
    Asserts it stayed that way."""
    from scripts.check_no_hardcoded_compaction_role_tuple import _ROOT, _SRC_DIR

    assert _SRC_DIR == _ROOT / "src"
    offenders = offending_files(_SRC_DIR)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
