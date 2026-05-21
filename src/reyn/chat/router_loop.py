"""RouterLoop — drives the chat router via native LLM tool_use (PR35).

Loop: build tools + prompt → call_llm_tools → if tool_calls, execute in
parallel, append results to messages, repeat → if text reply, emit to host
outbox and stop. Bounded by max_iterations.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import (
    _DESCRIBE_SKILL_STRIP_FIELDS,
    MAX_DESC_LEN_FOR_LISTING,
    build_tools,
    get_dispatch_kind,
)
from reyn.chat.services.skill_search import BM25Backend
from reyn.chat.session import _TOOL_FAILED_FALLBACK_MSG
from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.index.source_manifest import get_source_manifest
from reyn.llm.llm import call_llm_tools
from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    from reyn.config import SkillSearchConfig


# ---------------------------------------------------------------------------
# Empty-response detection (Option F — ADR-0021)
# ---------------------------------------------------------------------------

# Localized user-facing message when the model returns an empty response
# (finish_reason=stop, no content, no tool calls). Deterministic i18n so
# output_language is always honoured.  P7-clean: no skill / tool names.
# "en" is the global-safe default.
_EMPTY_RESPONSE_MSG: dict[str, str] = {
    "ja": (
        "モデルが空の応答を返しました。"
        " 別の表現で再入力するか、設定を確認してください。"
    ),
    "en": (
        "The model returned an empty response."
        " Please try rephrasing your request or check your configuration."
    ),
}


# Localized OS-level acknowledgment emitted when a skill spawns asynchronously
# (= invoke_skill / invoke_action returns ``{status: "spawned", ...}``). The
# H3 ablation exits the router loop before any further LLM call, so without
# this OS-injected message the user sees silence between request and the
# eventual ``[task_completed]`` arrival. The previous (pre-H3) LLM-composed
# spawn-ack was hallucinating skill output that hadn't happened yet (B32
# W3 S1); this deterministic OS message carries the same UX guarantee
# (= "/tasks" hint) without LLM composition, so the race condition does
# not re-emerge.  P7-clean: no skill names, no qualified action names.
_SPAWN_ACK_MSG: dict[str, str] = {
    "ja": (
        "スキルをバックグラウンドで実行しています。"
        " `/tasks` で進行状況を確認できます。"
    ),
    "en": (
        "Skill is running in the background."
        " Use `/tasks` to monitor progress."
    ),
}


# NF-W7-B43-2: positive directive injected into the spawn-ack tool_result
# content. Replaces the previous OS-synthetic spawn-ack outbox push +
# early-exit pattern with the standard tool_call → tool_result → LLM
# continuation pattern (= aligned with Claude / GPT competitor designs).
#
# Why directive, not bare status:
#   - Trace-patch-replay N=10 (= 5 variant ablation, 2026-05-20):
#       Variant A (bare status, no directive):      1/10 ACK, 8/10 EMPTY, 0/10 HALLUCINATE
#       Variant B (negative directive "don't"):     0/10 ACK, 10/10 EMPTY
#       Variant D (positive "write short reply"):   7/10 ACK, 3/10 EMPTY, **0/10 HALLUCINATE**
#   - H3 hallucination defense (= B32 W3 S1 race) preserved across all
#     variants thanks to "do not include skill output" framing.
#   - Variant D (= "1-2 sentences confirm" voice) ACK 7/10 baseline;
#     remaining 3/10 EMPTY covered by PR #265 retry mechanism
#     (REYN_EMPTY_STOP_RETRY=1).
#
# The directive is appended to the tool_result body using the same
# ``\n\n---\n`` separator pattern as PR #221's ``_post_text`` (= LLM
# reads it as a textual instruction following the JSON status block).
_SPAWN_ACK_TOOL_DIRECTIVE = (
    "The skill has been spawned and is running in the background. "
    "Write a short reply to the user (1-2 sentences) confirming the "
    "skill has been started. The actual result will arrive in a "
    "subsequent turn as a [task_completed] message — do not include "
    "any skill output here, only the status confirmation."
)


def _strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block (``---\\n...\\n---\\n``) from
    a memory file's text and return the body alone.

    Used by :meth:`RouterLoop._read_memory_body` to give the LLM the
    actual remembered text instead of metadata fields it doesn't need
    (= ``name`` / ``description`` / ``type``). When the input doesn't
    start with a frontmatter delimiter the original text is returned
    unchanged — handles legacy memory files written before the frontmatter
    convention existed.
    """
    text = content or ""
    if not text.lstrip().startswith("---"):
        return text
    # Find first non-blank line; require it to be exactly "---".
    lines = text.split("\n")
    # Skip leading blanks.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return text
    # Find the closing "---" after the opening one.
    close = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close == -1:
        # No closing delimiter — leave content alone rather than truncating.
        return text
    body_lines = lines[close + 1:]
    # Trim a single leading blank line that conventionally follows the
    # closing delimiter; keep subsequent whitespace as authored.
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return "\n".join(body_lines).rstrip("\n") + ("\n" if body_lines else "")


def _build_media_followup_message(
    *,
    tool_name: str,
    media_blocks: list[dict],
    media_store: Any = None,
) -> dict | None:
    """Build a multimodal follow-up user message for MCP tool results that
    include image / non-text content blocks (issue #362 → #383 PR-C).

    Strategy (= Option A): after each tool result that carries non-text
    media, append a synthetic user message containing those media blocks
    in litellm-normalised shape. Provider-agnostic because user messages
    with content lists are universally supported (Anthropic, Gemini,
    OpenAI vision models).

    Two media block shapes are accepted (= dual mode during the #383 PR-C
    transition):
      1. **Path-ref** (post-PR-C, MediaStore-backed):
         ``{"type": "image", "path": "...", "mime_type": "...", "content_hash": "..."}``
         → read the file via ``media_store.read_image``, base64-encode,
         embed as data URL.
      2. **Inline** (pre-PR-C / no MediaStore):
         ``{"type": "image", "data": "<b64>", "mimeType": "..."}``
         → embed directly as data URL.

    Returns None when no image-typed block can be rendered.
    """
    parts: list[dict] = [
        {"type": "text", "text": f"Tool `{tool_name}` returned the following image(s):"},
    ]
    rendered = 0
    for block in media_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "image":
            continue
        mime = block.get("mime_type") or block.get("mimeType") or "image/png"
        # Path-ref shape (PR-C): resolve via MediaStore.
        path = block.get("path")
        if isinstance(path, str) and path:
            if media_store is None:
                # Path-ref present but no MediaStore available — skip the
                # block rather than crash. Pre-PR-C consumers shouldn't
                # see path-refs in the first place, so this is defensive
                # only.
                continue
            try:
                data_bytes, found = media_store.read_image(path)
            except PermissionError:
                continue
            if not found:
                continue
            import base64
            data_b64 = base64.b64encode(data_bytes).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data_b64}"},
            })
            rendered += 1
            continue
        # Inline shape (pre-PR-C): use the base64 directly.
        data = block.get("data")
        if not isinstance(data, str) or not data:
            continue
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}"},
        })
        rendered += 1
    if rendered == 0:
        return None
    return {"role": "user", "content": parts}


def _is_empty_router_response(response: Any) -> bool:
    """OS-side detection: model emitted no text and no tool calls.

    Provider-level glitch (observed with weak models such as
    gemini-2.5-flash-lite at ~50% rate — ADR-0021 / B7-G12).  This is
    NOT recovered by Reyn — surfaced to the user as an explicit failure
    for user-side handling (no retry, no context change, no model switch).

    Trigger: finish_reason=="stop", content empty, tool_calls empty.
    """
    if response is None:
        return True
    finish = getattr(response, "finish_reason", None)
    content = getattr(response, "content", None) or ""
    tool_calls = getattr(response, "tool_calls", None) or []
    return finish == "stop" and not content.strip() and not tool_calls


