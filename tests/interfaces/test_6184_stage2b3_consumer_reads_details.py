"""Tier 2: #6184 段2b-3 — the consumer now reads the producer's own
structured ``details["args"]`` when present, instead of always
recomposing from the raw wire ``meta["args"]``. Covers all 3 call sites
lead-coder's own dispatch named: ``presenter.py``'s ``_tool_head`` (:297)
and ``_collapsed_retrieval_line`` (:527), and ``renderer.py``'s
``format_inline_message`` tool_call_started branch (:1128).

Accept is EQUIVALENCE, not permission (lead-coder's own framing) — a
mismatch is the defect, not a design choice:
⑴ equivalence — a `details["args"]`-bearing message renders the SAME
  final string a `meta["args"]`-only message with the same logical args
  would.
⑵ `details` absent (old-frame / restore / client-composed messages) —
  falls back to `_compose_args(meta["args"])`, monotonically, matching
  the pre-2b-3 behavior exactly. (Covered structurally by every OTHER
  existing tool-row test in this test suite, which constructs no
  `details` at all — none needed changing for this PR, which is itself
  part of ⑵'s own witness: the fallback path is unchanged code.)
⑶ the guard: a per-value cut (24 chars) still fires when reading
  through `details["args"]` — if the 2b-3 wiring accidentally JOINED
  `details["args"]` into a flat string before truncating (losing the
  per-value boundary `_truncate_args` needs), this goes RED.
⑷ the guard: `details["args"] == []` (a REAL, producer-declared "no
  args" fact) renders as `""` even when the SAME message's
  `meta["args"]` is non-empty — this is the `in`-vs-truthiness witness:
  writing the 3 call sites' guard as `if details.get("args"):` instead
  of `if "args" in details:` would silently prefer the (wrong, stale)
  `meta["args"]` here, since `[]` is falsy. Present tense: this shape is
  synthetic (no real producer disagrees with itself this way), but it is
  exactly the shape a truthiness regression would fail to distinguish
  from ⑵'s "details absent" case — both read as falsy/empty, and only
  `in` tells them apart.
"""
from __future__ import annotations

import io

from rich.console import Console

from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_KIND_KEY as _RESULT_KIND_KEY
from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_META_KEY as _RESULT_META_KEY
from reyn.interfaces.inline.textual_chat.presenter import (
    _collapsed_retrieval_line,
    _tool_head,
)
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage


def _plain_inline(msg: OutboxMessage) -> str:
    console = Console(width=120, file=io.StringIO(), color_system=None)
    console.print(format_inline_message(msg))
    return console.file.getvalue()


# ---------------------------------------------------------------------------
# renderer.py: format_inline_message's tool_call_started branch (:1128)
# ---------------------------------------------------------------------------


def test_renderer_equivalence_details_args_matches_meta_args() -> None:
    """Tier 2: ⑴: a message carrying BOTH `details["args"]` (composed, the shape
    `_compose_args` itself would have produced) and a logically-matching
    `meta["args"]` renders the identical final line either way."""
    from_meta = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
    )
    from_details = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
        details={"args": [("path", "docs/x.md")]},
    )
    assert _plain_inline(from_meta) == _plain_inline(from_details)


def test_renderer_details_absent_falls_back_to_meta_args() -> None:
    """Tier 2: ⑵: no `details` at all (the pre-2b-3 shape every OTHER existing
    tool-row test in this suite already exercises) — unaffected."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
    )
    out = _plain_inline(msg)
    assert "docs/x.md" in out


def test_renderer_per_value_cut_fires_through_details() -> None:
    """Tier 2: ⑶ (guard): a single value over 24 chars in `details["args"]`
    is still cut — proves `_truncate_args` (not a flat join) processes the
    details path."""
    long_value = "x" * 40
    msg = OutboxMessage(
        kind="tool_call_started", text="grep_files",
        meta={"tool": "grep_files"},
        details={"args": [("pattern", long_value)]},
    )
    out = _plain_inline(msg)
    assert "…" in out
    assert long_value not in out


def test_renderer_empty_details_args_wins_over_nonempty_meta_args() -> None:
    """Tier 2: ⑷ (guard): `details["args"] == []` renders as no-args, even though
    the SAME message's `meta["args"]` is non-empty — proves the 3 call
    sites test presence with `in`, never truthiness (`[]` is falsy;
    `.get("args")`/`if details.get("args")` would silently fall through
    to `meta["args"]` here, which is exactly the wrong, stale value)."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/should-not-appear.md"}},
        details={"args": []},
    )
    out = _plain_inline(msg)
    assert "should-not-appear" not in out


