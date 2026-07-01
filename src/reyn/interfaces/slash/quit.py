"""``/quit`` (and ``/exit`` alias) — exit the chat.

Both the inline CUI (``_accept``) and the plain REPL (``_input_loop``)
intercept ``/quit`` and ``/exit`` before ``submit_user_text`` — quit is
handled at the input level.  These registry entries exist solely so both
names appear in the palette and ``/help``; the handler itself is a no-op.

Two separate ``@slash`` registrations (not the ``aliases=`` field) keep
the palette's ``c.name.startswith(token)`` filter happy for ``/q`` and
``/ex`` prefixes.
"""
from __future__ import annotations

from reyn.interfaces.slash import slash


async def _quit_handler(session: "object", args: str) -> None:
    """No-op — quit is intercepted at the input level before this fires."""


# Both names get a registry entry. Using two ``@slash``-style
# registrations keeps the palette's ``c.name.startswith(token)`` filter
# happy for either ``/q`` or ``/ex`` prefixes.
slash("quit", summary="Exit the chat (alias: /exit, Ctrl+D)")(_quit_handler)
slash("exit", summary="Exit the chat (alias: /quit, Ctrl+D)")(_quit_handler)
