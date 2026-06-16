"""Tier 2: ``_WSSessionProxy.cancel_inflight`` — issue #276 Phase B.

Pins the wire-protocol shape that ``app.action_cancel_inflight``
delegates to in ``--connect`` mode. The server-side endpoint
(``src/reyn/web/ws/chat.py``) routes on ``payload["type"]`` so the
shape is the load-bearing contract.

Spies via a recording async lambda — no ``unittest.mock`` per
``docs/deep-dives/contributing/testing.ja.md``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.ws_client import _WSSessionProxy


@pytest.mark.asyncio
async def test_session_proxy_cancel_inflight_sends_wire_frame() -> None:
    """Tier 2: ``cancel_inflight`` sends ``{"type": "cancel_inflight"}``.

    No text payload — the server-side handler iterates its session's
    running_skills + running_plans and emits the result as a status
    outbox; the client doesn't need to enumerate anything.
    """
    sent: list[dict] = []

    async def _recorder(payload: dict) -> None:
        sent.append(payload)

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_recorder)
    await proxy.cancel_inflight()

    (only_frame,) = sent
    assert only_frame == {"type": "cancel_inflight"}


@pytest.mark.asyncio
async def test_session_proxy_cancel_inflight_returns_none() -> None:
    """Tier 2: returns ``None`` — actual cancellation is async server-side.

    Callers (= ``app.action_cancel_inflight``) optimistically show a
    "cancel sent" status; the authoritative count arrives on the next
    inbound status frame.
    """
    async def _noop(_payload: dict) -> None:
        return None

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_noop)
    result = await proxy.cancel_inflight()
    assert result is None
