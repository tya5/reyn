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
    has_matching_basename_rewrite_pair,
    is_tests_copy,
    offending_lines,
    position_dependent_rename_lines,
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
    renames). The SOURCE side specifically must be under tests/ — a copy
    whose source is OUTSIDE tests/ (#3913's false-positive shape) does not
    count, even though the DESTINATION is under tests/."""
    assert is_tests_copy("C100\tscripts/old.py\tscripts/new.py") is False
    assert is_tests_copy("C100\ttests/old.py\ttests/new.py") is True
    assert is_tests_copy("C100\tsrc/reyn/data/pipelines/old.py\ttests/new.py") is False


def test_is_tests_copy_excludes_init_py_destinations() -> None:
    """Tier 2: #3913 — a C-status match landing on an __init__.py
    destination is never a real activation signal, regardless of source.
    An empty __init__.py is trivially "100% similar" to every OTHER empty
    file in the tree, so -C --find-copies-harder can match a brand-new,
    legitimate tests/<pkg>/__init__.py against some unrelated empty file
    ANYWHERE (real repro: src/reyn/data/pipelines/__init__.py) — that
    shape is already legitimately handled elsewhere (the empty-content
    check), not by this activation signal."""
    assert is_tests_copy("C100\tsrc/reyn/data/pipelines/__init__.py\ttests/runtime/__init__.py") is False
    assert is_tests_copy("C100\ttests/other/__init__.py\ttests/runtime/__init__.py") is False


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


def test_a_new_package_init_py_matching_an_unrelated_empty_file_is_allowed(
    _repo: Path,
) -> None:
    """Tier 2: #3913 — the REAL reproduction lead-coder ran against the
    actual repo (issue #3879, PR #3913 review): a legitimate Stage-1
    migration PR (a real git-mv rename, activating the gate) that ALSO
    creates a brand-new tests/<pkg>/__init__.py must not be rejected just
    because that empty __init__.py happens to -C-match some UNRELATED
    empty file elsewhere in the tree. An unrelated empty file (mirroring
    src/reyn/data/pipelines/__init__.py in the real repro) is added to the
    fixture repo specifically so `-C --find-copies-harder` has a same-
    content candidate to (wrongly, pre-fix) match against."""
    (_repo / "unrelated").mkdir()
    (_repo / "unrelated" / "__init__.py").write_text("", encoding="utf-8")
    _commit_all(_repo, "add an unrelated empty __init__.py elsewhere in the tree")

    # The migration PR's diff must be measured from THIS commit as base —
    # the unrelated file needs to exist in the diff's BASE state (like
    # src/reyn/data/pipelines/__init__.py already sitting on main before
    # the real migration PR opened), not appear WITHIN the diffed range
    # itself. Falsify-verified this distinction directly: diffing from two
    # commits back (spanning both this commit and the next) made the
    # unrelated file ALSO look newly-added within the diff, and git's
    # copy matcher then reported plain `A` lines for both files instead of
    # the real `C100` match — silently making this test vacuous. Confirmed
    # against a real throwaway repo before fixing.
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    (core / "__init__.py").write_text("", encoding="utf-8")
    _commit_all(_repo, "real move + new package __init__.py")

    lines = diff_name_status(base, root=_repo)
    assert gate_is_active(lines), "the real git-mv rename should activate the gate"
    offenders = offending_lines(lines, root=_repo)
    assert offenders == [], (
        f"a legitimate new empty __init__.py, C-matched against an "
        f"unrelated empty file, was wrongly flagged: {offenders}"
    )


def test_a_new_test_plus_new_init_py_still_leaves_the_gate_inactive(
    _repo: Path,
) -> None:
    """Tier 2: lead-coder's required pre-check (b) — the #3885 deadlock
    scenario (a brand-new test file + a brand-new empty __init__.py, no
    deletion) must STILL leave the gate inactive after #3909's fix.

    ★ Corrected in review (lead-coder, #3913 follow-up): the first version
    of this fixture had NO pre-existing empty file anywhere in the tree, so
    the new __init__.py had no possible C-match candidate at all — it
    always fell back to a plain `A` line REGARDLESS of whether #3913's
    fix (condition ②: dest is not `__init__.py`) was present or stripped,
    the same "docstring claims a witness the fixture doesn't actually
    exercise" gap this PR's own reproduction fixture
    (``test_a_new_package_init_py_matching_an_unrelated_empty_file_is_allowed``)
    is built specifically to catch. Verified directly (a throwaway repo,
    not assumed): with no pre-existing empty file, this exact scenario's
    diff reports `A tests/newpkg/__init__.py` / `A tests/newpkg/test_new.py`
    — never a `C` line — so condition ② was never exercised here. Fixed by
    adding a pre-existing empty file to the fixture (mirroring lead-coder's
    own real-repo reproduction 1:1), which DOES give the new __init__.py a
    same-content match, making this test a genuine test of condition ②
    rather than a vacuous one."""
    (_repo / "unrelated_elsewhere.py").write_text("", encoding="utf-8")
    _commit_all(_repo, "add a pre-existing empty file elsewhere in the tree")

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
        "a genuinely new test file + new __init__.py, with a pre-existing "
        "empty file elsewhere as a possible C-match candidate, incorrectly "
        "activated the gate after #3909/#3913's fix"
    )


# ── #3930 — an unrelated new file + an unrelated deletion is not a move ─────
# #3929's real CI failure: an unrelated new test file (a brand-new
# tests/interfaces/ addition) landed in the SAME diff as an unrelated
# deletion (a wholly separate file's removal elsewhere in tests/), with no
# relationship between the two beyond both touching tests/. The prior
# signal (`has_new_subdir_file AND has_deletion`, independent of each
# other) treated "something appeared AND something disappeared anywhere in
# tests/" as evidence of a disguised move — which any PR that both adds a
# new test file and deletes an unrelated one will trip. The fix requires
# the appeared/disappeared pair to share a BASENAME before treating them as
# one rewrite-disguised-as-move candidate (`has_matching_basename_rewrite_
# pair`), since basename equality is the one signal that survives even a
# 0%-content-overlap disguised move (see the sibling test below) while
# discriminating it from two unrelated files.


def test_an_unrelated_new_file_and_an_unrelated_deletion_leaves_the_gate_inactive(
    _repo: Path,
) -> None:
    """Tier 2: #3930 — the real #3929 CI false positive, reproduced. A new
    test file appearing in one subdirectory and an unrelated pre-existing
    file being deleted elsewhere, with DIFFERENT basenames, must not
    activate the gate — this exact shape wrongly failed #3929's real CI
    before this fix (measured directly against the real PR diff, not
    assumed from the design)."""
    (_repo / "tests" / "test_unrelated_to_delete.py").write_text(
        "some content nobody moved\n", encoding="utf-8",
    )
    _commit_all(_repo, "add a second, unrelated flat test file")

    core = _repo / "tests" / "core"
    core.mkdir()
    (core / "test_brand_new.py").write_text(
        "a genuinely new test file, unrelated to anything deleted\n",
        encoding="utf-8",
    )
    (_repo / "tests" / "test_unrelated_to_delete.py").unlink()
    _commit_all(_repo, "add an unrelated new file + delete an unrelated old one")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert not has_matching_basename_rewrite_pair(lines), (
        "an unrelated new file (test_brand_new.py) and an unrelated "
        "deletion (test_unrelated_to_delete.py) — different basenames — "
        "were wrongly paired as a disguised-move candidate"
    )
    assert not gate_is_active(lines), (
        "an unrelated new-file-in-subdir + unrelated deletion elsewhere, "
        "with no basename relationship, incorrectly activated the gate — "
        "the exact #3929 false positive this fix closes"
    )


# ── #4002 (superseding #3995's own first attempt) — R100 does not imply
# "safe" for a position-dependent file. architect's final design: no static
# guessing at all — the move has ALREADY HAPPENED by the time this gate
# runs, so it re-resolves every __file__-rooted expression at the file's
# REAL new location and asks the one question that needs no guessing: does
# the target still exist? A real instance broke exactly this way mid-arc
# (#3989, #3994), caught at CI runtime, not by this gate — this is the fix
# that lets the gate itself say so, instead of declaring "safe".


def test_r100_rename_of_a_position_dependent_file_is_not_declared_safe(
    _repo: Path,
) -> None:
    """Tier 2: #4002 — THE gate's own founding axiom ("byte-identical ⇒
    safe") is false for a file whose meaning depends on its OWN LOCATION.
    A pure `git mv` of a file containing `Path(__file__).parent.parent`
    reports R100 (bytes unchanged) but the VALUE changes with the move —
    re-resolved at the new location, the target (repo root) is nonexistent
    from `tests/core/`'s vantage (this throwaway fixture has no `scripts/`
    at all, so the target is missing regardless — the real-repo case is
    the same nonexistence, just at the wrong depth instead of absent
    outright); either way this must now be flagged, not passed through."""
    (_repo / "tests" / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"\n',
        encoding="utf-8",
    )
    _commit_all(_repo, "give test_a.py a depth-2 __file__ reference")

    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    _commit_all(_repo, "pure byte-identical move")

    lines = diff_name_status("HEAD~1", root=_repo)
    offenders = offending_lines(lines, root=_repo)
    assert offenders, (
        "a byte-identical rename of a file whose __file__ reference leaves "
        "its own directory was wrongly declared safe"
    )
    flagged = position_dependent_rename_lines(offenders, root=_repo)
    assert flagged, (
        f"the offender was not classified as position-dependent: {offenders!r}"
    )


def test_r100_rename_of_a_still_resolvable_file_stays_clean(
    _repo: Path,
) -> None:
    """Tier 2: non-vacuity for the fix above — a file whose __file__
    reference resolves to something that GENUINELY STILL EXISTS at the new
    location (`Path(__file__).parent / "fixture.json"`, a fixture
    co-located with — and moved together with, in the same commit as — the
    test file) must still pass through untouched; the check must not
    over-fire on every __file__ usage, only ones whose target is actually
    missing post-move."""
    (_repo / "tests" / "test_a.py").write_text(
        "from pathlib import Path\n"
        '_FIXTURE = Path(__file__).parent / "fixture.json"\n',
        encoding="utf-8",
    )
    (_repo / "tests" / "fixture.json").write_text("{}\n", encoding="utf-8")
    _commit_all(_repo, "give test_a.py a co-located fixture reference")

    core = _repo / "tests" / "core"
    core.mkdir()
    subprocess.run(
        ["git", "mv", "tests/test_a.py", "tests/core/test_a.py"],
        cwd=_repo, check=True,
    )
    subprocess.run(
        ["git", "mv", "tests/fixture.json", "tests/core/fixture.json"],
        cwd=_repo, check=True,
    )
    _commit_all(_repo, "pure byte-identical move, fixture moved alongside")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert offending_lines(lines, root=_repo) == []


def test_a_same_basename_zero_similarity_disguised_move_still_activates_the_gate(
    _repo: Path,
) -> None:
    """Tier 2: non-vacuity for the test above — the signal's actual
    purpose (a 0%-content-overlap disguised move, indistinguishable from
    an unrelated add+delete by content alone) must still be caught via
    basename equality. Uses fully disjoint random content on both sides
    (no shared line) so git's own similarity search — measured directly
    beforehand — reports plain `A`/`D` lines rather than a low-similarity
    `R` line, exercising `has_matching_basename_rewrite_pair` itself
    rather than the `is_tests_rename` signal."""
    import random

    rnd = random.Random(2024)
    old_lines = [f"old_unique_line_{i}_{rnd.random()}" for i in range(50)]
    (_repo / "tests" / "test_a.py").write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _commit_all(_repo, "replace tests/test_a.py with disjoint content")

    core = _repo / "tests" / "core"
    core.mkdir()
    new_lines = [f"new_unique_line_{i}_{rnd.random()}" for i in range(50)]
    (core / "test_a.py").write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    (_repo / "tests" / "test_a.py").unlink()
    _commit_all(_repo, "disguised move: same basename, zero content overlap")

    lines = diff_name_status("HEAD~1", root=_repo)
    assert all(not line.startswith(("R", "C")) for line in lines), (
        f"fixture assumption broken — expected plain A/D lines, got: {lines!r}"
    )
    assert has_matching_basename_rewrite_pair(lines), (
        "a same-basename, zero-similarity disguised move was not recognized "
        "as a rewrite-pair candidate"
    )
    assert gate_is_active(lines), (
        "a same-basename disguised move with zero content overlap did not "
        "activate the gate — the signal's original purpose, which the "
        "#3930 basename-equality fix must preserve"
    )
