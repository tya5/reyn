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
# trailing space (e.g. for ``/plan discard ab`` the partial is
# ``"discard ab"``). Completers that don't need it (e.g. ``/attach``
# which always lists agent names) can ignore the arg via a default.
CompleterFn = Callable[..., list[str]]
# TabFooterFn signature: ``() -> str | None``. Supplies the optional
# picker-hint footer line (the dim ``↳ <message>`` sub-row shown once the
# user types ``/<cmd> ``). Returning ``None`` (or "") means "show nothing
# right now" — the command owns both the message text and its visibility
# condition (e.g. /find only surfaces its Tab-recall affordance when there
# is history to recall). The picker owns the ``↳`` chrome + styling.
TabFooterFn = Callable[[], "str | None"]


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
    # Optional structured usage line — when set, the SlashPicker hint
    # mode (= what shows once the user types ``/<cmd> ``) renders a
    # second line ``  ↳ usage: <usage>`` below the summary. Commands
    # that don't set this fall back to single-line hint (= current
    # behavior, backward-compatible for all existing commands).
    # Convention: ``/<name> <args>`` with ``<arg>`` for required and
    # ``[arg]`` for optional, matching the slash tradition (e.g.
    # ``/find <query>``, ``/copy [N|list]``).
    usage: str = ""
    # Optional docs paths for ``/help <cmd>`` focus mode. When non-empty,
    # the focus panel appends a ``  see also: <path1>, <path2>`` footer
    # so the user can navigate from the picker-hint summary to the
    # canonical concept doc. Paths are repo-relative (e.g.
    # ``"docs/concepts/plan-mode.md"``). Defaults to empty tuple so all
    # existing commands without explicit see_also are unaffected.
    see_also: tuple[str, ...] = ()
    # Optional picker-hint footer supplier. When set, the SlashPicker hint
    # mode renders a dim ``  ↳ <message>`` sub-row below the summary/usage,
    # where ``<message>`` is whatever this callable returns. The callable
    # returns ``None`` (or "") to render nothing — letting the command gate
    # the footer on runtime state (e.g. /find shows its Tab-recall hint only
    # when history is non-empty). Defaults to ``None`` so every command
    # without an explicit footer behaves exactly as before. Keeping the
    # message + its visibility inside the command (not the widget) is what
    # makes the picker generic: it never hardcodes a command name.
    tab_footer_fn: TabFooterFn | None = None


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


# ── unknown-command suggestion helper ──────────────────────────────────────


def suggest_for_unknown(cmd: str, *, names: list[str] | None = None) -> list[str]:
    """Return up to ~3 closest-match suggestions for a typo'd slash command.

    Used by :meth:`Session._dispatch_slash` to build the inline error
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
    # edit-distance matches so typing ``/fi`` reliably suggests ``/find``
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
    tab_footer_fn: TabFooterFn | None = None,
) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator that registers `fn` as a slash command on import.

    Arguments mirror :class:`SlashCommand`. The decorated function must be
    `async def fn(session, args: str) -> None`.

    ``usage`` is the optional structured usage line surfaced as the
    second row of the SlashPicker hint mode (see ``SlashCommand.usage``).

    ``see_also`` is an optional tuple of repo-relative doc paths surfaced
    in ``/help <cmd>`` focus mode as a footer link (see
    ``SlashCommand.see_also``).

    ``tab_footer_fn`` is an optional ``() -> str | None`` supplier for the
    picker-hint footer row (see ``SlashCommand.tab_footer_fn``).
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
            tab_footer_fn=tab_footer_fn,
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
from reyn.interfaces.slash import cost_inline as _cost_inline_mod  # noqa: E402, F401
from reyn.interfaces.slash import docs_filter as _docs_filter_mod  # noqa: E402, F401
from reyn.interfaces.slash import donut as _donut_mod  # noqa: E402, F401
from reyn.interfaces.slash import find as _find_mod  # noqa: E402, F401
from reyn.interfaces.slash import help as _help_mod  # noqa: E402, F401
from reyn.interfaces.slash import image as _image_mod  # noqa: E402, F401
from reyn.interfaces.slash import matrix as _matrix_mod  # noqa: E402, F401
from reyn.interfaces.slash import memory as _memory_mod  # noqa: E402, F401
from reyn.interfaces.slash import model as _model_mod  # noqa: E402, F401
from reyn.interfaces.slash import pending as _pending_mod  # noqa: E402, F401
from reyn.interfaces.slash import plan as _plan_mod  # noqa: E402, F401
from reyn.interfaces.slash import quit as _quit_mod  # noqa: E402, F401
from reyn.interfaces.slash import reset as _reset_mod  # noqa: E402, F401
from reyn.interfaces.slash import rewind as _rewind_mod  # noqa: E402, F401
from reyn.interfaces.slash import save as _save_mod  # noqa: E402, F401
from reyn.interfaces.slash import session as _session_mod  # noqa: E402, F401
from reyn.interfaces.slash import skill as _skill_mod  # noqa: E402, F401
from reyn.interfaces.slash import skills as _skills_mod  # noqa: E402, F401
from reyn.interfaces.slash import tasks as _tasks_mod  # noqa: E402, F401
from reyn.interfaces.slash import zen as _zen_mod  # noqa: E402, F401

__all__ = [
    "REGISTRY",
    "SlashRegistry",
    "SlashCommand",
    "slash",
    "reply",
    "reply_error",
]
