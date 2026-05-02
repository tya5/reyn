"""Tests for PR36 Layer 3a — op_runtime/file.py read permission gating.

Guards that read-class ops (read, glob, grep) are subject to the same
PermissionResolver checks as write-class ops. Prior to this change,
read ops bypassed the resolver entirely.

Test isolation: each test uses tmp_path + monkeypatch.chdir so all
default-zone checks (CWD-relative) are deterministic.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.file import handle
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import FileIROp
from reyn.workspace.workspace import Workspace


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_ctx(
    tmp_path: Path,
    *,
    permission_resolver: PermissionResolver | None,
    permission_decl: PermissionDecl | None = None,
    skill_name: str = "test_skill",
) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=permission_decl or PermissionDecl(),
        permission_resolver=permission_resolver,
        skill_name=skill_name,
    )


def _resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=False,
    )


def _run(coro):
    return asyncio.run(coro)


def _read_op(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def _glob_op(pattern: str) -> FileIROp:
    return FileIROp(kind="file", op="glob", path=pattern)


def _grep_op(path: str, pattern: str = "x") -> FileIROp:
    return FileIROp(kind="file", op="grep", path=path, pattern=pattern)


def _write_op(path: str) -> FileIROp:
    return FileIROp(kind="file", op="write", path=path, content="hello")


# ── read tests ─────────────────────────────────────────────────────────────────


def test_read_inside_scope_allowed(tmp_path, monkeypatch):
    """read op on a path inside CWD (default read zone) succeeds — no PermissionError.

    The file may not exist, but that should yield status='not_found', not a
    permission error.
    """
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    result = _run(handle(_read_op("src/foo.py"), ctx, "control_ir"))

    # Permission check passed — either ok or not_found, never a PermissionError
    assert result["status"] in ("ok", "not_found")
    assert result["op"] == "read"


def test_read_outside_scope_denied(tmp_path, monkeypatch):
    """read op on an absolute path outside CWD raises PermissionError."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    with pytest.raises(PermissionError, match="read from"):
        _run(handle(_read_op("/etc/passwd"), ctx, "control_ir"))


def test_read_with_no_decl_denied(tmp_path, monkeypatch):
    """Empty PermissionDecl + path outside CWD → PermissionError.

    Even when file.read is not declared at all, the resolver still denies
    paths outside the default read zone.
    """
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver, permission_decl=PermissionDecl())

    with pytest.raises(PermissionError, match="read from"):
        _run(handle(_read_op("/tmp/secret.txt"), ctx, "control_ir"))


def test_read_with_no_resolver_skips_check(tmp_path, monkeypatch):
    """When permission_resolver is None in OpContext, the op-level check is skipped.

    Backward-compatibility: callers that don't supply a resolver get the old
    behaviour — no PermissionError from the op-level gate. The Workspace still
    enforces its own CWD boundary, so we use a path inside CWD to isolate the
    op-level check.
    """
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=None)

    # Path inside CWD: Workspace allows it, op-level check is skipped (no resolver)
    result = _run(handle(_read_op("some/file.txt"), ctx, "control_ir"))
    # File doesn't exist but no PermissionError — op proceeded
    assert result["op"] == "read"
    assert result["status"] in ("ok", "not_found")


def test_read_config_allow_grants_access(tmp_path, monkeypatch):
    """file.read: allow in config satisfies the op-level require_file_read check.

    The op-level check (require_file_read) is satisfied. We use a path inside
    CWD so the Workspace layer also passes, isolating the op-level resolver check.
    """
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path, config={"file.read": "allow"})
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    # Path inside CWD: both op-level and Workspace checks pass
    result = _run(handle(_read_op("some/file.txt"), ctx, "control_ir"))
    assert result["op"] == "read"
    assert result["status"] in ("ok", "not_found")


# ── glob tests ─────────────────────────────────────────────────────────────────


def test_glob_subject_to_read_check(tmp_path, monkeypatch):
    """glob op on a path outside CWD raises PermissionError."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    with pytest.raises(PermissionError, match="read from"):
        _run(handle(_glob_op("/etc/**.conf"), ctx, "control_ir"))


def test_glob_inside_cwd_allowed(tmp_path, monkeypatch):
    """glob inside CWD is allowed without explicit declaration."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    result = _run(handle(_glob_op("**/*.py"), ctx, "control_ir"))
    assert result["op"] == "glob"
    assert result["status"] == "ok"


# ── grep tests ─────────────────────────────────────────────────────────────────


def test_grep_subject_to_read_check(tmp_path, monkeypatch):
    """grep op on a path outside CWD raises PermissionError."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    with pytest.raises(PermissionError, match="read from"):
        _run(handle(_grep_op("/etc"), ctx, "control_ir"))


def test_grep_inside_cwd_allowed(tmp_path, monkeypatch):
    """grep inside CWD is allowed without explicit declaration."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    result = _run(handle(_grep_op(".", pattern="hello"), ctx, "control_ir"))
    assert result["op"] == "grep"
    assert result["status"] == "ok"


# ── write regression ───────────────────────────────────────────────────────────


def test_write_check_unchanged(tmp_path, monkeypatch):
    """Existing write permission check is not broken by the refactor.

    write to a path outside the default write zone (.reyn/, reyn/) that has
    not been approved should still raise PermissionError.
    """
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    # /tmp is outside the default write zone (not under .reyn/ or reyn/)
    with pytest.raises(PermissionError):
        _run(handle(_write_op("/tmp/unexpected_write.txt"), ctx, "control_ir"))


def test_write_inside_default_zone_allowed(tmp_path, monkeypatch):
    """write to .reyn/ directory succeeds — default write zone is preserved."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(tmp_path, permission_resolver=resolver)

    result = _run(handle(_write_op(".reyn/some_artifact.txt"), ctx, "control_ir"))
    assert result["status"] == "ok"
    assert result["op"] == "write"
