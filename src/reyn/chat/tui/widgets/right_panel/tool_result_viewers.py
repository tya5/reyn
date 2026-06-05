"""Content-type-aware viewers for tool results (#1154 Phase 1).

A small registry mapping a tool result's content-type / MIME to a Rich
renderable. The Right Panel events-tab preview (``_show_event_in_preview``)
consults this to render ``tool_returned`` / ``tool_executed`` results richer
than the generic YAML fallback — the first slice of the Jupyter-style
content-type → viewer vision in #1154.

Design (lead-ratified #1154 Phase 1):
  - keyed by ``content_type`` / ``mimeType`` (the fields file/web/mcp op
    results already carry); no shape-sniff yet.
  - Phase 1 viewers: markdown (rendered) + CSV/table. Unknown types →
    ``render_tool_result`` returns ``None`` so the caller falls back to the
    existing YAML preview (degrade, never hide content).
  - Phase 3 (deferred): LLM-generated viewer templates for novel types.

This module is intentionally pure (dict in → Rich renderable | None) so it
is testable in isolation without the Textual app.
"""
from __future__ import annotations

from typing import Any

from rich.console import RenderableType
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


def render_tool_result(result: Any) -> RenderableType | None:
    """Pick a content-type viewer for ``result`` (or ``None`` for fallback).

    ``None`` when ``result`` is not a dict, carries no recognizable
    content-type, or the matched viewer has nothing to render — the caller
    falls back to the generic YAML preview.
    """
    if not isinstance(result, dict):
        return None
    ct = _content_type_of(result)
    if not ct:
        return None
    if "markdown" in ct or ct.endswith("/md"):
        return _viewer_markdown(result)
    if "csv" in ct or "tab-separated" in ct:
        return _viewer_csv(result)
    return None
