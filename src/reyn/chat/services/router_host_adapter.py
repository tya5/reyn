"""RouterHostAdapter — concrete RouterLoopHost implementation.

Extracted from ChatSession wave 3 PR3. Composes ChatSession's collaborators
(MemoryService, SnapshotJournal, op-runtime callbacks) so RouterLoop has no
direct dependency on ChatSession internals. The adapter satisfies the
RouterLoopHost Protocol structurally; ChatSession constructs one and exposes
it via `self._router_host`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from reyn.events.events import EventLog

logger = logging.getLogger(__name__)

_DEFAULT_STATE_DIR = Path(".reyn") / "state"


class RouterHostAdapter:
    """Concrete RouterLoopHost implementation extracted from ChatSession.

    Holds injected identity attrs, catalogue deps, and async callbacks so
    RouterLoop can call host methods without importing or referencing
    ChatSession directly.

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
        # ChatSession passes True by default since PR-3b-iv flipped the
        # ActionRetrievalConfig default; this constructor parameter
        # still defaults to False so direct callers (= tests that build
        # adapters by hand) preserve the prior tools= shape and don't
        # accidentally activate wrappers without intent.
        universal_wrappers_enabled: bool = False,
        # FP-0034 Phase 2 step 1: ActionEmbeddingIndex + EmbeddingProvider
        # for search_actions.  When all three are set (= operator configured
        # ``action_retrieval.embedding_class`` AND ChatSession built a
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
        # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list freq+recency.
        # ChatSession passes the session-scoped tracker; None when wrappers are
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
        # hot_list_seed.  ChatSession passes its config; None → default.
        action_retrieval_config: Any = None,
        # B25-S5-1: when True, RouterLoop awaits the action embedding index
        # build synchronously on the first turn before computing the D14
        # search_actions visibility gate. Off by default (= lazy bg build).
        eager_embedding_build: bool = False,
        # FP-0022 fix (#53): callable that yields an InterventionBus for
        # router-initiated tools that need the 4-layer approval flow
        # (web_fetch interactive prompt, mcp install / drop ask gates).
        # ChatSession passes a factory that wraps ``ChatInterventionBus(
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
        # Issue #383 PR-C: media + tool-result file storage.
        media_store: Any = None,
        # FP-0037 S1: persistent MCP tools cache directory.
        # Default is Path(".reyn/state") which resolves relative to cwd
        # (= the project root in all production entry points). Tests pass
        # a tmp_path subdirectory to isolate writes.
        state_dir: Path | None = None,
        # FP-0037 S2: project root for yaml mtime watch (3-scope cascade).
        # When None, only the user-global ~/.reyn/config.yaml is watched.
        # ChatSession passes the project root so all 3 tiers are covered.
        project_root: Path | None = None,
    ) -> None:
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
        # Issue #383 PR-C: store the MediaStore for path-ref save/read.
        self._media_store = media_store

    # --- RouterLoopHost identity attributes ---

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
        """Current working directory the agent process is running from.

        Threaded into the router's system prompt so unqualified user
        references like "this repo" / "this code" / "the codebase" map
        to the project at this path. Without it the LLM falls back to
        its training prior ("please share the repository URL") even when
        the user is obviously inside a checked-out repo.
        """
        import os
        return os.getcwd()

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

        FP-0034 Phase 2 step 1.  Bound by ChatSession when the operator
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
        from reyn.op_runtime.web import handle_web_search
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
        from reyn.op_runtime.web import handle_web_fetch
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
        ``skill_completed`` inbox kind. See ``ChatSession.spawn_skill``
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
        await self._put_outbox_cb(OutboxMessage(kind=kind, text=text, meta=meta))
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
            self._append_history_cb(ChatMessage(
                role="assistant", content=text, ts=_now_iso(), meta=meta,
            ))
            # Capture for agent-to-agent paths that need to forward the
            # reply upstream via _send_agent_response.
            replies = self._agent_replies_tracker()
            if replies is not None:
                replies.append(text)

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

    def resolve_model(self, name: str) -> str:
        """Resolve config model name (e.g. 'router') to actual model id."""
        return self._resolver.resolve(name).model

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
                from reyn.plan import decomposition_path
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
        from reyn.plan import write_decomposition
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
        from reyn.plan import delete_decomposition
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

        Task lifecycle (running_plans dict) stays with ChatSession.
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

        Called by `ChatSession._handle_user_message` at the start of each
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

        FP-0037 S1: called at each turn boundary (in ChatSession before
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
        from reyn.cli.commands.mcp import _probe_server_tools

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
        from reyn.op_runtime.context import OpContext
        from reyn.permissions.permissions import PermissionDecl
        from reyn.workspace.workspace import Workspace

        file_perms = self._get_file_permissions_for_router() or {}
        mcp_servers = self._get_mcp_servers_for_router() or []

        file_read = [{"path": p, "scope": "recursive"} for p in file_perms.get("read", [])]
        file_write = [{"path": p, "scope": "recursive"} for p in file_perms.get("write", [])]
        mcp_names = [s["name"] for s in mcp_servers]

        # #571 collapse arc Phase 5: explicit list axes replace the
        # former mcp_install / index_drop bool axes. See ChatSession's
        # _make_router_op_context for the matching pattern.
        file_write = list(file_write) + [
            {"path": ".reyn/mcp.yaml", "scope": "just_path"},
            {"path": ".reyn/cron.yaml", "scope": "just_path"},
            {"path": ".reyn/index/sources.yaml", "scope": "just_path"},
        ]
        decl = PermissionDecl(
            file_read=file_read,
            file_write=file_write,
            mcp=mcp_names,
            allowed_mcp=self._allowed_mcp,
            # #571 Phase 7: wildcard http.get + specific MCP registry —
            # see ChatSession's matching pattern.
            http_get=[
                {"host": "registry.modelcontextprotocol.io"},
                {"host": "*"},
            ],
            # #571 Phase 6: wildcard secret.write — see ChatSession.
            secret_write=["*"],
        )
        if self._perm is not None:
            for canonical in (".reyn/mcp.yaml", ".reyn/cron.yaml", ".reyn/index/sources.yaml"):
                self._perm.session_approve_path(canonical, "chat_router", "file.write")

        workspace = Workspace(
            events=self._events,
            permission_resolver=self._perm,
            skill_name="chat_router",
        )
        # FP-0022 fix (#53): wire intervention_bus so handlers that need
        # the 4-layer approval flow (web_fetch, mcp install / drop) can
        # progress past their ``intervention_bus is None`` defensive
        # guard. The config-deny check inside require_web_fetch fires
        # before the bus is touched, so leaving the bus None is still
        # safe for the deny path — but we wire a real bus when one is
        # available so the interactive (Layer 4) path also works.
        bus = (
            self._intervention_bus_factory()
            if self._intervention_bus_factory is not None
            else None
        )
        return OpContext(
            workspace=workspace,
            events=self._events,
            permission_decl=decl,
            permission_resolver=self._perm,
            skill_name="chat_router",
            mcp_servers=self._mcp_servers_flat(),
            intervention_bus=bus,
            # Issue #364: gate config for router-initiated binary ops.
            multimodal_config=self._multimodal_config,
            # Issue #383 PR-C: shared MediaStore for path-ref save/read.
            media_store=self._media_store,
        )
