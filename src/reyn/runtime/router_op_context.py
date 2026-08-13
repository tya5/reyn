"""The chat-router OpContext: one supplier, one construction.

``build_router_op_context`` assembles the ``actor="chat_router"`` OpContext
(PermissionDecl with the #571 axes, the canonical ``.reyn/`` write paths +
session-approval, the Workspace FS root, the OpContext itself) and
:class:`RouterOpContextSource` is its **only** caller: one object per Session
owns the materials and answers "give me an op-context" for every chat-router
surface.

Why a supplier rather than a set of materials each host carries: the two
surfaces that need this OpContext — ``Session`` (its own ``_file_op`` / MCP
callbacks) and ``RouterHostAdapter`` (``op_context_factory``, the registry
dispatch path) — used to *each* call the factory, one from its own attributes
and one from a 16-field bundle of copies of those same attributes. Two call
sites assembling the same object is a drift surface, and it had already
drifted on twelve fields: ``agent_id``, ``presentation_renderer``,
``intervention_bus``, ``presentation_registry``, ``multimodal_config``,
``compact_now``, ``cancel_event``, ``threat_scan`` and ``session_id`` reached
one path and not the other, and ``allowed_mcp`` / ``contextual_permission`` /
``sandbox_policy`` were read LIVE on one path and frozen at construction on the
other. So which capabilities an op got depended on which door it came through,
and for three of them, on how long the session had been running.

Per-turn values are held as zero-arg **suppliers**, not snapshots: a value
captured when the Session is constructed (``turn_origin``,
``contextual_permission``, the live session id, the sandbox policy, the
hot-reloaded presentation registry / skill set) is right on the first turn and
wrong on every turn after it.
"""
from __future__ import annotations

from typing import Any

# Canonical OS mutation paths the chat router declares + session-approves so
# LLM-emitted mcp_install / index_drop / mcp_drop_server ops pass the uniform
# permission gates without per-op prompts (#571 collapse arc).
_CANONICAL_WRITE_PATHS = (
    ".reyn/config/mcp.yaml",
    ".reyn/config/cron.yaml",
    ".reyn/config/index/sources.yaml",
)


