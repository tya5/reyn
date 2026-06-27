"""Pluggable chat UI backends for reyn chat."""
from __future__ import annotations

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


# Claude Code-style accent palette (matches the validated mock).
_CC_ACCENT = "#d97757"  # terracotta
_CC_DIM = "#6b7280"
_CC_DONE = "#7ee787"
_CC_ERR = "#f97066"

# Braille spinner frames for the working indicator (bottom toolbar).
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Per-kind leading marker for the inline CC-style stream. The agent / skill /
# intervention lines lead with ⏺; tool/trace detail lines lead with ⎿.
_CC_MARKER = {
    "agent": " ⏺ ",
    "status": " · ",
    "error": " ✗ ",
    "intervention": " ⏺ ",
    "trace": "   ⎿ ",
    "skill_done": " ⏺ ",
}


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
        op = result.get("op")
        path = result.get("path")
        status = result.get("status")
        if op == "read" or ("read" in t and "content" in result):
            content = result.get("content")
            if isinstance(content, str):
                lines = content.count("\n") + (1 if content else 0)
                more = " (truncated)" if status == "truncated" else ""
                return f"Read {lines} lines{more}"
        if op in ("write", "create"):
            return f"Wrote {path}" if path else "Wrote file"
        if op == "edit":
            return f"Edited {path}" if path else "Edited file"
        if status:
            return str(status)
    return _short(result, 80)


def format_inline_message(msg: OutboxMessage):
    """Pure formatter: OutboxMessage → rich Text (the inline CC-style line).

    Kept separate from rendering so the kind→marker+text mapping is testable on
    the public `.plain` surface without driving a live terminal.
    """
    from rich.text import Text
    kind = msg.kind
    meta = msg.meta or {}

    # Tool-call rows. The tool_call_* OutboxMessages arrive already in the pipe
    # (ChatLifecycleForwarder); meta carries tool / args / result / error.
    # PR2 renders every tool call; a noise filter (skip low-level read ops) is a
    # tracked follow-up if dogfood shows it's too chatty (see #2198).
    if kind == "tool_call_started":
        tool = str(meta.get("tool", msg.text))
        args = _summarize_args(meta.get("args"))
        return Text.assemble(
            (" ⏺ ", _CC_ACCENT), (tool, "bold"), (f"({args})", _CC_DIM)
        )
    if kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        return Text.assemble(("   ⎿ ", _CC_DIM), (summary, _CC_DIM))
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text
        return Text.assemble(
            ("   ⎿ ✗ ", _CC_ERR), (_short(err, 80), _CC_ERR)
        )

    text = f"{_meta_prefix(meta)}{msg.text}"
    marker = _CC_MARKER.get(kind)
    if marker is None:
        return Text(text)
    if kind == "agent":
        return Text.assemble((marker, _CC_ACCENT), (text, ""))
    if kind == "status":
        return Text.assemble((marker, _CC_DIM), (text, _CC_DIM))
    if kind == "error":
        return Text.assemble((marker, _CC_ERR), (text, _CC_ERR))
    if kind == "intervention":
        return Text.assemble((marker, _CC_ACCENT), (text, "bold"))
    if kind == "trace":
        return Text.assemble((marker, _CC_DIM), (text, _CC_DIM))
    # skill_done
    return Text.assemble((marker, _CC_DONE), (text, ""))


class InlineChatRenderer(ChatRenderer):
    """Claude Code-style inline renderer — the default interactive `reyn chat`
    backend (TTY, no `--cui`).

    Renders each OutboxMessage to stdout above the prompt_toolkit prompt via the
    same StringIO+`run_in_terminal` pattern as RichChatRenderer (the call site
    in `_output_loop` wraps each `message()` in `run_in_terminal`, so raw ANSI
    reaches the terminal without corrupting the prompt). Conversation history
    stays in the terminal's own scrollback; only the prompt is live below.

    PR1 (cutover) scope: `⏺`/`⎿` symbols + terracotta accent + per-kind
    formatting. The rule-sandwiched input bar, navigable status menu, and
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
        # Working-indicator state, driven by on_chat_event (turn_started/completed).
        self._thinking = False
        self._think_start = 0.0

    def on_chat_event(self, event) -> None:
        etype = getattr(event, "type", None)
        if etype == "turn_started":
            self._thinking = True
            self._think_start = time.monotonic()
        # turn_settled fires for every turn kind (incl. slash short-circuits);
        # turn_completed/turn_cancelled are kept as belt-and-suspenders.
        elif etype in ("turn_settled", "turn_completed", "turn_cancelled"):
            self._thinking = False

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
        self._console.print(format_inline_message(msg))
        self._flush()
        self._transient_active = msg.kind in _TRANSIENT_KINDS

    def prompt_text(self) -> AnyFormattedText:
        return HTML(f'<style fg="{_CC_ACCENT}"><b>&gt;</b></style> ')

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
