"""/cost and /budget slash commands.

Migrated out of ``session.py`` per the cli-redesign plan (`docs/deep-dives/
contributing/cli-redesign.md`): the session is no longer the home for
slash command logic — it just holds the BudgetGateway the handlers read.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, slash

if TYPE_CHECKING:
    from reyn.chat.session import Session


_TRACKER_DISABLED = (
    "budget tracker is disabled (no `cost:` config or non-chat mode)"
)


@slash("cost", summary="Quick token + USD cost summary for this agent")
async def cost_cmd(session: "Session", args: str) -> None:
    """``/cost`` — one-line token + USD spend for the attached agent."""
    line = session._budget.cost_line()
    if line is None:
        await reply(session, _TRACKER_DISABLED)
        return
    await reply(session, line)


@slash(
    "budget",
    summary="Full budget breakdown",
    usage="/budget [reset]",
    see_also=("docs/reference/config/budget.md",),
)
async def budget_cmd(session: "Session", args: str) -> None:
    """``/budget`` (full breakdown) or ``/budget reset`` (clear counters).

    ``reset`` clears per-process counters (agent tokens / cost, chain skill
    calls, rate-limit window) but leaves daily / monthly counters alone —
    those auto-reset at period boundary and are backed by a persistent
    ledger that the user shouldn't be able to wipe via a chat command.
    """
    sub = args.strip()
    if sub == "reset":
        before = session._budget.reset_all()
        if before is None:
            await reply(session, _TRACKER_DISABLED)
            return
        lines = ["Budget counters reset."]
        if before.get("agent_tokens"):
            for a, t in before["agent_tokens"].items():
                cost = before.get("agent_cost_usd", {}).get(a, 0.0)
                lines.append(f"  per-agent ({a}) tokens:    {t:>10,} → 0")
                lines.append(f"  per-agent ({a}) cost_usd:  ${cost:.4f} → $0.00")
        if before.get("chain_skill_calls"):
            lines.append("  per-chain skill calls:        cleared")
        if before.get("rate_window_sizes"):
            lines.append("  rate-limit window:            cleared")
        lines.append(
            "Note: daily / monthly counters are NOT reset — "
            "they auto-reset at period boundary."
        )
        lines.append("Use `/budget` to verify.")
        await reply(session, "\n".join(lines))
        return

    text = session._budget.budget_full()
    if text is None:
        await reply(session, _TRACKER_DISABLED)
        return
    await reply(session, text)
