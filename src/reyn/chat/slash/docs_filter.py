"""/docs-filter — set or clear the docs tab substring filter.

Usage::

    /docs-filter           → clear the filter
    /docs-filter <substr>  → keep only docs whose stem matches <substr>

Sends a `__docs_filter__` sentinel; the TUI app routes it to
`right_panel.set_docs_filter(substr)`.
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash(
    "docs-filter",
    summary="Filter the docs tab by substring (empty = clear)",
    usage="/docs-filter [<substring>]",
)
async def docs_filter_cmd(session: "object", args: str) -> None:
    await session._put_outbox(OutboxMessage(
        kind="__docs_filter__",
        text=(args or "").strip(),
    ))
