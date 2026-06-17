"""RouterHostAdapter — concrete RouterLoopHost implementation.

Extracted from Session wave 3 PR3. Composes Session's collaborators
(MemoryService, SnapshotJournal, op-runtime callbacks) so RouterLoop has no
direct dependency on Session internals. The adapter satisfies the
RouterLoopHost Protocol structurally; Session constructs one and exposes
it via `self._router_host`.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from reyn.core.events.events import EventLog

logger = logging.getLogger(__name__)

_DEFAULT_STATE_DIR = Path(".reyn") / "state"


class RouterHostAdapter:
    """Concrete RouterLoopHost implementation extracted from Session.

    Holds injected identity attrs, catalogue deps, and async callbacks so
    RouterLoop can call host methods without importing or referencing
    Session directly.

    Parameters
    ----------
    agent_name:
        Identity attribute — forwarded as ``chat_id`` and ``agent_name``.
    agent_role:
        Identity attribute.
    output_language:
        BCP-47 code or None. Stored as a plain attribute (not property) per
        the RouterLoopHost Protocol.
    allowed_skills:
        Optional allowlist for skill enumeration.
    allowed_mcp:
        Optional allowlist for MCP server scope (forwarded to PermissionDecl).
    permission_resolver:
        PermissionResolver instance (or None) for config-derived gates.
    mcp_servers:
        Raw MCP server config dict (may have ``{servers: {...}}`` wrapper).
    project_context:
        Project context text injected into the router system prompt.
    events:
        The session's EventLog — exposed as ``host.events``.
    resolver:
        ModelResolver instance for ``resolve_model``.
    memory:
        MemoryService instance for ``memory_path`` / ``memory_dir``.
    journal:
        SnapshotJournal instance for plan-lifecycle persistence.
    agent_registry:
        AgentRegistry (or None) for listing reachable peers.
    skill_enumerate_fn:
        Callable ``(exclude: set[str]) -> list[dict]`` — wraps
        ``enumerate_available_skills`` without importing it here.
    agent_workspace_dir:
        Path to ``.reyn/agents/<agent_name>`` — used for ``get_memory_index``.
    plan_registry_getter:
        Zero-arg callable returning the current PlanRegistry (or None).
    file_read:
        Async callback ``(path: str) -> dict``.
    file_write:
        Async callback ``(path: str, content: str) -> dict``.
    file_delete:
        Async callback ``(path: str) -> dict``.
    file_list_directory:
        Async callback ``(path: str) -> dict``.
    file_regenerate_index:
        Async callback ``(*, path, output_path, entry_template, header) -> dict``.
    mcp_list_servers:
        Async callback ``() -> list[dict]``.
    mcp_list_tools:
        Async callback ``(server: str) -> list[dict]``.
    mcp_call_tool:
        Async callback ``(server: str, tool: str, args: dict) -> dict``.
    run_skill_awaitable:
        Async callback ``(*, skill: str, input: dict, chain_id: str) -> dict``.
    spawn_skill:
        Async callback ``(*, skill, input, chain_id) -> dict`` — FP-0012
        non-blocking dispatch returning the spawn-ack
        ``{status: "spawned", run_id, chain_id, note}`` immediately.
    send_to_agent:
        Async callback ``(*, to, request, depth, chain_id) -> None``.
    put_outbox:
        Async callback ``(OutboxMessage) -> None`` — the raw outbox put.
    append_history:
        Sync callback ``(ChatMessage) -> None``.
    spawn_plan_task:
        Async callback forwarded from session's ``spawn_plan_task``.
    delegation_tracker:
        Zero-arg callable returning the current ``list[dict] | None``.
    agent_replies_tracker:
        Zero-arg callable returning the current ``list[str] | None``.
    """

    # RouterLoopHost Protocol attributes (non-property)
    output_language: str | None

    def __init__(
        self,
        *,
        agent_name: str,
        agent_role: str,
        output_language: str | None,
        allowed_skills: list[str] | None,
        allowed_mcp: list[str] | None,
        permission_resolver: Any,               # PermissionResolver | None
        mcp_servers: dict | None,
        project_context: str,
        events: EventLog,
        resolver: Any,                          # ModelResolver
        memory: Any,                            # MemoryService
        journal: Any,                           # SnapshotJournal
        agent_registry: Any,                    # AgentRegistry | None
        skill_enumerate_fn: Callable[[set], list],
        agent_workspace_dir: Path,
        plan_registry_getter: Callable[[], Any],
        # File op callbacks
        file_read: Callable[..., Awaitable[dict]],
        file_write: Callable[..., Awaitable[dict]],
        file_delete: Callable[..., Awaitable[dict]],
        file_list_directory: Callable[..., Awaitable[dict]],
        file_regenerate_index: Callable[..., Awaitable[dict]],
        # MCP op callbacks
        mcp_list_servers: Callable[..., Awaitable[list]],
        mcp_list_tools: Callable[..., Awaitable[list]],
        mcp_call_tool: Callable[..., Awaitable[dict]],
        # Action callbacks
        run_skill_awaitable: Callable[..., Awaitable[dict]],
        spawn_skill: Callable[..., Awaitable[dict]],
        send_to_agent: Callable[..., Awaitable[None]],
        put_outbox: Callable[..., Awaitable[None]],
        append_history: Callable,
        spawn_plan_task: Callable[..., Awaitable[None]],
        # Tracker getters (return mutable list or None)
        delegation_tracker: Callable[[], "list[dict] | None"],
        agent_replies_tracker: Callable[[], "list[str] | None"],
        # FP-0034 PR-3b-iii/iv: universal catalog wrapper visibility
        # (= reyn.yaml action_retrieval.universal_wrappers_enabled).
        # Session passes True by default since PR-3b-iv flipped the
        # ActionRetrievalConfig default; this constructor parameter
        # still defaults to False so direct callers (= tests that build
        # adapters by hand) preserve the prior tools= shape and don't
        # accidentally activate wrappers without intent.
        universal_wrappers_enabled: bool = False,
        # FP-0034 Phase 2 step 1: ActionEmbeddingIndex + EmbeddingProvider
        # for search_actions.  When all three are set (= operator configured
        # ``action_retrieval.embedding_class`` AND Session built a
        # provider AND the index has been initialized), search_actions
        # appears in tools= and routes to the index.  When any is None
        # the wrapper stays out of tools= (= D14 visibility gate).
        action_embedding_index: Any = None,
        embedding_provider: Any = None,
        embedding_model_class: str | None = None,
        # FP-0034 Phase 2: sandbox backend name for exec D14 visibility
        # gate. Passed from ``session._sandbox_config.backend`` so the
        # universal catalog ``_enumerate_category("exec")`` can decide
        # whether to expose ``exec__sandboxed_exec``. Default None hides
        # the exec category (= noop / no sandbox configured).
        sandbox_backend: str | None = None,
        sandbox_policy: dict | None = None,
        # #187: the FS EnvironmentBackend INSTANCE (docker for in-container repos)
        # + the container repo root + host-side state dir, for the router OpContext
        # Workspace. Distinct from ``sandbox_backend`` (a STRING for the exec D14
        # gate). Without these the LIVE file-op dispatch built a host-cwd Workspace
        # (the #187 wrong-FS defect: file ops on the reyn repo, not /testbed).
        environment_backend: Any = None,
        workspace_base_dir: Path | None = None,
        workspace_state_dir: Path | None = None,
        # #187 exec-seam (10th defect): the SandboxBackend INSTANCE for exec
        # EXECUTION (docker for in-container repos). Distinct from the
        # ``sandbox_backend`` STRING above (which only drives the D14 exec
        # visibility gate): the op handler (op_runtime/sandboxed_exec) reads
        # ``ctx.sandbox_backend`` (the instance) and falls back to the host
        # seatbelt backend when it is None. Without threading this, the LIVE
        # router's exec ran on the host (``No such file or directory:
        # '/testbed'``), so the agent's verify loop always failed. Parallel to
        # ``environment_backend`` for FS (#1411) — the legacy
        # ``Session._make_router_op_context`` already passes it; the live
        # adapter omitted it (the same live-vs-legacy seam gap as #1410/#1411).
        sandbox_backend_instance: Any = None,
        # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list freq+recency.
        # Session passes the session-scoped tracker; None when wrappers are
        # off or hot_list_n == 0.
        action_usage_tracker: Any = None,
        # FP-0034 refactor: zero-arg callable returning the live (=
        # uncompacted) tool-call ``(qualified_name, ts_epoch)`` records
        # extracted from the current chat history. Combined with the
        # tracker's compacted table to produce the hot-list ranking.
        # None → router degrades to compacted-table-only ranking
        # (= older test hosts / plan-mode sub-host).
        uncompacted_tool_call_records_fn: (
            Callable[[], list[tuple[str, float]]] | None
        ) = None,
        # FP-0034 Phase 2 step 5: ActionRetrievalConfig for hot_list_n /
        # hot_list_seed.  Session passes its config; None → default.
        action_retrieval_config: Any = None,
        # B25-S5-1: when True, RouterLoop awaits the action embedding index
        # build synchronously on the first turn before computing the D14
        # search_actions visibility gate. Off by default (= lazy bg build).
        eager_embedding_build: bool = False,
        # FP-0022 fix (#53): callable that yields an InterventionBus for
        # router-initiated tools that need the 4-layer approval flow
        # (web_fetch interactive prompt, mcp install / drop ask gates).
        # Session passes a factory that wraps ``ChatInterventionBus(
        # session, run_id=None, skill_name="chat_router")``; tests can
        # pass None and the OpContext gets ``intervention_bus=None`` (=
        # config-deny path still raises, interactive prompt path raises
        # the documented RuntimeError telling the caller a bus is needed).
        intervention_bus_factory: Callable[[], Any] | None = None,
        # Issue #364 multi-modal cluster: media-size gate config (reyn.yaml
        # ``multimodal:`` section). Threaded into the OpContext built by
        # ``make_router_op_context`` so router-initiated web_fetch /
        # file__read / mcp ops consult the cap + on_oversize policy.
        # ``None`` = no cap.
        multimodal_config: Any = None,
        # #1652: ReasoningConfig (continuity/display/recent_turns) + the session
        # callback that renders the bounded prior-reasoning text section (reads
        # history + applies the continuity gate). None → reasoning disabled.
        reasoning_config: Any = None,
        reasoning_continuity_section_fn: "Callable[[], str] | None" = None,
        # Issue #383 PR-C: media + tool-result file storage.
        media_store: Any = None,
        # #1128 size axis: per-turn tool-result cap/offload callable. Takes the
        # serialised tool-result string and returns it unchanged (within cap) or
        # an offloaded bounded preview. ``None`` = no cap (identity).
        cap_tool_result: Any = None,
        # #272 media axis: callable (tool_content_str) -> int giving the tokens
        # left for the media follow-up after the (capped) tool text, so
        # router_loop bounds media materialisation. ``None`` = unbounded (pre-#272).
        media_followup_budget: Any = None,
        # #272/#1128 compact op: awaitable () -> {freed_tokens, free_window_after}
        # wired by Session to its force_compact_now wrapper, so the LLM-
        # emittable `compact` control_ir op can voluntarily compact history.
        # ``None`` = no compaction context (compact op returns a clear error).
        compact_now: Any = None,
        # #272/#1128 context-size signal: callable () -> {free_window,
        # effective_trigger} (exact tokens) for the OS-injected SP header.
        # ``None`` = no signal rendered (e.g. test stubs).
        context_window_status: Any = None,
        # FP-0037 S1: persistent MCP tools cache directory.
        # Default is Path(".reyn/state") which resolves relative to cwd
        # (= the project root in all production entry points). Tests pass
        # a tmp_path subdirectory to isolate writes.
        state_dir: Path | None = None,
        # FP-0037 S2: project root for yaml mtime watch (3-scope cascade).
        # When None, only the user-global ~/.reyn/config.yaml is watched.
        # Session passes the project root so all 3 tiers are covered.
        project_root: Path | None = None,
        # #1092 PR-F1 (chat activation): the shared turn_budget engine the chat
        # axis budgets against. Built by Session via
        # build_default_turn_budget_engine off the CompactionEngine's RESOLVED
        # model (#1172-safe). Sole consumer (for now) is wrap_up_output_reserve —
        # which hard-caps the force-close wrap-up call's output. None = no engine
        # (legacy / test paths) → no cap (== pre-PR-F behaviour). ADDITIVE: chat
        # never calls _force_close_call until the F2 handoff lands, so wiring the
        # reserve here is inert until then.
        turn_budget_engine: Any = None,
        # #1468: cooperative turn-cancel signal. Session passes
        # self._is_turn_cancel_requested; test hosts pass None (= never cancel).
        # run_loop polls via getattr(host, "_is_turn_cancel_requested", None).
        turn_cancel_fn: "Callable[[], bool] | None" = None,
    ) -> None:
        self._turn_budget_engine = turn_budget_engine
        self._turn_cancel_fn = turn_cancel_fn  # #1468
        self._agent_name = agent_name
        self._agent_role = agent_role
        self.output_language = output_language
        self._allowed_skills = allowed_skills
        self._allowed_mcp = allowed_mcp
        self._perm = permission_resolver
        self._mcp_servers = mcp_servers
        # Lazy per-session cache for MCP tools — populated by
        # ensure_mcp_tools_cached() on the first user turn; None means
        # "not yet probed". See FP-0037 issue #160.
        self._mcp_tools_cache: dict[str, list[dict]] | None = None
        # FP-0037 S1: mtime of the cache file when we last loaded from it.
        # None = never loaded from disk. Used by maybe_reload_mcp_tools_cache_from_disk
        # to detect when the CLI has written a fresher version.
        self._mcp_tools_cache_mtime: float | None = None
        # FP-0037 S1: state dir for the persistent cache file.
        self._state_dir: Path = Path(state_dir) if state_dir is not None else _DEFAULT_STATE_DIR
        # FP-0037 S2: project root for yaml scope path resolution.
        # None = no project yaml tiers (user-global only).
        self._project_root: Path | None = (
            Path(project_root) if project_root is not None else None
        )
        # FP-0037 S2: last-seen mtimes for the 3 yaml scope tier files.
        # Keyed by Path; absent = never seen. Populated on first call to
        # maybe_refresh_mcp_tools_from_yaml; used to detect changes.
        self._yaml_mtimes_seen: dict[Path, float] = {}
        self._project_context = project_context
        self._events = events
        self._resolver = resolver
        self._memory = memory
        self._journal = journal
        self._registry = agent_registry
        self._skill_enumerate_fn = skill_enumerate_fn
        self._workspace_dir = Path(agent_workspace_dir)
        self._plan_registry_getter = plan_registry_getter
        # File callbacks
        self._file_read_cb = file_read
        self._file_write_cb = file_write
        self._file_delete_cb = file_delete
        self._file_list_directory_cb = file_list_directory
        self._file_regenerate_index_cb = file_regenerate_index
        # MCP callbacks
        self._mcp_list_servers_cb = mcp_list_servers
        self._mcp_list_tools_cb = mcp_list_tools
        self._mcp_call_tool_cb = mcp_call_tool
        # Action callbacks
        self._run_skill_awaitable_cb = run_skill_awaitable
        self._spawn_skill_cb = spawn_skill
        self._send_to_agent_cb = send_to_agent
        self._put_outbox_cb = put_outbox
        self._append_history_cb = append_history
        self._spawn_plan_task_cb = spawn_plan_task
        # Tracker getters
        self._delegation_tracker = delegation_tracker
        self._agent_replies_tracker = agent_replies_tracker
        # FP-0034 PR-3b-iii
        self._universal_wrappers_enabled = universal_wrappers_enabled
        # B25-S5-1
        self._eager_embedding_build = eager_embedding_build
        # FP-0034 Phase 2 step 1
        self._action_embedding_index = action_embedding_index
        self._embedding_provider = embedding_provider
        self._embedding_model_class = embedding_model_class
        # FP-0034 Phase 2
        self._sandbox_backend = sandbox_backend
        self._sandbox_backend_instance = sandbox_backend_instance
        self._environment_backend = environment_backend
        self._workspace_base_dir = workspace_base_dir
        self._workspace_state_dir = workspace_state_dir
        # #1339 / sandbox-model completion: the operator's reyn.yaml
        # sandbox.policy (dict | None) used to resolve the concrete agent-level
        # policy onto the router OpContext (None → the default policy).
        self._sandbox_policy = sandbox_policy
        # FP-0034 Phase 2 step 5
        self._action_usage_tracker = action_usage_tracker
        self._uncompacted_tool_call_records_fn = uncompacted_tool_call_records_fn
        self._action_retrieval_config = action_retrieval_config
        # FP-0022 fix (#53): intervention-bus factory used by
        # make_router_op_context to populate ``ctx.intervention_bus`` so
        # web_fetch / mcp install / mcp drop handlers can run their
        # interactive (Layer 4) approval flow without crashing on
        # ``intervention_bus is None`` defensive guards.
        self._intervention_bus_factory = intervention_bus_factory
        # Issue #364: store the gate config so make_router_op_context can
        # thread it into the OpContext for router-initiated binary ops.
        self._multimodal_config = multimodal_config
        # #1652: reasoning capture/continuity/display config + the section renderer.
        self._reasoning_config = reasoning_config
        self._reasoning_continuity_section_fn = reasoning_continuity_section_fn
        # Issue #383 PR-C: store the MediaStore for path-ref save/read.
        self._media_store = media_store
        # #1128 size axis: per-turn tool-result cap/offload callable (or None).
        self._cap_tool_result = cap_tool_result
        # #272 media axis: per-turn media-budget provider (or None).
        self._media_followup_budget = media_followup_budget
        # #272/#1128 compact op: voluntary-compaction callable (or None).
        self._compact_now = compact_now
        # #1470: per-turn cancel event set by RouterLoopDriver._set_cancel_event.
        # None until RouterLoopDriver registers itself at construction time.
        self._cancel_event: asyncio.Event | None = None
        # #272/#1128 context-size signal: live budget provider (or None).
        self._context_window_status = context_window_status

    @property
    def wrap_up_output_reserve(self) -> int | None:
        """#1092 PR-F1: the force-close wrap-up call's OUTPUT budget
        (``output_reserve``), or None when the chat axis has no turn_budget engine.
        ``RouterLoop._force_close_call`` passes it as ``max_tokens`` to HARD-CAP the
        consolidation ≤ output_reserve — the by-construction guarantee that the
        re-injected handoff stays below threshold (``assert_turn_budget_bounds``,
        run at engine construction, enforces output_reserve + offload_cap <
        threshold). Mirrors ``PhaseRouterLoopHost.wrap_up_output_reserve``.

        NOTE — chat deliberately exposes ONLY this (the wrap-up cap), not
        ``should_force_close``: chat is REACTIVE-only. Unlike a phase (task
        execution — proactively force-closing to wrap-up-and-continue is correct
        because the phase has a goal), chat is a *live conversation* where a
        proactive mid-turn force-close would truncate the user's conversation
        prematurely. So chat handles growth via the bounded ``retry_loop`` shrink
        and force-closes only at the last-resort floor-exhausted terminal (the F2
        handoff). This is a deliberate per-axis architectural choice
        (failure-mode separation), NOT a missing proactive trigger."""
        engine = self._turn_budget_engine
        return engine.budget.output_reserve if engine is not None else None

    def _is_turn_cancel_requested(self) -> bool:
        """#1468: True when the session has requested a cooperative turn cancel.

        Polled by run_loop at the top of each iteration via
        ``getattr(host, "_is_turn_cancel_requested", None)``. Returns False
        when no ``turn_cancel_fn`` was wired (= test hosts / phase sub-hosts).
        """
        return bool(self._turn_cancel_fn and self._turn_cancel_fn())

    # --- RouterLoopHost identity attributes ---

    def cap_tool_result(self, content_str: str) -> str:
        """#1128 size axis: cap an oversized tool-result string at the
        router_loop chokepoint. Delegates to the session-supplied callable
        (which offloads the full body via the #385 store + returns a bounded
        preview); identity when no capper was wired (= legacy / test paths).
        """
        if self._cap_tool_result is None:
            return content_str
        return self._cap_tool_result(content_str)

    def media_followup_budget(self, tool_content: str) -> int | None:
        """#272 media axis: tokens left for a tool turn's media follow-up after
        its (capped) text, or None when no media bound is wired (= pre-#272
        unbounded). router_loop passes this to the media-followup builder so
        overflow media stays a small lossless ref and the turn stays ≤ cap.
        """
        if self._media_followup_budget is None:
            return None
        return self._media_followup_budget(tool_content)

    @property
    def media_store(self) -> Any:
        """Issue #383 PR-C: expose the session's MediaStore so the
        RouterLoop's media-followup builder can materialise path-ref
        blocks at the wire boundary. ``None`` when no multimodal config
        was supplied (= legacy / test paths).
        """
        return self._media_store

    @property
    def chat_id(self) -> str:
        """chat_id — same as agent_name per protocol convention."""
        return self._agent_name

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def agent_role(self) -> str:
        return self._agent_role

    @property
    def events(self) -> Any:
        """EventLog for dispatch_tool events."""
        return self._events

    @property
    def permission_resolver(self) -> Any:
        """PermissionResolver bound at construction (= session's resolver).

        Exposed so ``RouterLoop._invoke_via_registry`` can populate the
        ToolContext.permission_resolver universal field via getattr; without
        this property the lookup falls through to None and every Tier-1
        config-deny check (web.fetch, mcp, …) silently bypasses for the
        router-invoked path. See #53 for the original silent-bypass bug.
        """
        return self._perm

    # --- Catalogue accessors ---

    def list_available_skills(self) -> list[dict]:
        """Return enumerated skills with router excluded.

        (FP-0011: skill_narrator was removed; the router LLM narrates inline.
        PR-N3: chat_compactor skill retired — compaction is now OS-internal.)
        """
        avail = self._skill_enumerate_fn({"skill_router"})
        if self._allowed_skills is not None:
            allow = set(self._allowed_skills)
            avail = [s for s in avail if s.get("name") in allow]
        return avail

    def list_available_agents(self) -> list[dict]:
        """Return topology-reachable peers (PR11/PR12)."""
        if self._registry is not None:
            return list(self._registry.iter_reachable_agents(self._agent_name))
        return []

    def get_memory_index(self) -> dict:
        """Return merged shared + agent memory index."""
        from reyn.chat.session import _merge_memory_indexes
        return _merge_memory_indexes(
            shared_path=Path(".reyn") / "memory" / "MEMORY.md",
            agent_path=self._workspace_dir / "memory" / "MEMORY.md",
            agent_name=self._agent_name,
        )

    def get_file_permissions(self) -> dict | None:
        return self._get_file_permissions_for_router()

    def get_mcp_servers(self) -> list[dict]:
        return self._get_mcp_servers_for_router()

    def get_web_fetch_allowed(self) -> bool:
        """Always returns True — FP-0022: web_fetch is now always in the catalog.

        The catalog-level gate has been removed; authorization is enforced at the
        handler level via PermissionResolver.require_web_fetch() (4-layer approval:
        config / approvals.yaml / session / interactive).

        Method kept for backward compatibility with RouterLoopHost protocol.
        """
        return True

    def get_project_context(self) -> str:
        """Project context text (REYN.md / ``project_context_path``).

        Threaded into the router's system prompt so casual chat queries see
        the operator's project context. Empty string when not configured.
        """
        return self._project_context or ""

    def get_cwd(self) -> str:
        """Agent-visible working directory for the SP Environment section.

        Backend-aware: when an environment backend is configured (e.g.
        DockerEnvironmentBackend), the agent sees the in-container path
        (backend.repo_dir) rather than the host cwd — these diverge when
        the repo is mounted inside a container at a different path. Without
        this fix the SP shows the host path but the actual FS/exec ops run
        against the container repo_dir (frame mismatch + host path leak).

        Resolution order (getattr-guarded for forward compat):
        1. backend.repo_dir  — ContainerBackend (e.g. DockerEnvironmentBackend)
        2. os.getcwd()       — HostBackend or no backend
        """
        import os
        repo_dir = getattr(self._environment_backend, "repo_dir", None)
        if repo_dir:
            return str(repo_dir)
        return os.getcwd()

    def get_environment_info(self) -> dict:
        """System metadata for the SP Environment section (#1479).

        Always returns:
          - ``date``       — today ISO-8601 (host-clock, universal)

        Returns additionally when the backend is absent or is a HostBackend
        (no ``repo_dir`` — same marker as #1477):
          - ``platform``   — OS family lower-cased ("linux", "darwin", …)
          - ``os_version`` — kernel/OS release string
          - ``shell``      — default shell executable
          - ``is_git_repo`` — bool; True when a .git entry exists at cwd

        When a non-host backend is present (``repo_dir`` set = container):
          - If backend implements ``get_environment_info()`` → use those values
          - If not implemented → omit platform/os_version/shell/is_git_repo
            (degrade, don't guess — returning host darwin/zsh for a linux
            container would repeat the #1477 host-value-leak pattern)

        Container probe (#1481): ``DockerEnvironmentBackend.get_environment_info``
        collects platform/os_version/shell/is_git_repo from INSIDE the container.
        The omission semantics above still hold when a probe sub-field fails.
        """
        import datetime
        import os
        import platform as _platform
        from pathlib import Path

        backend = self._environment_backend
        result: dict = {"date": datetime.date.today().isoformat()}

        # Determine whether this is a non-host (container) backend.
        # Same marker as get_cwd() (#1477): presence of repo_dir signals
        # a container backend whose agent-visible environment differs from host.
        _is_non_host_backend = bool(getattr(backend, "repo_dir", None))

        if _is_non_host_backend:
            # Non-host backend: only use values the backend explicitly provides.
            # If it doesn't implement get_environment_info(), omit all host-derived
            # fields — showing host platform/shell for a container = wrong context.
            _info_fn = getattr(backend, "get_environment_info", None)
            if callable(_info_fn):
                try:
                    backend_info = _info_fn() or {}
                except Exception:
                    backend_info = {}
                result["platform"] = backend_info.get("platform", "")
                result["os_version"] = backend_info.get("os_version", "")
                if backend_info.get("shell"):
                    result["shell"] = backend_info["shell"]
                # #1481: is_git_repo from the IN-CONTAINER probe — NOT a host-path
                # check. ``get_cwd()`` returns the container ``repo_dir``, so
                # ``(repo_dir / ".git").exists()`` on the host tests the wrong (or
                # absent) path — a #1477-class host/container frame mismatch. Use
                # the backend's value; omit when the probe didn't supply it.
                if "is_git_repo" in backend_info:
                    result["is_git_repo"] = bool(backend_info["is_git_repo"])
            # else: non-host backend without probe → omit platform/shell/git
        else:
            # Host backend or no backend: derive from local environment.
            result["platform"] = _platform.system().lower()
            result["os_version"] = _platform.release()
            _shell = os.environ.get("SHELL", "")
            if _shell:
                result["shell"] = _shell
            cwd_path = Path(self.get_cwd())
            result["is_git_repo"] = (cwd_path / ".git").exists()

        return result

    def get_universal_wrappers_enabled(self) -> bool:
        """Return whether FP-0034 universal catalog wrappers are enabled.

        Mirror of the ``action_retrieval.universal_wrappers_enabled`` flag
        from reyn.yaml. RouterLoop calls this when building tools= so the
        4 wrappers (list_actions / describe_action / invoke_action;
        search_actions gated separately by §D14) appear in the LLM's
        function-calling catalog. Default False preserves the prior
        tools= shape.
        """
        return self._universal_wrappers_enabled

    def get_action_embedding_index(self) -> Any:
        """Return the ActionEmbeddingIndex instance, or None.

        FP-0034 Phase 2 step 1.  Bound by Session when the operator
        has configured ``action_retrieval.embedding_class``.  RouterLoop
        forwards into ``RouterCallerState.action_embedding_index`` so
        the ``search_actions`` handler can call ``query()``.
        """
        return self._action_embedding_index

    def get_embedding_provider(self) -> Any:
        """Return the session's EmbeddingProvider instance, or None.

        FP-0034 Phase 2 step 1.  Used together with
        ``get_action_embedding_index()`` to power search_actions.
        """
        return self._embedding_provider

    def get_embedding_model_class(self) -> str | None:
        """Return the configured embedding model class name, or None.

        FP-0034 Phase 2 step 1.  Mirror of
        ``action_retrieval.embedding_class`` from reyn.yaml.  Used by
        ``RouterLoop._build_router_caller_state`` to bind the
        ``embedding_model_class`` field on ``RouterCallerState``.
        """
        return self._embedding_model_class

    def get_eager_embedding_build(self) -> bool:
        """Return True if RouterLoop should await the action embedding
        index build synchronously before computing the search_actions
        visibility gate on the first turn.

        B25-S5-1 fix for the cold-start race where ``is_ready()`` is False
        on Turn 1 because the background build hasn't finished, hiding
        ``search_actions`` from the LLM and inviting tool-name
        hallucinations (= B24 dogfood evidence: 2/3 hallucinated calls).
        Default False preserves the prior lazy background-build behavior.
        """
        return self._eager_embedding_build

    def get_sandbox_backend(self) -> str | None:
        """Return the configured sandbox backend name, or None.

        FP-0034 Phase 2.  Mirror of ``sandbox.backend`` from reyn.yaml
        (resolved via ``session._sandbox_config.backend``).  RouterLoop
        forwards this into ``RouterCallerState.sandbox_backend`` so the
        exec category D14 visibility gate in
        ``universal_catalog._enumerate_category`` can decide whether to
        expose ``exec__sandboxed_exec``.  ``None`` and ``"noop"`` both
        hide the exec category; any other value (``"seatbelt"`` /
        ``"landlock"`` / ``"auto"``) makes it visible.
        """
        return self._sandbox_backend

    def get_action_usage_tracker(self) -> Any:
        """Return the ActionUsageTracker for hot list freq+recency, or None.

        FP-0034 Phase 2 step 5.  RouterLoop reads this to build
        hot_list_aliases for build_tools.  None when universal wrappers
        are off or hot_list_n == 0.
        """
        return self._action_usage_tracker

    def get_uncompacted_tool_call_records(self) -> list[tuple[str, float]]:
        """Return live ``(qualified_name, ts_epoch)`` records from the
        current uncompacted chat history.

        FP-0034 refactor companion to ``get_action_usage_tracker``.
        RouterLoop combines these with the tracker's compacted table to
        build the hot-list each turn. Empty list when no live records
        are available (= host did not inject the accessor, or extractor
        returned nothing).
        """
        if self._uncompacted_tool_call_records_fn is None:
            return []
        try:
            return list(self._uncompacted_tool_call_records_fn() or [])
        except Exception:
            return []

    def get_action_retrieval_config(self) -> Any:
        """Return the ActionRetrievalConfig for hot_list_n / hot_list_seed.

        FP-0034 Phase 2 step 5.  RouterLoop reads this to determine how many
        hot list aliases to generate and which seed to apply when freq history
        is sparse.  Returns None when not set; RouterLoop falls back to a
        default-constructed ActionRetrievalConfig.
        """
        return self._action_retrieval_config

    # --- Web ops ---

    async def web_search(self, *, query: str, max_results: int) -> dict:
        """Dispatch the OS-native web/search op (DuckDuckGo) from the router."""
        from reyn.core.op_runtime.web import handle_web_search
        from reyn.schemas.models import WebSearchIROp

        op = WebSearchIROp(
            kind="web_search",
            query=query,
            max_results=max_results,
            backend="duckduckgo",
        )
        ctx = self.make_router_op_context()
        return await handle_web_search(op, ctx, caller="control_ir")

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        """Dispatch the OS-native web/fetch op from the router.

        FP-0022: authorization is now enforced at the handler level via
        PermissionResolver.require_web_fetch() inside handle_web_fetch().
        """
        from reyn.core.op_runtime.web import handle_web_fetch
        from reyn.schemas.models import WebFetchIROp

        op = WebFetchIROp(
            kind="web_fetch",
            url=url,
            max_length=max_length,
            timeout=15.0,
        )
        ctx = self.make_router_op_context()
        return await handle_web_fetch(op, ctx, caller="control_ir")

    async def reyn_src_list(self, *, path: str) -> dict:
        """List entries under ``<reyn_root>/path``.

        See :func:`_resolve_reyn_root` for root resolution and
        :func:`_safe_resolve_inside` for path-traversal protection.
        Returns ``{path, entries: [{name, type}]}`` on success or
        ``{error}`` on failure.
        """
        from reyn.chat.reyn_src import (
            list_entries,
            resolve_reyn_root,
            safe_resolve_inside,
        )
        try:
            root = resolve_reyn_root()
        except RuntimeError as exc:
            return {"error": str(exc)}
        try:
            target = safe_resolve_inside(root, path)
        except ValueError as exc:
            return {"error": str(exc)}
        return list_entries(root, target, path)

    async def reyn_src_read(self, *, path: str) -> dict:
        """Read text at ``<reyn_root>/path``."""
        from reyn.chat.reyn_src import (
            read_text,
            resolve_reyn_root,
            safe_resolve_inside,
        )
        try:
            root = resolve_reyn_root()
        except RuntimeError as exc:
            return {"error": str(exc)}
        try:
            target = safe_resolve_inside(root, path)
        except ValueError as exc:
            return {"error": str(exc)}
        return read_text(target, path)

    # --- Memory file paths ---

    def memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer + slug to file path via MemoryService."""
        return self._memory.memory_path(layer, slug)

    def memory_dir(self, layer: str) -> str:
        """Directory for the layer's memory files via MemoryService."""
        return self._memory.memory_dir(layer)

    # --- Action callbacks ---

    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict:
        return await self._run_skill_awaitable_cb(
            {"skill": skill, "input": input}, chain_id=chain_id,
        )

    async def spawn_skill(self, *, skill: str, input: dict,
                          chain_id: str) -> dict:
        """FP-0012 non-blocking spawn — returns immediately with the
        ``{status: "spawned", run_id, chain_id, note}`` ack. The skill
        runs in the background; completion arrives via the
        ``skill_completed`` inbox kind. See ``Session.spawn_skill``
        for the underlying implementation.
        """
        return await self._spawn_skill_cb(
            {"skill": skill, "input": input}, chain_id=chain_id,
        )

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None:
        """Dispatch to peer and record delegation for pending-chain registration."""
        await self._send_to_agent_cb(
            to=to, request=request, depth=depth, chain_id=chain_id,
        )
        # Track delegations so callers can register _PendingChain after the loop.
        tracker = self._delegation_tracker()
        if tracker is not None:
            tracker.append({"to": to, "request": request})

    def append_history_entry(
        self,
        *,
        role: str,
        content: Any,
        meta: dict | None = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
    ) -> None:
        """E-full PR-E (issue #383): persist a single ChatMessage entry
        without an outbox side-effect.

        Used by ``RouterLoop.run()`` to record per-iteration assistant
        tool_call turns (= ``role="assistant"`` + ``tool_calls`` field)
        and tool response turns (= ``role="tool"`` + ``tool_call_id`` +
        ``name``). The pre-PR-E producer only persisted the LLM's final
        text reply via ``put_outbox(kind="agent")``; this method closes
        the gap so the next ``_build_history_for_router`` rebuild
        replays the full LLM message sequence.
        """
        from reyn.chat.session import ChatMessage, _now_iso
        self._append_history_cb(ChatMessage(
            role=role,
            content=content,
            ts=_now_iso(),
            meta=meta if meta is not None else {},
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
        ))

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        from reyn.chat.outbox import OutboxMessage
        from reyn.chat.session import ChatMessage, _now_iso
        # #1652: centralised reasoning handling for agent replies. The router
        # passes the turn's reasoning as meta["reasoning"]; this single chokepoint
        # applies the two independent gates so every agent-reply site is covered
        # by-construction:
        #   - DISPLAY (toggle2): emit a discrete kind="reasoning" OutboxMessage
        #     BEFORE the reply (the channels render reasoning ONLY from this
        #     signal — never from agent meta — so display-off = no render).
        #   - then strip reasoning from the agent OutboxMessage's meta.
        #   - PERSIST (toggle1): keep reasoning on the persisted history
        #     ChatMessage only when continuity is on (so replay can read it);
        #     the wire-shape (content+tool_calls) never carries it → no
        #     native double-inject on gemini.
        _reasoning = meta.get("reasoning") if kind == "agent" else None
        if _reasoning and self.reasoning_display_enabled():
            await self._put_outbox_cb(OutboxMessage(
                kind="reasoning",
                text=_reasoning,
                meta={"chain_id": meta.get("chain_id"), "reasoning": _reasoning},
            ))
        _outbox_meta = (
            {k: v for k, v in meta.items() if k != "reasoning"}
            if "reasoning" in meta else meta
        )
        await self._put_outbox_cb(OutboxMessage(kind=kind, text=text, meta=_outbox_meta))
        # Persist agent (conversational) replies to history so the context
        # window stays coherent across turns.
        #
        # Note on empty-stop canned text: dogfood trace v6 showed that
        # filtering router-empty-response text out of history (= the naive
        # "don't pollute LLM context with failure notices" patch) creates
        # a worse downstream pattern — the next turn's LLM sees two
        # consecutive ``role="user"`` messages with no assistant between
        # them, which is itself an attractor (= same shape as the
        # commit 3732275 duplicate-user bug we fixed earlier). Keeping
        # the canned text in history maintains alternation; the
        # cascading-attractor mitigation needs to live elsewhere
        # (= context build / classifier-side, tracked as follow-up).
        if kind == "agent" and text:
            # Issue #383: chat history now uses ``role="assistant"`` +
            # ``content=`` (= wire shape mirror); the OutboxMessage above
            # keeps ``kind="agent"`` since that's the TUI-facing
            # OutboxMessage taxonomy, independent of the LLM-side role.
            # #1652: persist reasoning on the history ChatMessage ONLY when
            # continuity is on (so _reasoning_continuity_section can replay it);
            # otherwise persist the stripped meta. Either way the wire-shape
            # builder never emits meta to the LLM (no native double-inject).
            _persist_meta = (
                meta if (_reasoning and self.reasoning_continuity_enabled())
                else _outbox_meta
            )
            self._append_history_cb(ChatMessage(
                role="assistant", content=text, ts=_now_iso(), meta=_persist_meta,
            ))
            # Capture for agent-to-agent paths that need to forward the
            # reply upstream via _send_agent_response.
            replies = self._agent_replies_tracker()
            if replies is not None:
                replies.append(text)

    # --- #1652 reasoning capture/continuity/display ---

    def reasoning_display_enabled(self) -> bool:
        """Whether the model's reasoning text should be surfaced to the UI
        (config ``chat.reasoning.display``; default False when unconfigured)."""
        return bool(getattr(self._reasoning_config, "display", False))

    def reasoning_continuity_enabled(self) -> bool:
        """Whether reasoning is persisted to history + replayed into the next
        turn (config ``chat.reasoning.continuity``; default False unconfigured)."""
        return bool(getattr(self._reasoning_config, "continuity", False))

    def reasoning_continuity_section(self) -> str:
        """Pre-rendered prior-reasoning text section for the next system prompt,
        or ``""`` when continuity is off / no prior reasoning. The session
        callback reads recent history + applies the bound + continuity gate."""
        if self._reasoning_continuity_section_fn is None:
            return ""
        return self._reasoning_continuity_section_fn() or ""

    # --- File ops ---

    async def file_read(self, path: str) -> str:
        """Returns content string or JSON error."""
        import json
        res = await self._file_read_cb(path)
        if "content" in res:
            return res["content"]
        return json.dumps(res)

    async def file_write(self, path: str, content: str) -> dict:
        return await self._file_write_cb(path, content)

    async def file_delete(self, path: str) -> dict:
        return await self._file_delete_cb(path)

    async def file_list_directory(self, path: str) -> list[dict]:
        result = await self._file_list_directory_cb(path)
        if isinstance(result, dict):
            return result.get("entries", [result])
        return result

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict:
        return await self._file_regenerate_index_cb(
            path=path,
            output_path=output_path,
            entry_template=entry_template,
            header=header,
        )

    # --- MCP ops ---

    async def mcp_list_servers(self) -> list[dict]:
        return await self._mcp_list_servers_cb()

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return await self._mcp_list_tools_cb(server)

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return await self._mcp_call_tool_cb(server, tool, args)

    # --- Model resolution ---

    @property
    def resolver(self) -> Any:
        """The bound ``ModelResolver``.

        Exposed (#1172) so components that construct their own LLM callers —
        e.g. the planner's lazy ``CompactionEngine`` — can resolve model
        classes through the same chain as the router. ``resolve_model`` is the
        scalar convenience wrapper; this is the full resolver object.
        """
        return self._resolver

    def resolve_model(self, name: str) -> str:
        """Resolve config model name (e.g. 'router') to actual model id."""
        return self._resolver.resolve(name).model

    def resolve_model_spec(self, name: str) -> "Any":
        """#1654: resolve a config model name to the FULL ModelSpec (model +
        operator kwargs). The chat router must pass this to call_llm_tools so
        per-model kwargs (reasoning_effort, temperature, extra_body, …) reach
        litellm. ``resolve_model`` returns the bare ``.model`` string, DROPPING
        those kwargs — which left reasoning_effort (#1650/#1652) and every model
        kwarg inert on the chat-router path."""
        return self._resolver.resolve(name)

    def context_window_status(self) -> "dict | None":
        """#272/#1128: live exact-token context budget for the SP context-size
        signal, or None when no provider is wired (= signal omitted)."""
        if self._context_window_status is None:
            return None
        try:
            return self._context_window_status()
        except Exception:  # noqa: BLE001 — signal is best-effort, never break a turn
            return None

    # --- Plan-mode lifecycle persistence (ADR-0022 Phase 1) ---
    #
    # RouterLoopHost methods that wire through to SnapshotJournal so plan-
    # mode executions become crash-discoverable.

    async def record_plan_started(
        self, *, plan_id: str, goal: str, n_steps: int,
    ) -> None:
        await self._journal.record_plan_started(
            plan_id=plan_id, goal=goal, n_steps=n_steps,
        )
        # ADR-0023 Phase 2 + ADR-0025 wiring: per-plan snapshot is
        # created here so on-disk state mirrors AgentSnapshot's
        # active_plan_ids.
        plan_reg = self._plan_registry_getter()
        if plan_reg is not None:
            try:
                applied = self._journal.snapshot.applied_seq
                agent_state_dir = (
                    Path(".reyn") / "agents" / self._agent_name / "state"
                )
                from reyn.core.plan import decomposition_path
                artifact = decomposition_path(agent_state_dir, plan_id)
                plan_reg.start(
                    plan_id=plan_id,
                    chain_id=f"plan_{plan_id}",
                    goal=goal,
                    applied_seq=applied,
                    decomposition_artifact_path=str(artifact)
                    if artifact.exists()
                    else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PlanRegistry.start failed for %s: %r", plan_id, exc,
                )

    async def record_plan_completed(self, *, plan_id: str) -> None:
        await self._journal.record_plan_completed(plan_id=plan_id)
        plan_reg = self._plan_registry_getter()
        if plan_reg is not None:
            try:
                await plan_reg.complete(plan_id=plan_id, status="completed")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PlanRegistry.complete failed for %s: %r", plan_id, exc,
                )

    async def record_plan_aborted(
        self, *, plan_id: str, reason: str = "",
    ) -> None:
        await self._journal.record_plan_aborted(plan_id=plan_id, reason=reason)
        plan_reg = self._plan_registry_getter()
        if plan_reg is not None:
            try:
                await plan_reg.complete(plan_id=plan_id, status="aborted")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PlanRegistry.complete (abort) failed for %s: %r",
                    plan_id, exc,
                )

    # --- Plan-mode per-step WAL persistence (ADR-0023 Phase 2 step 6) ---

    async def record_plan_step_started(
        self, *, plan_id: str, step_id: str, depends_on: list[str],
        n_tools: int,
    ) -> None:
        seq = await self._journal.record_plan_step_started(
            plan_id=plan_id, step_id=step_id,
            depends_on=depends_on, n_tools=n_tools,
        )
        plan_reg = self._plan_registry_getter()
        if plan_reg is not None and seq is not None:
            plan_reg.record_step_started(
                plan_id=plan_id, step_id=step_id, applied_seq=seq,
            )

    async def record_plan_step_completed(
        self, *, plan_id: str, step_id: str, content_len: int,
        result_text: str | None = None,
    ) -> None:
        """Record durable step completion.

        ADR-0023 Phase 2 + ADR-0024: ``result_text`` is the optional
        full text — passed through to PlanRegistry which inlines or
        spills based on size.
        """
        seq = await self._journal.record_plan_step_completed(
            plan_id=plan_id, step_id=step_id, content_len=content_len,
        )
        plan_reg = self._plan_registry_getter()
        if plan_reg is not None and seq is not None:
            await plan_reg.record_step_completed(
                plan_id=plan_id, step_id=step_id, applied_seq=seq,
                result_text=result_text or "",
            )

    async def record_plan_step_failed(
        self, *, plan_id: str, step_id: str, error: str,
    ) -> None:
        seq = await self._journal.record_plan_step_failed(
            plan_id=plan_id, step_id=step_id, error=error,
        )
        plan_reg = self._plan_registry_getter()
        if plan_reg is not None and seq is not None:
            await plan_reg.record_step_failed(
                plan_id=plan_id, step_id=step_id, applied_seq=seq,
                error_repr=error,
            )

    # --- Decomposition artifact persistence (ADR-0023 §3.5) ---

    async def write_plan_decomposition(
        self, *, plan_id: str, plan: Any,
    ) -> str | None:
        """Persist the plan decomposition. Returns the artifact path or None."""
        from reyn.core.plan import write_decomposition
        agent_state_dir = (
            Path(".reyn") / "agents" / self._agent_name / "state"
        )
        try:
            return str(write_decomposition(agent_state_dir, plan_id, plan))
        except OSError as exc:
            logger.warning(
                "write_plan_decomposition failed for %s: %r", plan_id, exc,
            )
            return None

    async def delete_plan_decomposition(self, *, plan_id: str) -> None:
        """Remove the plan decomposition artifact (P5 cleanup on success)."""
        from reyn.core.plan import delete_decomposition
        agent_state_dir = (
            Path(".reyn") / "agents" / self._agent_name / "state"
        )
        try:
            delete_decomposition(agent_state_dir, plan_id)
        except OSError as exc:
            logger.warning(
                "delete_plan_decomposition failed for %s: %r", plan_id, exc,
            )

    async def spawn_plan_task(
        self, *, plan_id: str, runtime: Any, chain_id: str,
        parent_chain_id: str | None = None,
    ) -> None:
        """Delegate to the session-owned spawn_plan_task callback.

        Task lifecycle (running_plans dict) stays with Session.
        """
        await self._spawn_plan_task_cb(
            plan_id=plan_id,
            runtime=runtime,
            chain_id=chain_id,
            parent_chain_id=parent_chain_id,
        )

    # --- Private helpers ---

    def _get_file_permissions_for_router(self) -> dict | None:
        """Return {read: [paths], write: [paths]} or None if not configured."""
        if self._perm is None:
            return None
        config = self._perm._config or {}
        read_val = config.get("file.read") or (config.get("file") or {}).get("read")
        write_val = config.get("file.write") or (config.get("file") or {}).get("write")

        read_paths: list[str] = []
        write_paths: list[str] = []

        if read_val == "allow":
            read_paths = ["*"]
        elif isinstance(read_val, list):
            for entry in read_val:
                if isinstance(entry, str):
                    read_paths.append(entry)
                elif isinstance(entry, dict) and entry.get("path"):
                    read_paths.append(str(entry["path"]))

        if write_val == "allow":
            write_paths = ["*"]
        elif isinstance(write_val, list):
            for entry in write_val:
                if isinstance(entry, str):
                    write_paths.append(entry)
                elif isinstance(entry, dict) and entry.get("path"):
                    write_paths.append(str(entry["path"]))

        if not read_paths and not write_paths:
            return None
        return {"read": read_paths, "write": write_paths}

    def _mcp_servers_flat(self) -> dict:
        """Unwrap config.mcp's ``{servers: {...}}`` shape to flat ``{name: cfg}``."""
        raw = self._mcp_servers or {}
        if isinstance(raw, dict) and "servers" in raw:
            inner = raw.get("servers") or {}
            return inner if isinstance(inner, dict) else {}
        return raw if isinstance(raw, dict) else {}

    def _get_mcp_servers_for_router(self) -> list[dict]:
        """Return [{name, description, tools?}, ...] for configured MCP servers.

        ``tools`` is included when `ensure_mcp_tools_cached()` has populated
        the per-session tools cache; absent otherwise. Callers downstream
        (= `_enumerate_category("mcp.tool")` in `universal_catalog.py` and
        `router_loop.py`'s `mcp.tool__*` alias builder) iterate `tools`
        defensively so the missing-tools case is graceful.

        Issue #160 / FP-0037 context: chat startup intentionally does NOT
        probe MCP servers (= zero-startup-latency goal). The first user
        turn calls `ensure_mcp_tools_cached()` to fill the cache once per
        session; subsequent turns read it without additional probes.
        """
        servers = self._mcp_servers_flat()
        if not servers:
            return []
        tools_cache = self._mcp_tools_cache or {}
        result: list[dict] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            entry: dict = {
                "name": name,
                "description": cfg.get("description", ""),
            }
            cached_tools = tools_cache.get(name)
            if cached_tools is not None:
                entry["tools"] = cached_tools
            result.append(entry)
        return result

    async def ensure_mcp_tools_cached(
        self, *, per_server_timeout: float = 5.0,
    ) -> None:
        """Probe every configured MCP server's tool list and cache the
        results for the session lifetime.

        Called by `Session._handle_user_message` at the start of each
        user turn. The first call populates the cache (= lazy, post-startup,
        per FP-0037 issue #160). Subsequent calls are no-ops.

        FP-0037 S1: before probing, checks for a pre-written cache file at
        ``<state_dir>/mcp_tools_cache.json``. If present and parseable, the
        in-memory cache is warm-started from disk (= zero probe latency on
        sessions after the operator ran ``reyn mcp refresh``). On cache-miss
        (file absent / corrupt) the existing live-probe path runs unchanged,
        and the result is written back to disk for future warm-starts.

        Probes run in parallel via `asyncio.gather` with `return_exceptions=True`
        so a single slow / unreachable server does not block the others.
        Per-server timeout caps each probe; on timeout or exception the
        server is cached as an empty list (= still cached, so we don't
        re-probe a known-broken server every turn).

        The result feeds `_get_mcp_servers_for_router` which is consumed
        by `_enumerate_category("mcp.tool")` (= list_actions visibility)
        and the `mcp.tool__*` direct-alias builder in `router_loop.py`.
        """
        import asyncio

        from reyn.chat.services.mcp_cache_file import (
            cache_file_path,
            file_mtime,
            read_cache,
            write_cache,
        )

        if self._mcp_tools_cache is not None:
            return

        # FP-0037 S1: warm-start from persistent cache file when available.
        cache_path = cache_file_path(self._state_dir)
        disk_cache = read_cache(cache_path)
        if disk_cache is not None:
            self._mcp_tools_cache = disk_cache
            self._mcp_tools_cache_mtime = file_mtime(cache_path)
            return

        servers = self._mcp_servers_flat()
        if not servers:
            self._mcp_tools_cache = {}
            return

        async def _probe_one(server_name: str) -> tuple[str, list[dict]]:
            # ``asyncio.timeout()`` (Python 3.11+) instead of
            # ``asyncio.wait_for`` because the latter wraps the awaited
            # coroutine in a new asyncio.Task in some scenarios. When that
            # inner task is cancelled mid-``MCPClient.initialize`` (= the
            # underlying mcp SDK opens anyio cancel scopes inside an
            # AsyncExitStack), the cleanup ends up running in a different
            # task than the one that entered the scope, producing
            # ``RuntimeError: Attempted to exit cancel scope in a different
            # task than it was entered in``. ``asyncio.timeout()`` is a
            # task-local deadline (= no task wrap) and cancellation is
            # raised at the awaiter in the SAME task, so the AsyncExitStack
            # unwinds correctly.
            try:
                async with asyncio.timeout(per_server_timeout):
                    tools = await self._mcp_list_tools_cb(server_name)
            except (TimeoutError, asyncio.TimeoutError):
                return server_name, []
            except Exception:  # noqa: BLE001 — adapter must never raise
                return server_name, []
            # _mcp_list_tools may return [{"error": "..."}] on failure;
            # treat as empty so the cache shape stays uniform.
            cleaned = [
                t for t in (tools or [])
                if isinstance(t, dict) and "error" not in t and t.get("name")
            ]
            return server_name, cleaned

        results = await asyncio.gather(
            *(_probe_one(name) for name in servers),
            return_exceptions=False,  # _probe_one handles its own errors
        )
        self._mcp_tools_cache = dict(results)

        # FP-0037 S1: persist the live-probe result so subsequent sessions
        # and turns can warm-start from disk. Failures are opportunistic
        # and must NOT abort the session.
        try:
            write_cache(cache_path, self._mcp_tools_cache)
            self._mcp_tools_cache_mtime = file_mtime(cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ensure_mcp_tools_cached: could not write cache to %s: %r",
                cache_path, exc,
            )

    def maybe_reload_mcp_tools_cache_from_disk(self) -> None:
        """Reload the in-memory MCP tools cache if the on-disk file is newer.

        FP-0037 S1: called at each turn boundary (in Session before
        `ensure_mcp_tools_cached`). When the operator runs ``reyn mcp refresh``
        while a session is active, the cache file's mtime advances. This
        method detects that and hot-swaps the in-memory cache so the very next
        turn sees the refreshed tool list — no session restart required.

        Behaviour:
        - File absent or unreadable → no-op (silent).
        - File mtime unchanged since last load → no-op.
        - File mtime advanced → replace in-memory cache + update mtime record.
        Never raises.
        """
        from reyn.chat.services.mcp_cache_file import (
            cache_file_path,
            file_mtime,
            read_cache,
        )

        cache_path = cache_file_path(self._state_dir)
        current_mtime = file_mtime(cache_path)
        if current_mtime is None:
            return
        if (
            self._mcp_tools_cache_mtime is not None
            and current_mtime <= self._mcp_tools_cache_mtime
        ):
            return
        fresh = read_cache(cache_path)
        if fresh is None:
            return
        self._mcp_tools_cache = fresh
        self._mcp_tools_cache_mtime = current_mtime

    @property
    def mcp_tools_cache_snapshot(self) -> dict[str, list[dict]] | None:
        """Read-only snapshot of the current in-memory MCP tools cache.

        FP-0037 S1: test-supporting public surface (per Tier policy
        [[feedback_tier_policy_strict_compliance]]). Returns a shallow copy
        so callers cannot mutate adapter internals through the returned dict.
        Returns None when the cache has not yet been populated.
        """
        if self._mcp_tools_cache is None:
            return None
        return dict(self._mcp_tools_cache)

    @property
    def yaml_mtimes_snapshot(self) -> dict[Path, float]:
        """Read-only snapshot of the last-seen yaml mtime table.

        FP-0037 S2: test-supporting public surface. Returns a shallow copy
        keyed by Path so callers can inspect which yaml files have been
        observed without touching adapter internals. Empty dict until the
        first call to maybe_refresh_mcp_tools_from_yaml.
        """
        return dict(self._yaml_mtimes_seen)

    async def maybe_refresh_mcp_tools_from_yaml(self) -> None:
        """Re-probe MCP servers and update the cache if any yaml config has changed.

        FP-0037 S2: called at each turn boundary BEFORE
        ``maybe_reload_mcp_tools_cache_from_disk`` so that yaml edits are
        caught, probed, and written to disk before the disk-reload step picks
        them up.

        Algorithm:
        1. Resolve the 3 yaml scope tier paths via ``yaml_scope_paths``.
        2. Stat each existing path and compare against ``_yaml_mtimes_seen``.
        3. If any mtime advanced (or a new yaml appeared): re-read MCP config
           from the yaml files, re-probe each server, write the cache file,
           and update ``_yaml_mtimes_seen``.
        4. On first call (``_yaml_mtimes_seen`` is empty): seed the mtime table
           without triggering a probe (= first-call "no diff" semantics).

        All failures (stat error, yaml parse error, probe error, cache write
        error) degrade silently — a warning is logged but the method never
        raises so the user-message hot path is not broken.
        """
        import asyncio

        from reyn.chat.services.mcp_cache_file import (
            cache_file_path,
            write_cache,
            yaml_scope_paths,
        )

        try:
            yaml_paths = yaml_scope_paths(self._project_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("maybe_refresh_mcp_tools_from_yaml: yaml_scope_paths failed: %r", exc)
            return

        # --- Stat current mtimes (best-effort; missing files are silently skipped) ---
        current_mtimes: dict[Path, float] = {}
        for p in yaml_paths:
            try:
                mtime = p.stat().st_mtime
                current_mtimes[p] = mtime
            except OSError:
                # File does not exist or is unreadable — skip silently.
                pass

        # --- First call: seed the mtime table, no probe ---
        if not self._yaml_mtimes_seen:
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        # --- Detect changes: new file or advanced mtime ---
        changed = False
        for p, mtime in current_mtimes.items():
            prev = self._yaml_mtimes_seen.get(p)
            if prev is None or mtime > prev:
                changed = True
                break
        # Also detect files that appeared (= in current but not in seen)
        if not changed:
            new_paths = set(current_mtimes) - set(self._yaml_mtimes_seen)
            if new_paths:
                changed = True

        if not changed:
            return

        # --- Changed: re-read MCP server config from yaml files ---
        servers_flat: dict[str, dict] = {}
        try:
            servers_flat = self._read_mcp_servers_from_yaml(yaml_paths)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_refresh_mcp_tools_from_yaml: could not read yaml config: %r", exc,
            )
            # Still update mtime table so we don't hammer on every turn.
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        if not servers_flat:
            # No MCP servers in any yaml — write empty cache to advance mtime.
            try:
                cache_path = cache_file_path(self._state_dir)
                write_cache(cache_path, {})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "maybe_refresh_mcp_tools_from_yaml: cache write failed: %r", exc,
                )
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        # --- Re-probe servers in parallel (shared helper from CLI) ---
        from reyn.interfaces.cli.commands.mcp import _probe_server_tools

        async def _probe_all() -> dict[str, list[dict]]:
            tasks = [
                _probe_server_tools(name, cfg)
                for name, cfg in servers_flat.items()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=False)
            return dict(results)

        try:
            probe_results = await _probe_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_refresh_mcp_tools_from_yaml: probe failed: %r", exc,
            )
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        # --- Write updated cache to disk (= S1's disk-reload picks it up) ---
        try:
            cache_path = cache_file_path(self._state_dir)
            write_cache(cache_path, probe_results)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_refresh_mcp_tools_from_yaml: cache write failed: %r", exc,
            )

        # --- Update mtime table regardless of cache-write success ---
        self._yaml_mtimes_seen = dict(current_mtimes)

    @staticmethod
    def _read_mcp_servers_from_yaml(yaml_paths: "list[Path]") -> dict[str, dict]:
        """Read and merge MCP server configs from the given ordered yaml paths.

        Priority: later paths override earlier ones for the same server name
        (= local > project > user, following ``_all_servers_with_scope`` order).

        Returns a flat ``{server_name: cfg_dict}`` mapping.
        Never raises — yaml parse failures are logged and skipped.
        """
        merged: dict[str, dict] = {}
        for p in yaml_paths:
            if not p.exists():
                continue
            try:
                import yaml  # lazy import to avoid yaml dep at import time
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    continue
                servers = (raw.get("mcp") or {}).get("servers") or {}
                if not isinstance(servers, dict):
                    continue
                for name, cfg in servers.items():
                    merged[name] = cfg if isinstance(cfg, dict) else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_read_mcp_servers_from_yaml: could not parse %s: %r", p, exc,
                )
        return merged

    def make_router_op_context(self) -> Any:
        """Build an OpContext for router-initiated file / MCP / web ops.

        Public method (ADR-0026 Phase 3.5): the unified registry handlers
        in ``src/reyn/tools/`` delegate to op_runtime via this factory so
        the OpContext carries the operator-declared PermissionDecl and the
        Workspace with ``skill_name="chat_router"``. Without this, handlers
        would synthesize a ``PermissionDecl()`` empty default and op_runtime
        permission gates would deny operations.

        Uses the injected events log and permission resolver. The skill_name
        ``"chat_router"`` is used for permission key lookups. PermissionDecl
        is populated from the agent's effective permissions so that op_runtime
        layer permission checks actually gate access.
        """
        from reyn.chat.router_op_context import build_router_op_context

        # #1412: single-sourced via build_router_op_context (shared with
        # Session). RouterHostAdapter wires intervention_bus inline (via the
        # factory) + media/multimodal/compact (the registry-dispatch path serves
        # web/media ops). agent_id is unset here (registry-dispatch lacks one) —
        # a #1412 follow-up gap candidate, preserved behaviorally.
        bus = (
            self._intervention_bus_factory()
            if self._intervention_bus_factory is not None
            else None
        )
        return build_router_op_context(
            events=self._events,
            permission_resolver=self._perm,
            file_permissions=self._get_file_permissions_for_router(),
            mcp_servers=self._get_mcp_servers_for_router(),
            mcp_servers_flat=self._mcp_servers_flat(),
            allowed_mcp=self._allowed_mcp,
            workspace_base_dir=self._workspace_base_dir,
            workspace_state_dir=self._workspace_state_dir,
            environment_backend=self._environment_backend,
            sandbox_backend=self._sandbox_backend_instance,
            sandbox_policy=self._sandbox_policy,
            agent_id=None,
            intervention_bus=bus,
            multimodal_config=self._multimodal_config,
            media_store=self._media_store,
            compact_now=self._compact_now,
            cancel_event=self._cancel_event,
        )

    def _set_cancel_event(self, event: asyncio.Event) -> None:
        """#1470: called by RouterLoopDriver at construction to register the
        per-turn cancel event. make_router_op_context threads it into OpContext
        so sandboxed_exec backends can observe cancel_inflight() mid-subprocess.
        """
        self._cancel_event = event

    def make_intervention_bus(self) -> "Any | None":
        """Return the current intervention bus for safety-limit checkpoints.

        Called by RouterLoop when max_iterations is reached and
        safety.on_limit.mode=interactive. Returns None when no bus is
        wired (headless / test stubs) → limit degrades to unattended.
        """
        if self._intervention_bus_factory is None:
            return None
        return self._intervention_bus_factory()
