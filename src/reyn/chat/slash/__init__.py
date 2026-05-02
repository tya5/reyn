"""Slash command registry for `reyn chat`.

Add a new command with three lines::

    from reyn.chat.slash import slash, reply

    @slash("ping", summary="Echo pong")
    async def ping_cmd(session, args: str) -> None:
        await reply(session, "pong")

The decorator handles registration. `reply()` / `reply_error()` wrap
the OutboxMessage construction so handlers stay focused on logic.

For commands that just delegate to a `session._slash_X` method, the
body is a one-liner — see `chat.py`, `agents.py`, `budget.py`.

The TUI palette and session dispatch read from `REGISTRY` directly,
so registered commands are immediately available everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable


HandlerFn = Callable[..., Awaitable[None]]
CompleterFn = Callable[..., list[str]]


@dataclass
class SlashCommand:
    """Descriptor for a single slash command."""

    name: str               # command name without leading /  (e.g. "list")
    summary: str            # one-line description shown in /help and palette
    handler: HandlerFn      # async (session, args: str) -> None
    aliases: tuple[str, ...] = ()
    completer: CompleterFn | None = None  # optional: (session) -> list[str]
    hidden: bool = False    # if True, omit from /help and the Tab palette
                            # (still dispatchable when typed by name)


class SlashRegistry:
    """Registry mapping command names (and aliases) to SlashCommand descriptors."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}  # alias -> canonical name

    def register(self, cmd: SlashCommand) -> None:
        if cmd.name in self._commands or cmd.name in self._aliases:
            raise ValueError(f"slash command name collision: /{cmd.name}")
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            if alias in self._commands or alias in self._aliases:
                raise ValueError(f"slash alias collision: /{alias}")
            self._aliases[alias] = cmd.name

    def get(self, name: str) -> SlashCommand | None:
        """Resolve a typed name (canonical or alias) to its command."""
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def all_commands(self) -> list[SlashCommand]:
        """All registered canonical commands (excludes alias entries)."""
        return list(self._commands.values())

    def names(self) -> list[str]:
        """Sorted canonical command names (no aliases) for /help and palette."""
        return sorted(self._commands.keys())


REGISTRY: SlashRegistry = SlashRegistry()


# ── decorator ──────────────────────────────────────────────────────────────


def slash(
    name: str,
    *,
    summary: str,
    aliases: Iterable[str] = (),
    completer: CompleterFn | None = None,
    hidden: bool = False,
) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator that registers `fn` as a slash command on import.

    Arguments mirror :class:`SlashCommand`. The decorated function must be
    `async def fn(session, args: str) -> None`.
    """

    def _decorator(fn: HandlerFn) -> HandlerFn:
        REGISTRY.register(SlashCommand(
            name=name,
            summary=summary,
            handler=fn,
            aliases=tuple(aliases),
            completer=completer,
            hidden=hidden,
        ))
        return fn

    return _decorator


# ── reply helpers ──────────────────────────────────────────────────────────


async def reply(session: "object", text: str, *, kind: str = "status") -> None:
    """Emit a slash-command reply via the session outbox.

    Default kind is `status` (dim italic in the TUI). Use `reply_error`
    for errors, or pass an explicit kind for special cases.
    """
    from reyn.chat.outbox import OutboxMessage
    await session._put_outbox(OutboxMessage(kind=kind, text=text))


async def reply_error(session: "object", text: str) -> None:
    """Emit an error message (red ✗ in the TUI)."""
    await reply(session, text, kind="error")


# ── trigger registration of built-in commands ─────────────────────────────
# Sub-modules register on import; importing them here makes the registry
# fully populated as soon as `reyn.chat.slash` is imported.
from reyn.chat.slash import chat as _chat_mod  # noqa: E402, F401
from reyn.chat.slash import agents as _agents_mod  # noqa: E402, F401
from reyn.chat.slash import budget as _budget_mod  # noqa: E402, F401
from reyn.chat.slash import skills as _skills_mod  # noqa: E402, F401
from reyn.chat.slash import help as _help_mod  # noqa: E402, F401
from reyn.chat.slash import matrix as _matrix_mod  # noqa: E402, F401
from reyn.chat.slash import zen as _zen_mod  # noqa: E402, F401


__all__ = [
    "REGISTRY",
    "SlashRegistry",
    "SlashCommand",
    "slash",
    "reply",
    "reply_error",
]
