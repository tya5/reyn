"""SkillSnapshot — per-skill recovery state for crash recovery.

Lifecycle:
  - Created on `skill_started` WAL event (initial state via ``empty()``)
  - Updated on each phase transition / intervention / step
  - Deleted on `skill_completed` WAL event

Stored at:
  ``.reyn/agents/<agent_name>/state/skills/<run_id>.snapshot.json``

This is a **cache** derived from WAL events and can be reconstructed by
replaying WAL from ``applied_seq=0``. Atomic write (tmp + fsync + rename)
ensures mid-write crash leaves the previous file intact.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

SKILL_SNAPSHOT_VERSION = 1


@dataclass
class SkillSnapshot:
    """Per-skill recovery state — cache derived from WAL events.

    Lifecycle:
      - Created on ``skill_started`` WAL event (initial state)
      - Updated on each phase transition / intervention / step
      - Deleted on ``skill_completed`` WAL event

    Stored at: ``.reyn/agents/<agent_name>/state/skills/<run_id>.snapshot.json``
    """

    skill_run_id: str
    skill_name: str
    skill_input: dict
    applied_seq: int = 0
    current_phase: str = ""
    last_phase_artifact_path: str | None = None
    last_phase_applied_seq: int = 0  # for WAL truncation eligibility
    visit_counts: dict[str, int] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    awaiting_intervention_id: str | None = None
    # R-D16: monotonic timestamp captured when this run started awaiting an
    # intervention (e.g. ``ask_user``). ``None`` when not awaiting. Read by
    # ``AgentRegistry.compute_truncate_floor`` to exclude long-awaiting
    # skills from the WAL truncation floor: a single skill stuck on
    # ``ask_user`` for hours would otherwise pin the floor at its
    # ``last_phase_applied_seq`` indefinitely. Long-await skills accept
    # memo loss for the awaited window in exchange for unbounded WAL.
    # Optional / additive — old snapshots without this field load with
    # ``awaiting_since=None`` (= treated as not awaiting, matches R-D4).
    awaiting_since: float | None = None
    last_committed_step_id: str | None = None  # forward-replay anchor
    # R-D13: when this run was spawned by another skill via the
    # ``run_skill`` op, ``parent_run_id`` records the parent's run_id.
    # ``None`` = top-level skill (user-invoked). The parent / child
    # tree is used for ``/skill list`` display, debug logs, and future
    # cascade-discard semantics. Optional / additive — old snapshots
    # without this field load with ``parent_run_id=None`` (= treated
    # as root, backward compatible).
    parent_run_id: str | None = None

    SCHEMA_VERSION: ClassVar[int] = SKILL_SNAPSHOT_VERSION

    # ── factory ─────────────────────────────────────────────────────────

    @classmethod
    def empty(
        cls, run_id: str, skill_name: str, skill_input: dict
    ) -> "SkillSnapshot":
        """Create a brand-new snapshot with all defaults."""
        return cls(
            skill_run_id=run_id,
            skill_name=skill_name,
            skill_input=dict(skill_input),
        )

    # ── persistence ─────────────────────────────────────────────────────

    @classmethod
    def load(cls, run_id: str, path: Path) -> "SkillSnapshot":
        """Load from ``path``, falling back to a minimal empty snapshot on
        any read or parse error (defensive; forward-compatible with future
        fields).

        ``run_id`` is used as the key if the file is missing or corrupt so
        callers can always get a usable object back.

        PR-resume-ux β U4: when the file is parseable but has a mismatched
        schema version, raises :class:`SchemaVersionError` so the caller
        can refuse to resume rather than silently load stale fields.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls.empty(run_id, "", {})
        if not isinstance(data, dict):
            return cls.empty(run_id, "", {})
        # PR-resume-ux β U4: schema version refuse
        from reyn.core.events.agent_snapshot import SchemaVersionError
        version = data.get("version")
        if version != SKILL_SNAPSHOT_VERSION:
            raise SchemaVersionError(
                f"SkillSnapshot at {path} has version {version!r}, "
                f"expected {SKILL_SNAPSHOT_VERSION}. "
                "Run `reyn chat --reset` to wipe in-flight skill state "
                "(audit logs in .reyn/events/ are preserved)."
            )
        return cls(
            skill_run_id=str(data.get("skill_run_id", run_id)),
            skill_name=str(data.get("skill_name", "")),
            skill_input=dict(data.get("skill_input", {}) or {}),
            applied_seq=int(data.get("applied_seq", 0)),
            current_phase=str(data.get("current_phase", "")),
            last_phase_artifact_path=data.get("last_phase_artifact_path"),
            last_phase_applied_seq=int(data.get("last_phase_applied_seq", 0)),
            visit_counts=dict(data.get("visit_counts", {}) or {}),
            history=list(data.get("history", []) or []),
            awaiting_intervention_id=data.get("awaiting_intervention_id"),
            awaiting_since=data.get("awaiting_since"),
            last_committed_step_id=data.get("last_committed_step_id"),
            parent_run_id=data.get("parent_run_id"),
        )

    def save(self, path: Path) -> None:
        """Persist atomically: write to ``.tmp``, fsync, rename.

        A mid-write crash leaves the previous file intact.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": SKILL_SNAPSHOT_VERSION,
            "skill_run_id": self.skill_run_id,
            "skill_name": self.skill_name,
            "skill_input": self.skill_input,
            "applied_seq": self.applied_seq,
            "current_phase": self.current_phase,
            "last_phase_artifact_path": self.last_phase_artifact_path,
            "last_phase_applied_seq": self.last_phase_applied_seq,
            "visit_counts": self.visit_counts,
            "history": self.history,
            "awaiting_intervention_id": self.awaiting_intervention_id,
            "awaiting_since": self.awaiting_since,
            "last_committed_step_id": self.last_committed_step_id,
            "parent_run_id": self.parent_run_id,
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