def build_router_op_context(
    *,
    events: Any,
    permission_resolver: Any,
    file_permissions: dict | None,
    mcp_servers: list[dict] | None,
    mcp_servers_flat: list[dict],
    allowed_mcp: list[str] | None,
    workspace_base_dir: Any,  # Path | None — #187 container repo root
    workspace_state_dir: Any,  # Path | None — host-side OS state dir
    environment_backend: Any,  # FS seam backend instance (#1200 PR-F1)
    sandbox_backend: Any,  # exec seam backend instance (#1200 PR-F2)
    sandbox_policy: Any,  # raw policy → resolve_sandbox_policy here (#1339)
    # ── fields the single supplier resolves per call ───────────────────────
    agent_id: str | None,  # FP-0016 identity → the MCP client's X-Reyn-Agent-Id header
    agent_name: str | None = None,  # #4574: the live agent's NAME — see OpContext.agent_name's own docstring for why this is a DIFFERENT value from agent_id above
    intervention_bus: Any = None,  # the surface that answers a router op's intervention
    presentation_renderer: Any,  # #2708 P1: REQUIRED (no default) — the present sink must be an EXPLICIT decision (a PresentationRenderer or None), never a silent omission.
    presentation_registry: Any = None,  # FP-0054 PR-C: operator named-template registry (hot-reloadable)
    multimodal_config: Any = None,  # #364
    web_fetch_config: Any = None,  # #4274: reyn.yaml web_fetch.* → the web_fetch op's SSL/SSRF/size gates
    read_cap_config: Any = None,  # #4381 PR-5: reyn.yaml read_cap.* → file.py's/load_skill.py's read op cap
    media_store: Any = None,  # #383
    compact_now: Any = None,  # #272/#1128
    run_id: str | None = None,  # chat router is outside run scope (#FP-0021)
    cancel_event: Any = None,  # #1470: asyncio.Event for mid-subprocess cancel
    ephemeral: bool = False,  # #3903 a-2 ③: Session._ephemeral, live — see OpContext.ephemeral's own docstring for what this DOES and does NOT mean
    attended: bool = True,  # #4193 ①: Session._attended, live — a SEPARATE axis from ephemeral, see OpContext.attended's own docstring
    threat_scan: Any = None,  # FP-0050/#1822 S5 (EP4): exec command-scan config
    contextual_permission: Any = None,  # #1827 S3: per-session capability narrowing → OpContext
    session_id: str | None = None,
    hook_dispatcher: Any = None,  # #1800 slice 5c: the Session's HookDispatcher
    hook_bus: Any = None,  # Hook-Event Redesign Phase 5 part 2: the Session's HookBus → emit_hook_event
    turn_origin: str | None = None,  # proposal 0060 Phase 1 (A7): OS-derived turn provenance → install-op stamping (A9)
    hot_reloader: Any = None,  # #2761 PR-2: this session's HotReloader → immediate mid-turn install apply
    render_template_bounds: Any = None,  # #2679: operator RenderTemplateBounds → the render_template op cap. None → the op's in-handler defaults.
    budget_gateway: Any = None,  # FP-0063 PC: the calling Session's per-session BudgetGateway → the `embed` op's single embedding-cost recording entry point (fans out to session scope + agent/project scope via the tracker it holds, keyed by agent NAME).
    available_skills: Any = None,  # #3196: the host's registered SkillEntry list → the `file` op's skill-load provenance gate (the config-registered-entry class, alongside builtin/plugin). None → that provenance class is simply unavailable (fails closed, not open).
) -> Any:
    """Build the chat-router OpContext.

    Assembly only: every value arrives resolved. The one caller is
    :meth:`RouterOpContextSource.build`, which owns the materials and decides
    when each is read — see the module docstring."""
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    file_perms = file_permissions or {}
    servers = mcp_servers or []

    file_read = [{"path": p, "scope": "recursive"} for p in file_perms.get("read", [])]
    file_write = [{"path": p, "scope": "recursive"} for p in file_perms.get("write", [])]
    mcp_names = [s["name"] for s in servers]

    # #571 collapse arc Phase 5: explicit list axes for the canonical mutations.
    file_write = list(file_write) + [
        {"path": p, "scope": "just_path"} for p in _CANONICAL_WRITE_PATHS
    ]
    # #3198: unlike http_get/secret_write above (both wildcarded here because
    # THEIR runtime gate is a separate per-value operator prompt), env_expand
    # has NO such runtime prompt — the allowlist IS the whole gate, so it must
    # read the OPERATOR'S actual reyn.yaml declaration, never a wildcard
    # default. Read straight off `permission_resolver._config` (the raw
    # `config.permissions` dict), the SAME idiom the supplier already uses for
    # `file.read`/`file.write` (`_get_file_permissions_for_router`) — read here,
    # in the one place every chat-router OpContext funnels through, rather than
    # at each surface that asks for one.
    _raw_perm_config = getattr(permission_resolver, "_config", None) or {}
    env_expand = (
        PermissionDecl._parse_secret_key_list(_raw_perm_config.get("env.expand"))
        if isinstance(_raw_perm_config, dict)
        else []
    )
    decl = PermissionDecl(
        file_read=file_read,
        file_write=file_write,
        mcp=mcp_names,
        allowed_mcp=allowed_mcp,
        # #571 Phase 7: wildcard http.get (per-host 4-layer prompt at runtime) +
        # the MCP registry host specifically (mcp_install pre-approval).
        http_get=[
            {"host": "registry.modelcontextprotocol.io"},
            {"host": "*"},
        ],
        # #571 Phase 6: wildcard secret.write (operator per-value prompt is the gate).
        secret_write=["*"],
        # #3198: deny-by-default — empty unless reyn.yaml declares `permissions.env.expand`.
        env_expand=env_expand,
    )
    # Session-approve the canonical OS mutation paths so require_file_write passes
    # silently for LLM-emitted ops. Skipped when no resolver (ad-hoc test ctx).
    if permission_resolver is not None:
        for canonical in _CANONICAL_WRITE_PATHS:
            permission_resolver.session_approve_path(canonical, "chat_router", "file.write")

    workspace = Workspace(
        events=events,
        permission_resolver=permission_resolver,
        actor="chat_router",
        # #187: chat OpContext FS root = the container repo root with a container
        # env-backend (e.g. /testbed); state_dir stays host-side. None → cwd default.
        base_dir=workspace_base_dir,
        state_dir=workspace_state_dir,
        environment_backend=environment_backend,
    )
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=permission_resolver,
        actor="chat_router",
        agent_name=agent_name,
        mcp_servers=mcp_servers_flat,
        run_id=run_id,
        agent_id=agent_id,
        intervention_bus=intervention_bus,
        presentation_renderer=presentation_renderer,
        presentation_registry=presentation_registry,
        multimodal_config=multimodal_config,
        web_fetch_config=web_fetch_config,  # #4274
        read_cap_config=read_cap_config,  # #4381 PR-5
        media_store=media_store,
        compact_now=compact_now,
        sandbox_backend=sandbox_backend,
        # #1339: resolve the operator-or-default sandbox policy (was None → the
        # op_runtime handler fell back to LLM-set op fields = sandbox-escape gap).
        default_sandbox_policy=resolve_sandbox_policy(
            sandbox_policy,
            write_paths=[str(workspace.base_dir)],
        ),
        cancel_event=cancel_event,
        ephemeral=ephemeral,
        attended=attended,
        threat_scan=threat_scan,
        contextual_permission=contextual_permission,
        session_id=session_id,
        hook_dispatcher=hook_dispatcher,  # #1800 slice 5c
        hook_bus=hook_bus,  # Hook-Event Redesign Phase 5 part 2: emit_hook_event's publish target
        turn_origin=turn_origin,  # proposal 0060 Phase 1 (A7): OS-authoritative provenance source (A9)
        hot_reloader=hot_reloader,  # #2761 PR-2: per-session reloader for immediate mid-turn install apply
        render_template_bounds=render_template_bounds,  # #2679: operator render_template output cap
        budget_gateway=budget_gateway,  # FP-0063 PC: the embed op's embedding-cost recording entry point
        available_skills=available_skills,  # #3196: config-registered-entry provenance class for the file op's skill-load gate
    )


