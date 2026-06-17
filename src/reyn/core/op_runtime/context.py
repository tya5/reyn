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
    import asyncio
    from collections.abc import Awaitable, Callable

    from reyn.config import MultimodalConfig, SandboxConfig, WebConfig
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.media_store import MediaStore
    from reyn.data.workspace.workspace import Workspace
    from reyn.llm.model_resolver import ModelResolver
    from reyn.schemas.models import Skill
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.security.sandbox import SandboxBackend
    from reyn.security.sandbox.policy import SandboxPolicy
    from reyn.security.secrets.store import ScopedSecretStore
    from reyn.user_intervention import RequestBus


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

    # MCP
    mcp_servers: dict = field(default_factory=dict)
    # Mutable cache for MCP HTTP clients keyed by server name
    mcp_clients: dict = field(default_factory=dict)
    # FP-0016 Component E: agent identity for X-Reyn-Agent-Id header on
    # outgoing MCP / external HTTP calls. Plumbed from Session's
    # ReynConfig.agent.id (= `reyn/<hostname>` by default). None
    # preserves prior behaviour for direct OpContext construction (e.g.
    # tests that don't simulate a multi-agent identity).
    agent_id: str | None = None

    # User interventions (ask_user, permission prompts in PR7)
    intervention_bus: "RequestBus | None" = None
    current_phase: str = ""

    # #272/#1128: voluntary-compaction capability for the `compact` op.
    # An awaitable zero-arg callable the caller (Session / phase runtime)
    # wires to its synchronous compaction (force_compact_now), returning
    # {"freed_tokens", "free_window_after", ...} in exact tokens. None when no
    # compaction context is available (e.g. preprocessor / direct construction)
    # → the compact op returns a clear error rather than silently no-op'ing
    # (same contract as ask_user without an intervention_bus).
    compact_now: "Callable[[], Awaitable[dict]] | None" = None

    # #1190 stage (ii): BudgetTracker for cost recording from ops that make LLM
    # calls (judge_output → purpose="judge"). Threaded by the OpContext builders
    # (control_ir_executor / router_host_adapter); None = unrecorded.
    budget_tracker: object | None = None

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

    # FP-0017 follow-up: declarative sandbox config for sandboxed_exec op.
    # Callers that have a ReynConfig available should pass config.sandbox here.
    # When None, sandboxed_exec falls back to platform auto-detection
    # (= same as no-config-loaded behavior).
    sandbox_config: "SandboxConfig | None" = None

    # FP-0008 C7 #2: runtime backend-instance override for sandboxed_exec.
    # When set, the sandboxed_exec handler uses this backend INSTANCE verbatim
    # instead of resolving one by name from sandbox_config. This is the seam for
    # *stateful* backends bound to a runtime resource (e.g. a
    # DockerEnvironmentBackend bound to a specific container + host workspace)
    # that a name-based factory cannot construct. Generic: any caller owning such a resource may inject
    # one; None preserves the default name-based platform auto-selection.
    sandbox_backend: "SandboxBackend | None" = None

    # FP-0008 #1115 Stage 2 (D): phase-level default SandboxPolicy (dict of
    # SandboxPolicy kwargs) declared in the phase frontmatter. When set, the
    # sandboxed_exec handler builds the policy from this (phase-default WINS over
    # the op's own fields) so a skill declares the policy once + the LLM cannot
    # override it (deterministic + P8-clean). None → use the op-level fields.
    default_sandbox_policy: dict | None = None

    # Issue #364: declarative cap on binary media size (= images from
    # web__fetch / file__read / MCP / user input). When None, the gate
    # is skipped — direct-OpContext constructions in tests stay
    # backward-compatible. Callers with a ReynConfig should pass
    # config.multimodal here.
    multimodal_config: "MultimodalConfig | None" = None

    # Issue #383 PR-C: flat-file storage for image binary + tool result
    # text dumps. Tool handlers (web__fetch / file__read / mcp) save
    # binary via ``ctx.media_store.save_image`` and emit path-ref blocks
    # in their op result instead of inline base64. When None, handlers
    # fall back to the pre-#383 inline shape (= backward-compat for
    # direct-OpContext tests).
    media_store: "MediaStore | None" = None

    # FP-0016 D: per-skill credential scoping. None = unrestricted (= today's
    # behaviour; preserves backward compat for top-level / chat-router /
    # CLI-direct OpContext construction). The run_skill handler constructs a
    # ScopedSecretStore based on the sub-skill's required_credentials and
    # passes it down through Agent → OSRuntime → executors → OpContext.
    secret_store: "ScopedSecretStore | None" = None

    # Issue #214 (= #180 #2 split): plan_step context for any skill spawned
    # within a plan step's RouterLoop. ``run_skill`` op propagates this
    # forward to the sub-skill's OSRuntime so the child EventLog stamps
    # ``plan_step`` into every emit, letting ChatEventForwarder render
    # "plan N/M" detail on the child's SkillActivityRow. None = top-level
    # (= not inside a plan step). Shape: ``{"n_done": int, "n_total": int,
    # "step_id": str}``.
    plan_step: dict | None = None

    # #1470: per-turn asyncio.Event fired by cancel_inflight(). When set,
    # sandboxed_exec backends kill the running subprocess instead of waiting
    # for it to complete. None = no cancel-awareness (OS-internal ops,
    # non-interactive callers, pre-#1470 tests).
    cancel_event: "asyncio.Event | None" = None


def sandbox_policy_from_ctx(ctx: "OpContext") -> "SandboxPolicy | None":
    """Build the ``SandboxPolicy`` from ``ctx.default_sandbox_policy`` (the
    agent-level operator policy resolved onto the ctx; #1326), or ``None`` when
    unset.

    #1199 S3.1c-2: the file / http gates fold this into their SandboxLayer ∩.
    Mirrors the conversion ``sandboxed_exec`` already uses
    (``SandboxPolicy(**ctx.default_sandbox_policy)``) so the SAME policy governs
    both the sandboxed_exec subprocess and the OS's in-process file/http ops.
    ``None`` → the SandboxLayer is ⊤ (non-sandboxed callers unchanged)."""
    if ctx.default_sandbox_policy is None:
        return None
    from reyn.security.sandbox.policy import SandboxPolicy

    return SandboxPolicy(**ctx.default_sandbox_policy)
