"""Producer-side compose for a tool call's outbox wire fields — #6184 段2b-2.

**`text` is wire-only. The TUI never reads it as its PRIMARY value** — the
consumer (repl's own ``renderer.py``) already draws its own display from
the structured ``meta["args"]``/``meta["result"]`` fields — ``tool``
bold, ``(args)`` dim — via its own ``_compose_args`` (#6184 段2b-1) and
``core/present/tool_head.compose_tool_head`` (#6184 段3-3, the length-cut
half — ``renderer.py``'s own former in-module pair for that half,
``_truncate_args``, was removed in #6207 once every real consumer had
moved to that call instead). Reading this module's own :func:`compose_tool_call_text`
from that render path would mean parsing a flat, already-formatted
string back apart into its pieces — exactly the shape this arc's own
census (dispatch-table producer/consumer duplication) already closed
elsewhere; this module exists so a GENERIC AG-UI client (one with no
reyn-specific rendering at all) sees a readable one-line value, not so
reyn's own TUI has a second way to read it.

#6184 BLOCKING (lead-coder, measured, PR #6195 review) — HISTORICAL, now
fixed: the claim above was originally NOT unconditional. THREE call
sites (``presenter.py``'s ``_tool_head``/``_collapsed_retrieval_line``,
``renderer.py``'s ``format_inline_message``) used to read ``msg.text``
as a FALLBACK (``tool = str(meta.get("tool", msg.text))``) when
``meta["tool"]`` was absent — safe only because
``lifecycle_forwarder._enqueue_tool_call`` (this module's one producer
call site) ALWAYS sets ``meta["tool"]``, never because the TUI did not
read ``text`` at all. #6184's own cleanup PR (issuecomment-5684647871)
changed all three to ``meta.get("tool") or ""`` — not because the
branch became reachable (it still is not, and no test depends on it —
census, issuecomment-5684341577), but because THIS module made ``text``
carry the COMPOSED ``tool(args)`` wire form (段2b-2) instead of a bare
tool name, so a future accidental ``meta["tool"]``-dropping edit would
otherwise have silently put that composed string into the bold
tool-name slot instead of degrading honestly. The SAME
docstring-outran-code shape #6189 already corrected once in this arc
(``_append_frame``'s own "zero direct append call sites" claim).

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
  own per-value cut — :func:`~reyn.core.present.tool_head.
  compose_tool_head` (#6184 段3-3) is the real consumer that now applies
  it, cutting each value BEFORE joining (its own docstring) —
  unrecoverable once already joined.
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


def render_subject(value: object) -> str:
    """Convert a tool's declared subject RAW value
    (:func:`~reyn.tools.subject.resolve_tool_subject`'s own return) into
    a display string — #6184 段3-2 BLOCKING fix (lead-coder, measured,
    PR #6200 review): this conversion moved HERE (the architect's own
    段3 design, issuecomment-5685059570) from ``reyn/tools/subject.py``,
    which used to stringify with a bare ``str()`` — for ``exec``'s own
    ``argv`` (a ``list[str]``), that produced a Python repr
    (``"['python', '-m', 'pytest']"``), the exact display shape the
    owner's original request asked to move away from.

    Three shapes, per the architect's own design (verbatim):

    - ``str`` → AS-IS, no further transformation (not even this
      module's own :func:`_normalize` — the value is a tool's own
      declared subject, not raw wire content composed from multiple
      arg values, so the same control-char/whitespace defensiveness
      :func:`compose_tool_call_text` applies to ITS OWN flat string
      does not automatically apply here; this is a disclosed, narrow
      scope decision matching the literal design quote, not an
      oversight — a LATER stage that wires a consumer to `subject` is
      the place to revisit whether this needs the same defensive pass).
    - ``list[str]`` → SPACE-JOINED, so ``argv`` reads as the actual
      command line it represents (``["python", "-m", "pytest"]`` →
      ``"python -m pytest"``) — the whole point of this fix.
    - anything else (a non-string-only list, a number, a dict, ...) →
      the EXISTING normalize path (:func:`_normalize` — the same
      whitespace-collapse + control-strip every other composed value in
      this module already gets), so an unexpected shape still degrades
      to a safe, readable string rather than crashing or leaking a raw
      repr unfiltered.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return " ".join(value)
    return _normalize(value)
