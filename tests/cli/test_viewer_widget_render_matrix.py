"""Tier 2: per-content-type viewer dispatch through the REAL widget render path.

Durable hardening (scripted-fixture slice, 2026-06-20). The pure
``render_tool_result`` is well tested per content-type, and the widget wire test
(#1154) covers markdown + the unknown-type fallback — but the actual
``RightPanel._show_event_in_preview`` render path was NOT exercised for the
other content-types (csv / json / image / email / diff). That wiring is the
#1891-class blind spot: a viewer can be correct in isolation yet fail to reach
the pane through the widget (e.g. the ``ev["data"]["result"]`` nesting seam, or
a renderable the pane can't show). This matrix drives each content-type through
the mounted widget and asserts the right viewer output lands in the preview
pane — model-free, render-path-direct.

Falsification: a wire that read the wrong result key, or a viewer dropped from
the registry, makes a row's expected marker absent from the captured render.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _render(renderable) -> str:
    buf = io.StringIO()
    Console(file=buf, highlight=False, markup=True, width=100).print(renderable)
    return buf.getvalue()


# Each row: (id, result-dict, marker that must appear in the rendered preview).
_GIT_DIFF = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+print('hi')\n"
)
_MATRIX = [
    ("markdown", {"content_type": "text/markdown", "content": "# Heading\n\nbody"}, "Heading"),
    ("csv", {"content_type": "text/csv", "content": "name,age\nalice,30"}, "name"),
    ("json", {"content_type": "application/json", "content": '{"k": "vvv"}'}, "vvv"),
    ("image", {"mimeType": "image/png",
               "media_blocks": [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]},
     "image/png"),
    ("email", {"content_type": "message/rfc822", "from": "a@x", "to": "b@y",
               "subject": "Subj", "body": "Hello body"}, "Subj"),
    ("diff", {"content_type": "text/x-diff", "content": _GIT_DIFF}, "x.py"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("ctype, result, marker", _MATRIX, ids=[r[0] for r in _MATRIX])
async def test_content_type_renders_via_widget_path(ctype, result, marker) -> None:
    """Tier 2: each content-type dispatches through _show_event_in_preview to the pane."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel
    from reyn.interfaces.tui.widgets.right_panel.shells import _PreviewPane

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        pane = panel.query_one("#preview-pane", _PreviewPane)

        captured: list = []
        original = pane.show_text
        pane.show_text = lambda title, r: (captured.append(r), original(title, r))[1]

        panel._events_visible = [{"type": "tool_returned", "data": {"result": result}}]
        panel._events_cursor = 0
        panel._show_event_in_preview(pane)
        await pilot.pause()

        assert captured, f"{ctype}: preview pane received no renderable"
        rendered = _render(captured[-1])
        assert marker in rendered, (
            f"{ctype}: expected {marker!r} in the widget-rendered preview "
            f"(viewer dispatch via _show_event_in_preview broke); got:\n{rendered!r}"
        )


@pytest.mark.asyncio
async def test_email_value_escaped_through_widget_path() -> None:
    """Tier 2: untrusted email value markup stays literal through the real pane (#1822).

    Proves the escape applies on the actual render path, not just the pure
    viewer — a markup-injecting subject must not be interpreted by the pane.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel
    from reyn.interfaces.tui.widgets.right_panel.shells import _PreviewPane

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        pane = panel.query_one("#preview-pane", _PreviewPane)
        captured: list = []
        original = pane.show_text
        pane.show_text = lambda title, r: (captured.append(r), original(title, r))[1]

        panel._events_visible = [{
            "type": "tool_returned",
            "data": {"result": {"content_type": "message/rfc822", "from": "e@x",
                                "subject": "[bold]inject[/bold]", "body": "b"}},
        }]
        panel._events_cursor = 0
        panel._show_event_in_preview(pane)
        await pilot.pause()

        assert captured
        rendered = _render(captured[-1])
        assert "[bold]" in rendered, f"markup must render literally (escaped); got {rendered!r}"
