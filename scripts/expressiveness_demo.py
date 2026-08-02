"""#3642 sample CLI — three places a distinction that EXISTS IN THE DATA is not on screen.

Run it::

    python scripts/expressiveness_demo.py           # both variants, side by side
    python scripts/expressiveness_demo.py --icons nerd

Every item below carries the three things the issue asks for, and the survey
that produced them looked at all nine widgets in ``interfaces/inline/
textual_chat/`` (``_CursorFlowView`` / ``Composer`` / ``MenuBar`` /
``StatusLine`` / ``CompletionPopup`` / ``InterventionPanel`` / ``RewindPicker``
/ ``SearchBar`` / ``SentQueue``), not the two that were suggested.

**What was REJECTED, and why** — recorded because "no candidate here" is a
result, and inventing one is the failure mode this survey was warned about:

- ``visibility`` rows already distinguish three states with reasons
  (``[on]`` / ``[off]`` / ``[--] · envelope``, #3378/#3380).
- ``SentQueue`` has one state — ``show_item`` / ``remove_item``, nothing else.
- ``RewindPicker`` already carries the boundary ``kind`` as its own column.
- ``StatusLine`` already marks the attached session (``▸``).
- Entry state (RUNNING / SUCCESS / ERROR) is already colour + a blinking glyph.
- ``InterventionPanel``: no recommended/destructive flag exists on a choice, so
  there is nothing collapsed — only something absent, which is a different
  issue and not this one.
- ``CompletionPopup``: one namespace per menu, so the kind is implied by the
  character the operator just typed. Too weak to claim.

**Restraint.** The proposals add **4** glyph distinctions and remove **1**
(``·`` stops meaning three things), so the net is +3 across the whole
interface, all of them in the two places an operator watches most. No row gains
an icon merely for having one, and the ``ascii`` default is the same design
with the symbols withdrawn — not a lesser one.
"""
from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text

# ── the two variants ────────────────────────────────────────────────────────
#
# ``ascii`` is the default and needs nothing from the terminal. ``nerd`` is
# opt-in and assumes a Nerd Font (WezTerm ships ``Symbols Nerd Font Mono``
# built-in). The SHAPE of the design is identical: same distinctions, same
# positions, different glyphs — so a terminal that cannot render the second one
# loses decoration, never information.
GLYPHS = {
    "ascii": {
        "reasoning": "·",
        "status": "⋯",
        "system": "▪",
        "writes": "*",
        "reads": " ",
        "actionable": "*",
        "group_sep": "│",
    },
    "nerd": {
        "reasoning": "",   # brain
        "status": "",      # spinner ring
        "system": "",      # gear
        "writes": "",      # pencil
        "reads": "",       # eye
        "actionable": "",  # toggle
        "group_sep": "│",
    },
}

DIM = "dim"


# ── ① flow rows: three kinds, one appearance ────────────────────────────────
#
# DATA: ``_KIND_LINE`` (interfaces/repl/renderer.py:458) maps eight message
# kinds to (glyph, body style, gutter style). Three of them are byte-identical:
#     "reasoning": ("· ", dim, dim)   the model thinking
#     "status":    ("· ", dim, dim)   ambient progress, transient
#     "system":    ("· ", dim, dim)   lifecycle: compaction / budget / cost-warn
# All three are live: 4 / 10 / 8 production sites respectively.
#
# COLLAPSE: an operator cannot tell "the model is thinking" from "your context
# was just compacted" — one is noise, the other changed what the model can see.
_ROWS = [
    ("reasoning", "weighing whether the ref is still valid…"),
    ("status", "rag_ingest.ingest 7/15"),
    ("system", "context compacted — 42k tokens reclaimed"),
]


def flow_rows(g: dict) -> Table:
    out = Table.grid(padding=(0, 1))
    out.add_column()
    out.add_column()
    for kind, text in _ROWS:
        out.add_row(Text(g[kind], style=DIM), Text(text, style=DIM))
    return out


# ── ② tool rows: 41 tools change the world, 34 only look ────────────────────
#
# DATA: every ToolDefinition carries ``purity`` (tools/types.py:404). Measured
# on the live registry: 75 tools, 41 ``side_effect``, 34 ``read_only``.
#
# COLLAPSE: ``purity`` appears nowhere in ``presenter.py`` or ``renderer.py``.
# Reading a file and deleting one render identically as ``tool(args)``.
_TOOLS = [
    ("reyn_repo_read", "path=README.md", "read_only"),
    ("file_write", "path=notes.md", "side_effect"),
    ("list_agents", "", "read_only"),
    ("shell", "cmd=rm -rf build/", "side_effect"),
]


def tool_rows(g: dict) -> Table:
    out = Table.grid(padding=(0, 1))
    out.add_column(width=1)
    out.add_column()
    for name, args, purity in _TOOLS:
        mark = g["writes"] if purity == "side_effect" else g["reads"]
        body = Text.assemble((name, "bold"), (f"({args})", DIM))
        out.add_row(Text(mark, style=DIM), body)
    return out


# ── ③ the menu bar: 13 tabs, one appearance ─────────────────────────────────
#
# DATA: ``chrome.py:379-383`` states it outright — "Tool/MCP/Skill/Hook are
# ACTIONABLE (each row dispatches the ``/visibility`` or ``/hook`` that flips
# it); Pipe/Cron are read-only." The module docstring splits the 13 into five
# groups by nature (state / numbers / toggles / lists / reference).
#
# COLLAPSE: ``_MENU_TABS`` is a flat list of 13 identically-styled labels.
# Whether a tab leads somewhere that CHANGES something is invisible.
_GROUPS = [
    ("state", ["Model", "Agent"], False),
    ("numbers", ["Cost", "Ctx"], False),
    ("toggles", ["Tool", "MCP", "Skill", "Hook"], True),
    ("lists", ["Pipe", "Cron", "Hist", "Menu"], False),
    ("reference", ["Help"], False),
]


def menu_bar(g: dict) -> Text:
    out = Text()
    for i, (_name, labels, actionable) in enumerate(_GROUPS):
        if i:
            out.append(f" {g['group_sep']} ", style=DIM)
        for j, label in enumerate(labels):
            if j:
                out.append(" ")
            out.append(label)
            if actionable:
                out.append(g["actionable"], style=DIM)
    return out


def render(console: "Console", variant: str) -> None:
    g = GLYPHS[variant]
    console.rule(f"[bold]{variant}")
    console.print("\n[bold]① flow rows[/] — reasoning / status / system")
    console.print("   now: all three are '· ' + dim")
    console.print(flow_rows(g))
    console.print("\n[bold]② tool rows[/] — does this tool change anything?")
    console.print("   now: purity never reaches the display")
    console.print(tool_rows(g))
    console.print("\n[bold]③ menu bar[/] — which tabs DO something?")
    console.print("   now: 13 identical labels")
    console.print("  ", menu_bar(g))
    console.print()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--icons",
        choices=("ascii", "nerd", "both"),
        default="both",
        help="which variant to draw (default: both, side by side)",
    )
    args = parser.parse_args(argv)
    console = Console()
    variants = ("ascii", "nerd") if args.icons == "both" else (args.icons,)
    for variant in variants:
        render(console, variant)
    if "nerd" in variants:
        console.print(
            "[dim]nerd glyphs need a Nerd Font. WezTerm ships one built in; "
            "Alacritty takes them from the configured font and clips wide "
            "ones — which is why ascii is the default, not a fallback.[/]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
