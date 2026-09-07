"""Tier 2: OS invariant — #5944 (P0, owner-hit) grep's return has NO upper bound.

owner measured `.reyn/agents/*/history.jsonl` grep calls costing up to 369 MB
in a single response (16 calls, 554.4 MB total) — a `reyn web` startup that
reads this history then peaked at 8.2 GB RSS (#5896/#5851/#5894). Two
independent holes, architect's own discriminator (issue #5944, addendum):
**an op whose single item has a size bound needs only a COUNT cap (glob:
paths, tens of bytes each); an op whose single item has NO size bound also
needs a BYTE cap.** grep returns matched LINES verbatim — a `.reyn/` JSONL
line is a whole conversation message, unbounded — so `head_limit` (a count
cap) alone cannot bound it, however small it is set.

This module pins both halves of the fix:

  (1) `FileIROp.head_limit`'s SCHEMA DEFAULT is now 50 (was `None` = every
      match), matching the sibling `GrepFilesIROp.max_results: int = 50`
      already in this repo — count-axis, defense in depth for a caller that
      forgets to set it.
  (2) `op_runtime/file.py::_execute_grep_sync` applies the SAME shared
      inline byte cap `read`/`load_skill` already use
      (`control_ir_inline_cap`, config-driven via `ctx.read_cap_config`, no
      new ungrounded constant) to each matched line and context line,
      independent of (1) — a FEW oversized matches, well under the count
      cap, still get bounded. This is the actual owner-hit shape (matches
      were few; the 369 MB single hit was ONE oversized line) and the exact
      case lead-coder's brief calls out: "a test built so the count cap
      ALONE does not turn it green" — `test_few_oversized_matches_...`
      below is that test.

When either axis truncates, the result carries a structural signal (never
prose) mirroring #2998's glob precedent (`truncated`/`total_count`/
`returned_count`) plus a byte-specific one (`content_truncated`, and a
`...(+N bytes)` suffix naming exactly how much was cut on the entry
itself) — the same "never silent" contract `file.read`'s own self-bounding
truncation already keeps.

Real `Workspace`/`OpContext`/`handle()` throughout, no mocks — same pattern
`test_read_bounding_structural_signal_1209.py` (the sibling read-bounding
pin) uses for the same shared cap.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.config import ReadCapConfig
from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _make_ctx(tmp_path: Path, *, read_cap_config: ReadCapConfig | None = None) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        actor="test_skill",
        read_cap_config=read_cap_config,
    )


def _run(coro):
    return asyncio.run(coro)


def _write_lines(ctx: OpContext, name: str, lines: list[str]) -> None:
    ctx.workspace.write_file(name, "\n".join(lines) + "\n")


# ── (1) head_limit's schema default is now bounded ───────────────────────────

def test_head_limit_default_is_50_not_unlimited() -> None:
    """Tier 2: `FileIROp.head_limit`'s field default is now `50`, matching
    the sibling `GrepFilesIROp.max_results` precedent. Strip: reverting the
    default back to `None` fails this directly (no scan needed) — the
    fastest possible falsification for hole (1)."""
    op = FileIROp(kind="file", op="grep", pattern="needle", path=".")
    assert op.head_limit == 50


def test_head_limit_default_caps_a_real_scan(tmp_path: Path) -> None:
    """Tier 2: an UNSPECIFIED head_limit on a real grep call — more than 50
    real matches on disk — returns at most 50, with a structural truncation
    signal naming the true total. Strip: `FileIROp.head_limit: int | None =
    None` (hole 1 reopened) → `count` becomes 120, this goes red."""
    ctx = _make_ctx(tmp_path)
    _write_lines(ctx, "many.txt", [f"needle {i}" for i in range(120)])

    res = _run(handle(FileIROp(kind="file", op="grep", pattern="needle", path="many.txt"), ctx))

    assert res["status"] == "ok"
    assert res["count"] == 50
    assert res["truncated"] is True
    assert res["total_count"] == 120
    assert res["returned_count"] == 50


def test_explicit_head_limit_still_honored(tmp_path: Path) -> None:
    """Tier 2: an explicit `head_limit` (smaller or larger than the new
    default) is honored verbatim — the default change does not remove the
    caller's own control."""
    ctx = _make_ctx(tmp_path)
    _write_lines(ctx, "many.txt", [f"needle {i}" for i in range(10)])

    res = _run(handle(
        FileIROp(kind="file", op="grep", pattern="needle", path="many.txt", head_limit=3), ctx,
    ))

    assert res["count"] == 3
    assert res["truncated"] is True
    assert res["total_count"] == 10
    assert res["returned_count"] == 3


