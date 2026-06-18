"""Reyn plugin public API — stable surface for plugin authors.

Plugins (= webhook handlers under ``reyn.plugins.*`` or external pip
packages registered via the ``reyn.webhooks`` entry-point group)
SHOULD use the helpers in this module to interact with Reyn agents,
NOT call internal ``Session`` methods (= ``_put_inbox``,
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
  ``reyn.interfaces.web.deps``; tests pass a stub for isolation.
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
    plugins. Use it instead of touching ``Session._put_inbox``
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
        from reyn.interfaces.web.deps import _get_registry
        registry = _get_registry()
    # FP-0043 S4b-5: route to the sender's OWN webhook session (parsed from the
    # "<transport>:<external_id>" sender), not the agent's shared "main" — so an
    # external user's conversation is stateful + isolated, and peer traffic doesn't
    # pollute the user's REPL/web conversation. Fire-and-forget: ensure_session_running
    # boots the run-loop (no forwarder); the reply routes via the factory-wired outbox
    # interceptor from reply_to=ExternalRef below (output unchanged, reuse).
    from reyn.chat.webhook_routing import resolve_webhook_session
    session = resolve_webhook_session(registry, target_agent, sender)
    envelope: dict[str, Any] = {
        "text": text,
        "sender": sender,
    }
    if reply_to is not None:
        envelope["reply_to"] = reply_to
    if extra_meta:
        envelope["meta"] = dict(extra_meta)
    await session._put_inbox(kind, envelope)


# ── agent discovery ────────────────────────────────────────────────────


def list_agents(*, registry: Any | None = None) -> list[str]:
    """Return all agent names known to the project (= file-system view).

    Plugin authors use this at ``register_router`` time to validate the
    ``target_agent`` in their config OR to discover available agents
    dynamically. Returns a sorted list of agent names (= subdirs of
    ``.reyn/agents/`` carrying a profile file).

    Includes agents that are NOT currently running (= ``ensure_running``
    will start them on first push). Lazy / on-disk view, not the live
    set of router_loop tasks.

    ``registry`` is optional for tests; production callers omit it and
    the process-shared singleton from ``reyn.interfaces.web.deps`` is used.
    """
    if registry is None:
        from reyn.interfaces.web.deps import _get_registry
        registry = _get_registry()
    return registry.list_names()


def agent_exists(name: str, *, registry: Any | None = None) -> bool:
    """True iff ``name`` is registered in the project's agents dir.

    Useful for pre-flight validation in ``register_router`` so a
    plugin can return ``None`` (= skip mount) when the configured
    ``target_agent`` is mistyped, instead of failing on every webhook
    POST with a 503.

    Falls back to ``False`` on any registry error (= defensive: a
    boot-time registry hiccup shouldn't crash plugin discovery).
    """
    try:
        if registry is None:
            from reyn.interfaces.web.deps import _get_registry
            registry = _get_registry()
        return registry.exists(name)
    except Exception:
        return False


# ── sender formatting ──────────────────────────────────────────────────


def make_sender(
    transport: str,
    external_id: str,
    *,
    display: str | None = None,
    source_scope: str | None = None,
) -> str:
    """Build a Reyn ``sender`` attribution string.

    Reyn's dispatch attribution (= PR-A) emits a ``[context shift]``
    state_change history entry when the sender changes between turns.
    The format is ``<transport>[:<source_scope>]:<external_id>[:<display>]``
    so ``_format_sender_label`` (= reyn.chat.session) renders a
    readable label for the LLM.

    Examples
    --------
    Slack 1:1 chat::

        make_sender("slack", "U456")
        # → "slack:U456"

        make_sender("slack", "U456", display="bob")
        # → "slack:U456:bob"

    LINE 1:1 chat with explicit source scope::

        make_sender("line", "U456", source_scope="user")
        # → "line:user:U456"

    LINE group chat (= external_id is the group, display is the
    posting user)::

        make_sender("line", "G999", source_scope="group", display="U456")
        # → "line:group:G999:U456"

    Plugin authors SHOULD prefer this helper over raw f-string
    assembly so the dispatch attribution label rendering follows
    the documented format; future scope changes propagate without
    each plugin needing to update.
    """
    parts: list[str] = [transport]
    if source_scope:
        parts.append(source_scope)
    parts.append(external_id)
    if display:
        parts.append(display)
    return ":".join(parts)


__all__ = [
    "agent_exists",
    "list_agents",
    "make_sender",
    "push_to_agent",
]
