"""Tier 2: chat compaction is tool-aware (issue #383 PR-E2).

Post-PR-E1, history.jsonl contains assistant entries with ``tool_calls``
and ``role="tool"`` response entries. PR-E2 makes the compaction
controller:

  - Include tool turns in its candidate selection (= the filter no
    longer drops ``role="tool"`` / ``role="assistant"``).
  - Serialise each turn into a compactor-skill-input dict that
    surfaces structured tool detail (``tool_calls`` summary +
    ``tool_call_id`` + ``tool_name``).

The compactor skill's phase prompt is also updated to mention tool
turns + add an ``artifacts_referenced`` rule for tool-derived items;
that's documented in ``src/reyn/stdlib/skills/chat_compactor/phases/compact.md``
(not directly testable without an LLM call).
"""
from __future__ import annotations

from reyn.chat.services.compaction_controller import (
    _turn_to_compactor_input,
)
from reyn.chat.session import ChatMessage

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
    """Tier 2: the new role filter in ``_maybe_compact`` admits tool turns.

    Pin via source-text invariant since the filter is a literal tuple
    inside the method body. The string check guards against accidental
    revert to the pre-PR-E2 ``("user", "agent")``-only filter.
    """
    import inspect

    from reyn.chat.services import compaction_controller
    src = inspect.getsource(compaction_controller)
    assert '"user", "assistant", "tool", "agent"' in src, (
        "compaction candidate filter must include tool + assistant roles "
        "post-#383 PR-E2"
    )


def test_compaction_phase_prompt_mentions_tool_calls() -> None:
    """Tier 2: the compactor phase prompt explicitly mentions tool turns +
    the artifacts_referenced rule for tool-derived items.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    phase = (
        repo_root
        / "src/reyn/stdlib/skills/chat_compactor/phases/compact.md"
    ).read_text(encoding="utf-8")

    # Phase prompt acknowledges tool_calls + tool role in inputs.
    assert "tool_calls" in phase
    assert "`tool`" in phase or "role.*tool" in phase
    # artifacts_referenced rule explicitly covers tool-derived items.
    assert "tool activity" in phase or "Tool activity" in phase
    assert "web_fetch" in phase  # at least one canonical example
