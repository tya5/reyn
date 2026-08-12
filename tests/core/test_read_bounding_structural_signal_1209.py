"""Tier 2: OS invariant — #1209 read-bounding + structural signaling.

The fixed 8 KB control_ir inline cap (`context_builder.py`) offloaded `file.read`
content out of the editing model's decide context, starving the apply phase
(astropy-13236: the model fabricated `old_string`s for a file it could not see).
This pins the OS behavior fix:

  (cap) the per-result inline cap is a RESOURCE BOUND (#4381 PR-5: bytes,
        model-independent, config-driven — NOT window-derived; #1209's
        original window-derivation was itself the defect architect's
        resource/budget role-split later closed);
  (1)   an UNBOUNDED `file.read` over the cap is truncated to a head window with a
        STRUCTURAL truncation signal in SEPARATE fields (not embedded in content),
        bound-only-when-over (small reads + explicit offset/limit unchanged).

#2396 Step 4: point (2) of the original pin — that an offloaded control_ir_result carries an
explicit `_offload_status` flag — was dropped along with `offload_control_ir_result` itself (dead,
retired in #2396 Step 4; its last caller, the ContextFrame-driven phase path, was removed by earlier
convergence steps). `control_ir_inline_cap` — the resource-bound cap this file still pins — survives
as the shared read-bounding cap consulted by `file.py` / `load_skill.py`.

Real Workspace + EventLog, no collaborator mocks; cap helper tested as a pure
function. Behavior is at the shared OS op layer (consistent for chat/planner/phase).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.config import ReadCapConfig
from reyn.core.context_builder import (
    MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    control_ir_inline_cap,
)
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        actor="test_skill",
    )


def _run(coro):
    return asyncio.run(coro)


# ── cap helper: config-driven, model-independent (#4381 PR-5) ────────────────

def test_inline_cap_is_model_independent() -> None:
    """Tier 2: #4381 PR-5 (owner ruling) — the cap no longer takes a model at
    all; an explicit ``ReadCapConfig`` is honored VERBATIM, with no
    window-style scaling applied anywhere in this path."""
    cap = control_ir_inline_cap(ReadCapConfig(inline_bytes=999_999))
    assert cap == 999_999, f"an explicit config value must be returned verbatim, got {cap}"


def test_inline_cap_none_is_the_shipped_default() -> None:
    """Tier 2: no ``ReadCapConfig`` threaded to the caller → the shipped
    model-independent default (backward-compat for a direct-OpContext
    construction with no ReynConfig, e.g. many tests in this module)."""
    assert control_ir_inline_cap(None) == MAX_CONTROL_IR_RESULT_INLINE_BYTES


def test_inline_cap_honors_a_small_explicit_config_with_no_floor_clamp() -> None:
    """Tier 2: accept-side — an operator-set SMALL cap (e.g. tighter than the
    shipped default) is NOT floor-clamped back up. #1209's original
    window-derivation floored the cap at 8 KB so a large-window model could
    never go below it; #4381 PR-5 removed that clamp along with the
    window-derivation itself — the cap is now whatever config says, full
    stop (an operator choosing a smaller value is a deliberate choice, not
    a value this layer second-guesses; ``_build_read_cap_config``'s own
    positive-value guard is the only clamp left, and it only rejects
    zero/negative, not "small")."""
    small = ReadCapConfig(inline_bytes=100)
    assert control_ir_inline_cap(small) == 100


# ── (1) read-bounding: bound-only-when-over, structural fields separate ──────

def test_unbounded_read_over_cap_truncates_with_structural_fields(tmp_path: Path) -> None:
    """Tier 2: an unbounded read over the cap → status=truncated + separate signal fields.

    ctx has no resolver → cap = the 8 KB floor; a >8 KB file read (no offset/limit)
    is truncated to a head window with shown_lines/total_lines/next_offset/total_chars
    as SEPARATE keys, and the content carries no embedded truncation marker.
    """
    ctx = _make_ctx(tmp_path)
    big = "".join(f"line {i} ................................................\n" for i in range(400))
    # #4381 PR-5: the cap is BYTES; this fixture is pure ASCII so char count
    # == byte count, but the comparison is written byte-explicit (not
    # len(str)) so it stays correct if this fixture ever grows non-ASCII
    # content.
    assert len(big.encode("utf-8")) > MAX_CONTROL_IR_RESULT_INLINE_BYTES  # ensure over the cap
    ctx.workspace.write_file("big.py", big)

    res = _run(handle(FileIROp(kind="file", op="read", path="big.py"), ctx))

    assert res["status"] == "truncated"
    assert res["shown_lines"] < res["total_lines"] == 400
    assert res["next_offset"] == res["shown_lines"]
    assert res["total_chars"] == len(big)
    # structural signal is in separate fields, NOT embedded in the content text
    assert "TRUNCATED" not in res["content"]
    assert len(res["content"].encode("utf-8")) <= MAX_CONTROL_IR_RESULT_INLINE_BYTES


def test_unbounded_read_under_cap_returns_full_ok(tmp_path: Path) -> None:
    """Tier 2: a small unbounded read is unchanged (status=ok, full content, no signal)."""
    ctx = _make_ctx(tmp_path)
    small = "def f():\n    return 1\n"
    ctx.workspace.write_file("small.py", small)

    res = _run(handle(FileIROp(kind="file", op="read", path="small.py"), ctx))

    assert res["status"] == "ok"
    assert res["content"] == small
    assert "shown_lines" not in res and "next_offset" not in res


def test_explicit_offset_limit_honored_verbatim(tmp_path: Path) -> None:
    """Tier 2: an explicit offset/limit window bypasses auto read-bounding (honored as-is)."""
    ctx = _make_ctx(tmp_path)
    big = "".join(f"line {i}\n" for i in range(1000))
    ctx.workspace.write_file("big.py", big)

    res = _run(handle(FileIROp(kind="file", op="read", path="big.py", offset=10, limit=5), ctx))

    assert res["status"] == "ok"  # explicit window → not auto-truncated
    assert res["content"] == "".join(f"line {i}\n" for i in range(10, 15))
    assert "shown_lines" not in res
