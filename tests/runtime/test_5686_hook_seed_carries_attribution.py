"""Tier 2: #5686 — an E (wake=true hook) turn's own seed carries the SAME
``[hook:<name>]`` attribution its history entry and outbox announcement
already use, instead of the bare, unattributed payload text.

Background (found during #5678's own investigation, verified independently
by lead-coder): ``Session._handle_hook_message`` builds an ``attributed``
string (``_format_ride_along_attribution``) and uses it for BOTH the
persisted ``ChatMessage`` and the outbox announcement — but historically
passed the RAW ``text`` (unattributed) to ``_run_router_loop`` as the turn's
own seed. ``RouterLoop.run``'s own fallback guard
(``if not history or history[-1].get("role") != "user":
messages.append({"role": "user", "content": user_text})``) fires for every
hook-driven turn today (the history entry is ``role="system"``, currently
excluded from ``build_history``'s allowlist, so ``history[-1]`` is never
``"user"``) — so the hook's bare text reached the model as an unattributed
``role="user"`` turn, indistinguishable from the operator's own words
(a #3595-class misattribution, narrower blast radius: no slash-dispatch
gate is reachable this way — see #5686's own issue body).

Verified here via the SAME real-callable seam
``tests/runtime/test_3475_mcp_probe_priming_all_turn_kinds.py`` uses:
``Session._loop_driver.run_turn`` replaced with a plain async function
(instance method-assignment, not a mock) that captures its own ``user_text``
argument the instant it is invoked — no LLM call, no tool loop, the test
only cares what string reaches this seam.

strip-falsify (executed manually before landing): reverting
``_handle_hook_message``'s ``await self._run_router_loop(attributed,
chain_id)`` back to ``await self._run_router_loop(text, chain_id)`` makes
this test go RED — the captured ``user_text`` reads back as the bare
``"wake up"`` with no ``[hook:`` prefix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

AGENT = "5686-hook-seed-agent"


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name=AGENT,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


def _install_capturing_run_turn(session: Session) -> dict:
    """Real, plain async function (method-assignment, not a mock) that
    captures the exact ``user_text`` ``run_turn`` was invoked with, then
    ends the turn immediately — no LLM call, no tool loop."""
    box: dict = {"user_text": "UNSET"}

    async def _capturing_run_turn(user_text: str, chain_id: str) -> None:
        box["user_text"] = user_text

    session._loop_driver.run_turn = _capturing_run_turn
    return box


@pytest.mark.asyncio
async def test_hook_driven_turn_seed_carries_hook_attribution(tmp_path: Path) -> None:
    """Tier 2: #5686 accept — a ``kind="hook"`` (wake=true, E) turn's own
    seed carries the ``[hook:<name>]`` prefix, matching what the history
    entry and outbox announcement already carry — not the bare payload
    text."""
    session = _make_session(tmp_path)
    box = _install_capturing_run_turn(session)

    await session.inbox.put((
        "hook",
        {"name": "session_start", "text": "wake up", "chain_id": "chain-5686"},
    ))
    await session.run_one_iteration()

    assert box["user_text"] != "UNSET", "run_turn was never invoked"
    assert box["user_text"] == "[hook:session_start] wake up", (
        f"the hook turn's own seed must carry the SAME [hook:<name>] "
        f"attribution its history entry and outbox announcement already "
        f"use — got {box['user_text']!r} (bare, unattributed text reaching "
        f"the model as an operator-indistinguishable role=\"user\" turn is "
        f"the #5686 defect)"
    )
