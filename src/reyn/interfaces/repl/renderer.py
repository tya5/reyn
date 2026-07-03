"""Pluggable chat UI backends for reyn chat."""
from __future__ import annotations

import re
import sys
import time
from io import StringIO

from prompt_toolkit.formatted_text import HTML, AnyFormattedText

from reyn.llm.pricing import TokenUsage
from reyn.runtime.outbox import OutboxMessage


def _meta_prefix(meta: dict) -> str:
    """Build a `[skill_name#abcd] ` prefix from meta provenance, if present.

    Returns "" when neither skill_name nor run_id_short is set, so generic
    status / error messages stay clean.
    """
    skill = meta.get("skill_name")
    short = meta.get("run_id_short")
    if skill and short:
        return f"[{skill}#{short}] "
    if skill:
        return f"[{skill}] "
    if short:
        return f"[#{short}] "
    return ""


_BANNER = """\
 ██████╗ ███████╗██╗   ██╗███╗  ██╗
 ██╔══██╗██╔════╝╚██╗ ██╔╝████╗ ██║
 ██████╔╝█████╗   ╚████╔╝ ██╔██╗██║
 ██╔══██╗██╔══╝    ╚██╔╝  ██║╚████║
 ██║  ██║███████╗   ██║   ██║ ╚███║
 ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚══╝"""

_HELP = "/quit or Ctrl-D to exit"

# kinds that should overwrite the previous transient line in place rather than
# append a new line. The renderer tracks "did the last write leave a transient
# line on screen" and emits cursor-up + clear-line before the next write.
_TRANSIENT_KINDS = frozenset({"status", "trace"})

# ANSI: move cursor up one line + clear that line from cursor to end.
_CLEAR_PREV_LINE = "\033[1A\033[K"


class ChatRenderer:
    """Pluggable chat UI backend.

    Concrete renderers override any of these methods. Defaults are no-op so a
    partial override (e.g. a future TUI backend that owns its own banner)
    doesn't have to implement every method.
    """

    def banner(self, agent_name: str) -> None:
        """Render the startup banner. Called once before the input loop."""

    def message(self, msg: OutboxMessage) -> None:
        """Render one outbox item.

        msg.kind ∈ {"agent","status","error","intervention","trace","skill_done"}
        msg.meta carries provenance (skill_name, run_id, run_id_short, ...)
        """

    def prompt_text(self) -> AnyFormattedText:
        """Return the prompt passed verbatim to PromptSession.prompt_async.

        Never wrap the return value — the renderer returns the final form.
        """
        return "you > "

    def cost_summary(self, usage: TokenUsage, cost_usd: float | None) -> None:
        """Render token totals + estimated cost on shutdown."""

    def on_chat_event(self, event) -> None:
        """Hook for live session events (default no-op).

        `run_repl` subscribes this to the attached session via
        `Session.subscribe_chat_events`. Override to drive a working indicator.
        Called synchronously on the session loop with an `Event` (`.type`/`.data`).
        """

    def bottom_toolbar(self):
        """Optional prompt_toolkit bottom-toolbar content (default None = none).

        Re-evaluated on every prompt refresh; return a live spinner here to
        animate a working indicator while a turn runs.
        """
        return None

    def uses_app_input(self) -> bool:
        """Whether this renderer drives input via its own prompt_toolkit
        Application (rule-bar input) instead of the default PromptSession
        `_input_loop`. Default False → the plain PromptSession path is used.
        """
        return False


