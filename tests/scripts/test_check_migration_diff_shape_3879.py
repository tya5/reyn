"""Tier 2: #3879 Stage 1 M1 — the migration-diff-shape gate.

Placed in tests/scripts/ (mirrors test_flat_tests_ratchet_3879.py's own
placement rationale — this file is itself new, so it must obey Stage 0's
ratchet, which is already merged and live on this checkout).

Real git repos throughout (a real `git init`, real commits, real `git mv` /
rewrite / __init__.py additions) — the function under test is a thin wrapper
over `git diff`'s own output, so faking git would test nothing real.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.check_migration_diff_shape import (
    blob_at_head,
    diff_name_status,
    gate_is_active,
    is_tests_copy,
    offending_lines,
)


@pytest.fixture
def _repo(tmp_path: Path) -> Path:
    """A real git repo with one commit carrying a single flat test file —
    the starting state every scenario below mutates from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_pure_rename_is_clean(_repo: Path) -> None:
    """Tier 2: a byte-identical `git mv` produces zero offenders — the
    entire point of the gate is to let this shape through untouched."""
    (_repo / "tests" / "core").mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    _commit_all(_repo, "move")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert offending_lines(lines, root=_repo) == []


def test_rewrite_disguised_as_a_move_is_caught(_repo: Path) -> None:
    """Tier 2: THE gate's whole reason to exist — owner's exact scenario.
    An agent told to "move" the file instead deletes the old one and writes
    a new file at the destination with ONE extra line.

    ★ The exact diff SHAPE this produces changed under #3909's own
    falsify-verification, caught by this very test going red after that
    change (not assumed from the design): with the ``-M100%``-only gate
    (pre-#3909), this reported as separate ``A``/``D`` lines. Adding ``-C
    --find-copies-harder`` (needed for #3909's copy-left-behind detection)
    makes git's broadened similarity search classify this as a LOW-similarity
    RENAME instead (``R075`` measured for this exact tiny fixture — the
    percentage itself is not asserted below, since it is a function of file
    size/content and would be a brittle pin; only that it is a non-100
    rename line touching both paths). Either shape must still be rejected —
    :func:`offending_lines` only ever allows similarity ``"100"``, so this
    remains caught either way, just via a different line shape.

    ★ Checking activation explicitly, not just calling offending_lines()
    directly, closes a real gap this session's own falsify-verification
    already found once (a test bypassing gate_is_active() and silently no
    longer reflecting main()'s real behavior) — this exact scenario is what
    the second review round found BOTH this gate (originally, no rename =
    inactive) and Stage 0's non-recursive ratchet missed entirely."""
    core = _repo / "tests" / "core"
    core.mkdir()
    (core / "test_a.py").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    (_repo / "tests" / "test_a.py").unlink()
    _commit_all(_repo, "rewrite disguised as move")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert gate_is_active(lines), (
        "a fully-rewritten move into a subdirectory did not activate the "
        "gate — this is the exact shape neither this gate nor Stage 0's "
        "ratchet caught before the fix"
    )
    offenders = offending_lines(lines, root=_repo)
    matching = [
        line for line in offenders
        if line.startswith("R") and not line.startswith("R100\t")
        and "tests/test_a.py" in line and "tests/core/test_a.py" in line
    ]
    assert matching, (
        f"no non-100-similarity rename line referencing both the old and "
        f"new path was flagged as an offender: {offenders!r}"
    )


def test_empty_init_py_addition_is_allowed(_repo: Path) -> None:
    """Tier 2: a NEW, EMPTY tests/<pkg>/__init__.py is allowed — a migration
    PR creating a fresh subdirectory needs one."""
    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    (core / "__init__.py").write_text("", encoding="utf-8")
    _commit_all(_repo, "move + init")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert offending_lines(lines, root=_repo) == []


