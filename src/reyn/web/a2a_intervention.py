"""A2AInterventionBus — InterventionBus impl backed by RunRegistry (FP-0001).

When a skill running under an A2A async-mode task fires ``ask_user``,
this bus publishes the prompt to the run's RunEntry (= status changes
to ``input-required``, the question text and the IV are stored),
optionally fires a webhook to notify the peer, then awaits the IV's
``future`` until the peer POSTs an answer via
``POST /a2a/agents/<name> {task_id, answer}``.

Wired by ``mcp_server.send_to_agent_impl`` through
``ChatSession.register_intervention_override(chain_id, bus)``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.user_intervention import InterventionAnswer, UserIntervention
    from reyn.web.run_registry import RunRegistry

logger = logging.getLogger(__name__)


class A2AInterventionBus:
    """Per-task InterventionBus that routes ask_user via RunRegistry."""

    def __init__(self, run_id: str, registry: "RunRegistry") -> None:
        self._run_id = run_id
        self._registry = registry

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        entry = self._registry.get(self._run_id)
        if entry is None:
            raise RuntimeError(
                f"A2AInterventionBus: run {self._run_id!r} not in registry"
            )

        # Publish: status=input-required + question + IV reference so
        # POST /a2a/agents/<name> {task_id, answer} can resolve via
        # RunRegistry.answer_intervention.
        self._registry.update(
            self._run_id,
            status="input-required",
            question=iv.prompt,
            pending_intervention=iv,
        )

        # Optional webhook notification (fire-and-forget; failures logged).
        if entry.webhook_url:
            from reyn.web.notifications import post_webhook

            await post_webhook(
                entry.webhook_url,
                {
                    "run_id": self._run_id,
                    "status": "input-required",
                    "question": iv.prompt,
                    "agent_name": entry.agent_name,
                },
            )

        # Block until the peer POSTs an answer (= registry.answer_intervention
        # resolves iv.future).
        answer = await iv.future
        return answer


__all__ = ["A2AInterventionBus"]
