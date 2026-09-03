"""Tier 2: chat compaction is tool-aware (issue #383 PR-E2).

Post-PR-E1, history.jsonl contains assistant entries with ``tool_calls``
and ``role="tool"`` response entries. PR-E2 makes the compaction
controller:

  - Include tool turns in its candidate selection (= the filter no
    longer drops ``role="tool"`` / ``role="assistant"``).
  - Serialise each turn into a compactor-input dict that surfaces
    structured tool detail (``tool_calls`` summary + ``tool_call_id``
    + ``tool_name``).

PR-N3: the phase prompt is now a string constant in
``reyn.services.compaction.engine._COMPACTION_SYSTEM_PROMPT`` (skill retired).
"""
from __future__ import annotations

from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import (
    _turn_to_compactor_input,
)

# ── _turn_to_compactor_input ──────────────────────────────────────────


def test_user_turn_minimal_shape() -> None:
    """Tier 2: plain user turn → {role, text, seq} (= no tool fields)."""
    m = ChatMessage(role="user", content="hi", ts="t1", seq=1)
    out = _turn_to_compactor_input(m)
    assert out == {"role": "user", "text": "hi", "seq": 1}


def test_assistant_text_only_no_tool_fields() -> None:
    """Tier 2: assistant final-text turn (= no tool_calls) → no tool fields."""
    m = ChatMessage(role="assistant", content="here's the answer", ts="t1", seq=2)
    out = _turn_to_compactor_input(m)
    assert out == {"role": "assistant", "text": "here's the answer", "seq": 2}


def test_assistant_with_tool_calls_emits_compact_summary() -> None:
    """Tier 2: assistant turn that emitted tool_calls → output carries
    a ``tool_calls`` list with ``{name, args_chars}`` per call (= compact
    form, not full arg JSON, so the compactor input stays small).
    """
    m = ChatMessage(
        role="assistant", content="checking", ts="t1", seq=3,
        tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "file_read",
                          "arguments": '{"path": "src/a.py"}'}},
            {"id": "c2", "type": "function",
             "function": {"name": "web_fetch",
                          "arguments": '{"url": "https://example.com"}'}},
        ],
    )
    out = _turn_to_compactor_input(m)
    assert out["role"] == "assistant"
    assert out["text"] == "checking"
    assert out["tool_calls"] == [
        {"name": "file_read", "args_chars": len('{"path": "src/a.py"}')},
        {"name": "web_fetch", "args_chars": len('{"url": "https://example.com"}')},
    ]


def test_tool_response_carries_id_and_name() -> None:
    """Tier 2: tool response turn → output includes ``tool_call_id`` + ``tool_name``."""
    m = ChatMessage(
        role="tool", content='{"contents": "..."}', ts="t1", seq=4,
        tool_call_id="c1", name="file_read",
    )
    out = _turn_to_compactor_input(m)
    assert out["role"] == "tool"
    assert out["tool_call_id"] == "c1"
    assert out["tool_name"] == "file_read"


def test_helper_ignores_malformed_tool_call_entries() -> None:
    """Tier 2: a non-dict entry in tool_calls is skipped (= defensive)."""
    m = ChatMessage(
        role="assistant", content="", ts="t1", seq=5,
        tool_calls=[
            "not-a-dict",  # type: ignore[list-item]
            {"id": "c1", "function": {"name": "f", "arguments": "{}"}},
        ],
    )
    out = _turn_to_compactor_input(m)
    assert out["tool_calls"] == [{"name": "f", "args_chars": 2}]


# ── compaction filter (= candidate selection includes tool turns) ─────


def test_compaction_filter_includes_tool_role() -> None:
    """Tier 2: the role filter ``force_compact_now``'s own candidate
    selection uses admits tool turns.

    #5699: the literal role tuple this test used to pin via source-text
    (a brittle string check the #5699 refactor legitimately moved OUT of
    ``compaction_controller.py`` — the candidate filter now calls the ONE
    shared predicate, ``chat_message.is_compaction_eligible``, so a
    string search over ``compaction_controller``'s own source no longer
    finds the literal at all) is replaced by pinning the BEHAVIOUR
    directly against the real predicate every call site imports — the
    same one whose own source (``chat_message.py``) still names the tool
    + assistant roles the pre-PR-E2 filter used to drop."""
    from reyn.runtime.chat_message import ChatMessage, is_compaction_eligible

    assert is_compaction_eligible(ChatMessage(role="tool", content="t", ts="t1"))
    assert is_compaction_eligible(ChatMessage(role="assistant", content="a", ts="t1"))
    assert is_compaction_eligible(ChatMessage(role="user", content="u", ts="t1"))


def test_compaction_system_prompt_mentions_tool_calls() -> None:
    """Tier 2: the OS-internal compaction system prompt explicitly mentions
    tool-derived items so the LLM knows to surface them in artifacts_referenced.

    PR-N3: prompt moved from phases/compact.md to
    reyn.services.compaction.engine._COMPACTION_SYSTEM_PROMPT.
    """
    from reyn.services.compaction.engine import _COMPACTION_SYSTEM_PROMPT

    # #4951-B: this test's own name/docstring is about tool-derived items ->
    # artifacts_referenced — the new_turn_seqs assert that used to sit here
    # was unrelated to that (and is now doubly wrong: the key it named is
    # intentionally removed from the prompt). See test_4883_compaction_
    # schema_validation.py for schema-required-keys coverage instead.
    assert "artifacts_referenced" in _COMPACTION_SYSTEM_PROMPT
