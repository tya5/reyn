"""Tier 2: ``_WSSessionProxy._maybe_answer_oldest_intervention`` — issue #276 Phase B (2/5).

Pins the wire-protocol shape for the answer-intervention path. The
TUI's ``_mount_intervention`` callback path calls
``session._maybe_answer_oldest_intervention(answer)`` for both chip-
button labels and free-text answers — the proxy must accept the same
API + forward over WS.

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

from reyn.interfaces.tui.ws_client import _WSSessionProxy


@pytest.mark.asyncio
async def test_session_proxy_answer_intervention_sends_wire_frame() -> None:
    """Tier 2: ``_maybe_answer_oldest_intervention`` sends
    ``{"type": "answer_intervention", "text": <answer>}``.

    The server-side handler routes on ``payload["type"]`` so the
    shape is the load-bearing contract.
    """
    sent: list[dict] = []

    async def _recorder(payload: dict) -> None:
        sent.append(payload)

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_recorder)
    await proxy._maybe_answer_oldest_intervention("Yes")

    (only_frame,) = sent
    assert only_frame == {"type": "answer_intervention", "text": "Yes"}


@pytest.mark.asyncio
async def test_session_proxy_answer_intervention_preserves_free_text() -> None:
    """Tier 2: free-text answers (= not chip labels) pass through
    verbatim.

    The server's ``_intervention_handler.maybe_answer`` does the
    chip-label-vs-free-text dispatch (matches against the head
    intervention's ``choices``); the client just forwards whatever
    the user typed / clicked. Verifies UTF-8 + whitespace + multi-
    word strings are not mangled in transit.
    """
    sent: list[dict] = []

    async def _recorder(payload: dict) -> None:
        sent.append(payload)

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_recorder)
    answer = "  はい、 詳細は context.md を見てください  "
    await proxy._maybe_answer_oldest_intervention(answer)

    assert sent[0]["text"] == answer


@pytest.mark.asyncio
async def test_session_proxy_answer_intervention_returns_none() -> None:
    """Tier 2: returns ``None`` — actual answer-delivery outcome is
    async server-side.

    Mirrors the local ``Session._maybe_answer_oldest_intervention``
    return shape from the *TUI's* point of view: the local method
    returns bool, but the TUI's ``_mount_intervention`` callback
    doesn't use the return value (just calls + awaits). The remote
    proxy matches that effective shape.
    """
    async def _noop(_payload: dict) -> None:
        return None

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_noop)
    result = await proxy._maybe_answer_oldest_intervention("ok")
    assert result is None
