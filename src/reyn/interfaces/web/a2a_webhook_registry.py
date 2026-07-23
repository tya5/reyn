"""A2A-owned webhook registry for Task disposition notify (#1953 slice 5a-2).

P7: ``webhook_url`` is A2A vocabulary, so it is kept here in the A2A layer — NOT
on the core Task model (which stays term-neutral). Holds two things, both
persisted (so a server restart neither loses a pending disposition webhook nor
re-fires a delivered one), mirroring ``RunRegistry``'s persist-path pattern:

  - ``contextId → webhook_url`` — the external requester's push channel, populated
    when an A2A client creates a task with a ``webhook_url`` (the production
    populator lands in slice 5b; until then this is seeded directly in tests).
  - the **notified** ``task_id`` set — which dispositions have already been
    pushed, so the periodic sweep does not re-fire (idempotence across restarts).

The notified set is kept **bounded by construction**: every sweep calls
:meth:`reconcile_notified` to intersect it with the still-present (archived) task
ids, self-pruning any that have been hard-deleted (no slice-9 coupling).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class A2AWebhookRegistry:
    """Persistent A2A-owned map (contextId → webhook_url) + notified task-id set."""

    def __init__(self, *, persist_path: "Path | None" = None) -> None:
        self._webhooks: dict[str, str] = {}
        self._notified: set[str] = set()
        self._persist_path = persist_path
        if persist_path is not None and persist_path.exists():
            self._restore_from(persist_path)

    # ── webhook channel map ─────────────────────────────────────────────────

    def register_webhook(self, context_id: str, webhook_url: str) -> None:
        """Record the external requester's webhook channel for a contextId."""
        self._webhooks[context_id] = webhook_url
        self._persist()

    def webhook_for(self, context_id: str) -> "str | None":
        return self._webhooks.get(context_id)

    # ── notified set (idempotence) ──────────────────────────────────────────

    def is_notified(self, task_id: str) -> bool:
        return task_id in self._notified

    def mark_notified(self, task_id: str) -> None:
        self._notified.add(task_id)
        self._persist()

    def reconcile_notified(self, present_task_ids: "set[str]") -> None:
        """Prune the notified set to ids still present (archived) in the backend —
        self-pruning hard-deleted tasks so the set is bounded by construction."""
        pruned = self._notified & present_task_ids
        if pruned != self._notified:
            self._notified = pruned
            self._persist()

    # ── persistence (RunRegistry pattern: atomic JSON rewrite per mutation) ──

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"webhooks": self._webhooks, "notified": sorted(self._notified)}
        fd, tmp = tempfile.mkstemp(dir=str(self._persist_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, str(self._persist_path))
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _restore_from(self, path: "Path") -> None:
        try:
            data = json.loads(path.read_text())
        except Exception:
            return
        self._webhooks = dict(data.get("webhooks") or {})
        self._notified = set(data.get("notified") or [])


async def sweep_dispositions(run_registry, registry: A2AWebhookRegistry, *, post_fn=None) -> int:
    """One disposition-sweep pass (#1953 slice 5a-2). Notify the external requester
    of every cancelled run not yet notified, via its webhook; returns the count of
    webhooks fired.

    #2839 Phase 1: re-based off ``RunRegistry`` instead of the internal Task
    backend. Every ``RunEntry`` is structurally A2A-external (RunRegistry has
    no internal/self-origin concept — it exists only to back A2A async runs),
    so the prior ``origin == EXTERNAL`` filter is dropped rather than ported:
    it would always be true. The single cancellation trigger left after the
    decouple is the A2A ``cancel_task`` web endpoint itself (``run_registry.
    cancel()``) — self-heals across restarts the same way (a missed webhook
    is caught on the next sweep; a failed POST leaves the run un-notified →
    retried next sweep).
    """
    from reyn.interfaces.web.notifications import post_webhook
    from reyn.runtime.a2a_routing import a2a_context_id

    # ``post_fn`` is the (injectable) webhook poster — the real ``post_webhook`` in
    # production; tests inject a real recording callable (no mocks).
    post = post_fn if post_fn is not None else post_webhook

    from reyn.interfaces.web.run_registry import RunStatus  # noqa: PLC0415

    cancelled = run_registry.list(status=RunStatus.CANCELLED)
    registry.reconcile_notified({e.run_id for e in cancelled})

    fired = 0
    for entry in cancelled:
        if registry.is_notified(entry.run_id):
            continue
        if entry.session_id is None:
            continue  # no core session_id → no contextId to resolve a channel for
        context_id = a2a_context_id(entry.session_id)
        url = registry.webhook_for(context_id)
        if url is None:
            continue  # no webhook channel registered for this context (e.g. pre-5b)
        result = await post(
            url,
            {"task_id": entry.run_id, "contextId": context_id, "disposition": "aborted"},
        )
        if getattr(result, "ok", False):
            registry.mark_notified(entry.run_id)
            fired += 1
    return fired
