"""Presentation-guard — the output-side neutralization boundary for present (FP-0054).

Mirror of the input-side content-guard, applied at the output seam: every leaf
string that reaches a renderer is threat-scanned and **neutralized** —
regardless of whether the data was ever ingested by the LLM (the whole point of
present is blind routing). Two concerns:

- **Neutralize rendered leaf strings** so bound bulk data cannot drive the
  surface it is displayed on: terminal escape / control sequences (a raw ``ESC``
  can rewrite / spoof the user's terminal), Rich console markup (``[bold]`` …),
  and HTML (``<script>`` …). Neutralization is a *transform* — the value still
  renders, just inert — so no user data is lost; the ref remains the
  full-fidelity source.
- **Per-binding size caps** so a ``/`` (root) pointer bound into a ``text``
  component cannot dump a whole file, and a huge array cannot flood scrollback.
  Leaf strings cap by characters; arrays cap by row count (head-N + a remainder
  note the caller composes from the ref).

Pure: no I/O, no events, no config — the caller wires telemetry and decides the
drop-reason (``guard_stripped``) from the returned ``stripped`` flag.
"""
from __future__ import annotations

import re

# Per-binding default caps. A leaf string longer than this is truncated (the
# ``/`` root-into-text dump guard); an array longer than MAX_ROWS is capped to
# head-N rows. Both are present-specific defaults — present is unbounded by
# construction, so it carries its own cap rather than relying on LLM output
# tokens (which bound ordinary conversation output).
MAX_LEAF_CHARS: int = 10_000
MAX_ROWS: int = 500

# C0 control characters except tab / newline / carriage-return, plus the DEL
# (0x7f) and the C1 range (0x80-0x9f). ``\x1b`` (ESC) — the lead byte of every
# CSI / OSC terminal escape — is the headline threat; stripping the whole class
# also removes the trailing sequence bytes so no partial escape survives.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Rich console markup tags: ``[tag]`` / ``[/tag]`` / ``[/]``. Rich's own escape
# convention is a leading backslash on the ``[`` — we apply exactly that so the
# tag renders as literal text instead of styling the surface.
_RICH_TAG_RE = re.compile(r"\[(/?[a-zA-Z#][^\[\]]*|/)\]")


def neutralize_leaf(value: str) -> tuple[str, bool]:
    """Neutralize a single rendered leaf string. Returns ``(clean, stripped)``.

    ``stripped`` is True when the guard materially changed the value — a
    terminal-escape / control sequence was removed, Rich markup was escaped, or
    HTML angle brackets were entity-escaped. The caller reports a stripped
    binding with drop-reason ``guard_stripped``.

    The neutralized value still renders (inert) — this is a transform, not a
    drop, so the user never loses data (the ref remains authoritative).
    """
    out = value
    # 1. Strip terminal escape / control sequences.
    out = _CONTROL_RE.sub("", out)
    # 2. Escape Rich markup tags so they render literally (Rich unescapes a
    #    leading backslash, so ``\[bold]`` prints as ``[bold]``).
    out = _RICH_TAG_RE.sub(lambda m: "\\" + m.group(0), out)
    # 3. Neutralize HTML: entity-escape the structural characters. ``&`` first so
    #    the entities we introduce below are not themselves re-escaped.
    if "<" in out or ">" in out or "&" in out:
        out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out, out != value


def cap_leaf(value: str, *, max_chars: int = MAX_LEAF_CHARS) -> tuple[str, bool]:
    """Cap a leaf string to ``max_chars``. Returns ``(capped, was_capped)``.

    A capped leaf carries a compact ``… (+N chars)`` tail so the reader knows
    the value was truncated; the ref remains the full-fidelity escape hatch.
    A capped binding is reported with drop-reason ``guard_stripped``.
    """
    if len(value) <= max_chars:
        return value, False
    remainder = len(value) - max_chars
    return value[:max_chars] + f"… (+{remainder} chars — full data in the ref)", True


def cap_rows(rows: list, *, max_rows: int = MAX_ROWS) -> tuple[list, bool]:
    """Cap an array to ``max_rows`` head rows. Returns ``(capped, was_capped)``.

    Caps BEFORE render (the caller renders only the survivors), so a costly
    per-row render (syntax highlight etc.) never runs on the truncated tail.
    """
    if len(rows) <= max_rows:
        return rows, False
    return rows[:max_rows], True
