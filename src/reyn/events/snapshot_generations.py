"""Snapshot generations + PITR reconstruct (ADR-0038 Stage 1a).

A :class:`SnapshotGenerationStore` retains full :class:`AgentSnapshot`
generations keyed by the boundary WAL ``seq`` at which each was cut (turn /
plan-step / phase). Point-in-time reconstruction of any seq ``N`` is then:

    reconstruct(N) = nearest generation with applied_seq <= N
                     + forward-replay of WAL entries in (gen.applied_seq, N]

via :meth:`AgentSnapshot.apply_events`. Crash recovery is the special case
``reconstruct(head)``.

Generations are **additive** to the existing single ``snapshot.json`` (Stage 1a
introduces no behavior change): the most-recent generation equals the current
snapshot. Each generation is a full snapshot (ADR-0038 D1 — full, not delta),
written atomically with the same tmp→fsync→rename discipline as
``AgentSnapshot.save``.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog

_GEN_RE = re.compile(r"^gen-(\d+)\.json$")


class SnapshotGenerationStore:
    """Directory of full ``AgentSnapshot`` generations keyed by boundary seq.

    Layout: ``<generations_dir>/gen-<seq>.json`` — one full snapshot per
    boundary. Reuses ``AgentSnapshot.save``/``load`` (atomic write, schema
    versioned).
    """

    def __init__(self, agent_name: str, generations_dir: Path) -> None:
        self._agent_name = agent_name
        self._dir = Path(generations_dir)

    def _path_for(self, seq: int) -> Path:
        return self._dir / f"gen-{seq}.json"

    def record(self, snapshot: AgentSnapshot) -> Path:
        """Persist ``snapshot`` as the generation at ``snapshot.applied_seq``.

        Idempotent for a given seq (re-recording overwrites atomically). The
        seq key is the snapshot's ``applied_seq`` — the boundary at which the
        generation was cut.
        """
        path = self._path_for(snapshot.applied_seq)
        snapshot.save(path)  # tmp → fsync → rename
        return path

    def seqs(self) -> list[int]:
        """Sorted list of generation boundary seqs present on disk."""
        if not self._dir.is_dir():
            return []
        out: list[int] = []
        for child in self._dir.iterdir():
            m = _GEN_RE.match(child.name)
            if m:
                out.append(int(m.group(1)))
        out.sort()
        return out

    def nearest_at_or_below(self, n: int) -> int | None:
        """Highest generation seq ``<= n``, or ``None`` if there is none."""
        candidates = [s for s in self.seqs() if s <= n]
        return candidates[-1] if candidates else None

    def load(self, seq: int) -> AgentSnapshot:
        """Load the generation at ``seq`` (raises if absent / schema-mismatch)."""
        return AgentSnapshot.load(self._agent_name, self._path_for(seq))

    def prune_below(self, min_keep_seq: int) -> int:
        """Drop generations with seq < ``min_keep_seq``. Returns count dropped.

        Used by the retention policy (ADR-0038 D5, Stage 1e) to GC generations
        outside the coarse retention window. Stage 1a ships the primitive;
        wiring lands with the retention policy.
        """
        dropped = 0
        for s in self.seqs():
            if s < min_keep_seq:
                self._path_for(s).unlink(missing_ok=True)
                dropped += 1
        return dropped


def reconstruct(
    agent_name: str,
    store: SnapshotGenerationStore,
    state_log: StateLog,
    target_seq: int,
) -> AgentSnapshot:
    """Reconstruct ``agent_name``'s state as-of WAL ``target_seq`` (PITR).

    = nearest generation ``<= target_seq`` (or empty if none) + forward-replay
    of WAL entries in ``(base.applied_seq, target_seq]``. The returned
    snapshot's ``applied_seq`` is the highest seq ``<= target_seq`` whose effects
    affected this agent (``<= target_seq`` always).

    Crash recovery is ``reconstruct(head)`` where ``head = state_log.current_seq``.
    """
    base_seq = store.nearest_at_or_below(target_seq)
    base = store.load(base_seq) if base_seq is not None else AgentSnapshot.empty(agent_name)

    # Forward-replay the WAL delta, bounded above by target_seq. apply_events
    # already skips seq <= base.applied_seq; we additionally cap at target_seq so
    # reconstruction is point-in-time, not head.
    delta = [
        entry
        for entry in state_log.iter_from(base.applied_seq + 1)
        if isinstance(entry.get("seq"), int) and entry["seq"] <= target_seq
    ]
    base.apply_events(delta)
    return base
