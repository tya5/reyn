"""Pluggable chat UI backends for reyn chat."""
from __future__ import annotations

import re
import sys
import time
from io import StringIO

from prompt_toolkit.formatted_text import HTML, AnyFormattedText

from reyn.interfaces import palette
from reyn.llm.pricing import TokenUsage
from reyn.runtime.outbox import OutboxMessage


def _meta_prefix(meta: dict) -> str:
    """Build a `[actor#abcd] ` prefix from meta provenance, if present.

    Returns "" when neither actor nor run_id_short is set, so generic
    status / error messages stay clean.
    """
    actor = meta.get("actor")
    short = meta.get("run_id_short")
    if actor and short:
        return f"[{actor}#{short}] "
    if actor:
        return f"[{actor}] "
    if short:
        return f"[#{short}] "
    return ""


def user_submitted_display_message(event) -> OutboxMessage:
    """Build the display :class:`OutboxMessage` for a ``user_submitted``
    audit-event — the ONE neutralize-at-display-boundary seam every surface's
    ``on_audit_event`` (this module) / frame-pump handler
    (``interfaces.inline.textual_chat.app``) calls to render the user-line echo
    (#3300 P1 C).

    ``session.submit_user_text`` (``runtime/session.py``) emits ``user_submitted``
    carrying the RAW text (no neutralize — single source, the inbox path, stays
    untouched) + ``meta`` (attribution, built server-side by
    ``session._user_frame_meta``). Neutralization (ESC/control strip) happens
    HERE, at render time, via the same ``core/present/guard.get_neutralizer
    ("terminal")`` seam #2770 uses for intervention content — replacing the
    removed ``_put_outbox`` echo's inline neutralize call.
    """
    from reyn.core.present.guard import get_neutralizer
    data = event.data or {}
    text, _ = get_neutralizer("terminal").neutralize(str(data.get("text", "")))
    return OutboxMessage(kind="user", text=text, meta=dict(data.get("meta") or {}))


