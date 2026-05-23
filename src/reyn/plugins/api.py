"""Reyn plugin public API — stable surface for plugin authors.

Plugins (= webhook handlers under ``reyn.plugins.*`` or external pip
packages registered via the ``reyn.webhooks`` entry-point group)
SHOULD use the helpers in this module to interact with Reyn agents,
NOT call internal ``ChatSession`` methods (= ``_put_inbox``,
``_handle_user_message``) directly.

The contract here is intended to stay stable across Reyn minor
versions; internal session APIs may change without notice.

## Status

  ``push_to_agent`` — Tier-1 stable, FP-0041 PR plugins-api ✅
  Other helpers — TBD as plugin needs surface.

## Design notes

- The signature is **kwarg-only** so future additions don't shift
  positional arguments.
- ``kind`` defaults to ``"user"`` (= the typical webhook plugin case
  where an external message becomes a user-shaped turn). Future
  unification with A2A / MCP message paths may use other kind
  values; the parameter is exposed today so the contract can extend
  without API break.
- ``extra_meta`` is a passthrough hook for future per-path metadata
  (= e.g. A2A's ``chain_id``, MCP's ``request_id``). Webhook plugins
  typically omit it; A2A/MCP convergence work is tracked separately.
- ``registry`` defaults to the process-shared singleton from
  ``reyn.web.deps``; tests pass a stub for isolation.
"""
from __future__ import annotations

from typing import Any

from reyn.chat.transport import TransportRef


async def push_to_agent(
    *,
    target_agent: str,
    text: str,
    sender: str,
    reply_to: TransportRef | None = None,
    kind: str = "user",
    extra_meta: dict | None = None,
    registry: Any | None = None,
) -> None:
    """Deliver a message to a Reyn agent's inbox.

    This is the **stable public API** for webhook / chat-transport
    plugins. Use it instead of touching ``ChatSession._put_inbox``
    directly. Internal session APIs may change between Reyn versions;
    this function won't.

    Parameters
    ----------
    target_agent:
        Name of the agent in the project's ``.reyn/agents/`` registry
        that should receive the message. Raises ``FileNotFoundError``
        when no such agent exists.
    text:
        Message body the LLM will see as its turn input.
    sender:
        Attribution string with format ``<transport>:<external_id>``
        (= e.g. ``"slack:U456"``, ``"line:user:U999"``,
        ``"webhook:github:42"``). PR-A dispatch attribution emits a
        ``[context shift]`` state_change entry on transitions between
        senders.
    reply_to:
        Optional ``TransportRef`` describing where agent replies
        should route (= e.g. ``ExternalRef`` for outbound dispatch
        via MCP). When set, Reyn's outbox interceptor (= PR-D2)
        forwards replies through ``route_to_mcp``. When ``None``,
        replies follow the default surface (= TUI / detached).
    kind:
        Inbox dispatch kind. ``"user"`` for normal external user
        messages (= webhook plugin default). Other values are
        Reyn-internal (= ``"agent_request"`` / ``"agent_response"``
        for A2A; reserved for future unification work). Plugin
        authors should not need to override this in current Reyn.
    extra_meta:
        Per-path metadata for future unification (= A2A chain_id,
        MCP request_id, etc.). Webhook plugins typically omit it;
        passing arbitrary keys here is reserved — see future
        unification design before relying on it.
    registry:
        Optional ``AgentRegistry`` override for tests. Production
        callers omit it; the process-shared registry is used.

    Raises
    ------
    FileNotFoundError:
        When ``target_agent`` doesn't exist in the project registry.
    """
    if registry is None:
        from reyn.web.deps import _get_registry
        registry = _get_registry()
    session = await registry.ensure_running(target_agent)
    envelope: dict[str, Any] = {
        "text": text,
        "sender": sender,
    }
    if reply_to is not None:
        envelope["reply_to"] = reply_to
    if extra_meta:
        envelope["meta"] = dict(extra_meta)
    await session._put_inbox(kind, envelope)


__all__ = ["push_to_agent"]
