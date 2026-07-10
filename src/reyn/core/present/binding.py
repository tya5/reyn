"""Binding resolution for present — join a validated blueprint to data (FP-0054).

The LLM works from an offload schema + preview and binds JSON-Pointer paths; this
module joins those bindings against the *full* data the LLM never ingested. The
asymmetry (LLM sees shape, user sees content) is the designed contract.

This layer is also the **single leaf-neutralization seam**: every render-leaf
string — labels, literal slot values, AND bound data values — passes through the
surface-selected neutralizer here (`guard.get_neutralizer(surface)`) as the render
model is assembled. There is no parse-time neutralization path; nothing reaches a
renderer un-neutralized.

Semantics (§4):

- **Path hit** → bind the value.
- **Path miss** → soft-skip that binding and record ``{path, reason:
  path_not_found}``; never a hard failure.
- **Type mismatch** → coerce (a scalar bound into a ``table`` ``rows`` slot → a
  1-row table; a container bound into a text slot → its JSON form) and record
  ``{path, reason: type_mismatch}``.
- **Guard-stripped** → any render-leaf (bound value, literal, or label)
  neutralized or size-capped by the presentation-guard is recorded
  ``{path, reason: guard_stripped}``.
- **All bindings miss** → the ``all_bindings_missed`` outcome is exposed so the
  caller can route to the generic viewer (the fallback wiring itself is a later
  PR); never a hard failure here.

Table / list column paths resolve **row-relative** (RFC 6901 relative to each
iterated row). Only ``$bind`` paths count toward ``bindings_resolved``; a
literal/label is neutralized but is not a binding.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from reyn.core.present.catalog import binding_pointer, is_binding
from reyn.core.present.guard import LeafNeutralizer, cap_leaf, cap_rows, get_neutralizer

# Drop reason categories (§1 refined ack shape).
PATH_NOT_FOUND = "path_not_found"
TYPE_MISMATCH = "type_mismatch"
GUARD_STRIPPED = "guard_stripped"

_MISSING = object()


@dataclass
class ResolvedPresentation:
    """The outcome of joining a blueprint to data.

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


def _truncation_tail(omitted: int, ref: "str | None") -> str:
    """The §5-mandated visible truncation indicator for a `cap_rows`-capped
    `table`/`list` rows slot: ``…N more — full data: <ref>`` (ratified
    ``docs/deep-dives/proposals/0054-present-layer.md`` §5). ``ref`` is the
    op's `data_ref` when the presented data came from one; omitted (``None``)
    for inline data, which has no re-fetchable ref — the tail then reads
    ``…N more`` (issue #2669)."""
    if ref:
        return f"…{omitted} more — full data: {ref}"
    return f"…{omitted} more"


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


def _guard(text: str, neut: LeafNeutralizer, out: ResolvedPresentation, *, path: str) -> str:
    """Route a render-leaf string through the surface's single neutralizer seam +
    the size cap; record ``guard_stripped`` (at ``path``) when it changes. Returns
    the rendered value. Does NOT count toward ``bindings_resolved`` — that is the
    binding layer's decision (a literal/label is neutralized but is not a
    binding)."""
    clean, stripped = neut.neutralize(text)
    capped, was_capped = cap_leaf(clean)
    if stripped or was_capped:
        out._drop(path, GUARD_STRIPPED)
    return capped


def _guard_maybe(value: Any, neut: LeafNeutralizer, out: ResolvedPresentation, *, path: str) -> Any:
    """Neutralize a literal/label leaf when it is a string; non-strings pass
    through unchanged (the seam only transforms strings)."""
    if isinstance(value, str):
        return _guard(value, neut, out, path=path)
    return value


def _render_text_slot(
    slot: Any, doc: Any, out: ResolvedPresentation, neut: LeafNeutralizer, *, loc: str,
) -> Any:
    """Resolve + guard a text-family slot (literal or ``$bind``). Returns the
    rendered (neutralized, capped) string, or ``_MISSING`` when the binding
    missed."""
    if is_binding(slot):
        ptr = binding_pointer(slot)
        value, found = resolve_pointer(doc, ptr)
        if not found:
            out._drop(ptr, PATH_NOT_FOUND)
            return _MISSING
        text, mismatch = _coerce_text(value)
        out._resolve()
        if mismatch:
            out._drop(ptr, TYPE_MISMATCH)
        return _guard(text, neut, out, path=ptr)
    # A literal slot value — not a binding, but still a render-leaf → neutralize.
    return _guard_maybe(slot, neut, out, path=loc)


