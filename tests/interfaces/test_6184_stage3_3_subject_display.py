"""Tier 2: #6184 段3-3 — the consumer finally DRAWS the tool-call row from
``msg.subject`` (populated by the producer since 段3-2, #6200, but never
read until now). The only stage in this arc where the SCREEN changes.

Covers the 3 sites architect named (issuecomment-5689016062, #6193's own
corrected population): ``presenter.py``'s ``_tool_head`` and
``_collapsed_retrieval_line``, and ``renderer.py``'s
``format_inline_message`` (``tool_call_started`` branch) — all 3 now
route through the ONE new public mouth, ``core/present/tool_head.py``'s
``compose_tool_head``.

Accept, per architect's own 6-item table (issuecomment-5689016062):
⑴ subject outside/before the parens, nothing dropped (permission
   evidence: ``exec`` with ``network=True``).
⑵ the subject's own arg is excluded from the ``k=v`` listing.
⑶ a subject-less row is byte-identical to the pre-3-3 (段2b) shape.
⑷ HISTORICAL — width overflow cut the trailing OPTION, never the
   subject. #6208 R3 (architect's own retraction, issue #6208 thread)
   WITHDRAWS this: the subject is now cut too (matching every other
   displayed value's own per-value budget), once R3's call-level detail
   view (Space) gave the full text somewhere to still be read. See
   ``test_tool_head_width_overflow_cuts_the_subject_too_now`` below for
   the CURRENT behavior this accept item's own test now pins, and its
   docstring for the full retraction reasoning.
⑸ ESC disappears from BOTH the subject and the args (single boundary).
⑹ ``get_neutralizer(`` is not spread across the 3 consumer sites.

This PR closes #6193 (3 sites never routed through the neutralizer at
all) — ⑸/⑹ are that closure's own witness.

7th question (lead-coder's own standing instruction, issuecomment-
5689050164): does a LATER #6184 stage intend to falsify any assertion
here? Checked — 段4-A/4-B (dispatched_tool_calls count, default-collapse
predicate) touch neither `subject` nor the tool-head compose path; no
assertion in this file was expected to go stale on purpose by a stage
already dispatched AT THE TIME this file was written. #6208 (a LATER,
not-yet-dispatched arc at that time) intentionally invalidates ⑷ — see
its own updated entry above and the retired test's replacement below.
"""
from __future__ import annotations

import inspect
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
# ⑴ subject outside/before the parens; nothing dropped (permission evidence)
# ---------------------------------------------------------------------------


def test_tool_head_subject_is_outside_and_before_the_parens_network_survives() -> None:
    """Tier 2: accept ⑴, presenter.py's _tool_head. `network=True` is a
    PERMISSION-axis arg — dropping it (a "reverted to the monopoly shape"
    regression) would silently erase evidence CLAUDE.md's own "does the
    repair destroy the evidence?" question names."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "echo hi", "network": True}},
        subject="echo hi",
    )
    out = _tool_head(msg).plain
    assert out == "exec echo hi (network=True)"
    # Non-vacuity: the subject is not just present anywhere, it precedes "(".
    assert out.index("echo hi") < out.index("(")
    assert "network=True" in out


def test_format_inline_message_subject_is_outside_and_before_the_parens() -> None:
    """Tier 2: accept ⑴, renderer.py's format_inline_message."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "echo hi", "network": True}},
        subject="echo hi",
    )
    out = _plain_inline(msg)
    assert "echo hi (network=True)" in out
    assert out.index("echo hi") < out.index("(")


# ---------------------------------------------------------------------------
# ⑵ the subject's own arg is excluded from the k=v listing
# ---------------------------------------------------------------------------


