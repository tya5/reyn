"""Tier 2: ChatSession dispatch attribution — FP-0041 (#489) PR-A.

Humanic multi-consumer model foundation: when a different consumer
addresses the agent on the same session inbox (= sender transition),
a ``state_change`` history entry is injected before the new turn so
the LLM reads ``[context shift] Now responding to <X>. Previous turn
was from <Y>.`` instead of seeing a confused linear feed.

Pins:

  1. Payload with ``sender`` triggers transition detection on the
     first attributed turn (= no prior sender, "first attributed
     turn this session" wording).
  2. Sender transition (= different sender than prior turn) emits a
     state_change history entry with both labels.
  3. Same sender consecutive turns do NOT emit state_change (= no
     transition, normal within-conversation flow).
  4. Payload without ``sender`` field is dispatched unchanged (=
     backward compat for existing producers).
  5. Non-dict payload doesn't crash (= defensive).
  6. ``_last_sender`` tracks the last attributed sender across turns.

Plus ``_format_sender_label`` helper coverage for the documented
sender shapes (= ``user:tui`` / ``slack:U456:bob`` / ``cron:job`` /
``a2a:peer`` / unknown transport fall-through).

Tier 2 because the attribution is load-bearing for the humanic model
— a regression that dropped sender transitions silently regresses
multi-consumer agent UX to the "confused linear feed" failure mode.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.session import (
    ChatMessage,
    ChatSession,
    _format_sender_label,
)
from reyn.core.events.state_log import StateLog


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _state_changes(session: ChatSession) -> list[ChatMessage]:
    return [
        m for m in session.history
        if m.role == "system" and (m.meta or {}).get("kind") == "state_change"
    ]


def _attribution_entries(session: ChatSession) -> list[ChatMessage]:
    """state_change entries minted specifically by dispatch attribution."""
    return [
        m for m in _state_changes(session)
        if (m.meta or {}).get("source") == "dispatch_attribution"
    ]


# ── first attributed turn ──────────────────────────────────────────────


def test_first_sender_triggers_first_attributed_turn_entry(tmp_path):
    """Tier 2: the first inbox payload that carries a ``sender``
    triggers a state_change entry with "first attributed turn this
    session" wording (= prior sender is None, so the "previous turn
    was from ..." framing doesn't apply).
    """
    session = _make_session(tmp_path)
    assert session.last_sender() is None

    session._handle_sender_attribution({"sender": "slack:U456:bob"})

    entries = _attribution_entries(session)
    assert entries, "expected attribution entry"
    (entry,) = entries
    assert "bob (Slack)" in entry.content
    assert "first attributed turn" in entry.content
    assert session.last_sender() == "slack:U456:bob"


# ── sender transition ────────────────────────────────────────────────


def test_sender_transition_emits_state_change(tmp_path):
    """Tier 2: when the new sender differs from ``_last_sender``, a
    state_change history entry is minted with both old and new labels,
    so the LLM reads "[context shift] Now responding to X. Previous
    turn was from Y."
    """
    session = _make_session(tmp_path)
    session._handle_sender_attribution({"sender": "cron:morning_news"})
    pre_count = len(_attribution_entries(session))

    session._handle_sender_attribution({"sender": "slack:U456:bob"})

    entries = _attribution_entries(session)
    assert len(entries) == pre_count + 1
    last = entries[-1].content
    assert "bob (Slack)" in last
    assert "morning_news" in last  # previous = cron job
    assert "Previous turn was from" in last
    assert session.last_sender() == "slack:U456:bob"


# ── same sender = no transition ────────────────────────────────────────


def test_same_sender_consecutive_does_not_emit(tmp_path):
    """Tier 2: consecutive turns from the same sender (= normal
    within-conversation flow) do NOT emit state_change. Attribution
    only fires on the boundary between consumers.
    """
    session = _make_session(tmp_path)
    session._handle_sender_attribution({"sender": "user:tui"})
    pre_count = len(_attribution_entries(session))

    # Same sender again — no new transition.
    session._handle_sender_attribution({"sender": "user:tui"})
    session._handle_sender_attribution({"sender": "user:tui"})

    assert len(_attribution_entries(session)) == pre_count
    assert session.last_sender() == "user:tui"


# ── backward compat: no sender ─────────────────────────────────────────


def test_payload_without_sender_skips_attribution(tmp_path):
    """Tier 2: existing inbox producers that haven't adopted the
    ``sender`` envelope convention still work. ``_handle_sender_attribution``
    doesn't crash, doesn't mint a state_change, doesn't change
    ``_last_sender``.
    """
    session = _make_session(tmp_path)
    pre_count = len(_attribution_entries(session))
    pre_last = session.last_sender()

    session._handle_sender_attribution({"text": "hello", "chain_id": "c1"})

    assert len(_attribution_entries(session)) == pre_count
    assert session.last_sender() == pre_last  # unchanged


def test_empty_sender_string_skips_attribution(tmp_path):
    """Tier 2: ``sender=""`` is treated as absent (= no transition
    fired). Prevents accidental empty-string emissions from
    producers that haven't fully populated the field.
    """
    session = _make_session(tmp_path)
    session._handle_sender_attribution({"sender": ""})

    assert len(_attribution_entries(session)) == 0
    assert session.last_sender() is None


# ── defensive: non-dict payload ────────────────────────────────────────


def test_non_dict_payload_does_not_crash(tmp_path):
    """Tier 2: ``_handle_sender_attribution`` is called with raw
    payloads from the inbox; if a non-dict slips in (= producer bug),
    the attribution helper silently returns without crashing
    dispatch. Defensive isolation.
    """
    session = _make_session(tmp_path)
    session._handle_sender_attribution(None)
    session._handle_sender_attribution("string-not-dict")
    session._handle_sender_attribution(["list-not-dict"])
    session._handle_sender_attribution(42)

    assert len(_attribution_entries(session)) == 0
    assert session.last_sender() is None


# ── 3-way transition ──────────────────────────────────────────────────


def test_three_way_transition_each_fires(tmp_path):
    """Tier 2: three distinct senders in sequence fire 3 separate
    state_change entries — one per boundary.
    """
    session = _make_session(tmp_path)
    session._handle_sender_attribution({"sender": "user:tui"})
    session._handle_sender_attribution({"sender": "cron:nightly"})
    session._handle_sender_attribution({"sender": "a2a:peer_one"})

    entries = _attribution_entries(session)
    e0, e1, e2 = entries
    # Final state reflects the latest sender.
    assert session.last_sender() == "a2a:peer_one"
    # Each entry mentions the current sender's label.
    assert "user (TUI)" in e0.content
    assert "nightly" in e1.content
    assert "peer_one" in e2.content


# ── _format_sender_label coverage ──────────────────────────────────────


@pytest.mark.parametrize("sender, expected_fragment", [
    ("slack:U456:bob", "bob (Slack)"),
    ("slack:U456", "slack user U456"),
    ("line:U789:alice", "alice (LINE)"),
    ("line:U789", "line user U789"),
    ("cron:morning_news", "scheduled cron job 'morning_news'"),
    ("a2a:news_agent", "peer agent 'news_agent'"),
    ("user:tui", "user (TUI)"),
    ("user:web", "user (WEB)"),
    ("user:cli", "user (CLI)"),
    ("user", "user"),
    ("webhook:github", "external webhook (github)"),
    # Fall-through: unknown transport returns raw.
    ("unknown_transport:foo:bar", "unknown_transport:foo:bar"),
    # Edge: None → "an unknown sender".
    (None, "an unknown sender"),
])
def test_format_sender_label_known_shapes(sender, expected_fragment):
    """Tier 2: parametrised coverage of the documented sender shapes.
    Pins the human-readable labels that appear in attribution
    state_change entries.
    """
    result = _format_sender_label(sender)
    assert expected_fragment in result, (
        f"sender={sender!r} expected to contain {expected_fragment!r}, "
        f"got {result!r}"
    )


def test_format_sender_label_empty_string_falls_through():
    """Tier 2: empty string falls through to itself (= no crash, no
    spurious label).
    """
    assert _format_sender_label("") == ""


# ── attribution survives notify_state_change failure ──────────────────


def test_attribution_resilient_to_notify_state_change_failure(
    tmp_path, monkeypatch,
):
    """Tier 2: if ``notify_state_change`` raises (= subscriber bug /
    events log misconfig), the dispatch attribution helper swallows
    the exception so the inbox dispatch path doesn't crash. Defensive
    — observability must not break core dispatch.

    ``_last_sender`` IS still updated (= we recorded the transition
    even though the audit emission failed).
    """
    session = _make_session(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated subscriber failure")

    monkeypatch.setattr(session, "notify_state_change", _boom)

    # Must not raise.
    session._handle_sender_attribution({"sender": "slack:U456:bob"})

    # _last_sender did update despite the failure.
    assert session.last_sender() == "slack:U456:bob"
