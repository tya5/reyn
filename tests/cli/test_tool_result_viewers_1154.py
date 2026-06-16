"""Tier 2: content-type → viewer registry for tool results (#1154 Phase 1).

``render_tool_result(result)`` maps a tool result dict's content-type /
MIME to a Rich renderable (markdown / CSV-table in Phase 1), or returns
``None`` so the Right Panel events-tab preview falls back to its generic
YAML rendering. These pin the dispatch + the fallback contract via the
pure module's public return value (no Textual app needed).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.json import JSON as RichJSON
from rich.markdown import Markdown as RichMarkdown
from rich.table import Table

from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import render_tool_result

# ── dispatch: recognized content-types route to a viewer ─────────────────────


def test_markdown_content_type_returns_markdown_renderable() -> None:
    """Tier 2: a text/markdown result renders via the markdown viewer."""
    out = render_tool_result({"content_type": "text/markdown", "content": "# Title\n\nbody"})
    assert isinstance(out, RichMarkdown), f"expected RichMarkdown; got {type(out)!r}"


def test_csv_content_type_returns_table() -> None:
    """Tier 2: a text/csv result renders via the table viewer with columns."""
    out = render_tool_result({"content_type": "text/csv", "content": "name,age\nalice,30\nbob,25"})
    assert isinstance(out, Table), f"expected Table; got {type(out)!r}"
    headers = [str(c.header) for c in out.columns]
    assert headers == ["name", "age"], f"expected CSV header → table columns; got {headers}"


def test_mimetype_key_also_detected() -> None:
    """Tier 2: the ``mimeType`` field (mcp/media) is detected like content_type."""
    out = render_tool_result({"mimeType": "text/markdown", "content": "**bold**"})
    assert isinstance(out, RichMarkdown)


def test_media_blocks_mimetype_detected() -> None:
    """Tier 2: a media_blocks[0].mimeType surfaces the content-type."""
    out = render_tool_result({
        "media_blocks": [{"type": "text", "mimeType": "text/markdown"}],
        "content": "# md",
    })
    assert isinstance(out, RichMarkdown)


# ── fallback: anything unrecognized → None (caller renders YAML) ─────────────


def test_unknown_content_type_returns_none() -> None:
    """Tier 2: an unregistered content-type returns None → YAML fallback."""
    assert render_tool_result({"content_type": "application/x-unknown-binary", "content": "x"}) is None


def test_no_content_type_returns_none() -> None:
    """Tier 2: a result with no content-type field returns None → fallback."""
    assert render_tool_result({"status": "ok", "path": "/tmp/x"}) is None


def test_non_dict_returns_none() -> None:
    """Tier 2: a non-dict result (str / None) returns None → fallback."""
    assert render_tool_result("plain string") is None
    assert render_tool_result(None) is None


def test_markdown_empty_content_returns_none() -> None:
    """Tier 2: recognized type but empty payload → None (nothing to render)."""
    assert render_tool_result({"content_type": "text/markdown", "content": ""}) is None


# ── table viewer details ─────────────────────────────────────────────────────


def test_csv_ragged_rows_padded_not_crashing() -> None:
    """Tier 2: a CSV row shorter than the header is padded, not a crash."""
    out = render_tool_result({"content_type": "text/csv", "content": "a,b,c\n1,2\n3,4,5"})
    assert isinstance(out, Table)
    headers = [str(c.header) for c in out.columns]
    assert headers == ["a", "b", "c"], f"ragged CSV should keep header columns; got {headers}"


def test_csv_caps_rows_with_overflow_caption() -> None:
    """Tier 2: a CSV past the row cap renders a bounded table + overflow caption."""
    lines = ["h"] + [str(i) for i in range(200)]
    out = render_tool_result({"content_type": "text/csv", "content": "\n".join(lines)})
    assert isinstance(out, Table)
    assert out.caption and "more rows" in str(out.caption)


# ── JSON viewer (Phase 2a) ───────────────────────────────────────────────────


def test_json_content_type_returns_json_renderable() -> None:
    """Tier 2: an application/json result renders via the JSON viewer."""
    out = render_tool_result({"content_type": "application/json", "content": '{"a": 1, "b": [2, 3]}'})
    assert isinstance(out, RichJSON), f"expected RichJSON; got {type(out)!r}"


def test_json_invalid_payload_returns_none() -> None:
    """Tier 2: an application/json result with non-JSON text → None → fallback."""
    assert render_tool_result({"content_type": "application/json", "content": "not json {{{"}) is None


def test_json_empty_content_returns_none() -> None:
    """Tier 2: an application/json result with no payload → None → fallback."""
    assert render_tool_result({"content_type": "application/json", "content": ""}) is None


# ── image metadata card (Phase 2b) ───────────────────────────────────────────


def _render_to_text(renderable: object, width: int = 120) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=width).print(renderable)
    return buf.getvalue()


def test_image_via_media_blocks_returns_card_with_mime() -> None:
    """Tier 2: a file-shape image result (mime in media_blocks) → metadata card."""
    out = render_tool_result({
        "kind": "file", "path": "pic.png", "content": "",
        "media_blocks": [{"type": "image", "data": "QUJD", "mimeType": "image/png"}],
    })
    assert isinstance(out, Table), f"expected a Table card; got {type(out)!r}"
    text = _render_to_text(out)
    assert "image/png" in text, "card should show the image mime type"


def test_image_web_shape_shows_size_and_source() -> None:
    """Tier 2: a web-shape image result → card surfaces size + source url."""
    out = render_tool_result({
        "content_type": "image/jpeg", "size_bytes": 2048, "url": "https://ex/i.jpg",
        "media_blocks": [{"type": "image", "data": "QUJD", "mimeType": "image/jpeg"}],
    })
    assert isinstance(out, Table)
    text = _render_to_text(out)
    assert "image/jpeg" in text
    assert "https://ex/i.jpg" in text, "card should show the source url"
    assert "KB" in text or "2.0" in text, "card should show a human-readable size"


def test_image_card_does_not_dump_base64_blob() -> None:
    """Tier 2: the card omits the base64 data blob (the YAML-fallback pitfall)."""
    blob = "BASE64BLOBABCDEF" * 64
    out = render_tool_result({
        "content_type": "image/png", "path": "big.png",
        "media_blocks": [{"type": "image", "data": blob, "mimeType": "image/png"}],
    })
    assert isinstance(out, Table)
    text = _render_to_text(out)
    assert blob not in text, "image card must not spew the base64 data blob"
    assert "image/png" in text


# ── web-page-summary shape-sniff card (Phase 2c) ─────────────────────────────


def _web_summary_result(**over: object) -> dict:
    """A web-fetch HTML preview result (web.py _generate_web_fetch_preview shape)."""
    base = {
        "title": "Example Page",
        "outline": ["H1: Welcome", "H2: Details"],
        "first_paragraph": "An example page.",
        "link_count": 7,
        "content_chars": 1234,
    }
    base.update(over)
    return base


def test_web_summary_shape_renders_card() -> None:
    """Tier 2: a no-content_type result with the full web-summary shape → card."""
    out = render_tool_result(_web_summary_result())
    assert isinstance(out, Table), f"expected a Table card; got {type(out)!r}"
    text = _render_to_text(out)
    assert "Example Page" in text, "card should show the page title"
    assert "Welcome" in text, "card should show the heading outline"
    assert "7" in text, "card should show the link count"


def test_web_summary_partial_shape_not_sniffed() -> None:
    """Tier 2: precision — a partial field set must NOT shape-sniff as web summary."""
    # title + first_paragraph present but outline + link_count missing → None.
    assert render_tool_result({"title": "X", "first_paragraph": "y"}) is None


def test_web_summary_single_title_field_not_sniffed() -> None:
    """Tier 2: precision — a lone common field (title) must not false-positive."""
    assert render_tool_result({"title": "Just a title"}) is None


def test_web_summary_outline_capped_with_overflow() -> None:
    """Tier 2: a long outline is capped with an overflow indicator."""
    out = render_tool_result(_web_summary_result(outline=[f"H2: item {i}" for i in range(30)]))
    assert isinstance(out, Table)
    text = _render_to_text(out)
    assert "more" in text, "capped outline should show an overflow indicator"


# ── wire: _show_event_in_preview routes tool events through the viewer ───────
# These exercise the integration seam the pure tests above cannot: an event's
# emit kwargs are nested under ``data`` (events.py ``Event(type=, data=)``),
# so the result dict lives at ``ev["data"]["result"]``. A wire reading the
# wrong key silently never fires the viewer (always YAML) — these guard that.


def _capture_preview_renderable(pane: object) -> list:
    """Wrap a real preview pane's show_text to record the renderable it gets."""
    captured: list = []
    original = pane.show_text  # type: ignore[attr-defined]

    def _spy(title: str, renderable: object) -> None:
        captured.append(renderable)
        original(title, renderable)

    pane.show_text = _spy  # type: ignore[attr-defined]
    return captured


