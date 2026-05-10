from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from reyn.budget.budget import BudgetTracker
from reyn.config import LimitsConfig, OnLimitConfig
from reyn.events.event_store import EventStore
from reyn.kernel.runtime import OSRuntime, RunResult
from reyn.llm.model_resolver import ModelResolver
from reyn.permissions.permissions import PermissionResolver
from reyn.schemas.models import Skill
from reyn.user_intervention import InterventionBus

if TYPE_CHECKING:
    from reyn.events.state_log import StateLog
    from reyn.skill.skill_registry import SkillRegistry


_CALLER_RE = re.compile(r"^(direct|agents/[A-Za-z0-9_\-]+)$")


def _validate_caller(caller: str) -> str:
    if not _CALLER_RE.match(caller):
        raise ValueError(
            f"invalid caller {caller!r}; "
            "expected 'direct' or 'agents/<name>' (alphanumeric / _ / -)"
        )
    return caller


class Agent:
    def __init__(
        self,
        model: str,
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        intervention_bus: InterventionBus | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        limits: LimitsConfig | None = None,
        mcp_servers: dict | None = None,
        python_allowed_modules: list[str] | None = None,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        agent_role: str = "",
        caller: str = "direct",
        budget_tracker: BudgetTracker | None = None,
        # FP-0005: opt-in interactive / auto_extend behaviour on
        # safety-limit hits. None = legacy unattended (= abort).
        on_limit: "OnLimitConfig | None" = None,
    ) -> None:
        self.model = model
        self.state_dir = ".reyn"
        self.strict = strict
        self._subscribers = list(subscribers or [])
        self._intervention_bus = intervention_bus
        self._shell_allowed = shell_allowed
        self._limits = limits or LimitsConfig()
        self._resolver = resolver or ModelResolver({})
        self._permission_resolver = permission_resolver
        self._mcp_servers = mcp_servers
        self._python_allowed_modules = list(python_allowed_modules or [])
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context
        self._agent_role = agent_role
        self._caller = _validate_caller(caller)
        self._budget_tracker = budget_tracker
        # FP-0005: on_limit policy for safety-limit checkpoints.
        self._on_limit = on_limit or OnLimitConfig()
        self._runtime: OSRuntime | None = None
        self.run_id: str | None = None
        self.events_path: Path | None = None

    @property
    def caller(self) -> str:
        return self._caller

    async def run(
        self,
        skill: Skill,
        initial_input: dict,
        output_language: str | None = None,
        chain_id: str | None = None,
        skill_registry: "SkillRegistry | None" = None,
        state_log: "StateLog | None" = None,
        resume_plan: "Any | None" = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> RunResult:
        # On resume, callers pass the original run_id so the WAL events
        # stay scoped to the same skill run (= step events from before
        # the crash are paired with new ones via shared run_id). On
        # fresh starts, generate a new id.
        self.run_id = run_id or self._make_run_id(skill.name)
        # PR20: events live under
        #   <state_dir>/events/<caller>/skill_runs/<YYYY-MM>/<start>_<skill>.jsonl
        # caller ∈ {"direct", "agents/<name>"}.
        skill_dir = (
            Path(self.state_dir)
            / "events"
            / self._caller
            / "skill_runs"
        )
        store = EventStore(
            skill_dir,
            max_bytes=0,
            max_age_seconds=0,
            suffix=f"_{_safe_skill_name(skill.name)}",
        )
        # Open eagerly so events_path is populated even if the run errors
        # before the first emit (CLI prints `events saved → ...`).
        self.events_path = store.open()

        self._runtime = OSRuntime(
            skill, self.model,
            strict=self.strict,
            subscribers=[store] + self._subscribers,
            intervention_bus=self._intervention_bus,
            run_id=self.run_id,
            shell_allowed=self._shell_allowed,
            resolver=self._resolver,
            permission_resolver=self._permission_resolver,
            limits=self._limits,
            mcp_servers=self._mcp_servers,
            python_allowed_modules=self._python_allowed_modules,
            prompt_cache_enabled=self._prompt_cache_enabled,
            project_context=self._project_context,
            agent_role=self._agent_role,
            caller=self._caller,
            chain_id=chain_id,
            budget_tracker=self._budget_tracker,
            skill_name=skill.name,
            skill_registry=skill_registry,
            state_log=state_log,
            resume_plan=resume_plan,
            parent_run_id=parent_run_id,
            on_limit=self._on_limit,
        )
        return await self._runtime.run(initial_input, output_language=output_language)

    @property
    def phase_artifacts(self) -> list[dict]:
        """Return all artifacts stored during the run, excluding the initial input."""
        if self._runtime is None:
            return []
        return [
            a for a in self._runtime.workspace.artifacts
            if a["phase"] != "_input" and not a["phase"].endswith("_preprocessed")
        ]

    def get_events(self) -> list:
        return self._runtime.events.all() if self._runtime else []

    def get_events_json(self) -> list[dict]:
        return self._runtime.events.to_json() if self._runtime else []

    @staticmethod
    def _make_run_id(skill_name: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_name = _safe_skill_name(skill_name)
        return f"{ts}_{safe_name}"


def _safe_skill_name(name: str) -> str:
    """Produce a filename-safe skill name (truncated, alphanumeric / _ / -)."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", name)
    return cleaned.strip("_")[:40] or "skill"