def test_nonempty_init_py_is_rejected(_repo: Path) -> None:
    """Tier 2: non-vacuity for the __init__.py allowance above — a
    NON-EMPTY __init__.py (real code smuggled in under the placeholder's
    path) must NOT pass just because the filename matches."""
    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    (core / "__init__.py").write_text("import os  # smuggled\n", encoding="utf-8")
    _commit_all(_repo, "move + non-empty init")

    lines = diff_name_status("HEAD~1", root=_repo)
    offenders = offending_lines(lines, root=_repo)
    assert any("__init__.py" in line for line in offenders), (
        f"a non-empty __init__.py was not flagged: {offenders!r}"
    )


def test_baseline_shrink_is_allowed(_repo: Path) -> None:
    """Tier 2: scripts/flat_tests_baseline.json shrinking (a name dropping
    out as its file moves) is allowed — #3883 already permits this
    direction, and this gate must not re-forbid it."""
    (_repo / "scripts").mkdir()
    (_repo / "scripts" / "flat_tests_baseline.json").write_text(
        '["test_a.py"]\n', encoding="utf-8",
    )
    _commit_all(_repo, "baseline v1")

    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    (_repo / "scripts" / "flat_tests_baseline.json").write_text("[]\n", encoding="utf-8")
    _commit_all(_repo, "move + baseline shrink")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert offending_lines(lines, root=_repo) == []


def test_a_content_edit_mixed_with_a_real_move_is_rejected(_repo: Path) -> None:
    """Tier 2: lead-coder's explicit case (#3885 review correction) — once
    the gate is ACTIVE (a real tests/ rename is present), an UNRELATED
    content edit riding along in the SAME PR must still be flagged. This is
    what keeps "migrate this batch" from also smuggling in an unrelated
    fix, once the gate has something to enforce against at all."""
    core = _repo / "tests" / "core"
    core.mkdir()
    (_repo / "tests" / "test_b.py").write_text("unrelated file\n", encoding="utf-8")
    _commit_all(_repo, "add a second file to edit later")

    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    (_repo / "tests" / "test_b.py").write_text("unrelated CHANGE\n", encoding="utf-8")
    _commit_all(_repo, "real move + a smuggled edit")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert gate_is_active(lines), (
        "test setup did not actually produce a tests/ rename"
    )
    offenders = offending_lines(lines, root=_repo)
    assert any("test_b.py" in line for line in offenders), (
        f"the smuggled edit was not flagged once the gate was active: {offenders!r}"
    )


def test_low_similarity_rename_is_rejected(_repo: Path) -> None:
    """Tier 2: a rename git detects but scores BELOW 100% similarity (most
    content replaced, a handful of lines shared) must be rejected — only
    the exact R100 shape is a pure move."""
    core = _repo / "tests" / "core"
    core.mkdir()
    (core / "test_a.py").write_text(
        "totally different content\nline2\nnothing else shared\n", encoding="utf-8",
    )
    (_repo / "tests" / "test_a.py").unlink()
    _commit_all(_repo, "mostly-rewritten move")

    lines = diff_name_status("HEAD~1", root=_repo)
    offenders = offending_lines(lines, root=_repo)
    assert offenders, "a low-similarity rename/rewrite was not flagged"


def test_r099_line_is_rejected_by_the_parser_directly(_repo: Path) -> None:
    """Tier 2: the boundary itself, at the Python level, fed directly rather
    than through `git diff -M100%` — an R099 line CANNOT actually come out
    of `diff_name_status()` in production (confirmed by trial: `-M100%`
    makes git itself only ever report an exact rename as `R100` or fall
    back straight to `A`+`D` for anything less similar — a 200-line file
    with 1 line changed produces `A`/`D`, never an `Rxxx` line, under this
    flag). So `offending_lines()`'s own `similarity == "100"` check is
    defense-in-depth against a shape git's own flag already prevents from
    reaching it — and THIS is the only way to directly exercise that
    specific branch: hand-build the line git would never actually emit.
    Without this, a gate that accepted "R09x and up" (a plausible off-by-one
    on the equality check) would pass every other test in this file, none
    of which reach anywhere near the R100/R099 boundary."""
    offenders = offending_lines(
        ["R099\ttests/test_a.py\ttests/core/test_a.py"], root=_repo,
    )
    assert offenders, "an R099 (not exactly 100%) rename line was not flagged"


