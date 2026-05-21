"""Tier 2: file__ mkdir / move / stat ops (issue #356).

Three new sub-operations on FileIROp:

  - mkdir: idempotent directory creation under write permission.
  - move:  rename / move; requires write permission on BOTH paths.
  - stat:  metadata read; requires read permission.

These tests pin both happy-path behaviour AND the permission-gate
shape (= outside-CWD paths get denied without an explicit grant).
Uses real Workspace + PermissionResolver — no collaborator mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.file import handle
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import FileIROp
from reyn.workspace.workspace import Workspace


def _make_ctx(
    tmp_path: Path,
    *,
    permission_resolver: PermissionResolver | None,
    permission_decl: PermissionDecl | None = None,
) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=permission_resolver)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=permission_decl or PermissionDecl(),
        permission_resolver=permission_resolver,
        skill_name="test_skill",
    )


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
    )


def _run(coro):
    return asyncio.run(coro)


# ── mkdir ──────────────────────────────────────────────────────────────


def test_mkdir_creates_new_directory(tmp_path, monkeypatch):
    """Tier 2: mkdir creates a directory under the default write zone (.reyn/)
    + reports created=True.
    """
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="mkdir", path=".reyn/newdir/nested")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["created"] is True
    assert (tmp_path / ".reyn" / "newdir" / "nested").is_dir()


def test_mkdir_is_idempotent_when_dir_already_exists(tmp_path, monkeypatch):
    """Tier 2: second mkdir on the same path returns created=False (idempotent)."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="mkdir", path=".reyn/d")
    first = _run(handle(op, ctx, "control_ir"))
    second = _run(handle(op, ctx, "control_ir"))

    assert first["created"] is True
    assert second["status"] == "ok"
    assert second["created"] is False


def test_mkdir_fails_when_path_is_existing_file(tmp_path, monkeypatch):
    """Tier 2: mkdir on a path occupied by a regular file returns status=error."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()
    (tmp_path / ".reyn" / "blocker.txt").write_text("hi")
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="mkdir", path=".reyn/blocker.txt")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "error"
    assert "not a directory" in result["error"]


def test_mkdir_outside_cwd_denied(tmp_path, monkeypatch):
    """Tier 2: mkdir on an absolute path outside CWD raises PermissionError
    (= the same write-zone gate as write/edit/delete).
    """
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="mkdir", path="/tmp/reyn_test_should_be_denied_356")
    with pytest.raises(PermissionError):
        _run(handle(op, ctx, "control_ir"))


# ── move ───────────────────────────────────────────────────────────────


def test_move_renames_existing_file(tmp_path, monkeypatch):
    """Tier 2: move with both paths under the default write zone (.reyn/)
    succeeds.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()
    (tmp_path / ".reyn" / "src.txt").write_text("payload")
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(
        kind="file", op="move",
        path=".reyn/src.txt", dest_path=".reyn/dst.txt",
    )
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["moved"] is True
    assert not (tmp_path / ".reyn" / "src.txt").exists()
    assert (tmp_path / ".reyn" / "dst.txt").read_text() == "payload"


def test_move_returns_not_found_when_source_missing(tmp_path, monkeypatch):
    """Tier 2: move on a non-existent source returns status=not_found cleanly."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(
        kind="file", op="move",
        path=".reyn/missing.txt", dest_path=".reyn/elsewhere.txt",
    )
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "not_found"
    assert "not found" in result["error"]


def test_move_without_dest_path_errors(tmp_path, monkeypatch):
    """Tier 2: move requires dest_path; calling without one returns status=error.

    Permission gate runs first and only checks `op.path`, so the missing
    dest_path bubbles up as an op-level error from the handler (not a
    permission failure).
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()
    (tmp_path / ".reyn" / "src.txt").write_text("payload")
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="move", path=".reyn/src.txt")  # no dest_path
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "error"
    assert "dest_path" in result["error"]


def test_move_dest_outside_zone_denied(tmp_path, monkeypatch):
    """Tier 2: move where dest_path is outside the write zone raises
    PermissionError — the second write-gate (dest) blocks even when source
    is allowed.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()
    (tmp_path / ".reyn" / "src.txt").write_text("payload")
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(
        kind="file", op="move", path=".reyn/src.txt",
        dest_path="/tmp/reyn_test_should_be_denied_356.txt",
    )
    with pytest.raises(PermissionError):
        _run(handle(op, ctx, "control_ir"))


# ── stat ───────────────────────────────────────────────────────────────


def test_stat_returns_metadata_for_existing_file(tmp_path, monkeypatch):
    """Tier 2: stat on an existing file returns size + timestamps + mode."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("hello")
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="stat", path="f.txt")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    info = result["info"]
    assert info["size"] == 5
    assert info["is_file"] is True
    assert info["is_dir"] is False
    assert "mtime" in info and "ctime" in info
    assert info["mode"].startswith("0o")


def test_stat_returns_not_found_when_path_missing(tmp_path, monkeypatch):
    """Tier 2: stat on a missing path returns status=not_found (no exception)."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="stat", path="missing.txt")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "not_found"


def test_stat_outside_cwd_denied(tmp_path, monkeypatch):
    """Tier 2: stat on a path outside CWD raises PermissionError (read-gate)."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="stat", path="/etc/passwd")
    with pytest.raises(PermissionError):
        _run(handle(op, ctx, "control_ir"))


def test_stat_distinguishes_directory_from_file(tmp_path, monkeypatch):
    """Tier 2: stat on a directory returns is_dir=True / is_file=False."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "subdir").mkdir()
    ctx = _make_ctx(tmp_path, permission_resolver=_resolver(tmp_path))

    op = FileIROp(kind="file", op="stat", path="subdir")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["info"]["is_dir"] is True
    assert result["info"]["is_file"] is False
