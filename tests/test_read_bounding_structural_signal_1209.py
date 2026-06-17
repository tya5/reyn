"""Tier 2: OS invariant — #1209 read-bounding + window-derived offload cap + structural signaling.

The fixed 8 KB control_ir inline cap (`context_builder.py`) offloaded `file.read`
content out of the editing model's decide context, starving the apply phase
(astropy-13236: the model fabricated `old_string`s for a file it could not see).
This pins the OS behavior fix:

  (cap) the per-result inline cap is WINDOW-DERIVED (floored at 8 KB), so a normal
        file read stays inline on a large window instead of being offloaded;
  (1)   an UNBOUNDED `file.read` over the cap is truncated to a head window with a
        STRUCTURAL truncation signal in SEPARATE fields (not embedded in content),
        bound-only-when-over (small reads + explicit offset/limit unchanged);
  (2)   an offloaded result carries an explicit `_offload_status` flag.

Real Workspace + EventLog, no collaborator mocks; cap helper tested as a pure
function. Behavior is at the shared OS op layer (consistent for chat/planner/phase).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.context_builder import (
    MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    control_ir_inline_cap,
    offload_control_ir_result,
)
from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.file import handle
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        skill_name="test_skill",
    )


def _run(coro):
    return asyncio.run(coro)


# ── cap helper: window-derive + floor ───────────────────────────────────────

def test_inline_cap_window_derived_above_floor() -> None:
    """Tier 2: a known large-window model derives a cap well above the 8 KB floor."""
    cap = control_ir_inline_cap("gemini/gemini-2.5-flash-lite")
    assert cap > MAX_CONTROL_IR_RESULT_INLINE_BYTES, (
        f"window-derived cap should exceed the 8 KB floor, got {cap}"
    )
    # 1M-token window × 4 chars/token × 0.08 ≈ 320 KB → a 150 KB file stays inline.
    assert cap >= 150_000, f"cap should keep a 150 KB file inline, got {cap}"


def test_inline_cap_none_is_floor() -> None:
    """Tier 2: no model context → the fixed 8 KB floor (backward-compat)."""
    assert control_ir_inline_cap(None) == MAX_CONTROL_IR_RESULT_INLINE_BYTES


def test_inline_cap_floor_guard_for_unknown_model() -> None:
    """Tier 2: an unrecognized model never derives below the floor."""
    assert control_ir_inline_cap("nonexistent/model-xyz") >= MAX_CONTROL_IR_RESULT_INLINE_BYTES


# ── (1) read-bounding: bound-only-when-over, structural fields separate ──────

def test_unbounded_read_over_cap_truncates_with_structural_fields(tmp_path: Path) -> None:
    """Tier 2: an unbounded read over the cap → status=truncated + separate signal fields.

    ctx has no resolver → cap = the 8 KB floor; a >8 KB file read (no offset/limit)
    is truncated to a head window with shown_lines/total_lines/next_offset/total_chars
    as SEPARATE keys, and the content carries no embedded truncation marker.
    """
    ctx = _make_ctx(tmp_path)
    big = "".join(f"line {i} ................................................\n" for i in range(400))
    assert len(big) > MAX_CONTROL_IR_RESULT_INLINE_BYTES  # ensure over the floor cap
    ctx.workspace.write_file("big.py", big)

    res = _run(handle(FileIROp(kind="file", op="read", path="big.py"), ctx, "control_ir"))

    assert res["status"] == "truncated"
    assert res["shown_lines"] < res["total_lines"] == 400
    assert res["next_offset"] == res["shown_lines"]
    assert res["total_chars"] == len(big)
    # structural signal is in separate fields, NOT embedded in the content text
    assert "TRUNCATED" not in res["content"]
    assert len(res["content"]) <= MAX_CONTROL_IR_RESULT_INLINE_BYTES


def test_unbounded_read_under_cap_returns_full_ok(tmp_path: Path) -> None:
    """Tier 2: a small unbounded read is unchanged (status=ok, full content, no signal)."""
    ctx = _make_ctx(tmp_path)
    small = "def f():\n    return 1\n"
    ctx.workspace.write_file("small.py", small)

    res = _run(handle(FileIROp(kind="file", op="read", path="small.py"), ctx, "control_ir"))

    assert res["status"] == "ok"
    assert res["content"] == small
    assert "shown_lines" not in res and "next_offset" not in res


def test_explicit_offset_limit_honored_verbatim(tmp_path: Path) -> None:
    """Tier 2: an explicit offset/limit window bypasses auto read-bounding (honored as-is)."""
    ctx = _make_ctx(tmp_path)
    big = "".join(f"line {i}\n" for i in range(1000))
    ctx.workspace.write_file("big.py", big)

    res = _run(handle(FileIROp(kind="file", op="read", path="big.py", offset=10, limit=5), ctx, "control_ir"))

    assert res["status"] == "ok"  # explicit window → not auto-truncated
    assert res["content"] == "".join(f"line {i}\n" for i in range(10, 15))
    assert "shown_lines" not in res


# ── (2) offload structural status flag + window-derived trigger ──────────────

def test_offload_carries_structural_status_flag(tmp_path: Path) -> None:
    """Tier 2: a result over the cap is offloaded with an explicit `_offload_status` flag."""
    big_result = {"kind": "file", "op": "read", "content": "x" * 50_000}
    inline = offload_control_ir_result(
        big_result, 0, tmp_path, cap=MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    )
    assert inline.get("_offload_status") == "truncated"
    assert inline.get("_offload_total_chars") >= 50_000
    assert "_offload_ref" in inline


def test_offload_trigger_uses_window_derived_cap(tmp_path: Path) -> None:
    """Tier 2: the same 50 KB result offloads under the floor cap but stays INLINE under a high cap."""
    big_result = {"kind": "file", "op": "read", "content": "x" * 50_000}

    offloaded = offload_control_ir_result(
        big_result, 0, tmp_path, cap=MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    )
    assert offloaded.get("_offload_status") == "truncated"  # over 8 KB floor → offloaded

    inline = offload_control_ir_result(big_result, 1, tmp_path, cap=200_000)
    assert inline is big_result  # under the high (window-derived) cap → identity, stays inline
