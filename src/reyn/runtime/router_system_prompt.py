"""System prompt builder for the native tool_use router loop (PR35).

Size is O(categories), independent of item count — Progressive Disclosure
(Lazy Hierarchical Catalog).  Path-level detail is deferred to list_* tools
at runtime.

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector — it takes
a slot-map and injects slots at fixed positions. It does NOT build tool-use SP.
``build_universal_tool_use_slots`` has been relocated to
``reyn.tools.schemes._universal_sp`` (scheme layer, P7-clean).
"""
from __future__ import annotations

import re
from collections import defaultdict

from reyn.prompt.router_frame import (
    BEHAVIOUR_STATIC_CORE,
    DEFAULT_CWD_HOW_CLAUSE,
    IDENTITY_PREAMBLE,
    MEMORY_GUIDANCE_BULLET,
    PROJECT_CONTEXT_HEADER,
    PROJECT_CONTEXT_PREFERENCE_NOTE,
    ambiguity_rule,
    cwd_reference_mapping_sentence,
    output_language_directive,
    render_mechanism_routing_frame,
    role_stamp,
)
from reyn.runtime.router_tools import MAX_DESC_LEN_FOR_LISTING

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(
    *,
    agent_name: str,
    agent_role: str,
    available_agents: list[dict],
    memory_index: dict,
    file_permissions: dict | None = None,
    mcp_servers: list[dict] | None = None,
    web_fetch_allowed: bool = True,  # FP-0022: always-on; parameter kept for backward compat
    output_language: str | None = None,
    project_context: str = "",
    cwd: str | None = None,
    tool_use_sp: "str | dict[str, str] | None" = None,  # #1627 Stage 4: scheme-owned slot-map (or str back-compat shim); None → {} (bare OS frame, no tool-use SP)
    context_size_signal: str | None = None,  # #272/#1128 — pre-rendered, appended LAST
    environment_info: "dict | None" = None,  # #1479: date/platform/shell/git from get_environment_info()
    scheme_sp_fragment: str = "",  # kept for backward compat; prefer tool_use_sp slot_post_catalog
    reasoning_continuity_section: str = "",  # #1652: pre-rendered prior-reasoning text section ("" = omit, byte-identical)
    non_interactive: bool = False,  # sp-autonomy-revision: gates the OS-frame ambiguity/proceed-vs-ask Behaviour rule
) -> str:
    """Render the system prompt for the tool_use router loop.

    #1627 Stage 4: pure slot-injector. ``build_system_prompt`` takes a
    ``tool_use_sp`` slot-map (built by the scheme layer) and injects each
    slot at its fixed position in the OS frame. It does NOT build tool-use SP.

    ``tool_use_sp`` normalisation:
      - ``str``  → ``{"slot_pre_environment": str}`` (back-compat shim for
                    a bare replacement string, e.g. CodeAct's code-API).
      - ``dict`` → used as-is (slot-map from a universal/enumerate/retrieval
                    ``build_universal_tool_use_slots`` call).
      - ``None`` → ``{}`` (empty — bare OS frame, no tool-use SP injected).

    Args:
        agent_name: short identifier of the agent (e.g. "chat").
        agent_role: one-liner from agent profile.
        available_agents: list of dicts with at least ``name``; optional keys
            ``role``, ``cluster``.
        memory_index: ``{"status": "ok"|"not_found", "content": str}``.
        file_permissions: optional ``{"read": [paths], "write": [paths]}``.
            Accepted for backward compat; no longer rendered in the SP
            (FP-0034 wrapper-only: file discovery goes through list_actions).
        mcp_servers: accepted for backward compat but no longer used —
            FP-0034 wrapper-only removed the static ``## MCP servers``
            section in favour of runtime discovery via
            ``list_actions(category=['mcp.server','mcp.tool'])``. Kept
            on the signature so existing callers don't break.
        output_language: BCP-47-style language code (e.g. "ja", "en"),
            or None when unset. When set, the Behaviour section emits a
            strict "Always reply in language: <code>" directive so the
            LLM stays in that language even on clarifying / error paths.
            When None (= user did not configure output_language), no
            language directive is emitted — the LLM picks the reply
            language based on the user's input naturally.
        cwd: current working directory the agent is running from. When
            provided, an ## Environment section tells the LLM to treat
            unqualified references like "this repo" / "this code" /
            "the codebase" as the project at ``cwd``. When None
            (default), the section is omitted.
        tool_use_sp: scheme-owned slot-map (``dict[str, str]``) or a bare
            replacement string (``str`` back-compat shim). ``None`` → empty
            map (bare OS frame, no tool-use SP).
        context_size_signal: pre-rendered context-size header, appended LAST.
        environment_info: system info dict (date/platform/shell/git).
        scheme_sp_fragment: backward-compat legacy free-form SP fragment
            (prefer ``tool_use_sp["slot_post_catalog"]`` for new schemes).
        non_interactive: True for non-interactive (ephemeral/headless) sessions
            with no user to ask. Gates the wording of the OS-frame ambiguity/
            proceed-vs-ask Behaviour rule (scheme-agnostic — reaches every
            tool-use scheme, including CodeAct, unlike the old scheme-owned
            fork this superseded).
    """
    parts: list[str] = []

    # ==========================================================================
    # STATIC — cache prefix target (FP-0023 Change 1)
    # Sections 1–5: Identity, Role, Capabilities, Behaviour (static core)
    # These are session-invariant; placing them first maximises cache-prefix
    # coverage (~60% of prompt chars) for Anthropic prompt cache.
    # Dynamic sections follow below (project_context onward).
    # ==========================================================================

    # ── 1. OS-level identity preamble ──────────────────────────────────────
    parts.append(IDENTITY_PREAMBLE)
    parts.append("")

    # ── 2. Role ─────────────────────────────────────────────────────────────
    parts.append(role_stamp(agent_name, agent_role))
    parts.append("")

    # #1627 Stage 4: normalise tool_use_sp into a positional slot-map.
    # ``str``  → back-compat shim (slot_pre_environment only; R2/R3 absent).
    # ``dict`` → already a slot-map (from build_universal_tool_use_slots).
    # ``None`` → {} (bare OS frame — no tool-use SP).
    if isinstance(tool_use_sp, str):
        _slots: "dict[str, str]" = {"slot_pre_environment": tool_use_sp}
    elif tool_use_sp is None:
        _slots = {}
    else:
        _slots = tool_use_sp  # dict path

    # ── 3. Capabilities (routing guide) — FP-0023 Change 2 ─────────────────
    # Delivered via slot_pre_environment from the scheme's slot-map.
    if "slot_pre_environment" in _slots:
        parts.append(_slots["slot_pre_environment"])

    # ── 3.4. Environment (CWD context, P7-clean) ─────────────────────────────
    # Tells the LLM where it is running so unqualified references like
    # "this repo" / "this code" / "the codebase" / "ここのコード" map to
    # the workspace at cwd. P7: no domain-specific strings, only environment
    # facts and routing hints to existing categories.
    if cwd or environment_info:
        parts.append("## Environment")
        parts.append("")
        if cwd:
            parts.append(f"cwd: {cwd}")
        # #1479: system info — backend-derived, competitor-aligned.
        # Fields absent from environment_info are omitted (degrade, don't guess).
        if environment_info:
            if "date" in environment_info:
                parts.append(f"date: {environment_info['date']}")
            _plat = environment_info.get("platform", "")
            _ver = environment_info.get("os_version", "")
            if _plat and _ver:
                parts.append(f"platform: {_plat} {_ver}")
            elif _plat:
                parts.append(f"platform: {_plat}")
            if environment_info.get("shell"):
                parts.append(f"shell: {environment_info['shell']}")
            if "is_git_repo" in environment_info:
                parts.append(f"git repo: {'yes' if environment_info['is_git_repo'] else 'no'}")
        parts.append("")
        if cwd:
            # #1627 Stage 1: the cwd semantic mapping is OS-level; the HOW clause
            # is scheme-owned via slot_in_environment. OS keeps the generic neutral
            # default; the slot overrides it for universal schemes (P7-clean: no
            # isinstance / shape-check on slot-map).
            _cwd_how = _slots.get(
                "slot_in_environment",
                DEFAULT_CWD_HOW_CLAUSE,
            )
            parts.append(cwd_reference_mapping_sentence(_cwd_how))
            parts.append("")

    # ── 3.5. Universal catalog + discovery mandate (R2) ─────────────────────
    # Delivered via slot_post_environment from the scheme's slot-map.
    if "slot_post_environment" in _slots:
        parts.append(_slots["slot_post_environment"])

    # ── 3.6. Mechanism routing (part x role) — 0060 Addendum C, Layer C ──────
    # Cache-static, scheme-independent (C1): DERIVED from PART_TYPE_REGISTRY
    # (C3), NOT a scheme-owned tool_use_sp slot — it holds across all four
    # tool-use schemes because every scheme funnels through this one builder.
    # Placed before "## Behaviour" so it stays in the ~60%-coverage static
    # prefix alongside Identity/Role/Behaviour, not the dynamic tail.
    parts.append(render_mechanism_routing_frame())
    parts.append("")

    # ── 4 & 5. Behaviour (static core) ─────────────────────────────────────
    # FP-0023 Change 1: Static Behaviour rules moved here (before dynamic
    # sections) to maximise cache prefix coverage.
    parts.append("## Behaviour")
    # Cross-cutting rules that apply regardless of which tool was last called.
    # #1627: tool-use routing guidance is scheme-owned (delivered via the slot-map);
    # the OS keeps only these scheme-agnostic behaviour rules here.
    # #1791 A1 (adopted by design judgment): TASK_COMPLETION — anti-fabrication
    # + finish-the-task + honest-blocker. Static-core, all-model, cached.
    # Content moved to reyn.prompt.router_frame.BEHAVIOUR_STATIC_CORE.
    parts.extend(BEHAVIOUR_STATIC_CORE)
    # sp-autonomy-revision: ambiguity/proceed-vs-ask Behaviour rule, promoted to
    # the OS frame (was scheme-owned in _universal_sp.py, which only reached the
    # universal/enumerate/retrieval schemes — CodeAct's tool_use_sp REPLACES that
    # scheme's SP region entirely, so it never got the rule). Living here in the
    # static core makes it scheme-agnostic and reaches every scheme, incl CodeAct.
    parts.append(ambiguity_rule(non_interactive=non_interactive))

    # #1791 #3 (adopted by design judgment, GATED): memory-quality guidance, rendered
    # ONLY when the memory tool is active (memory_index present) — mirrors Hermes
    # MEMORY_GUIDANCE; gating keeps the cost off non-memory agents (SP-minimize-compatible).
    if memory_index.get("status") == "ok":
        parts.append(MEMORY_GUIDANCE_BULLET)

    # ── FP-0025 D — Plan decomposition Behaviour rule ─────────────────────────
    # B23-PRE-1 SP role-separation: ## Plan decomposition subsection (detail)
    # moved to plan.description.
    parts.append("")
    # ── R3 slot injection (post-behaviour position) ──────────────────────────
    # Scheme-owned content delivered via slot_in_behaviour; the OS only injects.
    if "slot_in_behaviour" in _slots:
        parts.append(_slots["slot_in_behaviour"])

    # ==========================================================================
    # DYNAMIC — varies per session / configuration
    # Sections 6–13: project_context, Memory,
    # + dynamic Behaviour conditionals (output_language).
    # ==========================================================================

    # ── 6. Project context (AGENTS.md / REYN.md) ────────────────────────────
    #
    # `project_context` carries the operator's AGENTS.md content (#1771 default;
    # REYN.md is the legacy fallback — or whatever
    # `project_context_path` points to). This is operator-editable
    # surface — do NOT use it to inject Reyn's own identity (that's the
    # preamble above). Inject only when non-empty so an unset / empty
    # REYN.md doesn't leak placeholder text into the prompt.
    if project_context.strip():
        parts.append(PROJECT_CONTEXT_HEADER)
        parts.append("")
        parts.append(project_context.strip())
        parts.append("")
        parts.append(PROJECT_CONTEXT_PREFERENCE_NOTE)
        parts.append("")

    # ── 7 & 8. Agents catalog ────────────────────────────────────────────────
    # Wrapper-only path: agent is one of the action categories (now
    # scheme-owned, no longer rendered by the OS). Dedicated sections would
    # impose a per-category special-case structure that contradicts FP-0034's
    # uniform-invoke design — so they are omitted here. (Resource discovery
    # guidance in the catalog-context sections below is a tracked content-layer
    # follow-up — see #1625.)

    # ── 9. Memory ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Memory inline section is dropped in the
    # wrapper-only path. Memory discovery goes through
    # list_actions(category=['memory_entry']) at runtime.

    # ── 10. Indexed sources ──────────────────────────────────────────────────
    # No ## Indexed sources section is rendered. B23-PRE-1 dropped it from the
    # wrapper-only path; #3025 then removed the vestigial ``indexed_sources_
    # section`` parameter and the per-turn ``SourceManifest.format_for_prompt()``
    # prefetch that fed it (the rendered string was accepted and discarded).
    # Corpus discovery is the ``list_rag_sources`` verb (#3026) — one tool
    # regardless of corpus count — not a per-corpus SP list that would scale
    # with the operator's corpora.

    # ── 11. Files ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Files section omitted in wrapper-only
    # path — permission scope communicated via file.* category at runtime.

    # ── 12. MCP servers and tools ────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## MCP servers section omitted in wrapper-
    # only path — list_actions(category=['mcp.server','mcp.tool']) discovers.

    # ── 13. Dynamic Behaviour conditionals ───────────────────────────────────
    # These vary per session config (output_language). They are Behaviour
    # addenda that cannot live in the static prefix above.

    # Explicit language instruction (only when the user configured one):
    # a concrete language tag is stronger than "match the user's language"
    # — the LLM stays in this language even on clarifying-question and
    # error fallback paths (F11). When output_language is None (= user did
    # not configure), we omit the directive entirely so the LLM can pick
    # the reply language based on the user's input naturally instead of
    # being forced into a Reyn default. (Q2 follow-up to F11 fix.)
    if output_language:
        parts.append(output_language_directive(output_language))

    # ── 13b. Scheme-owned SP fragment (#1593) / slot_post_catalog (#1627 Stage 3) ─
    # A tool-use scheme whose tool-use instructions are genuinely new content
    # (e.g. CodeAct's rendered fn-signature code-API, retrieval's search-tool SP)
    # supplies them here as free-form text. The OS appends it verbatim and does
    # NOT interpret it (P7: the OS has no notion of "code-API" / "search-SP").
    # Empty default ⇒ universal-category / enumerate-all (named-gate schemes) are
    # byte-identical. Placed before the volatile context-size signal so the
    # scheme's tool-use SP sits with the rest of the cached prefix.
    # #1627 Stage 3: slot_post_catalog — scheme-owned SP appended at the
    # post-catalog position (e.g. retrieval's search guidance). Sourced from the
    # slot-map FIRST (when a scheme owns its SP via tool_use_sp dict), else falls
    # back to the legacy scheme_sp_fragment channel (CodeAct str-shim path).
    _frag = (_slots.get("slot_post_catalog") if _slots else None) or scheme_sp_fragment
    if _frag:
        parts.append("")
        parts.append(_frag)

    # ── 13c. Skills block (#2548 PR-A) ───────────────────────────────────────
    # slot_post_skills is a DEDICATED slot (distinct from slot_post_catalog) so
    # the retrieval scheme's slot_post_catalog overwrite cannot clobber the
    # ## Skills block. Rendered from available_skills by the scheme layer
    # (build_universal_tool_use_slots); the OS only injects. Empty/absent slot
    # → no Skills section (byte-identical to no-skills configs).
    _skills_slot = _slots.get("slot_post_skills") if _slots else None
    if _skills_slot:
        parts.append("")
        parts.append(_skills_slot)

    # ── 14. Context-size signal (#272/#1128) ─────────────────────────────────
    # OS-injected, pre-rendered by the caller (router_loop / phase runtime) from
    # the live free-window. Placed LAST because it is the most per-turn-volatile
    # section — keeping it at the tail preserves the cached SP prefix above it.
    # P8-clean: OS-level vocabulary, no domain-specific enumeration; the `compact`
    # op format itself is advertised separately via the tool/control_ir catalog.
    # #1652 reasoning-continuity: prior turns' reasoning text section (pre-rendered
    # + bounded by the caller via render_reasoning_section/bound_reasoning). Empty
    # string when continuity is off / no prior reasoning → omitted (byte-identical
    # SP, LLMReplay-safe, same omit-when-empty discipline as act_turn_reasoning).
    if reasoning_continuity_section:
        parts.append(reasoning_continuity_section)

    if context_size_signal:
        parts.append(context_size_signal)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _render_agents(available_agents: list[dict]) -> str:
    """Return one-line cluster summary, or empty string when no agents."""
    counts: dict[str, int] = defaultdict(int)
    for agent in available_agents:
        cluster = agent.get("cluster") or "default"
        counts[cluster] += 1

    if not counts:
        return "(none)"

    ordered = sorted(counts.items(), key=lambda kv: (kv[0] != "default", kv[0]))
    tokens = [f"{cluster} ({n} peer{'s' if n != 1 else ''})" for cluster, n in ordered]
    return " / ".join(tokens)