# ── (2) per-match BYTE cap — independent of the count axis ───────────────────

def test_few_oversized_matches_bound_total_bytes_not_just_count(tmp_path: Path) -> None:
    """Tier 2: #5944's actual owner-hit shape — a HANDFUL of matches (well
    under head_limit's default 50), each individually far over the byte
    cap, mirroring a `.reyn/` JSONL line (one whole conversation message).

    A fix that added ONLY the count-axis default (hole 1) leaves this RED:
    3 matches is nowhere near 50, so head_limit never fires, yet each match
    alone would carry ~5x the byte cap — the exact "16 calls, 554 MB, one
    call 369 MB" shape from the owner's own measurement. This is the test
    lead-coder's brief named explicitly ("count cap alone must not turn
    this green")."""
    ctx = _make_ctx(tmp_path)
    huge_line = "x" * (MAX_CONTROL_IR_RESULT_INLINE_BYTES * 5)
    _write_lines(ctx, "huge.jsonl", [f"needle {huge_line}" for _ in range(3)])

    res = _run(handle(FileIROp(kind="file", op="grep", pattern="needle", path="huge.jsonl"), ctx))

    assert res["status"] == "ok"
    assert res["count"] == 3          # count axis never fires — well under 50
    assert "truncated" not in res     # so the COUNT truncation signal must not fire either
    assert res["content_truncated"] is True
    for entry in res["matches"]:
        assert len(entry["content"].encode("utf-8")) <= MAX_CONTROL_IR_RESULT_INLINE_BYTES + 32, (
            "a single match's content must stay within the shared inline byte "
            "cap (plus the small literal '...(+N bytes)' suffix) regardless "
            "of how few matches there are"
        )
        assert "...(+" in entry["content"] and " bytes)" in entry["content"], (
            "a truncated match must name exactly how much was cut, never "
            "silently — the same 'never silent' contract file.read keeps"
        )
    # the WHOLE response — the thing that actually lands in `.reyn/` history
    # (#5891/#5896's own concern) — stays proportional to head_limit x cap,
    # not to the huge_line's real size (5x cap x 3 matches, uncapped).
    assert res["returned_bytes"] < MAX_CONTROL_IR_RESULT_INLINE_BYTES * 4


def test_match_content_under_cap_is_returned_verbatim(tmp_path: Path) -> None:
    """Tier 2: ordinary small matches are unaffected — no truncation signal,
    content byte-identical to the source line (accept-side: the fix must
    not degrade the common case)."""
    ctx = _make_ctx(tmp_path)
    _write_lines(ctx, "small.py", ["def f():", "    return needle_here"])

    res = _run(handle(FileIROp(kind="file", op="grep", pattern="needle_here", path="small.py"), ctx))

    assert res["status"] == "ok"
    assert res["count"] == 1
    assert "truncated" not in res
    assert "content_truncated" not in res
    assert res["matches"][0]["content"] == "    return needle_here"


def test_explicit_larger_read_cap_config_returns_full_content(tmp_path: Path) -> None:
    """Tier 2: raising the shared `read_cap_config.inline_bytes` (the same
    knob `read`/`load_skill` already honor) gets the FULL, untruncated
    content back — the default does not remove the ability to opt into
    everything (architect's brief: 'raising the bound explicitly must still
    get every match — this must not kill the feature')."""
    huge_line = "x" * (MAX_CONTROL_IR_RESULT_INLINE_BYTES * 2)
    big_cap = ReadCapConfig(inline_bytes=MAX_CONTROL_IR_RESULT_INLINE_BYTES * 10)
    ctx = _make_ctx(tmp_path, read_cap_config=big_cap)
    _write_lines(ctx, "huge.jsonl", [f"needle {huge_line}"])

    res = _run(handle(FileIROp(kind="file", op="grep", pattern="needle", path="huge.jsonl"), ctx))

    assert "content_truncated" not in res
    assert res["matches"][0]["content"] == f"needle {huge_line}"
