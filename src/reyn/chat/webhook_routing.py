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
"""
from __future__ import annotations

_GENERIC_WEBHOOK_TRANSPORT = "webhook"


def parse_webhook_sender(sender: str) -> tuple[str, str]:
    """Split a webhook ``sender`` into ``(transport, external_id)``.

    ``"slack:U456"`` → ``("slack", "U456")``; ``"webhook:github:42"`` →
    ``("webhook", "github:42")`` (only the FIRST ``:`` splits, so an external_id
    that itself contains ``:`` is preserved). A sender with no transport prefix
    falls back to the generic ``webhook`` namespace with the whole sender as the
    external_id."""
    transport, sep, external_id = sender.partition(":")
    if not sep or not transport.strip():
        return _GENERIC_WEBHOOK_TRANSPORT, sender
    return transport, external_id


def resolve_webhook_session(registry, agent_name: str, sender: str):
    """Resolve (get-or-spawn) the per-sender webhook Session and boot its run-loop.

    Steps (pure registry + session ops):
      1. parse the sender → ``(transport, external_id)`` routing-key.
      2. ``resolve_session`` get-or-spawn (persistent per sender; the same external
         user resumes the same Session).
      3. ``ensure_session_running`` — run-loop WITHOUT a forwarder (webhook is
         fire-and-forget; the reply routes via the factory-wired outbox interceptor
         from the plugin's ``reply_to=ExternalRef``, not the REPL sink).

    Idempotent. Returns the resolved Session."""
    transport, native_id = parse_webhook_sender(sender)
    session = registry.resolve_session(agent_name, transport, native_id)
    registry.ensure_session_running(agent_name, f"{transport}:{native_id}")
    return session
