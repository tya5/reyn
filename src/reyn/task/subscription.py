"""#2187 backend-master: the Task SUBSCRIPTION registry — a WAL-derived live binding.

The Reyn-INTERNAL binding — which session is bound to / subscribes to each task (the
``assignee`` that executes it + the ``requester`` parent that owns it) — is the
SUBSCRIPTION. It lives in the WAL (Reyn's own trajectory), NOT in the backend: the
backend is the external MASTER of task-STATE (status / content / DAG), and Reyn does
not own or rewind that. The subscription is what Reyn DOES own, so it is what
time-travel rewinds — replay it ``up_to`` a cut (and skip abandoned branches via
``is_active``) to restore the as-of-cut bindings, then re-adapt to the current
external task-state.

This is the SAME WAL-derived-live-state pattern as the reverted (A) ``GlobalTaskState``
— but applied to the CORRECT target. (A) wrongly put task-STATE (the backend's, the
external master's) in the WAL, which split one task across two planes and broke the
content/DAG rewind. The binding is genuinely Reyn-internal, so putting IT in the WAL
is right: ``WAL = session + subscription`` (the owner's model). The WAL kinds
(``task_subscribed`` / ``task_rebound``) are applied to a live registry, updated on
each durable WAL append (the StateLog ``_post_append_cbs`` observer, #1560) and
rebuilt by replay on recovery / rewind.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

# The task SUBSCRIPTION WAL kinds (mirrors the additions to ``state_log.WAL_EVENT_KINDS``).
_TASK_SUBSCRIBED = "task_subscribed"   # a task's INITIAL binding (assignee + requester + kind)
_TASK_REBOUND = "task_rebound"         # the binding changed (reassign / unbind the assignee)
TASK_SUBSCRIPTION_KINDS = frozenset({_TASK_SUBSCRIBED, _TASK_REBOUND})


@dataclass
class SubscriptionRecord:
    """The Reyn-internal binding of one task. ``assignee`` is the executing session
    (the single-writer / who Reyn delivers execute-events to); ``None`` = UNASSIGNED.
    ``requester`` is the parent owner (a session id, or a task id when a task-as-request
    owns this sub-task) and ``requester_kind`` disambiguates (``"session"`` / ``"task"``)
    — the decomposition / recovery-routing binding. Both are Reyn-internal, hence
    WAL-resident + rewound. ``created_seq`` is the ``task_subscribed`` WAL seq."""

    assignee: "str | None"
    requester: "str | None"
    requester_kind: str
    created_seq: int


class SubscriptionRegistry:
    """The WAL-derived live task→binding map (the authority for the Reyn-internal
    subscription; the backend remains the authority for task-STATE)."""

    def __init__(self) -> None:
        self._subs: dict[str, SubscriptionRecord] = {}

    # ── apply / replay (the GlobalTaskState shape, for the binding) ───────────

    def apply(self, kind: str, seq: int, fields: dict) -> None:
        """Apply one subscription WAL entry to the live binding. A non-subscription
        kind is ignored (the observer is registered process-wide)."""
        task_id = fields.get("task_id")
        if not isinstance(task_id, str):
            return
        if kind == _TASK_SUBSCRIBED:
            self._subs[task_id] = SubscriptionRecord(
                assignee=fields.get("assignee"),
                requester=fields.get("requester"),
                requester_kind=fields.get("requester_kind", "session"),
                created_seq=seq,
            )
        elif kind == _TASK_REBOUND:
            rec = self._subs.get(task_id)
            if rec is not None:
                # ``assignee`` absent / None → UNASSIGNED (unbind / re-queue).
                rec.assignee = fields.get("assignee")

    def replay(
        self, events: Iterable[dict], *, up_to: "int | None" = None,
        is_active: "Callable[[int], bool] | None" = None,
    ) -> None:
        """Rebuild the live binding from the WAL (recovery / rewind). ``up_to`` (a WAL
        seq) gives the AS-OF-CUT binding — the time-travel restore. ``is_active`` (e.g.
        ``lambda s: is_active_seq(state_log, s)``) skips ABANDONED rewind-branch
        segments (multi-rewind) — the SAME active-branch predicate the workspace /
        runtime restore uses — so a prior rewind's undone (re)binding is not
        resurrected. Both filters mirror the task-STATE replay the (A) work proved
        correct; here they restore the BINDING (the right WAL-resident target)."""
        self._subs.clear()
        for entry in events:
            seq = entry.get("seq")
            if not isinstance(seq, int):
                continue
            if up_to is not None and seq > up_to:
                continue
            if is_active is not None and not is_active(seq):
                continue
            kind = entry.get("kind")
            if kind in TASK_SUBSCRIPTION_KINDS:
                self.apply(kind, seq, entry)

    # ── queries (the op-layer gating + recovery read these) ───────────────────

    def exists(self, task_id: str) -> bool:
        return task_id in self._subs

    def assignee_of(self, task_id: str) -> "str | None":
        rec = self._subs.get(task_id)
        return rec.assignee if rec is not None else None

    def requester_of(self, task_id: str) -> "str | None":
        rec = self._subs.get(task_id)
        return rec.requester if rec is not None else None

    def requester_kind_of(self, task_id: str) -> "str | None":
        rec = self._subs.get(task_id)
        return rec.requester_kind if rec is not None else None

    def task_ids(self) -> "list[str]":
        return list(self._subs)

    def unassigned(self) -> "list[str]":
        """The UNASSIGNED tasks (no assignee) — the pending-assignment queue."""
        return [tid for tid, rec in self._subs.items() if rec.assignee is None]


class SubscriptionWriter:
    """The op-layer seam that appends the task SUBSCRIPTION WAL kinds — the Reyn-internal
    binding WRITES. Wraps the registry-owned :class:`StateLog`; the registry's #1560
    post-append observer applies each append to the live :class:`SubscriptionRegistry`.
    None on the OpContext (direct construction / tests / no state_log) → the op skips
    the append (the opt-in contract)."""

    def __init__(self, state_log) -> None:
        self._state_log = state_log

    async def record_subscribed(
        self, task_id: str, *, assignee: "str | None", requester: "str | None",
        requester_kind: str,
    ) -> int:
        """A task's INITIAL binding (on create)."""
        return await self._state_log.append(
            _TASK_SUBSCRIBED, task_id=task_id, assignee=assignee,
            requester=requester, requester_kind=requester_kind)

    async def record_rebound(self, task_id: str, *, assignee: "str | None") -> int:
        """The assignee binding changed (reassign / unbind)."""
        return await self._state_log.append(
            _TASK_REBOUND, task_id=task_id, assignee=assignee)
