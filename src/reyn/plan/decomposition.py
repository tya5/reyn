"""Plan decomposition artifact — workspace-resident SSoT for the plan shape.

ADR-0023 §3.5: LLM-emitted decomposition is non-deterministic; re-calling
the planner LLM on resume yields a different plan and breaks step-result
memoization (= new ``step_id``s don't match recorded keys). The Plan
dataclass is therefore persisted as a workspace artifact (P5 SSoT) and
read verbatim on resume.

Storage path::

    .reyn/agents/<agent_name>/state/plans/<plan_id>/decomposition.json

Atomic write recipe (= ``tmp + fsync + rename``) mirrors
:meth:`SkillSnapshot.save`. A mid-write crash leaves any prior file
intact.

Schema (``DECOMPOSITION_SCHEMA_VERSION = 1``):

.. code-block:: json

    {
      "plan_id": "ab12cd34",
      "schema_version": 1,
      "goal": "...",
      "steps": [
        {"id": "s1", "description": "...", "tools": ["read_file"], "depends_on": []}
      ]
    }

This module is **standalone** — no ChatSession / RouterLoopHost coupling.
Callers (= ``dispatch_plan_tool`` write path, ``PlanRuntime`` read path,
``AgentRegistry.restore_all`` cleanup) thread the agent-state directory
explicitly. P7-clean: no skill-specific strings.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from reyn.chat.planner import Plan, PlanStep

DECOMPOSITION_SCHEMA_VERSION = 1


class DecompositionCorruptError(ValueError):
    """Raised when a decomposition artifact cannot be parsed.

    Distinct from :class:`FileNotFoundError` so the resume coordinator can
    branch ("file missing" → use snapshot ``steps_serialized`` fallback;
    "file corrupt" → force ``action=discard``).
    """


# ── Path helpers ────────────────────────────────────────────────────────────


def decomposition_dir(agent_state_dir: Path, plan_id: str) -> Path:
    """Return ``<agent_state>/plans/<plan_id>/`` (= per-plan directory).

    ``agent_state_dir`` is the ``.reyn/agents/<agent_name>/state/`` path
    chosen by the caller. The decomposition lives at
    ``<agent_state>/plans/<plan_id>/decomposition.json``; the per-plan
    directory keeps room for future per-plan artifacts (= step
    intermediate outputs, sub-loop traces) without cluttering the
    skills/ sibling.
    """
    return Path(agent_state_dir) / "plans" / plan_id


def decomposition_path(agent_state_dir: Path, plan_id: str) -> Path:
    """Return ``<agent_state>/plans/<plan_id>/decomposition.json``."""
    return decomposition_dir(agent_state_dir, plan_id) / "decomposition.json"


# ── Write / read / delete ───────────────────────────────────────────────────


def write_decomposition(
    agent_state_dir: Path, plan_id: str, plan: Plan
) -> Path:
    """Atomically persist the decomposition for ``plan_id`` and return its path.

    Atomic recipe: write to ``decomposition.json.tmp``, ``fsync``, then
    ``rename`` over ``decomposition.json``. Mid-write crash is safe — the
    ``.tmp`` file is incomplete and the rename never happened, so any
    prior artifact remains intact.

    The caller is responsible for ordering this **before** the
    ``plan_started`` WAL append (= ADR-0023 §3.5 lifecycle ordering: any
    plan in ``active_plan_ids`` MUST have a discoverable decomposition).
    """
    target = decomposition_path(agent_state_dir, plan_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "plan_id": plan_id,
        "schema_version": DECOMPOSITION_SCHEMA_VERSION,
        "goal": plan.goal,
        "steps": [
            {
                "id": s.id,
                "description": s.description,
                "tools": list(s.tools),
                "depends_on": list(s.depends_on),
            }
            for s in plan.steps
        ],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)
    return target


def read_decomposition(agent_state_dir: Path, plan_id: str) -> Plan:
    """Load the decomposition for ``plan_id``.

    Raises :class:`FileNotFoundError` if the artifact is missing
    (= caller may fall back to snapshot ``steps_serialized``).

    Raises :class:`DecompositionCorruptError` on JSON parse failure,
    schema-version mismatch, or structural defect (= caller forces
    ``action=discard`` per ADR-0023 §3.5 corruption fallback).
    """
    path = decomposition_path(agent_state_dir, plan_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecompositionCorruptError(
            f"plan decomposition at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise DecompositionCorruptError(
            f"plan decomposition at {path} must be an object, "
            f"got {type(data).__name__}"
        )
    version = data.get("schema_version")
    if version != DECOMPOSITION_SCHEMA_VERSION:
        raise DecompositionCorruptError(
            f"plan decomposition at {path} has schema_version "
            f"{version!r}, expected {DECOMPOSITION_SCHEMA_VERSION}"
        )
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal:
        raise DecompositionCorruptError(
            f"plan decomposition at {path}: goal must be a non-empty string"
        )
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise DecompositionCorruptError(
            f"plan decomposition at {path}: steps must be a non-empty list"
        )
    steps: list[PlanStep] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise DecompositionCorruptError(
                f"plan decomposition at {path}: steps[{i}] must be an object"
            )
        sid = raw.get("id")
        desc = raw.get("description")
        tools = raw.get("tools", [])
        deps = raw.get("depends_on", [])
        if not isinstance(sid, str) or not sid:
            raise DecompositionCorruptError(
                f"plan decomposition at {path}: steps[{i}].id "
                "must be a non-empty string"
            )
        if not isinstance(desc, str) or not desc:
            raise DecompositionCorruptError(
                f"plan decomposition at {path}: steps[{i}].description "
                "must be a non-empty string"
            )
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise DecompositionCorruptError(
                f"plan decomposition at {path}: steps[{i}].tools "
                "must be a list of strings"
            )
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise DecompositionCorruptError(
                f"plan decomposition at {path}: steps[{i}].depends_on "
                "must be a list of strings"
            )
        steps.append(
            PlanStep(
                id=sid,
                description=desc,
                tools=tuple(tools),
                depends_on=tuple(deps),
            )
        )
    return Plan(goal=goal, steps=tuple(steps))


def delete_decomposition(agent_state_dir: Path, plan_id: str) -> bool:
    """Remove ``decomposition.json`` and the per-plan directory if empty.

    Returns ``True`` if the artifact existed and was removed, ``False``
    otherwise. Idempotent — safe to call on a missing artifact (=
    used by ``AgentRegistry.restore_all`` cleanup which doesn't know
    if the artifact survived the crash).

    Removes the per-plan directory only when empty so future per-plan
    artifacts (= step intermediates) added by Phase 3 aren't dropped.
    """
    target = decomposition_path(agent_state_dir, plan_id)
    existed = target.exists()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
    parent = target.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return existed


def delete_plan_workspace(agent_state_dir: Path, plan_id: str) -> bool:
    """Recursively remove the per-plan directory + every artifact in it.

    ADR-0024 cleanup helper. Used by ``PlanRegistry.complete`` and
    ``/plan discard`` so the per-plan workspace (= decomposition.json
    + step_results/<step>.txt files + any future per-plan artifacts)
    is reclaimed atomically when the plan is no longer needed.

    Idempotent. Returns ``True`` if the directory existed (and is now
    gone), ``False`` if there was nothing to clean up.

    Distinct from ``delete_decomposition``: that helper removes only
    the decomposition.json + the per-plan dir if empty. With ADR-0024
    spilled step result files, the per-plan dir is rarely empty, so
    we need a recursive cleanup. Both helpers stay in the surface
    so call sites that intentionally want to preserve workspace
    artifacts (= forensics) keep that option.
    """
    plan_dir = decomposition_dir(agent_state_dir, plan_id)
    if not plan_dir.exists():
        return False
    shutil.rmtree(plan_dir, ignore_errors=True)
    return not plan_dir.exists()


__all__ = [
    "DECOMPOSITION_SCHEMA_VERSION",
    "DecompositionCorruptError",
    "decomposition_dir",
    "decomposition_path",
    "delete_decomposition",
    "delete_plan_workspace",
    "read_decomposition",
    "write_decomposition",
]