def _bind_rows_slot(slot: Any, doc: Any, out: ResolvedPresentation) -> tuple[list, bool, int]:
    """Resolve a ``rows`` / ``items`` array slot. Returns ``(rows, found, omitted)``
    — an empty list + ``found=False`` on a miss; ``omitted`` is the row count
    ``cap_rows`` dropped (0 when not capped), threaded to the render model so the
    caller can attach a visible truncation tail (§5, issue #2669). (The array
    itself is not a leaf; its cells are neutralized where they are rendered.)"""
    if not is_binding(slot):
        return (slot if isinstance(slot, list) else []), True, 0
    ptr = binding_pointer(slot)
    value, found = resolve_pointer(doc, ptr)
    if not found:
        out._drop(ptr, PATH_NOT_FOUND)
        return [], False, 0
    rows, mismatch = _coerce_rows(value)
    out.rows += len(rows)
    out._resolve()
    if mismatch:
        out._drop(ptr, TYPE_MISMATCH)
    capped_rows, was_capped = cap_rows(rows)
    omitted = (len(rows) - len(capped_rows)) if was_capped else 0
    if was_capped:
        out._drop(ptr, GUARD_STRIPPED)
    return capped_rows, True, omitted


def _bind_row_relative(
    rows: list, path: str, out: ResolvedPresentation, neut: LeafNeutralizer,
) -> list:
    """Resolve a row-relative column / item path across every row (cells routed
    through the neutralizer seam). Records a single ``path_not_found`` when the
    path misses on ALL rows; a single ``guard_stripped`` when any cell was
    neutralized. Missing cells → empty string (sparse data is normal)."""
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
        clean, stripped = neut.neutralize(text)
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


def _guard_list_items(
    rows: list, out: ResolvedPresentation, neut: LeafNeutralizer, *, loc: str,
) -> list:
    """Neutralize the raw row values of a ``list`` with no per-item path (each is
    a render-leaf). Aggregates one ``guard_stripped`` for the slot when any item
    was neutralized."""
    result: list[str] = []
    any_stripped = False
    for row in rows:
        text, _ = _coerce_text(row)
        clean, stripped = neut.neutralize(text)
        capped, was_capped = cap_leaf(clean)
        any_stripped = any_stripped or stripped or was_capped
        result.append(capped)
    if any_stripped:
        out._drop(loc, GUARD_STRIPPED)
    return result


def resolve_bindings(
    nodes: list[dict], doc: Any, *, surface: str = "null", ref: "str | None" = None,
) -> ResolvedPresentation:
    """Join a validated (normalized) blueprint to ``doc``, neutralizing every
    render-leaf through the ``surface``-selected seam.

    ``ref`` is the presented data's ``data_ref`` (``None`` for inline data) — it
    is threaded into a `table`/`list` node's ``truncation_tail`` only when
    ``cap_rows`` actually capped that node's rows (§5, issue #2669); it does
    nothing when no row slot was capped.

    Returns a :class:`ResolvedPresentation` — the render model plus the compact
    binding stats (``bindings_resolved`` / ``bindings_dropped`` / ``rows`` /
    ``all_bindings_missed``) the op ack + ``presented`` event carry.
    """
    neut = get_neutralizer(surface)
    out = ResolvedPresentation()
    for i, node in enumerate(nodes):
        loc = f"blueprint[{i}]"
        component = node["component"]
        rendered: dict[str, Any] = {"component": component}
        if component in {"text", "markdown", "code", "diff"}:
            if "text" in node:
                val = _render_text_slot(node["text"], doc, out, neut, loc=f"{loc}.text")
                if val is not _MISSING:
                    rendered["text"] = val
            if component == "code" and "language" in node:
                rendered["language"] = _guard_maybe(
                    node["language"], neut, out, path=f"{loc}.language"
                )
        elif component == "keyvalue":
            rows_out = []
            for j, row in enumerate(node["rows"]):
                val = _render_text_slot(row["value"], doc, out, neut, loc=f"{loc}.rows[{j}].value")
                if val is not _MISSING:
                    label = _guard_maybe(row["label"], neut, out, path=f"{loc}.rows[{j}].label")
                    rows_out.append({"label": label, "value": val})
            rendered["rows"] = rows_out
        elif component == "table":
            rows, _found, omitted = _bind_rows_slot(node.get("rows"), doc, out)
            columns_out = []
            for j, col in enumerate(node.get("columns", [])):
                cells = _bind_row_relative(rows, col["path"], out, neut)
                header = _guard_maybe(col["header"], neut, out, path=f"{loc}.columns[{j}].header")
                columns_out.append({"header": header, "cells": cells})
            rendered["columns"] = columns_out
            rendered["row_count"] = len(rows)
            if omitted:
                rendered["truncation_tail"] = _truncation_tail(omitted, ref)
        elif component == "list":
            rows, _found, omitted = _bind_rows_slot(node.get("items"), doc, out)
            if "item_path" in node:
                rendered["items"] = _bind_row_relative(rows, node["item_path"], out, neut)
            else:
                rendered["items"] = _guard_list_items(rows, out, neut, loc=f"{loc}.items")
            if omitted:
                rendered["truncation_tail"] = _truncation_tail(omitted, ref)
        elif component == "image":
            if "src" in node:
                val = _render_text_slot(node["src"], doc, out, neut, loc=f"{loc}.src")
                if val is not _MISSING:
                    rendered["src"] = val
            if "alt" in node:
                rendered["alt"] = _guard_maybe(node["alt"], neut, out, path=f"{loc}.alt")
        out.nodes.append(rendered)
    return out
