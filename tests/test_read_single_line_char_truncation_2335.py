"""Tier 2: #2335 — a single line exceeding the inline cap is CHAR-truncated (honest _self_bounded).

#2296 exempts self-bounded reads from the generic offload, trusting `_self_bounded` = "content ≤ cap
by construction". But the line-based read-bounding always included the FIRST line whole, so a single
line > cap → content > cap yet `_self_bounded: True` (dishonest) → the huge line ESCAPED offload =
context bloat. Fix: char-truncate the overflowing line, page its tail via (next_offset,
next_char_offset). The LLM's line-based offset/limit contract is preserved (char_offset is edge-only).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES as CAP
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _ctx(tmp_path: Path) -> OpContext:
    ev = EventLog()
    return OpContext(
        workspace=Workspace(events=ev, base_dir=tmp_path),
        events=ev, permission_decl=PermissionDecl(), skill_name="t",
    )


def _read(tmp_path: Path, **kw) -> dict:
    return asyncio.run(handle(FileIROp(kind="file", op="read", **kw), _ctx(tmp_path), "control_ir"))


def test_single_line_over_cap_is_char_truncated_and_honest(tmp_path: Path):
    """Tier 2: a file whose FIRST line alone exceeds the cap → content is CHAR-truncated to ≤ cap
    (honest `_self_bounded`), NOT included whole. RED without the fix (content > cap yet
    `_self_bounded` True = the dishonest state #2296's tests missed)."""
    huge = "x" * (CAP + 5000)  # a single line (no newline), well over the 8 KB floor cap
    (tmp_path / "min.js").write_text(huge)
    res = _read(tmp_path, path="min.js")
    assert res["status"] == "truncated"
    assert res["_self_bounded"] is True
    assert len(res["content"]) <= CAP, "content must be GENUINELY ≤ cap (honest self-bounded)"
    assert "next_char_offset" in res, "a char-truncated line pages its tail via next_char_offset"
    assert res["total_chars"] == len(huge), "total_chars reports the full size"


def test_single_line_tail_round_trips_via_char_offset(tmp_path: Path):
    """Tier 2: the truncated line's TAIL is fully recovered by follow-up reads at (next_offset,
    next_char_offset) — the round-trip, no data loss."""
    huge = "".join(f"{i:06d}" for i in range(6000))  # one 36000-char line (no newlines)
    (tmp_path / "data.txt").write_text(huge)
    page = _read(tmp_path, path="data.txt")
    assert page["status"] == "truncated" and "next_char_offset" in page
    recovered = page["content"]
    for _ in range(500):  # bounded-by-construction: char_offset advances monotonically
        if page["status"] != "truncated" or "next_char_offset" not in page:
            break
        page = _read(tmp_path, path="data.txt", offset=page["next_offset"], char_offset=page["next_char_offset"])
        recovered += page["content"]
    assert recovered == huge, "the full single line must be recoverable via char-offset paging (no tail loss)"


def test_multi_line_over_cap_unchanged(tmp_path: Path):
    """Tier 2: regression — a multi-line file over cap stops at the LINE boundary (pre-#2335), with
    whole lines and NO next_char_offset (the line-based continuation contract, byte-identical)."""
    lines = ["line%04d " % i + "y" * 100 + "\n" for i in range(300)]  # ~110 chars each, many lines
    (tmp_path / "multi.txt").write_text("".join(lines))
    res = _read(tmp_path, path="multi.txt")
    assert res["status"] == "truncated"
    assert "next_char_offset" not in res, "multi-line overflow stops at a LINE boundary — no mid-line resume"
    assert res["content"] == "".join(lines[: res["shown_lines"]]), "whole lines only (byte-identical to pre-#2335)"
    assert len(res["content"]) <= CAP


def test_small_explicit_line_window_stays_verbatim(tmp_path: Path):
    """Tier 2: a SMALL explicit LINE window (≤ cap) is honored VERBATIM — byte-identical, not
    self-bounded (the common #2335 line-read contract)."""
    (tmp_path / "s.txt").write_text("l0\nl1\nl2\n")
    res = _read(tmp_path, path="s.txt", offset=1, limit=1)  # just line 1
    assert res["status"] == "ok"
    assert res["content"] == "l1\n"
    assert "_self_bounded" not in res, "a small explicit window is verbatim, not self-bounded"


def test_oversized_explicit_line_window_is_truncated_not_offloaded(tmp_path: Path):
    """Tier 2: owner steer — file_read never offload-duplicates its (on-disk) source. An explicit
    LINE window whose slice EXCEEDS the cap is now SELF-BOUNDED (truncated + LLM-visible marker +
    on-disk path + re-read hint), NOT returned verbatim for the generic offload. Supersedes the prior
    #2335/#2296 verbatim-then-offload contract."""
    huge_line = "z" * (CAP + 5000)
    (tmp_path / "f.txt").write_text("a\n" + huge_line + "\nb\n")
    res = _read(tmp_path, path="f.txt", offset=1, limit=1)  # the huge line
    assert res["status"] == "truncated"
    assert res["_self_bounded"] is True, "an oversized explicit window is self-bounded (no offload copy)"
    assert res["_truncated"] is True, "LLM-visible truncation marker"
    assert len(res["content"]) <= CAP, "content is cut to fit the cap (full source stays on disk)"
    assert res["path"] == "f.txt", "the on-disk source path is surfaced for re-read"


def test_small_read_unchanged(tmp_path: Path):
    """Tier 2: a small file (≤ cap) is returned whole, self-bounded, no truncation fields (the
    common path, byte-identical)."""
    (tmp_path / "small.py").write_text("hello = 1\nworld = 2\n")
    res = _read(tmp_path, path="small.py")
    assert res["status"] == "ok"
    assert res["content"] == "hello = 1\nworld = 2\n"
    assert res["_self_bounded"] is True
    assert "next_offset" not in res and "next_char_offset" not in res
