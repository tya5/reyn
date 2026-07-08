"""Presentation-guard — the per-surface output neutralization boundary (FP-0054).

Mirror of the input-side content-guard, applied at the output seam: every leaf
string that reaches a renderer — labels, literal slot values, AND bound data
values alike — passes through ONE neutralizer, selected by the target surface.
There is no second neutralization path, so no render-leaf can reach a renderer
un-neutralized. Two concerns:

- **Neutralize rendered leaf strings** per surface so bound data cannot drive the
  surface it displays on. Neutralization is surface-specific: what is dangerous
  on a terminal (ESC / control sequences) is not what is dangerous in a browser
  (HTML). The neutralizer is a **per-surface strategy** (dispatch by surface
  name), so §6's per-surface boundary is structural, not a conditional — a
  future ``web`` strategy (HTML-escape) registers here without touching the
  binding seam.
- **Per-binding size caps** (surface-agnostic) so a ``/`` (root) pointer bound
  into a ``text`` component cannot dump a whole file, and a huge array cannot
  flood scrollback. Leaf strings cap by characters; arrays cap by row count.

The v1 **terminal** strategy strips ESC / control sequences (OSC / CSI — notably
OSC-52 clipboard — is a real attack surface on every terminal) and nothing else.
It does **not** escape Rich console markup, and does **not** HTML-escape.

**Rich-markup safety is NOT this module's responsibility (PR-B revision — see
FP-0054 §5).** An earlier PR-A revision escaped ``[tag]``-shaped substrings here
(the FP-0051 idiom), on the premise that Rich console markup is a surface-level
threat like ESC sequences. It is not: Rich markup injection is possible ONLY
through ``console.print(str, markup=True)`` — a choice the RENDERER makes per
Rich object, not a property of the terminal sink itself. ``rich.text.Text`` and
``rich.syntax.Syntax`` never interpret ``[tag]`` at all (markup-inert);
``rich.markdown.Markdown`` interprets CommonMark's OWN backslash-escape, not
Rich markup. Escaping here unconditionally corrupted Text/Syntax output with
visible literal backslashes (a real bug caught by empirical testing across all
three Rich paths, PR-B review) — the guard was neutralizing a threat that,
for two of the three render paths it feeds, does not exist at that sink.

The fix is structural, not a runtime escape/unescape pair: the inline-CUI
renderer (``interfaces/repl/present_renderer.py``) routes every leaf into a
markup-inert Rich object (``Text``/``Syntax``/``Markdown``) and never calls
``console.print(str)`` with markup interpretation on presented content — Rich
injection becomes impossible by construction, the same "safety from shape, not
policy" philosophy as reyn's structural write-gate. HTML neutralization
(HTML-escape) remains a future web renderer's own concern, for the same reason.

Pure: no I/O, no events, no config — the caller wires telemetry and decides the
drop-reason (``guard_stripped``) from the returned ``stripped`` flag.
"""
from __future__ import annotations

import re
from typing import Protocol

# Per-binding default caps. A leaf string longer than this is truncated (the
# ``/`` root-into-text dump guard); an array longer than MAX_ROWS is capped to
# head-N rows. Both are present-specific defaults — present is unbounded by
# construction, so it carries its own cap rather than relying on LLM output
# tokens (which bound ordinary conversation output).
MAX_LEAF_CHARS: int = 10_000
MAX_ROWS: int = 500

# C0 control characters except tab / newline / carriage-return, plus DEL (0x7f)
# and the C1 range (0x80-0x9f). ``\x1b`` (ESC) — the lead byte of every CSI / OSC
# terminal escape — is the headline threat; stripping the whole class also removes
# the trailing sequence bytes so no partial escape survives.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class LeafNeutralizer(Protocol):
    """A per-surface leaf-string neutralizer. ``neutralize`` returns
    ``(clean, stripped)`` — ``stripped`` True when the value was materially
    changed (the caller then reports the binding ``guard_stripped``)."""

    def neutralize(self, value: str) -> tuple[str, bool]:
        ...


class TerminalNeutralizer:
    """Terminal-surface strategy: strip ESC / control sequences. Does NOT escape
    Rich console markup (the renderer's job — see module docstring) and does NOT
    HTML-escape (a terminal renders ``<div>`` as a literal, and entity-escaping
    would corrupt ``code`` / ``diff`` content)."""

    def neutralize(self, value: str) -> tuple[str, bool]:
        out = _CONTROL_RE.sub("", value)
        return out, out != value


_TERMINAL = TerminalNeutralizer()

# Surface name → neutralizer strategy. v1 ships the terminal strategy for every
# terminal-family surface (the null renderer uses it too, so the guard runs
# unconditionally even with no real surface). A future ``web`` strategy
# (HTML-escape) is added here WITHOUT touching the core seam or the binding layer.
_STRATEGIES: dict[str, LeafNeutralizer] = {
    "terminal": _TERMINAL,
    "inline-cui": _TERMINAL,
    "null": _TERMINAL,
}
_DEFAULT_STRATEGY: LeafNeutralizer = _TERMINAL


def get_neutralizer(surface: str) -> LeafNeutralizer:
    """Select the leaf neutralizer for ``surface`` (defaults to the terminal
    strategy). The single dispatch point per-surface neutralization flows
    through — the binding seam asks for a neutralizer by surface, never branches
    on surface itself."""
    return _STRATEGIES.get(surface, _DEFAULT_STRATEGY)


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
