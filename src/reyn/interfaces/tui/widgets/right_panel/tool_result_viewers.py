"""Content-type-aware viewers for tool results (#1154 Phase 1–3).

A pluggable registry mapping a tool result's content-type / MIME to a Rich
renderable. The Right Panel events-tab preview (``_show_event_in_preview``)
consults this to render ``tool_returned`` / ``tool_executed`` results richer
than the generic YAML fallback — the Jupyter-style content-type → viewer
vision from #1154.

Design (lead-ratified #1154 Phase 1–3):
  - keyed by ``content_type`` / ``mimeType`` (the fields file/web/mcp op
    results already carry); explicit type wins, then a shape-sniff pass.
  - Phase 1 viewers: markdown (rendered) + CSV/table. Phase 2a adds a JSON
    viewer (formatted + syntax-highlighted); Phase 2b adds an image metadata
    card (mime / size / source — avoids dumping the base64 blob); Phase 2c
    adds a shape-sniffed web-page-summary card (web-fetch HTML preview, which
    carries no content_type but a distinctive field set).
  - Phase 3 (S1): inline if/elif replaced with a pluggable ``_ViewerEntry``
    registry; ``register_viewer()`` lets callers add new viewers without
    touching the dispatch core. Byte-behavior identical to Phase 2c.
  - Phase 3 (S2): ``TemplateSchema`` dataclass + ``_apply_template`` (label
    AND value escaped; both are untrusted content). ``_SHAPE_TEMPLATE_CACHE``
    for per-session shape fingerprint → schema caching. Not yet reachable
    (S3 adds LLM generation; S4 wires the async path at the call site).
  - Phase 3 (S3–S4, deferred): LLM-generated viewer templates for novel
    types; email-card deferred until a real in-repo email result producer
    exists.

This module is intentionally pure (dict in → Rich renderable | None) so it
is testable in isolation without the Textual app.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from rich.console import RenderableType
from rich.json import JSON as RichJSON
from rich.markdown import Markdown as RichMarkdown
from rich.table import Table

# Cap rows rendered in a preview table — a 10k-row CSV result must not blow
# up the preview pane. Past the cap the table shows a "… N more rows" footer.
_MAX_TABLE_ROWS = 50


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class _ViewerEntry:
    predicate: Callable[[dict], bool]
    viewer: Callable[[dict], RenderableType | None]
    name: str = ""


_VIEWERS: list[_ViewerEntry] = []


def register_viewer(
    predicate: Callable[[dict], bool],
    viewer: Callable[[dict], RenderableType | None],
    *,
    name: str = "",
    position: int = -1,
) -> None:
    """Register a viewer in the ordered dispatch list.

    First matching entry wins. ``position=-1`` appends (lowest priority);
    ``position=0`` inserts at the front (highest priority).
    """
    entry = _ViewerEntry(predicate=predicate, viewer=viewer, name=name)
    if position < 0:
        _VIEWERS.append(entry)
    else:
        _VIEWERS.insert(position, entry)


# ---------------------------------------------------------------------------
# Helpers shared by viewers
# ---------------------------------------------------------------------------

def _content_type_of(result: dict) -> str:
    """Best-effort lowercase content-type / MIME string from a result dict."""
    for key in ("content_type", "mimeType", "mime_type", "media_type"):
        v = result.get(key)
        if isinstance(v, str) and v:
            return v.lower()
    blocks = result.get("media_blocks")
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        m = blocks[0].get("mimeType") or blocks[0].get("mime_type")
        if isinstance(m, str) and m:
            return m.lower()
    return ""


def _result_text(result: dict) -> str:
    """Best-effort textual payload from a result dict (content/text/body)."""
    for key in ("content", "text", "body"):
        v = result.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _human_bytes(n: int) -> str:
    """A compact human-readable byte size (e.g. 12.3 KB)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# Built-in viewers (Phase 1–2c)
# ---------------------------------------------------------------------------

def _viewer_markdown(result: dict) -> RenderableType | None:
    text = _result_text(result)
    if not text:
        return None
    return RichMarkdown(text)


def _viewer_csv(result: dict) -> RenderableType | None:
    text = _result_text(result)
    if not text:
        return None
    rows = [ln.split(",") for ln in text.splitlines() if ln.strip()]
    if not rows:
        return None
    header = [c.strip() for c in rows[0]]
    if not header:
        return None
    table = Table(show_header=True, header_style="bold", expand=False)
    for col in header:
        table.add_column(col)
    body = rows[1:]
    for r in body[:_MAX_TABLE_ROWS]:
        cells = [c.strip() for c in r][: len(header)]
        cells += [""] * (len(header) - len(cells))
        table.add_row(*cells)
    if len(body) > _MAX_TABLE_ROWS:
        table.caption = f"… {len(body) - _MAX_TABLE_ROWS} more rows"
    return table


