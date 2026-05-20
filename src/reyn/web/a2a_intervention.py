"""A2AInterventionBus — A2A peer-facing **side-effect observer** for
ivs that arise on async-mode tasks.

issue #292 (α refactor): pre-#292 this class **owned** the iv lifecycle
when registered as a chain override — ``_dispatch_intervention``
replaced its normal handler-dispatch path with ``override.request(iv)``
and the bus awaited ``iv.future`` directly. The iv lived in this bus +
``RunRegistry.pending_intervention`` only; it never entered
``ChatSession._interventions._active`` or the WAL / snapshot persistence
channels. On restart, the bus coroutine died and the restored iv was
orphaned — no in-process awaiter, no R-D12 buffer eligibility, no
``SkillResumeCoordinator`` integration. See issue #292 body for the
full analysis.

Post-α this class is a **side-effect observer**: ``on_dispatch(iv)``
is invoked by ``ChatSession._dispatch_intervention`` BEFORE
``InterventionHandler.dispatch`` runs. The bus:

  - Mirrors ``status="input-required"`` on the RunEntry so polling
    peers see the prompt is pending.
  - Appends the input-required payload to ``RunEntry.history_events``
    so the SSE stream surfaces the prompt (issue #267 Gap 1).
  - POSTs the same payload to the peer's ``webhook_url`` if registered
    (issue #267 Gap 2 + Gap 4 ``kind`` / ``choices`` / ``detail`` shape).
  - Does NOT await ``iv.future``. The handler awaits it on behalf of
    the skill; the bus has no in-process answer-resolution role.

Peer answers arrive via the A2A router's ``POST /a2a/agents/<name>
{task_id, answer}`` → ``ChatSession.answer_pending_intervention`` →
``handler.deliver_answer_to`` (= same path the TUI uses). The iv future
is resolved by the handler; the bus is unaware. R-D12's persistent
answer buffer captures the answer if a crash intervenes before the
skill consumes it, so restart-resume picks up cleanly.

Wired by ``mcp_server.send_to_agent_impl`` through
``ChatSession.register_intervention_override(chain_id, bus)``.

Scope guard:
  - ⭕ in-scope: notify A2A peer of input-required state via webhook +
    SSE buffer + RunEntry status mirror.
  - ❌ out-of-scope: iv ownership (= ChatSession owns it post-α),
    iv.future resolution (= handler does it), skill completion
    narration (= flows through ``_handle_skill_completed``).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.user_intervention import UserIntervention
    from reyn.web.run_registry import RunRegistry

logger = logging.getLogger(__name__)


class A2AInterventionBus:
    """Per-task A2A-side iv side-effect observer (post-α refactor).

    issue #292 (α): no longer an iv owner; pure side-effect emitter.
    """

    def __init__(self, run_id: str, registry: "RunRegistry") -> None:
        self._run_id = run_id
        self._registry = registry

    @property
    def channel_id(self) -> str:
        """Stable channel identifier for issue #268 origin-pin routing.

        Format: ``a2a:<run_id>``. Used by:
          - bus stamping of ``iv.origin_channel_id`` on each ``on_dispatch``
          - listener registration in ``send_to_agent_impl`` so the
            agent's origin-pin check sees this channel as alive while
            the A2A task is running.
        """
        return f"a2a:{self._run_id}"

    async def on_dispatch(self, iv: "UserIntervention") -> None:
        """Fire A2A peer-facing side effects when ``iv`` is about to be
        dispatched by the agent.

        Called by ``ChatSession._dispatch_intervention`` for each iv
        whose chain has this bus registered as an override. Runs
        BEFORE ``InterventionHandler.dispatch`` so the peer learns
        input-required before the awaiter blocks.

        Side effects (each best-effort; failures logged, never raised):
          1. Stamp ``iv.origin_channel_id`` with ``a2a:<run_id>`` (= for
             #268 cross-channel routing).
          2. Mirror ``status="input-required"`` on the RunEntry.
          3. Append the input-required payload to
             ``RunEntry.history_events`` for SSE replay (issue #267
             Gap 1, payload shape per Gap 4).
          4. POST the same payload to ``webhook_url`` if configured
             (issue #267 Gap 2).
        """
        # issue #268 Phase 2: stamp origin channel for cross-channel routing.
        if iv.origin_channel_id is None:
            iv.origin_channel_id = self.channel_id

        entry = self._registry.get(self._run_id)
        if entry is None:
            logger.warning(
                "A2AInterventionBus.on_dispatch: run %r not in registry "
                "(= iv side effects skipped; dispatch will continue)",
                self._run_id,
            )
            return

        # Mirror status. Post-α the iv itself lives in ChatSession's
        # outstanding_interventions; we only reflect the high-level
        # state on the RunEntry for peer polling visibility.
        self._registry.update(self._run_id, status="input-required")

        # Build the canonical input-required payload (issue #267 Gap 4 shape).
        payload: dict = {
            "run_id": self._run_id,
            "status": "input-required",
            "question": iv.prompt,
            "agent_name": entry.agent_name,
            "kind": iv.kind,
            "choices": [
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in iv.choices
            ],
        }
        if iv.detail:
            payload["detail"] = iv.detail

        # SSE buffer append (issue #267 Gap 1). Best-effort.
        try:
            self._registry.append_event(self._run_id, payload)
        except Exception:  # noqa: BLE001
            logger.exception(
                "A2AInterventionBus.on_dispatch: history_events append "
                "failed for run %r", self._run_id,
            )

        # Webhook POST (issue #267 Gap 2). Best-effort, opt-in.
        if entry.webhook_url:
            from reyn.web.notifications import post_webhook  # noqa: PLC0415

            try:
                await post_webhook(entry.webhook_url, payload)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "A2AInterventionBus.on_dispatch: webhook POST failed "
                    "for run %r url=%s", self._run_id, entry.webhook_url,
                )


__all__ = ["A2AInterventionBus"]
