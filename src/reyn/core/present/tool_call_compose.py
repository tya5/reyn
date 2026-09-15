"""Producer-side compose for a tool call's outbox wire fields — #6184 段2b-2.

**`text` is wire-only. The TUI never reads it as its PRIMARY value** — the
consumer (repl's own ``renderer.py``) already draws its own display from
the structured ``meta["args"]``/``meta["result"]`` fields — ``tool``
bold, ``(args)`` dim — via its own ``_compose_args``/``_truncate_args``
(#6184 段2b-1). Reading this module's own :func:`compose_tool_call_text`
from that render path would mean parsing a flat, already-formatted
string back apart into its pieces — exactly the shape this arc's own
census (dispatch-table producer/consumer duplication) already closed
elsewhere; this module exists so a GENERIC AG-UI client (one with no
reyn-specific rendering at all) sees a readable one-line value, not so
reyn's own TUI has a second way to read it.

#6184 BLOCKING (lead-coder, measured, PR #6195 review): the claim above
is NOT unconditional — THREE call sites (``presenter.py:296``,
``presenter.py:517``, ``renderer.py:1127``, all the same idiom: ``tool =
str(meta.get("tool", msg.text))``) read ``msg.text`` as a FALLBACK when
``meta["tool"]`` is absent. Safe TODAY only because
``lifecycle_forwarder._enqueue_tool_call`` (this module's one producer
call site) ALWAYS sets ``meta["tool"]`` — never because the TUI does not
read ``text`` at all. This PR changed what a future accidental
``meta["tool"]``-dropping edit would put into that fallback's bold
tool-name slot: the bare name before this PR, this module's own
``tool(args)`` composed form after it — the SAME docstring-outran-code
shape #6189 already corrected once in this arc (``_append_frame``'s own
"zero direct append call sites" claim).

Lives in ``core/present/`` (architect design, #6184 issuecomment-5683686664;
lead-coder ruling, issuecomment-5683701917) — an EXISTING neutral home both
``runtime`` (``core/op_runtime/*``, 4 files) and ``interfaces`` (renderer /
presenter / sent_queue) already import from, not a newly-created one.
``src/reyn/runtime/`` itself did not previously import from this package —
this module's own producer call site (``lifecycle_forwarder.py``) adds that
ONE new cross-layer import edge.

Two DIFFERENT outputs, corrected mid-implementation (architect,
issuecomment-5683762265 — the original dispatch got both wrong):

- :func:`compose_tool_call_args` — STRUCTURED (``list[tuple[str, str]] |
  str``), matching :func:`~reyn.interfaces.repl.renderer._compose_args`'s
  own shape exactly (#6184 段2b-1) — this is what a future PRODUCER-side
  ``details`` field carries. NOT joined into a string: a future consumer
  reading ``details`` needs the per-value boundaries intact to apply its
  own per-value cut (``_truncate_args``'s own docstring: "each value is
  cut to ``value_width`` BEFORE joining" — unrecoverable once already
  joined).
- :func:`compose_tool_call_text` — the FLAT ``tool(k=v, k2=v2)`` wire
  string. No length cut (a producer does not know a future viewer's
  terminal width — that stays entirely on the consumer side, #6184
  裁定) — only a transport-safety CAP via
  :func:`~reyn.core.present.guard.cap_leaf` (a different concern: cap
  bounds how much ever reaches the wire at all; width is a viewer's
  preference for how much it chooses to show).

Control-character removal happens in THIS module, via
:func:`~reyn.core.present.guard.strip_control_chars` — applied to each
composed value (so ``details`` never carries an un-stripped leaf) AND to
the final flat text — NOT ``get_neutralizer``/``TerminalNeutralizer``
(``guard.py``'s own ``get_neutralizer`` docstring: "what is dangerous is
surface-specific"; a producer composing a wire value does not know, and
must not guess, which surface will eventually render it). Does NOT touch
``renderer.py``'s own ``_normalize_text`` — #6184 段2b-1's own accept
criterion was a BYTE-IDENTICAL split; adding ESC-removal there could
change that function's existing output for any ESC-bearing input,
undoing a property 段2b-1 already established. This module reimplements
the whitespace-collapse half independently instead (accept criterion ④:
the consumer side is not touched by this stage, so nothing there depends
on this module existing).
"""
from __future__ import annotations

from reyn.core.present.guard import cap_leaf, strip_control_chars


def _normalize(value: object) -> str:
    """Whitespace-collapse + control-char-strip ANY value into a
    single-line string — see module docstring for why control-char
    removal lives HERE (applied per composed value) rather than in
    ``renderer.py``'s own ``_normalize_text``."""
    s = value if isinstance(value, str) else repr(value)
    return strip_control_chars(" ".join(s.split()))


def compose_tool_call_args(args: object) -> "list[tuple[str, str]] | str":
    """Structured compose — goes into ``details``, cut-free. A dict
    becomes an ordered list of ``(normalized key, normalized value)``
    pairs (mirrors :func:`~reyn.interfaces.repl.renderer._compose_args`'s
    own shape exactly, #6184 段2b-1); a bare value becomes a normalized
    string; a falsy ``args`` becomes ``[]``.

    Wire note (architect, unconfirmed by an actual round trip —
    disclosed, not assumed): JSON has no tuple type, so a real AG-UI
    round trip turns each ``(k, v)`` pair into a 2-element LIST — a
    future consumer reading ``details["args"]`` back off the wire must
    accept both shapes."""
    if not args:
        return []
    if isinstance(args, dict):
        return [(_normalize(k), _normalize(v)) for k, v in args.items()]
    return _normalize(args)


def compose_tool_call_text(tool: object, composed: "list[tuple[str, str]] | str") -> str:
    """The FLAT ``tool(k=v, k2=v2)`` wire ``text`` — built from an
    ALREADY-COMPOSED structure (:func:`compose_tool_call_args`'s own
    return value — call that first, pass its result here, so ``details``
    and ``text`` are derived from the exact same compose pass rather
    than two independent ones that could drift). No length cut (a
    producer does not know a future viewer's terminal width) — only a
    transport-safety CAP (:func:`~reyn.core.present.guard.cap_leaf`,
    ``MAX_LEAF_CHARS``) so an unbounded line cannot reach the wire at
    all; a viewer's own display width preference is a different, later
    concern this function does not touch."""
    name = _normalize(tool)
    if isinstance(composed, list) and composed:
        body = ", ".join(f"{k}={v}" for k, v in composed)
        flat = f"{name}({body})"
    elif isinstance(composed, str) and composed:
        flat = f"{name}({composed})"
    else:
        flat = name
    flat = strip_control_chars(flat)
    capped, _ = cap_leaf(flat)
    return capped