_SLUG_TYPE_RE = re.compile(r"^(user|feedback|project|reference)_")
_MEMORY_TYPES = ("user", "feedback", "project", "reference")

# Matches section headers like "# Memory Index (shared)" or
# "# Memory Index (agent: chat_20240101)"
_SECTION_HEADER_RE = re.compile(
    r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
)
# Matches list entries like "- [Title](slug.md) — description"
# or "| slug | ..."  (table row)
_ENTRY_SLUG_RE = re.compile(r"\(([^)]+)\.md\)")


def _parse_memory_counts(content: str) -> dict[str, dict[str, int]]:
    """Return {layer: {type: count}} from merged memory index text.

    Layers are "shared" and "agent".  Types are user/feedback/project/reference.
    """
    shared: dict[str, int] = defaultdict(int)
    agent: dict[str, int] = defaultdict(int)

    current_bucket: dict[str, int] | None = None

    for line in content.splitlines():
        m = _SECTION_HEADER_RE.match(line.strip())
        if m:
            layer_raw = m.group("layer")
            if layer_raw == "shared":
                current_bucket = shared
            else:  # "agent:…"
                current_bucket = agent
            continue

        if current_bucket is None:
            continue

        # Look for slug references anywhere in the line
        for slug_match in _ENTRY_SLUG_RE.finditer(line):
            slug = slug_match.group(1)
            tm = _SLUG_TYPE_RE.match(slug)
            if tm:
                current_bucket[tm.group(1)] += 1

    return {"shared": dict(shared), "agent": dict(agent)}


def _render_files(file_permissions: dict | None) -> list[str]:
    """Return lines for the Files section, or [] when nothing to render."""
    if not file_permissions:
        return []
    read_paths = file_permissions.get("read") or []
    write_paths = file_permissions.get("write") or []
    if not read_paths and not write_paths:
        return []
    lines: list[str] = []
    if read_paths:
        lines.append(f"read scope:  {', '.join(read_paths)}")
    if write_paths:
        lines.append(f"write scope: {', '.join(write_paths)}")
    return lines
