"""Pluggable chat UI backends for reyn chat."""
from __future__ import annotations
import sys
from io import StringIO

from prompt_toolkit.formatted_text import AnyFormattedText, HTML

from reyn.chat.outbox import OutboxMessage
from reyn.pricing import TokenUsage


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
