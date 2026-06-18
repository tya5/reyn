"""FP-0043 Stage 4b-4: a2a-transport session routing (registry-only, unit-testable).

Peer (Agent2Agent) delegations run on a SHARED ``a2a`` Session per agent — option
(B): one a2a conversation per agent, isolated from the user's own "main" session
(so peer traffic never pollutes the REPL/web conversation), while preserving the
existing sync→Task escalation + continuation machinery (which assumes a single
per-agent session — the monitor pumps it, completion narration lands on the next
call to the same agent). Per-delegation isolation (option A) is deferred until that
escalation machinery is made per-session-aware.

The inbound A2A HTTP request carries no caller identity, so per-peer routing is
infeasible; a constant native-id gives the one shared a2a session. No run-loop —
the a2a handlers drive turns inline via ``MessageBus.request``.
"""
from __future__ import annotations

A2A_TRANSPORT = "a2a"
# Constant native-id → one shared a2a session per agent (option B). Per-delegation
# (a fresh id per request) is the future once escalation is per-session-aware.
A2A_NATIVE_ID = "a2a"


def a2a_session_id() -> str:
    """The logical session-id (routing-key) of the shared a2a session: ``a2a:a2a``."""
    return f"{A2A_TRANSPORT}:{A2A_NATIVE_ID}"


def resolve_a2a_session(registry, agent_name: str):
    """Resolve (get-or-spawn) the agent's shared a2a Session.

    Idempotent — every a2a inbound mode (sync send / escalation monitor / async /
    answer-injection) routes through here so they all act on the SAME a2a session
    (not "main"), keeping the escalation/continuation contract intact. No
    ``ensure_session_running`` — the a2a handlers drive the turn inline."""
    return registry.resolve_session(agent_name, A2A_TRANSPORT, A2A_NATIVE_ID)
