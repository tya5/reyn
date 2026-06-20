"""FP-0043 Stage 4b-4: a2a-transport session routing (registry-only, unit-testable).

Peer (Agent2Agent) delegations run on a **per-``contextId``** ``a2a`` Session
(#1814): each A2A conversation (identified by the request's ``contextId``) gets
its own isolated Session — ``a2a:<contextId>`` — so different callers' conversations
never interfere, and the same ``contextId`` continues the same conversation. This
mirrors ``webhook_routing.py`` (per-sender ``slack:<user>``). A request with no
``contextId`` is assigned a fresh ``contextId`` by the server (A2A-spec), returned
to the caller so they can continue by echoing it.

The escalation + continuation machinery is now per-``contextId``-aware: the
escalated ``RunEntry`` records its ``context_id`` so the monitor, answer-injection,
and the next-call completion-narration drain all resolve the SAME per-contextId
session. No run-loop — the a2a handlers drive turns inline via ``MessageBus.request``.
"""
from __future__ import annotations

A2A_TRANSPORT = "a2a"
# #1814: the per-request server-assigned default native-id is a fresh uuid (the
# caller passes its ``contextId``); this constant is the legacy single-session id,
# retained only as a documented fallback for callers that pass an empty contextId.
A2A_NATIVE_ID = "a2a"


def a2a_session_id(context_id: str) -> str:
    """The logical session-id (routing-key) of the per-contextId a2a session:
    ``a2a:<contextId>`` (#1814). Callers compute ``context_id`` once per request
    (``params.contextId`` or a server-assigned uuid) and pass it everywhere so the
    sync send, escalation monitor, answer-injection, and async paths all act on the
    SAME per-contextId session."""
    return f"{A2A_TRANSPORT}:{context_id or A2A_NATIVE_ID}"


def a2a_context_id(session_id: str | None) -> str:
    """Reverse of :func:`a2a_session_id` — the A2A ``contextId`` carried by a
    session routing-key ``a2a:<contextId>`` (#1814). Keeps the ``contextId ↔
    session_id`` mapping INSIDE the A2A layer: core (``RunEntry``) stores only the
    neutral ``session_id``, and the A2A handlers recover the ``contextId`` here
    (e.g. to re-resolve the escalated run's session for answer-injection)."""
    prefix = f"{A2A_TRANSPORT}:"
    sid = session_id or ""
    return sid[len(prefix):] if sid.startswith(prefix) else (sid or A2A_NATIVE_ID)


def resolve_a2a_session(registry, agent_name: str, context_id: str):
    """Resolve (get-or-spawn) the agent's per-``contextId`` a2a Session (#1814).

    Idempotent — every a2a inbound mode (sync send / escalation monitor / async /
    answer-injection) routes through here with the request's ``context_id`` so they
    all act on the SAME per-contextId session (not "main", not a shared one),
    keeping the escalation/continuation contract intact per conversation. No
    ``ensure_session_running`` — the a2a handlers drive the turn inline."""
    return registry.resolve_session(agent_name, A2A_TRANSPORT, context_id or A2A_NATIVE_ID)
