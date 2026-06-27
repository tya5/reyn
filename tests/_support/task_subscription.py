"""#2187 backend-master test support: a subscription-wired Task backend for backend-unit tests.

Under #2187 backend-master (2c) the Task backend no longer STORES the binding
(``assignee`` / ``requester`` / ``requester_kind``) — that binding is the Reyn-internal
SUBSCRIPTION, which lives in the WAL (a :class:`~reyn.task.subscription.SubscriptionRegistry`,
the X authority), and the OP layer records each create's binding to it. The backend reads
the binding THROUGH an injected ``subscription_reader`` (its ``get`` / ``list`` / abort
cascade hydrate the binding from that registry; the stored columns are gone / placeholders).

This helper lets a backend-UNIT test exercise that read-through WITHOUT standing up the full
op-runtime: it wraps a real backend (constructed with ``subscription_reader = a
SubscriptionRegistry``) and, on ``create(task)``, records the created task's binding to the
SAME registry EXACTLY as the op-append does (``task_subscribed`` with the task's
assignee / requester / requester_kind). So a test that calls ``backend.create(...)`` and then
asserts ``(await backend.get(id)).assignee`` / ``.requester`` / ``.requester_kind`` — or the
ownership-cascade / list-filter that follows the binding — now asserts the real
CONTROL-PLANE binding (the X path, the WAL-subscription) — meaning preserved, not weakened.
Every non-create method delegates to the real backend, whose read-through hydrates the
binding from this same registry — exactly as in production via the op.
"""
from __future__ import annotations

from reyn.task.subscription import SubscriptionRegistry


class SubscriptionBackend:
    """Wraps a real backend + its :class:`SubscriptionRegistry` reader, recording each
    ``create`` task's binding to the subscription — the op-append's behaviour. Read methods
    (get / list / abort cascade / …) pass through to the real backend, whose get / list
    hydrate the binding from the same subscription registry."""

    def __init__(self, real, subscription: SubscriptionRegistry) -> None:
        self._real = real
        self._subscription = subscription
        self._seq = 0

    @property
    def subscription(self) -> SubscriptionRegistry:
        return self._subscription

    async def create(self, task):
        created = await self._real.create(task)
        # Mimic the op-append: record the created task's binding to the subscription
        # (the seq is a monotonic stand-in for the WAL seq).
        self._seq += 1
        self._subscription.apply(
            "task_subscribed",
            self._seq,
            {
                "task_id": created.task_id,
                "assignee": created.assignee,
                "requester": created.requester,
                "requester_kind": created.requester_kind.value,
            },
        )
        return created

    def __getattr__(self, name):
        # get / list / update_status / abort / add_dependency / recompute_readiness /
        # set_awaiting / events / close / … delegate to the real backend (which hydrates
        # the binding from the same subscription registry on its read-through).
        return getattr(self._real, name)
