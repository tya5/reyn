"""Slash command registry for `reyn chat`.

Add a new command with three lines::

    from reyn.interfaces.slash import slash, reply

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
# CompleterFn signature: ``(session, arg_partial: str = "") -> list[str]``.
# ``arg_partial`` is the string typed after the slash command and the
# trailing space (e.g. for ``/attach <partial>`` the partial is what's typed
# so far). Completers that don't need it (e.g. ``/attach``
# which always lists agent names) can ignore the arg via a default.
CompleterFn = Callable[..., list[str]]


@dataclass
class SlashCommand:
    """Descriptor for a single slash command."""

    name: str               # command name without leading /  (e.g. "list")
    summary: str            # one-line description shown in /help and palette
    handler: HandlerFn      # async (session, args: str) -> None
    aliases: tuple[str, ...] = ()
    completer: CompleterFn | None = None  # optional: (session, arg_partial="") -> list[str]
    hidden: bool = False    # if True, omit from /help and the Tab palette
                            # (still dispatchable when typed by name)
    # Optional structured usage line. Two consumers, both keyed on "the user
    # has committed to this command and needs its argument syntax":
    # ``/help <cmd>`` focus mode (``help.py``), and the composer's completion
    # popup, which renders it as the ``↳ usage: <usage>`` header row of the
    # ARGUMENT stage — what shows once the user types ``/<cmd> ``
    # (``completion._usage_header``, #3364; the retired ``SlashPicker`` this
    # comment used to name was deleted by the #3273 Phase 6 rebuild).
    # Commands that don't set this render no header row at all — never a blank
    # or a placeholder.
    # Convention: ``/<name> <args>`` with ``<arg>`` for required and
    # ``[arg]`` for optional, matching the slash tradition (e.g.
    # ``/image <path>``, ``/copy [N|list]``). The bare syntax only — the
    # ``usage:`` label is the renderer's, so writing it here doubles it.
    usage: str = ""
    # Optional docs paths for ``/help <cmd>`` focus mode. When non-empty,
    # the focus panel appends a ``  see also: <path1>, <path2>`` footer
    # so the user can navigate from the picker-hint summary to the
    # canonical concept doc. Paths are repo-relative (e.g.
    # ``"docs/concepts/runtime/events.md"``). Defaults to empty tuple so all
    # existing commands without explicit see_also are unaffected.
    see_also: tuple[str, ...] = ()


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
        """Sorted visible canonical command names (no aliases, no hidden) for /help and palette."""
        return sorted(k for k, v in self._commands.items() if not v.hidden)


REGISTRY: SlashRegistry = SlashRegistry()


# ── unknown-command suggestion helper ──────────────────────────────────────


def suggest_for_unknown(cmd: str, *, names: list[str] | None = None) -> list[str]:
    """Return up to ~3 closest-match suggestions for a typo'd slash command.

    Used by :meth:`Session._maybe_handle_slash` — the OS's one slash
    dispatch method (``runtime/session.py``; the ``Session._dispatch_slash``
    this line used to name has never existed) — to build the inline error
    body when ``/<cmd>`` doesn't resolve. The suggestion list is
    intentionally tight: prefix-matches (= commands whose name starts with
    the typed token) come first, then fuzzy similarity matches
    (``difflib.get_close_matches``), deduplicated and capped at 3 total.
    When nothing matches at all, falls back to the alphabetical head.
    ``help`` is always appended as the escape hatch to the full catalog.

    Pure function (= no I/O, no registry mutation) so it's directly
    testable without the surrounding session machinery.
    """
    import difflib
    all_names = names if names is not None else REGISTRY.names()
    # Prefix-biased ranking: exact-prefix matches surface before
    # edit-distance matches so typing ``/im`` reliably suggests ``/image``
    # rather than a distantly-similar name that happens to score higher
    # in difflib. Dedup by seen-set; insertion order preserved.
    seen: set[str] = set()
    out: list[str] = []
    if cmd:
        for n in all_names:
            if n.startswith(cmd):
                if n not in seen:
                    seen.add(n)
                    out.append(n)
    # Fill remaining slots (up to 3 total) with fuzzy matches.
    fuzzy = difflib.get_close_matches(cmd, all_names, n=3, cutoff=0.3)
    for n in fuzzy:
        if n not in seen:
            seen.add(n)
            out.append(n)
    # Fall back to alphabetical head when neither prefix nor fuzzy hit.
    if not out:
        for n in all_names[:3]:
            if n not in seen:
                seen.add(n)
                out.append(n)
    # Cap at 3 before appending the always-on /help escape hatch.
    out = out[:3]
    if "help" not in out:
        out.append("help")
    return out


def slash_command_completions(
    prefix: str, *, commands: "list[SlashCommand] | None" = None
) -> list[tuple[str, str]]:
    """``(name, summary)`` pairs for the inline ``/`` autocomplete.

    Returns non-hidden commands whose name starts with ``prefix`` (the text typed
    after the leading ``/``), sorted by name. Hidden commands (donut / matrix /
    donut / matrix) are still dispatchable by name but never surface in the completion menu.
    Pure (no I/O) so it's directly testable.
    """
    cmds = commands if commands is not None else REGISTRY.all_commands()
    out = [
        (c.name, c.summary)
        for c in cmds
        if not c.hidden and c.name.startswith(prefix)
    ]
    return sorted(out)


# ── decorator ──────────────────────────────────────────────────────────────


def slash(
    name: str,
    *,
    summary: str,
    aliases: Iterable[str] = (),
    completer: CompleterFn | None = None,
    hidden: bool = False,
    usage: str = "",
    see_also: tuple[str, ...] = (),
) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator that registers `fn` as a slash command on import.

    Arguments mirror :class:`SlashCommand`. The decorated function must be
    `async def fn(session, args: str) -> None`.

    ``usage`` is the optional structured usage line surfaced by ``/help <cmd>``
    and as the completion popup's argument-stage header row (see
    ``SlashCommand.usage``).

    ``see_also`` is an optional tuple of repo-relative doc paths surfaced
    in ``/help <cmd>`` focus mode as a footer link (see
    ``SlashCommand.see_also``).
    """

    def _decorator(fn: HandlerFn) -> HandlerFn:
        REGISTRY.register(SlashCommand(
            name=name,
            summary=summary,
            handler=fn,
            aliases=tuple(aliases),
            completer=completer,
            hidden=hidden,
            usage=usage,
            see_also=see_also,
        ))
        return fn

    return _decorator


