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
from dataclasses import dataclass
from pathlib import Path

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog

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

    def load(self, seq: int, session_id: str = "main") -> AgentSnapshot:
        """Load the generation at ``seq`` (raises if absent / schema-mismatch).

        FP-0043 Stage 5: ``session_id`` is the fallback when the on-disk generation
        predates the field; a generation written post-S5 carries its own session_id
        (AgentSnapshot.save) which takes precedence."""
        return AgentSnapshot.load(
            self._agent_name, self._path_for(seq), session_id=session_id,
        )

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


class RewindBeyondRetentionError(Exception):
    """Rewind target is older than the retained WAL (ADR-0038 Stage 1e, D5).

    Rewind is bounded by the retention window — history truncated below the
    WAL's oldest kept seq cannot be reconstructed. Raised with a decision-enabling
    message (set retention deeper) rather than failing silently or reconstructing
    a partial state.
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
    the record sits on an already-abandoned branch, so it is moot (subsumed).

    The composition is well-defined for **both** Phase-1 undo (active target) and
    Phase-2 ``checkout`` (target on a dead branch — guard lifted): ``N < R`` always
    holds (the target is a real prior seq), so no degenerate interval; a later
    record subsumes an intervening one when its ``R`` falls inside the new
    interval, and an older abandonment *resurrects* when the subsuming record is
    itself later abandoned (the checkout-back case). Subsuming and partial nesting
    both fall out of the single latest-first pass.
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


# ── Phase-2 fork: derived branch tree (ADR-0038 D8, #1533) ─────────────────────
#
# Grounded in the SAME abandoned-interval machinery as is_active (inherits the
# 1b-1e correctness), NOT a fresh segment walk. Branch identity:
#   - the **active** branch (id 0) = the current live lineage = every is_active seq
#     (undo continues the active branch; the rewound-past content is NOT root, it's
#     a dead branch — distinguishing undo from a "new branch" was the subtlety
#     e2e's over-include repro exposed);
#   - each **abandoned interval** (N, R) = a dead branch (id R, forked at N) holding
#     the rewound-past content.
# Range-intersection over `[fork_point, head]` over-includes (an active parent's
# range physically spans its abandoned children); membership is interval-resolved.

ACTIVE_BRANCH_ID = 0


@dataclass(frozen=True)
class Branch:
    """One node of the derived branch tree (ADR-0038 D8 Phase-2 fork)."""

    branch_id: int            # 0 = the active live branch; else the reset-record R that orphaned it
    fork_point_seq: int       # where it diverges; active = 0
    head_seq: int             # highest seq owned (active = WAL head; dead = R-1)
    parent_branch_id: int | None  # branch owning fork_point; active = None
    is_active: bool


def _branch_of_seq(seq: int, abandoned: list[tuple[int, int]]) -> int:
    """Owning branch_id of ``seq``: 0 (active) if not abandoned, else the R of the
    tightest abandoned interval ``(N, R)`` containing it (innermost fork = max N)."""
    best: tuple[int, int] | None = None
    for (n, r) in abandoned:
        if n < seq < r and (best is None or n > best[0]):
            best = (n, r)
    return ACTIVE_BRANCH_ID if best is None else best[1]


def branch_ids_for(state_log: StateLog, seqs: "list[int]") -> dict[int, int]:
    """Map each seq → its owning branch_id (lineage-correct membership, #1533 2a→2b).

    ``is_active`` seqs → the active branch (0); a rewound-past seq → the dead branch
    (the reset-record ``R`` that abandoned it). NOT range-intersection (an active
    parent's ``[fork_point, head]`` range physically spans its abandoned children,
    so a naive intersect over-includes — e2e's repro). Grounded in the proven
    ``_abandoned_intervals`` (inherits 1b-1e correctness). ``list_rewind_points``
    tags each row with this so the UX groups by branch_id.
    """
    abandoned = _abandoned_intervals(_rewind_records(state_log))
    return {s: _branch_of_seq(s, abandoned) for s in seqs}


def list_branches(state_log: StateLog) -> list[Branch]:
    """Derive the branch tree (#1533 Phase-2 2a / D8).

    The active branch (id 0) = the live lineage (all is_active seqs). Each abandoned
    interval ``(N, R)`` = a dead branch (id R) forked at N, with ``parent`` = the
    branch owning N (active or an enclosing dead branch → nesting). Returns the
    active branch first, then dead branches ascending by id. Empty WAL → [].
    """
    head = state_log.current_seq
    if head <= 0:
        return []
    abandoned = _abandoned_intervals(_rewind_records(state_log))
    out: list[Branch] = [Branch(
        branch_id=ACTIVE_BRANCH_ID, fork_point_seq=0, head_seq=head,
        parent_branch_id=None, is_active=True,
    )]
    for (n, r) in sorted(abandoned, key=lambda t: t[1]):  # ascending by R (dead-branch id)
        out.append(Branch(
            branch_id=r,
            fork_point_seq=n,
            head_seq=r - 1,                       # top of the rewound-past content (N, R)
            parent_branch_id=_branch_of_seq(n, abandoned),  # branch owning the fork point (nesting)
            is_active=False,
        ))
    return out


def lineage_predecessor(
    state_log: StateLog, candidates: "list[int]", target: int,
) -> int | None:
    """The greatest candidate seq strictly below ``target`` on ``target``'s lineage.

    "Lineage" = ``target``'s branch + its ancestor branches back through each
    fork-point (the active path one would see if checked out to ``target``). Walks
    the derived branch tree (``parent_branch_id`` + ``fork_point_seq``), so when
    ``target`` is the FIRST checkpoint on a forked branch the predecessor correctly
    resolves to the PARENT branch's checkpoint at the fork-point — a same-branch-only
    max would miss it (the over-include sibling trap, #1533 2c). ``None`` when no
    ancestor candidate exists (e.g. ``target`` is the first checkpoint = genesis).

    ``candidates`` is the caller-filtered set (e.g. turn-kind checkpoint seqs);
    this function only resolves lineage ordering, staying kind-agnostic (P7).
    """
    cand = [int(c) for c in candidates]
    if not cand:
        return None
    abandoned = _abandoned_intervals(_rewind_records(state_log))
    by_id = {b.branch_id: b for b in list_branches(state_log)}
    cur: int | None = _branch_of_seq(int(target), abandoned)
    cutoff = int(target)            # collect candidates strictly below this on `cur`
    best: int | None = None
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        for s in cand:
            if s < cutoff and _branch_of_seq(s, abandoned) == cur:
                if best is None or s > best:
                    best = s
        branch = by_id.get(cur)
        if branch is None or branch.parent_branch_id is None:
            break                   # reached the active root — done
        # Jump to the parent at the fork-point: its checkpoints up to and including
        # fork_point are `target`'s ancestors (anything after is a divergent line).
        cutoff = branch.fork_point_seq + 1
        cur = branch.parent_branch_id
    return best


async def checkout(
    state_log: StateLog, *, target_seq: int, supersedes: int | None = None,
) -> int:
    """Append a reset-record to ``target_seq`` UNCONDITIONALLY (Phase-2 D8 fork).

    The unified time-travel primitive: no active-target guard, so it can switch
    the active cut to a seq on a *dead* branch (branch-switch / fork revival).
    ``rewind`` is the active-target-guarded special case (Phase-1 undo).

    Append-only: the just-left future is retained as an inactive branch (P6 / WAL
    stay append-only); ``is_active`` is re-derived from the full reset-record
    chain, so a single record ``(R, target_seq)`` flips the active lineage via the
    latest-first ``_abandoned_intervals`` composition — no new persisted field.
    The reset-record is fsync'd by ``StateLog.append`` *before* any
    reconstruction — the crash-mid-rewind idempotence keystone.

    ``supersedes`` is **audit-only** (records the prior active pointer for the
    branch-tree audit trail); ``is_active`` derivation does not depend on it.
    """
    return await state_log.append(
        REWIND_KIND, target_n=int(target_seq), supersedes=supersedes,
    )


async def rewind(
    state_log: StateLog, *, target_n: int, supersedes: int | None = None,
) -> int:
    """Phase-1 undo: ``checkout`` guarded to an **active** target.

    Validates ``target_n`` is on the active branch, then delegates to
    ``checkout``. The guard lives here (not in ``checkout``) — Phase-1 undo only
    rewinds along the live timeline; Phase-2 ``checkout`` lifts it.

    Raises ``RewindIntoAbandonedError`` when ``target_n`` is on an abandoned branch.
    """
    is_active = _make_is_active(_abandoned_intervals(_rewind_records(state_log)))
    if not is_active(target_n):
        raise RewindIntoAbandonedError(
            f"rewind target seq {target_n} is on an abandoned branch — switching "
            "to an abandoned branch is a Phase-2 fork, not supported by Phase-1 "
            "undo (use checkout). Rewind only to a seq on the active timeline."
        )
    return await checkout(state_log, target_seq=target_n, supersedes=supersedes)


def reconstruct(
    agent_name: str,
    store: SnapshotGenerationStore,
    state_log: StateLog,
    target_seq: int,
    session_id: str = "main",
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
    # FP-0043 Stage 5: the reconstructed base is tagged with session_id so the
    # WAL-delta apply_events below routes ONLY this session's entries into it
    # (the empty fallback for a session with no generation yet; an on-disk
    # generation carries its own session_id which store.load prefers).
    base = (
        store.load(base_seq, session_id=session_id)
        if base_seq is not None
        else AgentSnapshot.empty(agent_name, session_id)
    )

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