def test_tool_head_subject_arg_excluded_from_kv_not_shown_twice() -> None:
    """Tier 2: accept ⑵ — `cmd`'s own value is the subject; it must not
    ALSO appear as `cmd=echo hi` in the k=v listing."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "echo hi", "network": True}},
        subject="echo hi",
    )
    out = _tool_head(msg).plain
    assert "cmd=" not in out
    assert out.count("echo hi") == 1


def test_collapsed_retrieval_line_subject_arg_excluded_from_kv() -> None:
    """Tier 2: accept ⑵, presenter.py's _collapsed_retrieval_line —
    exec is not itself a retrieval tool, so this test declares its OWN
    synthetic subject_params-bearing scenario is out of reach for this
    function in production; instead this asserts the exclusion is a
    genuine no-op (not a crash, not an over-exclusion) when the tool
    declares no subject_params at all — the real population today for
    every retrieval tool (#6184 段3-1's own accept ④: 0 declarations
    besides exec)."""
    meta = {
        "tool": "search_knowledge",
        "args": {"query": "q"},
        _RESULT_KIND_KEY: "tool_call_completed",
        _RESULT_META_KEY: {"result": {"results": [1, 2, 3]}},
    }
    msg = OutboxMessage(kind="tool_call_completed", text="search_knowledge", meta=meta)
    out = _collapsed_retrieval_line(msg)
    assert out is not None
    assert "query=q" in out.plain


# ---------------------------------------------------------------------------
# ⑶ a subject-less row is byte-identical to the pre-3-3 (段2b) shape
# ---------------------------------------------------------------------------


def test_tool_head_no_subject_matches_pre_3_3_shape() -> None:
    """Tier 2: accept ⑶ — a message with no declared subject (the
    overwhelming majority of tools, 段3-1's own accept ④) renders the
    EXACT pre-3-3 string: no leading space, no parens change."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
    )
    assert msg.subject is None
    assert _tool_head(msg).plain == "read_file(path=docs/x.md)"


def test_format_inline_message_no_subject_matches_pre_3_3_shape() -> None:
    """Tier 2: accept ⑶, renderer.py's format_inline_message."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file",
        meta={"tool": "read_file", "args": {"path": "docs/x.md"}},
    )
    out = _plain_inline(msg)
    assert "read_file(path=docs/x.md)" in out
    assert " (path=docs/x.md)" not in out  # no stray leading space either


def test_collapsed_retrieval_line_no_subject_matches_pre_3_3_shape() -> None:
    """Tier 2: accept ⑶, presenter.py's _collapsed_retrieval_line."""
    meta = {
        "tool": "search_knowledge",
        "args": {"query": "q"},
        _RESULT_KIND_KEY: "tool_call_completed",
        _RESULT_META_KEY: {"result": {"results": [1, 2, 3]}},
    }
    msg = OutboxMessage(kind="tool_call_completed", text="search_knowledge", meta=meta)
    out = _collapsed_retrieval_line(msg)
    assert out is not None
    assert out.plain == "search_knowledge(query=q) → 3 results"


# ---------------------------------------------------------------------------
# ⑷ width overflow cuts the trailing OPTION, never the subject
# ---------------------------------------------------------------------------


def test_tool_head_width_overflow_cuts_the_subject_too_now() -> None:
    """Tier 2: accept ⑷, RETRACTED AND REVERSED — #6208 R3 (architect's
    own retraction, issue #6208 thread, verbatim): the original accept
    ④ premise ("a subject cut mid-string would defeat the owner's ask
    for the command line as the display's CENTER") implicitly assumed
    cutting it would throw the full text away with nowhere left to read
    it. R3's own call-level detail view (Space) removes that premise —
    the full, uncut subject is always one keypress away — so the
    conclusion built on it no longer holds either.

    A long subject now gets the SAME `…` cut every other displayed
    value gets (no longer a privileged exception); a trailing option
    still keeps its own cut, unaffected. Strip-falsify witness: if a
    future change reverted the subject cut (restored accept ④'s old
    shape), the FIRST assertion below goes red — the long subject would
    appear in full again."""
    long_subject = "python " + "x" * 100
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": long_subject, "extra_option": "y" * 80}},
        subject=long_subject,
    )
    out = _tool_head(msg).plain
    assert long_subject not in out, (
        "the subject must be cut now (#6208 R3 withdrew accept ④) -- "
        "if this is failing, the withdrawal was reverted"
    )
    assert "y" * 80 not in out
    assert out.count("…") == 2, (
        "BOTH the cut subject and the cut trailing option must carry "
        "their own witness -- exactly one `…` each"
    )


