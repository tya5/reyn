"""Tier 2: file_read truncation is LLM-visible and never offload-duplicates the on-disk source.

Owner steer: a file_read's source already exists on disk, so offloading a duplicate copy is wasteful
— truncate inline instead, and make the truncation RECOGNIZABLE to the LLM (an explicit
``_truncated`` marker + a plain ``note`` pointing at the on-disk path + a re-read offset hint) so the
model knows it holds a PART and can re-read the original file. Real Workspace + real ``handle`` +
real ``offload_control_ir_result`` (no mocks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.context_builder import offload_control_ir_result


def _read(tmp_path: Path, text: str, *, offset: int | None = None, limit: int | None = None) -> dict:
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.file import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import FileIROp
    from reyn.security.permissions.permissions import PermissionDecl

    (tmp_path / "big.txt").write_text(text, encoding="utf-8")
    events = EventLog()
    ctx = OpContext(
        workspace=Workspace(base_dir=tmp_path, events=events),
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
    )
    op = FileIROp(kind="file", op="read", path="big.txt", offset=offset, limit=limit)
    return asyncio.run(handle(op, ctx))


def test_large_read_is_truncated_with_llm_visible_marker(tmp_path, monkeypatch):
    """Tier 2: CORE — a large (unbounded) file_read is truncated and carries an LLM-visible marker:
    ``_truncated`` + a ``note`` that names the on-disk path and the re-read offset. RED before the
    marker: no ``_truncated`` / ``note`` fields."""
    monkeypatch.chdir(tmp_path)
    res = _read(tmp_path, "some line of text here\n" * 50000)

    assert res["status"] == "truncated"
    assert res["_truncated"] is True, "explicit LLM-visible truncation marker"
    assert res["_self_bounded"] is True, "truncated read is self-bounded (offload-exempt)"
    assert res["next_offset"] is not None, "a re-read continuation offset is provided"
    note = res["note"]
    assert "big.txt" in note, "the note names the on-disk source path"
    assert "offset" in note and "truncated" in note, "the note tells the LLM it is partial + how to continue"


def test_truncated_read_is_not_offloaded_no_duplicate(tmp_path, monkeypatch):
    """Tier 2: a truncated file_read is NOT offloaded — no duplicate copy of an on-disk file (owner
    steer). The generic offload returns it unchanged (self-bounded exempt) and writes no file."""
    monkeypatch.chdir(tmp_path)
    res = _read(tmp_path, "some line of text here\n" * 50000)

    out = offload_control_ir_result(res, 0, tmp_path, cap=200)
    assert "_offload_ref" not in out, "a self-bounded file_read is never offloaded"
    offload_dir = tmp_path / ".reyn" / "control_ir_offload"
    assert not offload_dir.exists() or not list(offload_dir.glob("*")), "no duplicate offload file written"


def test_small_read_has_no_truncation_marker_no_regression(tmp_path, monkeypatch):
    """Tier 2: a small file read is complete — no ``_truncated`` / ``note`` fields (the common path
    stays clean)."""
    monkeypatch.chdir(tmp_path)
    res = _read(tmp_path, "hello = 1\nworld = 2\n")

    assert res["status"] == "ok"
    assert "_truncated" not in res and "note" not in res, "no truncation marker on a complete read"
