"""OpContext — execution environment for op handlers.

Bundles the dependencies an op needs from the surrounding frontend
(workspace, events, permissions, sub-skill resolution helpers) so the
handler signatures stay flat. Frontends construct an OpContext once
and reuse it for the whole phase or act loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.config import WebConfig
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.schemas.models import Skill
    from reyn.user_intervention import InterventionBus
    from reyn.workspace.workspace import Workspace


@dataclass
class OpContext:
    """Execution context passed to every op handler."""

    workspace: "Workspace"
    events: "EventLog"

    # Permissions
    permission_decl: "PermissionDecl"
    permission_resolver: "PermissionResolver | None" = None
    skill_name: str = ""

    # Sub-skill invocation
    skill: "Skill | None" = None  # current skill (for preloaded preprocessor sub-skills)
    model: str = "standard"
    resolver: "ModelResolver | None" = None
    subscribers: list = field(default_factory=list)
    output_language: str | None = None
    max_phase_visits: int = 25

    # run_skill state_dir layout strategy
    # When set, run_skill uses this path verbatim. When None, the handler
    # computes a layout based on `state_dir_strategy`.
    sub_state_dir_override: str | None = None
    state_dir_strategy: str = "control_ir"  # "control_ir" or "preprocessor"
    # Used when state_dir_strategy=="preprocessor"
    preprocessor_phase_name: str = ""
    preprocessor_step_index: int = 0

    # Shell / MCP
    shell_allowed: bool = False
    mcp_servers: dict = field(default_factory=dict)
    # Mutable cache for MCP HTTP clients keyed by server name
    mcp_clients: dict = field(default_factory=dict)

    # User interventions (ask_user, permission prompts in PR7)
    intervention_bus: "InterventionBus | None" = None
    current_phase: str = ""

    # PR20: caller provenance threaded from the parent Agent so sub-skill
    # invocations land under the same `events/<caller>/skill_runs/...` tree.
    # Format: "direct" or "agents/<name>" (validated in Agent).
    caller: str = "direct"

    # R-D13: nested skill lineage. The currently-running OSRuntime sets
    # this to its own ``run_id`` when constructing OpContext for control
    # IR execution. ``run_skill`` handlers propagate it to the spawned
    # child run as ``parent_run_id``, so the per-skill snapshot tree
    # records the parent / child relationship for ``/skill list``,
    # debug logs, and future cascade-discard semantics. ``None`` means
    # "no parent visible" (e.g. preprocessor-spawned sub-skills, or
    # OSRuntime invocations that don't track a run_id).
    parent_skill_run_id: str | None = None

    # FP-0021: the run_id of the currently-executing OSRuntime run.
    # Threaded from OSRuntime → ControlIRExecutor / PreprocessorExecutor →
    # OpContext so event emit helpers can stamp every event with the correct
    # run scope. None when the OpContext is created outside a run scope
    # (e.g. chat router, CLI commands).
    run_id: str | None = None

    # FP-0022 follow-up: declarative SSL config for web_fetch and MCP registry.
    # Defaults to WebConfig() (= no override, falls through to env-var chain).
    # Callers that have a ReynConfig available should pass config.web here.
    web_config: "WebConfig | None" = None
