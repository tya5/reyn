"""Tier 1: OutboxMessage → ChainlitPayload mapping contract.

The adapter is the single boundary between reyn's outbox kinds and
Chainlit's render primitives. This test pins the mapping so a kind
rename / new kind doesn't silently start being dropped or mis-rendered.

Lives in ``reyn.interfaces.chainlit_app.adapter`` (= pure module, no chainlit
import) so this test runs without the ``[chainlit]`` extra installed.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.chainlit_app.adapter import (
    MSG_TYPE_ASSISTANT,
    MSG_TYPE_SYSTEM,
    ChainlitPayload,
    outbox_to_chainlit,
)
from reyn.runtime.outbox import OutboxMessage


@pytest.mark.parametrize(
    "kind,expected_role,expected_author,expected_type",
    [
        ("agent",        "message", "agent",          MSG_TYPE_ASSISTANT),
        ("status",       "message", "⚙ status",       MSG_TYPE_SYSTEM),
        ("skill_done",   "message", "✨ skill",       MSG_TYPE_SYSTEM),
        ("intervention", "message", "❓ intervention", MSG_TYPE_SYSTEM),
        ("system",       "message", "ℹ system",       MSG_TYPE_SYSTEM),
        ("error",        "error",   "error",          MSG_TYPE_SYSTEM),
    ],
)
def test_known_kinds_map_to_expected_payload(
    kind: str,
    expected_role: str,
    expected_author: str,
    expected_type: str,
):
    """Tier 1: each PoC-supported kind lands the right (role, author, type)."""
    msg = OutboxMessage(kind=kind, text="hello")
    payload = outbox_to_chainlit(msg)
    assert isinstance(payload, ChainlitPayload)
    assert payload.role == expected_role
    assert payload.author == expected_author
    assert payload.content == "hello"
    assert payload.message_type == expected_type


def test_agent_uses_assistant_message_type():
    """Tier 1: only ``agent`` uses ``assistant_message`` (= primary
    bubble); everything else uses ``system_message`` (= secondary
    bubble) so tool / status rows sit visually behind the assistant
    reply."""
    msg = OutboxMessage(kind="agent", text="reply")
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.message_type == MSG_TYPE_ASSISTANT


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
    assert payload.author == "ℹ system"
    assert payload.content == "future text"
    assert payload.message_type == MSG_TYPE_SYSTEM


def test_empty_text_is_preserved_as_empty_string():
    """Tier 1: missing text field becomes "" not None (chainlit cl.Message
    rejects None content)."""
    msg = OutboxMessage(kind="agent", text="")
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.content == ""


# ── tool_call_* kinds ─────────────────────────────────────────────────────


def test_tool_call_started_renders_arrow_prefix():
    """Tier 1: ``tool_call_started`` → author "🔧 tool", text "→ <name>"
    (= visual marker distinct from agent / system, system_message
    type so the bubble styles secondary)."""
    msg = OutboxMessage(
        kind="tool_call_started",
        text="file__read",
        meta={"tool": "file__read", "op_id": "h1"},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.role == "message"
    assert payload.author == "🔧 tool"
    assert payload.content == "→ file__read"
    assert payload.message_type == MSG_TYPE_SYSTEM


def test_tool_call_completed_renders_result_preview():
    """Tier 1: #1642 — ``tool_call_completed`` renders the result preview inline:
    ``✓ <name> → <result>`` (from meta["result"]); bare ``✓ <name>`` when absent."""
    msg = OutboxMessage(
        kind="tool_call_completed",
        text="shell__run",
        meta={"tool": "shell__run", "op_id": "h2", "result": "exit 0"},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.author == "🔧 tool"
    assert payload.content == "✓ shell__run → exit 0"
    assert payload.message_type == MSG_TYPE_SYSTEM

    # No result in meta → bare name (fallback).
    bare = OutboxMessage(kind="tool_call_completed", text="t", meta={"tool": "t"})
    assert outbox_to_chainlit(bare).content == "✓ t"


def test_tool_call_started_renders_args_inline():
    """Tier 1: #1642 — ``tool_call_started`` renders args inline: ``→ <name>(k=v, …)``
    (from meta["args"]); bare ``→ <name>`` when args absent/empty."""
    with_args = OutboxMessage(
        kind="tool_call_started",
        text="file__read",
        meta={"tool": "file__read", "args": {"path": "notes.txt"}},
    )
    assert outbox_to_chainlit(with_args).content == "→ file__read(path=notes.txt)"

    empty_args = OutboxMessage(
        kind="tool_call_started", text="x", meta={"tool": "x", "args": {}},
    )
    assert outbox_to_chainlit(empty_args).content == "→ x"


def test_tool_call_content_is_truncated():
    """Tier 1: #1642 — a large arg/result preview is truncated (the inline row is
    length-bounded; full content is out of scope). Behavior-pinned: truncation marker
    present + the full result is NOT inlined — no exact-length assertion."""
    full = "A" * 1000
    big = OutboxMessage(
        kind="tool_call_completed", text="x", meta={"tool": "x", "result": full},
    )
    content = outbox_to_chainlit(big).content
    assert content.endswith("…")   # truncation marker (the documented behavior)
    assert full not in content     # the full result is NOT inlined (bounded)


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
    assert payload.author == "🔧 tool"
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
