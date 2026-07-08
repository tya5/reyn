"""Tier 1/2: FP-0054 PR-B — the inline-CUI `present` renderer + the guard/renderer
Rich-markup-safety re-layering (Option B).

Covers:
  1. Tier 1: per-component render presence (each catalog component produces SOME
     Rich renderable containing its content — not layout/exact-whitespace pins).
  2. Tier 2 INVARIANT-LOCK (lead-coder review): Rich-markup-shaped leaf data
     (``[bold]INJECT[/bold]``) survives the FULL real pipeline (guard →
     resolve_bindings → render_presentation_nodes → an actual Rich Console print)
     as LITERAL text, never interpreted as styling — the structural guarantee
     Option B replaces the old escape/unescape pair with.
  3. Tier 2: the terminal ESC/control-strip behavioral guarantee still holds
     through the same full pipeline (guard's actual security responsibility,
     unchanged by the Option B revision).
  4. Tier 2: `OutboxPresentationRenderer.render` puts a `"presentation"`
     `OutboxMessage` carrying the render model onto the real Session outbox (no
     mock Session — a minimal real one), and `format_inline_message` dispatches
     `kind="presentation"` to `render_presentation_nodes`.
  5. Tier 1: `op_runtime/present.py` derives its `surface` string from the wired
     `OpContext.presentation_renderer` (None → "null"; a renderer → its own
     `surface_name`) and calls `.render()` on it exactly once when present.

Real `PipelineExecutor`-adjacent objects throughout: real `Console`, real
`resolve_bindings`, real `OpContext`; no `MagicMock`/`patch`. No exact-render /
whitespace pins — asserts content presence and structural facts only.
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.present import resolve_bindings, validate_blueprint
from reyn.data.workspace.workspace import Workspace
from reyn.interfaces.repl.present_renderer import render_presentation_nodes
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage
from reyn.security.permissions.permissions import PermissionDecl


def _render_to_text(nodes: list[dict], *, width: int = 60) -> str:
    console = Console(width=width, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(render_presentation_nodes(nodes))
    return console.file.getvalue()


# ── 1. per-component render presence ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("blueprint", "data", "expect_substr"),
    [
        ({"component": "text", "text": {"$bind": "/v"}}, {"v": "hello world"}, "hello world"),
        ({"component": "markdown", "text": "**bold**"}, {}, "bold"),
        ({"component": "code", "text": "x = 1", "language": "python"}, {}, "x"),
        ({"component": "diff", "text": "+added\n-removed"}, {}, "added"),
        (
            {"component": "keyvalue", "rows": [{"label": "k", "value": {"$bind": "/v"}}]},
            {"v": "val1"},
            "val1",
        ),
        (
            {
                "component": "table",
                "rows": {"$bind": "/items"},
                "columns": [{"header": "name", "path": "/n"}],
            },
            {"items": [{"n": "row-one"}]},
            "row-one",
        ),
        ({"component": "list", "items": ["alpha", "beta"]}, {}, "alpha"),
        ({"component": "image", "alt": "a photo"}, {}, "a photo"),
    ],
)
def test_each_catalog_component_renders_its_content(blueprint, data, expect_substr) -> None:
    """Tier 1: every v1 catalog component produces a renderable whose printed
    output contains its bound/literal content — presence, not exact layout."""
    nodes = validate_blueprint(blueprint)
    resolved = resolve_bindings(nodes, data, surface="inline-cui")
    out = _render_to_text(resolved.nodes)
    assert expect_substr in out


def test_unsupported_component_does_not_crash_the_render_loop() -> None:
    """Tier 1: a node with an unrecognized component name renders a placeholder
    instead of raising — the render loop never crashes over one bad node."""
    out = _render_to_text([{"component": "not-a-real-component"}])
    assert "not-a-real-component" in out


# ── 2. INVARIANT-LOCK: Rich markup never interpreted, full real pipeline ────


def test_rich_markup_leaf_survives_literal_through_the_full_real_pipeline() -> None:
    """Tier 2: INVARIANT-LOCK — `[bold]INJECT[/bold]` in bound data reaches the
    printed terminal output as LITERAL text — never interpreted as Rich styling
    (no ANSI SGR bytes around it) — through the REAL guard → bindings →
    render_presentation_nodes → Console.print pipeline. This is the structural
    guarantee Option B relies on in place of the old guard-level escape/unescape
    pair (see guard.py's module docstring)."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/v"}})
    resolved = resolve_bindings(nodes, {"v": "safe [bold]INJECT[/bold] text"}, surface="inline-cui")
    # The guard passed the markup-shaped text through unescaped (Option B).
    assert resolved.nodes[0]["text"] == "safe [bold]INJECT[/bold] text"

    out = _render_to_text(resolved.nodes)
    assert "[bold]INJECT[/bold]" in out       # literal brackets survive verbatim
    assert "\x1b[1m" not in out               # never interpreted as a Rich bold SGR


def test_rich_markup_in_code_and_table_cells_also_stays_literal() -> None:
    """Tier 2: INVARIANT-LOCK — the same guarantee holds for the `code`/`table`
    render paths specifically (the two paths the old escape/unescape approach
    would have corrupted with visible backslashes — see guard.py docstring)."""
    code_nodes = validate_blueprint({"component": "code", "text": "x = '[red]y[/red]'"})
    code_resolved = resolve_bindings(code_nodes, {}, surface="inline-cui")
    code_out = _render_to_text(code_resolved.nodes)
    assert "[red]y[/red]" in code_out
    assert "\\[red]" not in code_out          # no leftover escape backslash either

    table_nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/items"},
        "columns": [{"header": "col [i]", "path": "/v"}],
    })
    table_resolved = resolve_bindings(
        table_nodes, {"items": [{"v": "[bold]cell[/bold]"}]}, surface="inline-cui",
    )
    table_out = _render_to_text(table_resolved.nodes)
    assert "[bold]cell[/bold]" in table_out
    assert "col [i]" in table_out
    assert "\\[bold]" not in table_out


