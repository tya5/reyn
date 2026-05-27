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
    assert [e.author for e in out] == ["user"]
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


# ── cap behavior ──────────────────────────────────────────────────────────


def _seq_history(n: int) -> list:
    """Build ``n`` alternating user/assistant turns numbered 0..n-1."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(_FakeMsg(role=role, content=f"turn{i}"))
    return out


_FULL_TEN_CONTENT = [f"turn{i}" for i in range(10)]


def test_cap_none_returns_full_history():
    """Tier 1: cap=None (default) → no truncation, no marker, all entries."""
    out = history_to_chainlit(_seq_history(10))
    assert [e.content for e in out] == _FULL_TEN_CONTENT


def test_cap_zero_treated_as_unlimited():
    """Tier 1: cap=0 → unlimited (= the env-var "show all" sentinel)."""
    out = history_to_chainlit(_seq_history(10), cap=0)
    assert [e.content for e in out] == _FULL_TEN_CONTENT


def test_cap_negative_treated_as_unlimited():
    """Tier 1: cap=-1 → unlimited (= defensive, same as cap=0)."""
    out = history_to_chainlit(_seq_history(10), cap=-1)
    assert [e.content for e in out] == _FULL_TEN_CONTENT


def test_cap_larger_than_history_no_marker():
    """Tier 1: cap=100 on 10-entry history → no truncation, no marker."""
    out = history_to_chainlit(_seq_history(10), cap=100)
    assert [e.content for e in out] == _FULL_TEN_CONTENT


def test_cap_equal_to_history_no_marker():
    """Tier 1: cap=10 on 10-entry history → exact fit, no marker."""
    out = history_to_chainlit(_seq_history(10), cap=10)
    assert [e.content for e in out] == _FULL_TEN_CONTENT


def test_cap_slices_to_last_n_with_marker():
    """Tier 1: cap=3 on 10-entry history → 1 system marker + last 3 entries.

    seq alternates user/assistant by index parity; assistant maps to
    author "agent" via ``_AUTHOR_BY_ROLE``, so the kept tail (i=7,8,9)
    renders as agent / user / agent."""
    out = history_to_chainlit(_seq_history(10), cap=3)
    assert [e.author for e in out] == ["system", "agent", "user", "agent"]
    assert "7" in out[0].content  # 10 - 3 = 7 omitted
    assert [e.content for e in out[1:]] == ["turn7", "turn8", "turn9"]


def test_cap_marker_mentions_env_var_for_unbounded():
    """Tier 1: marker text tells the operator how to opt back into full
    replay (= avoids "where did my history go?" without a hint)."""
    out = history_to_chainlit(_seq_history(100), cap=10)
    assert out[0].author == "system"
    assert "REYN_CHAINLIT_HISTORY_CAP" in out[0].content


def test_cap_applied_after_filtering():
    """Tier 1: cap counts *visible* entries, not raw history length.
    A history of 100 internal entries + 5 visible should never trigger
    the marker for cap=10 (= visible < cap)."""
    msgs = []
    for i in range(100):
        msgs.append(_FakeMsg(role="tool", content=f"tool{i}"))
    msgs.extend([
        _FakeMsg(role="user", content="visible1"),
        _FakeMsg(role="assistant", content="visible2"),
    ])
    out = history_to_chainlit(msgs, cap=10)
    assert [e.content for e in out] == ["visible1", "visible2"]
    assert [e.author for e in out] == ["user", "agent"]