# ---------------------------------------------------------------------------
# presenter.py: _tool_head (:297)
# ---------------------------------------------------------------------------


def test_tool_head_equivalence_details_args_matches_meta_args() -> None:
    """Tier 2: ⑴, presenter.py's _tool_head."""
    from_meta = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
    )
    from_details = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
        details={"args": [("path", "docs/x.md")]},
    )
    assert _tool_head(from_meta).plain == _tool_head(from_details).plain


def test_tool_head_per_value_cut_fires_through_details() -> None:
    """Tier 2: ⑶ (guard), presenter.py's _tool_head."""
    long_value = "y" * 40
    msg = OutboxMessage(
        kind="tool_call_started", text="grep_files",
        meta={"tool": "grep_files"},
        details={"args": [("pattern", long_value)]},
    )
    out = _tool_head(msg).plain
    assert "…" in out
    assert long_value not in out


def test_tool_head_empty_details_args_wins_over_nonempty_meta_args() -> None:
    """Tier 2: ⑷ (guard), presenter.py's _tool_head."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/should-not-appear.md"}},
        details={"args": []},
    )
    out = _tool_head(msg).plain
    assert "should-not-appear" not in out


# ---------------------------------------------------------------------------
# presenter.py: _collapsed_retrieval_line (:527)
# ---------------------------------------------------------------------------


def _retrieval_msg(*, args_meta: dict, details: "dict | None" = None) -> OutboxMessage:
    meta = {
        "tool": "search_knowledge",
        "args": args_meta,
        _RESULT_KIND_KEY: "tool_call_completed",
        _RESULT_META_KEY: {"result": {"results": [1, 2, 3]}},
    }
    return OutboxMessage(
        kind="tool_call_completed", text="search_knowledge", meta=meta,
        details=details or {},
    )


def test_collapsed_retrieval_line_equivalence_details_args_matches_meta_args() -> None:
    """Tier 2: ⑴, presenter.py's _collapsed_retrieval_line."""
    from_meta = _retrieval_msg(args_meta={"query": "q"})
    from_details = _retrieval_msg(
        args_meta={"query": "q"}, details={"args": [("query", "q")]},
    )
    a = _collapsed_retrieval_line(from_meta)
    b = _collapsed_retrieval_line(from_details)
    assert a is not None and b is not None
    assert a.plain == b.plain


def test_collapsed_retrieval_line_per_value_cut_fires_through_details() -> None:
    """Tier 2: ⑶ (guard), presenter.py's _collapsed_retrieval_line."""
    long_value = "z" * 40
    msg = _retrieval_msg(
        args_meta={}, details={"args": [("query", long_value)]},
    )
    out = _collapsed_retrieval_line(msg)
    assert out is not None
    assert "…" in out.plain
    assert long_value not in out.plain


def test_collapsed_retrieval_line_empty_details_args_wins_over_nonempty_meta_args() -> None:
    """Tier 2: ⑷ (guard), presenter.py's _collapsed_retrieval_line."""
    msg = _retrieval_msg(
        args_meta={"query": "should-not-appear"}, details={"args": []},
    )
    out = _collapsed_retrieval_line(msg)
    assert out is not None
    assert "should-not-appear" not in out.plain