def test_tool_head_a_short_subject_is_unaffected_by_the_new_cut() -> None:
    """Tier 2: accept ⑷'s own deny side — a subject that already fits
    inside the new per-value budget is byte-identical to before #6208
    R3 (this file's own pre-existing short-subject tests, e.g.
    ``test_tool_head_subject_is_outside_and_before_the_parens_network_
    survives``, already cover this implicitly with `"echo hi"`; this
    test names the boundary explicitly so a future width-budget change
    has a dedicated witness for "nothing changes when nothing needed
    cutting")."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "echo hi", "network": True}},
        subject="echo hi",
    )
    out = _tool_head(msg).plain
    assert "echo hi" in out
    assert "…" not in out


def test_tool_head_width_overflow_never_drops_a_kv_pair_network_survives() -> None:
    """Tier 2: #6208 R3 accept③ (lead-coder's own verbatim requirement,
    issue #6208 dispatch: "network が概要行から消えない" — the
    permission axis must survive width overflow, never fold into a
    silent omission). A long subject triggers the cut above; the
    SIBLING `network=True` k=v pair, evidence CLAUDE.md's own "does the
    repair destroy the evidence?" question names, must still be present
    and unclipped (short enough to never need its own per-value cut)."""
    long_subject = "python " + "x" * 100
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": long_subject, "network": True}},
        subject=long_subject,
    )
    out = _tool_head(msg).plain
    assert "network=True" in out


# ---------------------------------------------------------------------------
# ⑸ ESC disappears from BOTH the subject and the args (single boundary)
# ---------------------------------------------------------------------------


def test_tool_head_esc_stripped_from_both_subject_and_args() -> None:
    """Tier 2: accept ⑸ — an ESC byte in EITHER half must be gone. If
    the single-boundary compose degraded back to per-branch neutralize
    calls that missed one half, this catches it (the exact shape #6193
    describes)."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "echo\x1b[31m hi", "extra": "y\x1b[0mz"}},
        subject="echo\x1b[31m hi",
    )
    out = _tool_head(msg).plain
    assert "\x1b" not in out


def test_collapsed_retrieval_line_esc_stripped_from_both_subject_and_args() -> None:
    """Tier 2: accept ⑸, presenter.py's _collapsed_retrieval_line."""
    meta = {
        "tool": "search_knowledge",
        "args": {"query": "q\x1b[31m"},
        _RESULT_KIND_KEY: "tool_call_completed",
        _RESULT_META_KEY: {"result": {"results": [1]}},
    }
    msg = OutboxMessage(
        kind="tool_call_completed", text="search_knowledge", meta=meta, subject="s\x1b[0mub",
    )
    out = _collapsed_retrieval_line(msg)
    assert out is not None
    assert "\x1b" not in out.plain


def test_format_inline_message_esc_stripped_from_both_subject_and_args() -> None:
    """Tier 2: accept ⑸, renderer.py's format_inline_message."""
    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={"tool": "exec", "args": {"cmd": "echo\x1b[31m hi", "extra": "y\x1b[0mz"}},
        subject="echo\x1b[31m hi",
    )
    out = _plain_inline(msg)
    assert "\x1b" not in out


# ---------------------------------------------------------------------------
# ⑹ get_neutralizer( is not spread across the 3 consumer sites (#6193)
# ---------------------------------------------------------------------------


def test_get_neutralizer_call_not_spread_across_the_3_consumer_sites() -> None:
    """Tier 2: accept ⑹ — grep-shaped witness, applied to the FUNCTION
    SOURCE of each of the 3 consumer sites (not the whole module, which
    legitimately calls `get_neutralizer` elsewhere for unrelated
    purposes — labels, result summaries, error text). None of the 3
    tool-head sites may call it directly; the ONE call site is
    `core/present/tool_head.py`'s own `compose_tool_head`."""
    from reyn.core.present import tool_head as tool_head_module
    from reyn.interfaces.inline.textual_chat import presenter as presenter_module
    from reyn.interfaces.repl import renderer as renderer_module

    tool_head_src = inspect.getsource(tool_head_module.compose_tool_head)
    assert tool_head_src.count("get_neutralizer(") == 1, (
        "compose_tool_head is meant to be the ONE call site -- found "
        f"{tool_head_src.count('get_neutralizer(')} occurrences"
    )

    for fn in (presenter_module._tool_head, presenter_module._collapsed_retrieval_line):
        src = inspect.getsource(fn)
        assert "get_neutralizer(" not in src, (
            f"{fn.__name__} must not call get_neutralizer directly -- route "
            "through compose_tool_head instead (#6193, accept ⑥)"
        )

    # format_inline_message is one big function with several kind branches;
    # scope the check to the tool_call_started branch's own source slice
    # (between its `if kind == "tool_call_started":` and the next `if
    # kind ==`), not the whole function (other branches legitimately call
    # get_neutralizer for unrelated purposes, e.g. tool_call_failed's error text).
    full_src = inspect.getsource(renderer_module.format_inline_message)
    start = full_src.index('if kind == "tool_call_started":')
    end = full_src.index('if kind == "tool_call_completed":', start)
    branch_src = full_src[start:end]
    assert "get_neutralizer(" not in branch_src, (
        "format_inline_message's tool_call_started branch must not call "
        "get_neutralizer directly -- route through compose_tool_head instead "
        "(#6193, accept ⑥)"
    )
