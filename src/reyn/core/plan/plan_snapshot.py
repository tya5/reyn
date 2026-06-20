"""PlanSnapshot — per-plan recovery state for plan-mode crash recovery.

ADR-0023 §3.1. Mirrors :class:`SkillSnapshot` shape (= cache derived
from WAL events; rebuildable via WAL replay; atomic save).

Lifecycle:
  - Created on ``plan_started`` WAL event (initial state via
    :meth:`empty`).
  - Updated on each ``plan_step_completed`` / ``plan_step_failed``
    (= ``last_step_applied_seq`` bumped, ``step_results`` /
    ``step_failures`` populated).
  - Deleted on ``plan_completed`` / ``plan_aborted`` (= ordering: WAL
    append first, then ``unlink(missing_ok=True)``).

Stored at::

    .reyn/agents/<agent_name>/state/plans/<plan_id>.snapshot.json

Sibling to the per-plan directory ``.reyn/agents/<agent>/state/plans/<plan_id>/``
(= which holds the decomposition artifact). Snapshot file lives one
level up so the directory's contents are exclusively per-plan
artifacts.

``PLAN_SNAPSHOT_VERSION = 1``. **Not** linked to ``AgentSnapshot``'s
version — Phase 1's ``active_plan_ids`` field is unchanged, so existing
agent snapshots load without migration.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

PLAN_SNAPSHOT_VERSION = 1


@dataclass
class PlanSnapshot:
    """Per-plan recovery state — cache derived from WAL events.

    Field semantics (ADR-0023 §3.1):

    - ``applied_seq`` — high-water mark of WAL events reflected in this
      snapshot (= ADR-0001 watermark).
    - ``last_step_applied_seq`` — WAL truncation gate; bumped on
      ``plan_started`` (initial stamp), ``plan_step_completed`` (durable
      progress), ``plan_step_failed`` (conservative — failure is real
      progress that shouldn't be replayed without policy intervention).
      ``plan_step_started`` does NOT bump (= mirrors ``step_started``
      for skills).
    - ``decomposition_artifact_path`` — canonical SSoT for the plan
      shape on resume (= P5). When ``None``, ``steps_serialized`` is the
      fallback.
    - ``steps_serialized`` — inline fallback when artifact unreadable.
      List of dicts in the same shape as the artifact's ``steps`` field.
    - ``step_results`` — text outputs of completed steps, keyed by
      ``step_id``. The memoized values that resume serves on hit.
    - ``step_failures`` — error reprs of failed steps, keyed by
      ``step_id``.
    - ``current_step_id`` — forward-replay anchor (= the step the
      runtime was executing when the snapshot was last written).
    - ``last_committed_step_id`` — the most recently completed step's
      id (= mirror of skill field).
    - ``spawned_skill_run_ids`` — ``step_id → child_run_id`` for plan
      steps that spawned skills via ``invoke_skill``. Used by the
      resume coordinator to coordinate adopt-vs-cancel decisions with
      the existing ``skill_resume`` infrastructure.
    - ``parent_skill_run_id`` — ADR-0017 lineage analog. Currently
      always ``None`` (= chat-router is the only plan tool surface),
      but kept for forward compatibility.
    - ``usage_tokens_so_far`` — optional cost bookkeeping snapshot.
    """

    plan_id: str
    agent_name: str
    chain_id: str
    goal: str
    applied_seq: int = 0
    last_step_applied_seq: int = 0
    decomposition_artifact_path: str | None = None
    steps_serialized: list[dict] = field(default_factory=list)
    step_results: dict[str, str] = field(default_factory=dict)
    # ADR-0024: large step results (> threshold) spill to
    # ``state/plans/<plan_id>/step_results/<step_id>.txt``; this dict
    # holds the per-plan-dir-relative path. Read access via
    # :func:`get_step_result` transparently resolves the file. Empty
    # for old snapshots and for plans whose every step is small —
    # backward-compatible additive field, no PLAN_SNAPSHOT_VERSION
    # bump needed.
    step_result_refs: dict[str, str] = field(default_factory=dict)
    # ADR-0025: per-step recorded LLM calls within the sub-loop.
    # Populated by ``SubLoopMemoProvider.record`` so a crash mid-step
    # doesn't re-pay the LLM cost on resume. Each entry is a list of
    # records; each record has the shape:
    #   {args_hash, inline, ref, usage}
    # Inline records hold the full LLMToolCallResult dict (≤32 KB);
    # spilled records hold None for inline and a relative path for ref
    # (= ADR-0024 spill pattern; same per-plan workspace dir, cleaned
    # up by delete_plan_workspace).
    step_llm_calls: dict[str, list[dict]] = field(default_factory=dict)
    step_failures: dict[str, str] = field(default_factory=dict)
    current_step_id: str | None = None
    last_committed_step_id: str | None = None
    spawned_skill_run_ids: dict[str, str] = field(default_factory=dict)
    parent_skill_run_id: str | None = None
    usage_tokens_so_far: dict | None = None

    SCHEMA_VERSION: ClassVar[int] = PLAN_SNAPSHOT_VERSION

    # ── factory ─────────────────────────────────────────────────────────

    @classmethod
    def empty(
        cls,
        *,
        plan_id: str,
        agent_name: str,
        chain_id: str,
        goal: str,
    ) -> "PlanSnapshot":
        """Create a brand-new snapshot at ``plan_started`` time."""
        return cls(
            plan_id=plan_id,
            agent_name=agent_name,
            chain_id=chain_id,
            goal=goal,
        )

    # ── persistence ─────────────────────────────────────────────────────

    @classmethod
    def load(cls, plan_id: str, path: Path) -> "PlanSnapshot":
        """Load from ``path``.

        On missing or unparseable file returns a minimal empty snapshot
        keyed by ``plan_id`` so callers always get a usable object back.

        On a parseable file with mismatched ``schema_version`` raises
        :class:`SchemaVersionError` (= mirrors :meth:`SkillSnapshot.load`
        precedent so the caller refuses to resume rather than silently
        load stale fields).
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls.empty(
                plan_id=plan_id, agent_name="", chain_id="", goal=""
            )
        if not isinstance(data, dict):
            return cls.empty(
                plan_id=plan_id, agent_name="", chain_id="", goal=""
            )
        from reyn.core.events.agent_snapshot import SchemaVersionError

        version = data.get("schema_version")
        if version != PLAN_SNAPSHOT_VERSION:
            raise SchemaVersionError(
                f"PlanSnapshot at {path} has version {version!r}, "
                f"expected {PLAN_SNAPSHOT_VERSION}. "
                "Run `reyn chat --reset` to wipe in-flight plan state "
                "(audit logs in .reyn/events/ are preserved)."
            )
        def _coerce_int(v: object) -> int:
            # A version-matched but hand-edited / corrupted snapshot may carry a
            # null / non-numeric seq; .get(k, 0) only defaults a *missing* key.
            # Mirrors the #1906 TokenUsage fix.
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        return cls(
            plan_id=str(data.get("plan_id", plan_id)),
            agent_name=str(data.get("agent_name", "")),
            chain_id=str(data.get("chain_id", "")),
            goal=str(data.get("goal", "")),
            applied_seq=_coerce_int(data.get("applied_seq", 0)),
            last_step_applied_seq=_coerce_int(data.get("last_step_applied_seq", 0)),
            decomposition_artifact_path=data.get("decomposition_artifact_path"),
            steps_serialized=list(data.get("steps_serialized", []) or []),
            step_results=dict(data.get("step_results", {}) or {}),
            step_result_refs=dict(data.get("step_result_refs", {}) or {}),
            step_llm_calls=dict(data.get("step_llm_calls", {}) or {}),
            step_failures=dict(data.get("step_failures", {}) or {}),
            current_step_id=data.get("current_step_id"),
            last_committed_step_id=data.get("last_committed_step_id"),
            spawned_skill_run_ids=dict(
                data.get("spawned_skill_run_ids", {}) or {}
            ),
            parent_skill_run_id=data.get("parent_skill_run_id"),
            usage_tokens_so_far=data.get("usage_tokens_so_far"),
        )

    def save(self, path: Path) -> None:
        """Persist atomically: write to ``.tmp``, ``fsync``, ``rename``.

        A mid-write crash leaves the previous file intact. Mirrors
        :meth:`SkillSnapshot.save` recipe verbatim.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "schema_version": PLAN_SNAPSHOT_VERSION,
            "plan_id": self.plan_id,
            "agent_name": self.agent_name,
            "chain_id": self.chain_id,
            "goal": self.goal,
            "applied_seq": self.applied_seq,
            "last_step_applied_seq": self.last_step_applied_seq,
            "decomposition_artifact_path": self.decomposition_artifact_path,
            "steps_serialized": self.steps_serialized,
            "step_results": self.step_results,
            "step_result_refs": self.step_result_refs,
            "step_llm_calls": self.step_llm_calls,
            "step_failures": self.step_failures,
            "current_step_id": self.current_step_id,
            "last_committed_step_id": self.last_committed_step_id,
            "spawned_skill_run_ids": self.spawned_skill_run_ids,
            "parent_skill_run_id": self.parent_skill_run_id,
            "usage_tokens_so_far": self.usage_tokens_so_far,
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)


def plan_snapshot_path(agent_state_dir: Path, plan_id: str) -> Path:
    """Return ``<agent_state>/plans/<plan_id>.snapshot.json``.

    Sibling to the per-plan directory ``plans/<plan_id>/`` (which holds
    the decomposition artifact and any future per-plan artifacts).
    """
    return Path(agent_state_dir) / "plans" / f"{plan_id}.snapshot.json"


def step_result_file_path(
    agent_state_dir: Path, plan_id: str, step_id: str,
) -> Path:
    """ADR-0024: per-plan-dir-relative path for a spilled step result.

    ``<agent_state>/plans/<plan_id>/step_results/<step_id>.txt``.

    The path is relative-to-state-dir for storage in
    ``PlanSnapshot.step_result_refs`` (only the
    ``step_results/<step_id>.txt`` suffix is stored — see
    :func:`get_step_result`); this helper returns the absolute path
    for I/O.
    """
    return (
        Path(agent_state_dir) / "plans" / plan_id
        / "step_results" / f"{step_id}.txt"
    )


def get_step_result(
    snap: "PlanSnapshot", agent_state_dir: Path, step_id: str,
) -> str | None:
    """ADR-0024 read-side accessor — returns the step's recorded text.

    Reads inline first (= cheap path for ≤ threshold), falls back to
    the spilled file. Missing-file or unreadable-file → ``None`` so
    the caller treats the case uniformly (= step classifies as
    ``failed`` with ``step_result_file_missing`` cause, ADR-0024 §4).

    ``None`` distinguishes "not recorded" from "empty string" — the
    latter is a legitimate recorded value (= a step that produced no
    output text but still completed).
    """
    if step_id in snap.step_results:
        return snap.step_results[step_id]
    rel = snap.step_result_refs.get(step_id)
    if rel is None:
        return None
    full = Path(agent_state_dir) / "plans" / snap.plan_id / rel
    try:
        return full.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None


__all__ = [
    "PLAN_SNAPSHOT_VERSION",
    "PlanSnapshot",
    "get_step_result",
    "plan_snapshot_path",
    "step_result_file_path",
]
