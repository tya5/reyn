"""Tier 1: ``reyn.chainlit_app.history`` contract.

Pinned invariants:

1. ``user`` / ``assistant`` roles land in the output; chainlit-side
   author labels match the live outbox adapter's mapping.
2. ``tool`` / ``system`` / ``summary`` / ``skill_event`` roles are
   dropped (= LLM-wire / Reyn-internal markers that don't belong in
   the chat thread).
3. Unknown role (= future ChatMessage role addition) is dropped, not
   rendered with a fallback author — same conservative posture as
   the ``_DROPPED_ROLES`` set so a new role surfaces as a silent
   skip until explicitly wired.
4. Multimodal ``content`` (= ``list[dict]`` of parts) flattens to
   text parts plus ``[image: <name>]`` markers for image parts.
5. Empty-content turns are dropped so the replay doesn't emit blank
   message cells.
6. Order preserved (= chronological replay).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reyn.chainlit_app.history import HistoryEntry, history_to_chainlit


@dataclass
class _FakeMsg:
    role: str
    content: "str | list[dict]" = ""
    meta: dict = field(default_factory=dict)


def test_user_and_assistant_roles_pass_through():
    """Tier 1: user → author="user", assistant → author="agent"."""
    out = history_to_chainlit([
        _FakeMsg(role="user", content="hi"),
        _FakeMsg(role="assistant", content="hello"),
    ])
    assert out == [
        HistoryEntry(author="user", content="hi"),
        HistoryEntry(author="agent", content="hello"),
    ]


def test_internal_roles_dropped():
    """Tier 1: tool / system / summary / skill_event filtered out."""
    out = history_to_chainlit([
        _FakeMsg(role="tool", content="tool result"),
        _FakeMsg(role="system", content="system prompt"),
        _FakeMsg(role="summary", content="compaction summary"),
        _FakeMsg(role="skill_event", content="skill marker"),
    ])
    assert out == []


def test_unknown_role_dropped_not_fallback():
    """Tier 1: unrecognised role (= future ChatMessage extension)
    does NOT render with a fallback author — drops silently so the
    addition surfaces as a deliberate wiring task."""
    out = history_to_chainlit([
        _FakeMsg(role="future_role", content="surprise"),
    ])
    assert out == []


def test_multimodal_list_content_extracts_text_parts():
    """Tier 1: list-of-parts content → text concatenated by newline,
    image parts → ``[image: <name>]`` markers."""
    out = history_to_chainlit([
        _FakeMsg(role="user", content=[
            {"type": "text", "text": "look at this"},
            {"type": "image", "path": "/tmp/shot.png", "mime_type": "image/png"},
            {"type": "text", "text": "what is it?"},
        ]),
    ])
    assert len(out) == 1
    assert out[0].author == "user"
    assert out[0].content == "look at this\n[image: shot.png]\nwhat is it?"


def test_multimodal_image_without_path_uses_generic_marker():
    """Tier 1: image part with empty path → ``[image]`` (= no name)."""
    out = history_to_chainlit([
        _FakeMsg(role="user", content=[
            {"type": "image", "path": "", "mime_type": "image/png"},
        ]),
    ])
    assert out == [HistoryEntry(author="user", content="[image: image]")]


def test_image_url_part_renders_as_image_marker():
    """Tier 1: ``image_url`` part (= data-URL form) → ``[image]`` marker."""
    out = history_to_chainlit([
        _FakeMsg(role="user", content=[
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]),
    ])
    assert out == [HistoryEntry(author="user", content="[image]")]


def test_empty_content_dropped():
    """Tier 1: turns whose flattened content is empty are dropped so
    blank cells don't show up on replay."""
    out = history_to_chainlit([
        _FakeMsg(role="user", content=""),
        _FakeMsg(role="assistant", content=""),
        _FakeMsg(role="user", content=[]),
        _FakeMsg(role="assistant", content=[{"type": "unknown_kind"}]),
    ])
    assert out == []


def test_chronological_order_preserved():
    """Tier 1: order in input → order in output (= the operator sees
    the conversation in the same sequence they had it)."""
    out = history_to_chainlit([
        _FakeMsg(role="user", content="1"),
        _FakeMsg(role="assistant", content="2"),
        _FakeMsg(role="user", content="3"),
        _FakeMsg(role="assistant", content="4"),
    ])
    assert [e.content for e in out] == ["1", "2", "3", "4"]
    assert [e.author for e in out] == ["user", "agent", "user", "agent"]


def test_empty_history_returns_empty_list():
    """Tier 1: no history (= fresh agent) → ``[]`` (= no replay frames)."""
    assert history_to_chainlit([]) == []


def test_mixed_visible_and_internal_roles_filters_correctly():
    """Tier 1: realistic mixed history — only user/assistant survive
    in their original order."""
    out = history_to_chainlit([
        _FakeMsg(role="system", content="boot"),
        _FakeMsg(role="user", content="q1"),
        _FakeMsg(role="assistant", content="a1"),
        _FakeMsg(role="tool", content="tool resp"),
        _FakeMsg(role="assistant", content="a1b"),
        _FakeMsg(role="user", content="q2"),
        _FakeMsg(role="summary", content="compaction"),
        _FakeMsg(role="assistant", content="a2"),
    ])
    assert [e.content for e in out] == ["q1", "a1", "a1b", "q2", "a2"]
