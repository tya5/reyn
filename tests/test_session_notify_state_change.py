"""Tier 2: ``Session.notify_state_change`` — first-class state-change
history entries (#398 v4 frozen contract, 2026-05-22).

Pins the API + storage shape that #398 lands. The contract:

  - Storage: ``ChatMessage(role="system", content=summary, meta={"kind":
    "state_change", "source"?})`` — per user judgment "role はむやみに
    増やすべきでない、 system あるならそれで" (= Q1).
  - API: single ``notify_state_change(summary, source=None)`` method,
    no builder (= Q2).
  - Compaction: state_change entries are NOT consumed by
    chat_compactor (= per-event preservation, Q3).
  - Audit: no ``meta.event_log_seq`` back-link (= Q4).

These tests pin those four decisions plus a few practical adjacents:
- empty source omits the ``meta.source`` key (= minimal storage)
- multiple calls produce separate entries (= no implicit deduplication)
- observability event emitted for sub-task 6 measurement

Tier 2 because the contract is load-bearing: every Reyn module that
wants the LLM to see a world-state change calls this API; a regression
silently breaks the #352 trap fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> Session:
    """Build a minimal Session redirected to ``tmp_path``."""
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _state_change_entries(session: Session) -> list[ChatMessage]:
    """Return all history entries that look like state-change events."""
    return [
        m for m in session.history
        if m.role == "system" and (m.meta or {}).get("kind") == "state_change"
    ]


# ── API shape (= Q2 single-method, Q1 role=system) ────────────────────


def test_notify_state_change_appends_system_role_entry(tmp_path):
    """Tier 2: ``notify_state_change`` appends a ``role="system"``
    history entry — no new role values introduced per user Q1 judgment
    "むやみに増やすべきでない".
    """
    session = _make_session(tmp_path)
    pre_count = len(session.history)

    session.notify_state_change("Permission for mcp.sqlite was granted.")

    assert len(session.history) == pre_count + 1
    entry = session.history[-1]
    assert entry.role == "system"
    assert entry.content == "Permission for mcp.sqlite was granted."


def test_notify_state_change_meta_kind_state_change(tmp_path):
    """Tier 2: ``meta.kind == "state_change"`` lets downstream consumers
    (= TUI display, replay, future compactor) distinguish state-change
    entries from genuine system-prompt history without parsing the
    content text. ``meta`` is annotation, not a role — adding it
    doesn't violate Q1 "don't increase role values".
    """
    session = _make_session(tmp_path)
    session.notify_state_change("config updated")

    entry = session.history[-1]
    assert entry.meta is not None
    assert entry.meta.get("kind") == "state_change"


def test_notify_state_change_source_stored_when_provided(tmp_path):
    """Tier 2: emitter identity (= ``source``) lands in ``meta.source``
    for audit / debugging. Storage-only annotation; LLM-visible content
    is just the summary text.
    """
    session = _make_session(tmp_path)
    session.notify_state_change(
        "MCP server 'sqlite' was installed.",
        source="mcp_install_handler",
    )

    entry = session.history[-1]
    assert entry.meta.get("source") == "mcp_install_handler"


def test_notify_state_change_source_omitted_when_absent(tmp_path):
    """Tier 2: when ``source`` is None, ``meta.source`` is NOT set as
    null — the field is simply absent. Keeps the storage minimal and
    matches Reyn's existing convention of omitting empty fields.
    """
    session = _make_session(tmp_path)
    session.notify_state_change("something changed")

    entry = session.history[-1]
    assert "source" not in (entry.meta or {})
    # Other meta fields still present for state_change identification.
    assert entry.meta.get("kind") == "state_change"


# ── No audit cross-ref (= Q4) ──────────────────────────────────────────


def test_notify_state_change_no_event_log_seq_backlink(tmp_path):
    """Tier 2: ``meta.event_log_seq`` is NOT minted. Per
    the frozen design, events.jsonl already records the underlying
    state-change event; chat history stays minimal and decouples from
    the events log structure.
    """
    session = _make_session(tmp_path)
    session.notify_state_change("x", source="some_emitter")

    entry = session.history[-1]
    assert "event_log_seq" not in (entry.meta or {})


# ── Multiple emissions (= no implicit dedup / batching) ────────────────


def test_multiple_calls_each_produce_separate_entries(tmp_path):
    """Tier 2: N calls produce N separate history entries — no implicit
    deduplication or batching. Per Q2 "single method", batched emission
    is a deliberate Phase 2 consideration if measurement demands it,
    not a hidden default.
    """
    session = _make_session(tmp_path)
    session.notify_state_change("change 1")
    session.notify_state_change("change 2")
    session.notify_state_change("change 3", source="emitter")

    entries = _state_change_entries(session)
    assert [e.content for e in entries] == ["change 1", "change 2", "change 3"]


# ── Compactor preservation (= Q3 per-event) ────────────────────────────


def test_state_change_entries_not_in_compactor_candidates(tmp_path):
    """Tier 2: the chat_compactor candidate filter selects
    only user/agent turns. state_change entries (= role=system) are
    NEVER candidates, so per-event preservation is implicit. A
    regression that changed the filter to include system would
    silently start collapsing state-change events.

    Replicates the actual filter from
    ``CompactionController.force_compact_now`` (= ``turns = [m for m in
    history if m.role in ("user", "assistant", "tool", "agent")]``) and
    verifies the state_change entry doesn't appear in the result.
    """
    session = _make_session(tmp_path)
    # Append a mix of role types.
    session.notify_state_change("permission granted")
    session.history.append(ChatMessage(role="user", content="hi", seq=1))
    session.notify_state_change("config updated")
    session.history.append(ChatMessage(role="assistant", content="hello", seq=2))

    # Mirror the compactor's actual candidate filter.
    candidates = [
        m for m in session.history if m.role in ("user", "agent", "assistant")
    ]
    state_change_in_candidates = [
        m for m in candidates
        if (m.meta or {}).get("kind") == "state_change"
    ]
    assert state_change_in_candidates == [], (
        "state_change entry leaked into compactor candidate set"
    )


# ── Observability event (= sub-task 6 measurement foundation) ──────────


def test_notify_state_change_emits_observability_event(tmp_path):
    """Tier 2: each notify_state_change call emits a
    ``state_change_notified`` event on the session's chat_events log
    so #398 sub-task 6 measurement can count emission frequency by
    source without scraping chat history.
    """
    session = _make_session(tmp_path)
    captured: list = []
    session._chat_events.add_subscriber(captured.append)

    session.notify_state_change("perm granted", source="permission_manager")

    state_events = [ev for ev in captured if ev.type == "state_change_notified"]
    assert state_events, "expected at least one state_change_notified event"
    assert state_events[-1].data["summary"] == "perm granted"
    assert state_events[-1].data["source"] == "permission_manager"


# ── Persistence to history.jsonl ───────────────────────────────────────


def test_notify_state_change_persists_to_history_file(tmp_path, monkeypatch):
    """Tier 2: the entry is written to ``history.jsonl`` like any other
    ChatMessage so it survives session restart — Phase 1 (a)
    "Persistent until user delete" lifecycle from sub-task 5 applies
    uniformly (= no special filter for state_change at persistence
    time).

    Uses ``monkeypatch.chdir(tmp_path)`` so the session's history file
    lands under the temporary project root rather than the real
    working directory (which would pick up unrelated stale entries
    from prior test / dev runs).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    # Capture the seq + content of the entry we're about to mint so the
    # test can find it deterministically even if history file already
    # has other entries from session bootstrap.
    pre_count = len(session.history)
    session.notify_state_change("persisted change", source="audit_emitter")
    new_entries = session.history[pre_count:]
    assert new_entries, "notify_state_change must append at least one history entry"
    minted = new_entries[-1]

    history_file = session.history_path
    assert history_file.exists()
    raw = history_file.read_text(encoding="utf-8").splitlines()
    # Look for the LAST state_change with our content (= the one we
    # just minted), not any earlier ones from prior sessions in the
    # same project root if monkeypatch.chdir didn't isolate them.
    import json
    matches = []
    for line in raw:
        if not line.strip():
            continue
        entry = json.loads(line)
        if (
            entry.get("role") == "system"
            and (entry.get("meta") or {}).get("kind") == "state_change"
            and entry.get("content") == "persisted change"
        ):
            matches.append(entry)
    assert matches, "minted state_change entry not found in history.jsonl"
    # The minted entry's content and source should round-trip.
    assert matches[-1]["meta"]["source"] == "audit_emitter"
    assert minted.content == "persisted change"
