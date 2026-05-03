"""SkillResumeAnalyzer — WAL → per-run resume plan (read-only).

On process restart, the resume runtime needs to know, for each active
skill run, what state to resume from:

  - Which phase was the skill in?
  - Which steps within that phase have committed (have a
    ``step_completed`` or ``step_failed`` event)? Their recorded
    results / errors are memoized — re-execution would duplicate side
    effects.
  - Which steps are *ambiguous*? Those have a ``step_started`` event
    but no matching completion. The op may or may not have produced a
    side effect externally; only the operator (via ``reyn.yaml``
    policy or a UI prompt) knows what to do.

This module is the read-only analysis layer. It reads:
  - the on-disk per-skill ``SkillSnapshot`` (current_phase, history,
    visit_counts, last_committed_step_id)
  - WAL events scoped to a single ``run_id``

… and produces a structured plan. No side effects, no WAL writes, no
runtime. Resume execution (memoize on dispatch, skip completed phases,
prompt for ambiguous) is wired separately in part D3.

Design invariants:
  - Pairing is by ``op_invocation_id``: a ``step_completed`` /
    ``step_failed`` matches a ``step_started`` with the same
    ``op_invocation_id``. Because the runtime resets the index per
    ``execute()`` call, op_invocation_ids are NOT unique across the
    full WAL — they're scoped to a single phase visit. Pairing must
    therefore consume started/completed events in WAL order.
  - World / LLM-purity steps emit only ``step_completed`` (no
    ``step_started``). They don't pair — each is its own committed
    step.
  - Pure ops emit nothing (no resume concern, re-execution is safe).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from reyn.skill.skill_snapshot import SkillSnapshot


@dataclass(frozen=True)
class CommittedStep:
    """A WAL-recorded step that resume can memoize without re-execution.

    ``op_invocation_id`` together with ``args_hash`` uniquely identifies
    the step within a single phase visit; resume memoization in
    dispatch_tool keys off the (run_id, phase, op_invocation_id) tuple
    and validates with args_hash to detect drift (e.g. the LLM emitted
    a different op shape this time).
    """

    op_invocation_id: str
    op_kind: str
    phase: str
    args_hash: str
    seq: int  # WAL seq of step_completed / step_failed
    # Either ``result`` or ``error_kind`` is set. Mutually exclusive.
    result: object = None
    error_kind: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AmbiguousStep:
    """A ``step_started`` event with no matching completion.

    The op may have committed externally (the canonical transactional-replay
    intermediate-state case). Only the operator can decide whether to
    retry, skip, or discard the run. Surfaced by the analyzer for the
    runtime / UX layer to act on.
    """

    op_invocation_id: str
    op_kind: str
    phase: str
    args_hash: str
    started_seq: int  # WAL seq of step_started
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ResumePlan:
    """Structured analysis output for a single skill run.

    ``phases_visited`` and ``visit_counts`` come from the snapshot
    (cheap, derivable). The interesting fields are ``committed_steps``
    (memoization keys) and ``ambiguous_steps`` (operator decisions).

    ``has_ambiguity`` is the fast-path predicate the resume runtime
    uses to decide whether a user prompt / policy lookup is needed
    before resuming.
    """

    run_id: str
    skill_name: str
    skill_input: dict
    current_phase: str
    last_phase_artifact_path: str | None
    awaiting_intervention_id: str | None
    phases_visited: list[str] = field(default_factory=list)
    visit_counts: dict[str, int] = field(default_factory=dict)
    committed_steps: list[CommittedStep] = field(default_factory=list)
    ambiguous_steps: list[AmbiguousStep] = field(default_factory=list)

    @property
    def has_ambiguity(self) -> bool:
        return bool(self.ambiguous_steps)


class SkillResumeAnalyzer:
    """Read-only analyzer producing a ResumePlan per skill run.

    Stateless — each call recomputes from the inputs. Cheap enough to
    invoke at every resume attempt; the snapshot read is one file open
    and the WAL scan is bounded by truncation.
    """

    def analyze(
        self,
        *,
        snapshot: SkillSnapshot,
        wal_events: Iterable[dict],
    ) -> ResumePlan:
        """Build a resume plan from the snapshot + WAL events for one run.

        ``wal_events`` should be the full set of events with this run's
        ``run_id`` (the caller has filtered the global WAL). Order
        matters — pairing of started/completed is by appearance order.
        Events at or before ``snapshot.applied_seq`` may be present;
        they're filtered out here so the analyzer's contract is
        "give me everything for this run; I'll figure out the cutover."
        """
        # Index step_started events by op_invocation_id, queued in arrival
        # order so we pair against the *oldest* unpaired start (handles
        # repeated visits where op_invocation_id collides).
        started_queues: dict[str, list[dict]] = {}
        committed: list[CommittedStep] = []
        ambiguous_finalized: set[int] = set()  # WAL seqs of paired step_started

        for event in wal_events:
            kind = event.get("kind")
            if kind == "step_started":
                key = event.get("op_invocation_id") or ""
                started_queues.setdefault(key, []).append(event)
            elif kind == "step_completed":
                key = event.get("op_invocation_id") or ""
                queue = started_queues.get(key, [])
                paired_started_seq: int | None = None
                if queue:
                    paired_event = queue.pop(0)
                    paired_started_seq = int(paired_event.get("seq", 0))
                    if paired_started_seq:
                        ambiguous_finalized.add(paired_started_seq)
                committed.append(CommittedStep(
                    op_invocation_id=key,
                    op_kind=str(event.get("op_kind", "")),
                    phase=str(event.get("phase", "")),
                    args_hash=str(event.get("args_hash", "")),
                    seq=int(event.get("seq", 0)),
                    result=event.get("result"),
                ))
            elif kind == "step_failed":
                key = event.get("op_invocation_id") or ""
                queue = started_queues.get(key, [])
                if queue:
                    paired_event = queue.pop(0)
                    paired_seq = int(paired_event.get("seq", 0))
                    if paired_seq:
                        ambiguous_finalized.add(paired_seq)
                committed.append(CommittedStep(
                    op_invocation_id=key,
                    op_kind=str(event.get("op_kind", "")),
                    phase=str(event.get("phase", "")),
                    args_hash=str(event.get("args_hash", "")),
                    seq=int(event.get("seq", 0)),
                    error_kind=str(event.get("error_kind", "")) or None,
                    error_message=str(event.get("message", "")) or None,
                ))

        # Whatever remains in started_queues was never matched — ambiguous.
        ambiguous: list[AmbiguousStep] = []
        for key, queue in started_queues.items():
            for ev in queue:
                ambiguous.append(AmbiguousStep(
                    op_invocation_id=key,
                    op_kind=str(ev.get("op_kind", "")),
                    phase=str(ev.get("phase", "")),
                    args_hash=str(ev.get("args_hash", "")),
                    started_seq=int(ev.get("seq", 0)),
                    args=dict(ev.get("args") or {}),
                ))
        # Sort ambiguous by seq for deterministic UX (oldest first)
        ambiguous.sort(key=lambda a: a.started_seq)

        return ResumePlan(
            run_id=snapshot.skill_run_id,
            skill_name=snapshot.skill_name,
            skill_input=dict(snapshot.skill_input),
            current_phase=snapshot.current_phase,
            last_phase_artifact_path=snapshot.last_phase_artifact_path,
            awaiting_intervention_id=snapshot.awaiting_intervention_id,
            phases_visited=list(snapshot.history),
            visit_counts=dict(snapshot.visit_counts),
            committed_steps=committed,
            ambiguous_steps=ambiguous,
        )
