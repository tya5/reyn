"""Build the tools= argument for the native tool_use router loop (PR35).

Public API
----------
build_tools(available_skills, available_agents, *, file_permissions, mcp_servers)
    Returns 14–23 tools in fixed order for litellm.acompletion.

Gemini-safe schema rules enforced throughout:
- No oneOf / anyOf / additionalProperties / format keys
- Nested objects max 1 level (input: object / args: object are untyped)
- enum values are strings only
- Tool order is a literal list — deterministic regardless of dict iteration order

Migration note (ADR-0026)
-------------------------
This file is an M1 adapter shim. The ToolSpec pattern defined here will be
progressively replaced as capabilities migrate to ToolDefinition instances in
src/reyn/tools/ during M2/M3. The public surface (build_tools() returning
list[dict]) is preserved unchanged throughout migration; M4 cleanup removes
the ToolSpec literals once all capabilities have migrated.

A private helper _build_tools_via_registry(registry) is available for M2/M3
integration; build_tools() itself remains the public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── FP-0024 Component D — Anthropic tool_search_tool threshold ───────────────
#
# When the number of MCP tools at or above this threshold, build_tools()
# replaces inline MCP tool schemas with a single tool_search_tool meta-tool
# (Anthropic GA 2025-11, ``type: "tool_search_tool_20251101"``).  The LLM
# queries the meta-tool to load only the 3–5 most relevant MCP tools on
# demand, rather than receiving all N schemas upfront.
#
# Spring AI experiment shows 63–64% token reduction for 40+ MCP tools.
# Override per-project via ``mcp.search_threshold:`` in reyn.yaml (parsed by
# config._parse_mcp_search_threshold; injected into build_tools() as kwarg).
# Setting threshold=0 disables the switch (always inline).
#
# The exact Anthropic ``tool_search_tool`` API spec as of 2025-11:
#   {
#     "type": "tool_search_tool_20251101",
#     "name": "tool_search",          # the name the LLM calls
#     "max_results": int,             # max tools returned per query (1–10)
#     "tools": [                      # the deferred tool list
#       {... standard tool schema with "cache_control": ... ...}
#     ]
#   }
# TODO(fp-0024-d): verify exact ``type`` string and ``tools`` element schema
# against Anthropic SDK release notes when the SDK is available in this env.
# The ``type`` value "tool_search_tool_20251101" is the version identifier
# confirmed in the Anthropic docs reference for the 2025-11 GA release.
MCP_SEARCH_THRESHOLD: int = 0
# FP-0032: Default 0 (always inline D1–D4). The prior value of 30 activated
# Anthropic's tool_search_tool_20251101, which is Anthropic-API-specific and
# conflicts with Reyn's provider-agnostic posture. Set > 0 via reyn.yaml
# mcp.search_threshold to opt in. Full removal of tool_search_tool is FP-0033.

# ── G12 attractor mitigation (B7 finding: skill description verbosity trigger) ──
#
# Empty-stop attractor root cause: skill description verbosity.  B7 finding
# B7-G12-context-root-cause.md (commit a62a9dad) confirmed that truncating
# descriptions to ≤80 chars in list_skills tool_response reduced empty-stop
# rate from 100% → 0% (H-b verification).  B7-G12-cross-attractor-pattern.md
# (commit a947255e) confirmed two trigger paths:
#   Pattern A: via list_skills tool_response
#   Pattern C: via system prompt inline skill list
# Both paths must truncate to the same threshold.  describe_skill returns the
# full description (details on demand — list is summary only).
MAX_DESC_LEN_FOR_LISTING: int = 80

# ── G12 attractor mitigation — describe_skill routing field strip (B11-R2) ──
#
# describe_skill returns the full catalogue entry dict.  When that dict
# includes the ``routing`` block (intents / when_to_use / when_not_to_use /
# examples), the serialised tool_response can exceed 1000 chars and triggers
# the same P-b verbosity attractor that list_skills descriptions trigger
# (Pattern D — describe_skill response verbosity).
#
# B11-R2 N-shot experiment (synthetic trace, N=10):
#   - Full routing included (~1000 chars): 2/10 empty-stop (20%)
#   - Routing stripped (~187 chars):       0/10 empty-stop (0%)
#   - invoke_skill desc truncation alone:  1/10 — not significant
#
# The ``routing`` block is decision-guidance for BEFORE the router calls
# describe_skill.  Once the LLM has issued the describe_skill call it is
# committed to that skill; the routing guidance is no longer needed and only
# adds verbosity that triggers the P-b attractor.  ``category`` is internal
# grouping metadata also redundant for invocation.
#
# P7-clean: ``routing`` and ``category`` are OS-level catalogue metadata
# fields (not skill-specific names).  Filtering applied uniformly across all
# skills (no skill-name / phase-name / artifact-name literals hardcoded).
_DESCRIBE_SKILL_STRIP_FIELDS: frozenset[str] = frozenset({"routing", "category"})


# ── ToolSpec — unified tool descriptor ──────────────────────────────────────
#
# Single source of truth for all chat-router tool metadata. Replaces the
# prior dual representation:
#   - dict literal in build_tools() (OpenAI schema)
#   - sidecar _DISPATCH_KIND dict (sync/async classification)
#
# dispatch_kind: intrinsic dispatch posture for the tool.
#   "sync"  — invoker awaits a result that's available in this RouterLoop
#             turn; the LLM sees the tool_result and decides next step.
#   "async" — invoker dispatches work whose result arrives via a separate
#             channel in a future router invocation (e.g. delegate_to_agent
#             result comes through PR14 pending_chain). The current loop
#             cannot wait for the answer; RouterLoop must exit after
#             dispatch and rely on the future invocation to resume.
#
# Future-proof for tool metadata growth (cost weight, rate-limit class,
# per-tool budget, log redaction policy). Add fields here as those needs
# surface; build_tools() and dispatcher consume from the same source.
#
# Future fields (not added yet):
#   cost_weight: float = 1.0
#   rate_limit_class: str | None = None
#   log_redaction: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class ToolSpec:
    """Unified spec for a chat-router tool exposed to the LLM.

    Replaces the prior dual representation:
      - dict literal in build_tools() (OpenAI schema)
      - sidecar _DISPATCH_KIND dict (sync/async classification)

    Future-proof for tool metadata growth (cost weight, rate-limit class,
    per-tool budget, log redaction policy). Add fields here as those needs
    surface; build_tools() and dispatcher consume from the same source.
    """

    name: str
    description: str
    parameters: dict                                  # JSON schema (object root)
    dispatch_kind: Literal["sync", "async"] = "sync"
    # Future fields (commented out, not added yet):
    # cost_weight: float = 1.0
    # rate_limit_class: str | None = None
    # log_redaction: list[str] = field(default_factory=list)

    def to_openai_dict(self) -> dict:
        """Render to the OpenAI tools array shape that LiteLLM expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ── get_dispatch_kind — registry-backed (ADR-0026 M4 Phase 4) ────────────────
