"""Tier 1/2: #4975 (architect ruling, issuecomment-5384508845, correcting
an earlier "messages" left-operand that named a knob which does not
exist) — ``llm_request_error``'s ``provider_body``/``provider_response``
fields are gated by a LATTICE-MEET, not a single flag: showing them
requires ALL 3 of #4666's own content opt-ins
(``agent_delta_include_text`` / ``completed_response_include_text`` /
``user_input_include_text``) AND this kind's own opt-in
(``provider_body_include_text``) — the narrowest participant wins, same
"compose_resolved is a lattice-meet (∩ allow, ∪ deny)" idiom this repo's
permission resolution already uses.

Root cause: a provider's own 4xx/5xx error response can quote back
content from the request it rejected, but reyn does not choose the shape
of a provider's own error body, so it cannot tell in advance WHICH of the
3 #4666 content classes a given quote would be — any of the 3 could be
the one that leaks, so an operator who has opted out of even ONE of them
must not be handed a blob that might contain it.

Real ``LocalEventBackend`` + a minimal real ``EventStoreLike`` (mirrors
``tests/core/test_4666_2_completed_response_opt_in.py``'s own
``_RecordingStore``) — no mocks.
"""
from __future__ import annotations

import pytest

from reyn.core.events.backend import LocalEventBackend
from reyn.schemas.models import Event


class _RecordingStore:
    def __init__(self) -> None:
        self.written: "list[Event]" = []

    def write(self, event: Event) -> None:
        self.written.append(event)


def _error_event(**extra) -> Event:
    return Event(
        type="llm_request_error",
        data={
            "error_type": "APIError",
            "error_message": "You exceeded your current quota",
            "status_code": 429,
            "provider_body": "the quick brown fox jumps over the lazy dog",
            "provider_response": "raw response text goes here too",
            **extra,
        },
    )


_ALL_FOUR = dict(
    agent_delta_include_text=True,
    completed_response_include_text=True,
    user_input_include_text=True,
    provider_body_include_text=True,
)


@pytest.mark.parametrize(
    "off_knob",
    [
        "agent_delta_include_text",
        "completed_response_include_text",
        "user_input_include_text",
        "provider_body_include_text",
    ],
)
def test_any_single_knob_off_hides_provider_body(off_knob: str) -> None:
    """Tier 1: acceptance① — with exactly ONE of the 4 participants off
    (the other 3 on), provider_body/provider_response must NOT appear —
    the meet's whole point (the narrowest participant wins), tested
    independently for each of the 4 knobs rather than only the
    all-off default."""
    kwargs = dict(_ALL_FOUR)
    kwargs[off_knob] = False
    store = _RecordingStore()
    backend = LocalEventBackend(store, **kwargs)

    backend.write(_error_event())

    data = store.written[0].data
    assert "provider_body" not in data, f"{off_knob}=False must hide provider_body"
    assert "provider_response" not in data, f"{off_knob}=False must hide provider_response"


def test_all_four_on_shows_provider_body_capped() -> None:
    """Tier 1: acceptance② — with all 4 on, the fields ARE shown, capped
    at ``provider_body_max_chars``."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, provider_body_max_chars=10, **_ALL_FOUR)

    backend.write(_error_event())

    data = store.written[0].data
    assert data["provider_body"] == "the quick "  # first 10 chars
    assert data["provider_body_truncated"] is True
    assert data["provider_response"] == "raw respon"
    assert data["provider_response_truncated"] is True


def test_all_four_on_short_body_is_not_marked_truncated() -> None:
    """Tier 1: acceptance② (the negative half) — a body genuinely under
    the cap is shown whole, with no truncated marker (so a caller can
    tell "capped" from "just short" — the two must not look the same)."""
    store = _RecordingStore()
    backend = LocalEventBackend(store, provider_body_max_chars=4000, **_ALL_FOUR)

    backend.write(_error_event())

    data = store.written[0].data
    assert data["provider_body"] == "the quick brown fox jumps over the lazy dog"
    assert "provider_body_truncated" not in data


def test_off_still_records_status_error_type_and_length() -> None:
    """Tier 1: acceptance③ — with the meet failing (all 4 knobs at their
    False default), error_type/status_code are still recorded
    UNCONDITIONALLY, and provider_body_length/provider_response_length
    are added so "a body existed but was not shown" is distinguishable
    from "there was none" — never silent (constitution's 2nd lens)."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)  # every #4975/#4666 knob at its False default

    backend.write(_error_event())

    data = store.written[0].data
    assert data["error_type"] == "APIError"
    assert data["status_code"] == 429
    assert data["error_message"] == "You exceeded your current quota"
    assert "provider_body" not in data
    assert "provider_response" not in data
    assert data["provider_body_length"] == len("the quick brown fox jumps over the lazy dog")
    assert data["provider_response_length"] == len("raw response text goes here too")


def test_a_missing_provider_body_records_no_length_either() -> None:
    """Tier 1: 'there was none' must stay distinguishable from 'there was
    one but it was hidden' — an error event with NO provider_body at all
    (litellm's own ``.body`` was ``None``) must not fabricate a
    ``provider_body_length`` key."""
    store = _RecordingStore()
    backend = LocalEventBackend(store)

    backend.write(Event(
        type="llm_request_error",
        data={"error_type": "APIError", "error_message": "boom", "status_code": 500},
    ))

    data = store.written[0].data
    assert "provider_body" not in data
    assert "provider_body_length" not in data
