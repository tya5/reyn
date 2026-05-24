"""Tier 1: OutboxMessage → ChainlitPayload mapping contract.

The adapter is the single boundary between reyn's outbox kinds and
Chainlit's render primitives. This test pins the mapping so a kind
rename / new kind doesn't silently start being dropped or mis-rendered.

Lives in ``reyn.chainlit_app.adapter`` (= pure module, no chainlit
import) so this test runs without the ``[chainlit]`` extra installed.
"""
from __future__ import annotations

import pytest

from reyn.chainlit_app.adapter import ChainlitPayload, outbox_to_chainlit
from reyn.chat.outbox import OutboxMessage


@pytest.mark.parametrize(
    "kind,expected_role,expected_author",
    [
        ("agent", "message", "agent"),
        ("status", "message", "status"),
        ("skill_done", "message", "skill"),
        ("intervention", "message", "intervention"),
        ("system", "message", "system"),
        ("error", "error", "error"),
    ],
)
def test_known_kinds_map_to_expected_payload(
    kind: str, expected_role: str, expected_author: str
):
    """Tier 1: each PoC-supported kind lands the right (role, author)."""
    msg = OutboxMessage(kind=kind, text="hello")
    payload = outbox_to_chainlit(msg)
    assert isinstance(payload, ChainlitPayload)
    assert payload.role == expected_role
    assert payload.author == expected_author
    assert payload.content == "hello"


def test_end_sentinel_returns_end_role():
    """Tier 1: ``__end__`` produces an end-role payload (drain loop terminator)."""
    msg = OutboxMessage(kind="__end__", text="")
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.role == "end"


@pytest.mark.parametrize(
    "kind",
    ["__stream_user__", "__stream_agent__", "__stream_partial__", "trace"],
)
def test_dropped_kinds_return_none(kind: str):
    """Tier 1: incremental / trace kinds are dropped (= return None)."""
    msg = OutboxMessage(kind=kind, text="incremental")
    assert outbox_to_chainlit(msg) is None


def test_unknown_kind_falls_back_to_system_author():
    """Tier 1: unrecognised kind renders as a system message (not dropped),
    so a future kind addition surfaces in the browser instead of being silently
    eaten."""
    msg = OutboxMessage(kind="some_future_kind", text="future text")
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.role == "message"
    assert payload.author == "system"
    assert payload.content == "future text"


def test_empty_text_is_preserved_as_empty_string():
    """Tier 1: missing text field becomes "" not None (chainlit cl.Message
    rejects None content)."""
    msg = OutboxMessage(kind="agent", text="")
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.content == ""
