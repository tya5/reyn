"""Tier 1/2: #6184 段2b-2 — compose (assembly + normalization) moves to the
producer side (``lifecycle_forwarder._enqueue_tool_call``), the ONE call
site. Display is UNCHANGED (accept criterion ④: the consumer,
``interfaces/repl/renderer.py``, is not touched by this stage at all — no
line in it changed).

**`text` is wire-only. The TUI never reads it.** ``interfaces/repl/
renderer.py`` draws its own display from ``meta["args"]``/``meta["result"]``
(``tool`` bold, ``(args)`` dim) via its own #6184 段2b-1 split
(``_compose_args``/``_truncate_args``) — reading the flat ``text`` this
stage adds would mean parsing an already-formatted string back apart into
its pieces, exactly the shape this arc's own census (dispatch-table
producer/consumer duplication) already closed elsewhere.

Home: ``core/present/`` — architect design (#6184 issuecomment-5683686664),
lead-coder ruling (issuecomment-5683701917) — an EXISTING neutral home both
``runtime`` (``core/op_runtime/*``) and ``interfaces`` (renderer/presenter/
sent_queue) already import from, not newly created.
``src/reyn/runtime/lifecycle_forwarder.py`` did NOT previously import from
``core/present/`` — this stage adds that ONE new cross-layer import edge
(``from reyn.core.present.tool_call_compose import ...``).

Design corrected TWICE mid-implementation (architect, issuecomment-
5683762265 — disclosed here since a first version of this file's own
production code briefly had both mistakes, caught before commit, not
after):

A. ``details`` carries the STRUCTURED compose result
   (``list[tuple[str, str]] | str`` — the SAME shape
   ``interfaces/repl/renderer.py``'s own ``_compose_args`` already
   returns, #6184 段2b-1), never a pre-joined string — a future consumer
   needs the per-value boundaries intact to apply its own per-value cut
   (``_truncate_args``'s own docstring: "each value is cut to
   ``value_width`` BEFORE joining").
B. ``text`` gets NO length cut (a producer does not know a future
   viewer's terminal width — width stays a consumer/viewer concern,
   #6184 裁定) — only a transport-safety CAP via
   ``core/present/guard.py``'s pre-existing ``cap_leaf``.
C. Control-character removal does NOT touch ``renderer.py``'s own
   ``_normalize_text`` (#6184 段2b-1's own accept criterion was a
   BYTE-IDENTICAL split — adding ESC-removal there could change that
   function's existing output for ESC-bearing input, undoing a property
   段2b-1 already established) — reimplemented independently in
   ``core/present/tool_call_compose.py`` instead, applied to both
   ``details``'s composed values and the final flat ``text``.

Real ``ChatLifecycleForwarder``/``OutboxMessage``/the real
``compose_tool_call_args``/``compose_tool_call_text`` throughout
(CLAUDE.md mock ban).
"""
from __future__ import annotations

import asyncio

from reyn.core.present.tool_call_compose import (
    compose_tool_call_args,
    compose_tool_call_text,
)
from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder


def _forwarder() -> "tuple[ChatLifecycleForwarder, asyncio.Queue]":
    outbox: asyncio.Queue = asyncio.Queue()
    return ChatLifecycleForwarder(outbox=outbox), outbox


def test_meta_args_is_preserved_additively_not_replaced():
    """Tier 2: accept criterion — ``meta["args"]`` (the consumer's own,
    unchanged read path) survives byte-identical; ``text``/``details``
    are ADDED, never a replacement for anything the consumer already
    reads."""
    fwd, outbox = _forwarder()
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "read_file",
        "chain_id": "c1", "args": {"path": "/x", "n": 5}, "args_hash": "h1",
    })
    msg = outbox.get_nowait()
    assert msg.meta.get("args") == {"path": "/x", "n": 5}
    assert msg.meta.get("tool") == "read_file"  # unchanged consumer read path


def test_text_is_flat_composed_form_with_no_length_cut():
    """Tier 1: correction B — text carries the full, un-cut compose
    result (no value_width=24/total_width=60 baked in, unlike the
    consumer's own _truncate_args)."""
    long_value = "x" * 100  # well past _truncate_args's own 24/60 widths
    fwd, outbox = _forwarder()
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "read_file",
        "chain_id": "c1", "args": {"path": long_value}, "args_hash": "h1",
    })
    msg = outbox.get_nowait()
    assert long_value in msg.text, "text must not be length-cut by the producer"


