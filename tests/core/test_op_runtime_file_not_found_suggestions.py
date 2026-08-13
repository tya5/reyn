"""Tests for read_file / edit_file not_found envelope shape.

When a file doesn't exist, the op result includes an ``error`` string and a
``suggestions`` list of sibling files under the same parent — matching the
shape of invoke_action's UnknownActionError so the LLM produces "did you mean
X" narration for missing files the same way it does for missing actions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,  # skip op-level perm; covered in test_op_runtime_file_permissions.py
        actor="test_skill",
    )


def _read(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def _edit(path: str, *, old: str = "x", new: str = "y") -> FileIROp:
    return FileIROp(kind="file", op="edit", path=path, old_string=old, new_string=new)


def _run(coro):
    return asyncio.run(coro)


# ── read not_found envelope ────────────────────────────────────────────────────


def test_read_not_found_returns_error_and_suggestions(tmp_path, monkeypatch):
    """Tier 2: read_file of a missing file in a populated parent dir returns
    status='not_found' plus ``error`` string and ``suggestions`` list."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "beta.md").write_text("beta", encoding="utf-8")
    (tmp_path / "gamma.md").write_text("gamma", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("nonexistent.md"), ctx))

    assert result["status"] == "not_found"
    assert result["op"] == "read"
    assert result["path"] == "nonexistent.md"
    # error string present (matches invoke_action UnknownActionError shape)
    assert "error" in result
    assert "not found" in result["error"].lower()
    # content kept for backward-compat (callers that read it pre-this-change)
    assert result["content"] == ""
    # suggestions populated from parent dir (no fuzzy match, just listing)
    assert "suggestions" in result
    suggestion_names = {Path(p).name for p in result["suggestions"]}
    assert suggestion_names >= {"alpha.md", "beta.md", "gamma.md"}


def test_read_not_found_empty_dir_returns_empty_suggestions(tmp_path, monkeypatch):
    """Tier 2: suggestions is an empty list (not missing key) when parent has no siblings."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_read("nowhere.md"), ctx))

    assert result["status"] == "not_found"
    assert result["suggestions"] == []


def test_read_not_found_capped_at_limit(tmp_path, monkeypatch):
    """Tier 2: suggestions list does not exceed _NOT_FOUND_SUGGESTIONS_LIMIT (8)."""
    monkeypatch.chdir(tmp_path)
    for i in range(20):
        (tmp_path / f"file{i:02d}.md").write_text("x", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("missing.md"), ctx))

    assert result["status"] == "not_found"
    assert result["suggestions"], "suggestions must be non-empty when siblings exist"
    # Cap is enforced — the full set of 20 files is not returned.
    # _NOT_FOUND_SUGGESTIONS_LIMIT == 8, so suggestions[8:] must be empty.
    assert result["suggestions"][8:] == [], (
        f"suggestions must be capped at the limit; got {result['suggestions']}"
    )


def test_read_not_found_signals_the_cap_when_it_actually_cuts(tmp_path, monkeypatch):
    """Tier 2: #4431 — a directory with more siblings than the 8-suggestion cap
    must SAY so (``suggestions_truncated`` + ``suggestions_total``), not just
    silently show 8 of 20 with no sign the other 12 exist (owner ruling:
    a silent cap with no config knob needs the loss to be visible instead —
    mirrors the ``glob`` op's own #2998 ``truncated``/``total_count`` fields)."""
    monkeypatch.chdir(tmp_path)
    for i in range(20):
        (tmp_path / f"file{i:02d}.md").write_text("x", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("missing.md"), ctx))

    assert result["status"] == "not_found"
    assert result["suggestions_truncated"] is True
    assert result["suggestions_total"] == 20


def test_read_not_found_no_truncation_signal_when_everything_fits(tmp_path, monkeypatch):
    """Tier 2: accept-side twin — with siblings at or under the cap, no
    truncation actually happened, so ``suggestions_truncated`` must NOT
    appear (a present-but-False flag would still read as "something was
    cut"; only its outright absence says nothing was)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.md").write_text("a", encoding="utf-8")
    (tmp_path / "beta.md").write_text("b", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("missing.md"), ctx))

    assert result["status"] == "not_found"
    assert "suggestions_truncated" not in result
    assert "suggestions_total" not in result


def test_read_ok_still_returns_ok_shape(tmp_path, monkeypatch):
    """Tier 2: existing file read path returns status='ok' with content; no error or suggestions field bloat."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "exists.md").write_text("hello", encoding="utf-8")
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_read("exists.md"), ctx))

    assert result["status"] == "ok"
    assert result["content"] == "hello"
    assert "error" not in result
    assert "suggestions" not in result


# ── edit not_found envelope ────────────────────────────────────────────────────


