from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from reyn.budget.budget import BudgetTracker
from reyn.config import OnLimitConfig, SafetyConfig

if TYPE_CHECKING:
    from reyn.config import MultimodalConfig, SandboxConfig
    from reyn.environment.backend import EnvironmentBackend
    from reyn.sandbox.backend import SandboxBackend
    from reyn.secrets.store import ScopedSecretStore
    from reyn.workspace.media_store import MediaStore
from reyn.events.event_store import EventStore
from reyn.kernel.runtime import OSRuntime, RunResult
from reyn.llm.model_resolver import ModelResolver
from reyn.permissions.permissions import PermissionResolver
from reyn.schemas.models import Skill
from reyn.user_intervention import RequestBus

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
        intervention_bus: RequestBus | None = None,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        safety: "SafetyConfig | None" = None,
        mcp_servers: dict | None = None,
        python_allowed_modules: list[str] | None = None,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        agent_role: str = "",
        caller: str = "direct",
        budget_tracker: BudgetTracker | None = None,
        sandbox_config: "SandboxConfig | None" = None,
        multimodal_config: "MultimodalConfig | None" = None,
        media_store: "MediaStore | None" = None,
        secret_store: "ScopedSecretStore | None" = None,
        workspace_base_dir: "Path | None" = None,
        workspace_state_dir: "Path | None" = None,
        run_id: str | None = None,
        environment_backend: "EnvironmentBackend | None" = None,
        sandbox_backend: "SandboxBackend | None" = None,
        tool_calls_op_loop_skills: list[str] | None = None,
    ) -> None:
        self.model = model
        # FP-0008 #1132: the events audit log lives under state_dir. Honor an
        # explicit workspace_state_dir (the same host-side dir that holds the
        # Workspace's artifacts + control_ir_offload) so events co-locate with
        # the rest of the run's state — required for an in-container run where
        # base_dir is the container repo and state is kept host-side. Falls back
        # to the cwd-relative ".reyn" default (unchanged host behavior) when no
        # explicit state dir is given.
        self.state_dir = str(workspace_state_dir) if workspace_state_dir is not None else ".reyn"
        self.strict = strict
        self._subscribers = list(subscribers or [])
        self._intervention_bus = intervention_bus
        # #1212: skills opted into the native-tools op-loop (config-driven gate,
        # threaded to OSRuntime so the op-loop is reachable on the real run path).
        self._tool_calls_op_loop_skills = list(tool_calls_op_loop_skills or [])
        # #1092 PR-B: skills opted into the converged op-loop (config-driven gate,
        # threaded to OSRuntime so the converged path is reachable on the real run).
        self._safety = safety or SafetyConfig()
        self._resolver = resolver or ModelResolver({})
        self._permission_resolver = permission_resolver
        self._mcp_servers = mcp_servers
        self._python_allowed_modules = list(python_allowed_modules or [])
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context
        self._agent_role = agent_role
        self._caller = _validate_caller(caller)
        self._budget_tracker = budget_tracker
        # FP-0017 follow-up: declarative sandbox config (reyn.yaml `sandbox:`).
        # None → platform auto-detect; otherwise honors backend/on_unsupported.
        self._sandbox_config = sandbox_config
        # Issue #364 multi-modal cluster: media-size gate config (reyn.yaml
        # ``multimodal:``). ``None`` → no cap (= permissive default).
        self._multimodal_config = multimodal_config
        # Issue #383 PR-C: media + tool-result file storage.
        self._media_store = media_store
        self._secret_store = secret_store
        self._workspace_base_dir = workspace_base_dir
        # FP-0008 #1115 Stage 2: host-side workspace state_dir (artifacts +
        # events), decoupled from base_dir. For an in-container run base_dir is
        # the container repo (e.g. /testbed) while state_dir stays on the host
        # so artifacts/events survive container teardown (Stage 0 decouple).
        # None → Workspace default (base_dir/.reyn = unchanged host behavior).
        self._workspace_state_dir = workspace_state_dir
        # FP-0008 #1115 Stage 2: per-run backend injection. The SAME instance
        # (a dual-Protocol DockerEnvironmentBackend) is passed as BOTH so repo
        # FS + exec hit one container target; defaults None → host (HostBackend
        # for FS, get_default_backend for exec) = behavior-preserving.
        self._environment_backend = environment_backend
        self._sandbox_backend = sandbox_backend
        self._runtime: OSRuntime | None = None
        # FP-0008 PR-R (= tui-coder finding #1 propagation): run_id from
        # construction site preserves the canonical run_id set by the
        # caller (e.g. ChatSession._build_agent_for_skill_runner passes
        # the skill_runner-generated canonical here so the Agent instance
        # carries the same id from birth, before agent.run() is invoked).
        # When None, agent.run() will generate a fresh canonical at
        # invocation time (= back-compat with direct callers).
        self.run_id: str | None = run_id
        self.events_path: Path | None = None

    @classmethod
    def from_config(
        cls,
        config: "ReynConfig",
        *,
        model: str | None = None,
        safety: "SafetyConfig | None" = None,
        resolver: ModelResolver | None = None,
        python_allowed_modules: list[str] | None = None,
        caller: str = "direct",
        unsafe_python: bool = False,
        interactive: bool | None = None,
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        intervention_bus: RequestBus | None = None,
        project_context: str = "",
        agent_role: str = "",
        budget_tracker: BudgetTracker | None = None,
        multimodal_config: "MultimodalConfig | None" = None,
        media_store: "MediaStore | None" = None,
        secret_store: "ScopedSecretStore | None" = None,
        workspace_base_dir: "Path | None" = None,
        workspace_state_dir: "Path | None" = None,
        run_id: str | None = None,
        environment_backend: "EnvironmentBackend | None" = None,
        sandbox_backend: "SandboxBackend | None" = None,
    ) -> "Agent":
        """Construct a fully-wired Agent from a ReynConfig (#997 dir2).

        Construction-time prevention of the FP-0008 / #1133 wiring-gap class:
        the permission/runtime bundle that direct callers historically
        hand-listed (and sometimes omitted — e.g. ``eval benchmark`` shipped
        without ``permission_resolver``, so a declared op got filtered to
        nothing and the LLM hallucinated a fake schema) is derived here from
        ``config`` once. The caller **cannot** forget ``permission_resolver`` /
        ``mcp_servers`` / ``python_allowed_modules`` / ``prompt_cache_enabled`` /
        ``sandbox_config``.

        ``model`` / ``safety`` / ``resolver`` default to the config-derived value
        but accept an override (e.g. ``reyn run`` passes an args-aware
        ``safety`` for ``--max-phase-visits`` and friends). The gap-prone bundle
        is always derived — it is intentionally not overridable.

        The permission-resolver derivation matches ``cli.run._build_permission_resolver``
        (inlined here to keep the factory in the core layer, dependency-free of
        the cli package). ``interactive`` defaults to ``sys.stdin.isatty()``.
        """
        import sys

        from reyn.config import _find_project_root

        perm_config = dict(getattr(config, "permissions", {}) or {})
        permission_resolver = PermissionResolver(
            config_permissions=perm_config,
            project_root=_find_project_root(Path.cwd()),
            # #1414: under a container backend the file zone anchors on the
            # in-container repo (workspace_base_dir = base_dir, #1410/#1411),
            # while approvals stay host-side. None (host) → defaults to
            # project_root (byte-identical).
            file_zone_root=workspace_base_dir,
            interactive=sys.stdin.isatty() if interactive is None else interactive,
            unsafe_python_allowed=unsafe_python,
        )
        return cls(
            model=model if model is not None else config.model,
            strict=strict,
            subscribers=subscribers,
            intervention_bus=intervention_bus,
            resolver=resolver if resolver is not None else ModelResolver(
                config.models,
                default_class=config.model,
                purpose_classes=config.model_class_by_purpose,
            ),
            permission_resolver=permission_resolver,
            safety=safety if safety is not None else config.safety,
            mcp_servers=config.mcp,
            python_allowed_modules=(
                list(python_allowed_modules)
                if python_allowed_modules is not None
                else list(config.python.allowed_modules)
            ),
            prompt_cache_enabled=config.prompt_cache_enabled,
            project_context=project_context,
            agent_role=agent_role,
            caller=caller,
            budget_tracker=budget_tracker,
            sandbox_config=config.sandbox,
            multimodal_config=multimodal_config,
            media_store=media_store,
            secret_store=secret_store,
            workspace_base_dir=workspace_base_dir,
            workspace_state_dir=workspace_state_dir,
            run_id=run_id,
            environment_backend=environment_backend,
            sandbox_backend=sandbox_backend,
            tool_calls_op_loop_skills=config.tool_calls_op_loop_skills,
        )

    @property
    def caller(self) -> str:
        return self._caller

    @property
    def secret_store(self):
        """Read-only accessor for the injected ScopedSecretStore (or None).

        Mirrors ``OSRuntime.secret_store`` / ``ControlIRExecutor.secret_store``
        so callers (= tests verifying DI identity) can probe the wiring via
        the public surface instead of reaching into ``_secret_store``.
        """
        return self._secret_store

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
        plan_step: dict | None = None,
    ) -> RunResult:
        # Run-id resolution priority (FP-0008 PR-R wiring contract):
        #   1. ``run_id`` kwarg to this call    (= caller overrides per-call)
        #   2. ``self.run_id`` set at __init__  (= ChatSession spawn path)
        #   3. fresh ``_make_run_id(skill.name)`` (= direct callers, resume)
        # The (2) layer is what PR-R adds: ChatSession's
        # ``_build_agent_for_skill_runner`` passes the skill_runner-
        # generated canonical to Agent.__init__, so by the time
        # agent.run() runs, the instance already owns the canonical id.
        # Prior to PR-R, the instance had ``self.run_id = None`` so
        # agent.run() always generated a fresh id even when the caller
        # had a canonical from skill_runner — driving the 2-form
        # mismatch tui-coder caught in 5-point smoke.
        self.run_id = run_id or self.run_id or self._make_run_id(skill.name)
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
            resolver=self._resolver,
            permission_resolver=self._permission_resolver,
            safety=self._safety,
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
            sandbox_config=self._sandbox_config,
            environment_backend=self._environment_backend,
            sandbox_backend=self._sandbox_backend,
            multimodal_config=self._multimodal_config,
            media_store=self._media_store,
            secret_store=self._secret_store,
            plan_step=plan_step,
            workspace_base_dir=self._workspace_base_dir,
            workspace_state_dir=self._workspace_state_dir,
            tool_calls_op_loop_skills=self._tool_calls_op_loop_skills,
        )
        return await self._runtime.run(initial_input, output_language=output_language)

    @property
    def phase_artifacts(self) -> list[dict]:
        """Return all artifacts stored during the run, excluding the initial input.

        FP-0008 #1115 Stage 0 Part B: ``store_artifact`` returns a state_dir-
        relative handle (decoupled from base_dir). These artifacts cross a
        workspace boundary — a parent skill (e.g. ``eval``) hands ``path`` to a
        sub-skill judge that ``file.read``s it from a *different* workspace. So
        the handle is resolved here, via this run's own workspace, to an
        absolute path the consumer can resolve regardless of its own base_dir
        (consistent with the LLM-facing artifact_ref resolution in
        ``runtime.build_frame``). Stage 2 (container backend) swaps the
        resolution for a host/container-served read without touching skills.
        """
        if self._runtime is None:
            return []
        ws = self._runtime.workspace
        out: list[dict] = []
        for a in ws.artifacts:
            if a["phase"] == "_input" or a["phase"].endswith("_preprocessed"):
                continue
            try:
                abs_path = str(ws.resolve_artifact_handle(a["path"]))
            except (PermissionError, KeyError, TypeError):
                abs_path = a["path"]
            out.append({**a, "path": abs_path})
        return out

    def get_events(self) -> list:
        return self._runtime.events.all() if self._runtime else []

    def get_events_json(self) -> list[dict]:
        return self._runtime.events.to_json() if self._runtime else []

    @staticmethod
    def _make_run_id(skill_name: str) -> str:
        """Canonical OS-level run_id. ALL spawn paths must use this.

        Form: ``{ts_with_microseconds}_{safe_name}_{4hex}``

        - Microsecond timestamp + 4-hex random suffix together prevent
          collision on concurrent spawns of the same skill (= asyncio.gather
          scenarios). Microsecond precision handles the typical case;
          the 4-hex suffix is belt-and-suspenders for the genuine
          same-microsecond case.
        - Single canonical form across spawn → events.jsonl → TUI
          subscriber → state log. Prior cross-layer mismatch (=
          ``skill_runner.spawn`` adding its own ``_4-hex`` suffix while
          ``_make_run_id`` had no suffix) caused TUI ``remove_async_task``
          to fail keying, leaving rows stuck (= tui-coder finding #1
          cross-layer 2026-05-28). Fixed by funneling all spawn sites
          through this method.
        """
        # %f is 6-digit microseconds; placed inside the timestamp so the
        # full timestamp portion is `YYYYMMDDTHHMMSSffffffZ`.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_name = _safe_skill_name(skill_name)
        suffix = uuid.uuid4().hex[:4]
        return f"{ts}_{safe_name}_{suffix}"


def _safe_skill_name(name: str) -> str:
    """Produce a filename-safe skill name (truncated, alphanumeric / _ / -)."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", name)
    return cleaned.strip("_")[:40] or "skill"