# ── reply helpers ──────────────────────────────────────────────────────────


async def reply(session: "object", text: str, *, kind: str = "system") -> None:
    """Emit a slash-command reply via the session outbox.

    Default kind is ``system`` (persistent log entry with a neutral
    ``system`` header) so prior command outputs remain visible when the
    user runs multiple commands in succession. Pass ``kind="status"``
    for ephemeral one-line indicators that should overwrite. Use
    ``reply_error`` for errors.
    """
    from reyn.runtime.outbox import OutboxMessage
    await session._put_outbox(OutboxMessage(kind=kind, text=text))


async def reply_error(session: "object", text: str) -> None:
    """Emit an error message (red ✗ in the TUI)."""
    await reply(session, text, kind="error")


# ── trigger registration of built-in commands ─────────────────────────────
# Sub-modules register on import; importing them here makes the registry
# fully populated as soon as `reyn.interfaces.slash` is imported.
from reyn.interfaces.slash import agent as _agent_mod  # noqa: E402, F401
from reyn.interfaces.slash import agents as _agents_mod  # noqa: E402, F401
from reyn.interfaces.slash import budget as _budget_mod  # noqa: E402, F401
from reyn.interfaces.slash import chat as _chat_mod  # noqa: E402, F401
from reyn.interfaces.slash import clear_history as _clear_history_mod  # noqa: E402, F401
from reyn.interfaces.slash import compact as _compact_mod  # noqa: E402, F401
from reyn.interfaces.slash import concept as _concept_mod  # noqa: E402, F401
from reyn.interfaces.slash import copy as _copy_mod  # noqa: E402, F401
from reyn.interfaces.slash import donut as _donut_mod  # noqa: E402, F401
from reyn.interfaces.slash import help as _help_mod  # noqa: E402, F401
from reyn.interfaces.slash import hook as _hook_mod  # noqa: E402, F401
from reyn.interfaces.slash import image as _image_mod  # noqa: E402, F401
from reyn.interfaces.slash import matrix as _matrix_mod  # noqa: E402, F401
from reyn.interfaces.slash import memory as _memory_mod  # noqa: E402, F401
from reyn.interfaces.slash import model as _model_mod  # noqa: E402, F401
from reyn.interfaces.slash import pending as _pending_mod  # noqa: E402, F401
from reyn.interfaces.slash import plugin as _plugin_mod  # noqa: E402, F401
from reyn.interfaces.slash import quit as _quit_mod  # noqa: E402, F401
from reyn.interfaces.slash import reload as _reload_mod  # noqa: E402, F401
from reyn.interfaces.slash import reset as _reset_mod  # noqa: E402, F401
from reyn.interfaces.slash import rewind as _rewind_mod  # noqa: E402, F401
from reyn.interfaces.slash import session as _session_mod  # noqa: E402, F401
from reyn.interfaces.slash import visibility as _visibility_mod  # noqa: E402, F401

__all__ = [
    "REGISTRY",
    "SlashRegistry",
    "SlashCommand",
    "slash",
    "reply",
    "reply_error",
]
