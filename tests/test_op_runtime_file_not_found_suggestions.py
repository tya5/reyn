"""Tests for file__read / file__edit not_found envelope shape.

When a file doesn't exist, the op result includes an ``error`` string and a
``suggestions`` list of sibling files under the same parent — matching the
shape of invoke_action's UnknownActionError so the LLM produces "did you mean
X" narration for missing files the same way it does for missing actions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.file import handle
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
        skill_name="test_skill",
    )


def _read(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def _edit(path: str, *, old: str = "x", new: str = "y") -> FileIROp:
    return FileIROp(kind="file", op="edit", path=path, old_string=old, new_string=new)


def _run(coro):
    return asyncio.run(coro)


# ── read not_found envelope ────────────────────────────────────────────────────


def test_read_not_found_returns_error_and_suggestions(tmp_path, monkeypatch):
    """Tier 2: file__read of a missing file in a populated parent dir returns
    status='not_found' plus ``error`` string and ``suggestions`` list."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "beta.md").write_text("beta", encoding="utf-8")
    (tmp_path / "gamma.md").write_text("gamma", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("nonexistent.md"), ctx, "control_ir"))

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

    result = _run(handle(_read("nowhere.md"), ctx, "control_ir"))

    assert result["status"] == "not_found"
    assert result["suggestions"] == []


def test_read_not_found_capped_at_limit(tmp_path, monkeypatch):
    """Tier 2: suggestions list does not exceed _NOT_FOUND_SUGGESTIONS_LIMIT (8)."""
    monkeypatch.chdir(tmp_path)
    for i in range(20):
        (tmp_path / f"file{i:02d}.md").write_text("x", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("missing.md"), ctx, "control_ir"))

    assert result["status"] == "not_found"
    assert result["suggestions"], "suggestions must be non-empty when siblings exist"
    # Cap is enforced — the full set of 20 files is not returned.
    # _NOT_FOUND_SUGGESTIONS_LIMIT == 8, so suggestions[8:] must be empty.
    assert result["suggestions"][8:] == [], (
        f"suggestions must be capped at the limit; got {result['suggestions']}"
    )


def test_read_ok_still_returns_ok_shape(tmp_path, monkeypatch):
    """Tier 2: existing file read path returns status='ok' with content; no error or suggestions field bloat."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "exists.md").write_text("hello", encoding="utf-8")
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_read("exists.md"), ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["content"] == "hello"
    assert "error" not in result
    assert "suggestions" not in result


# ── edit not_found envelope ────────────────────────────────────────────────────


def test_edit_not_found_returns_error_and_suggestions(tmp_path, monkeypatch):
    """Tier 2: file__edit on a missing file returns the same error+suggestions shape as read."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.py").write_text("pass", encoding="utf-8")
    (tmp_path / "other.py").write_text("pass", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_edit("missing.py", old="foo", new="bar"), ctx, "control_ir"))

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

    result = _run(handle(_edit("file.py", old="foo", new="baz"), ctx, "control_ir"))

    # old_string appears twice, replace_all not set → error (= existing behaviour)
    assert result["status"] == "error"
    assert result["op"] == "edit"
    # Pre-existing error path doesn't carry our new fields
    assert "suggestions" not in result


# ── parent-dir edge cases ──────────────────────────────────────────────────────


def test_read_not_found_in_nested_missing_dir(tmp_path, monkeypatch):
    """Tier 2: missing parent dir → suggestions empty, no crash."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)

    result = _run(handle(_read("nonexistent_dir/file.md"), ctx, "control_ir"))

    assert result["status"] == "not_found"
    assert result["suggestions"] == []


def test_read_not_found_in_nested_existing_dir(tmp_path, monkeypatch):
    """Tier 2: parent dir exists with siblings → those siblings are suggested."""
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "sibling1.txt").write_text("a", encoding="utf-8")
    (sub / "sibling2.txt").write_text("b", encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    result = _run(handle(_read("subdir/missing.txt"), ctx, "control_ir"))

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
    result = _run(handle(_read("missing.md"), ctx, "control_ir"))

    assert result["status"] == "not_found"
    names = {Path(p).name for p in result["suggestions"]}
    assert names >= {"alpha.md", "beta.md", "gamma.md", "delta.md", "epsilon.md"}