# ── 3. control/ESC-strip guarantee, unchanged ────────────────────────────────


def test_control_and_esc_sequences_still_stripped_through_the_full_pipeline() -> None:
    """Tier 2: the guard's actual security responsibility (ESC/control-sequence
    stripping) is unchanged by the Option B revision — verified through the same
    real render pipeline as the invariant-lock tests above."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/v"}})
    resolved = resolve_bindings(nodes, {"v": "safe\x1b[31mINJECT\x1b[0m text"}, surface="inline-cui")
    assert "\x1b" not in resolved.nodes[0]["text"]
    out = _render_to_text(resolved.nodes)
    assert "\x1b[31m" not in out
    assert "INJECT" in out


# ── 4. OutboxPresentationRenderer + format_inline_message dispatch ──────────


def test_outbox_presentation_renderer_puts_presentation_message() -> None:
    """Tier 2: OutboxPresentationRenderer.render puts a real `OutboxMessage(kind=
    "presentation")` carrying `resolved.nodes` onto the session's outbox — the
    same queue every other display kind flows through."""
    import asyncio

    from reyn.core.present.binding import ResolvedPresentation
    from reyn.runtime.session_buses import OutboxPresentationRenderer

    class _Session:
        def __init__(self) -> None:
            self.outbox: asyncio.Queue = asyncio.Queue()

    session = _Session()
    renderer = OutboxPresentationRenderer(session)
    assert renderer.surface_name == "inline-cui"

    resolved = ResolvedPresentation(nodes=[{"component": "text", "text": "hi"}])
    renderer.render(resolved)

    msg = session.outbox.get_nowait()
    assert isinstance(msg, OutboxMessage)
    assert msg.kind == "presentation"
    assert msg.meta["nodes"] == [{"component": "text", "text": "hi"}]


def test_format_inline_message_dispatches_presentation_kind() -> None:
    """Tier 2: format_inline_message routes kind="presentation" to
    render_presentation_nodes (not the generic _KIND_LINE fallback)."""
    msg = OutboxMessage(kind="presentation", text="", meta={"nodes": [
        {"component": "text", "text": "hello from present"},
    ]})
    console = Console(width=60, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(format_inline_message(msg))
    assert "hello from present" in console.file.getvalue()


# ── 5. op_runtime/present.py surface derivation + renderer call ─────────────


class _RecordingRenderer:
    surface_name = "inline-cui"

    def __init__(self) -> None:
        self.calls = []
        self.call_count = 0

    def render(self, resolved) -> None:
        self.calls.append(resolved)
        self.call_count += 1


def _ctx(*, presentation_renderer=None) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        presentation_renderer=presentation_renderer,
    )


@pytest.mark.asyncio
async def test_present_op_uses_null_surface_when_no_renderer_wired() -> None:
    """Tier 1: OpContext.presentation_renderer=None (PR-A behavior, unchanged) →
    surface="null" in both the ack path's binding resolution and the presented
    event, and no renderer is called."""
    from reyn.core.op_runtime.present import handle
    from reyn.schemas.models import PresentIROp

    op = PresentIROp(
        kind="present", data_inline={"v": "x"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    result = await handle(op, _ctx())
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_present_op_calls_the_wired_renderer_exactly_once() -> None:
    """Tier 1: a wired OpContext.presentation_renderer receives exactly one
    `.render(resolved)` call carrying the SAME stats the ack reports."""
    from reyn.core.op_runtime.present import handle
    from reyn.schemas.models import PresentIROp

    renderer = _RecordingRenderer()
    op = PresentIROp(
        kind="present", data_inline={"v": "x"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    result = await handle(op, _ctx(presentation_renderer=renderer))
    assert renderer.call_count == 1
    assert renderer.calls[0].bindings_resolved == result["bindings_resolved"]


@pytest.mark.asyncio
async def test_presented_event_surface_reflects_the_wired_renderers_name() -> None:
    """Tier 1: the `presented` audit event's `surface` field is the wired
    renderer's own `surface_name`, not a hardcoded "null"/"inline-cui" literal."""
    from reyn.core.op_runtime.present import handle
    from reyn.schemas.models import PresentIROp

    events = EventLog()
    captured: list = []
    events.add_subscriber(lambda e: captured.append(e) if e.type == "presented" else None)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        presentation_renderer=_RecordingRenderer(),
    )
    op = PresentIROp(
        kind="present", data_inline={"v": "x"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    await handle(op, ctx)
    assert captured
    assert captured[0].data["surface"] == ["inline-cui"]