def intervention_answer_display_message(event) -> OutboxMessage:
    """Build the display :class:`OutboxMessage` for an
    ``intervention_answer_submitted`` audit-event — the SAME
    neutralize-at-display-boundary seam :func:`user_submitted_display_message`
    uses, applied to the last remaining answer-echo path (#3300, following the
    #3301/P1(C) ``user_submitted`` precedent exactly).

    ``InterventionHandler.deliver_answer_to`` (``runtime/services/
    intervention_handler.py``) emits ``intervention_answer_submitted``
    carrying the RAW display text (the raw answer, or the matched choice's
    label — no neutralize at the producer) + ``meta`` (attribution). This
    replaces the removed ``_put_outbox`` echo's inline
    ``_neutralize_terminal`` call — neutralization now happens HERE, at
    render time, via the same ``core/present/guard.get_neutralizer
    ("terminal")`` seam #2770 uses for intervention content.
    """
    from reyn.core.present.guard import get_neutralizer
    data = event.data or {}
    text, _ = get_neutralizer("terminal").neutralize(str(data.get("text", "")))
    return OutboxMessage(kind="user", text=text, meta=dict(data.get("meta") or {}))


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

        msg.kind ∈ {"agent","status","error","intervention","trace"}
        msg.meta carries provenance (actor, run_id, run_id_short, ...)
        """

    def prompt_text(self) -> AnyFormattedText:
        """Return the prompt passed verbatim to PromptSession.prompt_async.

        Never wrap the return value — the renderer returns the final form.
        """
        return "you > "

    def cost_summary(self, usage: TokenUsage, cost_usd: float | None) -> None:
        """Render token totals + estimated cost on shutdown."""

    def on_audit_event(self, event) -> None:
        """Hook for live session events (default no-op).

        `run_repl` subscribes this to the attached session via
        `Session.subscribe_audit_events`. Override to drive a working indicator.
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
    }

    def __init__(self, *, neutralize_body: bool = False) -> None:
        # Tracks whether the last write left a single-line transient on screen
        # that the next write should overwrite.
        self._transient_active = False
        self._thinking = False  # driven by on_audit_event
        # #2280: the durability-halt reason once ``session_halted`` fires
        # (``None`` while running) — surfaced via ``bottom_toolbar`` below so a
        # plain ``--cui`` operator sitting idle at the prompt sees it the
        # moment it happens, not only on their next submit's raised
        # ``DurabilityHaltError``.
        self._halted_reason: "str | None" = None
        # #3318: opt-in body ESC/OSC neutralize (chat.neutralize_body), default
        # off — see format_inline_message/_body_renderable's own docstrings.
        self._neutralize_body = neutralize_body

    def on_audit_event(self, event) -> None:
        etype = event.type
        if etype == "turn_started":
            self._thinking = True
        elif etype in ("turn_settled", "turn_completed", "turn_cancelled"):
            self._thinking = False
        elif etype == "user_submitted":
            self.message(user_submitted_display_message(event))
        elif etype == "intervention_answer_submitted":
            # #3300: the last outbox `kind="user"` broadcast site
            # (InterventionHandler.deliver_answer_to) migrated to this
            # audit-event — same render/neutralize idiom as user_submitted.
            self.message(intervention_answer_display_message(event))
        elif etype == "session_halted":
            self._halted_reason = (event.data or {}).get("reason")

    def bottom_toolbar(self):
        if self._halted_reason:
            return f" ⚠ SESSION HALTED — {self._halted_reason} — agent stopped accepting ops"
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
        if msg.kind == "presentation":
            # FP-0054 PR-B gap (issue #2701): this plain (--cui) renderer never had
            # `format_inline_message`'s presentation handling, so a `present` op's
            # result was silently dropped here (rendered nowhere, the OS-level op
            # itself succeeded). Render via a plain (no ANSI) Console so this
            # renderer's existing no-color contract holds — same node→renderable
            # conversion `InlineChatRenderer` uses, just captured as plain text.
            from io import StringIO as _StringIO

            from rich.console import Console

            from reyn.interfaces.repl.present_renderer import render_presentation_nodes
            buf = _StringIO()
            Console(file=buf, color_system=None, width=100).print(
                render_presentation_nodes(msg.meta.get("nodes", []))
            )
            self._write(buf.getvalue())
            self._transient_active = False
            return
        kind_prefix = self._PREFIX.get(msg.kind, "")
        meta_prefix = _meta_prefix(msg.meta)
        body_text = msg.text
        if self._neutralize_body:
            # #3318: this method writes body_text to the terminal RAW (no
            # markdown/`_body_renderable` pass — this renderer has its own
            # separate, un-styled write path). `chat.render_mode: plain`
            # forces this renderer even on a real TTY (#3292, genuine `--cui`
            # equivalence), so an un-neutralized ESC/OSC sequence here reaches
            # a live terminal exactly like the TUI presenter's body would.
            from reyn.core.present.guard import get_neutralizer
            body_text = get_neutralizer("terminal").neutralize(body_text)[0]
        # Inject meta prefix between kind tag and text so logs read
        # "[trace] [default#abcd] llm_called: ..."
        if kind_prefix:
            line = f"{kind_prefix} {meta_prefix}{body_text}\n"
        else:
            line = f"{meta_prefix}{body_text}\n"
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
            theme=chat_markdown_theme(),  # #3469: palette-derived markdown styles
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
        # Rich's Console can't auto-detect terminal width writing to a StringIO —
        # it silently falls back to 80 columns. Read the LIVE terminal width per
        # render (the terminal can resize between turns) instead of inheriting
        # that fallback (issue #2655).
        c.width = _live_terminal_width()
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
# #4787: moved alone, not with _CC_USER_BG/_CC_ERR_BG below (still blocked
# on #4840's colour direction) — safe independently because the VALUE is
# unchanged, only where it's declared; the WCAG-measured contrast pairing
# against those two backgrounds (#3371, unmoved) is therefore untouched.
_CC_DIM = palette.TOKENS["@dim@"]  # low-importance / ambient, as a COLOUR (see _CC_AMBIENT)
# The same "low-importance" role expressed as an ATTRIBUTE rather than a colour
# (#3536). ``dim`` emits SGR 2 and forces no colour, so the terminal's own theme
# decides the shade — the owner's standing direction (#3525) and the only form
# that survives a TRANSPARENT terminal background, which is what made the right
# gutter's labels unreadable: a fixed mid-grey has whatever contrast the user's
# desktop happens to give it.
#
# ★ This does NOT replace _CC_DIM, and the split is forced by measurement, not
# taste. _CC_DIM is still required wherever a real COLOUR is:
#
# - ``prompt_toolkit`` rejects an attribute outright — ``fg:dim`` raises
#   ``ValueError: Wrong color format 'dim'`` (measured), so the repl status bar
#   needs a colour value.
# - On a row that carries a TINT (``_CC_USER_BG`` / ``_CC_ERR_BG``, both fixed
#   dark hex), terminal-default ink would be dark-on-dark on a LIGHT terminal,
#   and #3367's contrast gate cannot even see the pairing: it skips any segment
#   whose foreground is not concrete. Making a foreground terminal-relative
#   while its background stays a fixed hex trades a measurable guarantee for an
#   unmeasurable one.
#
# So this is used where the row carries NO tint — measured: ``agent`` and
# ``tool_call_started`` rows have ``background=None``, and those are exactly the
# rows the right gutter's token / elapsed labels ride on.
_CC_AMBIENT = "dim"
# #4787: these 5 no longer declare their own hex literal — they read the
# SAME value from ``interfaces/palette.py``'s ``TOKENS`` dict, the one
# place ``interfaces/`` names a colour (this file's own module-level
# import: ``from reyn.interfaces import palette``, safe — palette.py has
# zero framework imports, so this pulls in no Textual dependency, verified
# directly). Each is expected to become a Textual token reference
# (``$success``/``$error``/``$warning``/``$accent``/``$secondary``) once
# #4840's reyn theme module exists; for now the VALUE is unchanged, only
# WHERE it is declared moved — see palette.py's own comment on this batch
# for the full reasoning.
_CC_DONE = palette.TOKENS["@success@"]    # green — completion
_CC_ERR = palette.TOKENS["@error@"]       # red — failure
_CC_WARN = palette.TOKENS["@warning@"]    # amber — an intervention that needs the user to act
_CC_ACCENT = palette.TOKENS["@accent@"]   # terracotta — spinner / accents
_CC_COOL = palette.TOKENS["@secondary@"]  # cool blue — a secondary accent (status-bar agent value)
# Row-TINT backgrounds. The convention (established by _CC_USER_BG, extended to
# _CC_ERR_BG by #3367): a row tint is a FAINT DARK block that the row's normal
# foreground colour stays legible against — never a saturated foreground colour
# reused as a background. A _CC_*_BG constant is only ever a background, and a
# _CC_* foreground constant is never used as one; that separation is what keeps
# "pick a foreground" and "pick a background" from colliding on the same hue.
#
# Subtle background block behind the user's own submitted line (CC styles the
# user input differently from agent output — a faint highlighted block).
# #3371: darkened from #2b2f37 — _CC_DIM on the previous value measured a
# 2.78 contrast ratio (below WCAG AA-large's 3.0), the worst pairing the
# #3367 contrast gate found. Darkened within the same blue-gray family
# (still reads as "a faint dark block", not a new hue) to raise every
# foreground's contrast against it, _CC_DIM's included — measured 3.30 at
# this value. Not a new color: the same named constant, same design role.
#
# #4787 (architect finding): _CC_DIM's own value now lives in
# interfaces/palette.py (TOKENS["@dim@"]), a DIFFERENT file from this one —
# changing @dim@ there breaks this 3.30 measurement here, and re-measuring
# only one side leaves the pairing's real number unknown. Re-measure BOTH
# _CC_USER_BG and palette.TOKENS["@dim@"] together if either changes.
#
# #4840/#4787 (lead-coder, post-arc note): interfaces/palette.py's
# TOKENS["@theme-surface@"] (REYN_THEME's `surface` role, #4875) is the
# SAME hex, "#1e222a", but a DIFFERENT token for a DIFFERENT role — this
# constant is the plain REPL's row-tint background, @theme-surface@ is the
# Textual theme's generic raised-surface colour. Same value, unrelated
# migration tracks; changing @theme-surface@ does NOT touch the 3.30
# measurement above (that pairing is with palette.TOKENS["@dim@"] only) —
# but if the two are ever consolidated into one token, the 3.30 measurement
# needs re-verifying against whatever value survives.
_CC_USER_BG = "#1e222a"
# Failure block-tint behind a failed tool call / error row. A desaturated dark
# coral: it reads unmistakably as "the red row" edge to edge (CC's block-tint of
# a failed tool) while _CC_ERR text on top of it stays high-contrast. #3367:
# every failure row previously carried background=_CC_ERR — the SAME value as
# its own foreground — so the text was painted in its background colour and the
# row rendered as a solid illegible coral band, exactly when the user most needs
# to read why something failed.
_CC_ERR_BG = "#3a1c1a"