@pytest.mark.asyncio
async def test_wire_tool_returned_md_event_renders_via_viewer() -> None:
    """Tier 2: a tool_returned event with a markdown result → viewer fires.

    Guards the data-nesting seam: the result dict is at ev["data"]["result"],
    so a wire reading ev["result"] would fall back to YAML (regression).
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel
    from reyn.interfaces.tui.widgets.right_panel.shells import _PreviewPane

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        pane = panel.query_one("#preview-pane", _PreviewPane)
        captured = _capture_preview_renderable(pane)

        panel._events_visible = [
            {"type": "tool_returned",
             "data": {"result": {"content_type": "text/markdown", "content": "# Hi"}}},
        ]
        panel._events_cursor = 0
        panel._show_event_in_preview(pane)
        await pilot.pause()

        assert captured, "preview pane received no renderable"
        assert isinstance(captured[-1], RichMarkdown), (
            f"markdown tool result should render via the viewer; "
            f"got {type(captured[-1])!r} (wire likely read the wrong result key)"
        )


@pytest.mark.asyncio
async def test_wire_unknown_type_event_falls_back_and_shows_content() -> None:
    """Tier 2: an unrecognized content-type → YAML fallback that still shows content.

    The "never hide content" guard: when no viewer matches, the preview
    must fall back to the generic YAML render of the event — and that
    render must still surface the result payload, not an empty pane.
    """
    import io

    from rich.console import Console

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel
    from reyn.interfaces.tui.widgets.right_panel.shells import _PreviewPane

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        pane = panel.query_one("#preview-pane", _PreviewPane)
        captured = _capture_preview_renderable(pane)

        panel._events_visible = [
            {"type": "tool_returned",
             "data": {"result": {"content_type": "application/x-unknown-binary",
                                 "note": "ZZZSENTINEL"}}},
        ]
        panel._events_cursor = 0
        panel._show_event_in_preview(pane)
        await pilot.pause()

        assert captured, "preview pane received no renderable"
        # No viewer should have matched (not markdown / not JSON).
        assert not isinstance(captured[-1], (RichMarkdown, RichJSON)), (
            "unrecognized content-type should fall back to the YAML preview"
        )
        # …and the fallback must still surface the payload (never hide content).
        buf = io.StringIO()
        Console(file=buf, width=120).print(captured[-1])
        assert "ZZZSENTINEL" in buf.getvalue(), (
            "YAML fallback must still render the result payload, not hide it"
        )
