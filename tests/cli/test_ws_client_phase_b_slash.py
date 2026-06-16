"""Tier 2: ``_WSSessionProxy._maybe_handle_slash`` — issue #276 Phase B (4/5).

Pins the wire-protocol shape for slash-command forwarding. All TUI
slash commands (``/agents``, ``/attach``, ``/cost``, ``/budget``,
``/cancel``, ``/list``, ``/memory``, ``/pending``, etc.) run server-
side in ``--connect`` mode because they read ``session._registry``
and other server-side state the proxy cannot reach locally.

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

from reyn.interfaces.tui.ws_client import _parse_frame, _WSSessionProxy


@pytest.mark.asyncio
async def test_session_proxy_slash_sends_wire_frame() -> None:
    """Tier 2: ``_maybe_handle_slash`` sends
    ``{"type": "slash_command", "text": "/<full text>"}``.

    The server-side handler routes on ``payload["type"]`` and runs
    its session's ``_maybe_handle_slash(text)``, so the raw text
    (including leading ``/``) is the contract.
    """
    sent: list[dict] = []

    async def _recorder(payload: dict) -> None:
        sent.append(payload)

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_recorder)
    result = await proxy._maybe_handle_slash("/attach research")

    assert sent == [{"type": "slash_command", "text": "/attach research"}]
    # Local ChatSession returns True when a slash was handled — match
    # the shape so the TUI dispatch doesn't fall through to
    # user_message.
    assert result is True


@pytest.mark.asyncio
async def test_session_proxy_slash_preserves_args() -> None:
    """Tier 2: multi-argument slashes pass through verbatim.

    Verifies ``/attach <name with spaces?>`` / ``/budget reset
    --confirm`` / ``/answer iv-abcd1 yes`` all forward intact so
    server-side parsing matches local-mode semantics.
    """
    sent: list[dict] = []

    async def _recorder(payload: dict) -> None:
        sent.append(payload)

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_recorder)
    for cmd in [
        "/agents",
        "/attach test-agent_2",
        "/budget reset",
        "/answer iv-abcd1 yes please",
        "/memory list",
    ]:
        await proxy._maybe_handle_slash(cmd)

    assert [s["text"] for s in sent] == [
        "/agents",
        "/attach test-agent_2",
        "/budget reset",
        "/answer iv-abcd1 yes please",
        "/memory list",
    ]
    assert all(s["type"] == "slash_command" for s in sent)


def test_parse_frame_passes_through_attach_request() -> None:
    """Tier 2: ``kind="__attach_request__"`` is a recognised kind —
    no diagnostic log fires + ``OutboxMessage`` carries ``meta``
    + ``text`` intact for the TUI's ``_on_attach_request`` handler
    to consume.

    The server forwards this sentinel kind in remote mode so the
    TUI header label + conv pane stay in sync when a remote
    ``/attach`` triggers the server-side swap. Issue #276 Phase B
    (4/5).
    """
    import json as _json
    frame = _json.dumps({
        "kind": "__attach_request__",
        "text": "research",
        "meta": {},
    })
    msg = _parse_frame(frame)
    assert msg is not None
    assert msg.kind == "__attach_request__"
    assert msg.text == "research"