class ConsoleChatRenderer(ChatRenderer):
    _PREFIX = {
        "agent": "agent>",
        "status": "[…]",
        "error": "[error]",
        "intervention": "[ask]",
        "trace": "[trace]",
        "skill_done": "[done]",
    }

    def __init__(self) -> None:
        # Tracks whether the last write left a single-line transient on screen
        # that the next write should overwrite.
        self._transient_active = False
        self._thinking = False  # driven by on_chat_event

    def on_chat_event(self, event) -> None:
        etype = event.type
        if etype == "turn_started":
            self._thinking = True
        elif etype in ("turn_settled", "turn_completed", "turn_cancelled"):
            self._thinking = False

    def bottom_toolbar(self):
        if not self._thinking:
            return None
        frame = _SPINNER[int(time.monotonic() * 8) % len(_SPINNER)]
        return f" {frame} working…"

    def _write(self, s: str) -> None:
        # Bypass patch_stdout's proxy: it renders ANSI bytes (including cursor
        # control codes) as literal text. Safe to write directly because each
        # message() call is wrapped in run_in_terminal at the call site.
        sys.__stdout__.write(s)
        sys.__stdout__.flush()

    def _clear_transient(self) -> None:
        if self._transient_active:
            self._write(_CLEAR_PREV_LINE)
            self._transient_active = False

    def banner(self, agent_name: str) -> None:
        self._write(f"{_BANNER}\n  agent={agent_name}\n  {_HELP}\n\n")

    def message(self, msg: OutboxMessage) -> None:
        self._clear_transient()
        kind_prefix = self._PREFIX.get(msg.kind, "")
        meta_prefix = _meta_prefix(msg.meta)
        # Inject meta prefix between kind tag and text so logs read
        # "[trace] [skill_builder#abcd] phase started: ..."
        if kind_prefix:
            line = f"{kind_prefix} {meta_prefix}{msg.text}\n"
        else:
            line = f"{meta_prefix}{msg.text}\n"
        self._write(line)
        self._transient_active = msg.kind in _TRANSIENT_KINDS

    def prompt_text(self) -> AnyFormattedText:
        return "you > "

    def cost_summary(self, usage: TokenUsage, cost_usd: float | None) -> None:
        self._clear_transient()
        cost_str = f"${cost_usd:.4f}" if cost_usd is not None else "--"
        self._write(
            f"cost {cost_str}  "
            f"prompt={usage.prompt_tokens} "
            f"completion={usage.completion_tokens} "
            f"total={usage.total_tokens}\n"
        )


class RichChatRenderer(ChatRenderer):
    """Render via Rich, bypassing patch_stdout's proxy so ANSI escape codes
    reach the terminal raw (the proxy renders ANSI bytes as literal text).

    Strategy:
      - Rich writes to a StringIO buffer (preserving ANSI codes).
      - _flush() writes the buffer to sys.__stdout__ — the original, unpatched
        stdout — so the terminal sees real ANSI.
      - The call site in _output_loop wraps each message() in run_in_terminal,
        which pauses the prompt's render loop. The prompt won't redraw between
        our raw write and the next loop iteration, so the prompt stays clean.
    """

    def __init__(self) -> None:
        from rich.console import Console
        self._buffer = StringIO()
        self._console = Console(
            highlight=False, file=self._buffer, force_terminal=True,
        )
        self._transient_active = False

    def _flush(self) -> None:
        s = self._buffer.getvalue()
        self._buffer.seek(0)
        self._buffer.truncate()
        if not s:
            return
        sys.__stdout__.write(s)
        sys.__stdout__.flush()

    def _clear_transient(self) -> None:
        if self._transient_active:
            sys.__stdout__.write(_CLEAR_PREV_LINE)
            sys.__stdout__.flush()
            self._transient_active = False

    def banner(self, agent_name: str) -> None:
        self._console.print(_BANNER, style="bold cyan")
        self._console.print(f"  [dim]agent={agent_name}[/dim]")
        self._console.print(f"  [dim]{_HELP}[/dim]\n")
        self._flush()

    def message(self, msg: OutboxMessage) -> None:
        # Always pass user text with markup=False so brackets in event payloads
        # don't get interpreted as Rich style tags (which would silently drop
        # the bracketed token).
        self._clear_transient()
        c = self._console
        kind = msg.kind
        text = f"{_meta_prefix(msg.meta)}{msg.text}"
        if kind == "agent":
            from rich.text import Text
            rendered = Text.assemble(("agent  ", "bold cyan"), (text, ""))
            c.print(rendered)
        elif kind == "status":
            c.print(f"⟳ {text}", style="dim", markup=False)
        elif kind == "error":
            c.print(f"✗ {text}", style="bold red", markup=False)
        elif kind == "intervention":
            from rich.panel import Panel
            from rich.text import Text
            c.print(Panel(Text(text), border_style="yellow"))
        elif kind == "trace":
            c.print(f"  · {text}", style="dim", markup=False)
        elif kind == "skill_done":
            from rich.panel import Panel
            from rich.text import Text
            c.print(Panel(Text(text), border_style="green"))
        else:
            c.print(text, markup=False)
        self._flush()
        self._transient_active = kind in _TRANSIENT_KINDS

    def prompt_text(self) -> AnyFormattedText:
        return HTML("<ansicyan>you</ansicyan> <b>›</b> ")

    def cost_summary(self, usage: TokenUsage, cost_usd: float | None) -> None:
        self._clear_transient()
        from rich.rule import Rule
        p, c, t = usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
        cost_str = f"${cost_usd:.4f}" if cost_usd is not None else "--"
        self._console.print(Rule(f"cost {cost_str}", style="dim"))
        self._console.print(f"[dim]  prompt {p}  completion {c}  total {t}[/dim]")
        self._flush()


