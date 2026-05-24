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


# ── tool_call_* kinds ─────────────────────────────────────────────────────


def test_tool_call_started_renders_arrow_prefix():
    """Tier 1: ``tool_call_started`` → author "tool", text "→ <name>"
    (= visual marker distinct from agent / system)."""
    msg = OutboxMessage(
        kind="tool_call_started",
        text="file__read",
        meta={"tool": "file__read", "op_id": "h1"},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.role == "message"
    assert payload.author == "tool"
    assert payload.content == "→ file__read"


def test_tool_call_completed_renders_check_prefix():
    """Tier 1: ``tool_call_completed`` → ``✓ <name>``."""
    msg = OutboxMessage(
        kind="tool_call_completed",
        text="shell__run",
        meta={"tool": "shell__run", "op_id": "h2", "result": "..."},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.author == "tool"
    assert payload.content == "✓ shell__run"


def test_tool_call_failed_renders_x_with_error_message():
    """Tier 1: ``tool_call_failed`` → ``✗ <name>: <err>`` when error_message
    present; falls back to plain ``✗ <name>`` when absent."""
    failed_with_err = OutboxMessage(
        kind="tool_call_failed",
        text="net__fetch",
        meta={
            "tool": "net__fetch",
            "op_id": "h3",
            "error_kind": "TimeoutError",
            "error_message": "request timed out",
        },
    )
    payload = outbox_to_chainlit(failed_with_err)
    assert payload is not None
    assert payload.author == "tool"
    assert payload.content == "✗ net__fetch: request timed out"

    failed_no_err = OutboxMessage(
        kind="tool_call_failed", text="t", meta={"tool": "t"},
    )
    payload = outbox_to_chainlit(failed_no_err)
    assert payload is not None
    assert payload.content == "✗ t"


def test_tool_call_prefers_meta_tool_over_text_field():
    """Tier 1: when meta.tool and text differ (= a future emitter packs
    richer info into text), the meta-side name wins so the prefix marker
    stays clean."""
    msg = OutboxMessage(
        kind="tool_call_started",
        text="file__read(path=foo)",  # richer text
        meta={"tool": "file__read"},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.content == "→ file__read"


def test_tool_call_falls_back_to_text_when_meta_missing():
    """Tier 1: defensively, when meta.tool is absent the text field is
    used so the row still shows something meaningful instead of
    ``→ (unnamed)``."""
    msg = OutboxMessage(
        kind="tool_call_started", text="my_tool", meta={},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.content == "→ my_tool"


def test_tool_call_uses_unnamed_placeholder_when_both_absent():
    """Tier 1: edge case — neither meta nor text carries a name → use a
    visible placeholder rather than rendering ``→ `` (= operator sees
    something went wrong-ish but the row doesn't disappear)."""
    msg = OutboxMessage(kind="tool_call_started", text="", meta={})
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.content == "→ (unnamed)"
