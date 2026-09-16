"""Tier 2: #6205 — a #6184 regression: a tool-call row's trailing
``()`` was drawn even when NOTHING remained inside it, once ``subject``
(段3-2/3-3/3-4) consumed a single-param tool's only remaining ``k=v``
pair.

Symptom (issue #6205, verbatim, real code run against `origin/main`
`2256bd972`): ``read_file docs/…/x.md ()`` — a trailing empty pair the
reader has no reason to trust means anything.

Fix: ONE rule in ``core/present/tool_head.py``'s ``compose_tool_head``
— empty parens are never drawn, full stop. NOT "only when a subject is
present" (lead-coder's own explicit ruling, #6205: a special case
closes this ONE hole and reopens on the next path that reaches zero
args a different way).

Accept — 3 (issue #6205's own table, verbatim):
⑴ a tool whose subject consumed its only arg draws no trailing ``()``
   (``read_file``).
⑵ a tool with args remaining still draws them (``exec``'s own
   ``(timeout=120)``) — the DENY-side sibling: without it, "never draw
   parens" would pass ⑴ just as well as the real, conditional rule
   does.
⑶ a tool that never took an arg ALSO draws no parens (``list_plugins``)
   — the witness that the rule is genuinely ONE rule, not gated on
   ``subject`` being present: an implementation that special-cased
   "drop parens only when subject is set" would still draw
   ``list_plugins()`` here and fail this test.

Covers all 3 known consumer sites (#6184 段3-3's own population,
#6193's corrected count) — ``presenter.py``'s ``_tool_head`` and
``_collapsed_retrieval_line``, ``renderer.py``'s
``format_inline_message``. A 4th consumer site (one that builds a
tool-call head line WITHOUT going through ``compose_tool_head``) was
searched for and not found — reported on the issue, not fixed here
(out of this PR's own scope per dispatch).
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
    console = Console(width=200, file=io.StringIO(), color_system=None)
    console.print(format_inline_message(msg))
    return console.file.getvalue()


# ---------------------------------------------------------------------------
# ⑴ subject consumes the only arg -- no trailing ()
# ---------------------------------------------------------------------------


def test_tool_head_no_trailing_empty_parens_when_subject_consumes_only_arg() -> None:
    """Tier 2: accept ⑴, presenter.py's _tool_head — the exact #6205
    repro (`read_file`'s own `path`, declared as its subject in 段3-4)."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
        subject="docs/x.md",
    )
    out = _tool_head(msg).plain
    assert out == "read_file docs/x.md"
    assert "(" not in out and ")" not in out


def test_format_inline_message_no_trailing_empty_parens_when_subject_consumes_only_arg() -> None:
    """Tier 2: accept ⑴, renderer.py's format_inline_message."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
        subject="docs/x.md",
    )
    out = _plain_inline(msg)
    assert "()" not in out
    assert "read_file docs/x.md" in out


def test_collapsed_retrieval_line_no_trailing_empty_parens_when_subject_consumes_only_arg() -> None:
    """Tier 2: accept ⑴, presenter.py's _collapsed_retrieval_line."""
    meta = {
        "tool": "search_knowledge",
        "args": {"query": "q"},
        _RESULT_KIND_KEY: "tool_call_completed",
        _RESULT_META_KEY: {"result": {"results": [1, 2, 3]}},
    }
    msg = OutboxMessage(
        kind="tool_call_completed", text="search_knowledge", meta=meta, subject="q",
    )
    out = _collapsed_retrieval_line(msg)
    assert out is not None
    assert "()" not in out.plain
    assert out.plain == "search_knowledge q → 3 results"


# ---------------------------------------------------------------------------
# ⑵ deny side -- args remaining still draw the parens (exec)
# ---------------------------------------------------------------------------


def test_tool_head_args_remaining_still_draw_parens() -> None:
    """Tier 2: accept ⑵ (deny side) — exec's own `timeout` survives
    alongside its subject (`cmd`); without this test, "never draw
    parens" would pass accept ⑴ just as well as the real conditional
    rule does."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "ls", "timeout": 120}},
        subject="ls",
    )
    out = _tool_head(msg).plain
    assert out == "exec ls (timeout=120)"


def test_format_inline_message_args_remaining_still_draw_parens() -> None:
    """Tier 2: accept ⑵ (deny side), renderer.py's format_inline_message."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "ls", "timeout": 120}},
        subject="ls",
    )
    out = _plain_inline(msg)
    assert "(timeout=120)" in out


# ---------------------------------------------------------------------------
# ⑶ a tool that never takes an arg also draws no parens (single-rule witness)
# ---------------------------------------------------------------------------


def test_tool_head_no_args_tool_draws_no_parens_even_without_a_subject() -> None:
    """Tier 2: accept ⑶ — `list_plugins` (no declared subject, no args
    at all) draws no parens either. This is the witness that the fix is
    ONE rule ("no args left -> no parens"), not a special case gated on
    `subject` being set — an implementation reading "drop parens only
    when subject is present" would still draw `list_plugins()` here and
    fail this test (lead-coder's own explicit ruling, #6205)."""
    msg = OutboxMessage(
        kind="tool_call_started", text="list_plugins",
        meta={"tool": "list_plugins", "args": {}},
    )
    assert msg.subject is None
    out = _tool_head(msg).plain
    assert out == "list_plugins"
    assert "(" not in out


def test_format_inline_message_no_args_tool_draws_no_parens_even_without_a_subject() -> None:
    """Tier 2: accept ⑶, renderer.py's format_inline_message."""
    msg = OutboxMessage(
        kind="tool_call_started", text="list_plugins",
        meta={"tool": "list_plugins", "args": {}},
    )
    out = _plain_inline(msg)
    assert "list_plugins()" not in out
    assert "list_plugins" in out