# ---------------------------------------------------------------------------
# Host protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RouterLoopHost(Protocol):
    """Abstract surface RouterLoop needs.

    Implemented by RouterHostAdapter in
    src/reyn/chat/services/router_host_adapter.py.
    """

    # Static catalogue access
    chat_id: str
    agent_name: str
    agent_role: str
    # BCP-47 code (e.g. "ja", "en") when the user explicitly configured
    # output_language; None when unset, in which case build_system_prompt
    # skips the language directive entirely so the LLM picks based on
    # the user's input language naturally.
    output_language: str | None

    @property
    def events(self) -> Any:
        """EventLog (has .emit(type: str, **data)) for tool dispatch events."""
        ...

    def list_available_skills(self) -> list[dict]:
        """Each entry: {name, description, routing?, category?}"""
        ...

    def list_available_agents(self) -> list[dict]:
        """Each entry: {name, role, cluster?}"""
        ...

    def get_memory_index(self) -> dict:
        """Returns {status: 'ok'|'not_found', content: str}"""
        ...

    def get_file_permissions(self) -> dict | None:
        """{read: [paths], write: [paths]} or None"""
        ...

    def get_mcp_servers(self) -> list[dict]:
        """[{name, description, ...}, ...]"""
        ...

    def get_web_fetch_allowed(self) -> bool:
        """True if `web.fetch: allow` is in the operator's permissions."""
        ...

    def get_project_context(self) -> str:
        """Project context text (= REYN.md / `project_context_path` content),
        or empty string when the operator has not configured one. Threaded
        into the router's system prompt so the chat reply path knows about
        the user's project — without this, only the skill execution path
        sees REYN.md and casual chat queries get answered without
        project-specific context."""
        ...

    def get_universal_wrappers_enabled(self) -> bool:
        """Return whether the FP-0034 universal catalog wrappers should
        appear in tools=. Mirrors ``action_retrieval.universal_wrappers_enabled``
        from reyn.yaml. Default False preserves the prior tools= shape."""
        ...

    def get_action_embedding_index(self) -> Any:
        """Return the session-scoped ActionEmbeddingIndex, or None.

        FP-0034 Phase 2 step 1.  Bound by ChatSession when the operator
        has configured ``action_retrieval.embedding_class``.
        """
        ...

    def get_embedding_provider(self) -> Any:
        """Return the session's EmbeddingProvider instance, or None.

        FP-0034 Phase 2 step 1.  Used together with the
        ActionEmbeddingIndex to power search_actions semantic search.
        """
        ...

    def get_embedding_model_class(self) -> str | None:
        """Return the configured embedding model class name, or None.

        FP-0034 Phase 2 step 1.  Mirror of
        ``action_retrieval.embedding_class`` from reyn.yaml.
        """
        ...

    def get_sandbox_backend(self) -> "str | None":
        """Return the configured sandbox backend name, or None.

        FP-0034 Phase 2.  Mirror of ``sandbox.backend`` from reyn.yaml
        (resolved from ``session._sandbox_config.backend``).  RouterLoop
        forwards this into ``RouterCallerState.sandbox_backend`` so the
        ``exec`` category D14 visibility gate in
        ``universal_catalog._enumerate_category`` can decide whether to
        expose ``exec__sandboxed_exec``.  ``None`` and ``"noop"`` both
        hide the category; any other value (``"seatbelt"`` /
        ``"landlock"`` / ``"auto"``) makes it visible.
        """
        ...

    async def web_search(self, *, query: str, max_results: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/search op (DuckDuckGo)."""
        ...

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/fetch op."""
        ...

    async def reyn_src_list(self, *, path: str) -> dict:
        """RouterLoopHost: list entries under ``<reyn_root>/path``.

        ``reyn_root`` resolves to the directory containing
        ``pyproject.toml`` for the running Reyn install (= dev install /
        source clone). For wheel installs without a discoverable
        repo root, returns an error result so the LLM can fall back."""
        ...

    async def reyn_src_read(self, *, path: str) -> dict:
        """RouterLoopHost: read the file at ``<reyn_root>/path`` as text."""
        ...

    # Memory file paths (for list_memory / read_memory_body)
    def memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer ('shared'|'agent') + slug to file path"""
        ...

    def memory_dir(self, layer: str) -> str:
        """Directory for the layer's memory files"""
        ...

    # Action callbacks (async)
    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict: ...

    # FP-0012: non-blocking skill dispatch. Chat-mode hosts return
    # ``{status: "spawned", run_id, chain_id, note}`` immediately and
    # deliver completion via the ``skill_completed`` inbox kind. Hosts
    # that don't support spawn semantics (e.g. plan-mode steps) leave
    # this method un-bound (= duck-typing / hasattr check) so the
    # invoke_skill handler falls back to ``run_skill_awaitable``.
    async def spawn_skill(self, *, skill: str, input: dict,
                          chain_id: str) -> dict: ...

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None: ...

    async def put_outbox(self, *, kind: str, text: str,
                         meta: dict) -> None: ...

    # E-full PR-E (issue #383): persist a single ChatMessage entry
    # without routing through the outbox (= no TUI display side-effect).
    # Used by ``run()`` to record per-iteration assistant tool_call
    # turns and tool response turns so the next ``_build_history_for_router``
    # rebuilds the LLM message list with full fidelity.
    #
    # The host implementation constructs ChatMessage and feeds it to
    # the session's ``_append_history``.  ``meta`` should include
    # ``chain_id`` so the entry can be traced; other meta keys are
    # opaque to the router.
    def append_history_entry(
        self,
        *,
        role: str,
        content: Any,
        meta: dict | None = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
    ) -> None: ...

    # File ops (via op_runtime/file under permission scope)
    async def file_read(self, path: str) -> str: ...

    async def file_write(self, path: str, content: str) -> dict: ...

    async def file_delete(self, path: str) -> dict: ...

    async def file_list_directory(self, path: str) -> list[dict]: ...

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict: ...

    # MCP ops
    async def mcp_list_servers(self) -> list[dict]: ...

    async def mcp_list_tools(self, server: str) -> list[dict]: ...

    async def mcp_call_tool(self, server: str, tool: str,
                             args: dict) -> dict: ...

    # OpContext factory for unified-registry handlers (ADR-0026 Phase 3.5).
    # Builds a permission-aware OpContext with the operator-declared
    # PermissionDecl + Workspace(skill_name="chat_router") + mcp_servers,
    # so handlers in src/reyn/tools/{file,mcp,web*}.py can delegate to
    # op_runtime with the same gating the legacy router branches had.
    def make_router_op_context(self) -> Any: ...

    # Resolve router model (config "router" → real model id)
    def resolve_model(self, name: str) -> str: ...

    # Plan-mode lifecycle persistence (ADR-0022 Phase 1).
    # Optional — implementations that don't support plan-mode crash
    # recovery (e.g. test stubs) leave these as no-ops. Real chat
    # session implementation (`session.py`) wires through to
    # SnapshotJournal.record_plan_*.
    async def record_plan_started(
        self, *, plan_id: str, goal: str, n_steps: int,
    ) -> None: ...

    async def record_plan_completed(self, *, plan_id: str) -> None: ...

    async def record_plan_aborted(
        self, *, plan_id: str, reason: str = "",
    ) -> None: ...

    # Plan-mode per-step WAL persistence (ADR-0023 Phase 2 step 6).
    # Optional — test stubs may omit. ChatSession routes these through
    # SnapshotJournal.record_plan_step_*.
    async def record_plan_step_started(
        self, *, plan_id: str, step_id: str, depends_on: list[str],
        n_tools: int,
    ) -> None: ...

    async def record_plan_step_completed(
        self, *, plan_id: str, step_id: str, content_len: int,
        result_text: str | None = None,
    ) -> None: ...

    async def record_plan_step_failed(
        self, *, plan_id: str, step_id: str, error: str,
    ) -> None: ...

    # Decomposition artifact persistence (ADR-0023 §3.5).
    # The artifact is the canonical SSoT for the plan shape on resume.
    # ChatSession threads this through reyn.plan.decomposition with the
    # agent-specific state directory; test stubs may no-op.
    async def write_plan_decomposition(
        self, *, plan_id: str, plan: "Any",
    ) -> str | None:
        """Persist the plan decomposition. Returns the artifact path or None."""
        ...

    async def delete_plan_decomposition(self, *, plan_id: str) -> None:
        """Remove the plan decomposition artifact (= P5 cleanup on success)."""
        ...

    async def spawn_plan_task(
        self, *, plan_id: str, runtime: "Any", chain_id: str,
    ) -> None:
        """Register a PlanRuntime as a background task (ADR-0023 Phase 2.1).

        ChatSession owns the task lifecycle (= ``running_plans`` dict)
        and the wrap-around finally that emits the terminal aggregator
        text via ``put_outbox(kind="agent")`` and cleans up the
        decomposition artifact on clean exit. dispatch_plan_tool hands
        the constructed runtime here and returns immediately.
        """
        ...


# ---------------------------------------------------------------------------
# FP-0034 Phase 2 step 5: hot list alias builder
# ---------------------------------------------------------------------------

# Universal wrapper tool names that are already added by section I of
# build_tools().  Filtering them here prevents duplicate function
# declarations when ActionUsageTracker.get_top_n() returns a wrapper name
# that was recorded as usage (B27-C1).
_UNIVERSAL_WRAPPER_NAMES: frozenset[str] = frozenset({
    "list_actions",
    "search_actions",
    "describe_action",
    "invoke_action",
})


def _filter_ghost_names_by_registry(
    names: "list[str]",
    skill_meta_map: "dict[str, dict] | None",
    mcp_tool_map: "dict[str, dict] | None",
    available_agents: "list[dict] | None",
    *,
    known_skill_names: "frozenset[str] | None" = None,
    known_memory_entries: "frozenset[str]",
    _warned: "set[str] | None" = None,
) -> "list[str]":
    """Filter hot-list names that pass structural check but don't exist in the registry.

    B38 W2 finding: ``_is_valid_qualified_name`` only validates shape
    (= category + separator + entry). A renamed skill like
    ``skill__create_skill`` passes structural check but is a ghost — the
    skill no longer exists under that name. This filter adds the
    existence check at hot-list materialization time, when session
    registry data is available.

    Categories and their existence signals:
    - ``skill__*`` → must be a key in ``skill_meta_map``
      (= resolved skill list from ``host.list_available_skills()``).
    - ``agent.peer__*`` → must match a name in ``available_agents``.
    - ``mcp.tool__*`` / ``mcp.server__*`` → must be a key in ``mcp_tool_map``
      (or any server name prefix for ``mcp.server__*``).
    - ``memory.entry__*`` → must be in ``known_memory_entries`` (=
      qualified names enumerated by ``_enumerate_shared_memory_entries``
      at hot-list build time). Required parameter — caller must supply
      the enumerated set (possibly empty when no entries exist).
    - Operation categories (``file__*``, ``web__*``, ``memory.operation__*``,
      ``reyn.source__*``, ``rag.operation__*``, ``mcp.operation__*``,
      ``exec__*``) → must be in ``KNOWN_STATIC_QUALIFIED_NAMES`` (static
      op registry).
    - ``rag.corpus__*`` is currently routed through the static check; it
      is also a dynamic category and the same fix shape as memory.entry
      applies if its caller starts seeding dynamic corpus aliases — see
      the PR for the memory.entry case for the pattern.

    Ghost names are logged once per unique name per session to stderr.
    ``_warned`` is an optional set for cross-call deduplication.

    P7-clean: check is data-driven from registry enumeration only —
    no hardcoded ghost names.
    """
    import sys

    from reyn.tools.universal_catalog import split_qualified_name
    from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

    if _warned is None:
        _warned = set()

    # Build existence sets from session state.
    # Skills: use the broader ``known_skill_names`` set when provided
    # (= covers empty-input-schema skills too); fall back to
    # ``skill_meta_map`` keys for backwards-compat.
    if known_skill_names is not None:
        known_skills: frozenset[str] = known_skill_names
    else:
        known_skills = frozenset(skill_meta_map or {})
    known_mcp_tools: frozenset[str] = frozenset(mcp_tool_map or {})
    # Extract MCP server names from mcp_tool_map keys (mcp.tool__<server>.<tool>)
    known_mcp_servers: set[str] = set()
    for qn in known_mcp_tools:
        try:
            _cat, entry = split_qualified_name(qn)
        except ValueError:
            continue
        if "." in entry:
            known_mcp_servers.add(entry.split(".", 1)[0])
    known_agents: frozenset[str] = frozenset(
        a["name"] for a in (available_agents or [])
        if isinstance(a, dict) and a.get("name")
    )
    static_ops: frozenset[str] = frozenset(KNOWN_STATIC_QUALIFIED_NAMES)

    result: list[str] = []
    for name in names:
        try:
            category, entry_name = split_qualified_name(name)
        except ValueError:
            # Structural rejection (already handled at load; belt+suspenders).
            result.append(name)
            continue

        exists = True
        if category == "skill":
            exists = name in known_skills
        elif category == "agent.peer":
            exists = entry_name in known_agents
        elif category == "mcp.tool":
            exists = name in known_mcp_tools
        elif category == "mcp.server":
            exists = entry_name in known_mcp_servers
        elif category == "memory.entry":
            # Dynamic category enumerated per-session from .reyn/memory/*.md
            # by ``_enumerate_shared_memory_entries``. Static op registry
            # does NOT contain user-saved memory entry slugs; the caller
            # is required to supply ``known_memory_entries`` (= empty
            # frozenset is valid for sessions with zero entries).
            exists = name in known_memory_entries
        else:
            # Operation categories not enumerable from session state:
            # check static op registry. (``rag.corpus__*`` is also a
            # dynamic category but no caller currently seeds it; if/when
            # one does, mirror the memory.entry pattern above.)
            if name in static_ops:
                exists = True
            else:
                # Not in static ops: unknown to the registry.
                exists = False

        if not exists:
            if name not in _warned:
                print(
                    f"[reyn] action_usage: skipping ghost alias "
                    f"{name!r} — not found in current registry",
                    file=sys.stderr,
                )
                _warned.add(name)
            continue

        result.append(name)
    return result


def _build_hot_list_aliases(
    names: list[str],
    short_description_lookup: "dict[str, str] | None" = None,
    *,
    skill_metadata_lookup: "dict[str, dict] | None" = None,
    mcp_tool_lookup: "dict[str, dict] | None" = None,
) -> list[dict]:
    """Build OpenAI-format ToolDefinition dicts for hot list direct aliases.

    Each alias has additionalProperties=True so any args pass through.
    The dispatcher routes these via invoke_action semantics (same path).

    Lever D (B23-PRE-1): when ``short_description_lookup`` is provided,
    embeds the target action's ``short_description`` in the alias
    description with an assertive directive. This surfaces the action's
    purpose directly in the tool listing so the LLM can pick the alias
    without a list_actions / describe_action round-trip.

    When ``short_description_lookup`` is None or the name is absent from
    the map, falls back to the prior generic description so callers that
    don't supply the lookup stay unaffected.

    Universal wrapper names (``list_actions``, ``search_actions``,
    ``describe_action``, ``invoke_action``) are filtered out defensively:
    build_tools() already adds them in section I, so including them here
    would produce duplicate function declarations that Gemini rejects.
    """
    # Defensive filter: drop universal wrapper names before alias construction
    # so that any call site (present or future) cannot introduce duplicates.
    names = [n for n in names if n not in _UNIVERSAL_WRAPPER_NAMES]
    result = []
    lookup = short_description_lookup or {}
    for name in names:
        # D2-min: for operation-category aliases (= passthrough rules in
        # ``_OPERATION_RULES``: web__*, file__*, memory.operation__*,
        # reyn.source__*, rag.operation__*, mcp.operation__*, exec__*),
        # surface the target ToolDefinition's real description + JSON
        # schema directly. Without this the alias arrives at the LLM as
        # `description: "Direct alias for X. Use invoke_action for schema
        # details."` + `parameters: {properties: {}, additionalProperties:
        # true}` — the LLM has neither a use-case hint nor an arg
        # signature, falls back to its training prior ("AI cannot access
        # external sites") and refuses, or hallucinates control-character
        # tool-call text. The FP-0034 D2 "hot list direct alias は full
        # schema 提供" intent is realised here for the passthrough
        # categories; resource categories (skill__X / agent.peer__X /
        # mcp.tool__X.Y / memory.entry__X / rag.corpus__X) need per-
        # resource schema introspection and are out of scope for D2-min.
        rich = _operation_alias_metadata(name) or _resource_alias_metadata(
            name,
            skill_metadata_lookup=skill_metadata_lookup,
            mcp_tool_lookup=mcp_tool_lookup,
        )
        if rich is not None:
            description, parameters = rich
        else:
            short_desc = lookup.get(name, "")
            if short_desc:
                description = (
                    f"{short_desc}. "
                    f"Use this direct alias to invoke {name} without going "
                    "through invoke_action."
                )
            else:
                description = (
                    f"Direct alias for {name}. "
                    "Use invoke_action for schema details."
                )
            parameters = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        })
    return result


def _operation_alias_metadata(
    qualified_name: str,
) -> "tuple[str, dict] | None":
    """Return ``(description, parameters)`` for an operation-category alias.

    Scoped to qualified names whose category routes through ``_OPERATION_RULES``
    in ``reyn.tools.universal_dispatch`` (= ``_passthrough_args`` transform —
    the alias's args are forwarded verbatim to the target). For those, the
    target ``ToolDefinition.description`` and ``ToolDefinition.parameters``
    are the correct alias metadata.

    Returns ``None`` for resource-category aliases (skill / agent.peer /
    mcp.tool / memory.entry / rag.corpus) — those route through
    ``_RESOURCE_RULES`` whose target is a generic dispatcher (``invoke_skill``
    etc.) whose parameters do NOT match the resource's actual input schema.
    Those need per-resource schema introspection (D2-full).
    """
    # Late imports to avoid circular dependency at module load time.
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        KNOWN_STATIC_QUALIFIED_NAMES,
        resolve_describe_action,
    )

    if qualified_name not in KNOWN_STATIC_QUALIFIED_NAMES:
        return None
    try:
        resolved = resolve_describe_action(qualified_name)
    except Exception:
        return None
    tool = get_default_registry().lookup(resolved.target_tool_name)
    if tool is None:
        return None
    return tool.description, dict(tool.parameters)


def _resource_alias_metadata(
    qualified_name: str,
    *,
    skill_metadata_lookup: "dict[str, dict] | None" = None,
    mcp_tool_lookup: "dict[str, dict] | None" = None,
) -> "tuple[str, dict] | None":
    """Return ``(description, parameters)`` for a resource-category alias
    whose schema can be derived from either the routing target (static
    categories) or session-time per-resource metadata (dynamic categories).

    Covered:
      - ``agent.peer__<name>`` (D2-full step 1) — accepts ``{request, ...}``;
        rule curries ``to=<name>``. Source: ``delegate_to_agent`` parameters
        minus ``to``.
      - ``mcp.server__<name>`` (step 1) — accepts ``{}``; rule curries
        ``server=<name>``. The action IS "list this server's tools".
      - ``rag.corpus__<name>`` (step 1) — accepts ``{query, top_k?, ...}``;
        rule curries ``sources=[<name>]``. Source: ``recall`` parameters
        minus ``sources``.
      - ``skill__<name>`` (step 2) — caller supplies ``skill_metadata_lookup``
        keyed by qualified name with ``{description?, input_schema?,
        input_wrapped?}``. The transform ``_invoke_skill_args`` wraps caller
        args under the artifact ``data`` slot, so the alias's parameters
        are the artifact's data schema directly (= ``input_schema``).
      - ``mcp.tool__<server>.<tool>`` (step 3) — caller supplies
        ``mcp_tool_lookup`` keyed by qualified name with ``{description?,
        input_schema?}`` from the MCP server's declared tool schema.

    Returns ``None`` for:
      - any unhandled category, e.g. ``memory.entry__X`` — the current
        ``_read_memory_body_args`` transform sends ``{name: entry}`` but
        the target ``read_memory_body`` expects ``{layer, slug}``;
        pre-existing dispatch shape mismatch, surface separately.
    """
    from reyn.tools import get_default_registry
    from reyn.tools.universal_catalog import split_qualified_name

    try:
        category, entry_name = split_qualified_name(qualified_name)
    except ValueError:
        return None

    registry = get_default_registry()

    if category == "agent.peer":
        tool = registry.lookup("delegate_to_agent")
        if tool is None:
            return None
        params = _drop_required_field(dict(tool.parameters), "to")
        description = (
            f"Delegate a request to peer agent {entry_name!r}. "
            f"{tool.description}"
        )
        return description, params

    if category == "mcp.server":
        params = {"type": "object", "properties": {}, "required": []}
        description = (
            f"List the MCP tools exposed by server {entry_name!r}. "
            f"Returns name + description for each tool."
        )
        return description, params

    if category == "rag.corpus":
        tool = registry.lookup("recall")
        if tool is None:
            return None
        params = _drop_required_field(dict(tool.parameters), "sources")
        first_line = tool.description.splitlines()[0] if tool.description else ""
        description = (
            f"Recall (semantic search) against indexed source {entry_name!r}. "
            f"Single-source variant of: {first_line}"
        )
        return description, params

    if category == "skill":
        meta = (skill_metadata_lookup or {}).get(qualified_name)
        if not meta or "input_schema" not in meta:
            return None
        schema = dict(meta["input_schema"])
        desc_body = meta.get("description") or f"Skill {entry_name!r}"
        description = (
            f"{desc_body}. Hot-list direct alias for skill {entry_name!r} "
            f"— pass the skill's input fields as args; the dispatcher wraps "
            f"them into the input artifact for invoke_skill."
        )
        return description, schema

    if category == "mcp.tool":
        meta = (mcp_tool_lookup or {}).get(qualified_name)
        if not meta or "input_schema" not in meta:
            return None
        schema = dict(meta["input_schema"])
        desc_body = meta.get("description") or f"MCP tool {entry_name!r}"
        description = (
            f"{desc_body}. Hot-list direct alias for MCP tool "
            f"{entry_name!r} — pass the tool's declared inputSchema args; "
            f"the dispatcher routes via call_mcp_tool."
        )
        return description, schema

    if category == "memory.entry":
        # E2e-coder 2026-05-17 N4 probe: memory.entry__<slug> aliases were
        # previously marked unhandled because _read_memory_body_args sent
        # {name: slug} while read_memory_body required {layer, slug} — that
        # transform is now fixed (universal_dispatch.py) to send the
        # canonical {layer: "shared", slug} pair, so the alias can be
        # surfaced with an empty input schema (the qualified name encodes
        # the slug; layer defaults to "shared").
        meta = (skill_metadata_lookup or {}).get(qualified_name) or {}
        desc_body = meta.get("description") or f"shared memory entry {entry_name!r}"
        description = (
            f"Read the body of shared memory entry {entry_name!r}. "
            f"{desc_body}"
        )
        params = {"type": "object", "properties": {}, "required": []}
        return description, params

    return None


