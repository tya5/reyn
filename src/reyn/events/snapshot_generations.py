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


# ── rewind / branch model (ADR-0038 Stage 1b — keystone) ────────────────────

REWIND_KIND = "rewind"


class RewindIntoAbandonedError(Exception):
    """Phase-1 rewind target is on an abandoned branch.

    Phase-1 undo moves HEAD along the *active* timeline. Targeting a seq inside
    an abandoned segment means switching to an abandoned branch — that is a
    Phase-2 *fork* (branch checkout), not Phase-1 undo, so it is rejected with a
    decision-enabling message.
    """


def _rewind_records(state_log: StateLog) -> list[tuple[int, int]]:
    """All rewind reset-records as ``(R, target_n)`` (R = the record's own seq)."""
    out: list[tuple[int, int]] = []
    for e in state_log.iter_from(1):
        if e.get("kind") == REWIND_KIND and isinstance(e.get("seq"), int):
            out.append((e["seq"], int(e["target_n"])))
    return out


def _abandoned_intervals(rewinds: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Open intervals ``(target_n, R)`` abandoned by the rewind chain.

    Resolved **latest-first** (descending by R): a rewind ``(R, N)`` abandons
    ``(N, R)`` *unless* ``R`` itself is already abandoned by a later rewind — i.e.
    the rewind sits on an already-abandoned branch, so its undo is moot. With the
    Phase-1 active-target guard, each rewind targets a then-active seq, so the
    composition is well-defined (subsuming and partial nesting both fall out).
    """
    abandoned: list[tuple[int, int]] = []

    def _is_abandoned(s: int) -> bool:
        return any(lo < s < hi for (lo, hi) in abandoned)

    for (R, N) in sorted(rewinds, key=lambda t: t[0], reverse=True):
        if _is_abandoned(R):
            continue
        abandoned.append((N, R))
    return abandoned


def _make_is_active(abandoned: list[tuple[int, int]]):
    def is_active(seq: int) -> bool:
        return not any(lo < seq < hi for (lo, hi) in abandoned)
    return is_active


def is_active_seq(state_log: StateLog, seq: int) -> bool:
    """True if ``seq`` is on the current active branch (not in any abandoned segment)."""
    return _make_is_active(_abandoned_intervals(_rewind_records(state_log)))(seq)


def active_rewind_target(state_log: StateLog) -> int | None:
    """``target_n`` of the active reset-record, or ``None`` when no rewind exists.

    The active reset-record is the **highest-seq** rewind (latest wins — a later
    rewind can never be abandoned by an earlier one, so the max-R record is always
    the active pointer). Crash recovery uses this to re-materialise the active
    branch as-of-N idempotently (ADR-0038 Stage 1d two-substrate crash-safety).
    """
    records = _rewind_records(state_log)
    if not records:
        return None
    return max(records, key=lambda t: t[0])[1]


async def rewind(
    state_log: StateLog, *, target_n: int, supersedes: int | None = None,
) -> int:
    """Append a rewind reset-record after validating ``target_n`` is active (Phase 1).

    Append-only: the abandoned future is retained as an inactive branch (P6 /
    WAL stay append-only); reconstruction honors the active pointer. The
    reset-record is fsync'd by ``StateLog.append`` *before* any reconstruction —
    the crash-mid-rewind idempotence keystone.

    ``supersedes`` is **audit-only** (records the prior active pointer for the
    branch-tree audit trail); ``is_active`` derivation walks all rewind records and
    does not depend on it.

    Raises ``RewindIntoAbandonedError`` when ``target_n`` is on an abandoned branch.
    """
    is_active = _make_is_active(_abandoned_intervals(_rewind_records(state_log)))
    if not is_active(target_n):
        raise RewindIntoAbandonedError(
            f"rewind target seq {target_n} is on an abandoned branch — switching "
            "to an abandoned branch is a Phase-2 fork, not supported by Phase-1 "
            "undo. Rewind only to a seq on the active timeline."
        )
    return await state_log.append(
        REWIND_KIND, target_n=int(target_n), supersedes=supersedes,
    )


def reconstruct(
    agent_name: str,
    store: SnapshotGenerationStore,
    state_log: StateLog,
    target_seq: int,
) -> AgentSnapshot:
    """Reconstruct ``agent_name``'s state as-of WAL ``target_seq`` (PITR), on the
    **active branch**.

    = nearest **active** generation ``<= target_seq`` (or empty if none) +
    forward-replay of the **active** WAL entries in ``(base.applied_seq,
    target_seq]`` (Stage 1b: entries in abandoned segments are skipped, generations
    cut on an abandoned branch are not used as the base).

    With no rewind records every seq is active, so this is identical to the
    Stage-1a behavior (backward compatible). Crash recovery is
    ``reconstruct(head)`` where ``head = state_log.current_seq`` — which, after a
    rewind, yields the current active-branch state (and collapses to as-of-N when
    the rewind reset-record is itself head).
    """
    abandoned = _abandoned_intervals(_rewind_records(state_log))
    is_active = _make_is_active(abandoned)

    # Base = nearest ACTIVE generation <= target_seq (never an abandoned-branch
    # generation); empty if none.
    base_seq = next(
        (s for s in reversed(store.seqs()) if s <= target_seq and is_active(s)),
        None,
    )
    base = store.load(base_seq) if base_seq is not None else AgentSnapshot.empty(agent_name)

    # Replay the ACTIVE WAL delta, bounded above by target_seq. apply_events skips
    # seq <= base.applied_seq; we additionally cap at target_seq (point-in-time)
    # and skip abandoned-branch entries (active-path honoring).
    delta = [
        entry
        for entry in state_log.iter_from(base.applied_seq + 1)
        if isinstance(entry.get("seq"), int)
        and entry["seq"] <= target_seq
        and is_active(entry["seq"])
    ]
    base.apply_events(delta)
    return base
