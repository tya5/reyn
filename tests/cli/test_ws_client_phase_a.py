"""Tier 2: WS client frame parser + URL builder + session proxy submit.

Issue #276 Phase A (= TUI thin client `--connect ws://...` proof of
concept). Pins the public surface of ``reyn.chat.tui.ws_client``
without standing up a real WebSocket server (= server side is the
existing ``reyn web`` endpoint, no changes needed for Phase A).

Contract pinned:

1. ``_parse_frame`` reconstructs an ``OutboxMessage`` from a JSON
   server frame matching the existing ``src/reyn/web/ws/chat.py``
   protocol — ``{kind, text, meta}`` round-trips faithfully.
2. ``_parse_frame`` returns None on:
   - malformed JSON (no crash)
   - non-dict payload
   - keepalive ping (= ``meta.$keepalive`` true)
3. Unknown ``kind`` values pass through unfiltered (= future
   ``mcp_progress`` / ``peer_delegate_resolved`` etc. flow naturally).
4. ``_build_ws_url`` constructs the ``/ws/chat/<agent>`` path from a
   host-style base + agent name (trailing-slash idempotent).
5. ``_WSSessionProxy.submit_user_text`` sends the wire-protocol
   ``user_message`` JSON via the injected send fn (= no MagicMock,
   just a recording lambda).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.ws_client import (
    _build_ws_url,
    _parse_frame,
    _WSSessionProxy,
)

# ── _parse_frame ─────────────────────────────────────────────────────────────


def test_parse_frame_reconstructs_outbox_message_basic_shape() -> None:
    """Tier 2: ``{kind, text, meta}`` JSON → OutboxMessage with same values."""
    frame = json.dumps({
        "kind": "agent",
        "text": "hello world",
        "meta": {"chain_id": "c1"},
    })
    msg = _parse_frame(frame)
    assert msg is not None
    assert msg.kind == "agent"
    assert msg.text == "hello world"
    assert msg.meta == {"chain_id": "c1"}


def test_parse_frame_intervention_meta_preserves_full_shape() -> None:
    """Tier 2: intervention frames carry the full TUI-consumed meta shape
    intact (= the contract from #258 Phase 2 / #261 source_agent /
    #277 PendingOpView, transmitted over the wire identically)."""
    frame = json.dumps({
        "kind": "intervention",
        "text": "Permission request",
        "meta": {
            "intervention_id": "iv-abcd",
            "prompt": "Allow exec?",
            "detail": "/bin/ls",
            "choices": [{"label": "Yes", "id": "y", "hotkey": "y"}],
            "source_agent": "planner",  # #261 opt-in
        },
    })
    msg = _parse_frame(frame)
    assert msg is not None
    assert msg.meta["intervention_id"] == "iv-abcd"
    assert msg.meta["source_agent"] == "planner"
    assert msg.meta["choices"][0]["label"] == "Yes"


def test_parse_frame_keepalive_returns_none() -> None:
    """Tier 2: server keepalive pings (= ``meta.$keepalive: true``) are dropped."""
    frame = json.dumps({
        "kind": "status",
        "text": "",
        "meta": {"$keepalive": True},
    })
    assert _parse_frame(frame) is None


def test_parse_frame_malformed_json_returns_none() -> None:
    """Tier 2: invalid JSON doesn't crash — return None, log a warning."""
    assert _parse_frame("{not json") is None


def test_parse_frame_non_dict_payload_returns_none() -> None:
    """Tier 2: top-level list / string / number payloads → None."""
    assert _parse_frame(json.dumps(["not", "a", "dict"])) is None
    assert _parse_frame(json.dumps("just-a-string")) is None
    assert _parse_frame(json.dumps(42)) is None


def test_parse_frame_unknown_kind_passes_through() -> None:
    """Tier 2: future kinds (= e.g. ``mcp_progress``) flow without filter.

    The server may add new kinds before the TUI catches up; the WS
    client must not silently drop them. The TUI's OutboxRouter
    default branch will render them as plain text.
    """
    frame = json.dumps({
        "kind": "mcp_progress",
        "text": "fetching…",
        "meta": {"server": "web-search", "progress_pct": 45},
    })
    msg = _parse_frame(frame)
    assert msg is not None
    assert msg.kind == "mcp_progress"


def test_parse_frame_handles_bytes_input() -> None:
    """Tier 2: websockets may yield ``bytes`` for binary frames — decode + parse."""
    payload = json.dumps({"kind": "agent", "text": "ok", "meta": {}})
    msg = _parse_frame(payload.encode("utf-8"))
    assert msg is not None
    assert msg.kind == "agent"


def test_parse_frame_missing_meta_defaults_to_empty_dict() -> None:
    """Tier 2: a frame without ``meta`` builds a message with ``meta={}``."""
    frame = json.dumps({"kind": "agent", "text": "hi"})
    msg = _parse_frame(frame)
    assert msg is not None
    assert msg.meta == {}


# ── _build_ws_url ────────────────────────────────────────────────────────────


def test_build_ws_url_appends_ws_chat_path() -> None:
    """Tier 2: host base + agent name → full ``/ws/chat/<agent>`` URL."""
    url = _build_ws_url("ws://localhost:8080", "default")
    assert url == "ws://localhost:8080/ws/chat/default"


def test_build_ws_url_idempotent_on_trailing_slash() -> None:
    """Tier 2: trailing / on the base is tolerated (= no double slash)."""
    url = _build_ws_url("ws://localhost:8080/", "research")
    assert url == "ws://localhost:8080/ws/chat/research"


def test_build_ws_url_supports_wss_scheme() -> None:
    """Tier 2: secure ``wss://`` bases work the same shape as ``ws://``."""
    url = _build_ws_url("wss://reyn.example.com", "agent-1")
    assert url == "wss://reyn.example.com/ws/chat/agent-1"


# ── _WSSessionProxy ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_proxy_submit_sends_user_message_frame() -> None:
    """Tier 2: ``submit_user_text`` sends the wire-protocol JSON frame
    via the injected send fn.

    Spies via direct attribute substitution / recording lambda (= no
    MagicMock per testing.ja.md). The server-side endpoint
    (``src/reyn/web/ws/chat.py``) dispatches on
    ``payload["type"] == "user_message"`` so the shape is the
    load-bearing contract.
    """
    sent: list[dict] = []

    async def _recorder(payload: dict) -> None:
        sent.append(payload)

    proxy = _WSSessionProxy(agent_name="planner", send_fn=_recorder)
    await proxy.submit_user_text("hello world")

    assert len(sent) == 1
    assert sent[0]["type"] == "user_message"
    assert sent[0]["text"] == "hello world"


def test_session_proxy_exposes_attrs_tui_reads_defensively() -> None:
    """Tier 2: TUI reads ``agent_name`` / ``running_skills`` / ``_interventions``
    on the session — proxy provides safe defaults so no AttributeError.

    These attributes are accessed in multiple TUI paths
    (header / Pending tab / cancel handler etc.); the Phase A proxy
    returns empty / None so each consumer sees the same shape as
    "session attached, but nothing in flight". Phase B widens the
    proxy to round-trip these to / from the server.
    """
    proxy = _WSSessionProxy(agent_name="default", send_fn=lambda _: None)
    assert proxy.agent_name == "default"
    assert proxy.running_skills == {}
    assert proxy.running_plans == {}
    assert proxy._interventions is None
