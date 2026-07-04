"""Programmatic session-spawn entry point for non-LLM callers.

``AgentRegistry.spawn_session_recorded`` is the clean action-layer seam behind
``session_spawn`` (the LLM tool): it spawns a fresh-context session, persists +
enforces any capability narrowing, and emits the rewind-tracked
``session_spawned`` WAL event. The LLM tool path reaches it only through
``RouterCallerState.spawn_session_fn``, a closure the router loop builds — so a
deterministic, non-LLM caller (e.g. a Pipeline executor's ``agent`` step) has no
router-free way in.

``spawn_ephemeral_session`` closes that gap: it calls the SAME
``spawn_session_recorded`` primitive directly, with no ``RouterLoopHost`` /
``RouterCallerState`` / router-loop involvement at all — just a registry and a
target identity. It hardcodes ``mode="ephemeral"`` (the only mode a
programmatic driver needs today) and returns the new session id. Turn/token
budgeting for spawned sessions is a separate, harder mechanism (per-session
``max_turns``) and is deliberately out of scope here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.registry import AgentRegistry


async def spawn_ephemeral_session(
    registry: "AgentRegistry", *, identity: str, narrowing: "dict | None" = None,
) -> str:
    """Spawn an ephemeral session under ``identity`` for a non-LLM caller.

    Thin, direct wrapper over ``registry.spawn_session_recorded(identity,
    mode="ephemeral", narrowing=narrowing)`` — the same call the
    ``session_spawn`` tool's handler reaches via ``spawn_session_fn``, so the
    emitted ``session_spawned`` WAL event + the spawned session's narrowing
    enforcement are byte-identical to the tool path. Returns the new session id
    (the ``session_spawned`` event's ``sid``).

    No task is submitted here — that stays the caller's job (the Pipeline
    executor's ``agent`` step, in the eventual wiring), same as the S1bc
    action-layer seam does not submit either."""
    return await registry.spawn_session_recorded(
        identity, mode="ephemeral", narrowing=narrowing,
    )
