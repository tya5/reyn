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
from reyn.llm.pricing import CostBreakdown


def _cost_breakdown_wire(breakdown: "CostBreakdown | None") -> "dict | None":
    """#5771 stage②: the ONE place ``project_status`` puts a ``CostBreakdown``
    onto the wire — ``.to_dict()`` (llm/pricing.py), never a second,
    hand-rolled serialization (architect's own explicit instruction on
    #5771: a raw dataclass instance would otherwise reach ``json.dumps``
    with no ``default=`` at the SSE emit boundary and raise ``TypeError``,
    the exact failure the instruction names). ``None`` passes through
    unchanged — a snapshot that never populated this key (a test double,
    or a pre-#5771 producer) degrades to "nothing to show", never a
    fabricated all-zero breakdown."""
    return breakdown.to_dict() if breakdown is not None else None


def project_status(snapshot: "dict | None", *, waiting_on: "str | None" = None) -> dict:
    """Project the inline status snapshot to the wire read-model subset.

    ``snapshot`` is the CUI's ``_snapshot`` dict (or ``None`` when no session is
    attached). ``waiting_on`` is the current WaitingOn label (from the
    audit-event stream) folded in so the remote panel shows the same
    Thinking / Running / Waiting-for-you state the local one does.

    #5098: this dict literal is the read-model's whole wire vocabulary —
    the SOLE source of truth for which keys ride the wire (a private/
    expensive snapshot field, e.g. the bound ``ctx_compaction_status_fn``
    method, can never leak onto the wire just by existing in ``snap``,
    since only a key spelled out below is ever read off it). A prior
    revision kept a separate constant tuple, above this function, listing
    the "intended" wire keys — it was never read by anything (0 consumers,
    confirmed by grep), so a key added there alone silently did nothing;
    this dict was always the only thing that mattered. Removed rather than
    wired up, so there is one declaration, not two that can drift apart.
    """
    snap = snapshot or {}
    out = {
        "attached_name": snap.get("attached_name"),
        "model": snap.get("model"),
        # #5094: the agent roster + the model-class picker's own catalog —
        # all 4 were already computed server-side (``status._snapshot``'s
        # own ``registry.loaded_names()``/``registry.session_tree()``/
        # ``Session.active_model_class()``/``Session.known_model_classes()``)
        # but never forwarded past this projection, so a remote client's
        # agent tab and model-class picker were unconditionally empty
        # regardless of how many agents/sessions or model classes the
        # server actually had (owner live-blocked on this, #5041/#5094).
        # Real per-connection values, same as every other key here — not a
        # graceful-degrade placeholder.
        "agent_names": snap.get("agent_names", []),
        "session_tree": snap.get("session_tree", []),
        # #5729: turn_active/iv_waiting for every loaded session in THIS
        # process (owner ruling B: a remote client sees them even unattached)
        # — same pattern as #5094's session_tree forwarding above, real
        # per-connection data computed server-side
        # (``AgentRegistry.all_sessions_status()``), not a placeholder.
        "all_sessions_status": snap.get("all_sessions_status", []),
        "model_active_class": snap.get("model_active_class"),
        "model_classes": snap.get("model_classes", []),
        # #5185: the same pattern #5094 used above — real per-session data
        # already computed server-side (``status._snapshot``'s own
        # ``_session_visibility_items``/``_session_mcp_subscriptions``) but
        # never forwarded past this projection, so a remote MCP/tool/skill
        # pane was unconditionally "(not wired)"/empty regardless of the
        # session's real state (owner live-observed, #5185).
        # ``visibility_items`` MUST default to ``None``, not ``[]`` — ``None``
        # is itself a real, meaningful value here (#3378: "this session
        # wires no visibility seam"), and defaulting it away would silently
        # turn a genuine "can't say" into a fabricated "nothing is
        # narrowed" the moment this key happened to be absent from `snap`.
        "visibility_items": snap.get("visibility_items"),
        "mcp_subscriptions": snap.get("mcp_subscriptions", []),
        # #5774: the mcp pane's per-server 3(+1)-state probe display
        # (#4401 ②③ — "answered"/"failed"/"not_probed"/"retrying") — a
        # plain list of small dicts (name/state/tool_count/reason/
        # detail), already JSON-safe as-is, no encode step needed. Real,
        # per-connection data (RouterHostAdapter.mcp_probe_snapshot());
        # owner's own stated purpose for #4401 ("tui mcp tab でユーザは
        # 気付けて対処できる") was silently unmet for a remote attach
        # until this key rode the wire — see project_remote_snapshot's
        # own comment for the *_reported flip this pairs with.
        "mcp_probe_states": snap.get("mcp_probe_states", []),
        # #5774: per-session hooks.yaml parse warnings — a plain list of
        # strings, already JSON-safe. Real SESSION state (`Session.
        # hooks_config_warnings`) — unlike `unknown_config_key_count`/
        # `unknown_config_keys` (project_remote_snapshot's own client-
        # local keys, which name the CLIENT's own reyn.yaml and never
        # appear here at all), this one genuinely belongs on the wire.
        "hooks_config_warnings": snap.get("hooks_config_warnings", []),
        "cost_agent": snap.get("cost_agent", 0.0),
        "cost_total": snap.get("cost_total", 0.0),
        # #5771 stage②: the session-cumulative TOTAL (a different fact
        # from cost_agent above — see this project's own #5773 finding,
        # "cost_usd was aliased to cost_agent" — now genuinely wired
        # under its own name, not read off a neighbour).
        "cost_usd": snap.get("cost_usd", 0.0),
        # #5771 stage②: the 3-scope CostBreakdown table (Input/Output/
        # Saved/Saved% rows) the Cost pane already renders locally —
        # encoded via CostBreakdown.to_dict() (llm/pricing.py), the ONE
        # existing serialization, never a second one invented for the
        # wire. None only if the local snapshot itself never populated
        # the key (a test double, or a pre-#5771 producer) — never a
        # graceful-degrade placeholder for a real absence, since the
        # session-scope object is never None once #cost-panel-breakdown
        # actually ran.
        "cost_breakdown_session": _cost_breakdown_wire(snap.get("cost_breakdown_session")),
        "cost_breakdown_agent": _cost_breakdown_wire(snap.get("cost_breakdown_agent")),
        "cost_breakdown_project": _cost_breakdown_wire(snap.get("cost_breakdown_project")),
        # #5771 stage②: (prompt, completion, total) — a plain int triple,
        # already JSON-safe as-is (no CostBreakdown-style encode needed).
        "usage": snap.get("usage", (0, 0, 0)),
        "agent_tokens": snap.get("agent_tokens", 0),
        "ctx_used": snap.get("ctx_used", 0),
        "ctx_window": snap.get("ctx_window", 0),
        # #5774: the single most-recent call's own cache figures
        # (Session.last_call_usage) — a plain int pair, already JSON-safe.
        # This is the LAST key #5773's own axis-split left behind (see
        # read_model.py's own comment at this key for the full "one
        # spelling, two facts" history lead-coder's #5774 follow-up
        # corrected).
        "ctx_recent_usage": snap.get("ctx_recent_usage", (0, 0)),
        # #5771 stage②: cache-hit accounting is genuinely measured
        # LOCALLY (status.py's own inline comment at this key) — now
        # forwarded for real, the same pattern #5094/#5185 already used
        # for the agent roster / visibility keys above.
        "session_cached_tokens": snap.get("session_cached_tokens", 0),
        # #5771 stage②: the current/most-recent turn's own total — None
        # when there is no figure yet (status.py's own convention: never
        # a fabricated 0). turn_usage_fn (the KEYED per-row accessor the
        # gutter uses) is a callable and stays OFF the wire entirely —
        # not read here, not forwarded by project_remote_snapshot either.
        "turn_cost_usd": snap.get("turn_cost_usd"),
        "turn_tokens": snap.get("turn_tokens"),
        "waiting_on": waiting_on,
        # #5050: the pending closed-set intervention (id/prompt/detail/
        # choices), or None — the source ``RemoteReadModel.
        # intervention_head()`` reads instead of an unconditional None
        # (the #4996-family lying-None fix — see that method's own
        # docstring). See ``status._snapshot``'s own docstring for the
        # shape.
        "pending_intervention_head": snap.get("pending_intervention_head"),
        # #5802 (owner-hit: web/connect's /rewind showed the text list
        # only, no picker — the #5773 baseline had declared this
        # "permanently session-local", falsified by the owner's report).
        # The pending command-UI request (currently only ``{"kind":
        # "rewind", "points": [...], "branches": [...], "default_scope":
        # {...} | None}``, #5769) — the source
        # ``RemoteReadModel.pending_command_ui()`` reads instead of an
        # unconditional None. None when nothing is pending — never a
        # fabricated placeholder. Same STATE_SNAPSHOT/STATE_DELTA channel
        # as every other field here; ``points`` can be large (one row per
        # session checkpoint), but STATE_DELTA only carries CHANGED keys
        # (this project_status call's own contract), so this rides the
        # wire only when a picker opens or closes, not every frame — see
        # the delta-not-every-frame test this field's own PR pins.
        #
        # Named ``pending_command_ui_request`` — NOT ``pending_command_
        # ui`` — to avoid colliding with the LITERAL ``"pending_command_
        # ui": <bool>`` key ``snap`` already carries (the
        # ChatReadModelCapabilities FLAG value, spread in by ``status.py``'s
        # own ``_reported_snapshot_keys()``); see that call site's own
        # comment for the full reasoning.
        "pending_command_ui_request": snap.get("pending_command_ui_request"),
        # #3300 P2a: server-authoritative sent-queue state — the
        # undispatched inbox queue (list of {msg_id, chain_id, text}) +
        # whether a turn is currently dispatched. Rides this SAME
        # STATE_SNAPSHOT/STATE_DELTA channel (connect-time snapshot +
        # delta thereafter) so a remote client is late-joiner-safe: a
        # client connecting mid-turn gets the correct queue + turn_active
        # from the snapshot rather than needing to have observed every
        # prior turn_started/user_submitted audit-event.
        "queue": snap.get("queue", []),
        "turn_active": snap.get("turn_active", False),
        # The order-race gate token (#3300 P2a design-pass pin D) — see
        # `RemoteQueueView` below.
        "queue_seq": snap.get("queue_seq", 0),
        # #2280: the durability-halt reason (``None`` while running) —
        # rides this SAME snapshot/delta channel so a remote status panel
        # surfaces a halt proactively too, not only the local in-process
        # one.
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
