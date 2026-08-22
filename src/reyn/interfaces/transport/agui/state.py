"""STATE_* status read-model + present-on-wire re-guard (ADR-0039 P2).

P1 left the status bar (ctx / cost / token / WaitingOn) read straight off the
registry by duck-typing — fine in-process, but a remote client has no registry
to read. P2 streams the status view over the wire as ``STATE_SNAPSHOT`` (on
connect) + ``STATE_DELTA`` (on change). This module owns three pieces:

- :func:`project_status` — the **read-model projection**: the wire-relevant
  subset of the existing inline status snapshot (the ``_snapshot`` dict the CUI
  already builds). It is a *read-model*, NOT a file mirror — it derives from the
  session's live cost / token / ctx accessors and the current WaitingOn label,
  and carries only what a status panel renders. #3300 P2a additionally folds in
  the server-authoritative sent-queue state (``queue`` — the undispatched inbox
  items, ``Session.queued_user_messages()`` — + ``turn_active`` —
  ``Session.turn_active``) onto this SAME snapshot/delta channel, so a queue
  consumer (a test harness today; the P2b sent-queue widget later) is
  late-joiner-safe for free: it need not have observed every prior
  ``user_submitted``/``turn_started`` audit-event, only the current snapshot.
- :class:`StatusModel` — the server-side differ: holds the last projected view
  and yields the changed keys (:meth:`delta`) so the emitter streams a compact
  ``STATE_DELTA`` instead of a full snapshot on every tick.
- :class:`RemoteStatusView` — the client-side reader: applies a snapshot then
  deltas so the remote status panel reflects the SERVER's values.

Plus :func:`reguard_nodes` — the **per-connection re-guard hook** (A5): render
nodes are already neutralized at construction (inert-on-wire), but a
heterogeneous-surface client re-runs the surface neutralizer over every leaf at
the transport edge as defense in depth (idempotent for the terminal surface).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reyn.core.present.guard import get_neutralizer

# The status keys that ride the wire — the read-model's whole vocabulary. Kept
# as an explicit projection so a private/expensive snapshot field (e.g. the
# bound ``ctx_compaction_status_fn`` method) can never leak onto the wire.
_WIRE_KEYS = (
    "attached_name",
    "model",
    # #5094: the agent roster + the model-class picker's own catalog — all
    # 4 were already computed server-side (``status._snapshot``'s own
    # ``registry.loaded_names()``/``registry.session_tree()``/
    # ``Session.active_model_class()``/``Session.known_model_classes()``)
    # but never forwarded past this filter, so a remote client's agent tab
    # and model-class picker were unconditionally empty regardless of how
    # many agents/sessions or model classes the server actually had (owner
    # live-blocked on this, #5041/#5094). Real per-connection values, same
    # as every other key here — not a graceful-degrade placeholder.
    "agent_names",
    "session_tree",
    "model_active_class",
    "model_classes",
    "cost_agent",
    "cost_total",
    "agent_tokens",
    "ctx_used",
    "ctx_window",
    "waiting_on",
    # #5050: the pending closed-set intervention (id/prompt/detail/choices),
    # or None — the source ``RemoteReadModel.intervention_head()`` reads
    # instead of an unconditional None (the #4996-family lying-None fix —
    # see that method's own docstring). See ``status._snapshot``'s own
    # docstring for the shape.
    "pending_intervention_head",
    # #3300 P2a: server-authoritative sent-queue state — the undispatched
    # inbox queue (list of {msg_id, chain_id, text}) + whether a turn is
    # currently dispatched. Rides this SAME STATE_SNAPSHOT/STATE_DELTA
    # channel (connect-time snapshot + delta thereafter) so a remote client
    # is late-joiner-safe: a client connecting mid-turn gets the correct
    # queue + turn_active from the snapshot rather than needing to have
    # observed every prior turn_started/user_submitted audit-event.
    "queue",
    "turn_active",
    # The order-race gate token (#3300 P2a design-pass pin D) — see
    # `RemoteQueueView` below.
    "queue_seq",
    # #2280: the durability-halt reason (``None`` while running) — rides this
    # SAME snapshot/delta channel so a remote status panel surfaces a halt
    # proactively too, not only the local in-process one.
    "halted_reason",
)


def project_status(snapshot: "dict | None", *, waiting_on: "str | None" = None) -> dict:
    """Project the inline status snapshot to the wire read-model subset.

    ``snapshot`` is the CUI's ``_snapshot`` dict (or ``None`` when no session is
    attached). ``waiting_on`` is the current WaitingOn label (from the
    audit-event stream) folded in so the remote panel shows the same
    Thinking / Running / Waiting-for-you state the local one does.
    """
    snap = snapshot or {}
    out = {
        "attached_name": snap.get("attached_name"),
        "model": snap.get("model"),
        # #5094: see _WIRE_KEYS above.
        "agent_names": snap.get("agent_names", []),
        "session_tree": snap.get("session_tree", []),
        "model_active_class": snap.get("model_active_class"),
        "model_classes": snap.get("model_classes", []),
        "cost_agent": snap.get("cost_agent", 0.0),
        "cost_total": snap.get("cost_total", 0.0),
        "agent_tokens": snap.get("agent_tokens", 0),
        "ctx_used": snap.get("ctx_used", 0),
        "ctx_window": snap.get("ctx_window", 0),
        "waiting_on": waiting_on,
        # #5050: see _WIRE_KEYS above.
        "pending_intervention_head": snap.get("pending_intervention_head"),
        # #3300 P2a: sent-queue state, see _WIRE_KEYS above.
        "queue": snap.get("queue", []),
        "turn_active": snap.get("turn_active", False),
        "queue_seq": snap.get("queue_seq", 0),
        # #2280: see _WIRE_KEYS above.
        "halted_reason": snap.get("halted_reason"),
    }
    return out


@dataclass
class StatusModel:
    """Server-side status differ: last-projected view → changed-key deltas."""

    _last: dict = field(default_factory=dict)

    def snapshot(self, projected: dict) -> dict:
        """Record ``projected`` as the baseline and return it (the full view)."""
        self._last = dict(projected)
        return dict(projected)

    def delta(self, projected: dict) -> dict:
        """Return only the keys whose value changed since the last snapshot/delta.

        Empty dict when nothing changed — the emitter skips the STATE_DELTA emit
        so an idle stream stays quiet.
        """
        changed = {k: v for k, v in projected.items() if self._last.get(k, _UNSET) != v}
        if changed:
            self._last.update(changed)
        return changed


@dataclass
class RemoteStatusView:
    """Client-side status reader: apply a snapshot, then deltas, and read back.

    The remote status panel renders off :attr:`values`; :meth:`apply_snapshot`
    replaces it wholesale (connect / reconnect) and :meth:`apply_delta` merges
    changed keys — so the panel always reflects the SERVER's status values.
    """

    values: dict = field(default_factory=dict)

    def apply_snapshot(self, snapshot: dict) -> None:
        self.values = dict(snapshot)

    def apply_delta(self, delta: dict) -> None:
        self.values.update(delta)

    def get(self, key: str, default=None):
        return self.values.get(key, default)


@dataclass
class RemoteQueueView:
    """Client-side sent-queue reader (#3300 P2a) — merges a ``STATE_SNAPSHOT``
    queue baseline with the granular ``user_submitted`` (enqueue) /
    ``turn_started`` (dispatch) audit-event deltas, using the **seq-gate**
    order-race protocol (design-pass pin D).

    Why a seq gate: ``RemoteStatusView`` above is safe because its delta is
    always the SERVER's freshly-recomputed full value, diffed against its own
    last-sent view (self-healing on every frame, no stale-add). A consumer
    that instead reconstructs the queue from the granular
    ``user_submitted``/``turn_started`` events ONE ITEM AT A TIME (e.g. for a
    smooth enter/promote UI transition, P2b) has no such self-healing — it
    must resolve the classic race directly: a client that reads a
    ``STATE_SNAPSHOT`` AFTER an item was already dispatched (so the snapshot's
    ``queue`` no longer contains it) must not let an out-of-order / stale
    ``user_submitted`` delta for that SAME item resurrect it.

    Protocol: every mutation (enqueue via ``user_submitted``, dispatch via
    ``turn_started``) is stamped with a strictly-monotonic ``seq``
    (``Session._bump_queue_seq``); ``STATE_SNAPSHOT`` carries the current
    counter value as ``queue_seq``. This view tracks the highest ``seq`` it
    has applied (seeded from ``queue_seq`` on :meth:`apply_snapshot`) and
    discards any delta whose ``seq`` is not strictly greater. Because a
    dispatch's ``seq`` is always greater than its own item's enqueue ``seq``
    (single monotonic counter, same session, same event loop), and a snapshot
    taken after that dispatch carries a ``queue_seq`` at least as large as the
    dispatch's — a stale enqueue for an already-dispatched item can never pass
    the gate. This holds for ANY interleaving of the snapshot read and the
    delta stream: no duplicate (a replayed delta's ``seq`` was already
    applied), no resurrection-after-dispatch (the above), no loss (a delta
    whose ``seq`` IS new is always applied).
    """

    items: dict = field(default_factory=dict)  # msg_id -> {msg_id, chain_id, text}
    turn_active: bool = False
    _last_seq: int = 0

    def apply_snapshot(self, *, queue: "list[dict]", turn_active: bool, queue_seq: int) -> None:
        """Seed the view from a ``STATE_SNAPSHOT`` — replaces the queue
        wholesale and seeds the seq gate so no earlier-or-equal delta can
        still mutate a state this snapshot already supersedes."""
        self.items = {
            item["msg_id"]: dict(item) for item in queue if item.get("msg_id")
        }
        self.turn_active = turn_active
        self._last_seq = queue_seq

    def apply_user_submitted(self, *, msg_id: str, chain_id: "str | None", text: str, seq: int) -> bool:
        """Apply an enqueue delta; returns False (no-op) if the seq gate
        rejects it as already reflected by a prior snapshot/delta."""
        if seq <= self._last_seq:
            return False
        self.items[msg_id] = {"msg_id": msg_id, "chain_id": chain_id, "text": text}
        self._last_seq = seq
        return True

    def apply_turn_started(self, *, chain_id: "str | None", seq: int) -> bool:
        """Apply a dispatch delta (removes the queued item matching
        ``chain_id``, if any); returns False (no-op) if the seq gate rejects
        it as already reflected."""
        if seq <= self._last_seq:
            return False
        for msg_id, item in list(self.items.items()):
            if item.get("chain_id") == chain_id:
                del self.items[msg_id]
        self._last_seq = seq
        return True

    def apply_inbox_cancel(self, *, msg_id: str, seq: int) -> bool:
        """Apply a cancel-by-id delta (#3300 P3 Y-server) — removes the
        queued item BY ITS OWN msg_id (unlike ``apply_turn_started``, which
        matches by ``chain_id``: a cancel targets one specific queued item,
        never a whole chain). Returns ``False`` (no-op) if the seq gate
        rejects it as already reflected by a prior snapshot/delta — same
        order-race protocol as ``apply_user_submitted``/``apply_turn_started``
        (design-pass pin D): exclusive with a ``turn_started`` for the same
        item (the server guarantees only one of the two ever fires,
        issue #3300 owner addendum §6a), so no double-removal ambiguity."""
        if seq <= self._last_seq:
            return False
        self.items.pop(msg_id, None)
        self._last_seq = seq
        return True

    def apply_turn_active(self, turn_active: bool) -> None:
        """Apply a plain turn-active flip (e.g. from a ``turn_settled``
        audit-event or a ``STATE_DELTA``'s ``turn_active`` key) — not
        seq-gated: idempotent boolean, no resurrection risk."""
        self.turn_active = turn_active

    def queue(self) -> "list[dict]":
        """The current queue view, insertion order."""
        return list(self.items.values())


def reguard_nodes(nodes: "list[dict]", *, surface: str = "terminal") -> list[dict]:
    """Re-run the surface neutralizer over every leaf string in render nodes (A5).

    Nodes are already inert at construction; this is the per-connection edge
    re-guard for a heterogeneous-surface client — idempotent for a leaf the
    construction seam already neutralized, but load-bearing for a client whose
    upstream did not (or neutralized for a different surface). Structure is
    preserved; only leaf ``str`` values are passed through
    ``get_neutralizer(surface).neutralize``.
    """
    neutralizer = get_neutralizer(surface)

    def _walk(value):
        if isinstance(value, str):
            cleaned, _stripped = neutralizer.neutralize(value)
            return cleaned
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return [_walk(node) for node in nodes]


_UNSET = object()


__all__ = [
    "project_status",
    "StatusModel",
    "RemoteStatusView",
    "RemoteQueueView",
    "reguard_nodes",
]
