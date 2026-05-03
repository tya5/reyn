"""SkillRegistry — per-agent coordinator for active skill snapshots + WAL.

A `SkillRegistry` owns the lifecycle of in-flight skill runs for a single
agent. It is the only object that mutates per-skill snapshot files and
emits skill-lifecycle WAL events (`skill_started` / `skill_phase_advanced`
/ `skill_completed`). Callers (the runtime, future resume entry) talk to
the registry, not to snapshots directly.

Responsibilities:
  - Allocate / load / save / delete per-skill snapshots
  - Append skill-lifecycle events to the global WAL
  - Surface `list_active()` for AgentRegistry's truncation floor calc
    and for the future resume runtime entry
  - Stay agnostic about phase execution mechanics (kernel / OSRuntime)
    — the registry only sees lifecycle bookmarks, never op execution

Design invariants:
  - Per-skill snapshot is a CACHE; the WAL is the source of truth.
    Crash-loss of the snapshot is recoverable by replaying WAL events
    from `applied_seq=0` for that run_id.
  - Snapshot is mutated only via this registry's methods. External
    code reading the file (e.g. AgentRegistry's truncation floor calc)
    must treat the file as eventually-consistent and fall back to
    safe defaults on parse error.
  - One registry instance per (agent_name, project_root). Process can
    hold many registries — one per agent.
  - All WAL appends use `agent=<agent_name>` so AgentSnapshot's
    `_matches_agent` routes them to the right agent on replay (PR21).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from reyn.skill.skill_snapshot import SkillSnapshot

if TYPE_CHECKING:
    from reyn.events.state_log import StateLog

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Per-agent active-skill coordinator.

    Snapshots live at:
      ``<agent_state_dir>/skills/<run_id>.snapshot.json``

    Where ``agent_state_dir`` is typically
    ``.reyn/agents/<agent_name>/state``.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        agent_state_dir: Path,
        state_log: "StateLog | None" = None,
        truncate_eligible_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """
        truncate_eligible_hook: optional async callback fired *after* each
            ``skill_phase_advanced`` or ``skill_completed`` WAL append.
            Used to trigger ``AgentRegistry.truncate_wal_if_eligible()``
            on semantic boundaries (the recommended trigger pattern from
            the skill-resume design). Hook exceptions are caught and
            logged — truncation is opportunistic, never a reason to fail
            a phase advance. Hook is invoked even when ``state_log`` is
            None so test setups that observe the trigger don't depend on
            WAL wiring.
        """
        self._agent_name = agent_name
        self._state_dir = Path(agent_state_dir)
        self._skills_dir = self._state_dir / "skills"
        self._state_log = state_log
        self._truncate_hook = truncate_eligible_hook
        # In-memory cache: run_id → SkillSnapshot. Populated on start() /
        # load_active(); cleared on complete().
        self._snapshots: dict[str, SkillSnapshot] = {}

    @property
    def state_dir(self) -> Path:
        """Public view of the per-agent state directory (R-D10).

        Used by callers (runtime) that need to write workspace-backed
        side files (e.g. ``llm_results/<args_hash>.json`` for large LLM
        responses) under the same directory tree the registry owns.
        """
        return self._state_dir

    async def _fire_truncate_hook(self, *, trigger: str) -> None:
        """Fire the optional truncate-eligible hook. Defensive: never raise.

        ``trigger`` identifies the WAL event that motivated the call, used
        only for logging. The hook itself is opaque — typically wired to
        AgentRegistry.truncate_wal_if_eligible.
        """
        if self._truncate_hook is None:
            return
        try:
            await self._truncate_hook()
        except Exception as e:  # noqa: BLE001 — never fail caller
            logger.warning(
                "truncate_eligible_hook (%s) raised: %s", trigger, e,
            )

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(
        self,
        *,
        run_id: str,
        skill_name: str,
        skill_input: dict,
    ) -> SkillSnapshot:
        """Begin a new skill run.

        Effects:
          1. Append ``skill_started`` to the WAL
          2. Create the per-skill snapshot file with the WAL seq stamped
             into ``applied_seq`` and ``last_phase_applied_seq``
          3. Cache the snapshot in memory for fast access

        Idempotent on the in-memory cache: starting the same run_id twice
        is a logical error, but for robustness we overwrite the existing
        cache entry (a real implementation would warn at the call site).
        """
        snap = SkillSnapshot.empty(run_id, skill_name, skill_input)
        if self._state_log is not None:
            seq = await self._state_log.append(
                "skill_started",
                target=self._agent_name,
                agent=self._agent_name,
                run_id=run_id,
                skill_name=skill_name,
                skill_input=skill_input,
            )
            snap.applied_seq = seq
            # Stamp the phase-window watermark too: anything before this
            # seq is irrelevant to this run (run hadn't started yet).
            snap.last_phase_applied_seq = seq
        self._save(snap)
        self._snapshots[run_id] = snap
        return snap

    async def advance_phase(
        self,
        *,
        run_id: str,
        next_phase: str,
        last_phase_artifact_path: str | None = None,
    ) -> None:
        """Record a phase transition for an active skill run.

        Updates the snapshot's ``current_phase``, history, visit_counts,
        ``last_phase_artifact_path``, and ``last_phase_applied_seq``
        (which gates per-phase WAL truncation).

        No-op if the run_id is unknown — the caller may legitimately
        advance a phase before the skill has been registered (e.g. for
        a synthetic root phase). Logged at INFO so the situation is
        observable but not fatal.
        """
        snap = self._snapshots.get(run_id)
        if snap is None:
            logger.info(
                "advance_phase: unknown run_id %r — skipping snapshot update",
                run_id,
            )
            return
        if self._state_log is not None:
            seq = await self._state_log.append(
                "skill_phase_advanced",
                target=self._agent_name,
                agent=self._agent_name,
                run_id=run_id,
                next_phase=next_phase,
                last_phase_artifact_path=last_phase_artifact_path,
            )
            snap.applied_seq = seq
            snap.last_phase_applied_seq = seq
        snap.current_phase = next_phase
        snap.history.append(next_phase)
        snap.visit_counts[next_phase] = snap.visit_counts.get(next_phase, 0) + 1
        snap.last_phase_artifact_path = last_phase_artifact_path
        self._save(snap)
        await self._fire_truncate_hook(trigger="skill_phase_advanced")

    async def complete(
        self,
        *,
        run_id: str,
        status: str = "completed",
    ) -> None:
        """Mark a skill run as finished and remove its snapshot.

        ``status`` controls the lifecycle event kind:
          - ``"completed"`` (default): normal end-of-skill, emits ``skill_completed``
          - ``"discarded"``: user-driven abort via PR-resume-ux flow,
            emits ``skill_discarded``

        Deletion order (WAL append before file unlink) means a crash
        between the two leaves the snapshot file orphaned but
        recoverable: next startup sees the lifecycle event in the WAL,
        replays it onto AgentSnapshot (which removes run_id from
        ``active_skill_run_ids``), and the orphan is garbage-collected
        by ``load_active()``.
        """
        if status == "completed":
            kind = "skill_completed"
        elif status == "discarded":
            kind = "skill_discarded"
        else:
            raise ValueError(
                f"complete: invalid status {status!r}; "
                f"expected 'completed' or 'discarded'",
            )
        if self._state_log is not None:
            await self._state_log.append(
                kind,
                target=self._agent_name,
                agent=self._agent_name,
                run_id=run_id,
            )
        snap_path = self._skills_dir / f"{run_id}.snapshot.json"
        try:
            snap_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "complete: cannot remove skill snapshot %s: %s",
                snap_path, e,
            )
        # R-D10: also remove the per-run llm_results/ side directory, if
        # any large LLM responses were written to disk during this run.
        # Lifecycle is bound to the snapshot — deleting them together
        # keeps the state directory tidy and avoids orphaned files.
        from reyn.skill import llm_result_ref
        llm_result_ref.cleanup_for_run(self._state_dir, run_id)
        self._snapshots.pop(run_id, None)
        await self._fire_truncate_hook(trigger=kind)

    # ── read access ──────────────────────────────────────────────────────

    def get(self, run_id: str) -> SkillSnapshot | None:
        """Return the in-memory snapshot for ``run_id``, or None if unknown."""
        return self._snapshots.get(run_id)

    def list_active(self) -> list[str]:
        """Return run_ids of currently-tracked skill runs (in registration order)."""
        return list(self._snapshots.keys())

    # ── persistence ──────────────────────────────────────────────────────

    def load_active(self) -> dict[str, SkillSnapshot]:
        """Discover and load every per-skill snapshot under ``skills/``.

        Used at process startup to repopulate ``_snapshots`` from disk
        before any new skill activity. Files that fail to parse are
        skipped with a warning — the WAL is the source of truth and a
        replay will reconstruct the missing snapshot if needed.

        Idempotent: calling twice replaces the in-memory cache with
        whatever's currently on disk.
        """
        loaded: dict[str, SkillSnapshot] = {}
        if self._skills_dir.is_dir():
            for snap_file in self._skills_dir.glob("*.snapshot.json"):
                run_id = snap_file.stem.removesuffix(".snapshot")
                try:
                    snap = SkillSnapshot.load(run_id, snap_file)
                except Exception as e:  # noqa: BLE001 — defensive
                    logger.warning(
                        "load_active: cannot load %s: %s — skipping",
                        snap_file, e,
                    )
                    continue
                loaded[run_id] = snap
        self._snapshots = loaded
        return dict(loaded)

    def _save(self, snap: SkillSnapshot) -> None:
        path = self._skills_dir / f"{snap.skill_run_id}.snapshot.json"
        snap.save(path)