#
# Sunset: the prior sidecar ``_DISPATCH_KIND`` dict / ``_TOOL_SPECS_STATIC_ASYNC``
# duplicate has been removed.  ``ToolDefinition.dispatch_kind`` on entries in
# the unified ToolRegistry is now the single source of truth; this helper
# delegates to the registry.  Default for unknown names stays ``"sync"``.


def get_dispatch_kind(tool_name: str) -> str:
    """Return ``"sync"`` or ``"async"`` for the given tool name.

    Used by RouterLoop to decide whether to continue the loop after a
    tool dispatch (sync — result is in the tool_result, LLM can act on
    it) or to exit immediately and wait for a deferred result via a
    separate channel (async — pending_chain or equivalent).

    Resolves via ``get_default_registry().lookup(tool_name).dispatch_kind``.
    Default for unknown / unregistered names is ``"sync"`` (= safe default;
    the loop continues and the LLM sees a "no such tool" error result).
    """
    from reyn.tools import get_default_registry

    tool = get_default_registry().lookup(tool_name)
    if tool is None:
        return "sync"
    return tool.dispatch_kind


def build_mcp_search_tool(mcp_tool_specs: list[dict]) -> dict:
    """Build the Anthropic tool_search_tool meta-tool for deferred MCP loading.

    Returns a single ``tool_search_tool_20251101`` descriptor that wraps the
    full MCP tool catalog.  When the LLM calls ``tool_search``, Anthropic's
    server loads only the matching subset (``max_results`` tools), dramatically
    reducing the effective schema payload for large MCP deployments.

    Parameters
    ----------
    mcp_tool_specs:
        List of standard MCP tool dicts (OpenAI schema shape) to place inside
        the ``tools`` array of the search-tool wrapper.  Each entry should
        carry at least ``name``, ``description``, and ``parameters``.

    Returns
    -------
    dict
        A tool dict in the Anthropic ``tool_search_tool_20251101`` format.
        The ``type`` field distinguishes it from ordinary ``function`` tools
        so the Anthropic API can handle deferred-loading server-side.

    TODO(fp-0024-d): Verify the exact field names (``type``, ``name``,
    ``max_results``, ``tools``) against the published Anthropic SDK release
    notes for the 2025-11 GA build of tool_search_tool.  The spec below
    follows the reference at:
      https://docs.anthropic.com/en/docs/tool-use/tool-search-tool
    and is marked best-effort until validated against a live Anthropic
    endpoint.
    """
    return {
        "type": "tool_search_tool_20251101",
        "name": "tool_search",
        "max_results": 5,
        "tools": mcp_tool_specs,
    }


