"""Tier 2: #1240 Wave 1 + Wave 1.5 — fine-grained file op kinds dispatch
through the unified registry (the SAME path chat uses), proving the β-seam
obviation.

The phase→unified-ToolRegistry pivot (#1240, #1092 prerequisite) rests on one
premise: a phase-emitted *fine* op (``read_file`` / ``write_file`` /
``edit_file`` / ``glob_files`` / ``grep_files``) routes through
``control_ir_executor._invoker`` → ``_registry.lookup(op.kind)`` (a phase=allow
ToolDefinition) → the SAME handler the chat catalog uses (``READ_FILE.handler``
etc., which build a coarse ``FileIROp`` and reuse the single
``op_runtime.file.handle()`` backend). No separate phase op-universe, no β seam.

These tests PROVE that premise rather than assert it: each fine op produces real
file I/O via a real ``Workspace`` and a real (non-None) ``PermissionResolver``
(#1214 — a None resolver would auto-permit and mask the gate). Green here means
β-obviation is demonstrated end-to-end at the executor layer (recon Q4 confirmed),
de-risking the whole pivot. If a fine kind fell back to the legacy ``execute_op``
skip path (handler_not_implemented), or were silently coarsened to ``file``
before the allow-list check, these would fail.

Wave 1.5 adds ``test_fine_glob_files_dispatch_via_registry`` and
``test_fine_grep_files_dispatch_via_registry`` extending the same proof to the
glob/grep search ops.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.permissions.permissions import PermissionResolver
from reyn.schemas.models import (
    EditFileIROp,
    GlobFilesIROp,
    GrepFilesIROp,
    ReadFileIROp,
    WriteFileIROp,
)
from reyn.workspace.workspace import Workspace


def _executor(tmp_path: Path, *, grant: bool) -> ControlIRExecutor:
    """Real ControlIRExecutor with a real Workspace + real PermissionResolver.

    ``grant=True`` declares file.read/file.write allow (the skill-declared
    permission); ``grant=False`` declares nothing, so the real resolver denies
    (NOT a None resolver, which would auto-permit and mask the gate — #1214).
    """
    events = EventLog()
    ws = Workspace(events=events)
    resolver = PermissionResolver(
        config_permissions=(
            {"file.read": "allow", "file.write": "allow"} if grant else {}
        ),
        project_root=tmp_path,
        interactive=False,
    )
    return ControlIRExecutor(
        ws, events,
        permission_resolver=resolver,
        skill_name="test_skill",
        chain_id="c1",
        skill_run_id="run_001",
    )


def _ok(result: dict) -> bool:
    """A non-error, non-skipped, non-denied op result."""
    return result.get("status") not in ("error", "denied", "skipped")


def test_fine_write_then_read_dispatch_via_registry(tmp_path, monkeypatch):
    """Tier 2: fine write_file + read_file execute via the registry handler
    (same path as chat) with real file I/O — the β-obviation proof.

    ``allowed_ops`` uses the FINE name only (no coarse "file"), so a green
    result also proves the op dispatches AS write_file/read_file and is not
    coarsened to "file" before the allow-list / catalog check (recon Q4
    "no fine→coarse collapse").
    """
    monkeypatch.chdir(tmp_path)
    executor = _executor(tmp_path, grant=True)

    w = asyncio.run(executor.execute(
        [WriteFileIROp(kind="write_file", path="out.txt", content="hello pivot")],
        phase="draft", allowed_ops={"write_file"},
    ))
    assert w and _ok(w[0]), f"fine write_file must execute (not skip/deny): {w}"
    # Ground truth: the registry handler → file.handle() backend actually wrote.
    # A legacy skip (handler_not_implemented) or a coarsen-then-reject would
    # leave no file here.
    assert (tmp_path / "out.txt").read_text() == "hello pivot"

    r = asyncio.run(executor.execute(
        [ReadFileIROp(kind="read_file", path="out.txt")],
        phase="draft", allowed_ops={"read_file"},
    ))
    assert r and _ok(r[0]), f"fine read_file must execute: {r}"
    # The fine read returned what the fine write produced — unified backend.
    assert "hello pivot" in str(r[0])


def test_fine_edit_file_dispatch_via_registry(tmp_path, monkeypatch):
    """Tier 2: fine edit_file routes via the registry handler + applies a real
    unique-string edit (the #1240 edit_file addition)."""
    monkeypatch.chdir(tmp_path)
    executor = _executor(tmp_path, grant=True)
    (tmp_path / "doc.txt").write_text("alpha beta gamma")

    e = asyncio.run(executor.execute(
        [EditFileIROp(
            kind="edit_file", path="doc.txt",
            old_string="beta", new_string="DELTA",
        )],
        phase="draft", allowed_ops={"edit_file"},
    ))
    assert e and _ok(e[0]), f"fine edit_file must execute: {e}"
    assert (tmp_path / "doc.txt").read_text() == "alpha DELTA gamma"


def test_fine_write_real_resolver_denies_without_grant(tmp_path, monkeypatch):
    """Tier 2: the real (non-None) PermissionResolver actually gates the fine op
    — with NO file.write declaration the write is denied, not auto-permitted
    (#1214 falsification).

    The fine write_file traverses the SAME ``require_file_write`` gate the coarse
    op does (inside the shared ``file.handle()`` backend). A None resolver would
    let this slip; the deny proves the gate is live on the fine path.
    """
    monkeypatch.chdir(tmp_path)
    executor = _executor(tmp_path, grant=False)

    res = asyncio.run(executor.execute(
        [WriteFileIROp(kind="write_file", path="denied.txt", content="leak")],
        phase="draft", allowed_ops={"write_file"},
    ))
    assert res and not _ok(res[0]), (
        f"ungranted fine write must be denied by the real resolver: {res}"
    )
    assert not (tmp_path / "denied.txt").exists(), (
        "a denied write must not touch the filesystem"
    )


# ── #1240 Wave 1.5: glob_files / grep_files β-obviation proof ────────────────


def test_fine_glob_files_dispatch_via_registry(tmp_path, monkeypatch):
    """Tier 2: fine glob_files dispatches via the registry handler (GLOB_FILES
    ToolDefinition, phase=allow) → _handle_glob → coarse FileIROp(op=glob) →
    file.handle() backend — the same path chat uses.

    ``allowed_ops`` uses the FINE name only (``glob_files``, no coarse ``file``),
    so a green result also proves no fine→coarse collapse before the allow-list /
    catalog check (#1240 Wave 1.5, same Q4 proof as Wave 1 ops above).
    """
    monkeypatch.chdir(tmp_path)
    executor = _executor(tmp_path, grant=True)

    # Write a couple of files — one .py, one .txt — so we can verify the glob
    # only surfaces the .py file via the real file.handle() backend.
    (tmp_path / "a.py").write_text("print('hello')")
    (tmp_path / "b.txt").write_text("not python")

    result = asyncio.run(executor.execute(
        [GlobFilesIROp(kind="glob_files", path=".", pattern="*.py")],
        phase="draft", allowed_ops={"glob_files"},
    ))
    assert result and _ok(result[0]), (
        f"fine glob_files must execute (not skip/deny): {result}"
    )
    # The handler normalises output to {pattern, matches, count} (or a
    # superset); the real glob must surface a.py.
    result_str = str(result[0])
    assert "a.py" in result_str, (
        f"glob_files result must include a.py: {result[0]}"
    )
    assert "b.txt" not in result_str, (
        f"glob_files with *.py pattern must not include b.txt: {result[0]}"
    )


def test_fine_grep_files_dispatch_via_registry(tmp_path, monkeypatch):
    """Tier 2: fine grep_files dispatches via the registry handler (GREP_FILES
    ToolDefinition, phase=allow) → _handle_grep → coarse FileIROp(op=grep) →
    file.handle() backend — the same path chat uses.

    ``allowed_ops`` uses the FINE name only (``grep_files``, no coarse ``file``),
    proving no fine→coarse collapse. The grep must surface the matching line
    from real file I/O (not a stub).
    """
    monkeypatch.chdir(tmp_path)
    executor = _executor(tmp_path, grant=True)

    # Write a file with a distinctive string we can grep for.
    (tmp_path / "source.py").write_text(
        "def wave15_marker():\n    return 'glob_grep_proof'\n"
    )

    result = asyncio.run(executor.execute(
        [GrepFilesIROp(kind="grep_files", path=".", pattern="wave15_marker")],
        phase="draft", allowed_ops={"grep_files"},
    ))
    assert result and _ok(result[0]), (
        f"fine grep_files must execute (not skip/deny): {result}"
    )
    # The real grep must find the match in source.py.
    result_str = str(result[0])
    assert "wave15_marker" in result_str, (
        f"grep_files result must include the matched string: {result[0]}"
    )