class RouterOpContextSource:
    """The one supplier of chat-router OpContexts for a Session.

    Owns every material :func:`build_router_op_context` needs and is that
    function's ONLY caller, so "which capabilities does a chat-router op get"
    has one answer instead of one per surface. ``Session`` keeps a single
    instance and hands the SAME object to ``RouterHostAdapter``; both
    ``Session._make_router_op_context`` and
    ``RouterHostAdapter.make_router_op_context`` are one-line delegations to
    :meth:`build`, which is what makes the two entry points equivalent by
    construction rather than by review.

    **Suppliers, not snapshots.** Fields named ``*_fn`` are read at
    :meth:`build` time. Each of them is a value that changes AFTER this object
    is constructed — the per-turn provenance (``turn_origin``), a spawned
    session's real id, the operator's capability narrowing (built after the
    router waist, so it does not even exist yet at construction), the resolved
    sandbox policy, the two hot-reloadable registries, and (#4200)
    ``workspace_base_dir`` — a spawned session's real per-session base_dir
    override is fixed up by the registry AFTER this object is constructed,
    same shape as the real session id. A plain value in any of those slots is
    correct on the first turn and silently stale on every turn after it (or,
    for the spawn-time fields, correct for the PARENT and silently wrong for
    every spawned CHILD forever). A ``None`` supplier means "this Session
    wires no such value" and yields ``None`` (``mcp_servers_flat`` yields
    ``{}`` — its consumer indexes it).

    No parameter has a default: a caller that silently omits one would absorb a
    wiring change unnoticed, which is the failure mode #3482's default-free
    bundles were introduced to prevent.

    ``cancel_event`` is the one slot filled after construction — ``RouterLoop``
    creates the per-turn ``asyncio.Event`` and registers it via
    :meth:`set_cancel_event` (through ``RouterHostAdapter._set_cancel_event``),
    so it lives here rather than being copied into every holder that needs it.
    """

    def __init__(
        self,
        *,
        events: Any,
        permission_resolver: Any,
        file_permissions_fn: Any,
        mcp_servers_fn: Any,
        mcp_servers_flat_fn: Any,
        allowed_mcp_fn: Any,
        workspace_base_dir_fn: Any,
        workspace_state_dir: Any,
        environment_backend: Any,
        sandbox_backend: Any,
        sandbox_policy_fn: Any,
        agent_id: Any,
        agent_name: Any,  # #4574: the live agent's NAME — see OpContext.agent_name's own docstring
        intervention_bus_factory: Any,
        presentation_renderer_factory: Any,
        presentation_registry_fn: Any,
        multimodal_config: Any,
        web_fetch_config: Any,  # #4274: reyn.yaml web_fetch.* — plain value, same shape as multimodal_config
        read_cap_config: Any = None,  # #4381 PR-5: reyn.yaml read_cap.* — plain value, same shape as web_fetch_config
        media_store_fn: Any,
        compact_now: Any,
        threat_scan: Any,
        contextual_permission_fn: Any,
        session_id_fn: Any,
        hook_dispatcher: Any,
        hook_bus: Any,
        turn_origin_fn: Any,
        hot_reloader: Any,
        render_template_bounds: Any,
        budget_gateway: Any,
        available_skills_fn: Any,
        ephemeral_fn: Any,  # #3903 a-2 ③: Session._ephemeral, live — same reason turn_origin_fn/session_id_fn are `_fn`s, not values (reassigned post-construction)
        attended_fn: Any,  # #4193 ①: Session._attended, live — same reason as ephemeral_fn above
    ) -> None:
        self._events = events
        self._permission_resolver = permission_resolver
        self._file_permissions_fn = file_permissions_fn
        self._mcp_servers_fn = mcp_servers_fn
        self._mcp_servers_flat_fn = mcp_servers_flat_fn
        self._allowed_mcp_fn = allowed_mcp_fn
        self._workspace_base_dir_fn = workspace_base_dir_fn
        self._workspace_state_dir = workspace_state_dir
        self._environment_backend = environment_backend
        self._sandbox_backend = sandbox_backend
        self._sandbox_policy_fn = sandbox_policy_fn
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._intervention_bus_factory = intervention_bus_factory
        self._presentation_renderer_factory = presentation_renderer_factory
        self._presentation_registry_fn = presentation_registry_fn
        self._multimodal_config = multimodal_config
        self._web_fetch_config = web_fetch_config
        self._read_cap_config = read_cap_config
        self._media_store_fn = media_store_fn
        self._compact_now = compact_now
        self._threat_scan = threat_scan
        self._contextual_permission_fn = contextual_permission_fn
        self._session_id_fn = session_id_fn
        self._hook_dispatcher = hook_dispatcher
        self._hook_bus = hook_bus
        self._turn_origin_fn = turn_origin_fn
        self._hot_reloader = hot_reloader
        self._render_template_bounds = render_template_bounds
        self._budget_gateway = budget_gateway
        self._available_skills_fn = available_skills_fn
        self._ephemeral_fn = ephemeral_fn
        self._attended_fn = attended_fn
        self._cancel_event: Any = None

    @property
    def hot_reloader(self) -> Any:
        """#2073 S3 / #2761 PR-2: this session's HotReloader.

        Public because it has a SECOND consumer besides the OpContext —
        ``RouterLoop`` threads it onto the tool ctx so a self-reload tool
        reloads THIS session rather than a process global. Read from here so
        the two consumers share one holder."""
        return self._hot_reloader

    @property
    def cancel_event(self) -> Any:
        """The per-turn cancel event, or None outside a turn.

        Public because the MCP-listing seam needs the same event the OpContext
        carries; a second copy on the adapter is exactly the duplication this
        class exists to remove."""
        return self._cancel_event

    def set_cancel_event(self, event: Any) -> None:
        """#1470: register the turn's cancel event so a sandboxed op can
        observe cancellation mid-subprocess."""
        self._cancel_event = event

    @staticmethod
    def _resolve(supplier: Any, absent: Any = None) -> Any:
        """Call *supplier*, or yield *absent* when this Session wires none."""
        return supplier() if supplier is not None else absent

    def build(self) -> Any:
        """Build a chat-router OpContext with this Session's CURRENT state."""
        return build_router_op_context(
            events=self._events,
            permission_resolver=self._permission_resolver,
            file_permissions=self._resolve(self._file_permissions_fn),
            mcp_servers=self._resolve(self._mcp_servers_fn),
            mcp_servers_flat=self._resolve(self._mcp_servers_flat_fn, {}),
            allowed_mcp=self._resolve(self._allowed_mcp_fn),
            workspace_base_dir=self._resolve(self._workspace_base_dir_fn),
            workspace_state_dir=self._workspace_state_dir,
            environment_backend=self._environment_backend,
            sandbox_backend=self._sandbox_backend,
            sandbox_policy=self._resolve(self._sandbox_policy_fn),
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            intervention_bus=self._resolve(self._intervention_bus_factory),
            presentation_renderer=self._resolve(self._presentation_renderer_factory),
            presentation_registry=self._resolve(self._presentation_registry_fn),
            multimodal_config=self._multimodal_config,
            web_fetch_config=self._web_fetch_config,  # #4274
            read_cap_config=self._read_cap_config,  # #4381 PR-5
            media_store=self._resolve(self._media_store_fn),
            compact_now=self._compact_now,
            cancel_event=self._cancel_event,
            ephemeral=self._resolve(self._ephemeral_fn, False),
            attended=self._resolve(self._attended_fn, True),
            threat_scan=self._threat_scan,
            contextual_permission=self._resolve(self._contextual_permission_fn),
            session_id=self._resolve(self._session_id_fn),
            hook_dispatcher=self._hook_dispatcher,
            hook_bus=self._hook_bus,
            turn_origin=self._resolve(self._turn_origin_fn),
            hot_reloader=self._hot_reloader,
            render_template_bounds=self._render_template_bounds,
            budget_gateway=self._budget_gateway,
            available_skills=self._resolve(self._available_skills_fn),
        )
