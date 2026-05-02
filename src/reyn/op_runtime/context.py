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
    from reyn.schemas.models import Skill
    from reyn.workspace.workspace import Workspace
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.user_intervention import InterventionBus


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
    output_language: str = "ja"
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
