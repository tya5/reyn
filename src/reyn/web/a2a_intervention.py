"""A2AInterventionBus — ``UserChannel`` implementation backed by
``RunRegistry`` (FP-0001).

When a skill running under an A2A async-mode task fires ``ask_user``,
this channel publishes the prompt to the run's RunEntry (= status
changes to ``input-required``, the question text and the IV are stored),
optionally fires a webhook to notify the peer, then awaits the IV's
``future`` until the peer POSTs an answer via
``POST /a2a/agents/<name> {task_id, answer}``.

Wired by ``mcp_server.send_to_agent_impl`` through
``ChatSession.register_intervention_override(chain_id, bus)``.

Phase 2 (issue #254) — responsibility scope:

  - ⭕ in-scope: deliver ``ask_user`` prompts to the A2A peer (= the
    physical user surface for an async-mode A2A run) and receive the
    peer's answer.  ``deliver`` is the canonical method; ``request``
    is a Phase 2 backward-compat alias.
  - ❌ out-of-scope: skill completion narration, intermediate progress
    narration, direct writes to ``RunRegistry`` for run-lifecycle
    events.  Those flow through PR #253's ``_handle_message_send``
    auto-escalation path (sync→Task envelope on running-skill timeout)
    and the OS's ``_handle_skill_completed`` event chain, both of which
    are independent of this channel.  In particular this module MUST
    NOT import ``_handle_skill_completed`` — pinned by Tier 2 test
    (``tests/test_intervention_bus_protocols.py``).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.user_intervention import InterventionAnswer, UserIntervention
    from reyn.web.run_registry import RunRegistry

logger = logging.getLogger(__name__)


class A2AInterventionBus:
    """Per-task ``UserChannel`` that routes ``ask_user`` via RunRegistry."""

    def __init__(self, run_id: str, registry: "RunRegistry") -> None:
        self._run_id = run_id
        self._registry = registry

    @property
    def channel_id(self) -> str:
        """Stable channel identifier for issue #268 origin-pin routing.

        Format: ``a2a:<run_id>``. Used by:
          - bus stamping of ``iv.origin_channel_id`` on each ``deliver``
            (= so the iv carries provenance for cross-channel observe /
            discard / claim)
          - listener registration in ``_handle_async_mode._run`` so the
            ``ChatSession.handle_intervention`` origin-pin check sees
            this channel as alive while the A2A task is running
        """
        return f"a2a:{self._run_id}"

    async def deliver(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``UserChannel.deliver`` — route the prompt to the A2A peer
        via RunRegistry + optional webhook, then block until the peer's
        POST resolves ``iv.future``.

        issue #268 Phase 2: stamps ``iv.origin_channel_id`` so the
        agent layer can later attribute the iv to this A2A task (=
        used by stall detection + cross-channel observe / claim).
        Pre-existing stamping (= if a caller already set the field)
        is respected — overwrite would clobber an explicit origin
        from an upstream layer.
        """
        # issue #268 Phase 2: stamp origin channel for cross-channel routing.
        if iv.origin_channel_id is None:
            iv.origin_channel_id = self.channel_id

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
        # issue #267 Gap 4: surface iv ``kind`` + ``choices`` in the payload
        # so peer can render structured affordance (= permission yes/no/always
        # hotkeys) instead of guessing from prompt text. Free-text ``ask_user``
        # still works unchanged (= ``choices`` is an empty list). ``detail``
        # is included when present so the peer has the same context the
        # in-process TUI surfaces below the prompt.
        if entry.webhook_url:
            from reyn.web.notifications import post_webhook

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
            await post_webhook(entry.webhook_url, payload)

        # Block until the peer POSTs an answer (= registry.answer_intervention
        # resolves iv.future).
        answer = await iv.future
        return answer

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``RequestBus.request`` — Phase 2 backward-compat alias.

        Existing callers wired through
        ``ChatSession.register_intervention_override`` still receive
        this class typed as ``InterventionBus`` and invoke ``request``.
        Delegates to ``deliver`` so the underlying behaviour is
        unchanged.  Phase 3 will route OS-level requests through the
        Agent, which will call ``deliver`` directly.
        """
        return await self.deliver(iv)


__all__ = ["A2AInterventionBus"]
