"""Default-viewer synthesizers for the present layer's 4-stage fallback (FP-0054 §3, PR-C).

The FP-0054 §3 view-source fallback chain (FP-0055 PR-1: stages 1-2 are skipped
entirely — not "unknown" — when the caller omits both ``view`` and
``blueprint``; resolution enters directly at stage 3)::

    registered view (operator) → inline blueprint (LLM)
        → content-type default viewer → generic YAML/text

Stages 1-2 (registered / inline) are the *requested* rendering, driven in
``op_runtime/present.py``. This module builds the two SYNTHESIZED fallback stages,
each as a plain declarative blueprint so it runs the SAME
``validate_blueprint`` → ``resolve_bindings`` → render path — reusing the guard,
size caps and non-executable-by-construction shape, never a bespoke render path.
FP-0051's viewer *registry* was deleted (design lineage only); these are built
fresh here.

- **Stage 3 — content-type default viewer** (:func:`default_viewer_blueprint`):
  a minimal default chosen by a lightweight recognition ladder (architect-endorsed):
  **declared content-type → diff-sniff (FP-0051 heuristic) → data shape**.

  1. *Declared content-type* — an explicit ``text``/``markdown`` type → a
     ``markdown`` component; a ``code`` type → a ``code`` component. This stage is
     populated **only** from inline data / an explicit op arg: a `present` op has no
     content-type field today, and — verified — an offloaded ``data_ref`` carries no
     content-type in its frontmatter (the tool-result canonical mappers drop
     ``content_type`` as transport). So for a ``data_ref`` source ``content_type`` is
     ``None`` and we fall straight to diff-sniff → shape (an endorsed correct
     degrade), never reading a content-type that is not there.
  2. *Diff-sniff* — a string that looks like a unified diff defaults to the ``diff``
     viewer (diff is common and should highlight).
  3. *Shape* — a list of objects → a ``table`` over the union of the first rows'
     keys; a list of scalars → a ``list``; an object → a ``keyvalue`` card of its
     top-level keys; the opaque ``{"binary": ...}`` marker → a ``text`` placeholder
     (byte count + ref, never markdown/code); any other scalar or plain string →
     a **``text``** component (NOT ``markdown`` — markdown re-interprets ``#`` /
     ``*`` / ``[]()`` and would mangle plain output, violating the fidelity
     principle). Every binding is derived from the data itself, so it does not
     "show an empty shell".
- **Stage 4 — generic YAML/text** (:func:`generic_blueprint`): the whole value as
  one text-family leaf — structured data dumped to YAML in a ``code`` component,
  plain text in a ``text`` component. The terminal, always-renders catch: the
  ref/data always reaches the user (design principle 4 — "degrade, don't fail").

Both synthesizers emit blueprints in the *inline-blueprint* shape (catalog
components + ``{"$bind": <pointer>}`` bindings / literals), so the caller validates
and binds them identically to an LLM-authored blueprint. The value is bound (not
inlined as a literal, where a pointer is natural) so the render model still carries
the neutralized/capped data the surface renderer consumes.
"""
from __future__ import annotations

import re
from typing import Any

# Cap on how many columns a shape-derived table advertises — a wide object would
# otherwise produce an unreadable table. The row values themselves are size-capped
# at the guard; this bounds the *column* fan-out, a shape choice, not a leaf cap.
_MAX_DEFAULT_COLUMNS = 20

# How many leading rows to scan for the table-column key union. First-row-only
# drops keys under sparse data (a later row with an extra key); scanning ALL rows
# is unbounded work on a huge list. First-K is the bounded middle ground.
_COLUMN_KEY_SCAN_ROWS = 50

# Diff-sniff (stage-3 recognition ladder step 2, FP-0051 lineage). Only the first
# _SNIFF_BYTES of a string are inspected — a strong, low-false-positive signal:
# a ``diff --git`` header or a unified-diff ``@@ … @@`` hunk header.
_SNIFF_BYTES = 4096
_DIFF_HUNK_RE = re.compile(r"^@@ .* @@", re.MULTILINE)

# Content-type prefixes that map to the markdown / code default components. Matched
# case-insensitively against a *declared* content-type only (inline data / explicit
# op arg) — never derived from an offloaded ``data_ref`` (no content-type there).
_MARKDOWN_TYPES = ("text/markdown", "text/x-markdown")
_CODE_TYPE_HINTS = ("application/json", "text/x-", "application/x-", "text/css", "text/html")


def _is_markdown_type(content_type: "str | None") -> bool:
    """True iff a DECLARED content-type marks the value as markdown."""
    if not content_type:
        return False
    ct = content_type.lower()
    return any(ct.startswith(p) for p in _MARKDOWN_TYPES)


def _is_code_type(content_type: "str | None") -> bool:
    """True iff a DECLARED content-type marks the value as source/code-like."""
    if not content_type:
        return False
    ct = content_type.lower()
    return any(ct.startswith(p) for p in _CODE_TYPE_HINTS)


def _looks_like_diff(text: str) -> bool:
    """FP-0051 diff-sniff — a conservative unified-diff detector over the head of
    ``text`` (a ``diff --git`` line or a ``@@ … @@`` hunk header). Deliberately
    strict to avoid mis-flagging prose as a diff."""
    head = text[:_SNIFF_BYTES]
    if head.startswith("diff --git ") or "\ndiff --git " in head:
        return True
    return bool(_DIFF_HUNK_RE.search(head))