def test_edit_not_found_returns_error_and_suggestions(tmp_path, monkeypatch):
    """Tier 2: edit_file on a missing file returns the same error+suggestions shape as read."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.py").write_text("pass", encoding="utf-8")
    (tmp_path / "other.py").write_text("pass", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_edit("missing.py", old="foo", new="bar"), ctx))

    assert result["status"] == "not_found"
    assert result["op"] == "edit"
    assert result["path"] == "missing.py"
    assert "error" in result
    assert "not found" in result["error"].lower()
    assert "suggestions" in result
    suggestion_names = {Path(p).name for p in result["suggestions"]}
    assert suggestion_names >= {"existing.py", "other.py"}


def test_edit_existing_file_unchanged(tmp_path, monkeypatch):
    """Tier 2: edit on an existing file follows the existing code path (no error/suggestions injected)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "file.py").write_text("foo bar foo", encoding="utf-8")
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_edit("file.py", old="foo", new="baz"), ctx))

    # old_string appears twice, replace_all not set → error (= existing behaviour)
    assert result["status"] == "error"
    assert result["op"] == "edit"
    # Pre-existing error path doesn't carry our new fields
    assert "suggestions" not in result


# ── parent-dir edge cases ──────────────────────────────────────────────────────


def test_read_not_found_in_nested_missing_dir_suggests_nearest_ancestor(tmp_path, monkeypatch):
    """Tier 2: (#3629) a missing PARENT dir — not just missing siblings — now
    returns the nearest EXISTING ancestor as a suggestion, asserted by VALUE
    (the workspace root itself, since nothing under it exists here), rather
    than the pre-#3629 empty list. This is "no parent", the case a rename,
    move, or plugin reinstall produces — see ``_nearest_existing_ancestor``'s
    docstring (file.py) for why it must be distinguished from "no
    neighbours" (test_read_not_found_in_nested_existing_but_empty_dir,
    below), which legitimately still returns ``[]``."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_read("nonexistent_dir/file.md"), ctx))

    assert result["status"] == "not_found"
    assert result["suggestions"] == ["./"]


def test_read_not_found_in_nested_existing_but_empty_dir(tmp_path, monkeypatch):
    """Tier 2: (#3629) an EXISTING but empty parent dir is "no neighbours",
    not "no parent" — the suggestions list stays legitimately empty rather
    than falling back to an ancestor (that fallback exists to recover a
    missing STRUCTURE, not to pad a genuinely-empty directory)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty_dir").mkdir()
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_read("empty_dir/file.md"), ctx))

    assert result["status"] == "not_found"
    assert result["suggestions"] == []


def test_read_not_found_under_renamed_skill_dir_suggests_current_ancestor(tmp_path, monkeypatch):
    """Tier 2: (#3629 strip-falsify) the reported scenario, reproduced
    directly — a skill directory is renamed (mirroring #3588's
    underscore-to-hyphen shipped-skill rename) and a later read against the
    OLD (now-vanished) path is asked for suggestions.

    Asserted by VALUE: the suggestion names the CURRENT ancestor
    (``skills/``, which still exists and now contains the renamed dir) —
    not merely "suggestions is non-empty". Before the #3629 fix this
    returned ``[]`` (RED — see this module's git history / the PR that
    introduced this test for the pre-fix measurement)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills" / "reyn_cheat_sheet").mkdir(parents=True)
    (tmp_path / "skills" / "reyn_cheat_sheet" / "SKILL.md").write_text("body", encoding="utf-8")
    # The rename #3588 performed: reyn_cheat_sheet -> reyn-cheat-sheet.
    (tmp_path / "skills" / "reyn_cheat_sheet").rename(tmp_path / "skills" / "reyn-cheat-sheet")
    ctx = _make_ctx(tmp_path)

    # A read against the OLD, now-dead path (as an old history entry would
    # still name it — history is immutable, #3629's whole premise).
    result = _run(handle(_read("skills/reyn_cheat_sheet/reference.md"), ctx))

    assert result["status"] == "not_found"
    assert result["suggestions"] == ["skills/"]


def test_read_not_found_in_nested_existing_dir(tmp_path, monkeypatch):
    """Tier 2: parent dir exists with siblings → those siblings are suggested."""
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "sibling1.txt").write_text("a", encoding="utf-8")
    (sub / "sibling2.txt").write_text("b", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("subdir/missing.txt"), ctx))

    assert result["status"] == "not_found"
    suggestion_names = {Path(p).name for p in result["suggestions"]}
    assert suggestion_names >= {"sibling1.txt", "sibling2.txt"}


def test_suggestions_not_starved_by_dirs(tmp_path, monkeypatch):
    """Tier 2: ``Workspace.glob_files`` must not starve file suggestions when
    a parent dir's first entries are directories.

    Regression guard: pre-fix sliced the result list before filtering for
    files, so a parent dir whose first ``max_results`` entries were directories
    produced almost no suggestions.

    With ~10 hidden dirs (matching the project-root case .claude/.git/.github/
    .reyn/.venv/...) and 5 real files, the suggestions must still surface
    the files, not be starved out by the dirs.
    """
    monkeypatch.chdir(tmp_path)
    for i in range(10):
        (tmp_path / f".hiddendir{i:02d}").mkdir()
    for name in ("alpha.md", "beta.md", "gamma.md", "delta.md", "epsilon.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("missing.md"), ctx))

    assert result["status"] == "not_found"
    names = {Path(p).name for p in result["suggestions"]}
    assert names >= {"alpha.md", "beta.md", "gamma.md", "delta.md", "epsilon.md"}
