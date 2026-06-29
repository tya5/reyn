"""Tier 2: _repair_dangling_tool_calls — synthetic tool_result injection.

Crash or ctrl-c between the role=assistant+tool_calls write and the
role=tool writes leaves the history in an API-invalid state. The repair
function inserts synthetic interrupted error results for every tool_call_id
that has no matching role=tool, so the LLM wire format satisfies the
provider pairing invariant on the next turn.
"""
from __future__ import annotations

import json

from reyn.runtime.services.router_history_buffer import _repair_dangling_tool_calls


def _tc(id: str, name: str = "some_tool") -> dict:
    return {"id": id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _assistant(tool_call_ids: list[str]) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [_tc(i) for i in tool_call_ids]}


def _tool(id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": id, "content": content}


def _user(text: str = "hi") -> dict:
    return {"role": "user", "content": text}


def _is_interrupted(m: dict) -> bool:
    try:
        d = json.loads(m["content"])
        return d.get("error", {}).get("kind") == "interrupted"
    except Exception:
        return False


# ── No tool_calls → pass-through ─────────────────────────────────────────────


def test_no_tool_calls_returns_messages_unchanged():
    """Tier 2: messages with no tool_calls are returned unchanged."""
    msgs = [_user("hello"), {"role": "assistant", "content": "hi"}]
    assert _repair_dangling_tool_calls(msgs) == msgs


def test_empty_list_returns_empty():
    """Tier 2: empty input → empty output."""
    assert _repair_dangling_tool_calls([]) == []


# ── Fully answered tool_calls → pass-through ─────────────────────────────────


def test_fully_answered_single_tool_call_unchanged():
    """Tier 2: assistant+tool_calls followed by all matching role=tool → no injection."""
    msgs = [_assistant(["id-1"]), _tool("id-1")]
    result = _repair_dangling_tool_calls(msgs)
    assert result == msgs


def test_fully_answered_two_tool_calls_unchanged():
    """Tier 2: two tool_calls both answered → no injection."""
    msgs = [_assistant(["a", "b"]), _tool("a"), _tool("b")]
    result = _repair_dangling_tool_calls(msgs)
    assert result == msgs


# ── Dangling (missing role=tool) → synthetic inserted ────────────────────────


def test_single_dangling_tool_call_gets_interrupted_result():
    """Tier 2: one tool_call with no role=tool → synthetic interrupted result inserted."""
    msgs = [_assistant(["id-1"])]
    result = _repair_dangling_tool_calls(msgs)
    tool_msgs = {m["tool_call_id"]: m for m in result if m.get("role") == "tool"}
    assert "id-1" in tool_msgs
    assert _is_interrupted(tool_msgs["id-1"])


def test_two_tool_calls_one_missing_injects_only_missing():
    """Tier 2: two tool_calls, one answered — only the missing id gets synthetic."""
    msgs = [_assistant(["a", "b"]), _tool("a")]
    result = _repair_dangling_tool_calls(msgs)
    tool_by_id = {m["tool_call_id"]: m for m in result if m.get("role") == "tool"}
    assert "a" in tool_by_id and "b" in tool_by_id
    assert not _is_interrupted(tool_by_id["a"]), "answered call must not be replaced"
    assert _is_interrupted(tool_by_id["b"]), "missing call must get synthetic"


def test_all_tool_calls_missing_all_get_synthetics():
    """Tier 2: two tool_calls, none answered → both ids get interrupted synthetics."""
    msgs = [_assistant(["x", "y"])]
    result = _repair_dangling_tool_calls(msgs)
    tool_by_id = {m["tool_call_id"]: m for m in result if m.get("role") == "tool"}
    assert "x" in tool_by_id and "y" in tool_by_id
    assert _is_interrupted(tool_by_id["x"])
    assert _is_interrupted(tool_by_id["y"])


# ── Multi-block: earlier blocks complete, last block dangling ─────────────────


def test_earlier_block_complete_later_block_dangling():
    """Tier 2: first assistant+tool_calls fully answered; second block dangling."""
    msgs = [
        _user("first"),
        _assistant(["id-1"]),
        _tool("id-1"),
        _user("second"),
        _assistant(["id-2"]),      # ← dangling (no role=tool follows)
    ]
    result = _repair_dangling_tool_calls(msgs)
    tool_by_id = {m["tool_call_id"]: m for m in result if m.get("role") == "tool"}
    assert "id-1" in tool_by_id and "id-2" in tool_by_id
    assert not _is_interrupted(tool_by_id["id-1"]), "answered call must not be replaced"
    assert _is_interrupted(tool_by_id["id-2"]), "dangling call must get synthetic"


def test_synthetics_inserted_before_next_user_turn():
    """Tier 2: synthetic result appears immediately after the assistant turn, before next user."""
    msgs = [_assistant(["id-1"]), _user("follow-up")]
    result = _repair_dangling_tool_calls(msgs)
    roles = [m["role"] for m in result]
    assert roles == ["assistant", "tool", "user"]


def test_non_tool_call_assistant_between_blocks_not_affected():
    """Tier 2: an assistant message without tool_calls is left untouched."""
    msgs = [
        _assistant(["id-1"]),
        _tool("id-1"),
        {"role": "assistant", "content": "plain reply"},
        _user("next"),
    ]
    result = _repair_dangling_tool_calls(msgs)
    assert result == msgs
