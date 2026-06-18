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
    """Tier 2: the role filter in ``force_compact_now`` admits tool turns.

    Pin via source-text invariant since the filter is a literal tuple
    inside the method body. The string check guards against accidental
    revert to the pre-PR-E2 ``("user", "agent")``-only filter. (#1128 PR-a:
    the former ``_maybe_compact`` background path was removed; the same
    candidate role filter lives in the surviving ``force_compact_now``.)
    """
    import inspect

    from reyn.runtime.services import compaction_controller
    src = inspect.getsource(compaction_controller)
    assert '"user", "assistant", "tool", "agent"' in src, (
        "compaction candidate filter must include tool + assistant roles "
        "post-#383 PR-E2"
    )


def test_compaction_system_prompt_mentions_tool_calls() -> None:
    """Tier 2: the OS-internal compaction system prompt explicitly mentions
    tool-derived items so the LLM knows to surface them in artifacts_referenced.

    PR-N3: prompt moved from phases/compact.md to
    reyn.services.compaction.engine._COMPACTION_SYSTEM_PROMPT.
    """
    from reyn.services.compaction.engine import _COMPACTION_SYSTEM_PROMPT

    assert "new_turn_seqs" in _COMPACTION_SYSTEM_PROMPT, (
        "prompt must mention new_turn_seqs so LLM copies the verbatim seq list"
    )
    assert "artifacts_referenced" in _COMPACTION_SYSTEM_PROMPT
