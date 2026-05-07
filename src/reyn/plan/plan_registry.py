"""PlanRegistry — per-agent coordinator for active plan snapshots.

ADR-0023 §3.1 + §3.4. Mirrors :class:`SkillRegistry` shape. Owns the
lifecycle of per-plan snapshot files (create on ``plan_started``,
mutate on each ``plan_step_*`` event, delete on
``plan_completed`` / ``plan_aborted``).

Responsibilities (Step 3 of the migration path):
  - Allocate / load / save / delete per-plan snapshots
  - Provide snapshot mutation methods for step lifecycle events
  - Surface ``list_active()`` for AgentRegistry's truncation floor
    calculation and the future resume coordinator
  - Stay agnostic about WAL emission (= Phase 1's SnapshotJournal owns
    ``plan_started`` / ``plan_completed`` / ``plan_aborted`` WAL appends,
    Step 4 will route ``plan_step_*`` WAL appends through SnapshotJournal
    too — PlanRegistry only sees the resulting ``applied_seq`` from those
    appends).

Design invariants (mirror SkillRegistry):
  - Per-plan snapshot is a CACHE; the WAL is the source of truth.
    Crash-loss of the snapshot is recoverable by replaying WAL events
    from ``applied_seq=0`` for that plan_id.
  - Snapshot is mutated only via this registry's methods.
  - One registry instance per (agent_name, project_root). Process can
    hold many registries — one per agent.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from reyn.plan.decomposition import delete_decomposition
from reyn.plan.plan_snapshot import PlanSnapshot, plan_snapshot_path

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Step 3: bound the persisted text of a step result so a multi-page
# scrape doesn't blow up the snapshot file. Phase 2 v1 uses inline
# truncation (= ADR-0023 "Open issues: Step result size cap"). Future
# work may spill to side files mirroring R-D10's llm_results pattern.
_STEP_RESULT_MAX_CHARS = 32_768
_STEP_RESULT_TRUNC_SUFFIX = "\n[truncated]"


def _bound_step_result(text: str) -> str:
    """Bound a step result text to keep snapshot files small (= ADR-0023
    "Open issues: Step result size cap")."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= _STEP_RESULT_MAX_CHARS:
        return text
    cap = _STEP_RESULT_MAX_CHARS - len(_STEP_RESULT_TRUNC_SUFFIX)
    return text[:cap] + _STEP_RESULT_TRUNC_SUFFIX