def test_details_carries_structured_not_joined_compose_result():
    """Tier 1: correction A, the accept criterion's own witness — details
    must be a structured list of (key, value) pairs, never a pre-joined
    string (which would make a future per-value cut unrecoverable)."""
    fwd, outbox = _forwarder()
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "read_file",
        "chain_id": "c1", "args": {"path": "/x", "n": 5}, "args_hash": "h1",
    })
    msg = outbox.get_nowait()
    args_detail = msg.details.get("args")
    assert isinstance(args_detail, list), (
        "details['args'] must be a structured list, not a joined string"
    )
    assert args_detail == [("path", "/x"), ("n", "5")]


def test_tool_call_completed_and_failed_degrade_to_bare_tool_name():
    """Tier 2: dispatcher.py's tool_returned/tool_failed events never
    carry args (only args_hash) — text/details must degrade to the bare
    tool name / empty structure automatically, with no per-kind branch
    in the producer, and this must be BYTE-IDENTICAL to the pre-#6184
    段2b-2 text (bare tool name)."""
    fwd, outbox = _forwarder()
    fwd.on_tool_called({
        "caller_kind": "router", "caller_id": "r1", "tool": "read_file",
        "chain_id": "c1", "args": {"path": "/x"}, "args_hash": "h1",
    })
    outbox.get_nowait()  # drain the started row

    fwd.on_tool_returned({
        "caller_kind": "router", "caller_id": "r1", "tool": "read_file",
        "chain_id": "c1", "args_hash": "h1", "result": {"ok": True},
    })
    completed = outbox.get_nowait()
    assert completed.text == "read_file"
    assert completed.details == {"args": []}

    fwd.on_tool_failed({
        "caller_kind": "router", "caller_id": "r1", "tool": "read_file",
        "chain_id": "c1", "args_hash": "h1", "error_kind": "exception",
        "message": "boom",
    })
    failed = outbox.get_nowait()
    assert failed.text == "read_file"
    assert failed.details == {"args": []}


def test_control_characters_are_stripped_in_both_text_and_details():
    """Tier 1: correction C — control-char removal applies to BOTH the
    composed details values and the final flat text (not routed through
    renderer.py's own _normalize_text, which #6184 段2b-1 must keep
    byte-identical)."""
    composed = compose_tool_call_args({"path": "a\tb\x07\x1b[31mred"})
    assert "\x07" not in composed[0][1]
    assert "\x1b" not in composed[0][1]

    text = compose_tool_call_text("tool\x1b[31m", composed)
    assert "\x1b" not in text
    assert "\x07" not in text


def test_a_huge_value_is_capped_not_silently_unbounded():
    """Tier 1: correction B's other half — cap (a transport safety bound)
    IS applied, distinct from width (which is not). A value well past
    guard.py's own MAX_LEAF_CHARS default must not reach an unbounded
    wire text."""
    composed = compose_tool_call_args({"x": "y" * 50_000})
    text = compose_tool_call_text("t", composed)
    # The cap tail is the semantic signal a cap actually fired — checking
    # this rather than a numeric length keeps the assertion about BEHAVIOR
    # (was it capped) not a pinned size/shape (test_tier_audit.py Tier 4).
    assert "full data in the ref" in text


def test_compose_tool_call_args_matches_renderers_own_compose_shape():
    """Tier 1: structural equivalence witness — the producer's own
    compose_tool_call_args returns the SAME shape
    interfaces/repl/renderer.py's own _compose_args returns for an
    equivalent input (an ordered list of (key, value) pairs for a dict,
    a bare string otherwise) — read independently here (not by
    importing renderer.py, which stays untouched) so a future drift
    between the two compose implementations has a witness."""
    from reyn.interfaces.repl.renderer import _compose_args

    args = {"path": "/x", "n": 5}
    producer_side = compose_tool_call_args(args)
    consumer_side = _compose_args(args)
    assert [tuple(p) for p in producer_side] == [tuple(c) for c in consumer_side]
