"""Tier 2: #187 B1 — the router-loop swallow handler surfaces the exception.

Before #187 B1, a mid-turn router-loop exception (e.g. the final call_llm
raising after retries — root cause: 200 + empty choices) was swallowed at
``_handle_user_message``'s ``except Exception`` into a classified outbox
summary and a graceful return, silently terminating the turn with no
diagnosable trace (req=resp+1, no logged response).

This test confirms the instrument: when ``_run_router_loop`` raises, the
handler emits a ``router_loop_terminated_by_exception`` P6 event carrying the
error type + repr, so any future swallowed loop error is primary-evidence
(deny-message principle: surface, don't silently swallow). The classified
outbox message still goes out unchanged — the instrument is additive.

The failure is injected with a real async stub (a Fake that raises) — no
MagicMock — and the assertion reads the public EventLog surface
(``_chat_events.all()``), not private state.
"""
from __future__ import annotations

import pytest

from reyn.chat.session import Session


@pytest.mark.asyncio
async def test_swallowed_router_loop_exception_emits_p6_event():
    """Tier 2: a mid-turn router-loop exception emits router_loop_terminated_by_exception (#187 B1).

    Invariant: the swallow handler does not silently swallow — it emits a P6
    event with the error type + repr so the root error is recoverable from the
    event log even when the outbox only carries a classified summary.
    """
    s = Session(agent_name="t")

    async def _raise_mid_work(text: str, chain_id: str) -> None:
        # Stand-in for the real mid-work crash (final call_llm raising after
        # retries). A real async callable — a Fake, not a mock.
        raise RuntimeError("simulated final-call crash")

    s._run_router_loop = _raise_mid_work  # inject the failure at the loop seam

    # Must not propagate — the handler swallows-but-surfaces.
    await s._handle_user_message("hello", chain_id="c-test")

    terminated = [
        e for e in s._chat_events.all()
        if e.type == "router_loop_terminated_by_exception"
    ]
    assert terminated, (
        "swallow handler must emit router_loop_terminated_by_exception so the "
        "root error is primary-evidence, not silently swallowed"
    )
    ev = terminated[0]
    assert ev.data["chain_id"] == "c-test"
    assert ev.data["error_type"] == "RuntimeError"
    assert "simulated final-call crash" in ev.data["error"]
