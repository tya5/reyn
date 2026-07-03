"""Tier 2: /cost and /budget slash command handlers.

``cost_cmd`` and ``budget_cmd`` in ``slash/budget.py`` are thin wrappers over
the session's ``_budget`` gateway. Two stub gateways exercise:

1. Disabled (returns None) → "tracker disabled" reply, never crashes.
2. Active (returns a string/dict) → reply with formatted output.

The ``/budget reset`` path has non-trivial formatting logic (agent_tokens /
agent_cost_usd / rate_window_sizes branches) that is
pinned separately.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.budget import budget_cmd, cost_cmd
from reyn.runtime.outbox import OutboxMessage


class _FakeSession:
    def __init__(self, *, budget) -> None:
        self._budget = budget
        self.outbox_calls: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_calls.append(msg)

    def reply_text(self) -> str:
        return "\n".join(m.text for m in self.outbox_calls if m.text)


class _DisabledBudget:
    def cost_line(self) -> None:
        return None

    def budget_full(self) -> None:
        return None

    def reset_all(self) -> None:
        return None


class _ActiveBudget:
    def cost_line(self) -> str:
        return "$0.0042 (105 tok)"

    def budget_full(self) -> str:
        return "full breakdown text"

    def reset_all(self) -> dict:
        return {
            "agent_tokens": {"alpha": 1500},
            "agent_cost_usd": {"alpha": 0.003},
            "rate_window_sizes": [5],
        }


# ── /cost ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_cmd_disabled_reports_disabled() -> None:
    """Tier 2: /cost with no tracker → disabled message, not a crash."""
    session = _FakeSession(budget=_DisabledBudget())
    await cost_cmd(session, "")
    assert session.outbox_calls, "expected a reply"
    assert any("disabled" in m.text.lower() for m in session.outbox_calls)


@pytest.mark.asyncio
async def test_cost_cmd_active_replies_cost_line() -> None:
    """Tier 2: /cost with an active tracker replies with the cost_line string."""
    session = _FakeSession(budget=_ActiveBudget())
    await cost_cmd(session, "")
    assert "$0.0042" in session.reply_text()


# ── /budget (full breakdown) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_cmd_no_sub_disabled_reports_disabled() -> None:
    """Tier 2: /budget with no tracker → disabled message."""
    session = _FakeSession(budget=_DisabledBudget())
    await budget_cmd(session, "")
    assert any("disabled" in m.text.lower() for m in session.outbox_calls)


@pytest.mark.asyncio
async def test_budget_cmd_no_sub_active_replies_full_breakdown() -> None:
    """Tier 2: /budget (no sub-command) replies with the budget_full string."""
    session = _FakeSession(budget=_ActiveBudget())
    await budget_cmd(session, "")
    assert "full breakdown text" in session.reply_text()


# ── /budget reset ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_reset_disabled_reports_disabled() -> None:
    """Tier 2: /budget reset with no tracker → disabled message, no reset performed."""
    session = _FakeSession(budget=_DisabledBudget())
    await budget_cmd(session, "reset")
    assert any("disabled" in m.text.lower() for m in session.outbox_calls)


@pytest.mark.asyncio
async def test_budget_reset_active_mentions_per_agent_counters() -> None:
    """Tier 2: /budget reset with an active tracker formats per-agent token/cost rows."""
    session = _FakeSession(budget=_ActiveBudget())
    await budget_cmd(session, "reset")
    body = session.reply_text()
    assert "alpha" in body, "per-agent name surfaced"
    assert "1,500" in body or "1500" in body, "token count surfaced"
    assert "$0.003" in body or "0.003" in body, "cost surfaced"


@pytest.mark.asyncio
async def test_budget_reset_mentions_agent_and_rate() -> None:
    """Tier 2: /budget reset reports per-agent + rate-limit window lines."""
    session = _FakeSession(budget=_ActiveBudget())
    await budget_cmd(session, "reset")
    body = session.reply_text()
    assert "per-agent" in body.lower() or "agent" in body.lower()
    assert "rate" in body.lower()


@pytest.mark.asyncio
async def test_budget_reset_includes_confirmation_prefix() -> None:
    """Tier 2: /budget reset reply starts with a 'reset' confirmation."""
    session = _FakeSession(budget=_ActiveBudget())
    await budget_cmd(session, "reset")
    body = session.reply_text()
    assert "reset" in body.lower()
