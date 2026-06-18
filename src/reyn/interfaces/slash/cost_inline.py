"""/cost-inline — toggle per-turn cost suffix in the conversation view.

Sends a sentinel OutboxMessage (kind ``__cost_inline_toggle__``) that the TUI
app intercepts to flip the cost-display state.  Falls back gracefully in
``--cui`` mode (the sentinel is simply ignored).

Usage::

    /cost-inline          # toggle (default)
    /cost-inline on       # force on
    /cost-inline off      # force off
    /cost-inline toggle   # explicit toggle
"""
from __future__ import annotations

from reyn.interfaces.slash import slash
from reyn.runtime.outbox import OutboxMessage


def _normalise(args: str) -> str:
    """Return ``"on"``, ``"off"``, or ``""`` (toggle) from raw args text."""
    token = args.strip().lower()
    if token == "on":
        return "on"
    if token == "off":
        return "off"
    # empty / "toggle" / anything unrecognised → toggle
    return ""


@slash("cost-inline", summary="Toggle per-turn cost suffix in conversation")
async def cost_inline_cmd(session: "object", args: str) -> None:
    normalised_arg = _normalise(args)
    await session._put_outbox(OutboxMessage(
        kind="__cost_inline_toggle__",
        text=normalised_arg,
    ))
