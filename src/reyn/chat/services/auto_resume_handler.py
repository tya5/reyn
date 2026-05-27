"""AutoResumeHandler — crash recovery for in-flight skill_runs.

Extracted from ChatSession (FP-0019 Wave 3). Reads WAL on session start,
identifies skill_runs that were in-flight at the last shutdown / crash,
and re-spawns them via the injected launcher callback.

Depends on FP-0011 (narrator removal, landed ``59c991a``) which removed the
narrator path that previously co-existed with the resume logic.
Depends on FP-0019 Wave 1b SkillRunner (landed ``9ae66fa``).

All event emissions go through the injected ``event_log``; no silent
state changes (P6).  Business logic lives entirely here; ChatSession
delegates via :meth:`resume_active` (P3).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from reyn.events.events import EventLog

if TYPE_CHECKING:
    from reyn.config import SkillResumeConfig
    from reyn.events.state_log import StateLog
    from reyn.skill.skill_registry import SkillRegistry
    from reyn.skill.skill_resume_coordinator import ResumeDecision, SkillResumeCoordinator

logger = logging.getLogger(__name__)


class AutoResumeHandler:
    """Crash recovery service — re-spawns in-flight skill_runs on session start.

    Parameters
    ----------
    event_log:
        Session-scoped :class:`~reyn.events.events.EventLog`.  All resume
        lifecycle events are emitted here (P6).
    state_log:
        Per-process WAL used by :class:`~reyn.skill.skill_resume_coordinator.SkillResumeCoordinator`
        to read ambiguous-step history and by :class:`~reyn.skill.skill_registry.SkillRegistry`
        to persist discard events.  May be ``None`` in test / non-chat contexts —
        :meth:`resume_active` returns 0 immediately when ``None``.
    get_skill_registry:
        Zero-argument callable returning ``SkillRegistry | None``.  Mirrors the
        pattern used by SkillRunner.  Returns ``None`` in standalone / test mode.
    drop_interventions_for_run:
        Sync callable ``(run_id | None) -> None``.  Called for discarded runs so
        stale pending interventions are pruned from the session.
    launcher:
        Async callable ``(ResumeDecision) -> None``.  In production this is
        ``ChatSession._spawn_resumed_skill``; in tests it is a stub that records
        dispatched decisions without launching a real Agent.
    """

    def __init__(
        self,
        *,
        event_log: EventLog,
        state_log: "StateLog | None",
        get_skill_registry: "Callable[[], SkillRegistry | None]",
        drop_interventions_for_run: "Callable[[str | None], None]",
        launcher: "Callable[[ResumeDecision], Awaitable[None]]",
    ) -> None:
        self._events = event_log
        self._state_log = state_log
        self.get_skill_registry = get_skill_registry
        self._drop_interventions_for_run = drop_interventions_for_run
        self._launcher = launcher

    # ── public API ────────────────────────────────────────────────────────────

    async def resume_active(
        self,
        *,
        coordinator: "SkillResumeCoordinator | None" = None,
        config: "SkillResumeConfig | None" = None,
        launcher: "Callable[[ResumeDecision], Awaitable[None]] | None" = None,
    ) -> int:
        """Identify in-flight skill_runs from WAL and re-spawn them.

        Returns count of decisions that were launched (= decisions minus discards).

        See :meth:`_resume_and_collect` for full algorithm details.
        """
        remaining = await self._resume_and_collect(
            coordinator=coordinator, config=config, launcher=launcher,
        )
        return len(remaining)

    # ── internal ──────────────────────────────────────────────────────────────

    async def _resume_and_collect(
        self,
        *,
        coordinator: "SkillResumeCoordinator | None" = None,
        config: "SkillResumeConfig | None" = None,
        launcher: "Callable[[ResumeDecision], Awaitable[None]] | None" = None,
    ) -> "list[ResumeDecision]":
        """Discover active skill_runs, apply resume policy, and launch tasks.

        Algorithm:
          1. Discover active runs via SkillResumeCoordinator
             (per-skill SkillSnapshot files + WAL)
          2. For each, build a ResumePlan and apply the operator's
             ``reyn.yaml`` policy (default = retry)
          3. ``discard`` decisions: call SkillRegistry.complete +
             drop pending interventions (no task launched)
          4. All other decisions: invoke ``launcher(decision)`` so
             the caller (production = ``ChatSession._spawn_resumed_skill``)
             can wire the actual asyncio task

        ``launcher`` kwarg overrides the instance-level launcher injected at
        construction time.  This override exists so tests can inject a stub
        without launching real skill runtimes; production callers omit it.

        Returns the list of decisions that were launched (= decisions minus discards).
        Exposed internally so the session's backward-compat wrapper can forward
        the same list to existing callers.
        """
        from reyn.config import SkillResumeConfig as _Config
        from reyn.skill.skill_resume_coordinator import (
            SkillResumeCoordinator as _Coord,
        )

        registry = self.get_skill_registry()
        if registry is None or self._state_log is None:
            return []

        coord = coordinator or _Coord()
        cfg = config or _Config()
        decisions = coord.discover_and_decide(
            skill_registry=registry,
            state_log=self._state_log,
            policy=cfg,
        )
        if not decisions:
            return []

        remaining = await coord.apply_decisions(
            decisions, skill_registry=registry,
            drop_interventions_for_run=self._drop_interventions_for_run,
        )
        actual_launcher = launcher or self._launcher
        for decision in remaining:
            await actual_launcher(decision)
            self._events.emit(
                "skill_run_resumed",
                run_id=decision.plan.run_id,
                skill=decision.plan.skill_name,
                action=decision.action,
            )
        return remaining


__all__ = ["AutoResumeHandler"]
