"""FP-0043 Stage 4b-5: webhook-transport session routing (registry-only, unit-testable).

An inbound webhook (slack / line / generic plugin) maps to its OWN conversation
Session of the target agent, keyed by the message sender. The webhook ``sender``
already carries ``"<transport>:<external_id>"`` (e.g. ``"slack:U456"`` /
``"line:user:U999"`` / ``"webhook:github:42"``), so it is parsed straight into the
routing-key: slack/line get their own logical-transport namespace (matching the
0043 §Routing-key doc — namespaced by the LOGICAL transport, not the delivery
surface), generic webhooks get ``webhook:``. Per-external-user continuity (a slack
user's conversation persists + is isolated), consistent with web per-thread.

Behaviour change note (FP-0043 S4b-5, owner-approved): webhook delivery moves from
the agent's shared "main" session to a per-sender mapping — peer/external traffic
no longer pollutes the user's REPL/web conversation. Output is UNCHANGED: the plugin
sets ``reply_to=ExternalRef`` and the factory-wired outbox interceptor routes the
agent's reply back to the source (fire-and-forget; reuse, no new output code).

#2608 H5: :func:`dispatch_webhook_received` fires the ``webhook_received``
external-event hook on the resolved session — the LAST source in the
external-event->hooks arc (after H1's MCP push, H4's fs-watcher, H5's own
``cron_fired``). Called from ``reyn.gateway.api.push_to_agent`` (the single
stable ingress every webhook plugin routes through), right after
:func:`resolve_webhook_session`.
"""
from __future__ import annotations

from reyn.hooks.ingress import WebhookIngressAdapter

_GENERIC_WEBHOOK_TRANSPORT = "webhook"

# Hook-Event Redesign Phase 2 (proposal 0059 §6.2): the Webhook Adapter is
# stateless (no bound queue/session — it resolves its target Session fresh
# at request time), so one module-level instance is shared by every call.
_ADAPTER = WebhookIngressAdapter()


def parse_webhook_sender(sender: str) -> tuple[str, str]:
    """Split a webhook ``sender`` into ``(transport, external_id)``.

    ``"slack:U456"`` → ``("slack", "U456")``; ``"webhook:github:42"`` →
    ``("webhook", "github:42")`` (only the FIRST ``:`` splits, so an external_id
    that itself contains ``:`` is preserved). A sender with no transport prefix
    falls back to the generic ``webhook`` namespace with the whole sender as the
    external_id."""
    return _ADAPTER.parse_sender(sender)


def resolve_webhook_session(registry, agent_name: str, sender: str):
    """Resolve (get-or-spawn) the per-sender webhook Session and boot its run-loop.

    Hook-Event Redesign Phase 2 (proposal 0059 §6.2): delegates to
    ``WebhookIngressAdapter.resolve_session`` — the out-of-process
    Session-resolve step of the unified Ingress Adapter interface, closed
    inside the adapter (Sync dispatch / a future Async Bus never see it).
    Byte-identical steps (parse sender → routing-key, get-or-spawn, boot the
    run-loop with no forwarder — webhook is fire-and-forget).

    Idempotent. Returns the resolved Session."""
    return _ADAPTER.resolve_session(registry, agent_name, sender)


def dispatch_webhook_received(session, sender: str) -> None:
    """#2608 H5 / Hook-Event Phase 2 §6.2: fire the ``webhook_received``
    external-event hook on ``session`` (the sender's own resolved Session —
    pass the object :func:`resolve_webhook_session` returned).

    Delegates to ``WebhookIngressAdapter``'s ``to_event`` (builds the typed
    ``HookEvent`` via Phase 1's ``build_hook_payload``, unchanged field-set —
    SECURITY invariant preserved: ONLY ``transport`` + ``sender``, never the
    raw inbound body/text, which may carry tokens/PII the operator never
    intended a hook action to see; contrast
    ``reyn.runtime.cron.routing.dispatch_cron_fired``, whose ``job_name``/
    ``to`` are operator-authored config, never end-user-supplied) then
    ``deliver`` (``reyn.hooks.external_fire.fire_and_forget`` — a slow hook
    action must never stall the webhook plugin's HTTP response). Both
    ``transport`` and ``sender`` are exact-match fields (not glob — see
    ``reyn.hooks.matcher``), e.g. ``matcher: {transport: "slack"}``.
    """
    event = _ADAPTER.to_event(sender)
    _ADAPTER.deliver(event, session)