# Claude Code-style palette. Default is plain text (_CC_TEXT); colour is reserved
# to signal STATE — error (red), needs-action (amber), done (green), ambient/low
# (dim) — so a coloured glyph always means "something to notice".
_CC_TEXT = "default"    # terminal default fg — normal text + markers (no forced colour)
_CC_DIM = "#6b7280"     # low-importance / ambient
_CC_DONE = "#7ee787"    # green — completion
_CC_ERR = "#f97066"     # red — failure
_CC_WARN = "#e3b341"    # amber — an intervention that needs the user to act
_CC_ACCENT = "#d97757"  # terracotta — spinner / accents
_CC_COOL = "#6cb6ff"    # cool blue — a secondary accent (status-bar agent value)
# Subtle background block behind the user's own submitted line (CC styles the
# user input differently from agent output — a faint highlighted block).
_CC_USER_BG = "#2b2f37"

# Braille spinner frames for the working indicator (bottom toolbar).
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Per-kind line layout: (gutter, gutter_style, body_style). A CC-style 2-cell
# marker gutter (glyph + space) sits in its own column so a wrapped / multi-line
# body hang-indents into the body column and never bleeds into the gutter. The
# agent (LLM) body is rendered as markdown (body_style then unused). Tool-result /
# trace ⎿ rows nest one level under the parent body column (2-space indent + ⎿).
#
# Glyphs are distinct per kind so the eye separates them; colour is reserved for
# STATE — default terminal fg (_CC_TEXT), then amber=needs-you, red=error,
# green=done, dim=ambient/low — so a coloured glyph always signals something to
# notice. Distinct glyphs: ⏺ assistant · ❯ you · ▸ tool · ◆ needs-you · ✗ error ·
# ✓ done · · status · ⎿ detail.
_KIND_LINE = {
    "user":         ("❯ ",   _CC_TEXT,   _CC_DIM),   # your input  (default fg, + bg block)
    "agent":        ("⏺ ",   _CC_TEXT,   _CC_TEXT),  # normal reply — terminal default fg
    "reasoning":    ("· ",   _CC_DIM,    _CC_DIM),   # model thinking (dim; only shown when chat.reasoning.display=true)
    "intervention": ("◆ ",   _CC_WARN,   "bold"),    # needs you   — amber
    "error":        ("✗ ",   _CC_ERR,    _CC_ERR),   # error       — red
    "skill_done":   ("✓ ",   _CC_DONE,   _CC_DIM),   # done        — green glyph, dim body
    "status":       ("· ",   _CC_DIM,    _CC_DIM),   # ambient     — dim
    "system":       ("· ",   _CC_DIM,    _CC_DIM),   # lifecycle marker (compaction / budget / cost-warn)
    "trace":        ("  ⎿ ", _CC_DIM,    _CC_DIM),   # detail      [low]  nested
}

# ⎿ detail rows nest under the line above them, so no blank-line separator goes
# before these — a tool call and its result stay grouped as one block.
_NESTED_KINDS = frozenset({"tool_call_completed", "tool_call_failed", "trace"})


def wants_separator(kind: str, seen_message: bool) -> bool:
    """Pure: whether a blank line should precede this message in the scrollback.

    One blank line separates top-level message blocks for breathing room, but not:
    - before the very first message;
    - before a nested ⎿ detail row (it belongs to the block above it);
    - before a TRANSIENT status/trace line. A transient is cleared in place by the
      next message, so a separator before it would be orphaned as a stray blank.
      This is what made an agent reply show two blanks: the per-turn "thinking…"
      status got a separator, was cleared, and left its blank behind — then the
      reply added its own. Skipping transients leaves exactly one.
    """
    return (
        seen_message
        and kind not in _NESTED_KINDS
        and kind not in _TRANSIENT_KINDS
    )


