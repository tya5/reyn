"""SkillResumeCoordinator — ties Analyzer + Config + Registry into resume decisions.

On process restart, this module is the single entry that:
  1. Discovers in-flight skill runs (via SkillRegistry.load_active)
  2. Builds a ResumePlan for each (via SkillResumeAnalyzer)
  3. Applies the operator's reyn.yaml ``skill_resume`` policy to
     ambiguous steps
  4. Produces a per-run ``ResumeDecision`` for the runtime + UX
     layers to act on

Out of scope (deliberately):
  - Actually re-launching the skill (that's D3b — runtime
    memoization in dispatch_tool + phase fast-forward in OSRuntime).
  - User prompting (PR-resume-ux — slash commands + UI).

The coordinator is stateless / functional: each method takes its
inputs explicitly. The same coordinator instance can serve multiple
agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Literal

from reyn.skill.skill_resume_analyzer import (
    AmbiguousStep,
    ResumePlan,
    SkillResumeAnalyzer,
)

if TYPE_CHECKING:
    from reyn.config import SkillResumeConfig
    from reyn.events.state_log import StateLog
    from reyn.skill.skill_registry import SkillRegistry


ResumeAction = Literal["resume", "retry", "skip", "discard", "prompt_required"]


@dataclass(frozen=True)
class ResumeDecision:
    """Per-run decision derived from the policy + ambiguity analysis.

    Action semantics:
      - ``resume``           — no ambiguity; runtime can resume directly
                               from ``current_phase`` with memoized
                               step results.
      - ``retry``            — ambiguity present, policy says retry the
                               ambiguous steps. Runtime drops them
                               from the memo so dispatch_tool re-invokes
                               on resume.
      - ``skip``             — ambiguity present, policy says synthesize
                               an empty completion. Runtime memoizes
                               the ambiguous steps with a sentinel
                               result.
      - ``discard``          — abort the skill run. Runtime calls
                               ``SkillRegistry.complete`` (with a
                               failure marker so callers see it).
      - ``prompt_required``  — ambiguity present, policy says prompt.
                               UX layer must collect a per-step
                               decision before resume can proceed.

    ``ambiguous_steps`` is only meaningful for ``retry`` / ``skip`` /
    ``prompt_required`` (it's empty for ``resume`` and irrelevant for
    ``discard``).
    """

    plan: ResumePlan
    action: ResumeAction
    ambiguous_steps: list[AmbiguousStep] = field(default_factory=list)


class SkillResumeCoordinator:
    """Stateless orchestrator that maps active runs → ResumeDecision."""

    def __init__(self, analyzer: SkillResumeAnalyzer | None = None) -> None:
        self._analyzer = analyzer or SkillResumeAnalyzer()

    def discover_and_decide(
        self,
        *,
        skill_registry: "SkillRegistry",
        state_log: "StateLog",
        policy: "SkillResumeConfig",
    ) -> list[ResumeDecision]:
        """Discover active runs, build plans, apply policy.

        Returns a ResumeDecision per active run. Caller (the resume
        runtime) iterates and dispatches: ``resume`` is auto-applied,
        ``retry`` / ``skip`` adjust the memo before resume,
        ``prompt_required`` defers to the UX layer, ``discard`` cleans
        up.
        """
        active = skill_registry.load_active()
        decisions: list[ResumeDecision] = []
        for run_id, snapshot in active.items():
            # Filter WAL events to this run only — analyzer pairs by
            # op_invocation_id and we don't want cross-run interleaving
            # to confuse pairing.
            wal_events = [
                ev for ev in state_log.iter_from(0)
                if ev.get("run_id") == run_id
            ]
            plan = self._analyzer.analyze(
                snapshot=snapshot, wal_events=wal_events,
            )
            decisions.append(self.decide_for_plan(plan, policy))
        return decisions

    def decide_for_plan(
        self,
        plan: ResumePlan,
        policy: "SkillResumeConfig",
    ) -> ResumeDecision:
        """Apply the operator policy to a single resume plan.

        Pure-function: no side effects, no I/O. Easy to test and reason
        about. Caller (the runtime) translates the action into the
        actual resume behavior.
        """
        if not plan.has_ambiguity:
            return ResumeDecision(plan=plan, action="resume")

        action_str = policy.policy_for(plan.skill_name)
        # SkillResumeConfig validates policy values at load time; the
        # mapping below is exhaustive for SKILL_RESUME_POLICIES.
        action: ResumeAction
        if action_str == "prompt":
            action = "prompt_required"
        elif action_str == "retry":
            action = "retry"
        elif action_str == "skip":
            action = "skip"
        elif action_str == "discard_skill":
            action = "discard"
        else:
            # Defensive: unknown policy → safest (prompt). Should not
            # happen under normal config-load path.
            action = "prompt_required"
        return ResumeDecision(
            plan=plan,
            action=action,
            ambiguous_steps=list(plan.ambiguous_steps),
        )

    @staticmethod
    def summarize(decisions: Iterable[ResumeDecision]) -> dict[str, int]:
        """Count decisions by action — handy for logging / status output."""
        out: dict[str, int] = {}
        for d in decisions:
            out[d.action] = out.get(d.action, 0) + 1
        return out