# #3469: the COMPLETE rich-markdown style family, derived from the palette
# above — the single place LLM-output markdown styling is decided. rich's own
# ``DEFAULT_STYLES`` carry colours from a different world (``markdown.h2 =
# "underline magenta"``, ``markdown.item.number = "cyan"``, …), and any
# ``markdown.*`` key NOT overridden here resolves to that default — which is
# how #3326's single-key fix (``markdown.code`` only) left H2/H3 headings
# rendering in neon magenta. Structure is expressed as WEIGHT/EMPHASIS
# (bold / italic / underline / dim), with ``_CC_COOL`` as the one accent
# (code / links / list numbers) — matching the palette rule that colour
# signals state, not decoration. Both chat surfaces consume this: the
# Textual app pushes it onto its console (``textual_chat/app.py``), and the
# plain renderers below construct their Consoles with it — one constant, no
# per-surface drift. ``tests/interfaces/test_markdown_palette_gate_3469.py`` walks a
# rendered sample and fails if any foreground colour outside the palette
# reaches the screen, so the NEXT rich default that leaks (a new key, a
# changed default) goes RED instead of shipping.
CHAT_MARKDOWN_THEME_STYLES: "dict[str, str]" = {
    "markdown.h1": "bold underline",
    "markdown.h2": "bold underline",
    "markdown.h3": "bold",
    "markdown.h4": "bold italic",
    "markdown.h5": "italic",
    "markdown.h6": "dim",
    "markdown.h7": "dim italic",
    "markdown.block_quote": f"italic {_CC_DIM}",
    "markdown.code": _CC_COOL,
    "markdown.code_block": _CC_COOL,
    "markdown.item.number": _CC_COOL,
    "markdown.list": _CC_COOL,
    "markdown.kbd": f"bold {_CC_WARN}",
    "markdown.link": _CC_COOL,
    "markdown.link_url": f"underline {_CC_COOL}",
    "markdown.table.border": _CC_DIM,
    "markdown.table.header": "bold",
    "markdown.hr": _CC_DIM,
}


