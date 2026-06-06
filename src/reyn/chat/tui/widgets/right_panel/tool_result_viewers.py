"""Content-type-aware viewers for tool results (#1154 Phase 1).

A small registry mapping a tool result's content-type / MIME to a Rich
renderable. The Right Panel events-tab preview (``_show_event_in_preview``)
consults this to render ``tool_returned`` / ``tool_executed`` results richer
than the generic YAML fallback — the first slice of the Jupyter-style
content-type → viewer vision in #1154.

Design (lead-ratified #1154 Phase 1–2):
  - keyed by ``content_type`` / ``mimeType`` (the fields file/web/mcp op
    results already carry); explicit type wins, then a shape-sniff pass.
  - Phase 1 viewers: markdown (rendered) + CSV/table. Phase 2a adds a JSON
    viewer (formatted + syntax-highlighted); Phase 2b adds an image metadata
    card (mime / size / source — avoids dumping the base64 blob); Phase 2c
    adds a shape-sniffed web-page-summary card (web-fetch HTML preview, which
    carries no content_type but a distinctive field set). Unknown / unmatched
    → ``render_tool_result`` returns ``None`` so the caller falls back to the
    existing YAML preview (degrade, never hide content).
  - Phase 3 (deferred): LLM-generated viewer templates for novel types;
    email-card deferred until a real in-repo email result producer exists.

This module is intentionally pure (dict in → Rich renderable | None) so it
is testable in isolation without the Textual app.
"""
from __future__ import annotations

import json
from typing import Any

from rich.console import RenderableType
from rich.json import JSON as RichJSON
from rich.markdown import Markdown as RichMarkdown
from rich.table import Table

# Cap rows rendered in a preview table — a 10k-row CSV result must not blow
# up the preview pane. Past the cap the table shows a "… N more rows" footer.
_MAX_TABLE_ROWS = 50


def _content_type_of(result: dict) -> str:
    """Best-effort lowercase content-type / MIME string from a result dict.

    Checks the explicit fields op results carry (``content_type`` on
    file/web, ``mimeType`` on mcp/media blocks). Returns ``""`` when no
    type is discoverable — the caller then falls back to YAML.
    """
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
        cells += [""] * (len(header) - len(cells))  # pad ragged rows
        table.add_row(*cells)
    if len(body) > _MAX_TABLE_ROWS:
        table.caption = f"… {len(body) - _MAX_TABLE_ROWS} more rows"
    return table


def _viewer_json(result: dict) -> RenderableType | None:
    """Syntax-highlighted, indented JSON (Phase 2a).

    Parses the text payload and renders via ``rich.json.JSON`` (formats +
    highlights). Returns ``None`` when there's nothing to render or the
    payload isn't valid JSON — the caller then falls back to YAML, which
    still shows the raw event (degrade, never hide content).
    """
    text = _result_text(result)
    if not text:
        return None
    try:
        return RichJSON(text)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _viewer_image(result: dict) -> RenderableType | None:
    """Metadata card for an image result (Phase 2b).

    A terminal preview can't raster the image, and the raw result carries a
    large base64 ``data`` blob — so YAML fallback would spew that blob. This
    card summarizes the useful metadata (mime / size / source) instead, which
    is strictly more readable than the raw dump. Always returns a renderable
    (never None) when dispatched, since the content-type alone is informative.
    """
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
            size = (len(data) * 3) // 4  # approx decoded bytes from base64 len
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


# Shape-sniff (Phase 2c): some results carry no ``content_type`` but have a
# distinctive field set. The web-fetch HTML preview (web.py ``_generate_web_
# fetch_preview``) returns ``{title, outline, first_paragraph, link_count,
# content_chars}``. We require ALL of these distinctive keys to match — a
# single-field sniff (e.g. just ``title``) would false-positive on unrelated
# results, so precision comes from the combination.
_WEB_SUMMARY_KEYS = ("title", "outline", "first_paragraph", "link_count")
_MAX_OUTLINE_ROWS = 12


def _looks_like_web_summary(result: dict) -> bool:
    """True when *result* carries the full web-page-summary field set."""
    return all(k in result for k in _WEB_SUMMARY_KEYS)


def _viewer_web_summary(result: dict) -> RenderableType | None:
    """Card for a web-fetch HTML page summary (Phase 2c, shape-sniffed).

    Renders the distilled page (title / first paragraph / heading outline /
    link count) — far more readable than the raw nested dict. Returns the
    card; the dispatcher only calls this once the shape has matched.
    """
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


def render_tool_result(result: Any) -> RenderableType | None:
    """Pick a viewer for ``result`` (or ``None`` for the YAML fallback).

    Explicit ``content_type`` / MIME wins; when none matches, a shape-sniff
    pass handles results that carry a distinctive field set but no content
    type (web-page summary). ``None`` when ``result`` is not a dict, nothing
    matches, or the matched viewer has nothing to render — the caller then
    falls back to the generic YAML preview.
    """
    if not isinstance(result, dict):
        return None
    ct = _content_type_of(result)
    if ct:
        if "markdown" in ct or ct.endswith("/md"):
            return _viewer_markdown(result)
        if "csv" in ct or "tab-separated" in ct:
            return _viewer_csv(result)
        if "json" in ct:
            return _viewer_json(result)
        if ct.startswith("image/"):
            return _viewer_image(result)
    # No explicit content-type match → shape-sniff distinctive field sets.
    if _looks_like_web_summary(result):
        return _viewer_web_summary(result)
    return None