def _short(v, n: int = 60) -> str:
    """Collapse whitespace and truncate any value to a one-line summary."""
    if v is None:
        return ""
    s = v if isinstance(v, str) else repr(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _summarize_args(args) -> str:
    """Compact ``k=v`` summary of a tool's args dict (or a bare value)."""
    if not args:
        return ""
    if isinstance(args, dict):
        return _short(", ".join(f"{k}={_short(v, 24)}" for k, v in args.items()))
    return _short(args)


def summarize_tool_result(tool, result) -> str:
    """Human one-line summary of a tool result (CC-style, e.g. ``Read 42 lines``).

    Best-effort per tool name / result shape; ALWAYS degrades gracefully — any
    unrecognised shape (or an error reading it) falls back to a truncated repr,
    so it never raises and never loses the result entirely.
    """
    try:
        return _summarize_result(tool, result)
    except Exception:
        return _short(result, 80)


def _summarize_result(tool, result) -> str:
    t = (tool or "").lower()
    if result is None or result == "":
        return "done"
    if isinstance(result, list):
        n = len(result)
        word = "result" if "search" in t else "item"
        return f"{n} {word}{'' if n == 1 else 's'}"
    if isinstance(result, dict):
        # Error always wins — a dict with "error" is a failure regardless of
        # any other keys (e.g. file__read returns op="read", content="", error="file
        # not found: ..." for a missing file; without this guard the read branch
        # below would short-circuit to "Read 0 lines" and the error is never seen).
        error = result.get("error")
        if isinstance(error, str):
            return _short(error, 80)
        error_message = result.get("error_message")
        if isinstance(error_message, str):
            return _short(error_message, 80)
        op = result.get("op")
        path = result.get("path")
        status = result.get("status")
        if op == "read" or ("read" in t and "content" in result):
            content = result.get("content")
            if isinstance(content, str):
                lines = content.count("\n") + (1 if content else 0)
                more = " (truncated)" if status == "truncated" else ""
                return f"Read {lines} line{'s' if lines != 1 else ''}{more}"
            # A read whose content wasn't a usable string (e.g. None on an error
            # result): prefer the status (handled below) if any, else a clean
            # note — never fall through to dumping the raw dict repr.
            if not status:
                return "Read (no content)"
        if op in ("write", "create"):
            return f"Wrote {path}" if path else "Wrote file"
        if op == "edit":
            return f"Edited {path}" if path else "Edited file"
        if op == "delete":
            return f"Deleted {path}" if path else "Deleted file"
        if op == "grep":
            count = result.get("count")
            n = int(count) if isinstance(count, (int, float)) else 0
            return f"{n} match{'es' if n != 1 else ''}"
        if op == "mkdir":
            return f"Created {path}" if path else "Created directory"
        if op == "move":
            dest = result.get("dest_path")
            return f"Moved to {dest}" if dest else "Moved"
        saved = result.get("saved")
        if isinstance(saved, str):
            return f"Saved {saved}"
        forgotten = result.get("deleted")
        if isinstance(forgotten, str):
            return f"Forgot {forgotten}"
        tasks = result.get("tasks")
        if isinstance(tasks, list):
            n = len(tasks)
            return f"{n} task{'s' if n != 1 else ''}"
        entries = result.get("entries")
        if isinstance(entries, list):
            n = len(entries)
            return f"Listed {n} {'entry' if n == 1 else 'entries'}"
        matches = result.get("matches")
        if isinstance(matches, list):
            n = len(matches)
            return f"{n} match{'es' if n != 1 else ''}"
        chunks = result.get("chunks")
        if isinstance(chunks, list):
            n = len(chunks)
            return f"{n} chunk{'s' if n != 1 else ''}"
        servers = result.get("servers")
        if isinstance(servers, list):
            n = len(servers)
            return f"{n} server{'s' if n != 1 else ''}"
        mcp_tools = result.get("mcp_tools")
        if isinstance(mcp_tools, list):
            n = len(mcp_tools)
            return f"{n} tool{'s' if n != 1 else ''}"
        items = result.get("items")
        if isinstance(items, list):
            n = len(items)
            return f"{n} item{'s' if n != 1 else ''}"
        results = result.get("results")
        if isinstance(results, list):
            n = len(results)
            return f"{n} result{'s' if n != 1 else ''}"
        jobs = result.get("jobs")
        if isinstance(jobs, list):
            n = len(jobs)
            return f"{n} job{'s' if n != 1 else ''}"
        chunks_dropped = result.get("chunks_dropped")
        if isinstance(chunks_dropped, int):
            n = chunks_dropped
            return f"Dropped {n} chunk{'s' if n != 1 else ''}"
        if isinstance(result.get("input_schema"), dict):
            name_or_desc = result.get("name") or result.get("description") or ""
            return _short(str(name_or_desc), 60)
        if result.get("kind") == "mcp":
            mcp_content = result.get("content")
            if isinstance(mcp_content, str) and mcp_content:
                return _short(mcp_content.split("\n")[0], 60)
        passed = result.get("passed")
        if isinstance(passed, bool):
            score = result.get("score")
            pct = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
            return ("Passed" if passed else "Failed") + pct
        returncode = result.get("returncode")
        if isinstance(returncode, int) and status == "ok":
            return f"exit {returncode}"
        if status:
            return str(status)
    return _short(result, 80)


def _gutter_grid(gutter: str, gutter_style: str, body, *, row_style: str = "",
                 expand: bool = False):
    """A 2-column grid: a reserved marker gutter + a wrapping body column.

    Continuation lines of a wrapped / multi-line body stay in the body column, so
    the CC-style gutter (glyph + space) is never bled into — unlike a single Text
    that wraps back to column 0. ``row_style`` paints a background behind the whole
    line; ``expand`` stretches the body column to the full width so that background
    fills the line edge-to-edge (the user-input block).

    The body column sets ``overflow="fold"`` so a long unbreakable token (a path,
    identifier, hash, URL) folds onto the next line instead of being truncated at
    the right edge with an ellipsis — rich Table columns default to
    ``overflow="ellipsis"``, which would crop such tokens.
    """
    from rich.table import Table
    from rich.text import Text
    g = Text(gutter, style=gutter_style)
    grid = Table.grid(padding=0, expand=expand)
    grid.add_column(width=g.cell_len, no_wrap=True)
    grid.add_column(ratio=1 if expand else None, overflow="fold")
    grid.add_row(g, body, style=row_style or None)
    return grid


# Matches non-fence structural markdown elements outside fenced code blocks:
# headings, blockquotes, list items (unordered/ordered), code-fence delimiters
# (``` or ~~~, with optional language tag), and blank lines. Used by
# _harden_soft_breaks to skip hardening on lines where the markdown parser
# depends on the raw newline for element recognition. Fenced code block
# CONTENT is handled separately (in_fence state) so trailing spaces are never
# added inside a code block.
_STRUCTURAL_LINE_RE = re.compile(
    r"^(#|>|```|~~~|\s*[-*+] |\s*\d+\. |$)"
)


def _harden_soft_breaks(text: str) -> str:
    """Append two trailing spaces to bare paragraph lines before a single newline.

    CommonMark (and rich.Markdown) collapses a single newline inside a paragraph
    to a space, so ``line1\\nline2`` renders as ``line1 line2``. LLM output often
    uses single newlines for visual separation; this preserves them as hard line
    breaks (CommonMark ``  \\n`` = ``<br>``).

    Lines inside fenced code blocks (``` or ~~~ delimiters) are always preserved
    verbatim — trailing spaces would corrupt code content (invisible on screen but
    present in copy-paste and significant for whitespace-sensitive tools). Other
    structural lines (headings, list items, blockquotes, blank lines) are also
    exempt; the parser uses the raw newlines around them to recognise the element.
    """
    if not text:
        return text
    lines = text.split("\n")
    out = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Fence delimiters (``` or ~~~ with optional language tag) toggle the
        # in-fence state. Always append verbatim — the delimiter itself is
        # structural and must not gain trailing spaces.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        # Inside a fenced code block: preserve content bytes exactly.
        if in_fence:
            out.append(line)
            continue
        # Outside a fence: harden non-structural lines not adjacent to a
        # structural one (heading / list / blockquote / blank / fence delimiter).
        is_structural = bool(_STRUCTURAL_LINE_RE.match(line))
        next_is_structural = i + 1 >= len(lines) or bool(
            _STRUCTURAL_LINE_RE.match(lines[i + 1])
        )
        if is_structural or next_is_structural:
            out.append(line)
        else:
            out.append(line + "  ")
    return "\n".join(out)


def _body_renderable(kind: str, text: str, body_style: str):
    """The body cell: markdown for agent (LLM) output, a styled Text otherwise."""
    from rich.markdown import Heading, Markdown
    from rich.text import Text

    if kind == "agent":
        # rich.Markdown centers H1 by default (LEVEL_ALIGN = {"h1": "center"}).
        # In a gutter-grid chat context that produces heavy leading whitespace —
        # "⏺                          My Heading". Override to left for all levels.
        class _LeftHeading(Heading):
            LEVEL_ALIGN = {tag: "left" for tag in Heading.LEVEL_ALIGN}

        class _ChatMarkdown(Markdown):
            elements = {**Markdown.elements, "heading_open": _LeftHeading}

        # Render the LLM reply as markdown (headings / bold / lists / code) like
        # Claude Code. Single newlines are hardened to CommonMark hard line breaks
        # so the model's per-line output is preserved rather than collapsed to one
        # paragraph — matching how CC displays LLM output.
        return _ChatMarkdown(_harden_soft_breaks(text or ""))
    return Text(text, style=body_style)


def format_inline_message(msg: OutboxMessage):
    """Pure formatter: OutboxMessage → a rich renderable (the inline CC-style line).

    A 2-cell marker gutter (glyph + space) sits in its own column; the body wraps
    in a second column so multi-line / wrapped output hang-indents under the body
    and never bleeds into the gutter. The agent (LLM) body renders as markdown; the
    user's own line gets a faint background block. Kept separate from rendering so
    the mapping stays testable.
    """
    from rich.text import Text
    kind = msg.kind
    meta = msg.meta or {}

    # Tool-call rows. ▸ marks an invocation (distinct from the ⏺ assistant reply);
    # the ⎿ result / failure rows nest one level under it (2-space indent).
    if kind == "tool_call_started":
        tool = str(meta.get("tool", msg.text))
        args = _summarize_args(meta.get("args"))
        body = Text.assemble((tool, "bold"), (f"({args})", _CC_DIM))
        return _gutter_grid("▸ ", _CC_TEXT, body)
    if kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        return _gutter_grid("  ⎿ ", _CC_DIM, Text(summary, style=_CC_DIM))
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text
        return _gutter_grid("  ⎿ ", _CC_ERR, Text(f"✗ {_short(err, 80)}", style=_CC_ERR))

    line = _KIND_LINE.get(kind)
    if line is None:
        return Text(f"{_meta_prefix(meta)}{msg.text}")
    gutter, gutter_style, body_style = line
    # A provenance prefix ([skill#id]) is kept inline (rare for agent replies); it
    # renders as literal text inside the agent markdown body.
    # Intervention is user-facing: suppress the cryptic run_id_short hash — the
    # user doesn't need disambiguation for a prompt that has one active caller.
    # skill_name context (e.g. "[skill_builder] ") is still shown if present.
    if kind == "intervention":
        skill = meta.get("skill_name")
        _pfx = f"[{skill}] " if skill else ""
    else:
        _pfx = _meta_prefix(meta)
    body_text = f"{_pfx}{msg.text}"
    if kind == "user":
        # The user's own submitted line: echoed into scrollback (the inline input
        # clears on submit) AND given a faint background block so it reads as a
        # distinct "you said this" line, like Claude Code.
        bg = f"on {_CC_USER_BG}"
        return _gutter_grid(
            gutter, f"{gutter_style} {bg}",
            Text(body_text, style=f"{body_style} {bg}"), row_style=bg, expand=True,
        )
    return _gutter_grid(gutter, gutter_style, _body_renderable(kind, body_text, body_style))


class InlineChatRenderer(ChatRenderer):
    """Claude Code-style inline renderer — the default interactive `reyn chat`
    backend (TTY, no `--cui`).

    Renders each OutboxMessage to stdout above the prompt_toolkit prompt via the
    same StringIO+`run_in_terminal` pattern as RichChatRenderer (the call site
    in `_output_loop` wraps each `message()` in `run_in_terminal`, so raw ANSI
    reaches the terminal without corrupting the prompt). Conversation history
    stays in the terminal's own scrollback; only the prompt is live below.

    PR1 (cutover) scope: `⏺`/`⎿` symbols + terracotta accent + per-kind
    formatting. The rule-sandwiched input bar, navigable status bar, and
    in-conversation animations land in follow-up PRs (a custom prompt_toolkit
    Application that replaces the PromptSession input).
    """

    def __init__(self) -> None:
        from rich.console import Console
        self._buffer = StringIO()
        self._console = Console(
            highlight=False, file=self._buffer, force_terminal=True,
        )
        self._transient_active = False
        # True once any message has been rendered → drives the blank-line separator
        # between message blocks (none before the first).
        self._seen_message = False
        # Working-indicator state, driven by on_chat_event (turn_started/completed).
        self._thinking = False
        self._think_start = 0.0
        # ctrl-c cancel-in-flight flag: set via request_cancel(), cleared on
        # turn end so it never leaks into the next turn. Owned here (not as a
        # closure dict in run_inline_input) so on_chat_event can clear it even
        # though the ConditionalContainer stops rendering the working row the
        # moment _thinking becomes False.
        self._cancelling = False

    def request_cancel(self) -> None:
        """Record ctrl-c cancel-in-flight; cleared automatically by on_chat_event on turn end."""
        self._cancelling = True

    def working_frags(self, now: float) -> list:
        """Current working-row fragments — delegates to app.working_line with live state."""
        from reyn.interfaces.inline.app import working_line  # deferred to avoid circular
        return working_line(self._thinking, self._think_start, now, cancelling=self._cancelling)

    def on_chat_event(self, event) -> None:
        etype = getattr(event, "type", None)
        if etype == "turn_started":
            self._thinking = True
            self._think_start = time.monotonic()
        # turn_settled fires for every turn kind (incl. slash short-circuits);
        # turn_completed/turn_cancelled are kept as belt-and-suspenders.
        elif etype in ("turn_settled", "turn_completed", "turn_cancelled"):
            self._thinking = False
            self._cancelling = False

    def bottom_toolbar(self):
        """Animated working indicator while a turn runs (spinner + elapsed).

        Re-evaluated on each prompt refresh; returns None when idle so no bar
        shows. The frame is derived from the wall clock so it advances smoothly
        regardless of refresh jitter.
        """
        if not self._thinking:
            return None
        frame = _SPINNER[int(time.monotonic() * 8) % len(_SPINNER)]
        elapsed = int(time.monotonic() - self._think_start)
        return HTML(
            f'<style fg="{_CC_ACCENT}">{frame}</style> '
            f'<style fg="{_CC_DIM}">Working… {elapsed}s</style>'
        )

    def uses_app_input(self) -> bool:
        # Interactive inline drives input via its own rule-bar Application
        # (reyn.interfaces.inline.app.run_inline_input) on a TTY.
        return True

    def _flush(self) -> None:
        s = self._buffer.getvalue()
        self._buffer.seek(0)
        self._buffer.truncate()
        if not s:
            return
        sys.__stdout__.write(s)
        sys.__stdout__.flush()

    def _clear_transient(self) -> None:
        if self._transient_active:
            sys.__stdout__.write(_CLEAR_PREV_LINE)
            sys.__stdout__.flush()
            self._transient_active = False

    def banner(self, agent_name: str) -> None:
        self._console.print(f"[{_CC_DIM}]· {agent_name} · /quit to exit ·[/]\n")
        self._flush()

    def message(self, msg: OutboxMessage) -> None:
        self._clear_transient()
        if wants_separator(msg.kind, self._seen_message):
            self._console.print()  # blank line between message blocks
        self._console.print(format_inline_message(msg))
        self._seen_message = True
        self._flush()
        self._transient_active = msg.kind in _TRANSIENT_KINDS

    def prompt_text(self) -> AnyFormattedText:
        return HTML(f'<style fg="{_CC_ACCENT}"><b>❯</b></style> ')

    def cost_summary(self, usage: TokenUsage, cost_usd: float | None) -> None:
        self._clear_transient()
        from rich.rule import Rule
        p, c, t = usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
        cost_str = f"${cost_usd:.4f}" if cost_usd is not None else "--"
        self._console.print(Rule(f"cost {cost_str}", style=_CC_DIM))
        self._console.print(
            f"[{_CC_DIM}]  prompt {p}  completion {c}  total {t}[/]"
        )
        self._flush()