def chat_markdown_theme() -> "object":
    """The :data:`CHAT_MARKDOWN_THEME_STYLES` palette as a rich ``Theme`` —
    built lazily so this always-loaded module keeps its import surface flat."""
    from rich.theme import Theme
    return Theme(CHAT_MARKDOWN_THEME_STYLES)


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
# notice. Distinct glyphs: ● assistant · ❯ you · ▸ tool · ◆ needs-you · ✗ error ·
# ✓ done · · status · ⎿ detail.
_KIND_LINE = {
    "user":         ("❯ ",   _CC_TEXT,   _CC_DIM),   # your input  (default fg, + bg block)
    "agent":        ("● ",   _CC_TEXT,   _CC_TEXT),  # normal reply — terminal default fg
    "reasoning":    ("· ",   _CC_DIM,    _CC_DIM),   # model thinking (dim; only shown when chat.reasoning.display=true)
    "intervention": ("◆ ",   _CC_WARN,   "bold"),    # needs you   — amber
    "error":        ("✗ ",   _CC_ERR,    _CC_ERR),   # error       — red
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


def _live_terminal_width(default: int = 80) -> int:
    """The live prompt_toolkit app's terminal width in columns, re-read per call (the
    terminal can resize between turns). Falls back to `default` (Rich's own fallback)
    when no app is running — e.g. a headless test importing this module directly."""
    try:
        from prompt_toolkit.application import get_app
        return get_app().output.get_size().columns
    except Exception:
        return default


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

    #4758 (lead-coder design decision, e2e-coder): every branch of
    :func:`_summarize_result` can fold WORLD-derived bytes into the
    returned summary — ``stderr``/``content``/``answer`` and friends are
    arbitrary output from a sandboxed process or a fetched URL, not
    operator-typed ``reyn.yaml`` text. ``_short``'s own truncation
    (``" ".join(s.split())``) does NOT strip ESC/control sequences —
    ``str.split()`` splits on whitespace, and ESC is not whitespace — so
    a naive per-branch fix would need repeating at every CURRENT branch
    and remembering at every FUTURE one (the #4754 gap this issue
    reopened: that PR added exactly one such branch and missed it).
    Neutralized ONCE here instead, at this function's own SINGLE return
    boundary — every one of its 7 call sites, across 3 modules
    (``renderer.py`` itself, ``presenter.py`` x3, ``app.py`` x3; grepped
    at #4758 review after a stale "3 call sites" claim from #4753/#4754
    was caught, uncounted, sitting a few lines above this docstring)
    receives an already-safe string, structurally, not by each call
    site remembering to ask for one. Same ``get_neutralizer("terminal")``
    seam FP-0054 already established (``presenter.py``'s own
    ``_neutralized_label``, applied there to a different leaf — labels,
    not tool-result summaries — same discipline, different call site)."""
    from reyn.core.present.guard import get_neutralizer

    try:
        summary = _summarize_result(tool, result)
    except Exception:
        summary = _short(result, 80)
    return get_neutralizer("terminal").neutralize(summary)[0]


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
        # any other keys (e.g. read_file returns op="read", content="", error="file
        # not found: ..." for a missing file; without this guard the read branch
        # below would short-circuit to "Read 0 lines" and the error is never seen).
        error = result.get("error")
        if isinstance(error, str):
            return f"✗ {_short(error, 78)}"
        error_message = result.get("error_message")
        if isinstance(error_message, str):
            return f"✗ {_short(error_message, 78)}"
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
        if op == "regenerate_index":
            return f"Indexed {path}" if path else "Indexed"
        saved = result.get("saved")
        if isinstance(saved, str):
            return f"Saved {saved}"
        forgotten = result.get("deleted")
        if isinstance(forgotten, str):
            return f"Forgot {forgotten}"
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
        if isinstance(returncode, int) and status in ("cancelled", "timeout"):
            return f"✗ {status} (exit {returncode})"
        # #4753: an ordinary nonzero exit (sandboxed_exec.py's `status = "ok" if
        # returncode == 0 else ("timeout" if returncode == -1 else "error")`) fell
        # through every branch above to the bare `str(status)` = "error", discarding
        # returncode AND stderr — both present right here in `result`. This function
        # is DISPLAY-ONLY (all 7 call sites, across 3 modules — renderer.py,
        # presenter.py x3, app.py x3 — are under `interfaces/`; the LLM's own
        # `role: "tool"` content comes from `render_tool_result` via
        # `sandboxed_exec_to_canonical`, which already carries stdout/stderr and the
        # returncode). So what was lost was the OPERATOR's signal, not the model's:
        # a human saw a bare "error" and had to expand the row to learn why.
        # `stderr` is truncated via the SAME `_short(..., 78)` boundary the
        # `error`/`error_message` branches above already use (don't mint a new
        # constant when one for the same purpose — a one-line error summary —
        # already exists in this file).
        if isinstance(returncode, int) and status == "error":
            stderr = result.get("stderr")
            detail = _short(stderr, 78) if isinstance(stderr, str) and stderr else ""
            return f"✗ exit {returncode}" + (f": {detail}" if detail else "")
        freed_tokens = result.get("freed_tokens")
        if isinstance(freed_tokens, int):
            return f"Freed {freed_tokens} token{'s' if freed_tokens != 1 else ''}"
        answer = result.get("answer")
        if isinstance(answer, str) and answer:
            return _short(answer, 60)
        server_name = result.get("server_name")
        if isinstance(server_name, str) and server_name:
            return f"Installed {server_name}"
        url = result.get("url")
        if isinstance(url, str) and url:
            return _short(url, 60)
        server = result.get("server")
        if isinstance(server, str) and server and status == "ok" and result.get("kind") == "mcp_drop_server":
            return f"Removed {server}"
        enabled = result.get("enabled")
        if isinstance(enabled, bool):
            name_val = result.get("name") or ""
            verb = "Enabled" if enabled else "Disabled"
            return f"{verb} {name_val}" if name_val else verb
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


def _body_renderable(
    kind: str, text: str, body_style: str, *, neutralize_body: bool = False
):
    """The body cell: markdown for agent (LLM) output, a styled Text otherwise.

    #3318: ``neutralize_body`` (opt-in, ``chat.neutralize_body: true`` — see
    :class:`~reyn.config.chat.ChatConfig`) routes ``text`` through the SAME
    terminal neutralizer the #3302 label-side fix uses
    (``core/present/guard.get_neutralizer("terminal")``, ESC/control strip)
    BEFORE this function's own per-kind rendering. The one LIVE production
    caller of this flag is :class:`ReynPresenter` (the Textual TUI), via
    :func:`_body_and_background`. ``format_inline_message`` (below) also
    forwards the flag here, but ``format_inline_message`` itself currently
    has no live production caller — its only caller, ``InlineChatRenderer``,
    always defers to the Textual app instead (``uses_app_input() == True``,
    see that class's own docstring) — so wiring it through was cheap
    consistency/future-safety, not a second live surface. The actual live
    plain-renderer path, ``ConsoleChatRenderer.message()``, writes
    ``msg.text`` RAW (no ``_body_renderable`` pass at all) and is wired to
    the SAME ``chat.neutralize_body`` flag directly at that call site
    instead — see its own comment for why. Applied first: only real
    control bytes are stripped
    (``\\x00-\\x08``/``\\x0b``/``\\x0c``/``\\x0e-\\x1f``/``\\x7f-\\x9f``),
    none of which is valid CommonMark syntax, so markdown parsing below sees
    an equivalent-or-shorter string, never a corrupted one. Off by default —
    the neutralizer strips only real ESC/control bytes, never any printable
    character, but changing conversation body BYTES by default is still a
    fidelity change owner ruling B declined to make unconditional.
    """
    from rich.markdown import Heading, Markdown
    from rich.text import Text

    if neutralize_body:
        from reyn.core.present.guard import get_neutralizer
        text = get_neutralizer("terminal").neutralize(text)[0]

    if kind == "agent":
        # rich.Markdown centers H1 by default (LEVEL_ALIGN = {"h1": "center"}).
        # In a gutter-grid chat context that produces heavy leading whitespace —
        # "●                          My Heading". Override to left for all levels.
        class _LeftHeading(Heading):
            LEVEL_ALIGN = {tag: "left" for tag in Heading.LEVEL_ALIGN}

        class _ChatMarkdown(Markdown):
            elements = {**Markdown.elements, "heading_open": _LeftHeading}

        # Render the LLM reply as markdown (headings / bold / lists / code) like
        # Claude Code. Single newlines are hardened to CommonMark hard line breaks
        # so the model's per-line output is preserved rather than collapsed to one
        # paragraph — matching how CC displays LLM output.
        return _ChatMarkdown(_harden_soft_breaks(text or ""))
    if kind == "reasoning":
        # #3469: reasoning text is LLM output too, but full markdown here would
        # be over-rendering for an ambient block (headings/lists inside a dim
        # aside read as noise). Providers do reliably emit ``**bold**`` section
        # markers though (e.g. Gemini's "**Constructing the Output**"), and the
        # plain-Text path was showing the raw asterisks verbatim — every turn.
        # Convert JUST the paired bold markers into bold spans on the dim base;
        # everything else stays literal text.
        return _bold_marked_text(text, body_style)
    return Text(text, style=body_style)


def _bold_marked_text(text: str, base_style: str):
    """``**…**`` pairs → bold spans on ``base_style``; all else literal.

    Deliberately NOT a markdown parse: unpaired ``**`` stays visible as-is
    (never silently swallowed), and no other markdown syntax is interpreted."""
    import re

    from rich.text import Text

    out = Text(style=base_style)
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text, flags=re.DOTALL):
        out.append(text[pos:m.start()])
        out.append(m.group(1), style="bold")
        pos = m.end()
    out.append(text[pos:])
    return out


def format_inline_message(msg: OutboxMessage, *, neutralize_body: bool = False):
    """Pure formatter: OutboxMessage → a rich renderable (the inline CC-style line).

    ``neutralize_body`` (#3318, default off) is forwarded to
    :func:`_body_renderable` for the non-``user``/non-``presentation``/
    non-``intervention-with-nodes`` body path below — see that function's
    docstring for what it does, why it's opt-in, and (important) why THIS
    function currently has no live production caller (``InlineChatRenderer``
    always defers to the Textual app instead).

    A 2-cell marker gutter (glyph + space) sits in its own column; the body wraps
    in a second column so multi-line / wrapped output hang-indents under the body
    and never bleeds into the gutter. The agent (LLM) body renders as markdown; the
    user's own line gets a faint background block. Kept separate from rendering so
    the mapping stays testable.
    """
    from rich.text import Text
    kind = msg.kind
    meta = msg.meta or {}

    # FP-0054 PR-B: a `present` op's resolved render model — a one-shot inline block,
    # not a gutter-marked conversation line (it is a whole rendered document, not a
    # single reply). See `present_renderer.py` for the markup-inert render invariant.
    if kind == "presentation":
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        return render_presentation_nodes(meta.get("nodes", []))

    # Tool-call rows. ▸ marks an invocation (distinct from the ● assistant reply);
    # the ⎿ result / failure rows nest one level under it (2-space indent).
    if kind == "tool_call_started":
        tool = str(meta.get("tool", msg.text))
        args = _summarize_args(meta.get("args"))
        body = Text.assemble((tool, "bold"), (f"({args})", _CC_DIM))
        return _gutter_grid("▸ ", _CC_TEXT, body)
    if kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        err_style = summary.startswith("✗")
        s = _CC_ERR if err_style else _CC_DIM
        return _gutter_grid("  ⎿ ", s, Text(summary, style=s))
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text
        # #4762: err is WORLD-derived -- dispatcher.py's own
        # `message=f"{type(e).__name__}: {e}"` wraps ANY tool-handler
        # exception (an MCP call, a sandboxed subprocess, a provider HTTP
        # error), the same class #4758 fixed for tool_call_completed's own
        # stderr branch (via summarize_tool_result's single return
        # boundary) -- that fix never covered this branch (explicitly
        # scoped out, tracked as #4762; measured here: err does mix in
        # external content, so this IS the same hole). _short's own
        # truncation (" ".join(s.split())) does not strip ESC/control —
        # neutralize AFTER truncating (same order #4758 used).
        from reyn.core.present.guard import get_neutralizer
        safe_err = get_neutralizer("terminal").neutralize(_short(err, 80))[0]
        return _gutter_grid("  ⎿ ", _CC_ERR, Text(f"✗ {safe_err}", style=_CC_ERR))

    # #2770: an intervention announcement carries a `present`-shaped render model
    # (meta["nodes"], neutralized at the source in InterventionHandler.announce).
    # Draw its body through the SAME markup-inert `render_presentation_nodes`
    # primitive `present` uses (rendering consistency), keeping the "◆ needs you"
    # amber gutter so the affordance survives. The two-way-pause flow is unchanged
    # — this is display only.
    if kind == "intervention" and meta.get("nodes") is not None:
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        gutter, gutter_style, _ = _KIND_LINE["intervention"]
        return _gutter_grid(gutter, gutter_style, render_presentation_nodes(meta["nodes"]))

    line = _KIND_LINE.get(kind)
    if line is None:
        return Text(f"{_meta_prefix(meta)}{msg.text}")
    gutter, gutter_style, body_style = line
    # A provenance prefix ([actor#id]) is kept inline (rare for agent replies); it
    # renders as literal text inside the agent markdown body.
    # Intervention is user-facing: suppress the cryptic run_id_short hash — the
    # user doesn't need disambiguation for a prompt that has one active caller.
    # actor context (e.g. "[default] ") is still shown if present.
    if kind == "intervention":
        actor = meta.get("actor")
        _pfx = f"[{actor}] " if actor else ""
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
    return _gutter_grid(
        gutter, gutter_style,
        _body_renderable(kind, body_text, body_style, neutralize_body=neutralize_body),
    )


class InlineChatRenderer(ChatRenderer):
    """Claude Code-style inline renderer — the default interactive `reyn chat`
    backend (TTY, no `--cui`).

    Renders each OutboxMessage to stdout above the prompt_toolkit prompt via the
    same StringIO+`run_in_terminal` pattern as RichChatRenderer (the call site
    in `_output_loop` wraps each `message()` in `run_in_terminal`, so raw ANSI
    reaches the terminal without corrupting the prompt). Conversation history
    stays in the terminal's own scrollback; only the prompt is live below.

    PR1 (cutover) scope: `●`/`⎿` symbols + terracotta accent + per-kind
    formatting. The rule-sandwiched input bar, navigable status bar, and
    in-conversation animations land in follow-up PRs (a custom prompt_toolkit
    Application that replaces the PromptSession input).
    """

    def __init__(self) -> None:
        from rich.console import Console
        self._buffer = StringIO()
        self._console = Console(
            highlight=False, file=self._buffer, force_terminal=True,
            theme=chat_markdown_theme(),  # #3469: palette-derived markdown styles
        )
        self._transient_active = False
        # True once any message has been rendered → drives the blank-line separator
        # between message blocks (none before the first).
        self._seen_message = False
        # Working-indicator state, driven by on_audit_event (turn_started/completed).
        self._thinking = False
        self._think_start = 0.0
        # ctrl-c cancel-in-flight flag: set via request_cancel(), cleared on
        # turn end so it never leaks into the next turn. Owned here (on the
        # renderer, not in an input-driver closure) so on_audit_event can clear it even
        # though the ConditionalContainer stops rendering the working row the
        # moment _thinking becomes False.
        self._cancelling = False
        # Working-indicator sub-state (owner: "Working… もっと状態細分化できないの?" →
        # "何に待たされているのか知りたい"). Set/cleared from two DIFFERENT signals:
        # tool_called/tool_returned/tool_failed arrive via on_audit_event (below,
        # the SAME _audit_events subscription driving _thinking already); the
        # user-wait state is NOT one of those — verified that ask_user.py is the
        # ONLY one of the 6 intervention_bus.request() callers
        # (permissions.py/limit_handler.py/mcp_install.py/elicitation.py/
        # hooks/shell_runner.py all route through the SAME primitive but do NOT
        # emit user_intervention_requested themselves) that emits the event pair
        # directly — so this is driven by the outbox `kind="intervention"`
        # message every intervention path announces through instead (see
        # message() below), which all 6 paths DO share.
        from reyn.interfaces.repl.status import _WAITING_ON_THINKING
        self._waiting_on = _WAITING_ON_THINKING
        self._waiting_on_since = 0.0
        # #2280: same halt-reason surface as ConsoleChatRenderer — this
        # renderer's own bottom_toolbar (below) was live when ``chat.render_mode``
        # resolved ``"plain"`` on a TTY without the Textual app taking over
        # (``client_driver.run_chat_client``'s plain PromptSession loop wires
        # ``bottom_toolbar=renderer.bottom_toolbar`` — see
        # ``stream_client.run_input_loop``). #3292: that config value now
        # selects ``ConsoleChatRenderer`` upstream instead (genuine ``--cui``
        # equivalence). Since this class's ``uses_app_input()`` is always True
        # (below), it now ALWAYS takes the Textual-app branch of
        # ``run_chat_client`` and returns before reaching that loop — so this
        # ``_halted_reason``/``bottom_toolbar`` machinery is presently dead in
        # every production call site, kept as the class's own tested contract
        # (defense-in-depth / future call-site safety net), not a live path.
        self._halted_reason: "str | None" = None

    def request_cancel(self) -> None:
        """Record ctrl-c cancel-in-flight; cleared automatically by on_audit_event on turn end."""
        self._cancelling = True

    def working_frags(self, now: float) -> list:
        """Current working-row fragments — delegates to app.working_line with live state."""
        from reyn.interfaces.repl.status import working_line  # deferred to avoid circular
        return working_line(
            self._thinking, self._think_start, now, cancelling=self._cancelling,
            waiting_on=self._waiting_on, waiting_on_since=self._waiting_on_since,
        )

    def _set_waiting_on(self, waiting_on) -> None:
        self._waiting_on = waiting_on
        self._waiting_on_since = time.monotonic()

    def on_audit_event(self, event) -> None:
        from reyn.interfaces.repl.status import _WAITING_ON_BY_EVENT, _WAITING_ON_THINKING
        etype = getattr(event, "type", None)
        if etype == "turn_started":
            self._thinking = True
            self._think_start = time.monotonic()
            self._set_waiting_on(_WAITING_ON_THINKING)
        # turn_settled fires for every turn kind (incl. slash short-circuits);
        # turn_completed/turn_cancelled are kept as belt-and-suspenders.
        elif etype in ("turn_settled", "turn_completed", "turn_cancelled"):
            self._thinking = False
            self._cancelling = False
            self._set_waiting_on(_WAITING_ON_THINKING)
        elif etype in _WAITING_ON_BY_EVENT:
            data = getattr(event, "data", None) or {}
            self._set_waiting_on(_WAITING_ON_BY_EVENT[etype](data))
        elif etype == "user_answered_intervention":
            # The one signal common to ALL 6 intervention_bus.request() callers
            # (see the __init__ note above) — fires when InterventionHandler
            # records the user's answer, regardless of which kind of
            # intervention it was.
            self._set_waiting_on(_WAITING_ON_THINKING)
        elif etype == "user_submitted":
            # #3300 P1 (C): before #3292, reachable when this renderer ran the
            # shared plain PromptSession loop (``chat.render_mode: plain``
            # configured on an interactive TTY, no ``--cui``) — the default TTY
            # path bypasses this renderer entirely for ``TextualChatApp``
            # (client_driver.py), which has its OWN ``user_submitted`` handler
            # (``interfaces/inline/textual_chat/app.py``). #3292 made
            # ``render_mode: plain`` select ``ConsoleChatRenderer`` upstream
            # instead (genuine ``--cui`` equivalence), so this branch is
            # presently unreached by any production call site — kept as this
            # class's own tested contract, not a claim of live reachability.
            self.message(user_submitted_display_message(event))
        elif etype == "intervention_answer_submitted":
            # #3300: the last outbox `kind="user"` broadcast site
            # (InterventionHandler.deliver_answer_to) migrated to this
            # audit-event — same render/neutralize idiom as user_submitted, and
            # the same #3292 now-unreached status (see the note above) — the
            # default TTY path has its own handler
            # (TextualChatApp._handle_intervention_answer_event).
            self.message(intervention_answer_display_message(event))
        elif etype == "session_halted":
            # #2280: the durability-halt observability surface — see
            # ConsoleChatRenderer.on_audit_event's identical branch.
            self._halted_reason = (getattr(event, "data", None) or {}).get("reason")

    def bottom_toolbar(self):
        """Animated working indicator while a turn runs (spinner + elapsed).

        Re-evaluated on each prompt refresh; returns None when idle so no bar
        shows. The frame is derived from the wall clock so it advances smoothly
        regardless of refresh jitter.

        #2280: a latched ``_halted_reason`` takes priority over the working
        spinner — once a session halts it never resumes running, so there is
        no "thinking" state left to show, and the halt is the more important
        thing an operator needs to see.
        """
        if self._halted_reason:
            return HTML(
                f'<style fg="{_CC_WARN}">⚠ SESSION HALTED</style> '
                f'<style fg="{_CC_DIM}">{self._halted_reason} — agent stopped '
                f'accepting ops</style>'
            )
        if not self._thinking:
            return None
        frame = _SPINNER[int(time.monotonic() * 8) % len(_SPINNER)]
        elapsed = int(time.monotonic() - self._think_start)
        return HTML(
            f'<style fg="{_CC_ACCENT}">{frame}</style> '
            f'<style fg="{_CC_DIM}">Working… {elapsed}s</style>'
        )

    def uses_app_input(self) -> bool:
        # Interactive inline drives input via the Textual conversation-pane app
        # (reyn.interfaces.inline.textual_chat.run_textual_chat) on a TTY.
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
        # Ctrl+J is the GUARANTEED-works newline binding (documented per the
        # #1765-adjacent Shift+Enter investigation: most terminals can't
        # distinguish Shift+Enter from plain Enter at all — Ctrl+J sends a
        # distinct byte, LF vs CR, on every VT100-compatible terminal). Shift+
        # Enter also works out of the box on terminals with an extended
        # keyboard protocol (mintty/Git Bash default; iTerm2/kitty/Ghostty/
        # Alacritty default) — worth advertising, but Ctrl+J is the one that
        # never silently fails, so it leads.
        self._console.print(
            f"[{_CC_DIM}]· {agent_name} · Enter to send · "
            f"ctrl+j / shift+enter for newline · /quit to exit ·[/]\n"
        )
        # Multi-line paste is verified working (tmux round-trip: literal
        # insert, no premature submit, single-turn send) on any terminal that
        # respects prompt_toolkit's auto-enabled bracketed-paste mode — true
        # for virtually every terminal in current use. The rare-terminal
        # caveat (same hard-limit class as Shift+Enter): a terminal/input path
        # that ignores bracketed paste entirely sends each embedded newline as
        # a plain Enter, submitting one line at a time instead of one paste.
        self._console.print(
            f"[{_CC_DIM}]  paste: multi-line pastes work as one block on "
            f"bracketed-paste-aware terminals (virtually all modern ones); "
            f"without that, each line may submit separately.[/]\n"
        )
        self._flush()

    def message(self, msg: OutboxMessage) -> None:
        # Working-indicator sub-state: an intervention announcement is the ONE
        # signal common to ALL 6 intervention_bus.request() callers (ask_user,
        # permission confirm, cost-warn, safety-limit checkpoint, MCP install
        # confirm, elicitation, hook confirm) — InterventionHandler.announce()
        # (intervention_handler.py) puts a kind="intervention" OutboxMessage for
        # every one of them, unlike user_intervention_requested (ask_user only).
        # The OUT transition is on_audit_event's user_answered_intervention
        # handler (also common to all 6 — InterventionHandler.record_answer).
        if msg.kind == "intervention":
            from reyn.interfaces.repl.status import _WAITING_ON_FOR_USER
            self._set_waiting_on(_WAITING_ON_FOR_USER)
            # A CLOSED-SET intervention (meta["choices"] non-empty, #2770) is
            # ALSO live-rendered as a selectable region above the input by the
            # Textual chat app (interfaces/inline/textual_chat) — the
            # SAME prompt+choices this scrollback print would show. Printing it
            # here too is a permanent, redundant duplicate: once written to
            # terminal scrollback it cannot be un-printed or collapsed after the
            # answer resolves (unlike the live region, which correctly clears),
            # so it sits there looking perpetually "needs-you" even once
            # answered — the reported "残り続ける" (message never goes away) UX
            # complaint. Skip the scrollback print for closed-set only; the
            # resolved answer still lands as a compact `kind="user"` echo
            # (`deliver_answer_to`, ADR-0039 broadcast), which IS the
            # correct permanent record. Free-text interventions (no `choices`)
            # have no live region alternative — this scrollback print is their
            # ONLY visible prompt, so they are unaffected (print unchanged).
            if msg.meta.get("choices"):
                self._clear_transient()  # still drop any dangling "· thinking…" line
                return
        self._clear_transient()
        if wants_separator(msg.kind, self._seen_message):
            self._console.print()  # blank line between message blocks
        # Rich's Console cannot auto-detect terminal width writing to a StringIO —
        # it silently falls back to 80 columns. Read the LIVE terminal width per
        # render (the terminal can resize between turns) rather than inheriting
        # that fallback. Applies to every kind, not just `presentation`'s tables —
        # plain agent replies can carry wide code/diff blocks too (issue #2655).
        self._console.width = _live_terminal_width()
        # #3318: NOT wired to chat.neutralize_body here — this class's
        # uses_app_input() is always True, so it never actually reaches this
        # method in production (see the class docstring); the live plain path
        # is ConsoleChatRenderer.message() (above), wired separately.
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
