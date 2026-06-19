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

from reyn.runtime.router_tools import MAX_DESC_LEN_FOR_LISTING

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(
    *,
    agent_name: str,
    agent_role: str,
    available_skills: list[dict],
    available_agents: list[dict],
    memory_index: dict,
    file_permissions: dict | None = None,
    mcp_servers: list[dict] | None = None,
    web_fetch_allowed: bool = True,  # FP-0022: always-on; parameter kept for backward compat
    output_language: str | None = None,
    project_context: str = "",
    indexed_sources_section: str | None = None,
    cwd: str | None = None,
    tool_use_sp: "str | dict[str, str] | None" = None,  # #1627 Stage 4: scheme-owned slot-map (or str back-compat shim); None → {} (bare OS frame, no tool-use SP)
    context_size_signal: str | None = None,  # #272/#1128 — pre-rendered, appended LAST
    environment_info: "dict | None" = None,  # #1479: date/platform/shell/git from get_environment_info()
    scheme_sp_fragment: str = "",  # kept for backward compat; prefer tool_use_sp slot_post_catalog
    reasoning_continuity_section: str = "",  # #1652: pre-rendered prior-reasoning text section ("" = omit, byte-identical)
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
        available_skills: list of dicts with at least ``name``; optional keys
            ``description``, ``routing``, ``category``.
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
        indexed_sources_section: pre-rendered "## Indexed sources ..."
            markdown string from ``SourceManifest.format_for_prompt()``.
            When provided, injected verbatim after the Memory section.
            When None (default), no Indexed sources section is emitted.
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
    parts.append(
        "# Identity"
        "\n\n"
        "You are a Reyn agent (open-source LLM workflow OS). "
        "To learn the project's runtime, see the Capabilities routing "
        "guide below — the \"About Reyn itself\" path is the canonical entry."
        "\n\n"
        "**Identity rules:**"
        "\n"
        "- When asked who or what you are (or otherwise describing yourself), "
        "identify as \"a Reyn agent\". This applies ONLY to identity questions — "
        "do NOT prepend it to answers on unrelated topics. A normal reply must "
        "begin with its actual content, never with \"I am a Reyn agent\"."
        "\n"
        "- Always apply: MUST NOT identify as Google, OpenAI, Anthropic, or any "
        "LLM vendor."
        "\n"
        "- Always apply: MUST NOT begin with \"I am a large language model\"."
    )
    parts.append("")

    # ── 2. Role ─────────────────────────────────────────────────────────────
    parts.append(
        f"Role: chat router for agent {agent_name} (role: {agent_role})."
    )
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
    # the workspace at cwd. P7: no skill-specific strings, only environment
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
                "read the contents using your available actions within the cwd's read scope.",
            )
            parts.append(
                "When the user refers to \"this repo\", \"this code\", \"the codebase\","
                " \"this project\", \"ここ\", or any other unqualified reference to"
                " surrounding source, interpret it as the project at the cwd above."
                " Do NOT ask for a repository URL or path — " + _cwd_how
            )
            parts.append("")

    # ── 3.5. Universal catalog + discovery mandate (R2) ─────────────────────
    # Delivered via slot_post_environment from the scheme's slot-map.
    if "slot_post_environment" in _slots:
        parts.append(_slots["slot_post_environment"])

    # ── 4 & 5. Behaviour (static core) ─────────────────────────────────────
    # FP-0023 Change 1: Static Behaviour rules moved here (before dynamic
    # sections) to maximise cache prefix coverage.
    parts.append("## Behaviour")
    # Cross-cutting rules that apply regardless of which tool was last called.
    # #1627: tool-use routing guidance is scheme-owned (delivered via the slot-map);
    # the OS keeps only these scheme-agnostic behaviour rules here.
    parts.extend([
        "  - Errors MUST surface verbatim. Never narrate an error as success.",
        "    Optimism bias on errors is the single largest router-narration"
        " failure mode.",
        # #1791 A1 (adopted by design judgment): TASK_COMPLETION — anti-fabrication
        # + finish-the-task + honest-blocker. Static-core, all-model, cached.
        "  - Finishing the job: when asked to build, run, or verify something, the"
        " deliverable is a working result backed by REAL tool output — not a"
        " description of one. Do not stop after a stub, a plan, or a single command;"
        " keep working until you have actually produced the requested result, then"
        " report what real execution returned.",
        "  - NEVER substitute fabricated output (invented data, file contents, or"
        " tool/command results) for results you could not actually produce. If a tool"
        " or call fails and blocks the real path, say so directly and try an"
        " alternative — reporting a blocker honestly is always better than inventing"
        " a result.",
    ])

    # #1791 #3 (adopted by design judgment, GATED): memory-quality guidance, rendered
    # ONLY when the memory tool is active (memory_index present) — mirrors Hermes
    # MEMORY_GUIDANCE; gating keeps the cost off non-memory agents (SP-minimize-compatible).
    if memory_index.get("status") == "ok":
        parts.append(
            "  - Memory guidance: save durable facts only (user preferences, recurring"
            " corrections, environment quirks, stable conventions). Do NOT save PR or"
            " issue numbers, commit SHAs, completed-task logs, or anything that will be"
            " stale within a week. Write memories as declarative facts, not instructions"
            " to yourself."
        )

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
    # Sections 6–13: project_context, Memory, Indexed sources,
    # + dynamic Behaviour conditionals (output_language).
    # ==========================================================================

    # ── 6. Project context (REYN.md) ────────────────────────────────────────
    #
    # `project_context` carries the operator's REYN.md content (or whatever
    # `project_context_path` points to). This is operator-editable
    # surface — do NOT use it to inject Reyn's own identity (that's the
    # preamble above). Inject only when non-empty so an unset / empty
    # REYN.md doesn't leak placeholder text into the prompt.
    if project_context.strip():
        parts.append("## About this project (project_context)")
        parts.append("")
        parts.append(project_context.strip())
        parts.append("")
        parts.append(
            "Prefer project_context (above) as the primary source when "
            "answering questions about this project. Use `web__search` only as "
            "a supplementary source when project_context lacks the "
            "information needed."
        )
        parts.append("")

    # ── 7 & 8. Skills / Agents catalog ──────────────────────────────────────
    # Wrapper-only path: skill / agent are 2 of the action categories (now
    # scheme-owned, no longer rendered by the OS). Dedicated sections would
    # impose a per-category special-case structure that contradicts FP-0034's
    # uniform-invoke design — so they are omitted here. (Resource discovery
    # guidance in the catalog-context sections below is a tracked content-layer
    # follow-up — see #1625.)

    # ── 9. Memory ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Memory inline section is dropped in the
    # wrapper-only path. Memory discovery goes through
    # list_actions(category=['memory_entry']) at runtime.

    # ── 10. Indexed sources (ADR-0033 UX gap fix A) ──────────────────────────
    # B23-PRE-1 SP role-separation: ## Indexed sources omitted in wrapper-only
    # path — list_actions(category=['rag_corpus']) discovers at runtime.

    # ── 11. Files ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Files section omitted in wrapper-only
    # path — permission scope communicated via file.* category at runtime.

    # ── 12. MCP servers and tools ────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## MCP servers section omitted in wrapper-
    # only path — list_actions(category=['mcp.server','mcp.tool']) discovers.

    # ── 13. Dynamic Behaviour conditionals ───────────────────────────────────
    # These vary per session config (output_language) or per-request state
    # (indexed_sources_section). They are Behaviour addenda that cannot live
    # in the static prefix above.

    # Explicit language instruction (only when the user configured one):
    # a concrete language tag is stronger than "match the user's language"
    # — the LLM stays in this language even on clarifying-question and
    # error fallback paths (F11). When output_language is None (= user did
    # not configure), we omit the directive entirely so the LLM can pick
    # the reply language based on the user's input naturally instead of
    # being forced into a Reyn default. (Q2 follow-up to F11 fix.)
    if output_language:
        parts.append(
            f"  - Always reply in language: {output_language}."
            "  Do NOT switch language even for error messages or clarifying questions."
        )

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

    # ── 14. Context-size signal (#272/#1128) ─────────────────────────────────
    # OS-injected, pre-rendered by the caller (router_loop / phase runtime) from
    # the live free-window. Placed LAST because it is the most per-turn-volatile
    # section — keeping it at the tail preserves the cached SP prefix above it.
    # P8-clean: OS-level vocabulary, no skill-specific enumeration; the `compact`
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