def _viewer_json(result: dict) -> RenderableType | None:
    """Syntax-highlighted, indented JSON (Phase 2a)."""
    text = _result_text(result)
    if not text:
        return None
    try:
        return RichJSON(text)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _viewer_image(result: dict) -> RenderableType | None:
    """Metadata card for an image result (Phase 2b)."""
    blocks = result.get("media_blocks")
    block = (
        blocks[0]
        if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict)
        else {}
    )
    mime = _content_type_of(result) or "image"
    table = Table(show_header=False, box=None, expand=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("type", mime)

    size = result.get("size_bytes")
    if not isinstance(size, int):
        data = block.get("data")
        if isinstance(data, str) and data:
            size = (len(data) * 3) // 4
    if isinstance(size, int):
        table.add_row("size", _human_bytes(size))

    source = result.get("path") or result.get("url")
    if isinstance(source, str) and source:
        table.add_row("source", source)

    n_blocks = len([b for b in (blocks or []) if isinstance(b, dict)])
    if n_blocks > 1:
        table.add_row("blocks", str(n_blocks))

    table.caption = "🖼  image not rendered inline (terminal preview)"
    return table


_WEB_SUMMARY_KEYS = ("title", "outline", "first_paragraph", "link_count")
_MAX_OUTLINE_ROWS = 12


def _looks_like_web_summary(result: dict) -> bool:
    return all(k in result for k in _WEB_SUMMARY_KEYS)


def _viewer_web_summary(result: dict) -> RenderableType | None:
    """Card for a web-fetch HTML page summary (Phase 2c, shape-sniffed)."""
    table = Table(show_header=False, box=None, expand=False)
    table.add_column("field", style="bold")
    table.add_column("value")

    title = result.get("title")
    if isinstance(title, str) and title:
        table.add_row("title", title)

    para = result.get("first_paragraph")
    if isinstance(para, str) and para:
        table.add_row("summary", para)

    outline = result.get("outline")
    if isinstance(outline, list) and outline:
        rows = [str(h) for h in outline[:_MAX_OUTLINE_ROWS] if str(h).strip()]
        extra = len(outline) - len(rows)
        if rows:
            joined = "\n".join(rows)
            if extra > 0:
                joined += f"\n… {extra} more"
            table.add_row("outline", joined)

    links = result.get("link_count")
    if isinstance(links, int):
        table.add_row("links", str(links))

    table.caption = "🌐  web page summary"
    return table


# ---------------------------------------------------------------------------
# Default registry population (Phase 1–2c, same priority as the former
# inline chain: explicit content-type first, shape-sniff last)
# ---------------------------------------------------------------------------

def _pred_markdown(r: dict) -> bool:
    ct = _content_type_of(r)
    return bool(ct) and ("markdown" in ct or ct.endswith("/md"))

def _pred_csv(r: dict) -> bool:
    ct = _content_type_of(r)
    return bool(ct) and ("csv" in ct or "tab-separated" in ct)

def _pred_json(r: dict) -> bool:
    ct = _content_type_of(r)
    return bool(ct) and "json" in ct

def _pred_image(r: dict) -> bool:
    ct = _content_type_of(r)
    return bool(ct) and ct.startswith("image/")


register_viewer(_pred_markdown, _viewer_markdown, name="markdown")
register_viewer(_pred_csv, _viewer_csv, name="csv")
register_viewer(_pred_json, _viewer_json, name="json")
register_viewer(_pred_image, _viewer_image, name="image")
register_viewer(_looks_like_web_summary, _viewer_web_summary, name="web_summary")


# ---------------------------------------------------------------------------
# Phase 3 S2: TemplateSchema + safe _apply_template
# (S3 will add async LLM generation; S4 will wire it at the call site)
# ---------------------------------------------------------------------------

@dataclass
class TemplateSchema:
    """Display schema produced by the LLM (S3) and applied by _apply_template.

    ``rows`` is a list of (escaped_label, field_key) pairs. Labels are escaped
    at schema construction time (S3). ``caption`` is also pre-escaped.
    Field keys are validated against the result dict at construction.
    """
    rows: list[tuple[str, str]]
    caption: str = ""


# Per-session in-memory cache: shape fingerprint → TemplateSchema | None.
# None means "LLM generation was attempted and failed; do not retry."
_SHAPE_TEMPLATE_CACHE: dict[frozenset[str], TemplateSchema | None] = {}


def _shape_fingerprint(result: dict) -> frozenset[str]:
    """Stable cache key for a result — the frozenset of its top-level keys."""
    return frozenset(result.keys())


def _apply_template(result: dict, schema: TemplateSchema) -> RenderableType:
    """Render a result dict using a TemplateSchema as a Rich Table.

    Safety contract:
    - ``label`` values are pre-escaped at schema construction (S3).
    - ``field_key`` values are validated against ``result.keys()`` at schema
      construction; missing fields are silently skipped here.
    - ``result`` values are untrusted external content (#1822 threat surface).
      ``escape(str(val)[:500])`` strips any Rich markup before display.
    """
    from rich.markup import escape

    table = Table(show_header=False, box=None, expand=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for label, field_key in schema.rows:
        val = result.get(field_key)
        if val is None:
            continue
        table.add_row(label, escape(str(val)[:500]))
    if schema.caption:
        table.caption = schema.caption
    return table


# ---------------------------------------------------------------------------
# Public dispatch entry point (API unchanged from Phase 1–2c)
# ---------------------------------------------------------------------------

def render_tool_result(result: Any) -> RenderableType | None:
    """Pick a viewer for ``result`` (or ``None`` for the YAML fallback).

    Walks the registered viewer list in order; returns the first non-None
    renderable. Returns ``None`` when ``result`` is not a dict, nothing
    matches, or the matched viewer has nothing to render — the caller then
    falls back to the generic YAML preview.
    """
    if not isinstance(result, dict):
        return None
    for entry in _VIEWERS:
        if entry.predicate(result):
            return entry.viewer(result)
    return None
