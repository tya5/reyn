"""``/quit`` (and ``/exit`` alias) — exit the chat from inside the TUI.

Wave-2 finding P3. Previously these were tracked only in ``help.py``'s
``_BUILTIN_HINTS`` because the TUI / REPL intercepted them before the
slash registry ever saw them — but the slash *palette* reads from the
registry, so typing ``/q`` (looking for ``/quit``) produced an empty
picker. Users who knew ``/quit`` existed from ``/help`` couldn't
discover it through the palette autocomplete that they'd just learned
from ``/help`` itself.

Register both names as registry commands. Each sends a sentinel
``OutboxMessage(kind="__quit__")`` that ``app_outbox._on_quit`` routes
to the App's existing ``action_quit_tui`` coroutine — the same path
Ctrl+D fires, so the shutdown semantics stay identical.

Two separate ``@slash`` registrations (not the ``aliases=`` field)
because the palette's prefix-match loop reads ``c.name`` only — an
alias entry would not surface on ``/ex`` input. Two separate
canonical commands cost nothing schema-wise and give both names
first-class palette + ``/help`` visibility.
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.slash import slash


async def _quit_handler(session: "object", args: str) -> None:
    """Dispatch the quit sentinel to the TUI / REPL outbox.

    ``args`` is ignored — quit takes no arguments. A future "exit with
    summary" or "exit code" feature could read it; for now any value
    is silently discarded, mirroring how Ctrl+D ignores any in-flight
    input.
    """
    await session._put_outbox(OutboxMessage(kind="__quit__", text=""))


# Both names get a registry entry. Using two ``@slash``-style
# registrations keeps the palette's ``c.name.startswith(token)`` filter
# happy for either ``/q`` or ``/ex`` prefixes.
slash("quit", summary="Exit the chat (alias: /exit, Ctrl+D)")(_quit_handler)
slash("exit", summary="Exit the chat (alias: /quit, Ctrl+D)")(_quit_handler)