def _render_skills(available_skills: list[dict]) -> str:
    """Return one-line category summary, or empty string when no skills."""
    counts: dict[str, int] = defaultdict(int)
    for skill in available_skills:
        cat = skill.get("category") or "general"
        counts[cat] += 1

    if not counts:
        return "(none)"

    # Stable sort: alphabetical within category, general first
    ordered = sorted(counts.items(), key=lambda kv: (kv[0] != "general", kv[0]))
    tokens = [f"{cat} ({n})" for cat, n in ordered]
    return " / ".join(tokens)


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


_ENTRY_FULL_RE = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\.md\)\s*[—–-]+\s*(.+)$"
)


def _parse_memory_entries(
    content: str,
) -> dict[str, list[tuple[str, str, str]]]:
    """Return {layer: [(slug, name, description), ...]} from merged memory
    index text. Layers are "shared" and "agent"."""
    shared: list[tuple[str, str, str]] = []
    agent: list[tuple[str, str, str]] = []
    current: list[tuple[str, str, str]] | None = None

    for line in content.splitlines():
        h = _SECTION_HEADER_RE.match(line.strip())
        if h:
            current = shared if h.group("layer") == "shared" else agent
            continue
        if current is None:
            continue
        m = _ENTRY_FULL_RE.match(line)
        if m:
            name, slug, desc = m.group(1), m.group(2), m.group(3).strip()
            current.append((slug, name, desc))
    return {"shared": shared, "agent": agent}


def _render_memory(memory_index: dict) -> list[str]:
    """Return lines for the memory section.

    Inline all entries with their descriptions so the LLM can answer
    recall queries directly without round-tripping through list_memory.
    Memory is bounded per agent (~tens of entries typically) so linear
    rendering is acceptable. read_memory_body remains available for cases
    where the description is too vague to answer from.
    """
    if memory_index.get("status") != "ok":
        return ["shared: (no entries)", "agent: (no entries)"]

    content = memory_index.get("content", "")
    entries = _parse_memory_entries(content)

    lines: list[str] = []
    for layer in ("shared", "agent"):
        layer_entries = entries.get(layer, [])
        if not layer_entries:
            lines.append(f"{layer}: (no entries)")
            continue
        lines.append(f"{layer}:")
        for slug, _name, desc in layer_entries:
            lines.append(f"  - {slug}: {desc}")
    return lines
