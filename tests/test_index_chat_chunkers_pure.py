"""Tier 2: stdlib/skills/index_chat/chunkers.py unique pure-helper contracts.

_extract_agent_name(file_path, agents_root) derives an agent name from the
path structure agents_root/<name>/chat/...

_build_chat_turn_text(...) assembles human-readable text for a chat-turn chunk.
"""
from __future__ import annotations

from reyn.stdlib.skills.index_chat.chunkers import (
    _build_chat_turn_text,
    _extract_agent_name,
)

# ── _extract_agent_name ───────────────────────────────────────────────────────


def test_extract_agent_name_canonical_path() -> None:
    """Tier 2: standard path yields the agent directory component."""
    path = "/workspace/.reyn/agents/alice/chat/session.jsonl"
    root = "/workspace/.reyn/agents"
    assert _extract_agent_name(path, root) == "alice"


def test_extract_agent_name_trailing_slash_on_root() -> None:
    """Tier 2: agents_root with trailing slash is normalised before stripping."""
    path = "/root/agents/bob/chat/session.jsonl"
    root = "/root/agents/"
    assert _extract_agent_name(path, root) == "bob"


def test_extract_agent_name_path_outside_root() -> None:
    """Tier 2: path not under agents_root falls back to first segment of path."""
    path = "other/charlie/session.jsonl"
    root = "/workspace/.reyn/agents"
    assert _extract_agent_name(path, root) == "other"


def test_extract_agent_name_no_slash_after_strip_returns_unknown() -> None:
    """Tier 2: no subdirectory after root prefix → 'unknown'."""
    root = "/workspace/.reyn/agents"
    path = "/workspace/.reyn/agents/flat-no-subdir"
    assert _extract_agent_name(path, root) == "unknown"


# ── _build_chat_turn_text ─────────────────────────────────────────────────────


def test_build_chat_turn_text_minimal_turn() -> None:
    """Tier 2: inline_reply turn without media or routed_action."""
    text = _build_chat_turn_text(
        agent="alice",
        chain_id="chain-1",
        turn_ts="2024-01-15T12:00:00Z",
        user_text="hello",
        media_count=0,
        turn_outcome="inline_reply",
        routed_action=None,
    )
    assert "agent: alice" in text
    assert "chain_id: chain-1" in text
    assert "user: hello" in text
    assert "turn_outcome: inline_reply" in text
    assert "media_blocks" not in text
    assert "routed_action" not in text


def test_build_chat_turn_text_routing_with_action() -> None:
    """Tier 2: routing turn includes routed_action line."""
    text = _build_chat_turn_text(
        agent="bob",
        chain_id="chain-2",
        turn_ts="2024-01-15T13:00:00Z",
        user_text="run something",
        media_count=0,
        turn_outcome="routing",
        routed_action="my_skill",
    )
    assert "turn_outcome: routing" in text
    assert "routed_action: my_skill" in text


def test_build_chat_turn_text_with_media_blocks() -> None:
    """Tier 2: media_count > 0 inserts media_blocks line."""
    text = _build_chat_turn_text(
        agent="alice",
        chain_id="chain-3",
        turn_ts="2024-01-15T14:00:00Z",
        user_text="see image",
        media_count=2,
        turn_outcome="inline_reply",
        routed_action=None,
    )
    assert "media_blocks: 2" in text


def test_build_chat_turn_text_field_order() -> None:
    """Tier 2: agent and user fields appear before turn_outcome."""
    text = _build_chat_turn_text(
        agent="alice",
        chain_id="chain-4",
        turn_ts="ts",
        user_text="msg",
        media_count=0,
        turn_outcome="spawned",
        routed_action=None,
    )
    lines = text.splitlines()
    agent_idx = next(i for i, l in enumerate(lines) if l.startswith("agent:"))
    outcome_idx = next(i for i, l in enumerate(lines) if l.startswith("turn_outcome:"))
    assert agent_idx < outcome_idx