def _enumerate_shared_memory_entries(host: Any) -> dict[str, dict]:
    """List shared memory entries as ``memory.entry__<slug>`` → metadata.

    Scans the shared memory layer's directory (= ``<cwd>/.reyn/memory``) for
    ``*.md`` files, ignoring the ``MEMORY.md`` index. The returned mapping is
    keyed by qualified action name so the caller can:

      - extend the hot-list seed (so the alias appears in ``tools=`` without
        the LLM running a discovery ``list_actions(category=['memory.entry'])``
        first), and
      - populate the alias metadata lookup so ``_resource_alias_metadata``
        renders a human description from the entry's frontmatter rather
        than a generic placeholder.

    Returns an empty dict when the layer's directory is absent, a host
    method is missing, or the filesystem read raises — this surface is
    advisory (= a missing entry just means the LLM has to discover it via
    ``list_actions``), so failures are silently absorbed rather than raised.

    Used by 2026-05-17 N4 fix; see project memory entry for context.
    """
    out: dict[str, dict] = {}
    memory_dir_fn = getattr(host, "memory_dir", None)
    if memory_dir_fn is None:
        return out
    try:
        mem_dir = Path(memory_dir_fn("shared"))
    except Exception:
        return out
    if not mem_dir.is_dir():
        return out
    for md_file in sorted(mem_dir.glob("*.md")):
        if md_file.name == "MEMORY.md":
            continue
        slug = md_file.stem
        meta: dict = {"description": f"shared memory entry {slug!r}"}
        # Best-effort frontmatter parse to surface the user-facing
        # "description" field. Files without a parseable frontmatter
        # fall back to the generic placeholder above.
        try:
            text = md_file.read_text(encoding="utf-8")
            stripped = _strip_frontmatter(text)
            if stripped != text:
                # frontmatter present; extract description line
                lines = text.split("\n")
                in_fm = False
                for line in lines:
                    s = line.strip()
                    if s == "---":
                        if in_fm:
                            break
                        in_fm = True
                        continue
                    if in_fm and s.startswith("description:"):
                        desc = s.split(":", 1)[1].strip()
                        if desc:
                            meta["description"] = desc
                        break
        except OSError:
            pass
        out[f"memory.entry__{slug}"] = meta
    return out


def _drop_required_field(params: dict, field_name: str) -> dict:
    """Return a copy of a JSON schema with ``field_name`` removed from
    ``properties`` and ``required``.

    Used by ``_resource_alias_metadata`` to remove curried fields from a
    target tool's parameters before exposing them on the alias.
    """
    out = dict(params)
    props = dict(out.get("properties") or {})
    props.pop(field_name, None)
    out["properties"] = props
    req = [r for r in (out.get("required") or []) if r != field_name]
    out["required"] = req
    return out


def _collect_all_session_ars_entries(
    skill_meta_map: "dict[str, dict] | None" = None,
    mcp_tool_map: "dict[str, dict] | None" = None,
    available_agents: "list[dict] | None" = None,
    *,
    known_skill_names: "frozenset[str] | None" = None,
) -> "list[tuple[str, dict]]":
    """Collect (qualified_name, properties) for all session-visible actions.

    D2-wrapper scope expansion (B38): B37's D2-wrapper fix was hot-list-only,
    causing schema-blind LLM calls for any action absent from the hot list
    (N=4 same-batch B37 observations: file__write, rag.operation__drop_source,
    agent.peer__researcher all hallucinated non-canonical keys).

    Collects the full session-visible ARS set from five sources:

    1. **Static operations**: all entries in ``KNOWN_STATIC_QUALIFIED_NAMES``
       (file / web / memory.operation / reyn.source / rag.operation /
       mcp.operation / exec). Schemas come from the target ToolDefinition in
       the default registry via ``resolve_describe_action``. Always populated;
       no session state required.

    2. **Session skills (schemed)**: keyed by qualified name ``skill__<name>``
       in ``skill_meta_map``, which carries the skill's ``input_schema``.
       Only skills with a non-empty input schema contribute properties.

    2b. **Session skills (empty-schema)** — B40 cognitive-bias fix: when
       ``known_skill_names`` is supplied, skills that exist in the session
       registry but have an empty / absent input schema (= no
       ``user_message.yaml`` artifact in their dir) are emitted with empty
       properties. Without this, names like ``skill__mcp_search`` are
       invisible to wrapper-path routing — the LLM falls back to
       category-prefix guessing (B39 W6 R-WEB: ``"mcp_search スキル"``
       → 100% ``mcp.server`` miscategorization at fresh action_usage state).
       Care-boundary §1 ("the LLM doesn't have to guess what exists")
       places skill visibility in OS pre-call structural responsibility.

    3. **Session MCP tools**: keyed by ``mcp.tool__<server>.<tool>`` in
       ``mcp_tool_map``, which carries the tool's ``input_schema`` from the
       MCP server's declared tool schema.

    4. **Session peer agents**: derived from ``available_agents`` using the
       same ``delegate_to_agent`` schema logic as ``_resource_alias_metadata``.
       The ``to`` field is curried by the router, so it is dropped from the
       exposed schema.

    Returns a deduplicated list of ``(qualified_name, properties_dict)`` pairs.
    Schemed actions carry their property dict; empty-schema actions from
    Source 2b carry ``{}``. P7-clean: no hardcoded action names; all data
    comes from the registry or caller-supplied session state.
    """
    # Late imports to avoid circular dependency at module load time.
    from reyn.tools import get_default_registry
    from reyn.tools.universal_catalog import build_qualified_name
    from reyn.tools.universal_dispatch import (
        KNOWN_STATIC_QUALIFIED_NAMES,
        resolve_describe_action,
    )

    entries: list[tuple[str, dict]] = []
    seen: set[str] = set()

    # Source 1: static operations — always available from the registry.
    registry = get_default_registry()
    for qn in KNOWN_STATIC_QUALIFIED_NAMES:
        try:
            resolved = resolve_describe_action(qn)
            tool = registry.lookup(resolved.target_tool_name)
        except Exception:
            continue
        if tool is None:
            continue
        props = tool.parameters.get("properties") or {}
        if not props:
            continue
        if qn not in seen:
            entries.append((qn, dict(props)))
            seen.add(qn)

    # Source 2: session skills from skill_meta_map.
    for qn, meta in (skill_meta_map or {}).items():
        if qn in seen:
            continue
        schema = meta.get("input_schema") or {}
        props = schema.get("properties") if isinstance(schema, dict) else {}
        if not props:
            continue
        entries.append((qn, dict(props)))
        seen.add(qn)

    # Source 2b (B40 cognitive-bias fix): empty-schema session skills.
    # See docstring Source 2b for full rationale.
    if known_skill_names:
        for qn in sorted(known_skill_names):
            if qn in seen:
                continue
            entries.append((qn, {}))
            seen.add(qn)

    # Source 3: session MCP tools from mcp_tool_map.
    for qn, meta in (mcp_tool_map or {}).items():
        if qn in seen:
            continue
        schema = meta.get("input_schema") or {}
        props = schema.get("properties") if isinstance(schema, dict) else {}
        if not props:
            continue
        entries.append((qn, dict(props)))
        seen.add(qn)

    # Source 4: session peer agents from available_agents.
    # Derive schema from ``delegate_to_agent`` minus the curried ``to`` field,
    # same as ``_resource_alias_metadata`` does for hot-list aliases.
    if available_agents:
        delegate_tool = registry.lookup("delegate_to_agent")
        if delegate_tool is not None:
            base_props = dict(
                _drop_required_field(dict(delegate_tool.parameters), "to")
                .get("properties") or {}
            )
            if base_props:
                for agent in available_agents:
                    if not isinstance(agent, dict) or not agent.get("name"):
                        continue
                    qn = build_qualified_name("agent.peer", agent["name"])
                    if qn in seen:
                        continue
                    entries.append((qn, base_props))
                    seen.add(qn)

    return entries


def _enrich_invoke_action_description(
    tools: list[dict],
    ars_entries: "list[tuple[str, dict]]",
) -> list[dict]:
    """Append an ACTION ARG SCHEMAS block to invoke_action's description.

    D2-wrapper scope expansion (B38): extends the B37 fix from hot-list-only
    to all session-visible actions. ``ars_entries`` is a list of
    ``(qualified_name, properties_dict)`` pairs produced by
    ``_collect_all_session_ars_entries`` — a superset of what the hot list
    covered.

    The ARS block gives the LLM canonical arg key names for any action it
    might route via ``invoke_action``, regardless of whether that action is
    in the current hot list. This eliminates schema-blind hallucination
    (B37 N=4: file__write -> ``text`` not ``content``,
    rag.operation__drop_source -> ``source_id`` / ``source_name`` not
    ``source``, agent.peer__researcher -> ``message`` not ``request``).

    P7-clean: no hardcoded action names; all data is data-driven from
    ``ars_entries`` supplied by the caller.
    """
    if not ars_entries:
        return tools

    # Build compact schema lines: "  <action_name>: {key1, key2, ...}"
    # Empty-props entries (= B40 cognitive-bias fix Source 2b: empty-schema
    # skills) render as "  <action_name>: {}" so they're visible to wrapper
    # routing without a false schema signal.
    schema_lines: list[str] = []
    for name, props in ars_entries:
        if not name:
            continue
        if props:
            keys = ", ".join(sorted(props.keys()))
            schema_lines.append(f"  {name}: {{{keys}}}")
        else:
            schema_lines.append(f"  {name}: {{}}")

    if not schema_lines:
        return tools

    # Issue #229: the action names below are NOT directly callable as
    # tool functions — they are operands for invoke_action. Weak models
    # (B42 W5-S6 observed) read this block, treat ``skill__mcp_install``
    # as a function name, and emit a direct ``function_call`` that the
    # dispatcher rejects with ``unknown_tool``. The explicit instruction
    # below is the structural countermeasure; the router-loop salvage
    # in ``_maybe_salvage_qualified_direct_call`` is the safety net.
    hint = (
        "\n\nACTION ARG SCHEMAS (canonical keys for all session-visible actions):\n"
        + "\n".join(schema_lines)
        + "\n\nThe names above are NOT direct-callable tool functions — "
        + "they are operands. Always invoke them as "
        + "``invoke_action(action_name=\"<name>\", args={...})``; do NOT "
        + "emit them as direct function calls.\n"
        + "Use these exact key names in args when calling invoke_action."
    )

    # Find and patch invoke_action in-place (mutates a copy to avoid
    # modifying the ToolDefinition registry object).
    result = []
    for tool in tools:
        fn = tool.get("function") or {}
        if fn.get("name") == "invoke_action":
            tool = {
                "type": tool.get("type", "function"),
                "function": {
                    **fn,
                    "description": fn.get("description", "") + hint,
                },
            }
        result.append(tool)
    return result


# ---------------------------------------------------------------------------
# RouterLoop
# ---------------------------------------------------------------------------

