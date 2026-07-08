"""present renderer — `ResolvedPresentation.nodes` → Rich renderables (FP-0054 PR-B).

**Invariant (do not violate — FP-0054 §5 Option B, PR-B review).** Every leaf string
here reaches a MARKUP-INERT Rich object — `Text` / `Syntax` / `Markdown` — and this
module never calls `console.print(str)` (or hands a bare `str` to a Rich API that
itself defaults to markup interpretation, e.g. `Table.add_column`/`add_row`). Rich
console-markup injection becomes structurally impossible: `guard.py`'s terminal
strategy strips ESC/control sequences only (the surface-universal threat) and
deliberately does NOT escape `[tag]`-shaped text — see its module docstring for why
that used to be here and was wrong (Rich markup is reachable ONLY through
`console.print(str, markup=True)`, a renderer choice, not a sink property).
`rich.markdown.Markdown` is the one exception: it interprets CommonMark, not Rich
console markup, so a `markdown` component's raw (control-stripped) text goes to it
directly — no wrapping needed, no injection vector either way.

Pure: takes the already bound/neutralized/capped render model and a target width;
produces a Rich renderable. No I/O — the caller (`interfaces/repl/renderer.py`'s
`InlineChatRenderer`) owns the `Console` + `run_in_terminal` print.
"""
from __future__ import annotations

from typing import Any


def _cell(value: Any) -> "Any":
    """Wrap a leaf value as a markup-inert `Text` — the ONE conversion every string
    destined for a Rich renderable goes through in this module."""
    from rich.text import Text

    return Text(str(value))


def _render_keyvalue(node: dict) -> "Any":
    from rich.table import Table

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold")
    grid.add_column()
    for row in node.get("rows", []):
        grid.add_row(_cell(row.get("label", "")), _cell(row.get("value", "")))
    return grid


def _render_table(node: dict) -> "Any":
    from rich.table import Table

    columns = node.get("columns", [])
    table = Table(show_lines=False)
    for col in columns:
        table.add_column(_cell(col.get("header", "")))
    n_rows = max((len(col.get("cells", [])) for col in columns), default=0)
    for i in range(n_rows):
        table.add_row(*[
            _cell(col["cells"][i]) if i < len(col.get("cells", [])) else _cell("")
            for col in columns
        ])
    return table


def _render_list(node: dict) -> "Any":
    from rich.console import Group

    return Group(*[_cell(f"• {item}") for item in node.get("items", [])])


def _render_code_or_diff(node: dict, *, lexer: str) -> "Any":
    from rich.syntax import Syntax

    # §5 "cap before render": the text is already head-N-capped by guard.cap_leaf
    # (binding.py) before it ever reaches this render model — Syntax highlights only
    # the survivors, never the full pre-cap source.
    return Syntax(node.get("text", ""), lexer, word_wrap=True, background_color="default")


def _render_node(node: dict) -> "Any":
    from rich.markdown import Markdown
    from rich.text import Text

    component = node.get("component")
    if component == "text":
        return Text(node.get("text", ""))
    if component == "markdown":
        # CommonMark, not Rich console markup — no injection vector, no wrapping needed.
        return Markdown(node.get("text", ""))
    if component == "code":
        return _render_code_or_diff(node, lexer=node.get("language") or "text")
    if component == "diff":
        return _render_code_or_diff(node, lexer="diff")
    if component == "keyvalue":
        return _render_keyvalue(node)
    if component == "table":
        return _render_table(node)
    if component == "list":
        return _render_list(node)
    if component == "image":
        alt = node.get("alt") or node.get("src") or ""
        return Text(f"[image: {alt}]", style="dim")
    # Unregistered/future component — never crash the render loop over one bad node.
    return Text(f"<unsupported present component {component!r}>", style="dim")


def render_presentation_nodes(nodes: list[dict]) -> "Any":
    """Convert a `ResolvedPresentation.nodes` render model into ONE Rich renderable
    (a `Group` of per-node renderables) — the one-shot inline block `present` prints
    to the conversation scrollback. See module docstring for the markup-inert
    invariant every branch here must preserve."""
    from rich.console import Group

    return Group(*[_render_node(node) for node in nodes])
