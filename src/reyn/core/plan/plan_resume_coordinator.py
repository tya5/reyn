"""PlanResumeCoordinator — adopt-vs-cancel decisions for plan resume.

ADR-0023 §3.3. Sits between :class:`PlanResumeAnalyzer` (= produces
``PlanResumePlan`` from WAL + snapshot) and :class:`PlanRuntime` (=
consumes the plan to drive memo replay).

Phase 2 v1 keeps the policy simple: default ``retry_pending_steps``
applies to every recovered plan. The reyn.yaml schema and per-purity
child-action table from ADR-0023 §3.3 are stubbed in
:class:`PlanResumeConfig` but not yet sourced from disk; future work
adds the loader symmetric with ``_build_skill_resume_config``.

Top-level actions (= ADR-0023):

  - ``resume`` — all steps complete, finalize aggregation
  - ``retry_pending`` — memo committed steps, re-execute pending
  - ``discard`` — abort, cancel children, surface outbox notice
  - ``prompt_required`` — kept type-level only; ADR-0012 carryover
    means no Phase 2 path produces it

Per-purity child actions (= when a step spawned a child skill):

  - ``adopt`` — leave for skill auto-resume
  - ``cancel`` — discard via SkillRegistry.complete(status="discarded")

P7-clean: coordinator takes registries via dependency injection, no
direct imports of ChatSession internals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from reyn.core.plan.plan_resume_analyzer import (
    PlanResumeAnalyzer,
    PlanResumePlan,
)
from reyn.core.plan.plan_snapshot import PlanSnapshot

logger = logging.getLogger(__name__)


PlanResumeAction = Literal[
    "resume",
    "retry_pending",
    "discard",
    "prompt_required",
]

PlanResumeChildAction = Literal["adopt", "cancel"]


# ── Config ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanResumeConfig:
    """Operator-supplied policy for plan resume (= reyn.yaml plan_resume:).

    Phase 2 v1: only ``default`` is honored. ``child_purity`` table is
    parsed-but-applied-as-adopt-everywhere (= safest default — child
    skills handle their own resume via skill_resume infra).
    """

    default: PlanResumeAction = "retry_pending"
    child_purity: dict[str, PlanResumeChildAction] = field(
        default_factory=lambda: {
            "pure": "cancel",
            "world": "adopt",
            "side_effect": "adopt",
            "external": "adopt",
            "llm": "adopt",
        }
    )


def build_plan_resume_config(raw: dict | None) -> PlanResumeConfig:
    """Parse a ``plan_resume:`` block from reyn.yaml.

    Mirrors the validation posture of ``_build_skill_resume_config``:
    invalid top-level value → fall back to default with warning log;
    invalid purity key or action → log + skip.
    """
    if not isinstance(raw, dict):
        return PlanResumeConfig()
    cfg = PlanResumeConfig()
    default = raw.get("default", cfg.default)
    if default not in ("retry_pending", "retry_pending_steps", "discard",
                      "discard_plan"):
        logger.warning(
            "plan_resume.default %r invalid; falling back to %r",
            default, cfg.default,
        )
        default = cfg.default
    # Accept the documented yaml aliases (retry_pending_steps / discard_plan).
    if default == "retry_pending_steps":
        default = "retry_pending"
    elif default == "discard_plan":
        default = "discard"
    purity_table = dict(cfg.child_purity)
    purity_block = raw.get("child_purity")
    if isinstance(purity_block, dict):
        for k, v in purity_block.items():
            if k not in purity_table:
                logger.warning("plan_resume.child_purity unknown key %r", k)
                continue
            if v not in ("adopt", "cancel"):
                logger.warning(
                    "plan_resume.child_purity[%r] = %r invalid", k, v,
                )
                continue
            purity_table[k] = v  # type: ignore[assignment]
    return PlanResumeConfig(default=default, child_purity=purity_table)  # type: ignore[arg-type]


# ── Decision dataclass ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanResumeDecision:
    """A coordinator decision for one plan."""

    plan: PlanResumePlan
    action: PlanResumeAction
    pending_step_ids: tuple[str, ...] = ()
    child_actions: dict[str, PlanResumeChildAction] = field(default_factory=dict)


# ── Coordinator ───────────────────────────────────────────────────────────


class PlanResumeCoordinator:
    """Decides what to do with each recovered plan + applies side effects."""

    def __init__(self, config: PlanResumeConfig | None = None) -> None:
        self._config = config or PlanResumeConfig()
        self._analyzer = PlanResumeAnalyzer()

    @property
    def config(self) -> PlanResumeConfig:
        return self._config

    # ── decide ────────────────────────────────────────────────────────────

    def decide_for_plan(
        self, plan: PlanResumePlan,
    ) -> PlanResumeDecision:
        """Produce a decision for one analyzed plan.

        Default policy:
          - All steps complete → ``resume``
          - Otherwise → ``retry_pending`` (per ``config.default``;
            ``discard`` is the alternative top-level value)

        Child action defaulting: every child gets ``adopt`` so the
        existing skill_resume infrastructure drives its recovery.
        """
        all_complete = (
            len(plan.committed_step_ids) == plan.n_steps
        )
        if all_complete:
            return PlanResumeDecision(plan=plan, action="resume")
        if self._config.default == "discard":
            child_actions = {
                s.child_run_id: "cancel"
                for s in plan.step_states
                if s.state == "interrupted_with_child" and s.child_run_id
            }
            return PlanResumeDecision(
                plan=plan, action="discard",
                pending_step_ids=plan.pending_step_ids,
                child_actions=child_actions,
            )
        # retry_pending default
        child_actions: dict[str, PlanResumeChildAction] = {}
        for s in plan.step_states:
            if s.state == "interrupted_with_child" and s.child_run_id:
                # Phase 2 v1: adopt by default. Future: per-purity decision
                # via self._config.child_purity[skill_purity].
                child_actions[s.child_run_id] = "adopt"
        return PlanResumeDecision(
            plan=plan, action="retry_pending",
            pending_step_ids=plan.pending_step_ids,
            child_actions=child_actions,
        )

    def discover_and_decide(
        self,
        *,
        plan_registry: Any,
        wal_events: Iterable[dict],
        decomposition_loader: Callable[[str], Any],
        child_skill_lookup: Callable[[str], str | None] | None = None,
        agent_state_dir: "Any" = None,
    ) -> list[PlanResumeDecision]:
        """Scan the registry's active plans and produce decisions.

        ``plan_registry``: a ``PlanRegistry`` instance (pre-loaded via
        ``load_active()``).
        ``wal_events``: usually ``state_log.iter_from(0)`` — coordinator
        filters per-plan internally.
        ``decomposition_loader(plan_id) -> Plan``: callable that returns
        the parsed decomposition (= reads the artifact). On
        :class:`FileNotFoundError` or
        :class:`reyn.core.plan.DecompositionCorruptError`, the coordinator
        forces ``action=discard`` for that plan (= ADR-0023 §3.5
        corruption fallback).
        ``child_skill_lookup``: forwarded to the analyzer.
        """
        events = list(wal_events)  # materialize once for multi-iteration
        decisions: list[PlanResumeDecision] = []
        for plan_id in plan_registry.list_active():
            snap: PlanSnapshot = plan_registry.get(plan_id)
            if snap is None:
                continue
            try:
                decomposition = decomposition_loader(plan_id)
            except FileNotFoundError:
                # No artifact + snapshot inline fallback also empty?
                # Force discard with corrupt outbox reason.
                decisions.append(
                    self._force_discard(
                        snap, reason="decomposition_artifact_missing",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 — corruption etc.
                logger.warning(
                    "decomposition load failed for %s: %r", plan_id, exc,
                )
                decisions.append(
                    self._force_discard(
                        snap, reason="decomposition_artifact_corrupt",
                    )
                )
                continue
            analyzed = self._analyzer.analyze(
                snapshot=snap, decomposition=decomposition,
                wal_events=events,
                child_skill_lookup=child_skill_lookup,
                agent_state_dir=agent_state_dir,
            )
            decisions.append(self.decide_for_plan(analyzed))
        return decisions

    def _force_discard(
        self, snap: PlanSnapshot, *, reason: str,
    ) -> PlanResumeDecision:
        plan = PlanResumePlan(
            plan_id=snap.plan_id,
            chain_id=snap.chain_id,
            goal=snap.goal,
            n_steps=0,
            decomposition_artifact_path=snap.decomposition_artifact_path,
            step_states=(),
            has_ambiguity=False,
            has_in_flight_child=False,
        )
        return PlanResumeDecision(
            plan=plan, action="discard",
            pending_step_ids=(),
            child_actions={},
        )

    # ── apply destructive effects ────────────────────────────────────────

    async def apply_decisions(
        self,
        decisions: Iterable[PlanResumeDecision],
        *,
        plan_registry: Any,
        skill_registry: Any | None = None,
        on_outbox_notice: Callable[[str, str], "Any"] | None = None,
    ) -> list[PlanResumeDecision]:
        """Execute side effects for each decision and return launchable ones.

        Side effects:
          - ``discard`` → cancel children flagged ``cancel``, complete
            the plan (= ``plan_registry.complete(status="discarded")``),
            optionally surface an outbox notice.
          - ``retry_pending`` → cancel children flagged ``cancel``;
            adopted children stay live for skill auto-resume.

        Returns the subset where ``action ∈ {"resume", "retry_pending"}``
        — the caller (ChatSession) launches a ``PlanRuntime`` for each.
        """
        launchable: list[PlanResumeDecision] = []
        for decision in decisions:
            plan_id = decision.plan.plan_id
            if decision.action == "discard":
                await self._discard_plan(
                    decision, plan_registry=plan_registry,
                    skill_registry=skill_registry,
                    on_outbox_notice=on_outbox_notice,
                )
                continue
            # retry_pending or resume — cancel any explicitly cancel-
            # flagged children; adopted children survive.
            if skill_registry is not None:
                for child_run_id, action in decision.child_actions.items():
                    if action == "cancel":
                        try:
                            await skill_registry.complete(
                                run_id=child_run_id, status="discarded",
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "child cancel failed for %s: %r",
                                child_run_id, exc,
                            )
            launchable.append(decision)
        return launchable

    async def _discard_plan(
        self,
        decision: PlanResumeDecision,
        *,
        plan_registry: Any,
        skill_registry: Any | None,
        on_outbox_notice: Callable[[str, str], "Any"] | None,
    ) -> None:
        plan_id = decision.plan.plan_id
        # Cancel children flagged "cancel".
        if skill_registry is not None:
            for child_run_id, action in decision.child_actions.items():
                if action == "cancel":
                    try:
                        await skill_registry.complete(
                            run_id=child_run_id, status="discarded",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "discard cascade child cancel failed: %r", exc,
                        )
        # Plan-level cleanup.
        try:
            await plan_registry.complete(plan_id=plan_id, status="aborted")
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_registry.complete failed: %r", exc)
        # Outbox notice.
        if on_outbox_notice is not None:
            try:
                await on_outbox_notice(
                    plan_id,
                    "A plan-mode reply was discarded during resume; "
                    "please re-issue your request.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_outbox_notice failed: %r", exc)


__all__ = [
    "PlanResumeAction",
    "PlanResumeChildAction",
    "PlanResumeCoordinator",
    "PlanResumeConfig",
    "PlanResumeDecision",
    "build_plan_resume_config",
]
