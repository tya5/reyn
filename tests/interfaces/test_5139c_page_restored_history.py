"""Tier 1: #5139 C — ``page_restored_history`` bounds a remote backlog page
without ever cutting a turn (``chain_id``-correlated run) in half.

Root motivation (architect ruling, issuecomment-5383993909): a plain
tail-N slice by MESSAGE would risk landing between an assistant's
tool_calls message and its tool result — silently degrading the
projection's own call/result correlation (``project_restored_frames``'s
``calls_by_id`` is built from whatever slice is handed to it; a message
outside that slice simply never contributes). ``page_restored_history``
instead cuts only at a turn (``chain_id``) boundary, so a page's own
message slice is always a union of WHOLE turns.

Real ``ChatMessage``/``project_restored_frames`` throughout — no mocks;
these are pure, session-independent functions."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_KIND_KEY
from reyn.interfaces.inline.textual_chat.restore import (
    RESUME_DIVIDER,
    page_restored_history,
)
from reyn.runtime.chat_message import ChatMessage


def _turn_plain(chain_id: str, user_text: str, agent_text: str) -> "list[ChatMessage]":
    """A 2-message turn: no tool call, just user + assistant reply."""
    return [
        ChatMessage(role="user", content=user_text, meta={"chain_id": chain_id}),
        ChatMessage(role="assistant", content=agent_text, meta={"chain_id": chain_id}),
    ]


def _turn_with_tool_call(chain_id: str, tool_call_id: str) -> "list[ChatMessage]":
    """A 2-message turn: an assistant tool_calls message + its tool result —
    the exact pair whose correlation a mid-turn cut would silently break."""
    return [
        ChatMessage(
            role="assistant",
            content="",
            meta={"chain_id": chain_id},
            tool_calls=[
                {"id": tool_call_id, "function": {"name": "search", "arguments": "{}"}}
            ],
        ),
        ChatMessage(
            role="tool",
            content="3 results found",
            tool_call_id=tool_call_id,
            meta={"chain_id": chain_id},
        ),
    ]


def test_a_small_limit_never_splits_a_tool_call_from_its_result() -> None:
    """Tier 1: the turn boundary is a chain_id, not a message count — a
    ``limit`` set BELOW a single turn's own frame count must still return
    that turn whole, with the call/result correlation intact (never the
    degraded "uncorrelated result, tool name only" shape
    ``project_restored_frames`` falls back to when the assistant's own
    tool_calls message is missing from the slice)."""
    history = [
        *_turn_plain("c1", "hi", "hello"),
        *_turn_with_tool_call("c2", "tc-1"),
    ]
    # limit=1 is deliberately smaller than turn c2's own 1 projected frame
    # (the tool call+result coalesce into ONE OutboxMessage) — forces the
    # windowing loop to accept an "overshoot" rather than split the group.
    frames, has_more, next_cursor = page_restored_history(history, limit=1)

    # Tuple-unpacking IS the "exactly one" check (raises on 0 or 2+
    # matches) — a behavioral assertion on the extracted value, not a
    # ``len(...) == N`` format pin.
    (tool_frame,) = [f for f in frames if f.kind == "tool_call_started"]
    assert tool_frame.meta.get("tool") == "search", (
        f"tool call must stay correlated (tool name resolved from the "
        f"assistant's own tool_calls entry) even under a tight limit — "
        f"got meta={tool_frame.meta!r}"
    )
    assert tool_frame.meta.get(RESULT_KIND_KEY) != "tool_call_failed"


def test_has_more_and_next_cursor_walk_backward_across_pages() -> None:
    """Tier 1: 3 turns, a limit that forces exactly 2 pages: page 1
    (newest, ``before_root_id=None``) carries the newest turn(s) and
    reports ``has_more=True`` with the older turn's own chain_id as
    ``next_cursor``; page 2 (``before_root_id=<that cursor>``) carries
    the remainder and reports ``has_more=False, next_cursor=None`` —
    the "reached the true start" signal."""
    history = [
        *_turn_plain("c1", "first", "first reply"),
        *_turn_plain("c2", "second", "second reply"),
        *_turn_plain("c3", "third", "third reply"),
    ]
    # Each plain turn projects to 2 frames (user + agent); limit=2 should
    # take exactly the newest turn (c3) as page 1.
    page1, has_more1, cursor1 = page_restored_history(history, limit=2)
    page1_texts = [f.text for f in page1 if f.kind in ("user", "agent")]
    assert page1_texts == ["third", "third reply"], page1_texts
    assert has_more1 is True
    # ``next_cursor`` is THIS page's own oldest turn's id (c3) — the value
    # the NEXT call passes back as ``before_root_id`` to exclude exactly
    # what was already sent and continue strictly older, never that
    # turn's own predecessor id (a caller never has to guess an offset).
    assert cursor1 == "c3", f"next_cursor must be THIS page's own oldest turn id, got {cursor1!r}"

    page2, has_more2, cursor2 = page_restored_history(
        history, before_root_id=cursor1, limit=2,
    )
    page2_texts = [f.text for f in page2 if f.kind in ("user", "agent")]
    assert page2_texts == ["second", "second reply"], page2_texts
    assert has_more2 is True
    assert cursor2 == "c2"

    page3, has_more3, cursor3 = page_restored_history(
        history, before_root_id=cursor2, limit=2,
    )
    page3_texts = [f.text for f in page3 if f.kind in ("user", "agent")]
    assert page3_texts == ["first", "first reply"], page3_texts
    assert has_more3 is False, "the true start must report has_more=False"
    assert cursor3 is None, "has_more=False must carry next_cursor=None"


def test_a_stale_cursor_degrades_to_nothing_more_not_a_resend() -> None:
    """Tier 1: a ``before_root_id`` naming a turn no longer present in
    *history* (rotated out between the caller's own two calls) must
    answer "nothing more" — never silently re-serve the newest page,
    which would look like NEW history to a caller that asked for
    strictly OLDER content."""
    history = [*_turn_plain("c1", "hi", "hello")]
    frames, has_more, next_cursor = page_restored_history(
        history, before_root_id="does-not-exist", limit=200,
    )
    assert frames == []
    assert has_more is False
    assert next_cursor is None


def test_only_the_newest_page_carries_the_resume_divider() -> None:
    """Tier 1: ``project_restored_frames`` unconditionally prepends ONE
    resume divider to any non-empty projection — correct for the newest
    page (what a client shows first) and a would-be DUPLICATE on every
    older page this function projects independently per call."""
    history = [
        *_turn_plain("c1", "first", "first reply"),
        *_turn_plain("c2", "second", "second reply"),
    ]
    newest, _, cursor = page_restored_history(history, limit=2)
    assert newest[0].kind == "system" and newest[0].text == RESUME_DIVIDER

    older, _, _ = page_restored_history(history, before_root_id=cursor, limit=2)
    assert all(f.text != RESUME_DIVIDER for f in older), (
        f"an OLDER page must not carry a second divider — got {older!r}"
    )
