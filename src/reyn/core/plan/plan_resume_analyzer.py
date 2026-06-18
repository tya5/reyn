"""PlanResumeAnalyzer — derive a PlanResumePlan from WAL + PlanSnapshot.

ADR-0023 §3.2. The analyzer pairs ``plan_step_started`` with
``plan_step_completed`` | ``plan_step_failed`` by ``(plan_id, step_id)``,
FIFO per-key, and produces a per-step state under one of:

  - ``pending`` — no started event, or started without terminal where
    the step is non-effectful (= safe to re-execute)
  - ``completed_with_result`` — started + completed pair seen
  - ``failed`` — started + failed pair seen
  - ``interrupted_with_child`` — started, no terminal, but the step
    spawned a child skill (= coordinator must reconcile via skill_resume)

The output ``PlanResumePlan`` is consumed by ``PlanRuntime`` (memo
replay path, Step 7b) and ``PlanResumeCoordinator`` (adopt-vs-cancel
decisions, Step 7c).

P7-clean: no skill-specific strings; ``child_skill_lookup`` is injected
by the runtime to keep the analyzer decoupled from ``SkillRegistry``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

from reyn.core.plan.plan_snapshot import PlanSnapshot, get_step_result
from reyn.runtime.planner import Plan, PlanStep

logger = logging.getLogger(__name__)


PlanStepStateKind = Literal[
    "pending",
    "completed_with_result",
    "failed",
    "interrupted_with_child",
]


# Tools that, when present in step.tools, indicate the step has
# side-effect potential (= ambiguous-no-terminal must escalate to
# `failed("ambiguous_no_terminal")` rather than `pending`).
#
# Phase 2 v1 uses a coarse name-list; ADR-0023 §3.2 allows refinement
# via OP_KIND_REGISTRY purity tier. The chat router tool catalog uses
# different names than op_runtime registry, so we maintain a small
# explicit list.
_EFFECTFUL_TOOLS = frozenset({
    "write_file",
    "delete_file",
    "call_mcp_tool",
    "remember",
    "forget",
    "remember_shared",
    "forget_shared",
})

# Tools that imply spawning a child skill (= the spawn graph is
# coordinated via skill_resume infra). Keeps analyzer P7-clean by
# treating "invoke_skill" as a generic spawn marker rather than peeking
# at the skill name.
_CHILD_SPAWNING_TOOLS = frozenset({"invoke_skill"})


def _step_is_effectful(step: PlanStep) -> bool:
    return any(t in _EFFECTFUL_TOOLS for t in step.tools)


def _step_spawns_child(step: PlanStep) -> bool:
    return any(t in _CHILD_SPAWNING_TOOLS for t in step.tools)


# ── PlanStepState (richer than Step 5's PlanResumePlan) ────────────────────


@dataclass(frozen=True)
class PlanStepState:
    """Per-step recovery state derived by the analyzer.

    ``state`` drives the runtime's classify_step decision (Step 7b):

      - ``completed_with_result`` → memo (use ``result_text``)
      - ``failed`` → propagate as recorded failure
      - ``interrupted_with_child`` → coordinator decides adopt vs cancel
      - ``pending`` → fresh execute
    """

    step_id: str
    state: PlanStepStateKind
    started_seq: int | None = None
    result_text: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    child_run_id: str | None = None
    child_state: Literal["completed", "in_flight", "discarded", "unknown"] | None = None
    n_attempts: int = 1
    is_effectful: bool = False
    step_signature: str = ""


@dataclass(frozen=True)
class PlanResumePlan:
    """Analyzer output — full ADR-0023 §3.2 shape (Step 7).

    Distinct from the Step 5 forward-compat dataclass in plan_runtime.py,
    which carried only the minimal subset needed to stabilise the
    constructor signature. This is the canonical resume directive.
    """

    plan_id: str
    chain_id: str
    goal: str
    n_steps: int
    decomposition_artifact_path: str | None
    step_states: tuple[PlanStepState, ...] = ()
    has_ambiguity: bool = False
    has_in_flight_child: bool = False
    # ADR-0025: per-step recorded sub-loop LLM call log, copied from
    # PlanSnapshot.step_llm_calls. execute_plan constructs a
    # SubLoopMemoProvider per pending step seeded from this map.
    step_llm_call_log: dict = field(default_factory=dict)

    @property
    def committed_step_ids(self) -> frozenset[str]:
        return frozenset(
            s.step_id for s in self.step_states
            if s.state == "completed_with_result"
        )

    @property
    def pending_step_ids(self) -> tuple[str, ...]:
        return tuple(
            s.step_id for s in self.step_states if s.state == "pending"
        )

    @property
    def failed_step_ids(self) -> tuple[str, ...]:
        return tuple(
            s.step_id for s in self.step_states if s.state == "failed"
        )

    @property
    def interrupted_with_child_step_ids(self) -> tuple[str, ...]:
        return tuple(
            s.step_id for s in self.step_states
            if s.state == "interrupted_with_child"
        )

    def step_result_map(self) -> dict[str, str]:
        return {
            s.step_id: s.result_text
            for s in self.step_states
            if s.state == "completed_with_result" and s.result_text is not None
        }


# ── analyzer ──────────────────────────────────────────────────────────────


class PlanResumeAnalyzer:
    """Reduce WAL plan_step_* events + PlanSnapshot into a PlanResumePlan."""

    def analyze(
        self,
        *,
        snapshot: PlanSnapshot,
        decomposition: Plan,
        wal_events: Iterable[dict],
        child_skill_lookup: Callable[[str], str | None] | None = None,
        agent_state_dir: Path | None = None,
    ) -> PlanResumePlan:
        """Produce a PlanResumePlan for the given plan_id.

        ``wal_events`` is an iterable of dicts in WAL format (= what
        StateLog.iter_from yields). The analyzer filters by
        ``plan_id == snapshot.plan_id`` itself; callers may pre-filter
        for efficiency but it isn't required.

        ``child_skill_lookup`` maps ``child_run_id`` → state literal
        (``"completed" | "in_flight" | "discarded" | "unknown"``). When
        absent, ``interrupted_with_child`` states use ``"unknown"`` for
        ``child_state`` (= caller must default-cancel or query
        elsewhere).
        """
        plan_id = snapshot.plan_id

        # Bucket events per step_id, FIFO. We accumulate started_seqs
        # (queue) then drain by completed/failed in order so a re-emit
        # pairs in arrival order. Phase 2 v1 plans run steps once, but
        # the FIFO shape mirrors SkillResumeAnalyzer for symmetry.
        starts: dict[str, list[int]] = {}
        completes: dict[str, list[tuple[int, int]]] = {}  # (seq, content_len)
        fails: dict[str, list[tuple[int, str]]] = {}      # (seq, error)

        for evt in wal_events:
            if evt.get("plan_id") != plan_id:
                continue
            kind = evt.get("kind")
            sid = evt.get("step_id")
            seq = evt.get("seq")
            if not isinstance(sid, str) or not isinstance(seq, int):
                continue
            if kind == "plan_step_started":
                starts.setdefault(sid, []).append(seq)
            elif kind == "plan_step_completed":
                completes.setdefault(sid, []).append(
                    (seq, int(evt.get("content_len", 0)))
                )
            elif kind == "plan_step_failed":
                fails.setdefault(sid, []).append(
                    (seq, str(evt.get("error", "")))
                )

        step_states: list[PlanStepState] = []
        any_ambig = False
        any_in_flight_child = False

        for step in decomposition.steps:
            sid = step.id
            sig = self._step_signature(step)

            # FIFO pair: pop earliest started, match against earliest
            # terminal (completed | failed). If both terminals exist,
            # whichever is earlier wins.
            n_starts = len(starts.get(sid, []))
            comp_list = completes.get(sid, [])
            fail_list = fails.get(sid, [])
            n_terminals = len(comp_list) + len(fail_list)

            started_seq = starts[sid][0] if n_starts > 0 else None

            if n_terminals == 0 and n_starts == 0:
                # No events at all — step never reached.
                step_states.append(PlanStepState(
                    step_id=sid, state="pending",
                    is_effectful=_step_is_effectful(step),
                    step_signature=sig,
                ))
                continue

            if n_terminals == 0 and n_starts > 0:
                # Started, no terminal. Two sub-cases:
                #  - spawned a child → interrupted_with_child
                #  - no child + non-effectful → pending (re-execute)
                #  - no child + effectful → failed (ambiguous_no_terminal)
                child_run_id = snapshot.spawned_skill_run_ids.get(sid)
                if child_run_id and _step_spawns_child(step):
                    child_state: Literal[
                        "completed", "in_flight", "discarded", "unknown"
                    ] = "unknown"
                    if child_skill_lookup is not None:
                        try:
                            looked = child_skill_lookup(child_run_id)
                        except Exception:  # noqa: BLE001 — defensive
                            looked = None
                        if looked in ("completed", "in_flight",
                                      "discarded", "unknown"):
                            child_state = looked  # type: ignore[assignment]
                    if child_state == "in_flight":
                        any_in_flight_child = True
                    step_states.append(PlanStepState(
                        step_id=sid, state="interrupted_with_child",
                        started_seq=started_seq,
                        child_run_id=child_run_id,
                        child_state=child_state,
                        is_effectful=_step_is_effectful(step),
                        step_signature=sig,
                    ))
                    any_ambig = True
                elif _step_is_effectful(step):
                    # Effectful step started but no terminal: safer to
                    # mark failed than to silently re-execute.
                    step_states.append(PlanStepState(
                        step_id=sid, state="failed",
                        started_seq=started_seq,
                        error_kind="ambiguous_no_terminal",
                        error_message=(
                            "step started but no completion/failure "
                            "event recorded; effectful tools prevent "
                            "automatic retry"
                        ),
                        is_effectful=True,
                        step_signature=sig,
                    ))
                    any_ambig = True
                else:
                    # Non-effectful: safe to re-execute.
                    step_states.append(PlanStepState(
                        step_id=sid, state="pending",
                        started_seq=started_seq,
                        is_effectful=False,
                        step_signature=sig,
                    ))
                continue

            # Terminal exists. Choose the earliest among completes/fails.
            earliest_complete = comp_list[0][0] if comp_list else None
            earliest_fail = fail_list[0][0] if fail_list else None
            if (
                earliest_complete is not None
                and (earliest_fail is None or earliest_complete < earliest_fail)
            ):
                # Hit completed. Recover result_text from snapshot.
                # (WAL doesn't carry the text — only content_len. The
                # snapshot, populated by PlanRegistry.record_step_completed,
                # is the source of truth.)
                #
                # ADR-0024: get_step_result transparently resolves
                # inline vs spilled-to-file. None return = ref present
                # but file unreadable → classify as failed
                # (= step_result_file_missing) per ADR-0024 §4.
                if agent_state_dir is not None:
                    text = get_step_result(snapshot, agent_state_dir, sid)
                else:
                    # Backward-compat: callers that don't supply
                    # agent_state_dir get the inline-only path. Spilled
                    # entries surface as None → failed below.
                    text = snapshot.step_results.get(sid)
                if text is None and sid in snapshot.step_result_refs:
                    # Spilled but unreadable.
                    step_states.append(PlanStepState(
                        step_id=sid, state="failed",
                        started_seq=started_seq,
                        error_kind="step_result_file_missing",
                        error_message=(
                            f"step result file referenced "
                            f"({snapshot.step_result_refs[sid]!r}) but "
                            "could not be read"
                        ),
                        is_effectful=_step_is_effectful(step),
                        step_signature=sig,
                    ))
                    any_ambig = True
                else:
                    step_states.append(PlanStepState(
                        step_id=sid, state="completed_with_result",
                        started_seq=started_seq,
                        result_text=text or "",
                        is_effectful=_step_is_effectful(step),
                        step_signature=sig,
                    ))
            else:
                # Hit failed.
                fail_seq, error_str = fail_list[0]
                step_states.append(PlanStepState(
                    step_id=sid, state="failed",
                    started_seq=started_seq,
                    error_kind="step_failed",
                    error_message=error_str,
                    is_effectful=_step_is_effectful(step),
                    step_signature=sig,
                ))

        return PlanResumePlan(
            plan_id=plan_id,
            chain_id=snapshot.chain_id,
            goal=snapshot.goal or decomposition.goal,
            n_steps=len(decomposition.steps),
            decomposition_artifact_path=snapshot.decomposition_artifact_path,
            step_states=tuple(step_states),
            has_ambiguity=any_ambig,
            has_in_flight_child=any_in_flight_child,
            # ADR-0025: forward the per-step LLM call log so the runtime
            # can seed a SubLoopMemoProvider per pending step.
            step_llm_call_log=dict(snapshot.step_llm_calls),
        )

    def _step_signature(self, step: PlanStep) -> str:
        """Stable signature for decomposition-drift detection.

        Phase 2 v1 uses a simple repr; full hash isn't needed at this
        layer (= the canonical SSoT is the decomposition artifact, not
        the signature).
        """
        return f"{step.description}|tools={','.join(step.tools)}|deps={','.join(step.depends_on)}"


__all__ = [
    "PlanResumeAnalyzer",
    "PlanResumePlan",
    "PlanStepState",
    "PlanStepStateKind",
]
