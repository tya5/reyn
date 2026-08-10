"""Tier 2: file_read truncation is LLM-visible (an explicit truncation marker).

Owner steer: a file_read's source already exists on disk, so offloading a duplicate copy is wasteful
— truncate inline instead, and make the truncation RECOGNIZABLE to the LLM (an explicit
``_truncated`` marker + a plain ``note`` pointing at the on-disk path + a re-read offset hint) so the
model knows it holds a PART and can re-read the original file. Real Workspace + real ``handle``
(no mocks).

#2396 Step 4: the generic control_ir offload (``offload_control_ir_result``) this file's
"never-offload-duplicates" test used to falsify against was retired — its last caller (the
ContextFrame-driven phase path) was removed by earlier convergence steps, so there is no longer a
generic offload for a self-bounded read to be exempt from. #3334 therefore removed the
``_self_bounded`` stamp that marked that exemption; what this file pins instead is the property the
stamp only *claimed* — the returned ``content`` is genuinely ≤ the inline cap.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES as CAP


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
    assert len(res["content"]) <= CAP, "the truncated read is genuinely bounded by the inline cap"
    assert res["next_offset"] is not None, "a re-read continuation offset is provided"
    note = res["note"]
    assert "big.txt" in note, "the note names the on-disk source path"
    assert "offset" in note and "truncated" in note, "the note tells the LLM it is partial + how to continue"


def test_small_read_has_no_truncation_marker_no_regression(tmp_path, monkeypatch):
    """Tier 2: a small file read is complete — no ``_truncated`` / ``note`` fields (the common path
    stays clean)."""
    monkeypatch.chdir(tmp_path)
    res = _read(tmp_path, "hello = 1\nworld = 2\n")

    assert res["status"] == "ok"
    assert "_truncated" not in res and "note" not in res, "no truncation marker on a complete read"