class PlanRegistry:
    """Per-agent active-plan coordinator.

    Snapshots live at::

        <agent_state_dir>/plans/<plan_id>.snapshot.json

    Where ``agent_state_dir`` is typically
    ``.reyn/agents/<agent_name>/state``.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        agent_state_dir: Path,
        truncate_eligible_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """
        truncate_eligible_hook: optional async callback fired *after* each
            ``plan_step_completed`` / ``plan_completed`` mutation. Used to
            trigger ``AgentRegistry.truncate_wal_if_eligible()`` on
            semantic boundaries (= mirror SkillRegistry's hook). Hook
            exceptions are caught and logged; truncation is opportunistic.
        """
        self._agent_name = agent_name
        self._state_dir = Path(agent_state_dir)
        self._truncate_hook = truncate_eligible_hook
        # In-memory cache: plan_id → PlanSnapshot. Populated on start() /
        # load_active(); cleared on complete().
        self._snapshots: dict[str, PlanSnapshot] = {}

    @property
    def state_dir(self) -> Path:
        """Public view of the per-agent state directory."""
        return self._state_dir

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def _fire_truncate_hook(self, *, trigger: str) -> None:
        """Fire the optional truncate-eligible hook. Defensive: never raise."""
        if self._truncate_hook is None:
            return
        try:
            await self._truncate_hook()
        except Exception as e:  # noqa: BLE001 — never fail caller
            logger.warning(
                "truncate_eligible_hook (%s) raised: %s", trigger, e,
            )

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(
        self,
        *,
        plan_id: str,
        chain_id: str,
        goal: str,
        applied_seq: int,
        decomposition_artifact_path: str | None = None,
        steps_serialized: list[dict] | None = None,
        parent_skill_run_id: str | None = None,
    ) -> PlanSnapshot:
        """Begin a new plan run.

        Effects:
          1. Create the per-plan snapshot file with ``applied_seq`` and
             ``last_step_applied_seq`` stamped at the WAL seq the caller
             received from the corresponding ``plan_started`` append.
          2. Cache the snapshot in memory.

        ``applied_seq`` is the seq returned by the caller's
        ``state_log.append("plan_started", ...)``; PlanRegistry does NOT
        emit WAL events for plan-level lifecycle (= Phase 1's
        SnapshotJournal owns that path).

        Idempotent on the in-memory cache: starting the same plan_id twice
        overwrites the existing entry.
        """
        snap = PlanSnapshot.empty(
            plan_id=plan_id,
            agent_name=self._agent_name,
            chain_id=chain_id,
            goal=goal,
        )
        snap.applied_seq = applied_seq
        snap.last_step_applied_seq = applied_seq
        snap.decomposition_artifact_path = decomposition_artifact_path
        snap.steps_serialized = list(steps_serialized or [])
        snap.parent_skill_run_id = parent_skill_run_id
        self._save(snap)
        self._snapshots[plan_id] = snap
        return snap

    async def complete(
        self,
        *,
        plan_id: str,
        status: str = "completed",
        delete_artifact: bool = True,
    ) -> None:
        """Mark a plan run as finished and remove its snapshot.

        ``status`` is currently advisory (= "completed" / "aborted" /
        "discarded"); the WAL event kind is determined by the caller
        (Phase 1 SnapshotJournal). It's accepted here for symmetry with
        :meth:`SkillRegistry.complete` and surfaced to the truncate hook.

        ``delete_artifact``: when True (default), also remove the
        decomposition artifact (= P5 cleanup, mirrors ADR-0023 §3.4
        finally-clause). Set False for the rare case where the caller
        wants to preserve the artifact for forensics.

        Lifecycle ordering: the caller appends the lifecycle WAL event
        first (= ``plan_completed`` / ``plan_aborted``); this method
        runs after, removing the snapshot file. A crash between the two
        leaves the snapshot orphaned but recoverable: next startup sees
        the lifecycle event in the WAL, replays it onto AgentSnapshot
        (which removes plan_id from ``active_plan_ids``), and the
        orphan is garbage-collected by ``load_active()`` cleanup.
        """
        snap_path = plan_snapshot_path(self._state_dir, plan_id)
        try:
            snap_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "complete: cannot remove plan snapshot %s: %s",
                snap_path, e,
            )
        if delete_artifact:
            try:
                delete_decomposition(self._state_dir, plan_id)
            except OSError as e:
                logger.warning(
                    "complete: cannot remove decomposition artifact for %s: %s",
                    plan_id, e,
                )
        self._snapshots.pop(plan_id, None)
        await self._fire_truncate_hook(trigger=status)

    # ── per-step mutations (Step 4 will wire WAL appends) ────────────────

    def record_step_started(
        self, *, plan_id: str, step_id: str, applied_seq: int,
    ) -> None:
        """Record the executor moving onto ``step_id`` (forward-replay anchor).

        Bumps ``applied_seq`` but **not** ``last_step_applied_seq`` —
        ``plan_step_started`` doesn't represent durable progress
        (= mirror SkillRegistry's ``step_started`` non-bump).
        """
        snap = self._snapshots.get(plan_id)
        if snap is None:
            logger.info(
                "record_step_started: unknown plan_id %r — skipping", plan_id,
            )
            return
        snap.applied_seq = max(snap.applied_seq, applied_seq)
        snap.current_step_id = step_id
        self._save(snap)

    async def record_step_completed(
        self,
        *,
        plan_id: str,
        step_id: str,
        applied_seq: int,
        result_text: str,
    ) -> None:
        """Record durable step completion.

        Bumps ``applied_seq`` AND ``last_step_applied_seq`` (= durable
        progress, gates WAL truncation per ADR-0023 §3.1). Stores
        bounded ``result_text`` in ``step_results``.
        """
        snap = self._snapshots.get(plan_id)
        if snap is None:
            logger.info(
                "record_step_completed: unknown plan_id %r — skipping", plan_id,
            )
            return
        snap.applied_seq = max(snap.applied_seq, applied_seq)
        snap.last_step_applied_seq = max(snap.last_step_applied_seq, applied_seq)
        snap.step_results[step_id] = _bound_step_result(result_text)
        snap.last_committed_step_id = step_id
        if snap.current_step_id == step_id:
            # Clear forward-replay anchor: the executor is between steps.
            snap.current_step_id = None
        self._save(snap)
        await self._fire_truncate_hook(trigger="plan_step_completed")

    async def record_step_failed(
        self,
        *,
        plan_id: str,
        step_id: str,
        applied_seq: int,
        error_repr: str,
    ) -> None:
        """Record a step failure.

        Bumps ``applied_seq`` AND ``last_step_applied_seq`` —
        conservative per ADR-0023 §3.1 (= a recorded failure is real
        progress that shouldn't be replayed without policy intervention).
        """
        snap = self._snapshots.get(plan_id)
        if snap is None:
            logger.info(
                "record_step_failed: unknown plan_id %r — skipping", plan_id,
            )
            return
        snap.applied_seq = max(snap.applied_seq, applied_seq)
        snap.last_step_applied_seq = max(snap.last_step_applied_seq, applied_seq)
        snap.step_failures[step_id] = error_repr
        if snap.current_step_id == step_id:
            snap.current_step_id = None
        self._save(snap)
        await self._fire_truncate_hook(trigger="plan_step_failed")

    def record_child_spawned(
        self, *, plan_id: str, step_id: str, child_run_id: str,
    ) -> None:
        """Record that ``step_id`` spawned skill_run ``child_run_id``.

        Used by the resume coordinator (Step 7) to coordinate adopt-vs-
        cancel decisions with the existing skill_resume infrastructure.
        """
        snap = self._snapshots.get(plan_id)
        if snap is None:
            logger.info(
                "record_child_spawned: unknown plan_id %r — skipping", plan_id,
            )
            return
        snap.spawned_skill_run_ids[step_id] = child_run_id
        self._save(snap)

    # ── read access ──────────────────────────────────────────────────────

    def get(self, plan_id: str) -> PlanSnapshot | None:
        """Return the in-memory snapshot for ``plan_id``, or None if unknown."""
        return self._snapshots.get(plan_id)

    def list_active(self) -> list[str]:
        """Return plan_ids of currently-tracked plans (in registration order)."""
        return list(self._snapshots.keys())

    # ── persistence ──────────────────────────────────────────────────────

    def load_active(self) -> dict[str, PlanSnapshot]:
        """Discover and load every per-plan snapshot under ``plans/``.

        Used at process startup to repopulate ``_snapshots`` from disk
        before any new plan activity. Files that fail to parse are
        skipped with a warning — the WAL is the source of truth.

        Idempotent: calling twice replaces the in-memory cache with
        whatever's currently on disk.
        """
        loaded: dict[str, PlanSnapshot] = {}
        plans_dir = self._state_dir / "plans"
        if plans_dir.is_dir():
            for snap_file in plans_dir.glob("*.snapshot.json"):
                # Skip the per-plan directories' decomposition.json siblings
                # (= they live under plans/<plan_id>/, not plans/*.snapshot.json).
                plan_id = snap_file.stem.removesuffix(".snapshot")
                try:
                    snap = PlanSnapshot.load(plan_id, snap_file)
                except Exception as e:  # noqa: BLE001 — defensive
                    logger.warning(
                        "load_active: cannot load %s: %s — skipping",
                        snap_file, e,
                    )
                    continue
                loaded[plan_id] = snap
        self._snapshots = loaded
        return dict(loaded)

    def _save(self, snap: PlanSnapshot) -> None:
        path = plan_snapshot_path(self._state_dir, snap.plan_id)
        snap.save(path)


__all__ = ["PlanRegistry"]