def _column_keys(rows: list) -> list[str]:
    """The ordered union of string keys across the first :data:`_COLUMN_KEY_SCAN_ROWS`
    row objects (column count capped at :data:`_MAX_DEFAULT_COLUMNS`). Used to derive
    a default ``table``'s columns from a list-of-objects' actual shape."""
    seen: dict[str, None] = {}
    for row in rows[:_COLUMN_KEY_SCAN_ROWS]:
        if isinstance(row, dict):
            for k in row:
                if isinstance(k, str) and k not in seen:
                    seen[k] = None
                    if len(seen) >= _MAX_DEFAULT_COLUMNS:
                        return list(seen)
    return list(seen)


def _escape_token(key: str) -> str:
    """Escape a dict key into a single RFC 6901 reference token (``~`` → ``~0``,
    ``/`` → ``~1``) so a key containing those characters binds correctly."""
    return key.replace("~", "~0").replace("/", "~1")


def default_viewer_blueprint(data: Any, *, content_type: "str | None" = None) -> "list[dict]":
    """Stage 3 — synthesize a content-type/shape default blueprint for ``data``.

    The recognition ladder (architect-endorsed): **declared content-type →
    diff-sniff → data shape**.

    - ``content_type`` is a DECLARED type (from inline data / an explicit op arg
      only — an offloaded ``data_ref`` has none, so callers pass ``None`` for a ref
      source and the ladder degrades to diff-sniff → shape). A markdown type → a
      ``markdown`` component; a code type → a ``code`` component.
    - A string that sniffs as a unified diff → a ``diff`` component.
    - ``list`` of objects → a ``table`` binding the whole list, columns = the union
      of the first rows' keys (row-relative ``/<key>`` paths);
    - ``list`` of scalars (or mixed / empty) → a ``list`` binding the whole list;
    - ``dict`` → a ``keyvalue`` card, one row per top-level key; the opaque
      ``{"binary": ...}`` marker → a ``text`` placeholder (byte count + ref);
    - any other scalar / plain string → a ``text`` of the whole value (NOT
      ``markdown`` — plain output must not be re-interpreted as markup).

    Returns a blueprint (list of component nodes) in the inline-blueprint shape.
    """
    # 1. Declared content-type (populated only for inline/explicit-arg sources).
    if isinstance(data, str):
        if _is_markdown_type(content_type):
            return [{"component": "markdown", "text": {"$bind": ""}}]
        if _is_code_type(content_type):
            return [{"component": "code", "text": {"$bind": ""}}]
        # 2. Diff-sniff (FP-0051 heuristic) — a unified diff highlights by default.
        if _looks_like_diff(data):
            return [{"component": "diff", "text": {"$bind": ""}}]
        # 3. Plain string → text (fidelity: never markdown for un-typed text).
        return [{"component": "text", "text": {"$bind": ""}}]

    # 3. Shape-based defaults for structured data.
    if isinstance(data, list):
        if any(isinstance(row, dict) for row in data):
            keys = _column_keys(data)
            if keys:
                return [{
                    "component": "table",
                    "rows": {"$bind": ""},
                    "columns": [
                        {"header": k, "path": f"/{_escape_token(k)}"} for k in keys
                    ],
                }]
        # Scalars, mixed, or empty → a plain list of the whole array.
        return [{"component": "list", "items": {"$bind": ""}}]

    if isinstance(data, dict):
        # The opaque non-text binary marker from resolve_present_source → a text
        # placeholder (never markdown/code — there is nothing to highlight).
        if data.get("binary") is True:
            size = data.get("byte_size")
            size_txt = f"{size} bytes" if isinstance(size, int) else "unknown size"
            return [{
                "component": "text",
                "text": f"[binary data — {size_txt}; full data in the ref]",
            }]
        keys = [k for k in data if isinstance(k, str)]
        if keys:
            return [{
                "component": "keyvalue",
                "rows": [
                    {"label": k, "value": {"$bind": f"/{_escape_token(k)}"}}
                    for k in keys
                ],
            }]
        # An empty / non-string-keyed object → fall through to the whole-doc view.

    # Any other scalar (number / bool / null) → a text of the whole value.
    return [{"component": "text", "text": {"$bind": ""}}]


def generic_blueprint(data: Any) -> "list[dict]":
    """Stage 4 — the generic YAML/text terminal viewer for ``data``.

    Structured data (dict / list) → a ``code`` component (``language: yaml``) whose
    text is the whole value dumped to YAML; plain text / a scalar → a ``text``
    component of the value. The text is a LITERAL (the value is already in hand),
    so it still passes through the guard + the per-leaf size cap at the render seam.
    Always renders — the final catch so the data never fails to reach the user.
    """
    if isinstance(data, (dict, list)):
        return [{
            "component": "code",
            "language": "yaml",
            "text": _to_yaml(data),
        }]
    if data is None:
        return [{"component": "text", "text": ""}]
    if isinstance(data, str):
        return [{"component": "text", "text": data}]
    return [{"component": "text", "text": str(data)}]


def _to_yaml(data: Any) -> str:
    """Dump ``data`` to a YAML string for the generic viewer, falling back to a
    JSON dump if the value is not YAML-serializable (defensive — the resolved
    value is JSON-origin, so ``safe_dump`` handles it, but a stray non-serializable
    object must never crash the always-renders terminal stage)."""
    import yaml

    try:
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except yaml.YAMLError:
        import json

        return json.dumps(data, ensure_ascii=False, indent=2, default=str)


__all__ = ["default_viewer_blueprint", "generic_blueprint"]
