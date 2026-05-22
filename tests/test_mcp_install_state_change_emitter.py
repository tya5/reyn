"""Tier 2: mcp_install → state_change emitter wiring (#398 v4 emitter #2).

Second concrete emitter for the ``notify_state_change`` API. When the
LLM (or any chat-router-initiated path) successfully installs an MCP
server, the op_runtime ``mcp_install`` handler emits an
``mcp_server_installed`` event on the session's chat_events log. The
``_on_chat_event_for_state_change`` subscriber sees it and mints a
``state_change`` history entry so the LLM's next turn sees "MCP
server 'X' was installed." — breaks the symmetric trap to #352
where the LLM kept saying "I can't access X" after X was newly
installed.

Pins:

  1. ``mcp_server_installed`` event on the session's chat_events log
     triggers a state_change history entry.
  2. The summary text uses the ``server_name`` field from the event.
  3. The state_change carries ``source="mcp_install"`` for audit.
  4. Unknown event types don't trigger anything (= dispatch table
     filters cleanly).
  5. Malformed event data (= missing template keys) is silently
     skipped — observability must not crash the events bus.
  6. The dispatch table is extensible (= adding a new emitter is one
     entry).

Tier 2 because the contract is the foundation for the rest of the
#398 v4 emitter family — config_watcher / sp_loader / etc. follow the
same dispatch table pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.session import (
    _STATE_CHANGE_EVENT_MAPPINGS,
    ChatMessage,
    ChatSession,
)
from reyn.events.state_log import StateLog


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


# ── dispatch table contract ───────────────────────────────────────────


def test_mcp_server_installed_is_in_dispatch_table():
    """Tier 2: the dispatch table contains an entry for the
    ``mcp_server_installed`` event with source ``"mcp_install"`` and a
    template that consumes ``server_name``.

    Pins the table-driven emitter shape so future PRs that add
    additional events can follow the same pattern (= one entry per
    new emitter).
    """
    assert "mcp_server_installed" in _STATE_CHANGE_EVENT_MAPPINGS
    source, template = _STATE_CHANGE_EVENT_MAPPINGS["mcp_server_installed"]
    assert source == "mcp_install"
    assert "{server_name}" in template


# ── mcp_server_installed emission ─────────────────────────────────────


def test_mcp_server_installed_event_mints_state_change(tmp_path):
    """Tier 2 (#398 v4 emitter #2): emitting
    ``mcp_server_installed`` on the session's chat_events log triggers
    a state_change history entry. The LLM's next turn reads "MCP server
    'X' was installed." and breaks out of the "I can't access X" trap.
    """
    session = _make_session(tmp_path)
    pre_count = len(_state_changes(session))

    session._chat_events.emit(
        "mcp_server_installed",
        server_id="io.github.modelcontextprotocol/server-sqlite",
        server_name="sqlite",
        scope="project",
        runtime="npx",
    )

    entries = _state_changes(session)
    assert len(entries) == pre_count + 1
    assert entries[-1].content == "MCP server 'sqlite' was installed."
    assert entries[-1].meta.get("source") == "mcp_install"


def test_mcp_server_installed_uses_server_name_field(tmp_path):
    """Tier 2: the template substitutes ``server_name`` from event data,
    not ``server_id`` (= some registries use IDs that differ from the
    config key the user actually references). The user-readable name
    is what the LLM should see.
    """
    session = _make_session(tmp_path)

    session._chat_events.emit(
        "mcp_server_installed",
        server_id="long.registry.id/server-fs",
        server_name="filesystem",
    )

    entries = _state_changes(session)
    assert entries[-1].content == "MCP server 'filesystem' was installed."


# ── dispatch hygiene (= non-mapped events ignored) ─────────────────────


def test_non_mapped_event_does_not_trigger_state_change(tmp_path):
    """Tier 2: events not in the dispatch table are silently ignored —
    the subscriber doesn't accidentally mint state_change entries for
    every event on the chat_events log.

    Without this filter the subscriber would create a flood of
    state_change entries for events like ``router_loop_started`` or
    ``llm_called`` that have nothing to do with world-state changes.
    """
    session = _make_session(tmp_path)
    pre_count = len(_state_changes(session))

    # Several non-mapped events that ARE legitimate chat_events traffic.
    session._chat_events.emit("router_iteration_started")
    session._chat_events.emit("llm_called", model="x")
    session._chat_events.emit("act_executed", tool="some_tool")

    # No new state_change entries.
    assert len(_state_changes(session)) == pre_count


def test_malformed_event_data_skipped_defensively(tmp_path):
    """Tier 2: a mapped event with missing required template keys
    (= e.g. ``mcp_server_installed`` without ``server_name``) is
    skipped silently — does not crash the events bus or propagate
    a KeyError to the producer.

    Defensive isolation: observability must not break the core
    operation that emitted the event.
    """
    session = _make_session(tmp_path)
    pre_count = len(_state_changes(session))

    # Emit without the ``server_name`` field the template needs.
    session._chat_events.emit("mcp_server_installed", server_id="x")

    # No state_change entry minted (= silently skipped).
    assert len(_state_changes(session)) == pre_count


# ── notify_state_change emitter wiring as a system property ────────────


def test_multiple_installs_each_mint_separate_state_change(tmp_path):
    """Tier 2: multiple installs in sequence each mint their own
    state_change entry — no implicit deduplication or batching.
    Matches the per-event preservation policy from #398 v4 Q3.
    """
    session = _make_session(tmp_path)
    pre_count = len(_state_changes(session))

    session._chat_events.emit("mcp_server_installed", server_name="sqlite")
    session._chat_events.emit("mcp_server_installed", server_name="git")
    session._chat_events.emit("mcp_server_installed", server_name="fetch")

    entries = _state_changes(session)
    assert len(entries) == pre_count + 3
    contents = [e.content for e in entries[-3:]]
    assert contents == [
        "MCP server 'sqlite' was installed.",
        "MCP server 'git' was installed.",
        "MCP server 'fetch' was installed.",
    ]
