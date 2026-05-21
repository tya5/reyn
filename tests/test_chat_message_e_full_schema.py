"""Tier 2: ChatMessage Design-B schema + read-time migration (issue #383).

E-full Phase 1 pins:
  - New constructor: ``role`` ∈ {user, assistant, tool, system, summary,
    skill_event}; ``content`` is ``str | list[dict]``;
    ``tool_calls`` / ``tool_call_id`` / ``name`` for tool-turn fields.
  - Constructor still accepts legacy ``text=`` / ``media=`` kwargs (= folds
    into ``content``). PR-A compat shim — removed in PR-B sweep.
  - ``role="agent"`` is renamed to ``"assistant"`` at construction time.
  - Backward-compat read properties: ``m.text`` returns the str content
    (or extracts the first text part from a list); ``m.media`` returns
    non-text content parts from a list.
  - ``_migrate_legacy_chat_message`` rewrites pre-#383 dicts into the
    new shape (used by ``load_history`` on disk-read).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from reyn.chat.session import ChatMessage, _migrate_legacy_chat_message

# ── Constructor: new shape ─────────────────────────────────────────────


def test_chat_message_minimal_text_construction() -> None:
    """Tier 2: bare new-shape construction with str content."""
    m = ChatMessage(role="user", content="hi", ts="t1")
    assert m.role == "user"
    assert m.content == "hi"
    assert m.tool_calls is None
    assert m.tool_call_id is None
    assert m.name is None


def test_chat_message_assistant_with_tool_calls() -> None:
    """Tier 2: assistant role + tool_calls list — mirrors OpenAI shape."""
    tc = [{
        "id": "call_1", "type": "function",
        "function": {"name": "file_read", "arguments": '{"path":"a.py"}'},
    }]
    m = ChatMessage(role="assistant", content="", ts="t1", tool_calls=tc)
    assert m.role == "assistant"
    assert m.tool_calls == tc


def test_chat_message_tool_role_with_call_id_and_name() -> None:
    """Tier 2: tool role carries ``tool_call_id`` + ``name`` linking back
    to the originating tool_call on the prior assistant turn.
    """
    m = ChatMessage(
        role="tool", content="<file contents>", ts="t1",
        tool_call_id="call_1", name="file_read",
    )
    assert m.role == "tool"
    assert m.tool_call_id == "call_1"
    assert m.name == "file_read"


def test_chat_message_multimodal_user_content_list() -> None:
    """Tier 2: user turn with attached image — content is a list of parts
    (text + image_url) per litellm wire format.
    """
    parts = [
        {"type": "text", "text": "describe"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    m = ChatMessage(role="user", content=parts, ts="t1")
    assert m.role == "user"
    assert isinstance(m.content, list)
    assert m.content == parts


# ── Constructor: legacy kwargs compat (= PR-A only, removed in PR-B) ───


def test_legacy_text_kwarg_folds_into_content() -> None:
    """Tier 2 (compat): pre-#383 callers pass ``text=...``. Result has
    ``content`` set to the same string; ``text`` is not stored.
    """
    m = ChatMessage(role="user", text="hello", ts="t1")
    assert m.content == "hello"
    # Property delegates back to content.
    assert m.text == "hello"


def test_legacy_media_kwarg_folds_into_content_list() -> None:
    """Tier 2 (compat): pre-#383 callers pass ``text=`` + ``media=``.
    Result has list-of-parts content with text-part first, media after.
    """
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    m = ChatMessage(role="user", text="see this", media=[block], ts="t1")
    assert isinstance(m.content, list)
    assert m.content == [{"type": "text", "text": "see this"}, block]


def test_legacy_media_only_no_text_folds_into_list() -> None:
    """Tier 2 (compat): media without text → content is list with just
    the media block(s)."""
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    m = ChatMessage(role="user", media=[block], ts="t1")
    assert m.content == [block]


def test_role_agent_renames_to_assistant_on_construct() -> None:
    """Tier 2: ``role="agent"`` is the pre-#383 spelling; constructor
    normalises it to ``"assistant"`` so no live code path sees the
    legacy value.
    """
    m = ChatMessage(role="agent", text="reply", ts="t1")
    assert m.role == "assistant"


# ── Read properties (= text / media) ───────────────────────────────────


def test_text_property_from_str_content() -> None:
    """Tier 2: m.text returns content directly when content is a str."""
    m = ChatMessage(role="user", content="hi", ts="t1")
    assert m.text == "hi"


def test_text_property_from_list_content_extracts_text_part() -> None:
    """Tier 2: m.text extracts the first text-typed part when content
    is a list-of-parts.
    """
    m = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "see this"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ],
        ts="t1",
    )
    assert m.text == "see this"


def test_text_property_empty_when_no_text_part() -> None:
    """Tier 2: list content with no text part → m.text returns empty str."""
    m = ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "data:..."}}],
        ts="t1",
    )
    assert m.text == ""


def test_media_property_returns_non_text_parts() -> None:
    """Tier 2: m.media returns the non-text content parts."""
    block = {"type": "image_url", "image_url": {"url": "data:..."}}
    m = ChatMessage(
        role="user",
        content=[{"type": "text", "text": "see this"}, block],
        ts="t1",
    )
    assert m.media == [block]


def test_media_property_empty_for_str_content() -> None:
    """Tier 2: when content is a plain str, m.media is empty."""
    m = ChatMessage(role="user", content="hi", ts="t1")
    assert m.media == []


# ── Migration ──────────────────────────────────────────────────────────


def test_migrate_legacy_agent_to_assistant() -> None:
    """Tier 2: legacy ``role: "agent"`` with ``text`` is rewritten to
    ``role: "assistant"`` with ``content``.
    """
    legacy = {"role": "agent", "text": "hello", "ts": "t1"}
    new = _migrate_legacy_chat_message(legacy)
    assert new["role"] == "assistant"
    assert new["content"] == "hello"
    assert "text" not in new


def test_migrate_legacy_user_text_only() -> None:
    """Tier 2: legacy user turn with text only → content=str."""
    legacy = {"role": "user", "text": "hi", "ts": "t1"}
    new = _migrate_legacy_chat_message(legacy)
    assert new["role"] == "user"
    assert new["content"] == "hi"
    assert "text" not in new


def test_migrate_legacy_user_with_media() -> None:
    """Tier 2: legacy user turn with text + media → content as list."""
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    legacy = {
        "role": "user", "text": "see", "media": [block], "ts": "t1",
    }
    new = _migrate_legacy_chat_message(legacy)
    assert new["content"] == [{"type": "text", "text": "see"}, block]
    assert "media" not in new
    assert "text" not in new


def test_migrate_already_new_shape_is_idempotent() -> None:
    """Tier 2: dicts already in the new shape pass through unchanged
    (with the one exception of role='agent' → 'assistant' normalisation).
    """
    new_input = {"role": "assistant", "content": "hi", "ts": "t1"}
    out = _migrate_legacy_chat_message(new_input)
    assert out == new_input


def test_migrate_new_shape_normalises_stale_agent_role() -> None:
    """Tier 2: even a dict already containing ``content`` gets its role
    normalised if it slipped in as ``agent`` (= belt-and-suspenders).
    """
    out = _migrate_legacy_chat_message(
        {"role": "agent", "content": "reply", "ts": "t1"}
    )
    assert out["role"] == "assistant"


# ── history.jsonl round-trip ───────────────────────────────────────────


def test_chat_message_round_trips_via_asdict_and_constructor() -> None:
    """Tier 2: asdict(m) → json.dumps → json.loads → ChatMessage(**dict)
    yields an equivalent message. Pins the persistence cycle so loaded
    history matches what was written.
    """
    tc = [{"id": "call_1", "type": "function",
           "function": {"name": "f", "arguments": "{}"}}]
    original = ChatMessage(
        role="assistant", content="ack", ts="t1",
        tool_calls=tc, meta={"chain_id": "abc"},
    )
    raw = json.loads(json.dumps(asdict(original)))
    reloaded = ChatMessage(**raw)
    assert reloaded.role == original.role
    assert reloaded.content == original.content
    assert reloaded.tool_calls == original.tool_calls
    assert reloaded.meta == original.meta


def test_load_history_migrates_legacy_lines(tmp_path: Path) -> None:
    """Tier 2: ChatSession.load_history rewrites pre-#383 entries on read
    (= the on-disk file stays in the old shape until the next append, but
    the in-memory ``self.history`` carries the migrated shape).
    """
    # Build a minimal Session-like object that exercises load_history's
    # file-reading code path without booting the full ChatSession.
    from reyn.chat.session import ChatSession

    session = ChatSession.__new__(ChatSession)  # bypass __init__
    session.history_path = tmp_path / "history.jsonl"
    session.history = []
    session._next_seq = 1  # touched by post-load init; safe default

    legacy_lines = [
        {"role": "user", "text": "hi", "ts": "t1"},
        {"role": "agent", "text": "hello", "ts": "t2",
         "meta": {"chain_id": "abc"}},
    ]
    session.history_path.write_text(
        "\n".join(json.dumps(line) for line in legacy_lines) + "\n",
        encoding="utf-8",
    )
    session.load_history()

    assert len(session.history) == 2
    assert session.history[0].role == "user"
    assert session.history[0].content == "hi"
    assert session.history[1].role == "assistant"  # renamed from "agent"
    assert session.history[1].content == "hello"
    assert session.history[1].meta == {"chain_id": "abc"}
