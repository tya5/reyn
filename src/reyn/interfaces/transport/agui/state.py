"""STATE_* status read-model + present-on-wire re-guard (ADR-0039 P2).

P1 left the status bar (ctx / cost / token / WaitingOn) read straight off the
registry by duck-typing — fine in-process, but a remote client has no registry
to read. P2 streams the status view over the wire as ``STATE_SNAPSHOT`` (on
connect) + ``STATE_DELTA`` (on change). This module owns three pieces:

- :func:`project_status` — the **read-model projection**: the wire-relevant
  subset of the existing inline status snapshot (the ``_snapshot`` dict the CUI
  already builds). It is a *read-model*, NOT a file mirror — it derives from the
  session's live cost / token / ctx accessors and the current WaitingOn label,
  and carries only what a status panel renders.
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
    "cost_agent",
    "cost_total",
    "agent_tokens",
    "ctx_used",
    "ctx_window",
    "task_count",
    "waiting_on",
)


def project_status(snapshot: "dict | None", *, waiting_on: "str | None" = None) -> dict:
    """Project the inline status snapshot to the wire read-model subset.

    ``snapshot`` is the CUI's ``_snapshot`` dict (or ``None`` when no session is
    attached). ``waiting_on`` is the current WaitingOn label (from the
    chat-event stream) folded in so the remote panel shows the same
    Thinking / Running / Waiting-for-you state the local one does.
    """
    snap = snapshot or {}
    out = {
        "attached_name": snap.get("attached_name"),
        "model": snap.get("model"),
        "cost_agent": snap.get("cost_agent", 0.0),
        "cost_total": snap.get("cost_total", 0.0),
        "agent_tokens": snap.get("agent_tokens", 0),
        "ctx_used": snap.get("ctx_used", 0),
        "ctx_window": snap.get("ctx_window", 0),
        # #ADR-0039 P3: the active-task count rides the wire so the remote inline
        # status bar's `task` chip reaches MAIN-bar parity with local (the task
        # TREE — the dropdown expansion — is NOT projected; a remote task chip is
        # a count only, the tree degrades to empty). The server's status snapshot
        # only carries a non-zero count when its status provider folds in a
        # task-backend poll (endpoint.py) — a bare `_snapshot(registry)` is 0.
        "task_count": snap.get("task_count", 0),
        "waiting_on": waiting_on,
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
    "reguard_nodes",
]
