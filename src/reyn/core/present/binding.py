"""Binding resolution for present — join a validated blueprint to data (FP-0054).

The LLM works from an offload schema + preview and binds JSON-Pointer paths; this
module joins those bindings against the *full* data the LLM never ingested. The
asymmetry (LLM sees shape, user sees content) is the designed contract.

Semantics (§4):

- **Path hit** → bind the value.
- **Path miss** → soft-skip that binding and record ``{path, reason:
  path_not_found}``; never a hard failure.
- **Type mismatch** → coerce (a scalar bound into a ``table`` ``rows`` slot → a
  1-row table; a container bound into a text slot → its JSON form) and record
  ``{path, reason: type_mismatch}``.
- **Guard-stripped** → a bound leaf neutralized or size-capped by the
  presentation-guard is recorded ``{path, reason: guard_stripped}``.
- **All bindings miss** → the ``all_bindings_missed`` outcome is exposed so the
  caller can route to the generic viewer (the fallback wiring itself is a later
  PR); never a hard failure here.

Table / list column paths resolve **row-relative** (RFC 6901 relative to each
iterated row). Bindings are resolved against a **null renderer** in this layer —
the returned model is data (bound values + drop stats), never surface bytes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from reyn.core.present.catalog import binding_pointer, is_binding
from reyn.core.present.guard import cap_leaf, cap_rows, neutralize_leaf

# Drop reason categories (§1 refined ack shape).
PATH_NOT_FOUND = "path_not_found"
TYPE_MISMATCH = "type_mismatch"
GUARD_STRIPPED = "guard_stripped"

_MISSING = object()


@dataclass
class ResolvedPresentation:
    """The outcome of joining a blueprint to data against a null renderer.

    ``nodes`` is the render model (bound values, neutralized + capped) a surface
    renderer (later PR) consumes. The remaining fields are the compact,
    high-signal stats the op ack + the ``presented`` event carry.
    """

    nodes: list[dict] = field(default_factory=list)
    bindings_resolved: int = 0
    bindings_dropped: list[dict] = field(default_factory=list)
    rows: int = 0
    _missed: int = 0

    @property
    def all_bindings_missed(self) -> bool:
        """True iff the blueprint had ≥1 binding and none resolved (§4 full-miss
        → generic-viewer fallback signal)."""
        return self.bindings_resolved == 0 and self._missed > 0

    def _resolve(self) -> None:
        self.bindings_resolved += 1

    def _drop(self, path: str, reason: str) -> None:
        self.bindings_dropped.append({"path": path, "reason": reason})
        if reason == PATH_NOT_FOUND:
            self._missed += 1


def resolve_pointer(doc: Any, pointer: str) -> tuple[Any, bool]:
    """Resolve an RFC 6901 JSON Pointer against ``doc``. Returns ``(value, found)``.

    ``""`` → the whole document. Array tokens index by integer; object tokens by
    key. A miss (bad key / out-of-range index / scalar-with-remaining-tokens)
    returns ``(None, False)`` — never raises.
    """
    if pointer == "":
        return doc, True
    if not pointer.startswith("/"):
        return None, False
    cur = doc
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if token not in cur:
                return None, False
            cur = cur[token]
        elif isinstance(cur, list):
            try:
                idx = int(token)
            except ValueError:
                return None, False
            if idx < 0 or idx >= len(cur):
                return None, False
            cur = cur[idx]
        else:
            return None, False
    return cur, True


def _coerce_text(value: Any) -> tuple[str, bool]:
    """Coerce a bound value into a text-slot string. Returns ``(text, mismatch)``.

    Scalars (str / number / bool / null) are natural text — not a mismatch. A
    container (dict / list) into a text slot is a real type mismatch → its JSON
    form + ``mismatch=True``.
    """
    if isinstance(value, str):
        return value, False
    if isinstance(value, bool):
        return ("true" if value else "false"), False
    if value is None:
        return "", False
    if isinstance(value, (int, float)):
        return str(value), False
    return json.dumps(value, ensure_ascii=False, default=str), True


def _coerce_rows(value: Any) -> tuple[list, bool]:
    """Coerce a bound value into an array of rows. Returns ``(rows, mismatch)``.

    A scalar / object bound into a ``rows`` / ``items`` slot → a 1-row array
    (§4 coercion rule) + ``mismatch=True``.
    """
    if isinstance(value, list):
        return value, False
    return [value], True


def _bind_text_slot(slot: Any, doc: Any, out: ResolvedPresentation) -> Any:
    """Resolve a text-family slot (literal or ``$bind``). Returns the rendered
    (neutralized, capped) string, or ``_MISSING`` when the binding missed."""
    if not is_binding(slot):
        # A literal — already escaped at parse; render as-is (still cap defensively).
        if isinstance(slot, str):
            capped, _ = cap_leaf(slot)
            return capped
        return slot
    ptr = binding_pointer(slot)
    value, found = resolve_pointer(doc, ptr)
    if not found:
        out._drop(ptr, PATH_NOT_FOUND)
        return _MISSING
    text, mismatch = _coerce_text(value)
    clean, stripped = neutralize_leaf(text)
    capped, was_capped = cap_leaf(clean)
    out._resolve()
    if mismatch:
        out._drop(ptr, TYPE_MISMATCH)
    if stripped or was_capped:
        out._drop(ptr, GUARD_STRIPPED)
    return capped


def _bind_rows_slot(slot: Any, doc: Any, out: ResolvedPresentation) -> tuple[list, bool]:
    """Resolve a ``rows`` / ``items`` array slot. Returns ``(rows, found)`` — an
    empty list + ``found=False`` on a miss."""
    if not is_binding(slot):
        return (slot if isinstance(slot, list) else []), True
    ptr = binding_pointer(slot)
    value, found = resolve_pointer(doc, ptr)
    if not found:
        out._drop(ptr, PATH_NOT_FOUND)
        return [], False
    rows, mismatch = _coerce_rows(value)
    out.rows += len(rows)
    out._resolve()
    if mismatch:
        out._drop(ptr, TYPE_MISMATCH)
    capped_rows, was_capped = cap_rows(rows)
    if was_capped:
        out._drop(ptr, GUARD_STRIPPED)
    return capped_rows, True


def _bind_row_relative(rows: list, path: str, out: ResolvedPresentation) -> list:
    """Resolve a row-relative column / item path across every row. Records a
    single ``path_not_found`` when the path misses on ALL rows; a single
    ``guard_stripped`` when any cell was neutralized. Returns the rendered cell
    values (missing cells → empty string, sparse data is normal)."""
    cells: list[str] = []
    any_hit = False
    any_stripped = False
    for row in rows:
        value, found = resolve_pointer(row, path)
        if not found:
            cells.append("")
            continue
        any_hit = True
        text, _ = _coerce_text(value)
        clean, stripped = neutralize_leaf(text)
        capped, was_capped = cap_leaf(clean)
        any_stripped = any_stripped or stripped or was_capped
        cells.append(capped)
    if not any_hit and rows:
        out._drop(path, PATH_NOT_FOUND)
    elif any_hit:
        out._resolve()
        if any_stripped:
            out._drop(path, GUARD_STRIPPED)
    return cells


def resolve_bindings(nodes: list[dict], doc: Any) -> ResolvedPresentation:
    """Join a validated (normalized) blueprint to ``doc`` against a null renderer.

    Returns a :class:`ResolvedPresentation` — the render model plus the compact
    binding stats (``bindings_resolved`` / ``bindings_dropped`` / ``rows`` /
    ``all_bindings_missed``) the op ack + ``presented`` event carry.
    """
    out = ResolvedPresentation()
    for node in nodes:
        component = node["component"]
        rendered: dict[str, Any] = {"component": component}
        if component in {"text", "markdown", "code", "diff"}:
            if "text" in node:
                val = _bind_text_slot(node["text"], doc, out)
                if val is not _MISSING:
                    rendered["text"] = val
            if component == "code" and "language" in node:
                rendered["language"] = node["language"]
        elif component == "keyvalue":
            rows_out = []
            for row in node["rows"]:
                val = _bind_text_slot(row["value"], doc, out)
                if val is not _MISSING:
                    rows_out.append({"label": row["label"], "value": val})
            rendered["rows"] = rows_out
        elif component == "table":
            rows, _found = _bind_rows_slot(node.get("rows"), doc, out)
            columns_out = []
            for col in node.get("columns", []):
                cells = _bind_row_relative(rows, col["path"], out)
                columns_out.append({"header": col["header"], "cells": cells})
            rendered["columns"] = columns_out
            rendered["row_count"] = len(rows)
        elif component == "list":
            rows, _found = _bind_rows_slot(node.get("items"), doc, out)
            if "item_path" in node:
                rendered["items"] = _bind_row_relative(rows, node["item_path"], out)
            else:
                rendered["items"] = [_coerce_text(r)[0] for r in rows]
        elif component == "image":
            if "src" in node:
                val = _bind_text_slot(node["src"], doc, out)
                if val is not _MISSING:
                    rendered["src"] = val
            if "alt" in node:
                rendered["alt"] = node["alt"]
        out.nodes.append(rendered)
    return out
