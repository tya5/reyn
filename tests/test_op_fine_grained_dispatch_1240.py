"""Tier 2: #1240 Wave 1 — fine-grained file op kinds dispatch through the
unified registry (the SAME path chat uses), proving the β-seam obviation.

The phase→unified-ToolRegistry pivot (#1240, #1092 prerequisite) rests on one
premise: a phase-emitted *fine* op (``read_file`` / ``write_file`` /
``edit_file``) routes through ``control_ir_executor._invoker`` →
``_registry.lookup(op.kind)`` (a phase=allow ToolDefinition) → the SAME handler
the chat catalog uses (``READ_FILE.handler`` etc., which build a coarse
``FileIROp`` and reuse the single ``op_runtime.file.handle()`` backend). No
separate phase op-universe, no β seam.

These tests PROVE that premise rather than assert it: each fine op produces real
file I/O via a real ``Workspace`` and a real (non-None) ``PermissionResolver``
(#1214 — a None resolver would auto-permit and mask the gate). Green here means
β-obviation is demonstrated end-to-end at the executor layer (recon Q4 confirmed),
de-risking the whole pivot. If a fine kind fell back to the legacy ``execute_op``
skip path (handler_not_implemented), or were silently coarsened to ``file``
before the allow-list check, these would fail.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.permissions.permissions import PermissionResolver
from reyn.schemas.models import EditFileIROp, ReadFileIROp, WriteFileIROp
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