# ── gate_is_active — #3885 review correction, the fix to the scope gap ──────


def test_a_plain_new_test_file_in_a_subdir_leaves_the_gate_inactive(_repo: Path) -> None:
    """Tier 2: THE deadlock this gate hit on its OWN introducing PR
    (lead-coder's finding via real CI failure, not a design read — this
    file itself is a brand-new .py in tests/scripts/, no deletion anywhere,
    and the gate rejected its own PR before this fix). A plain new-test
    addition in a subdirectory — appeared, nothing disappeared — must NOT
    activate the gate; only "appeared AND something disappeared" (a real
    move) does."""
    core = _repo / "tests" / "core"
    core.mkdir()
    (core / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")
    _commit_all(_repo, "add a genuinely new test file in a subdirectory")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert not gate_is_active(lines), (
        "a plain new test file in a subdirectory (no deletion anywhere) "
        "incorrectly activated the gate — this is the exact shape that "
        "made the gate reject its own introducing PR"
    )


def test_a_pure_content_edit_leaves_the_gate_inactive(_repo: Path) -> None:
    """Tier 2: the fix itself — an ordinary in-place content edit (a Q3/Q4
    assert repair, a bug fix), with NO rename anywhere in the diff, must
    NOT activate this gate at all. Before this fix, the gate fired on every
    PR touching tests/, including this exact shape (lead-coder's #3885
    correction, after flagging the risk pre-merge)."""
    (_repo / "tests" / "test_a.py").write_text("line1\nline2\nCHANGED\n", encoding="utf-8")
    _commit_all(_repo, "ordinary edit, no rename")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert not gate_is_active(lines), (
        "an ordinary content edit with no rename activated the gate"
    )


def test_a_pure_rename_activates_the_gate(_repo: Path) -> None:
    """Tier 2: non-vacuity for the inactive case above — a REAL tests/
    rename, alone, does activate it."""
    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    _commit_all(_repo, "real move")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert gate_is_active(lines), "a real tests/ rename did not activate the gate"


def test_a_rename_outside_tests_does_not_activate_the_gate(_repo: Path) -> None:
    """Tier 2: the activation signal is scoped to tests/ specifically — an
    unrelated rename elsewhere in the repo (e.g. a scripts/ refactor riding
    in the same PR, however unlikely) must not turn this gate on."""
    (_repo / "scripts").mkdir()
    (_repo / "scripts" / "old_name.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(_repo, "add a scripts file")
    subprocess.run(
        ["git", "mv", "scripts/old_name.py", "scripts/new_name.py"],
        cwd=_repo, check=True,
    )
    _commit_all(_repo, "rename outside tests/")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert not gate_is_active(lines), (
        "a rename outside tests/ incorrectly activated the gate"
    )


def test_blob_at_head_returns_none_for_a_missing_path(_repo: Path) -> None:
    """Tier 2: a defensive-code path — offending_lines calls blob_at_head
    only for an __init__.py-shaped A line, but the helper itself must not
    crash on a path that does not exist at HEAD."""
    assert blob_at_head("tests/does_not_exist/__init__.py", root=_repo) is None


# ── #3909 — copy without deleting the original ──────────────────────────────
# architect's measurement (issue #3879 comment 5229557446): a copy to the
# destination that leaves the original file in place activated NEITHER prior
# signal (no R100 rename — nothing was deleted for git to pair against; no
# new-subdir-file+deletion pair either, by construction). Both this gate's
# activation AND its rejection needed a third signal. Falsify-verified
# directly against a real repo BEFORE writing these tests (not assumed from
# the design): a real `cp` + `git add` (no `rm`) genuinely reports
# `C100 <old> <new>` under `-C --find-copies-harder`, and a genuine unrelated
# `git mv` in the SAME diff still reports plain `R100` — the two are not
# confused with each other in a mixed batch (see module docstring).


def test_copy_without_deleting_the_original_is_caught(_repo: Path) -> None:
    """Tier 2: THE hole #3909 exists to close — copying the file to the
    destination without deleting the original passed silently before this
    fix (gate_is_active stayed False, offending_lines was never even
    called)."""
    core = _repo / "tests" / "core"
    core.mkdir()
    (core / "test_a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    # Deliberately NOT `git mv` / no deletion of tests/test_a.py — the
    # original stays, exactly the bug shape.
    _commit_all(_repo, "copy without deleting the original")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert gate_is_active(lines), (
        "a copy-without-delete did not activate the gate — the exact hole "
        "#3909 exists to close"
    )
    offenders = offending_lines(lines, root=_repo)
    assert any(line.startswith("C") for line in offenders), (
        f"no C (copy) line was flagged as an offender: {offenders}"
    )


def test_is_tests_copy_requires_a_tests_path() -> None:
    """Tier 2: non-vacuity — a copy entirely OUTSIDE tests/ is not this
    gate's concern (mirrors the existing outside-tests exclusion for
    renames)."""
    assert is_tests_copy("C100\tscripts/old.py\tscripts/new.py") is False
    assert is_tests_copy("C100\ttests/old.py\ttests/new.py") is True


def test_a_pure_copy_left_behind_scenario_alone_is_correctly_isolated(
    _repo: Path,
) -> None:
    """Tier 2: lead-coder's required pre-check (a) — a mixed batch where
    ONE file is a genuine `git mv` and a SEPARATE, content-DISTINCT file is
    copied-without-deleting must flag only the copy, not the legitimate
    move. Content must be genuinely distinct (not merely a second copy of
    the same fixture) — falsify-verification found git's copy/rename
    matcher picks ANY same-content deleted file as a source when multiple
    candidates share content, which would confound this test if both
    fixture files were identical."""
    (_repo / "tests" / "test_b.py").write_text(
        "distinct line one\ndistinct line two\ndistinct line three\n",
        encoding="utf-8",
    )
    _commit_all(_repo, "add a second, content-distinct file")

    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a_moved.py"],
        cwd=_repo, check=True,
    )
    (core / "test_b_copy.py").write_text(
        "distinct line one\ndistinct line two\ndistinct line three\n",
        encoding="utf-8",
    )
    # tests/test_b.py is NOT deleted — the copy-left-behind half of this
    # mixed batch.
    _commit_all(_repo, "real move + separate copy-left-behind")

    lines = diff_name_status("HEAD~2", root=_repo)
    offenders = offending_lines(lines, root=_repo)
    offender_text = "\n".join(offenders)
    assert "test_a_moved.py" not in offender_text, (
        f"the legitimate git-mv was wrongly flagged: {offenders}"
    )
    assert any("test_b_copy.py" in line for line in offenders), (
        f"the copy-left-behind was not flagged: {offenders}"
    )


def test_a_new_test_plus_new_init_py_still_leaves_the_gate_inactive(
    _repo: Path,
) -> None:
    """Tier 2: lead-coder's required pre-check (b) — the #3885 deadlock
    scenario (a brand-new test file + a brand-new empty __init__.py, no
    deletion, no pre-existing file with matching content) must STILL leave
    the gate inactive after #3909's fix. A genuinely new test shares no
    content with anything already in the tree, so -C finds no copy source
    for it — unlike the copy-left-behind scenario above, where the content
    DOES already exist elsewhere."""
    newpkg = _repo / "tests" / "newpkg"
    newpkg.mkdir()
    (newpkg / "test_new.py").write_text(
        "brand new content shared with nothing else in the tree\n",
        encoding="utf-8",
    )
    (newpkg / "__init__.py").write_text("", encoding="utf-8")
    _commit_all(_repo, "new test file + new empty __init__.py, both genuinely new")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert not gate_is_active(lines), (
        "a genuinely new test file + new __init__.py (no matching content "
        "anywhere else) incorrectly activated the gate after #3909's fix"
    )