def build_tools(
    available_skills: list[dict],  # [{name, description, routing?}, ...]
    available_agents: list[dict],  # [{name, role}, ...]
    *,
    file_permissions: dict | None = None,  # {"read": [paths], "write": [paths]}
    mcp_servers: list[dict] | None = None,  # [{"name": ..., "description": ...}, ...]
    web_fetch_allowed: bool = True,         # FP-0022: always-on; parameter kept for backward compat
    mcp_search_threshold: int = MCP_SEARCH_THRESHOLD,  # FP-0024: override via config
    universal_wrappers_enabled: bool = False,  # FP-0034 PR-3b-i: opt-in catalog wrappers
    hide_legacy_tools: bool = False,            # FP-0034 Phase 2 prep: exclusive-wrapper mode
    search_actions_visible: bool = False,       # FP-0034 Phase 2 step 1: D14 visibility gate
    hot_list_aliases: list[dict] | None = None,  # FP-0034 Phase 2 step 3: hot list direct aliases
) -> list[dict]:
    """Build the tools= argument for litellm.acompletion.

    Returns 14–23 tools in fixed order (Anthropic prompt cache compatibility).
    Tool order matches the plan's canonical ordering:
      A1 list_skills, A2 describe_skill, A3 list_agents, A4 describe_agent,
      A5 list_memory, A6 read_memory_body,
      B1 invoke_skill, B2 delegate_to_agent,
      B3 remember_shared, B4 remember_agent, B5 forget_memory,
      C1 list_directory, C2 read_file (when any file scope),
      C3 write_file, C4 delete_file (only when write scope),
      D1 list_mcp_servers, D2 list_mcp_tools, D3 call_mcp_tool, D4 describe_mcp_tool (when mcp configured).

    Internally collects ToolSpec objects (= single source of truth for name,
    description, parameters, dispatch_kind) and returns the OpenAI dict shape
    via ToolSpec.to_openai_dict(). The public return type stays list[dict] for
    backward compatibility with all callers.

    Parameters
    ----------
    available_skills:
        Skill catalogue entries. Each dict must have at least ``name``.
        When the list is non-empty the ``name`` field of ``invoke_skill`` gets
        an ``enum`` constraint so dispatch_tool's schema validation rejects
        hallucinated skill names (S13b gap). When empty, plain ``string`` is
        used (no enum) to avoid an empty-enum schema that some providers reject.
    available_agents:
        Peer agent entries. Each dict must have at least ``name``.
        Same enum strategy as above for ``delegate_to_agent.to``.
    file_permissions:
        Optional dict with ``read`` and/or ``write`` lists of path strings.
        - None or both empty → File tools omitted entirely (C1–C4).
        - read non-empty, write empty → include C1+C2 only.
        - write non-empty → include all 4 file tools (C1–C4).
    mcp_servers:
        Optional list of MCP server dicts (each with ``name`` and
        ``description``). None or [] → MCP tools omitted. Otherwise all 3
        MCP tools (D1–D3) are included, unless ``mcp_search_threshold`` is
        exceeded (see below).
    web_fetch_allowed:
        Kept for backward compatibility. FP-0022: web_fetch is now always
        included in the catalog; approval is handled at the handler level
        via the 4-layer PermissionResolver._approve() flow.
    mcp_search_threshold:
        FP-0024 Component D. When the total MCP tool count is >= this value
        (and > 0), the D1–D3 inline MCP tools are replaced by a single
        tool_search_tool meta-tool that loads specific tools on demand.
        Default: MCP_SEARCH_THRESHOLD (30). Set 0 to always inline.
        Override per-project via ``mcp.search_threshold:`` in reyn.yaml.
    """
    # RETRO-H1+H2 fix: dynamic enum injection for invoke_skill.name and
    # delegate_to_agent.to closes the schema-level hallucination gap (P4
    # alignment — LLM picks only from OS-provided candidates).
    #
    # History: PR37 wave 2D added enum; post-2D dogfood showed an attractor
    # side-effect ("hello" → ai_article_writer). That regression was caused by
    # surfacing skill names *only* in the schema without a flat list in the
    # system prompt — the LLM saw names but lacked context to judge relevance.
    # RETRO fix pairs enum (schema layer) with a flat list + one-line
    # description in the system prompt (context layer), giving the LLM both
    # constraint and context to resist the attractor.
    #
    # Wave 2c migration: dynamic enum injection has moved to schema_enricher
    # in tools/invoke_skill.py (_enrich_router_schema) and
    # tools/delegate_to_agent.py (_enrich_router_schema). The inline
    # _invoke_skill_name_schema / _delegate_to_schema locals are no longer
    # needed; render_for_router(state=...) applies the enrichment per-call.
    #
    # When available_skills is empty, invoke_skill is omitted from the tools
    # list to avoid an empty-enum schema that some providers reject.
    # Same strategy for available_agents / delegate_to_agent.
    skill_names = [s["name"] for s in available_skills]
    agent_names = [a["name"] for a in available_agents]

    # Collect ToolSpec objects in canonical order (single source of truth).
    # Each spec carries name + description + parameters + dispatch_kind.
    # build_tools() converts to OpenAI dict shape via to_openai_dict().
    from reyn.tools import get_default_registry as _get_default_registry
    _registry = _get_default_registry()

    specs: list[ToolSpec] = []

    # ── A1: list_skills ──────────────────────────────────────────────────
    _list_skills_def = _registry.lookup("list_skills")
    if _list_skills_def is not None and _list_skills_def.gates.router == "allow":
        _list_skills_rendered = _list_skills_def.render_for_router()
        specs.append(ToolSpec(
            name=_list_skills_rendered["function"]["name"],
            description=_list_skills_rendered["function"]["description"],
            parameters=_list_skills_rendered["function"]["parameters"],
            dispatch_kind=_list_skills_def.dispatch_kind,
        ))

    # ── A2: describe_skill ───────────────────────────────────────────────
    _describe_skill_def = _registry.lookup("describe_skill")
    if _describe_skill_def is not None and _describe_skill_def.gates.router == "allow":
        _describe_skill_rendered = _describe_skill_def.render_for_router()
        specs.append(ToolSpec(
            name=_describe_skill_rendered["function"]["name"],
            description=_describe_skill_rendered["function"]["description"],
            parameters=_describe_skill_rendered["function"]["parameters"],
            dispatch_kind=_describe_skill_def.dispatch_kind,
        ))

    # ── A3: list_agents ──────────────────────────────────────────────────
    _list_agents_def = _registry.lookup("list_agents")
    if _list_agents_def is not None and _list_agents_def.gates.router == "allow":
        _list_agents_rendered = _list_agents_def.render_for_router()
        specs.append(ToolSpec(
            name=_list_agents_rendered["function"]["name"],
            description=_list_agents_rendered["function"]["description"],
            parameters=_list_agents_rendered["function"]["parameters"],
            dispatch_kind=_list_agents_def.dispatch_kind,
        ))

    # ── A4: describe_agent ───────────────────────────────────────────────
    _describe_agent_def = _registry.lookup("describe_agent")
    if _describe_agent_def is not None and _describe_agent_def.gates.router == "allow":
        _describe_agent_rendered = _describe_agent_def.render_for_router()
        specs.append(ToolSpec(
            name=_describe_agent_rendered["function"]["name"],
            description=_describe_agent_rendered["function"]["description"],
            parameters=_describe_agent_rendered["function"]["parameters"],
            dispatch_kind=_describe_agent_def.dispatch_kind,
        ))

    # ── A5: list_memory ──────────────────────────────────────────────────
    _list_memory_def = _registry.lookup("list_memory")
    if _list_memory_def is not None and _list_memory_def.gates.router == "allow":
        _list_memory_rendered = _list_memory_def.render_for_router()
        specs.append(ToolSpec(
            name=_list_memory_rendered["function"]["name"],
            description=_list_memory_rendered["function"]["description"],
            parameters=_list_memory_rendered["function"]["parameters"],
            dispatch_kind=_list_memory_def.dispatch_kind,
        ))

    # ── A6: read_memory_body ─────────────────────────────────────────────
    _read_memory_body_def = _registry.lookup("read_memory_body")
    if _read_memory_body_def is not None and _read_memory_body_def.gates.router == "allow":
        _read_memory_body_rendered = _read_memory_body_def.render_for_router()
        specs.append(ToolSpec(
            name=_read_memory_body_rendered["function"]["name"],
            description=_read_memory_body_rendered["function"]["description"],
            parameters=_read_memory_body_rendered["function"]["parameters"],
            dispatch_kind=_read_memory_body_def.dispatch_kind,
        ))

    # ── B1: invoke_skill (conditional — omitted when no skills registered) ──
    # Wave 2c: registry-driven render with schema_enricher for per-call enum.
    from reyn.tools.types import RouterCallerState as _RouterCallerState
    _state = _RouterCallerState(
        available_skills=[{"name": n} for n in skill_names],
        available_agents=[{"name": n} for n in agent_names],
    )
    _invoke_skill_def = _registry.lookup("invoke_skill")
    if _invoke_skill_def is not None and _invoke_skill_def.gates.router == "allow" and skill_names:
        _invoke_skill_rendered = _invoke_skill_def.render_for_router(state=_state)
        specs.append(ToolSpec(
            name=_invoke_skill_rendered["function"]["name"],
            description=_invoke_skill_rendered["function"]["description"],
            parameters=_invoke_skill_rendered["function"]["parameters"],
            dispatch_kind=_invoke_skill_def.dispatch_kind,
        ))

    # ── B2: delegate_to_agent ────────────────────────────────────────────
    # Wave 2c: registry-driven render with schema_enricher for per-call enum.
    _delegate_def = _registry.lookup("delegate_to_agent")
    if _delegate_def is not None and _delegate_def.gates.router == "allow":
        _delegate_rendered = _delegate_def.render_for_router(state=_state)
        specs.append(ToolSpec(
            name=_delegate_rendered["function"]["name"],
            description=_delegate_rendered["function"]["description"],
            parameters=_delegate_rendered["function"]["parameters"],
            dispatch_kind=_delegate_def.dispatch_kind,
        ))

    # ── B3: remember_shared ──────────────────────────────────────────────
    _remember_shared_def = _registry.lookup("remember_shared")
    if _remember_shared_def is not None and _remember_shared_def.gates.router == "allow":
        _remember_shared_rendered = _remember_shared_def.render_for_router()
        specs.append(ToolSpec(
            name=_remember_shared_rendered["function"]["name"],
            description=_remember_shared_rendered["function"]["description"],
            parameters=_remember_shared_rendered["function"]["parameters"],
            dispatch_kind=_remember_shared_def.dispatch_kind,
        ))

    # ── B4: remember_agent ───────────────────────────────────────────────
    _remember_agent_def = _registry.lookup("remember_agent")
    if _remember_agent_def is not None and _remember_agent_def.gates.router == "allow":
        _remember_agent_rendered = _remember_agent_def.render_for_router()
        specs.append(ToolSpec(
            name=_remember_agent_rendered["function"]["name"],
            description=_remember_agent_rendered["function"]["description"],
            parameters=_remember_agent_rendered["function"]["parameters"],
            dispatch_kind=_remember_agent_def.dispatch_kind,
        ))

    # ── B5: forget_memory ────────────────────────────────────────────────
    _forget_memory_def = _registry.lookup("forget_memory")
    if _forget_memory_def is not None and _forget_memory_def.gates.router == "allow":
        _forget_memory_rendered = _forget_memory_def.render_for_router()
        specs.append(ToolSpec(
            name=_forget_memory_rendered["function"]["name"],
            description=_forget_memory_rendered["function"]["description"],
            parameters=_forget_memory_rendered["function"]["parameters"],
            dispatch_kind=_forget_memory_def.dispatch_kind,
        ))

    # ── C. File tools (permission-gated) ─────────────────────────────────────
    #
    # File access tools are gated on the operator's `permissions.file.*`
    # declaration. The OS-level dispatch layer
    # (`permissions._in_default_read_zone`) does grant reads within the
    # project root by default, but exposing the tools without a matching
    # config declaration mixes "operator opt-in" with "OS auto-grant" in a
    # way that makes the safety boundary fuzzy — a previous attempt to
    # align the two layers (= unconditional tool exposure) was reverted
    # because it dragged the chat router into the user-file protection
    # surface. Reyn's own source / docs are accessed via the dedicated
    # `reyn_src_*` tools (see section F below), which carry no
    # permission-protected content and so don't need this gate.
    _file_read = (file_permissions or {}).get("read") or []
    _file_write = (file_permissions or {}).get("write") or []

    if _file_read or _file_write:
        # ── C1: list_directory ───────────────────────────────────────────
        _list_directory_def = _registry.lookup("list_directory")
        if _list_directory_def is not None and _list_directory_def.gates.router == "allow":
            _list_directory_rendered = _list_directory_def.render_for_router()
            specs.append(ToolSpec(
                name=_list_directory_rendered["function"]["name"],
                description=_list_directory_rendered["function"]["description"],
                parameters=_list_directory_rendered["function"]["parameters"],
                dispatch_kind=_list_directory_def.dispatch_kind,
            ))

        # ── C2: read_file ────────────────────────────────────────────────
        _read_file_def = _registry.lookup("read_file")
        if _read_file_def is not None and _read_file_def.gates.router == "allow":
            _read_file_rendered = _read_file_def.render_for_router()
            specs.append(ToolSpec(
                name=_read_file_rendered["function"]["name"],
                description=_read_file_rendered["function"]["description"],
                parameters=_read_file_rendered["function"]["parameters"],
                dispatch_kind=_read_file_def.dispatch_kind,
            ))

        if _file_write:
            # C3 and C4 only when write scope is configured
            # ── C3: write_file ───────────────────────────────────────────
            _write_file_def = _registry.lookup("write_file")
            if _write_file_def is not None and _write_file_def.gates.router == "allow":
                _write_file_rendered = _write_file_def.render_for_router()
                specs.append(ToolSpec(
                    name=_write_file_rendered["function"]["name"],
                    description=_write_file_rendered["function"]["description"],
                    parameters=_write_file_rendered["function"]["parameters"],
                    dispatch_kind=_write_file_def.dispatch_kind,
                ))

            # ── C4: delete_file ──────────────────────────────────────────
            _delete_file_def = _registry.lookup("delete_file")
            if _delete_file_def is not None and _delete_file_def.gates.router == "allow":
                _delete_file_rendered = _delete_file_def.render_for_router()
                specs.append(ToolSpec(
                    name=_delete_file_rendered["function"]["name"],
                    description=_delete_file_rendered["function"]["description"],
                    parameters=_delete_file_rendered["function"]["parameters"],
                    dispatch_kind=_delete_file_def.dispatch_kind,
                ))

    # ── E. Web tools (OS-native, backed by Control IR ops web/search +
    #         web/fetch). E1 web_search is always exposed (read-only, public
    #         queries — comparable security level to a logged query string).
    #         E2 web_fetch is opt-in: arbitrary URL fetches can be misused for
    #         data exfiltration (LLM bakes secrets into the URL and the
    #         attacker's server logs them) or to probe internal endpoints, so
    #         the operator enables it explicitly via `web.fetch: allow` in
    #         reyn.yaml.
    # ── E1: web_search (always available) — sourced from unified registry ────
    # ADR-0026 M2: web_search is the first capability migrated to the unified
    # ToolRegistry. build_tools() renders it via WEB_SEARCH.render_for_router()
    # which produces byte-identical output to the prior ToolSpec literal.
    # WEB_SEARCH is now the single source of truth. M4 cleanup removes the
    # ToolSpec pattern here.
    _web_search_def = _registry.lookup("web_search")
    if _web_search_def is not None and _web_search_def.gates.router == "allow":
        # Render via unified ToolDefinition; produces the OpenAI tools[] shape.
        # Byte-identity with the prior ToolSpec.to_openai_dict() is verified
        # by test_web_search_unified.py.
        _ws_rendered = _web_search_def.render_for_router()
        specs.append(ToolSpec(
            name=_ws_rendered["function"]["name"],
            description=_ws_rendered["function"]["description"],
            parameters=_ws_rendered["function"]["parameters"],
        ))

    # ── E2: web_fetch (FP-0022: always in catalog; approval at handler level) ────
    # ADR-0026 M3 Wave 1: rendered from unified ToolDefinition.
    # FP-0022: removed catalog-level gate (was `if web_fetch_allowed`). The
    # `web_fetch_allowed` parameter is kept for backward compat but ignored.
    # Authorization is now enforced by handle_web_fetch() via the standard
    # 4-layer PermissionResolver._approve() flow.
    _wf = _registry.lookup("web_fetch")
    if _wf is not None and _wf.gates.router == "allow":
        _wf_rendered = _wf.render_for_router()
        specs.append(ToolSpec(
            name=_wf_rendered["function"]["name"],
            description=_wf_rendered["function"]["description"],
            parameters=_wf_rendered["function"]["parameters"],
            dispatch_kind=_wf.dispatch_kind,
        ))

    # ── G. Plan tool (always present) ────────────────────────────────────────
    #
    # `plan` lets the LLM decompose a complex query into 2-7 sub-tasks
    # that the OS executes in topological order, each in a narrow LLM
    # call (= focused tool catalog + step-specific system prompt). The
    # terminal step's reply becomes the user-facing answer.
    #
    # Why opt-in by the LLM (= just another tool, not a forced mode):
    # simple chat queries should still work via direct reply or single
    # tool call. Plan adds latency / cost (= 2-7 extra LLM calls per
    # turn), so the description nudges the LLM to use it ONLY when the
    # query genuinely needs multi-source synthesis.
    #
    # The schema enforces 2-7 steps, string ids, and strings-only tool
    # arrays. Per-step tool name validity (= must be in current
    # catalog) is checked at dispatch time by `parse_and_validate_plan`,
    # not here, because the tool list is dynamic per-session. Cycle
    # detection is also at dispatch time.
    # ADR-0026 M3 Wave 1: rendered from unified ToolDefinition.
    _plan_def = _registry.lookup("plan")
    if _plan_def is not None and _plan_def.gates.router == "allow":
        _plan_rendered = _plan_def.render_for_router()
        specs.append(ToolSpec(
            name=_plan_rendered["function"]["name"],
            description=_plan_rendered["function"]["description"],
            parameters=_plan_rendered["function"]["parameters"],
            dispatch_kind=_plan_def.dispatch_kind,
        ))
    else:  # pragma: no cover - defensive fallback if registry mis-init
        # ── G1: plan ─────────────────────────────────────────────────────────
        specs.append(ToolSpec(
            name="plan",
            description=(
                "Decompose a complex query into 2-7 independent "
                "sub-tasks. Use ONLY when the query needs multi-"
                "source synthesis (e.g. \"explain X with code "
                "references\", \"compare A vs B from multiple "
                "docs\", \"build a summary across these N "
                "files\"). For simple queries — chitchat, single-"
                "tool retrieval, single-source narration — reply "
                "directly or call one tool; do NOT use plan. "
                "Each step summarises what it found; the router "
                "synthesises the final reply after all steps "
                "complete."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "1-sentence restatement of the user's overall query."
                        ),
                    },
                    # steps is a JSON-encoded string instead of a nested
                    # object array because the chat-router schema budget
                    # forbids depth-2 object properties (= Gemini-safe
                    # constraint, see test_nested_objects_max_depth_1).
                    # The dispatch layer parses + validates the JSON.
                    "steps_json": {
                        "type": "string",
                        "description": (
                            "JSON-encoded array of 2-7 step objects. Each "
                            "step has shape: "
                            "{\"id\": str, \"description\": str, "
                            "\"tools\": [str, ...], \"depends_on\": [str, ...]}. "
                            "id: short unique identifier. description: what "
                            "this step does. "
                            # 2026-05-07 dogfood fix: clarify step.tools field —
                            # LLM was confusing skill names (= invoke_skill enum
                            # values like \"direct_llm\") with top-level tool
                            # names. Be explicit about both the source of truth
                            # AND the empty-list semantics for synthesis steps.
                            "tools: list of TOP-LEVEL tool names this step "
                            "calls (e.g. \"reyn_src_read\", \"web_search\", "
                            "\"invoke_skill\"). Use [] for steps that only "
                            "need prior step outputs as context — the step "
                            "LLM reasons from those natively. To run a skill, "
                            "use [\"invoke_skill\"], NOT the skill's name. "
                            "depends_on: ids of prior steps whose output this "
                            "step needs (default []). Each step should "
                            "summarise what it found; the router synthesises "
                            "the final reply after all steps complete. Example: "
                            "[{\"id\": \"s1\", \"description\": \"read README\", "
                            "\"tools\": [\"reyn_src_read\"], \"depends_on\": []}, "
                            "{\"id\": \"s2\", \"description\": \"compare and "
                            "summarise findings\", "
                            "\"tools\": [], \"depends_on\": [\"s1\"]}]"
                        ),
                    },
                },
                "required": ["goal", "steps_json"],
            },
            dispatch_kind="async",
        ))

    # ── F. Reyn-source tools (always present, no permission) ────────────────
    #
    # `reyn_src_list` / `reyn_src_read` give the agent read access to
    # **Reyn's own** repository (= the project where pyproject.toml
    # declares Reyn). They serve a single use case: when the user asks
    # how Reyn works or wants a deep-dive into its implementation, the
    # agent should answer from Reyn's source/docs, not web search.
    #
    # Why no permission gate: the resolver scopes paths to the Reyn
    # repository tree, which is by definition public open-source content
    # (= GitHub secret-scanning blocks credentials at push time, so
    # nothing in the tree is sensitive). Operators don't configure this
    # — it's an OS-internal capability, distinct from `file_*` (= which
    # accesses the *user's* project files and IS permission-gated).
    #
    # Why two tools, not one: `list` lets the LLM discover the layout
    # before reading; `read` returns the file body. Mirrors the file_*
    # pair so the LLM's tool-use pattern is consistent across both
    # Reyn-source and user-file access.

    # ── F1: reyn_src_list ────────────────────────────────────────────────────
    _reyn_src_list_def = _registry.lookup("reyn_src_list")
    if _reyn_src_list_def is not None and _reyn_src_list_def.gates.router == "allow":
        _reyn_src_list_rendered = _reyn_src_list_def.render_for_router()
        specs.append(ToolSpec(
            name=_reyn_src_list_rendered["function"]["name"],
            description=_reyn_src_list_rendered["function"]["description"],
            parameters=_reyn_src_list_rendered["function"]["parameters"],
            dispatch_kind=_reyn_src_list_def.dispatch_kind,
        ))

    # ── F2: reyn_src_read ────────────────────────────────────────────────────
    _reyn_src_read_def = _registry.lookup("reyn_src_read")
    if _reyn_src_read_def is not None and _reyn_src_read_def.gates.router == "allow":
        _reyn_src_read_rendered = _reyn_src_read_def.render_for_router()
        specs.append(ToolSpec(
            name=_reyn_src_read_rendered["function"]["name"],
            description=_reyn_src_read_rendered["function"]["description"],
            parameters=_reyn_src_read_rendered["function"]["parameters"],
            dispatch_kind=_reyn_src_read_def.dispatch_kind,
        ))

    # ── H. RAG tools (always present when registered) ────────────────────────
    #
    # `recall` performs semantic search over indexed sources; `drop_source`
    # removes an indexed source (permission-gated at the op level via the
    # index_drop permission resolver gate).  Both are gated only by registry
    # gates (= gates.router="allow"); no operator config is required to expose
    # them — they appear unconditionally when the registry contains them.
    #
    # ADR-0033 Phase 1: wired here after the reyn_src cluster (F) and before
    # MCP (D) so the LLM sees them as first-class tools rather than
    # capability-gated extras.
    #
    # B17-S6-1 / B17-S8-2 fix: these were registered in ToolRegistry but
    # missing from build_tools(), so the LLM could not see or call them.

    # ── H1: recall ───────────────────────────────────────────────────────────
    _recall_def = _registry.lookup("recall")
    if _recall_def is not None and _recall_def.gates.router == "allow":
        _recall_rendered = _recall_def.render_for_router()
        specs.append(ToolSpec(
            name=_recall_rendered["function"]["name"],
            description=_recall_rendered["function"]["description"],
            parameters=_recall_rendered["function"]["parameters"],
            dispatch_kind=_recall_def.dispatch_kind,
        ))

    # ── H2: drop_source ──────────────────────────────────────────────────────
    _drop_source_def = _registry.lookup("drop_source")
    if _drop_source_def is not None and _drop_source_def.gates.router == "allow":
        _drop_source_rendered = _drop_source_def.render_for_router()
        specs.append(ToolSpec(
            name=_drop_source_rendered["function"]["name"],
            description=_drop_source_rendered["function"]["description"],
            parameters=_drop_source_rendered["function"]["parameters"],
            dispatch_kind=_drop_source_def.dispatch_kind,
        ))

    # ── D. MCP tools (permission-gated) ──────────────────────────────────────
    #
    # FP-0024 Component D: threshold-based switch.
    # When the configured MCP server count >= mcp_search_threshold (and the
    # threshold is > 0), substitute the three inline D1–D3 tools with a single
    # Anthropic tool_search_tool meta-tool.  The LLM issues tool_search calls;
    # Anthropic's server loads only the K most relevant MCP tool schemas on
    # demand (Spring AI: 63–64% token reduction vs inline at 40+ servers).
    #
    # When below threshold: existing behavior — D1 list_mcp_servers, D2
    # list_mcp_tools, D3 call_mcp_tool are all included inline as before.
    #
    # Note: mcp_servers here is a list of server config dicts (one per server).
    # Each server can expose multiple tools, but the tool count is not known at
    # this layer without async enumeration.  The threshold is applied against
    # len(mcp_servers) as a proxy.  Callers may pass a higher
    # mcp_search_threshold to keep inline mode if server-per-tool ratio is low.
    if mcp_servers:
        _mcp_count = len(mcp_servers)
        _use_search_tool = (
            mcp_search_threshold > 0
            and _mcp_count >= mcp_search_threshold
        )

        if _use_search_tool:
            # ── D-S: tool_search_tool (deferred-loading mode) ────────────────
            # Build the per-server stub tool specs that the search meta-tool
            # wraps.  Each stub carries the server's name and description so
            # the search backend can match queries; the actual tool schemas are
            # loaded on demand by Anthropic's infrastructure.
            _mcp_stub_specs: list[dict] = []
            for _srv in mcp_servers:
                _mcp_stub_specs.append({
                    "type": "function",
                    "function": {
                        "name": str(_srv.get("name", "")),
                        "description": str(_srv.get("description", "")),
                        "parameters": {"type": "object", "properties": {}},
                    },
                })
            # Append the meta-tool directly (not wrapped in ToolSpec — its
            # ``type`` is "tool_search_tool_20251101", not "function").
            _search_tool = build_mcp_search_tool(_mcp_stub_specs)
            # Emit as a raw dict; convert step at the end handles list[dict].
            # We store it in a separate list and merge after ToolSpec conversion.
            _mcp_search_tool_raw: list[dict] = [_search_tool]
        else:
            _mcp_search_tool_raw = []
            # ── D1: list_mcp_servers ─────────────────────────────────────────
            _list_mcp_servers_def = _registry.lookup("list_mcp_servers")
            if _list_mcp_servers_def is not None and _list_mcp_servers_def.gates.router == "allow":
                _list_mcp_servers_rendered = _list_mcp_servers_def.render_for_router()
                specs.append(ToolSpec(
                    name=_list_mcp_servers_rendered["function"]["name"],
                    description=_list_mcp_servers_rendered["function"]["description"],
                    parameters=_list_mcp_servers_rendered["function"]["parameters"],
                    dispatch_kind=_list_mcp_servers_def.dispatch_kind,
                ))

            # ── D2: list_mcp_tools ───────────────────────────────────────────
            _list_mcp_tools_def = _registry.lookup("list_mcp_tools")
            if _list_mcp_tools_def is not None and _list_mcp_tools_def.gates.router == "allow":
                _list_mcp_tools_rendered = _list_mcp_tools_def.render_for_router()
                specs.append(ToolSpec(
                    name=_list_mcp_tools_rendered["function"]["name"],
                    description=_list_mcp_tools_rendered["function"]["description"],
                    parameters=_list_mcp_tools_rendered["function"]["parameters"],
                    dispatch_kind=_list_mcp_tools_def.dispatch_kind,
                ))

            # ── D3: call_mcp_tool ────────────────────────────────────────────
            # FP-0032: enum injection via _enrich_router_schema — builds a
            # minimal RouterCallerState carrying mcp_servers so the enricher
            # can inject server + mcp_tool_name enums (P4 alignment).
            from reyn.tools.types import RouterCallerState as _RouterCallerState
            _mcp_state = _RouterCallerState(mcp_servers=list(mcp_servers or []))
            _call_mcp_tool_def = _registry.lookup("call_mcp_tool")
            if _call_mcp_tool_def is not None and _call_mcp_tool_def.gates.router == "allow":
                _call_mcp_tool_rendered = _call_mcp_tool_def.render_for_router(
                    state=_mcp_state
                )
                specs.append(ToolSpec(
                    name=_call_mcp_tool_rendered["function"]["name"],
                    description=_call_mcp_tool_rendered["function"]["description"],
                    parameters=_call_mcp_tool_rendered["function"]["parameters"],
                    dispatch_kind=_call_mcp_tool_def.dispatch_kind,
                ))

            # ── D4: describe_mcp_tool ─────────────────────────────────────────
            _describe_mcp_tool_def = _registry.lookup("describe_mcp_tool")
            if _describe_mcp_tool_def is not None and _describe_mcp_tool_def.gates.router == "allow":
                _describe_mcp_tool_rendered = _describe_mcp_tool_def.render_for_router(
                    state=_mcp_state
                )
                specs.append(ToolSpec(
                    name=_describe_mcp_tool_rendered["function"]["name"],
                    description=_describe_mcp_tool_rendered["function"]["description"],
                    parameters=_describe_mcp_tool_rendered["function"]["parameters"],
                    dispatch_kind=_describe_mcp_tool_def.dispatch_kind,
                ))
    else:
        _mcp_search_tool_raw = []

    # ── I. Universal catalog wrappers (FP-0034 PR-3b-iv default-on) ──────────
    #
    # When universal_wrappers_enabled=True (= production default since
    # PR-3b-iv flipped ActionRetrievalConfig), append the 3 universal
    # wrappers (list_actions / describe_action / invoke_action) at the
    # END of the specs list per §D21.  The flag stays False for direct
    # callers that don't pass an ActionRetrievalConfig (= LLMReplay
    # fixture-safe path).
    #
    # search_actions is gated separately by §D14 (= embedding configured).
    # Phase 1 keeps it OFF unconditionally because (a) the handler is a
    # NotImplementedError stub awaiting Phase 2's ActionEmbeddingIndex,
    # and (b) embedding_class plumbing through RouterHostAdapter is also
    # a Phase 2 task — visibility + handler must land together.
    if universal_wrappers_enabled:
        # Default 3 wrappers always exposed when the flag is on.
        # search_actions is added conditionally per §D14 only when the
        # session has an ActionEmbeddingIndex ready AND the operator
        # configured ``action_retrieval.embedding_class``.  Callers
        # (= RouterLoop) compute ``search_actions_visible`` from both
        # signals before invoking ``build_tools``.
        _wrapper_names: tuple[str, ...] = (
            ("list_actions", "search_actions", "describe_action", "invoke_action")
            if search_actions_visible
            else ("list_actions", "describe_action", "invoke_action")
        )
        for _wrapper_name in _wrapper_names:
            _wrapper_def = _registry.lookup(_wrapper_name)
            if _wrapper_def is None or _wrapper_def.gates.router != "allow":
                continue
            _wrapper_rendered = _wrapper_def.render_for_router()
            specs.append(ToolSpec(
                name=_wrapper_rendered["function"]["name"],
                description=_wrapper_rendered["function"]["description"],
                parameters=_wrapper_rendered["function"]["parameters"],
                dispatch_kind=_wrapper_def.dispatch_kind,
            ))

    # ── J. Phase 2 prep: hide_legacy_tools exclusive-wrapper mode ─────────────
    #
    # When both flags are true, strip all legacy per-kind tools so the LLM
    # surface is the 3 wrappers only.  Safety: only takes effect when the
    # wrappers are also enabled (= the LLM still has *some* addressing path).
    # When only hide_legacy_tools=True with universal_wrappers_enabled=False,
    # this branch is a no-op — the operator misconfigured, but stripping
    # all addressing surfaces would leave the LLM tool-less.  Future Phase 2
    # may raise on this combination once the wrapper surface is dogfood-stable.
    if hide_legacy_tools and universal_wrappers_enabled:
        _LEGACY_TOOL_NAMES = frozenset({
            "list_skills", "describe_skill",
            "invoke_skill",
            "list_agents", "describe_agent",
            "delegate_to_agent",
            "list_mcp_servers", "list_mcp_tools",
            "call_mcp_tool", "describe_mcp_tool",
            "list_memory", "read_memory_body",
            "remember_shared", "remember_agent", "forget_memory",
            "recall", "drop_source",
            "read_file", "write_file", "delete_file", "list_directory",
            "web_search", "web_fetch",
            "reyn_src_list", "reyn_src_read",
            "plan",
        })
        specs = [s for s in specs if s.name not in _LEGACY_TOOL_NAMES]

    # ── K. Hot list direct aliases (FP-0034 Phase 2 step 3) ─────────────────
    #
    # When universal_wrappers_enabled=True and hot_list_aliases is a non-empty
    # list, append the alias dicts at the end of the tools list (after the
    # universal wrappers).  Each alias is already in OpenAI dict shape
    # {"type": "function", "function": {"name": ..., "description": ...,
    # "parameters": ...}} — RouterLoop constructs them from the catalog.
    # args are passed through to invoke_action dispatch unchanged.
    #
    # None / empty list → no-op (= Phase 2 step 2 callers unchanged).
    _hot_list_raw: list[dict] = []
    if universal_wrappers_enabled and hot_list_aliases:
        _hot_list_raw = list(hot_list_aliases)

    # Convert ToolSpec list → OpenAI dict list (backward-compat return type).
    # Append tool_search_tool raw dict last (D-S deferred mode only; empty
    # list otherwise — no-op for the inline path).
    # Hot list aliases follow MCP search tool (= ephemeral, session-specific).
    return [spec.to_openai_dict() for spec in specs] + _mcp_search_tool_raw + _hot_list_raw


def _build_tools_via_registry(registry) -> list[dict]:
    """Private helper for M2/M3 registry-driven tool building.

    Produces the OpenAI tools[] list from a ToolRegistry's router-allowed
    entries via ToolDefinition.render_for_router(). Used by M2/M3 capability
    migrations; the public build_tools() function is unchanged.

    Per ADR-0026 M1 adapter shim pattern: this helper exists so M2/M3 can
    wire registry entries into the router surface without touching the
    public API surface.
    """
    return [tool.render_for_router() for tool in registry.for_router()]