class RouterLoop:
    """Drives the chat router via native LLM tool_use.

    Loops: build tools+prompt → call_llm_tools → if tool_calls, execute
    in parallel, append results to messages, repeat → if text, emit to
    outbox and stop. Bounded by max_iterations.
    """

    def __init__(
        self,
        host: RouterLoopHost,
        chain_id: str,
        max_iterations: int = 5,
        router_model: str = "light",  # config tier (light = intent classification)
        budget: Any = None,  # BudgetTracker | None — process-shared cost tracker
        system_prompt_override: str | None = None,
        exclude_tools: set[str] | None = None,
        memo_provider: Any = None,  # SubLoopMemoProvider | None (ADR-0025)
        skill_search_config: "SkillSearchConfig | None" = None,  # FP-0024-A BM25 pre-filter
        empty_stop_retry_directive: str | None = None,  # B42-NF-W6-1 opt-in retry
        llm_caller: "Any | None" = None,  # Tier 2 test seam: real-fake injection
    ):
        self.host = host
        self.chain_id = chain_id
        self.max_iterations = max_iterations
        self.router_model = router_model
        self.budget = budget
        # When set, RouterLoop skips ``build_system_prompt(host=...)`` and uses
        # this string verbatim as the system message. Plan executor uses this
        # to inject a step-specific narrow prompt (= "you are executing step X
        # of a plan") instead of the full chat router prompt. The host facade
        # still controls the tool catalog narrowing.
        self._system_prompt_override = system_prompt_override
        # Tool names to drop from the catalog (= post-build filter). Used by
        # plan executor to pass ``{"plan"}`` so plan steps cannot recursively
        # call plan (= prevents unbounded nesting). Discovered 2026-05-07:
        # without this, plan-mode dogfood "Read README and CLAUDE.md, then
        # compare" produced 3 plan invocations because step LLMs saw plan
        # in their tool catalog and self-decomposed.
        self._exclude_tools: frozenset[str] = frozenset(exclude_tools or set())
        # FP-0034 Phase 2 step 1: action embedding index background
        # build task handle.  None until the first turn that finds the
        # index configured + not ready, then asyncio.Task while
        # building.  Stays set after completion so we don't re-spawn
        # (the build itself is idempotent via catalog hash, but we
        # avoid the extra Task overhead).
        self._action_index_build_task: "asyncio.Task[None] | None" = None
        # ADR-0025: optional sub-loop LLM call memoization. When set,
        # ``call_llm_tools`` invocations consult the provider before
        # invoking — args_hash hit returns the recorded LLMToolCallResult
        # without paying LLM cost. Used by plan-mode resume so a crashed
        # mid-step sub-loop replays its earlier LLM turns from snapshot
        # rather than re-paying. ``None`` = normal execution (no memo).
        self._memo_provider = memo_provider
        # FP-0024-A: BM25 skill pre-filter config. None = use OS defaults.
        self._skill_search_config = skill_search_config
        # B42-NF-W6-1: directive used as a continuation prompt when an empty
        # stop is detected after a tool-call round. None (= default) preserves
        # the existing chat-router "observe + surface" policy. The plan
        # executor passes a plan-step-appropriate directive ("now report what
        # you found") so the post-tool empty-stop attractor that hits 10/10
        # on Gemini 2.5 Flash Lite (and is documented across providers — see
        # platform.claude.com handling-stop-reasons docs) can be broken with
        # one retry. Even when set, the actual retry behaviour is gated by
        # the ``REYN_EMPTY_STOP_RETRY`` env var so operators opt in per
        # process — the directive plumbing lands in the codebase but no
        # default runtime behaviour change.
        self._empty_stop_retry_directive = empty_stop_retry_directive
        # Tier 2 test seam: when set, ``run()`` calls this callable instead of
        # the module-level ``call_llm_tools``. Allows real-fake injection
        # (= scripted async callable) without ``unittest.mock.patch`` — per
        # testing.ja.md hard rule that forbids ``MagicMock / AsyncMock /
        # patch``. Production callers leave this as ``None``.
        self._llm_caller = llm_caller
        self._catalog: dict[str, dict] = {}  # populated per run()
        self._tool_names: frozenset[str] = frozenset()  # kept for backward compat
        self._total_usage: TokenUsage = TokenUsage()

    @property
    def total_usage(self) -> TokenUsage:
        """Accumulated token usage across all LLM calls made in this loop."""
        return self._total_usage

    async def run(self, user_text: str, history: list[dict]) -> TokenUsage:
        """Process one user utterance end-to-end. Emits to host.put_outbox.

        Returns the total TokenUsage accumulated across all LLM calls so the
        caller can credit it to the session-level usage counter (F4 Bug 2).
        """
        self._total_usage = TokenUsage()
        host = self.host
        all_skills = host.list_available_skills()
        # FP-0024 Component A — BM25 skill pre-filter.
        # Narrow available_skills to top-K BM25 keyword matches when the
        # catalogue exceeds the threshold. Falls through to full enum on
        # 0 BM25 results so no skill can become invisible (Fallback safety).
        skills_for_tools = self._apply_skill_search(all_skills, user_text)
        # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list recording.
        # Resolved once per run() so recording below can reuse without re-fetching.
        _tracker_getter = getattr(host, "get_action_usage_tracker", None)
        _tracker = _tracker_getter() if _tracker_getter else None
        # FP-0034 PR-3b-iii: read universal wrapper visibility from host.
        # getattr fallback so narrow hosts (= plan-mode sub-host) that
        # don't implement the method default to off (= preserve prior
        # plan-step tools= shape).
        _univ_enabled_getter = getattr(
            host, "get_universal_wrappers_enabled", None,
        )
        _univ_enabled = bool(_univ_enabled_getter()) if _univ_enabled_getter else False
        # Same getattr-fallback pattern: hosts without get_cwd (= FakeRouterHost
        # in LLMReplay tests) skip the Environment section so the SP byte
        # content stays unchanged for cached fixtures.
        _cwd_getter = getattr(host, "get_cwd", None)
        _cwd_str = _cwd_getter() if _cwd_getter else None
        # FP-0034 Phase 2 step 1: D14 visibility gate for search_actions.
        # Only show search_actions when (a) wrappers are on, (b) the
        # operator configured an embedding model class, AND (c) the
        # session has an ActionEmbeddingIndex that is_ready().  Any
        # missing signal degrades to "hide" so the LLM does not see a
        # tool whose query would return empty results.
        _search_visible = False
        if _univ_enabled:
            _idx_getter = getattr(host, "get_action_embedding_index", None)
            _provider_getter = getattr(host, "get_embedding_provider", None)
            _model_getter = getattr(host, "get_embedding_model_class", None)
            _eager_getter = getattr(host, "get_eager_embedding_build", None)
            _idx = _idx_getter() if _idx_getter else None
            _provider = _provider_getter() if _provider_getter else None
            _model_class = _model_getter() if _model_getter else None
            _eager_embedding_build = bool(_eager_getter()) if _eager_getter else False
            # B25-S5-1: when eager flag is set, await the build synchronously
            # before computing _search_visible. This pays the build cost on
            # the first turn (= once per session; subsequent turns see
            # is_ready() True via SQLite cache) but eliminates the cold-start
            # race where search_actions is hidden from the LLM on Turn 1.
            if (
                _eager_embedding_build
                and _idx is not None
                and _provider is not None
                and _model_class
                and not getattr(_idx, "is_ready", lambda: False)()
            ):
                await self._build_action_embedding_index_background(
                    _idx, _provider, _model_class,
                )
            if (
                _idx is not None
                and _model_class
                and getattr(_idx, "is_ready", lambda: False)()
            ):
                _search_visible = True
            # FP-0034 Phase 2 step 1: kick off the background build
            # when the index is configured but not yet ready.  The
            # build is idempotent (= same catalog hash → no-op) and
            # serialised by the index's internal lock.  Only spawned
            # once per RouterLoop (= per chain) via the
            # ``_action_index_build_task`` flag below.
            if (
                _idx is not None
                and _provider is not None
                and _model_class
                and not getattr(_idx, "is_ready", lambda: False)()
                and getattr(self, "_action_index_build_task", None) is None
            ):
                self._action_index_build_task = asyncio.create_task(
                    self._build_action_embedding_index_background(
                        _idx, _provider, _model_class,
                    )
                )
        # D2-wrapper scope expansion (B38): build session-level resource
        # metadata maps when universal wrappers are enabled — regardless of
        # whether a tracker is present. These maps feed both the hot-list
        # alias builder (below) AND _collect_all_session_ars_entries (which
        # needs skill / MCP tool schemas to populate the full ARS block).
        _skill_meta_map: dict[str, dict] = {}
        _mcp_tool_map: dict[str, dict] = {}
        if _univ_enabled:
            # Lever D (B23-PRE-1): build short_description map from
            # available skills so aliases embed the target's purpose.
            # FP-0034 D2-full step 2: also capture per-skill
            # ``input_schema`` (when the skill's entry artifact has
            # a structured shape) so ``skill__<name>`` hot-list
            # aliases expose the actual input parameters instead
            # of the empty ``properties: {}, additionalProperties:
            # True`` stub.
            _short_desc_map: dict[str, str] = {}
            _known_skill_names: set[str] = set()
            for _s in host.list_available_skills():
                if not isinstance(_s, dict) or "name" not in _s:
                    continue
                _qn = f"skill__{_s['name']}"
                _known_skill_names.add(_qn)
                _sd = _s.get("description") or _s.get("short_description") or ""
                if _sd:
                    _short_desc_map[_qn] = str(_sd)
                if "input_schema" in _s:
                    _skill_meta_map[_qn] = {
                        "description": str(_sd) if _sd else "",
                        "input_schema": _s["input_schema"],
                        "input_wrapped": bool(_s.get("input_wrapped", True)),
                    }
            # FP-0034 D2-full step 3: per-MCP-tool inputSchema lookup
            # so ``mcp.tool__<server>.<tool>`` aliases expose the
            # tool's declared args directly. host.get_mcp_servers()
            # returns the FP-0032 expanded shape with nested tools.
            for _srv in (host.get_mcp_servers() or []):
                if not isinstance(_srv, dict):
                    continue
                _server_name = _srv.get("name")
                if not _server_name:
                    continue
                for _t in (_srv.get("tools") or []):
                    if not isinstance(_t, dict):
                        continue
                    _tool_name = _t.get("name")
                    _input_schema = _t.get("inputSchema") or _t.get("input_schema")
                    if not _tool_name or not _input_schema:
                        continue
                    _qn = f"mcp.tool__{_server_name}.{_tool_name}"
                    _mcp_tool_map[_qn] = {
                        "description": str(_t.get("description") or ""),
                        "input_schema": _input_schema,
                    }
        # FP-0034 Phase 2 step 5: hot list aliases for frequent actions.
        # Build only when universal wrappers are on and a tracker is present.
        _hot_list_aliases: list[dict] | None = None
        if _univ_enabled and _tracker is not None:
            from reyn.config import ActionRetrievalConfig as _ARC
            _ar_cfg_getter = getattr(host, "get_action_retrieval_config", None)
            _ar_cfg = _ar_cfg_getter() if _ar_cfg_getter else None
            if _ar_cfg is None:
                _ar_cfg = _ARC()
            _n = _ar_cfg.hot_list_n
            if _n > 0:
                from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
                _seed_cfg = _ar_cfg.hot_list_seed
                _seed: list[str] = (
                    list(DEFAULT_HOT_LIST_SEED)
                    if _seed_cfg == "default"
                    else (list(_seed_cfg) if isinstance(_seed_cfg, list) else [])
                )
                # N4 (2026-05-17): seed shared memory entries dynamically so
                # the LLM can read user-saved memory in a fresh session
                # without first running list_actions(category=['memory.entry']).
                # Without this, cross-session memory retrieval requires a
                # discovery step the weak default model rarely takes.
                # Populates skill_metadata_lookup so the alias gets a
                # human-readable description (= the entry's frontmatter
                # `description`).
                _memory_entries = _enumerate_shared_memory_entries(host)
                for _qn, _meta in _memory_entries.items():
                    if _qn not in _seed:
                        _seed.append(_qn)
                    # _skill_meta_map is reused as a generic
                    # qualified-name → metadata lookup; see
                    # _resource_alias_metadata's memory.entry branch.
                    _skill_meta_map.setdefault(_qn, _meta)
                _top_names = _tracker.get_top_n(_n, _seed)
                # B38 W2: registry-existence check — filter names that pass
                # structural validation but no longer resolve to a real action
                # in the current session registry. Runs after get_top_n so
                # RouterState (skill / mcp / agent registry) is available.
                _top_names = _filter_ghost_names_by_registry(
                    _top_names,
                    skill_meta_map=_skill_meta_map or None,
                    mcp_tool_map=_mcp_tool_map or None,
                    available_agents=host.list_available_agents() or None,
                    known_skill_names=frozenset(_known_skill_names) or None,
                    # Dynamic memory.entry__<slug> names enumerated above
                    # from .reyn/memory/*.md. Empty set means "no entries
                    # exist this session" — filter rejects any stale
                    # memory.entry name still in the action_usage tracker.
                    known_memory_entries=frozenset(_memory_entries),
                )
                if _top_names:
                    _hot_list_aliases = _build_hot_list_aliases(
                        _top_names,
                        short_description_lookup=_short_desc_map or None,
                        skill_metadata_lookup=_skill_meta_map or None,
                        mcp_tool_lookup=_mcp_tool_map or None,
                    )
        tools = build_tools(
            skills_for_tools,
            host.list_available_agents(),
            file_permissions=host.get_file_permissions(),
            mcp_servers=host.get_mcp_servers(),
            web_fetch_allowed=host.get_web_fetch_allowed(),
            universal_wrappers_enabled=_univ_enabled,
            search_actions_visible=_search_visible,
            hot_list_aliases=_hot_list_aliases,
        )
        # D2-wrapper scope expansion (B38): propagate schemas for ALL
        # session-visible actions into invoke_action's description so the
        # LLM can see canonical arg key names when it routes via the wrapper,
        # regardless of hot-list state. B37's D2-wrapper was hot-list-only;
        # B38 expands the scope to the full static operation catalog plus
        # session-visible skills / MCP tools / peer agents.
        if _univ_enabled:
            _ars_entries = _collect_all_session_ars_entries(
                skill_meta_map=_skill_meta_map or None,
                mcp_tool_map=_mcp_tool_map or None,
                available_agents=host.list_available_agents() or None,
                # B40 cognitive-bias fix: surface empty-schema skills in ARS
                # so wrapper-path routing can pick them by name. Without this,
                # cold-start LLMs at hot_list_n=10 + fresh action_usage fall
                # back to category-prefix guessing for skill names with
                # mcp_/file_/web_ overlap (B39 W6 R-WEB: narr-1 / S4 / others).
                known_skill_names=(
                    frozenset(_known_skill_names) if _known_skill_names else None
                ),
            )
            tools = _enrich_invoke_action_description(tools, _ars_entries)
        if self._exclude_tools:
            tools = [
                t for t in tools
                if t.get("function", {}).get("name") not in self._exclude_tools
            ]
        self._catalog = {t["function"]["name"]: t for t in tools}
        self._tool_names = frozenset(self._catalog.keys())  # backward compat
        if self._system_prompt_override is not None:
            system_prompt = self._system_prompt_override
        else:
            # ADR-0033: pre-fetch indexed sources before building the
            # (sync) system prompt. format_for_prompt() reads the mem
            # cache (fast path when manifest already loaded) and returns
            # the rendered section including the empty-state hint.
            indexed_sources = await get_source_manifest(
                Path.cwd()
            ).format_for_prompt()
            system_prompt = build_system_prompt(
                agent_name=host.agent_name,
                agent_role=host.agent_role,
                available_skills=host.list_available_skills(),
                available_agents=host.list_available_agents(),
                memory_index=host.get_memory_index(),
                file_permissions=host.get_file_permissions(),
                mcp_servers=host.get_mcp_servers(),
                web_fetch_allowed=host.get_web_fetch_allowed(),
                output_language=host.output_language,
                project_context=host.get_project_context(),
                indexed_sources_section=indexed_sources,
                # FP-0034 PR-3b-v: same getattr-fallback pattern as build_tools.
                # Hosts without get_universal_wrappers_enabled (= FakeRouterHost
                # in LLMReplay tests) default to False so SP byte content stays
                # unchanged for cached fixtures.
                universal_wrappers_enabled=_univ_enabled,
                cwd=_cwd_str,
                # FP-0034 §D14: propagate the search_actions D14 visibility gate
                # into the SP so the wrapper enumeration matches tools=.
                # When wrappers are off (_univ_enabled=False), pass True so the
                # SP stays byte-identical to the pre-fix output — those callers
                # are non-wrapper-path tests whose fixtures already include
                # search_actions in the wrapper line and re-recording is not
                # wanted.  When wrappers are on, _search_visible is the runtime
                # truth derived from is_search_available() (= embedding_class
                # configured + index ready); False there means the SP and tools=
                # both exclude search_actions, eliminating the N5 hallucination.
                search_actions_enabled=_search_visible if _univ_enabled else True,
            )
        # ChatSession._handle_user_message appends the user turn to history
        # BEFORE invoking _run_router_loop, so by the time we get here the
        # caller's `history` argument already ends with this turn's user
        # message. Appending it again as a trailing user message creates a
        # consecutive-duplicate-user pair that confuses the LLM (= G12-style
        # empty-stop attractor was reproduced via mcp_probe at ~80% rate
        # against gemini-2.5-flash-lite). Use history as-is; only fall back
        # to an explicit append if for some reason the latest history entry
        # is NOT this turn's user text (= defensive — keeps tests that pass
        # an empty / mismatched history alive).
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        # When history's last entry is a user message, trust it — it is
        # either:
        #   - text identical to ``user_text`` (= the normal chat path,
        #     ChatSession already appended it via _append_history); or
        #   - a content-list shape (= issue #366 multimodal turn where
        #     the user attached images via /image; comparing string
        #     ``user_text`` against the list would always fail and
        #     produce a duplicate text-only entry).
        # Only append the fallback text user message when history is
        # empty / mismatched (= defensive for direct-RouterLoop tests).
        if not history or history[-1].get("role") != "user":
            messages.append({"role": "user", "content": user_text})

        # B28-Q2 Case A: per-turn counters for chat_turn_completed_inline.
        # _routing_decided_fired: set to True the first time routing_decided
        #   is emitted in this turn (= invoke_action or hot_list_alias path).
        # _tool_calls_attempted: count of tool_call rounds where the LLM
        #   invoked at least one tool (including non-catalog tools).
        _routing_decided_fired: bool = False
        _tool_calls_attempted: int = 0
        # B42-NF-W6-1: empty-stop retry counter. The empty-stop handler
        # consults this before injecting a continuation prompt + looping,
        # so retries are bounded at 1 per turn (= no infinite loops if
        # the LLM keeps returning empty stops even with the continuation
        # prompt; the second empty stop falls through to the standard
        # "observe + surface" path).
        _empty_stop_retries: int = 0

        for _iteration in range(self.max_iterations):
            resolved_model = host.resolve_model(self.router_model)
            # ADR-0025: memo lookup — a recorded LLMToolCallResult for
            # this exact (model, messages, tools, tool_choice) tuple
            # short-circuits the call. Used by plan-mode resume so a
            # crashed mid-step sub-loop replays earlier LLM turns
            # without re-paying. memo_provider is None for non-resume
            # paths (= chat router main loop, fresh plan runs).
            result = None
            args_hash: str | None = None
            if self._memo_provider is not None:
                from reyn.plan.sub_loop_memo import compute_sub_loop_args_hash
                args_hash = compute_sub_loop_args_hash(
                    model=resolved_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                memo = self._memo_provider.get_recorded_result(args_hash)
                if memo is not None:
                    host.events.emit(
                        "plan_step_llm_memoized",
                        chain_id=self.chain_id,
                        plan_id=getattr(self._memo_provider, "plan_id", None),
                        step_id=getattr(self._memo_provider, "step_id", None),
                        args_hash=args_hash,
                    )
                    result = memo
            if result is None:
                # Tier 2 testability: tests inject a real-fake callable via
                # ``_llm_caller`` (= no unittest.mock.patch needed). None
                # falls through to the module-level ``call_llm_tools`` so
                # production callers don't have to know about the seam.
                _llm = self._llm_caller or call_llm_tools
                result = await _llm(
                    model=resolved_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    skill_name="router",
                    budget=self.budget,
                    budget_agent=host.agent_name,
                    trace_caller="router",
                )
                # Record the fresh result for future resume hit. Defensive:
                # never let recording failure break the loop.
                if self._memo_provider is not None and args_hash is not None:
                    try:
                        await self._memo_provider.record(
                            args_hash=args_hash, result=result,
                        )
                    except Exception as exc:  # noqa: BLE001
                        import logging
                        logging.getLogger(__name__).warning(
                            "sub-loop memo record failed: %r", exc,
                        )
            if result.usage:
                self._total_usage += result.usage
            if result.tool_calls:
                # B28-Q2: count non-empty tool_call rounds for chat_turn_completed_inline.
                _tool_calls_attempted += 1
                # F5 fix (dogfood batch 1): dedupe duplicate async
                # tool_calls within the same round. Weak models
                # occasionally emit `delegate_to_agent` twice in one
                # tool_calls list, which would inbox_put the same
                # request twice and double-charge the peer.
                # G3 fix (dogfood batch 5 B5-M1): extend dedupe to
                # invoke_skill — same skill + same args in one round
                # spawns redundant runs (333k tokens / 51 LLM calls
                # observed). invoke_skill is sync but NOT idempotent
                # from a cost perspective; deduping is safe because
                # same args → same deterministic result.
                tool_calls = self._dedupe_tool_calls_round(result.tool_calls)
                # parallel execute all tool calls (deduped)
                tool_results = await asyncio.gather(*[
                    self._execute_tool(tc) for tc in tool_calls
                ])
                # Detect async-deferred dispatches via the canonical
                # registry (router_tools.get_dispatch_kind() →
                # ToolDefinition.dispatch_kind).  Async tools'
                # results arrive via a separate channel (e.g.
                # delegate_to_agent → PR14 pending_chain re-invokes router
                # in a future turn). The current loop can't wait for the
                # result; if we continue, the LLM would see only "dispatched"
                # status and re-dispatch (per dogfood verify_lead repro).
                # Exit after the dispatch; the future invocation resumes.
                async_count = sum(
                    1
                    for tc in tool_calls
                    if get_dispatch_kind(tc["function"]["name"]) == "async"
                )
                if async_count:
                    plural = "s" if async_count > 1 else ""
                    await self.host.put_outbox(
                        kind="status",
                        text=(
                            f"dispatched {async_count} async request{plural}; "
                            f"awaiting peer reply"
                        ),
                        meta={"chain_id": self.chain_id},
                    )
                    return self._total_usage

                # ADR-0023 Phase 2.1: plan dispatch is now async — the
                # async tool exit branch above already returned. The
                # legacy synchronous "status=ok with text" special-case
                # is preserved as a safety net for hosts that lack
                # spawn_plan_task (= test stubs running planner sync via
                # the AttributeError fallback in dispatch_plan_tool).
                for tc, r in zip(tool_calls, tool_results):
                    if (
                        tc["function"]["name"] == "plan"
                        and isinstance(r, dict)
                        and r.get("status") == "ok"
                        and isinstance(r.get("text"), str)
                        and r["text"]
                    ):
                        await self.host.put_outbox(
                            kind="agent",
                            text=r["text"],
                            meta={"chain_id": self.chain_id, "source": "plan"},
                        )
                        return self._total_usage
                # G10 / B2-M2 fix: intercept invoke_skill tool_failed results and
                # emit a deterministic i18n message instead of letting the LLM
                # generate an English fallback reply. Checked before accumulating
                # messages so the LLM is never called for this error path.
                for tc, r in zip(tool_calls, tool_results):
                    if (
                        tc["function"]["name"] == "invoke_skill"
                        and isinstance(r, dict)
                        and r.get("status") == "error"
                    ):
                        try:
                            args = json.loads(tc["function"].get("arguments", "{}"))
                        except (json.JSONDecodeError, KeyError):
                            args = {}
                        tool_name = args.get("name", "invoke_skill")
                        err_info = r.get("error", {})
                        error_msg = (
                            err_info.get("message", str(r))
                            if isinstance(err_info, dict)
                            else str(err_info)
                        )
                        lang = getattr(host, "output_language", None)
                        tmpl = _TOOL_FAILED_FALLBACK_MSG.get(
                            lang,
                            _TOOL_FAILED_FALLBACK_MSG["en"],
                        )
                        fallback = tmpl.format(
                            tool_name=tool_name, error=error_msg
                        )
                        await host.put_outbox(
                            kind="agent",
                            text=fallback,
                            meta={"chain_id": self.chain_id},
                        )
                        return self._total_usage

                # FP-0034 Phase 2 step 5: record tool calls for hot list freq+recency.
                # Done before message accumulation so recording happens even on
                # subsequent iterations. invoke_action calls record the target
                # action_name; hot list alias calls record the alias name directly.
                if _tracker is not None:
                    for tc in tool_calls:
                        _tc_name = tc.get("function", {}).get("name", "")
                        if not _tc_name:
                            continue
                        if _tc_name == "invoke_action":
                            _tc_args = tc.get("function", {}).get("arguments", {})
                            if isinstance(_tc_args, str):
                                try:
                                    import json as _json_inner
                                    _tc_args = _json_inner.loads(_tc_args)
                                except Exception:
                                    _tc_args = {}
                            _target = (
                                _tc_args.get("action_name", "")
                                if isinstance(_tc_args, dict)
                                else ""
                            )
                            if _target:
                                _tracker.record(_target)
                        else:
                            # hot list direct alias or other tool
                            _tracker.record(_tc_name)
                # FP-0034 Phase 3: routing_decided P6 event for catalog dispatch audit.
                # Emitted independently of tracker (tracker=None is valid when
                # hot_list_n=0, but P6 audit must fire whenever catalog routing
                # actually happened). Guard: only when universal wrappers are on,
                # which is the only condition under which catalog routing occurs.
                if _univ_enabled:
                    for tc, r in zip(tool_calls, tool_results):
                        _rd_name = tc.get("function", {}).get("name", "")
                        if _rd_name == "invoke_action":
                            _rd_args = tc.get("function", {}).get("arguments", {})
                            if isinstance(_rd_args, str):
                                try:
                                    import json as _json_rd
                                    _rd_args = _json_rd.loads(_rd_args)
                                except Exception:  # noqa: BLE001
                                    _rd_args = {}
                            _rd_action = (
                                _rd_args.get("action_name", "")
                                if isinstance(_rd_args, dict) else ""
                            )
                            _rd_source = "invoke_action"
                        elif "__" in _rd_name:
                            # Issue #241: distinguish "real hot-list alias the
                            # LLM correctly used" (= name actually surfaced in
                            # tools[]) from "ARS-only direct call the salvage
                            # path covers" (= name appeared only in the
                            # invoke_action.description ARS block, salvaged
                            # by PR #240). Pre-#241, both cases were tagged
                            # ``"hot_list_alias"`` regardless, muddying the
                            # audit chain for downstream readers (B42 W5-S6).
                            _rd_action = _rd_name
                            if _rd_name in self._catalog:
                                _rd_source = "hot_list_alias"
                            else:
                                _rd_source = "ars_direct"
                        else:
                            continue  # non-catalog tool — skip
                        if not _rd_action:
                            continue
                        _rd_outcome = "error" if (
                            isinstance(r, dict)
                            and ("error" in r or r.get("status") == "error")
                        ) else "success"
                        host.events.emit(
                            "routing_decided",
                            action_name=_rd_action,
                            source=_rd_source,
                            outcome=_rd_outcome,
                            chain_id=self.chain_id,
                        )
                        _routing_decided_fired = True  # B28-Q2: track for inline exclusivity
                # H3-ablation race fix: detect invoke_skill / invoke_action
                # spawn-ack and exit the router loop instead of accumulating
                # the spawn-ack into messages for the next iteration.
                #
                # Race condition observed in dogfood batch 32 §4.2 (= W3 S1
                # file_read_via_chat) and confirmed at the OS layer by the
                # H3 ablation (= patch flipped only that single scenario;
                # K/N = 1/22 at batch scale, but the fix is structural and
                # model-agnostic — H1 strong-model ablation reproduced the
                # same "Understood" hallucination under gemini-2.5-flash,
                # confirming this is OS-layer, not LLM-layer).
                #
                # NF-W7-B43-2 (2026-05-20): the OS-synthetic spawn-ack
                # message itself became an in-context-learning attractor —
                # multi-turn conversations accumulated the literal text in
                # the assistant slot, and weak LLMs echoed it in
                # subsequent turns (10/10 deterministic in trace-patch-
                # replay). The env-gated alternative path below switches
                # to the standard role=tool + LLM-composed reply pattern
                # (= Claude / GPT alignment) with H3 hallucination
                # defense via a tool_result directive (= PR #221
                # ``_post_text`` mechanism). Default behaviour is
                # unchanged; ``REYN_SPAWN_ACK_TO_LLM=1`` opts in to the
                # new pattern.
                _spawn_ack_indices: list[int] = [
                    i
                    for i, (tc, r) in enumerate(zip(tool_calls, tool_results))
                    if (
                        tc["function"]["name"] in ("invoke_skill", "invoke_action")
                        and isinstance(r, dict)
                        and (
                            r.get("status") == "spawned"
                            or (
                                isinstance(r.get("data"), dict)
                                and r["data"].get("status") == "spawned"
                            )
                        )
                    )
                ]
                if _spawn_ack_indices:
                    host.events.emit(
                        "invoke_skill_spawn_ack_exit",
                        spawn_ack_count=len(_spawn_ack_indices),
                        chain_id=self.chain_id,
                    )
                    if os.environ.get("REYN_SPAWN_ACK_TO_LLM") == "1":
                        # NF-W7-B43-2 opt-in path: annotate each spawn-ack
                        # tool_result with the directive via ``_post_text``
                        # so the existing PR #221 serialisation layer below
                        # appends it outside the JSON body with the standard
                        # ``\n\n---\n`` separator. The loop continues
                        # normally — the next LLM iteration composes the
                        # user-facing reply (= 7/10 ACK in N=10 trace
                        # replay) with H3 hallucination defense (= 0/10
                        # hallucinate via the directive wording). Residual
                        # 3/10 EMPTY is covered by PR #265 / PR #287's
                        # ``REYN_EMPTY_STOP_RETRY=1`` retry mechanism.
                        for idx in _spawn_ack_indices:
                            r = tool_results[idx]
                            if isinstance(r, dict):
                                r["_post_text"] = _SPAWN_ACK_TOOL_DIRECTIVE
                        # Fall through to the standard
                        # ``messages.append(...)`` block below.
                    else:
                        # Default (= pre-NF-W7-B43-2) behaviour: OS pushes
                        # the deterministic spawn-ack text to the outbox
                        # and exits the loop. Preserves the existing
                        # contract (= ``meta.source="spawn_ack"``
                        # downstream consumers, no LLM composition for
                        # this turn). The in-context-learning attractor
                        # the new path closes is still present here.
                        lang = getattr(host, "output_language", None)
                        ack_text = _SPAWN_ACK_MSG.get(lang, _SPAWN_ACK_MSG["en"])
                        await host.put_outbox(
                            kind="agent",
                            text=ack_text,
                            meta={"chain_id": self.chain_id, "source": "spawn_ack"},
                        )
                        return self._total_usage
                # No delegation — accumulate messages for next iteration.
                # Use deduped tool_calls so the assistant message and tool
                # result messages stay in sync (matching tool_call_ids).
                assistant_content = result.content or ""
                messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls,
                })
                # E-full PR-E (#383): persist this assistant tool-call turn
                # into chat history so the next ``_build_history_for_router``
                # rebuild emits the same message sequence — without this,
                # follow-up user turns lose visibility into what tools were
                # invoked + with what args.
                #
                # ``getattr`` guard: test fakes that pre-date PR-E may not
                # implement ``append_history_entry``. Production hosts
                # (= RouterHostAdapter) always implement it.
                _append_entry = getattr(host, "append_history_entry", None)
                if _append_entry is not None:
                    _append_entry(
                        role="assistant",
                        content=assistant_content,
                        meta={"chain_id": self.chain_id, "source": "router_tool_turn"},
                        tool_calls=tool_calls,
                    )
                for tc, r in zip(tool_calls, tool_results):
                    # B41-NF-W7-1: tool handlers may attach `_post_text` to
                    # the result dict to surface a textual directive after
                    # the JSON-serialised content (= a place the LLM reads
                    # as instruction, not as part of the structured data).
                    # The field is stripped before JSON serialisation and
                    # appended outside the JSON body. P3-clean: OS handles
                    # the serialisation contract, tool handler declares
                    # intent via an optional field.
                    post_text: str | None = None
                    if isinstance(r, dict) and isinstance(r.get("_post_text"), str):
                        post_text = r["_post_text"]
                        r = {k: v for k, v in r.items() if k != "_post_text"}

                    # Issue #362: extract MCP media blocks (= image, etc.)
                    # BEFORE JSON-serialising the tool result. The blocks
                    # would JSON-stringify into the tool message as opaque
                    # base64 (= wasted tokens) — instead, surface them as a
                    # multimodal follow-up user message so vision-capable
                    # models actually see the image. Strip from `r` so the
                    # tool result text stays compact.
                    media_blocks: list[dict] = []
                    if isinstance(r, dict) and isinstance(r.get("media_blocks"), list):
                        media_blocks = list(r["media_blocks"])
                        r = {k: v for k, v in r.items() if k != "media_blocks"}

                    content_str = json.dumps(r, default=str)
                    if post_text:
                        content_str = f"{content_str}\n\n---\n{post_text}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": content_str,
                    })
                    # E-full PR-E (#383): persist this tool response so the
                    # next router turn sees what the tool returned (= image
                    # follow-up / grep result follow-up / multi-step plan
                    # use cases listed in #383). The path-ref shape from
                    # PR-C keeps storage light when results contain media.
                    if _append_entry is not None:
                        _append_entry(
                            role="tool",
                            content=content_str,
                            meta={"chain_id": self.chain_id, "source": "router_tool_turn"},
                            tool_call_id=tc["id"],
                            name=tc.get("function", {}).get("name"),
                        )
                    if media_blocks:
                        # Issue #383 PR-C: pass the host's media_store so
                        # path-ref blocks are materialised to data URLs at
                        # the LLM wire boundary. ``getattr`` keeps tests
                        # that mock the host with bare attributes happy.
                        followup = _build_media_followup_message(
                            tool_name=tc.get("function", {}).get("name", "tool"),
                            media_blocks=media_blocks,
                            media_store=getattr(host, "media_store", None),
                        )
                        if followup is not None:
                            messages.append(followup)
                continue

            # Option F (ADR-0021): detect empty-stop before treating as text reply.
            # Empty-stop = finish_reason="stop", content empty, no tool calls.
            # This is a provider-level glitch (observed at ~50% rate with weak
            # models — B7-G12 measurement).  Reyn does NOT retry, change context,
            # or switch models.  Responsibility: observe + surface to user.
            #
            # B42-NF-W6-1: when a continuation directive is configured AND the
            # ``REYN_EMPTY_STOP_RETRY`` env var opts in, attempt ONE retry per
            # turn with the directive appended as a synthetic user message
            # before re-entering the loop. The retry path matches the
            # Anthropic-recommended "continuation prompts as last resort"
            # pattern (= platform.claude.com handling-stop-reasons docs) and
            # the Hermes-agent #9400 community implementation. Without the
            # env var, behaviour is unchanged from the original "observe +
            # surface" policy.
            if _is_empty_router_response(result):
                # P6: emit audit event — state change must be observable.
                self.host.events.emit(
                    "router_empty_response_detected",
                    finish_reason=result.finish_reason,
                    completion_tokens=getattr(result.usage, "completion_tokens", 0)
                    if result.usage else 0,
                    prompt_tokens=getattr(result.usage, "prompt_tokens", 0)
                    if result.usage else 0,
                    caller_hint="router",
                    model=host.resolve_model(self.router_model),
                )
                # B42-NF-W6-1 detect-and-retry: env-var-gated, directive-gated,
                # max 1 retry per turn. Trace-patch-replay verified 0/10 →
                # 10/10 narration recovery on the W6-S1 plan-step empty stop.
                if (
                    self._empty_stop_retry_directive
                    and _empty_stop_retries < 1
                    and os.environ.get("REYN_EMPTY_STOP_RETRY") == "1"
                ):
                    _empty_stop_retries += 1
                    messages.append({
                        "role": "user",
                        "content": self._empty_stop_retry_directive,
                    })
                    self.host.events.emit(
                        "router_empty_response_retry_injected",
                        directive_length=len(self._empty_stop_retry_directive),
                        chain_id=self.chain_id,
                    )
                    continue  # re-enter the loop with the directive in messages
                lang = getattr(host, "output_language", None)
                failure_text = _EMPTY_RESPONSE_MSG.get(
                    lang, _EMPTY_RESPONSE_MSG["en"]
                )
                await host.put_outbox(
                    kind="agent",
                    text=failure_text,
                    meta={
                        "chain_id": self.chain_id,
                        "source": "router_empty_response",
                    },
                )
                return self._total_usage  # no retry

            # Text reply — emit and stop
            # B28-Q2 Case A: emit chat_turn_completed_inline when no catalog
            # dispatch happened in this turn (= routing_decided never fired).
            # Mutually exclusive with routing_decided per turn (P6 audit).
            if _univ_enabled and not _routing_decided_fired:
                host.events.emit(
                    "chat_turn_completed_inline",
                    chain_id=self.chain_id,
                    decision="inline_reply",
                    tool_calls_attempted=_tool_calls_attempted,
                )
            await self.host.put_outbox(
                kind="agent",
                text=result.content or "",
                meta={"chain_id": self.chain_id},
            )
            return self._total_usage

        # max_iterations exhausted
        await self.host.put_outbox(
            kind="error",
            text=f"Router loop exceeded max iterations ({self.max_iterations}).",
            meta={"chain_id": self.chain_id},
        )
        return self._total_usage

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    def _dedupe_tool_calls_round(self, tool_calls: list[dict]) -> list[dict]:
        """Dedupe duplicate tool_calls within the same round (F5 + G3).

        Covers two categories of duplicates that weak models emit:

        1. Async tools (e.g. `delegate_to_agent`) — F5 fix (batch 1).
           Duplicates would inbox_put the same request twice, doubling
           peer cost and confusing the chain.

        2. `invoke_skill` — G3 fix (batch 5 B5-M1).
           Three identical invoke_skill calls in one round caused 333k
           tokens / 51 LLM calls. Same skill + same args → same result;
           only the first call is needed.

        Keyed on (tool_name, arguments_json). The original tool_call_id
        is preserved for the kept copy so the assistant/tool message
        alignment downstream stays intact.

        Emits a `tool_call_deduped` audit event per suppressed call.
        """
        # Tools that are dedupe candidates: async tools (by dispatch kind)
        # and invoke_skill (sync but non-idempotent from a cost standpoint).
        # Other sync tools (describe_skill, list_skills, read_file, …) are
        # deliberately excluded — dupes there are wasteful but
        # correctness-preserving and the tool_call_id count must stay
        # consistent with what the LLM emitted.
        _DEDUPE_SYNC_TOOLS: frozenset[str] = frozenset({"invoke_skill"})

        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for tc in tool_calls:
            name = tc["function"]["name"]
            is_async = get_dispatch_kind(name) == "async"
            is_dedupe_sync = name in _DEDUPE_SYNC_TOOLS
            if is_async or is_dedupe_sync:
                key = (name, tc["function"].get("arguments", ""))
                if key in seen:
                    reason = (
                        "duplicate_async_in_round"
                        if is_async
                        else "duplicate_invoke_skill_in_round"
                    )
                    self.host.events.emit(
                        "tool_call_deduped",
                        name=name,
                        chain_id=self.chain_id,
                        reason=reason,
                    )
                    continue
                seen.add(key)
            deduped.append(tc)
        return deduped

    # Keep backward-compat alias (tests and callers that reference the old name
    # will still work; the alias delegates to the unified implementation).
    _dedupe_async_tool_calls = _dedupe_tool_calls_round

    async def _execute_tool(self, tc: dict) -> dict:
        """Dispatch one tool call via dispatch_tool (cross-cutting concerns).

        Returns the tool_result content (will be JSON-serialized into the
        next round's messages).

        Issue #229 fallback (= ARS-only direct call salvage):
        weak LLMs sometimes read a qualified name from the ARS block
        inside ``invoke_action.description`` and emit it as a direct
        ``function_call`` rather than wrapping with
        ``invoke_action(action_name=..., args=...)``.  The name lands
        in ``self._catalog`` only when an actual hot-list alias was
        surfaced; ARS-only entries don't get a top-level tool slot, so
        the dispatcher would otherwise reject with ``unknown_tool``.
        When the missed name resolves through ``universal_dispatch``,
        rewrite the call as ``invoke_action(action_name=name, args=args)``
        and dispatch via the wrapper path so the user-visible behavior
        matches what the LLM intended.
        """
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

        if name not in self._catalog and "__" in name:
            name, args = self._maybe_salvage_qualified_direct_call(name, args)

        dctx = DispatchContext(
            caller_kind="router",
            caller_id=self.host.agent_name,
            chain_id=self.chain_id,
            tool_catalog=self._catalog,
            events=self.host.events,
        )

        return await dispatch_tool(
            name=name,
            args=args,
            ctx=dctx,
            invoker=functools.partial(self._invoke_router_tool, name),
        )

    def _maybe_salvage_qualified_direct_call(
        self, name: str, args: dict,
    ) -> tuple[str, dict]:
        """Issue #229: rewrite an ARS-only direct call into invoke_action.

        Triggered when the LLM emitted ``<category>__<entry>`` as a
        direct function call but the name isn't in ``self._catalog``
        (= it wasn't a hot-list alias, only an ARS schema-hint entry).
        Returns ``(name, args)`` unchanged when ``universal_dispatch``
        cannot resolve the qualified name — the original ``unknown_tool``
        rejection path then surfaces the error normally.

        Audit event ``direct_alias_call_salvaged`` records the rewrite
        so we can count how often this fires in dogfood and inform
        whether the ARS block wording fix (= ``β`` in #229) reduces
        the rate over time.
        """
        try:
            from reyn.tools.universal_dispatch import (
                UnknownActionError,
                resolve_invoke_action,
            )
        except Exception:  # noqa: BLE001
            return name, args
        try:
            resolve_invoke_action(name, args or {})
        except UnknownActionError:
            return name, args
        except Exception:  # noqa: BLE001 — never crash the dispatch on a salvage attempt
            return name, args
        rewritten_args = {"action_name": name, "args": dict(args or {})}
        try:
            self.host.events.emit(
                "direct_alias_call_salvaged",
                original_name=name,
                rewritten_to="invoke_action",
                chain_id=self.chain_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return "invoke_action", rewritten_args

    # Capabilities dispatched via the unified ToolRegistry (ADR-0026 M4 Phase 3
    # step 2). Their handlers in `src/reyn/tools/` delegate via typed
    # RouterCallerState callable fields populated by ``_build_router_caller_state``.
    # Tools NOT in this set fall through to the legacy if/elif tree below; the
    # set expands cluster-by-cluster as Phase 3.5 lands the remaining adapters.
    _REGISTRY_DISPATCH_TOOLS: frozenset[str] = frozenset({
        # Phase 3 step 2 (commit 649a426)
        "list_skills", "describe_skill", "list_agents", "describe_agent",
        "delegate_to_agent", "plan",
        # Phase 3.5-D — zero-diff handlers (reyn_src + web).
        # reyn_src handlers are literal copies of RouterHostAdapter helpers;
        # web handlers delegate to op_runtime.web with a synthesized OpContext
        # that the read-only handlers don't consult (= behavior preserved).
        "reyn_src_list", "reyn_src_read",
        "web_search", "web_fetch",
        # Phase 3.5-A+C — file cluster.  Handlers consume
        # RouterCallerState.op_context_factory (= host.make_router_op_context)
        # so op_runtime sees the operator-declared PermissionDecl /
        # Workspace, matching legacy router-branch behavior.
        # _normalise_router_tool_result unwraps read_file / list_directory
        # to the bare-content / bare-list shapes the host adapter returned.
        "read_file", "write_file", "delete_file", "list_directory",
        # Phase 3.5-B-light — invoke_skill.  Handler delegates via
        # RouterCallerState.run_skill_fn (= chain_id pre-bound) so PR14
        # multi-hop chain semantics propagate into nested run_skill /
        # delegate_to_agent paths.  Defense Layer B (skill-name
        # validation) is applied inside the handler.
        "invoke_skill",
        # Phase 3.5-B-mid — mcp cluster.  Handlers access the
        # RouterHostAdapter via ctx.router_state.host so the session-
        # level MCPClient cache is preserved (= no per-call re-handshake
        # when the LLM repeatedly calls list_mcp_tools / call_mcp_tool).
        # _normalise_router_tool_result unwraps list_mcp_servers and
        # list_mcp_tools dict envelopes back to bare list shape.
        "list_mcp_servers", "list_mcp_tools", "call_mcp_tool", "describe_mcp_tool",
        # Phase 3.5-B-heavy — memory cluster.  Handlers delegate via
        # RouterCallerState.{list_memory_fn, read_memory_body_fn,
        # remember_fn, forget_fn} bound to RouterLoop's private helpers
        # which consume the agent-aware ``host.get_memory_index()`` /
        # ``host.memory_path`` paths.  This preserves per-agent memory
        # privacy that the registry handlers' filesystem-direct fallback
        # cannot guarantee.
        "list_memory", "read_memory_body",
        "remember_shared", "remember_agent", "forget_memory",
        # H1/H2: RAG tools (ADR-0033 Phase 1, B17-S6-1 / B17-S8-2 fix).
        # Handlers in src/reyn/tools/recall.py and src/reyn/tools/drop_source.py
        # delegate to op_runtime.recall / op_runtime.index_drop via execute_op.
        # OpContext is constructed from ctx.router_state.op_context_factory
        # (= host.make_router_op_context) so the permission resolver and
        # intervention_bus are present for the index_drop gate.
        "recall", "drop_source",
        # FP-0034 Phase 1: universal catalog wrappers.  Handlers in
        # src/reyn/tools/universal_catalog.py — list_actions enumerates
        # via ctx.router_state, describe_action / invoke_action route
        # via universal_dispatch.  search_actions stays included for
        # registry-completeness even though router_tools.build_tools
        # currently excludes it from the LLM-visible tools= list
        # (= Phase 2 wires the §D14 visibility gate + the real handler;
        # listing it here is harmless because the catalog already
        # filters it out before the LLM can call it).
        "list_actions", "search_actions",
        "describe_action", "invoke_action",
    })

    async def _build_action_embedding_index_background(
        self, idx: Any, provider: Any, model_class: str,
    ) -> None:
        """FP-0034 Phase 2 step 1: background ActionEmbeddingIndex build.

        Enumerates the catalog via ``LIST_ACTIONS`` against a fresh
        ``RouterCallerState`` snapshot and feeds the items into
        ``idx.build()``.  The build is idempotent (= same catalog
        hash skipped) and serialised by the index's internal lock,
        so concurrent calls are safe.

        Errors are swallowed and logged via ``host.events`` so a
        misconfigured embedding provider does not crash the chat
        session — the next turn finds ``is_ready()`` False and
        keeps ``search_actions`` hidden.
        """
        from reyn.tools import get_default_registry
        from reyn.tools.types import ToolContext
        try:
            rs = await self._build_router_caller_state()
            tool_ctx = ToolContext(
                events=self.host.events,
                permission_resolver=getattr(self.host, "permission_resolver", None),
                workspace=getattr(self.host, "workspace", None),
                caller_kind="router",
                router_state=rs,
            )
            list_actions_def = get_default_registry().lookup("list_actions")
            if list_actions_def is None:
                return
            result = await list_actions_def.handler({}, tool_ctx)
            items = result.get("items", []) if isinstance(result, dict) else []
            await idx.build(items, provider, model_class)
        except Exception as exc:
            try:
                self.host.events.emit(
                    "action_index_build_failed",
                    error=repr(exc),
                    model_class=model_class,
                )
            except Exception:
                pass

    async def _build_router_caller_state(self) -> Any:
        """Build a RouterCallerState populated with bound callbacks.

        Bindings follow the wiring contract documented in
        ``reyn.tools.types.RouterCallerState``:

        * Catalog ``_fn`` callables wrap RouterLoop's private helpers
          (``_list_skills`` / ``_describe_skill`` / ``_list_agents`` /
          ``_describe_agent``) so the registry handlers stay decoupled from
          RouterLoopHost type.
        * ``send_to_agent`` is bound with ``chain_id`` and ``depth=0`` at
          population time so the delegate handler signature stays pure
          ``(to, request)``.
        * ``dispatch_plan_tool`` is bound with ``parent_host`` / ``chain_id``
          / ``budget`` / ``router_model`` / ``available_tool_names``; the
          plan handler passes only ``args``.

        Forward-looking fields (``available_skills`` / ``available_agents``
        for schema enrichment, identity / cost / model context) are also
        populated so future handler activations have what they need.
        """
        from reyn.tools.types import RouterCallerState

        async def _send_to_agent_bound(*, to: str, request: str) -> None:
            await self.host.send_to_agent(
                to=to, request=request, depth=0, chain_id=self.chain_id,
            )

        async def _run_skill_bound(*, skill: str, input: dict) -> Any:
            return await self.host.run_skill_awaitable(
                skill=skill, input=input, chain_id=self.chain_id,
            )

        # FP-0012: non-blocking spawn binding. Only chat-mode hosts
        # implement ``spawn_skill``; plan-mode ``_PlanStepHost`` does not,
        # so the hasattr check leaves this None and invoke_skill falls
        # back to run_skill_fn (= blocking) inside plan steps.
        _spawn_skill_bound: Any = None
        if hasattr(self.host, "spawn_skill") and callable(
            getattr(self.host, "spawn_skill", None)
        ):
            async def _spawn_skill_bound_impl(*, skill: str, input: dict) -> Any:
                return await self.host.spawn_skill(
                    skill=skill, input=input, chain_id=self.chain_id,
                )
            _spawn_skill_bound = _spawn_skill_bound_impl

        async def _dispatch_plan_bound(*, args: dict) -> Any:
            from reyn.chat.planner import dispatch_plan_tool
            return await dispatch_plan_tool(
                args=args,
                parent_host=self.host,
                chain_id=self.chain_id,
                budget=self.budget,
                router_model=self.router_model,
                available_tool_names=set(self._tool_names) - {"plan"},
            )

        # FP-0034 Phase 2 prep: snapshot indexed RAG corpora for the
        # universal catalog's rag.corpus enumeration. SourceManifest
        # caches the parsed YAML in-process so this is O(1) when the
        # cache is warm (= when the system-prompt path already loaded
        # the manifest earlier in this turn). Failures (= missing file,
        # malformed YAML) degrade to an empty list — the catalog
        # handler then reports zero corpora rather than crashing.
        _rag_sources: list[Mapping[str, Any]] | None = None
        try:
            _manifest = get_source_manifest(Path.cwd())
            _entries = await _manifest.get_all()
            _rag_sources = [
                {
                    "name": e.name,
                    "description": e.description,
                    "backend": e.backend,
                    "chunk_count": e.chunk_count,
                }
                for e in _entries.values()
            ]
        except Exception:
            # Manifest unavailable (= no workspace, no .reyn/index/
            # sources.yaml, transient I/O). Treat as empty catalogue
            # rather than failing the entire tool dispatch.
            _rag_sources = None

        return RouterCallerState(
            # Catalog access (= activated handlers)
            list_skills_fn=self._list_skills,
            describe_skill_fn=self._describe_skill,
            list_agents_fn=self._list_agents,
            describe_agent_fn=self._describe_agent,
            available_skills=list(self.host.list_available_skills()),
            available_agents=list(self.host.list_available_agents()),
            # Async dispatch (= activated handlers)
            send_to_agent=_send_to_agent_bound,
            dispatch_plan_tool=_dispatch_plan_bound,
            # Skill invocation bridge (= for invoke_skill handler;
            # Phase 3.5-B-light) — chain_id pre-bound to preserve PR14
            # multi-hop chain semantics. Plan-mode keeps using this for
            # blocking step execution; chat-mode prefers spawn_skill_fn.
            run_skill_fn=_run_skill_bound,
            # FP-0012: non-blocking spawn binding for chat-mode invoke_skill.
            # None for plan-mode hosts (= no spawn_skill method on host).
            spawn_skill_fn=_spawn_skill_bound,
            # Memory tool bridges (= for memory cluster handlers;
            # Phase 3.5-B-heavy) — bound to RouterLoop's private helpers
            # so registry handlers consume the same agent-aware
            # ``host.get_memory_index()`` / ``host.memory_path`` /
            # ``host.file_*`` paths the legacy router branches used.
            list_memory_fn=self._list_memory,
            read_memory_body_fn=self._read_memory_body,
            remember_fn=self._remember,
            forget_fn=self._forget,
            # Identity + cost + model context (forward-looking; consumed by
            # schema_enricher hooks and future activated handlers)
            chain_id=self.chain_id,
            budget=self.budget,
            router_model=self.router_model,
            available_tool_names=list(self._tool_names),
            # OpContext factory (= for file / mcp / web handlers; Phase 3.5-A+C).
            # ``getattr`` fallback keeps test stubs (= FakeRouterHost without
            # the method) compatible — the handler then uses its minimal
            # synthesis path, which is fine for tests that don't exercise
            # permission gating.
            op_context_factory=getattr(self.host, "make_router_op_context", None),
            # Host duck-type (= for mcp handlers; Phase 3.5-B-mid).  MCP
            # handlers call ``host.mcp_list_servers / mcp_list_tools /
            # mcp_call_tool`` directly to preserve the session-level
            # MCPClient cache (= no per-call re-handshake).
            host=self.host,
            # FP-0034 Phase 2 prep: rag.corpus enumeration snapshot.
            available_rag_sources=_rag_sources,
            # FP-0034 Phase 2 step 1: search_actions wiring.  All
            # three resolve via getattr fallback so narrow hosts
            # (= plan-step host, FakeRouterHost) get None and
            # search_actions degrades gracefully.
            action_embedding_index=(
                getattr(self.host, "get_action_embedding_index", lambda: None)()
            ),
            embedding_provider=(
                getattr(self.host, "get_embedding_provider", lambda: None)()
            ),
            embedding_model_class=(
                getattr(self.host, "get_embedding_model_class", lambda: None)()
            ),
            # FP-0034 Phase 2: sandbox backend name for exec D14 gate.
            # getattr fallback so narrow hosts (= FakeRouterHost, plan-step
            # host) without this method default to None, hiding exec category.
            sandbox_backend=(
                getattr(self.host, "get_sandbox_backend", lambda: None)()
            ),
            # FP-0032 follow-up: mcp_servers must be populated so the
            # universal_catalog ``mcp.server`` / ``mcp.tool`` category
            # enumerations surface the actually-configured servers.
            # Without this the enumeration sees ``rs.mcp_servers is None``
            # and returns [], leaving ``list_actions(category="mcp.server")``
            # silently empty even when ``reyn mcp list`` shows servers.
            # Shape: list[Mapping[name, description, tools?]] from host.
            mcp_servers=self.host.get_mcp_servers() if hasattr(
                self.host, "get_mcp_servers"
            ) else None,
        )

    async def _invoke_via_registry(self, name: str, args: dict) -> Any:
        """Dispatch a tool through the unified ToolRegistry handler.

        Builds a ToolContext with a populated RouterCallerState and calls
        the canonical handler from ``src/reyn/tools/<name>.py``. Wrapped
        externally by ``dispatch_tool`` (= same cross-cutting events /
        validation / error envelope as the legacy invoker path).

        Some handlers return raw op_runtime dict envelopes whereas the
        legacy router branches returned extracted shapes (= the host
        adapter did the extraction).  ``_normalise_router_tool_result``
        replicates that extraction so byte-identity with prior LLM-visible
        output is preserved (= refactor-only migration, no external spec
        change).
        """
        from reyn.tools import get_default_registry
        from reyn.tools.dispatch import invoke_tool
        from reyn.tools.types import ToolContext

        rs = await self._build_router_caller_state()
        tool_ctx = ToolContext(
            events=self.host.events,
            permission_resolver=getattr(self.host, "permission_resolver", None),
            workspace=getattr(self.host, "workspace", None),
            caller_kind="router",
            router_state=rs,
        )
        result = await invoke_tool(get_default_registry(), name, args, tool_ctx)
        return self._normalise_router_tool_result(name, result)

    @staticmethod
    def _normalise_router_tool_result(name: str, result: Any) -> Any:
        """Match registry-handler output to the legacy router-branch shape.

        File handlers in ``src/reyn/tools/file.py`` return raw op_runtime
        dict envelopes (e.g. ``{"kind": "file", "op": "read", "status":
        "ok", "content": "..."}``) but the legacy router path
        (RouterHostAdapter.file_read / file_list_directory) extracted
        ``content`` / ``entries`` before returning so the LLM saw a
        bare string / list. This helper applies the same extraction so
        registry dispatch is LLM-visible-identical to the prior path.
        """
        import json
        if name == "read_file":
            if isinstance(result, dict):
                if "content" in result:
                    return result["content"]
                return json.dumps(result)
            return result
        if name == "list_directory":
            if isinstance(result, dict):
                return result.get("entries", [result])
            return result
        if name == "list_mcp_servers":
            if isinstance(result, dict) and "servers" in result:
                return result["servers"]
            return result
        if name == "list_mcp_tools":
            # FP-0032: key renamed from "tools" to "mcp_tools"; handle both
            # for backward-compat during transition.
            if isinstance(result, dict):
                if "mcp_tools" in result:
                    return result["mcp_tools"]
                if "tools" in result:
                    return result["tools"]
            return result
        if name == "describe_mcp_tool":
            # Return the full dict (= {name, description, input_schema}).
            # No unwrapping needed — the LLM sees it as structured data.
            return result
        return result

    async def _invoke_router_tool(self, name: str, args: dict) -> Any:
        """Execute a validated tool call by name.

        Called by dispatch_tool after name/args validation. Tools in
        ``_REGISTRY_DISPATCH_TOOLS`` go through the unified registry path
        (= ADR-0026); the rest fall through to the legacy if/elif tree
        until Phase 3.5 ports their handlers.
        """
        # ADR-0026 M4 Phase 3 step 2 — registry dispatch for activated tools
        if name in self._REGISTRY_DISPATCH_TOOLS:
            return await self._invoke_via_registry(name, args)

        # All router tool clusters are now dispatched via the unified
        # registry — see ``_REGISTRY_DISPATCH_TOOLS`` at the top of this
        # method.  Phase 3 step 2 + Phase 3.5-D / A+C / B-light / B-mid /
        # B-heavy migrations land here; the legacy if/elif tree was
        # retained only for clusters whose adapter design needed
        # per-tool review.  When that review surfaces a new cluster /
        # capability not yet in the dispatch set, the new branch lands
        # here as the legacy stop-gap until the adapter migrates.

        # FP-0034 Phase 2 step 5: hot list direct alias dispatch.
        # Qualified names (containing "__") not in the static dispatch set
        # are hot list aliases. Route them as invoke_action so the catalog
        # dispatch handles them correctly.
        if "__" in name:
            return await self._invoke_via_registry(
                "invoke_action",
                {"action_name": name, "args": args or {}},
            )

        # Should not be reached if catalog is correct — dispatch_tool already
        # validated name is in catalog. Return error for safety.
        return {"error": f"unhandled tool: {name}"}

    # -----------------------------------------------------------------------
    # FP-0024-A: BM25 pre-filter
    # -----------------------------------------------------------------------

    def _apply_skill_search(
        self, all_skills: list[dict], query: str
    ) -> list[dict]:
        """Return the skills list to pass to build_tools.

        When the catalogue exceeds ``skill_search_config.threshold``, run
        BM25 keyword search against name + description and keep only the
        top-K candidates.  Emits a ``skill_search_invoked`` event (P6 audit)
        on every BM25 dispatch.

        Fallback safety: if BM25 returns 0 results (no keyword overlap),
        return the full catalogue unchanged so no skill becomes invisible.

        Below threshold: returns all_skills unchanged (no BM25, no event).
        """
        from reyn.config import SkillSearchConfig  # local import to break circular

        cfg: SkillSearchConfig = (
            self._skill_search_config
            if self._skill_search_config is not None
            else SkillSearchConfig()
        )

        if len(all_skills) <= cfg.threshold:
            return all_skills

        backend = BM25Backend(all_skills)
        candidates = backend.search(query, top_k=cfg.top_k)

        # P6: emit audit event for every BM25 dispatch.
        self.host.events.emit(
            "skill_search_invoked",
            query=query,
            candidates_count=len(candidates),
            top_k=cfg.top_k,
        )

        if not candidates:
            # 0 results → full enum fall-through (no skill invisibility).
            return all_skills

        candidate_names = {c.name for c in candidates}
        return [s for s in all_skills if s.get("name") in candidate_names]

    # -----------------------------------------------------------------------
    # Discovery helpers (pure, no async host calls)
    # -----------------------------------------------------------------------

    @staticmethod
    def _skill_item(s: dict) -> dict:
        """Build a list_skills item from a catalogue entry.

        Always includes ``name`` and ``description``. Passes through
        ``input_artifact`` and ``input_fields`` when present so the LLM
        sees the correct input field names before calling ``invoke_skill``
        (RETRO-H2 fix — plan D: pre-call structural context provision).

        Description is truncated to MAX_DESC_LEN_FOR_LISTING chars + "..."
        to mitigate the G12 empty-stop attractor (B7 finding: skill
        description verbosity triggers the attractor — a62a9dad / a947255e).
        describe_skill returns the full description (details on demand).
        """
        raw_desc = s.get("description", "")
        if len(raw_desc) > MAX_DESC_LEN_FOR_LISTING:
            desc = raw_desc[:MAX_DESC_LEN_FOR_LISTING] + "..."
        else:
            desc = raw_desc
        item: dict = {
            "name": s["name"],
            "description": desc,
        }
        if "input_artifact" in s:
            item["input_artifact"] = s["input_artifact"]
        if "input_fields" in s:
            item["input_fields"] = s["input_fields"]
        return item

    def _list_skills(self, path: str) -> list[dict]:
        """Browse skill catalogue hierarchically.

        path == "" → group by category, return [{category, count, sample_names}, ...]
        path == "<category>" → return [{name, description, input_artifact?, input_fields?}, ...]

        The ``sample_names`` preview (up to 5 names per category) was added to
        defuse a G12 empty-stop attractor: with only ``count`` in the
        response, the LLM had nothing concrete to narrate when answering
        "list available skills" and exited with ``finish=stop`` /
        ``content=""``. Surfacing actual skill names gives it material to
        speak with — confirmed via dogfood trace re-run.
        """
        skills = self.host.list_available_skills()

        if not path:
            # Group by category, with a small sample of names per category to
            # defuse the empty-stop attractor on path="".
            categories: dict[str, list[dict]] = {}
            for skill in skills:
                cat = skill.get("category") or "general"
                categories.setdefault(cat, []).append(skill)
            return [
                {
                    "category": cat,
                    "count": len(items),
                    # Up to 5 names per category — concrete enough to narrate,
                    # bounded enough to stay under the verbosity attractor
                    # threshold for projects with hundreds of skills.
                    "sample_names": [s.get("name", "") for s in items[:5]],
                }
                for cat, items in sorted(categories.items())
            ]

        # Return skills in the given category
        by_category = [
            self._skill_item(s)
            for s in skills
            if (s.get("category") or "general") == path
        ]
        if by_category:
            return by_category

        # path didn't match any category — try as a skill name (fallback)
        by_name = [
            self._skill_item(s)
            for s in skills
            if s.get("name") == path
        ]
        return by_name

    def _describe_skill(self, name: str) -> dict:
        """Return router-optimised entry for one skill, or error dict.

        Returns the catalogue entry with internal-only fields stripped
        (``routing``, ``category``) to keep the tool_response concise.
        The ``routing`` block (when_to_use / when_not_to_use / examples)
        averages 800–1400 chars and triggers the G12 P-b verbosity attractor
        when included in the describe_skill tool_response (Pattern D —
        B11-R2 diagnosis: 20% → 0% empty-stop after stripping).

        ``name``, ``description``, ``input_artifact``, and ``input_fields``
        are preserved — they are all the router needs to build a valid
        invoke_skill call.  (P7-clean: filtering uses OS-level field names
        in ``_DESCRIBE_SKILL_STRIP_FIELDS``, not any skill-specific strings.)
        """
        for skill in self.host.list_available_skills():
            if skill.get("name") == name:
                return {k: v for k, v in skill.items() if k not in _DESCRIBE_SKILL_STRIP_FIELDS}
        return {"error": f"skill not found: {name}"}

    def _list_agents(self, path: str) -> list[dict]:
        """Browse agent catalogue hierarchically.

        path == "" → group by cluster, return [{cluster, count}, ...]
        path == "<cluster>" → return [{name, role}, ...] for that cluster
        """
        agents = self.host.list_available_agents()

        if not path:
            clusters: dict[str, list[dict]] = {}
            for agent in agents:
                cluster = agent.get("cluster") or "default"
                clusters.setdefault(cluster, []).append(agent)
            return [
                {"cluster": cluster, "count": len(items)}
                for cluster, items in sorted(clusters.items())
            ]

        return [
            {"name": a["name"], "role": a.get("role", "")}
            for a in agents
            if (a.get("cluster") or "default") == path
        ]

    def _describe_agent(self, name: str) -> dict:
        """Return full entry for one agent, or error dict."""
        for agent in self.host.list_available_agents():
            if agent.get("name") == name:
                return agent
        return {"error": f"agent not found: {name}"}

    def _list_memory(self, path: str) -> list[dict]:
        """Browse memory hierarchically.

        path == "" → [{path: "shared", count: N}, {path: "agent", count: M}]
        path == "shared" or "agent" → sub-type counts
        path == "shared/<type>" or "agent/<type>" → items in that layer+type
        """
        memory_index = self.host.get_memory_index()
        content = memory_index.get("content", "") if memory_index.get("status") == "ok" else ""

        if not path:
            shared_count = self._count_memory_layer(content, "shared")
            agent_count = self._count_memory_layer(content, "agent")
            return [
                {"path": "shared", "count": shared_count},
                {"path": "agent", "count": agent_count},
            ]

        parts = path.split("/", 1)
        layer = parts[0]  # "shared" or "agent"

        if len(parts) == 1:
            # Return sub-categories (types) for this layer
            type_counts = self._count_memory_types(content, layer)
            return [
                {"path": f"{layer}/{mtype}", "count": count}
                for mtype, count in sorted(type_counts.items())
                if count > 0
            ]

        # path == "shared/user" etc. → return items matching layer + type
        mtype = parts[1]
        return self._list_memory_items(content, layer, mtype)

    def _count_memory_layer(self, content: str, layer: str) -> int:
        """Count total entries in the given memory layer from index content."""
        import re
        total = 0
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        slug_re = re.compile(r"\(([^)]+)\.md\)")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if in_layer:
                for _ in slug_re.finditer(line):
                    total += 1
        return total

    def _count_memory_types(self, content: str, layer: str) -> dict[str, int]:
        """Return {type: count} for a given layer."""
        import re
        counts: dict[str, int] = {}
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        slug_re = re.compile(r"\(([^)]+)\.md\)")
        type_re = re.compile(r"^(user|feedback|project|reference)_")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if in_layer:
                for slug_m in slug_re.finditer(line):
                    slug = slug_m.group(1)
                    tm = type_re.match(slug)
                    if tm:
                        mtype = tm.group(1)
                        counts[mtype] = counts.get(mtype, 0) + 1
        return counts

    def _list_memory_items(
        self, content: str, layer: str, mtype: str
    ) -> list[dict]:
        """Return [{slug, name, description}, ...] for layer+type."""
        import re
        items: list[dict] = []
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        # Match "- [Name](slug.md) — description" or table rows
        entry_re = re.compile(
            r"\[([^\]]+)\]\(([^)]+)\.md\)(?:\s*[—–-]+\s*(.+))?"
        )
        type_re = re.compile(r"^(user|feedback|project|reference)_")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if not in_layer:
                continue
            for em in entry_re.finditer(line):
                name = em.group(1)
                slug = em.group(2)
                desc = (em.group(3) or "").strip()
                tm = type_re.match(slug)
                if tm and tm.group(1) == mtype:
                    items.append({"slug": slug, "name": name, "description": desc})
        return items

    async def _read_memory_body(self, layer: str, slug: str) -> dict:
        """Read the full body of a memory entry.

        Memory files are stored as Markdown with a YAML frontmatter (= the
        ``name`` / ``description`` / ``type`` metadata fields written by
        ``_remember``). Returning the full file content with the frontmatter
        intact triggered a G12 empty-stop attractor: when the LLM asked
        ``read_memory_body`` and got back e.g.::

            ---
            name: User Name
            description: User Name
            type: user
            ---

            Yasuda

        it sometimes parsed the frontmatter as the content and exited with
        ``finish=stop`` / ``content=""`` instead of narrating "Yasuda".
        Confirmed via dogfood trace on Q10 ``who am I?`` — the recall
        returned the body with frontmatter and produced an empty reply.

        Stripping the frontmatter before returning gives the LLM clean
        text to narrate. The metadata fields are not LLM-actionable here
        (they were emitted at write time and are surfaced separately via
        ``list_memory``), so dropping them costs nothing.
        """
        path = self.host.memory_path(layer, slug)
        try:
            content = await self.host.file_read(path)
            return {
                "content": _strip_frontmatter(content),
                "layer": layer,
                "slug": slug,
            }
        except Exception as exc:
            return {"error": str(exc), "layer": layer, "slug": slug}

    async def _remember(
        self,
        *,
        layer: str,
        slug: str,
        name: str,
        description: str,
        type: str,
        body: str,
    ) -> dict:
        """Write a memory entry and regenerate the index."""
        # Defensive: strip trailing .md if LLM emitted it in slug despite
        # the tool description saying "Filename stem".
        if slug.endswith(".md"):
            slug = slug[:-3]
        frontmatter = (
            f"---\nname: {name}\ndescription: {description}\ntype: {type}\n---\n\n{body}\n"
        )
        # memory_path appends .md itself — pass bare slug.
        file_path = self.host.memory_path(layer, slug)
        await self.host.file_write(file_path, frontmatter)

        mem_dir = self.host.memory_dir(layer)
        index_path = mem_dir + "/MEMORY.md"
        await self.host.file_regenerate_index(
            mem_dir,
            index_path,
            "- [{name}]({slug}.md) — {description}",
            "# Memory Index\n\n",
        )
        return {"saved": slug, "layer": layer}

    async def _forget(self, layer: str, slug: str) -> dict:
        """Delete a memory entry and regenerate the index."""
        # Defensive: strip trailing .md if LLM emitted it.
        if slug.endswith(".md"):
            slug = slug[:-3]
        # memory_path appends .md itself.
        file_path = self.host.memory_path(layer, slug)
        await self.host.file_delete(file_path)

        mem_dir = self.host.memory_dir(layer)
        index_path = mem_dir + "/MEMORY.md"
        await self.host.file_regenerate_index(
            mem_dir,
            index_path,
            "- [{name}]({slug}.md) — {description}",
            "# Memory Index\n\n",
        )
        return {"deleted": slug, "layer": layer}
