"""OpContext — execution environment for op handlers.

Bundles the dependencies an op needs from the surrounding frontend
(workspace, events, permissions, resolver, and sub-run helpers) so the
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
    from reyn.core.events.state_log import StateLog
    from reyn.core.op_runtime.render_template import RenderTemplateBounds
    from reyn.core.present import PresentationRenderer
    from reyn.data.workspace.media_store import MediaStore
    from reyn.data.workspace.workspace import Workspace
    from reyn.llm.model_resolver import ModelResolver
    from reyn.mcp.connection_service import MCPConnectionService
    from reyn.mcp.pool import MCPClientPool
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
    actor: str = ""

    model: str = "standard"
    resolver: "ModelResolver | None" = None
    subscribers: list = field(default_factory=list)
    output_language: str | None = None

    # Sub-run state_dir layout strategy.
    # When set, the sub-run handler uses this path verbatim. When None, the
    # handler computes a layout based on `state_dir_strategy`.
    sub_state_dir_override: str | None = None
    state_dir_strategy: str = "control_ir"  # "control_ir" or "preprocessor"
    # Used when state_dir_strategy=="preprocessor"
    preprocessor_phase_name: str = ""
    preprocessor_step_index: int = 0

    # MCP
    mcp_servers: dict = field(default_factory=dict)
    # #a359 P2: the per-turn structured MCP client pool (owns open+reuse+close in one task). Replaces
    # the old raw ``mcp_clients`` dict (lazily filled by the op handler, closed by a separate teardown
    # in a possibly-different task → the cross-SDK-task cancel-scope crash). None outside an MCP
    # context (non-MCP ops never invoke the mcp handler).
    mcp_pool: "MCPClientPool | None" = None
    # #2597 S2a: the session-owned held-open connection service (Option C — one
    # persistent MCPClient per server, reused for the session's lifetime). When
    # set, the mcp op handler prefers THIS over ``mcp_pool`` (pool-compatible
    # ``get()`` — see connection_service.py). None for the ephemeral-session /
    # one-shot-probe path, which intentionally keeps the per-call ``mcp_pool``
    # (held connections would just churn for a sub-second-lived session).
    mcp_connection_service: "MCPConnectionService | None" = None
    # FP-0016 Component E: agent identity for X-Reyn-Agent-Id header on
    # outgoing MCP / external HTTP calls. Plumbed from Session's
    # ReynConfig.agent.id (= `reyn/<hostname>` by default). None
    # preserves prior behaviour for direct OpContext construction (e.g.
    # tests that don't simulate a multi-agent identity).
    agent_id: str | None = None

    # User interventions (ask_user, permission prompts in PR7)
    intervention_bus: "RequestBus | None" = None
    current_phase: str = ""

    # FP-0054 PR-B: the surface a `present` op renders to. None = PR-A's null-surface
    # behavior (no UI reached, resolve_bindings(surface="null")). A wired renderer names
    # its own `surface_name` (e.g. inline-CUI's OutboxPresentationRenderer = "inline-cui").
    presentation_renderer: "PresentationRenderer | None" = None

    # FP-0054 PR-C: the operator's named-view registry (from
    # `.reyn/config/presentations.yaml`) a `present` op resolves `op.view`
    # (FP-0055 PR-1 rename of `op.template`) against — the §3 fallback chain's
    # stage 1. A `PresentationRegistry` (duck-typed `.get(name) -> validated
    # nodes | None`). None = no registry wired (direct/test construction) →
    # every named view is "unknown" and falls through to the
    # generic fallback viewer (never a hard error). Sourced fresh per op-ctx build
    # from the session/adapter's current registry, so a hot-reload swap is picked up
    # at the next turn boundary.
    presentation_registry: "object | None" = None

    # FP-0055 PR-2: resource bounds for the `render_template` op (streaming
    # output-size + wall-clock cap, applied DURING generation). None → the
    # safety-spirit defaults (RenderTemplateBounds()). The override seam is used by
    # operator config (future yaml wiring) and by tests that inject a tiny cap; a
    # production-default None correctly applies the generous defaults in-handler.
    render_template_bounds: "RenderTemplateBounds | None" = None

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

    # PR20: caller provenance threaded from the parent Agent so sub-run
    # invocations land under the same `events/<caller>/...` tree.
    # Format: "direct" or "agents/<name>" (validated in Agent).
    caller: str = "direct"

    # R-D13: nested run lineage. The currently-running runtime sets
    # this to its own ``run_id`` when constructing OpContext for control
    # IR execution. Sub-run handlers propagate it to the spawned
    # child run as ``parent_run_id``, so the snapshot tree records the
    # parent / child relationship for debug logs and future cascade-discard
    # semantics. ``None`` means "no parent visible" (e.g. preprocessor-spawned
    # sub-runs, or runtime invocations that don't track a run_id).
    parent_run_id: str | None = None

    # FP-0021: the run_id of the currently-executing run.
    # Threaded from the runtime through the ctx-build seams to OpContext so
    # event emit helpers can stamp every event with the correct
    # run scope. None when the OpContext is created outside a run scope
    # (e.g. chat router, CLI commands).
    run_id: str | None = None

    # #2259 PR-1: the process-shared WAL, threaded so a recovery-core config op
    # (mcp_install / mcp_drop / index_drop) can record a config GENERATION (keyed by the
    # WAL head) after persisting its `.yaml` — making the yaml a derived projection of the
    # generation truth. None outside a persistence-enabled chat context (tests /
    # non-chat) → the op skips it (the opt-in contract, same as the step-event gate).
    state_log: "StateLog | None" = None

    # #2761 PR-2: this SESSION's HotReloader (the #2073 S3 per-session route), so an
    # install op (skill_install / pipeline_install) can apply its reload IMMEDIATELY
    # (mid-turn) for a PURE ADDITION — making the just-installed NEW entry resolvable
    # this turn. A same-name overwrite / no reloader keeps the deferred turn-boundary
    # path. Per-session (never the process-global get_active_hot_reloader, which is the
    # last-registered session — a multi-session footgun). None outside a live chat
    # session (tests / CLI separate-process install) → the op falls back to the deferred
    # path (unchanged behavior).
    hot_reloader: "object | None" = None

    # FP-0022 follow-up: declarative SSL config for web_fetch and MCP registry.
    # Defaults to WebConfig() (= no override, falls through to env-var chain).
    # Callers that have a ReynConfig available should pass config.web here.
    web_config: "WebConfig | None" = None

    # FP-0017 follow-up: declarative sandbox config for sandboxed_exec op.
    # Callers that have a ReynConfig available should pass config.sandbox here.
    # When None, sandboxed_exec falls back to platform auto-detection
    # (= same as no-config-loaded behavior).
    sandbox_config: "SandboxConfig | None" = None

    # FP-0050/#1822 S5 (EP4): content-threat scan config. When enabled, the
    # sandboxed_exec command (argv) is exec-scope scanned before exec — a
    # block-severity hit denies; warn emits + proceeds. None = no scan.
    threat_scan: "object | None" = None

    # #1827 S1: per-session contextual capability narrowing (a
    # ``ContextualPermission``). When set, permission gates add it as one more
    # restrict-only ∩ layer (ContextualLayer) on top of the static authority —
    # never-elevate is the structural ``all()`` in EffectivePermission. None =
    # no contextual narrowing (byte-identical to the pre-#1827 gate). Sourced
    # per-session from delegation / topology / ephemeral context (later slices).
    contextual_permission: "object | None" = None

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
    # the op's own fields) so a phase declares the policy once + the LLM cannot
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

    # FP-0016 D: per-run credential scoping. None = unrestricted (= preserves
    # backward compat for top-level / chat-router / CLI-direct OpContext
    # construction). When set, restricts secret access to the declared
    # required_credentials and is passed through Agent → the ctx-build seams.
    secret_store: "ScopedSecretStore | None" = None

    # #1470: per-turn asyncio.Event fired by cancel_inflight(). When set,
    # sandboxed_exec backends kill the running subprocess instead of waiting
    # for it to complete. None = no cancel-awareness (OS-internal ops,
    # non-interactive callers, pre-#1470 tests).
    cancel_event: "asyncio.Event | None" = None

    # #1953 slice 3a: the config-selected, session-scoped Task backend instance.
    # Threaded from the Session (which owns the session-scoped db path) down
    # through the ctx-build seams to OpContext, exactly like sandbox_backend.
    # The task.* op handlers use this when set; None → the op-runtime falls back
    # to its process-local in-memory backend (slice-1 stub for tests / direct
    # OpContext construction). Mirrors the contextual_permission (#1912) chain
    # across BOTH the control-IR and preprocessor ctx-build seams.
    task_backend: "object | None" = None

    # #1953 slice 6-ext: the OS TaskWaker driver (parallel to task_backend). The
    # abort/failed → parent-routing hub calls it to wake the parent's session to
    # decide recovery. None = no-op stub here (slice 7 wires the real TaskWaker +
    # threads it through the same Session → OpContext chain). Tests
    # inject a recording waker to verify the call-site fires.
    task_waker: "object | None" = None

    # #2187 backend-master: the Task SUBSCRIPTION writer (a SubscriptionWriter; parallel
    # to task_backend / task_waker, threaded down the SAME Session → OpContext
    # chain). The mutating task ops (create / reassign) call it to append the
    # task↔session BINDING to the WAL (the Reyn-internal subscription — what Reyn owns +
    # rewinds; the backend keeps task-STATE). None = no-op (direct/test construction or
    # no state_log) → the op skips the append (the opt-in contract). Tests inject a
    # recording writer to verify the binding append.
    task_subscription_writer: "object | None" = None

    # #1800 slice 5c: the awaited HookDispatcher (the Session's instance, with the
    # loaded hooks registry + the _put_inbox/_stage/_run_shell seams from 5b),
    # threaded down the SAME Session → router / kernel chain as task_waker. The
    # task op handlers (_create → task_start, _update_status→COMPLETED → task_end)
    # call ``ctx.hook_dispatcher.dispatch(...)``. None = no-op (direct/test
    # construction or no hooks) → the dispatch site is skipped.
    hook_dispatcher: "object | None" = None

    # #1953 slice 3 (rework): the caller's session identity (#1814 per-contextId
    # routing-key ``Session._session_id``), threaded down the same chain. This is
    # the single-writer key for Task ``update_status`` — the backend CAS-rejects
    # when ``task.assignee != ctx.session_id`` (assignee is immutable, so a fixed
    # equality suffices — no claim/version). agent_id (= agent_name) is too coarse
    # because one agent can own many per-contextId sessions (#1814). None = no
    # session identity (direct construction / OS-internal callers).
    session_id: "str | None" = None

    # #1953 §16 (recursive-request): the task_id the caller is currently EXECUTING
    # as a task-as-request, when this op-ctx is built for a turn the OS woke to
    # execute an assigned task. ``task.create`` reads this to derive ownership: set
    # → the new sub-task is owned by this task (``requester=current_task_id``,
    # ``requester_kind=task``); None → a top-level/session-owned task
    # (``requester=session_id``, ``requester_kind=session``). OS-SET from the
    # execution context (NOT an op field — the recursive-request invariant requires
    # the LLM cannot mark ownership). This is the STABLE seam: its SOURCE evolves
    # (execute-wake meta now; a persistent session-assignment later) but the
    # ``_create`` read-side stays fixed. None = not executing a task-as-request.
    current_task_id: "str | None" = None


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
