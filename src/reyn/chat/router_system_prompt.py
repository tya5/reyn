"""System prompt builder for the native tool_use router loop (PR35).

Size is O(categories), independent of item count — Progressive Disclosure
(Lazy Hierarchical Catalog).  Path-level detail is deferred to list_* tools
at runtime.
"""
from __future__ import annotations

import re
from collections import defaultdict

from reyn.chat.router_tools import MAX_DESC_LEN_FOR_LISTING

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
    universal_wrappers_enabled: bool = False,  # FP-0034 PR-3b-v
    cwd: str | None = None,
    search_actions_enabled: bool = True,  # FP-0034 §D14 — default True preserves byte-compat
) -> str:
    """Render the system prompt for the tool_use router loop.

    Returns text matching the structure in the plan's "System prompt 構成"
    section.  Size is O(categories), independent of item count.

    Args:
        agent_name: short identifier of the agent (e.g. "chat").
        agent_role: one-liner from agent profile.
        available_skills: list of dicts with at least ``name``; optional keys
            ``description``, ``routing``, ``category``.
        available_agents: list of dicts with at least ``name``; optional keys
            ``role``, ``cluster``.
        memory_index: ``{"status": "ok"|"not_found", "content": str}``.
        file_permissions: optional ``{"read": [paths], "write": [paths]}``.
            When non-empty (either list), a Files section is rendered.
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
            language based on the user's input naturally. This avoids
            forcing a default (= "ja") onto users who haven't expressed
            a preference.
        indexed_sources_section: pre-rendered "## Indexed sources ..."
            markdown string from ``SourceManifest.format_for_prompt()``.
            When provided, injected verbatim after the Memory section.
            When None (default), no Indexed sources section is emitted
            (= backward compat for callers that have not yet wired up
            the manifest, e.g. tests and non-chat execution paths).
        cwd: current working directory the agent is running from. When
            provided, an ## Environment section tells the LLM to treat
            unqualified references like "this repo" / "this code" /
            "the codebase" as the project at ``cwd``. When None
            (default), the section is omitted — preserves SP byte
            content for tests that don't plumb cwd through.
        search_actions_enabled: Whether ``search_actions`` is included in
            the tool catalogue for this session (= D14 visibility gate:
            ``action_retrieval.embedding_class`` is configured AND the
            ActionEmbeddingIndex is ready). When True (default), the
            Capabilities wrapper enumeration lists all 4 wrappers
            including ``search_actions``. When False, the enumeration
            lists only the 3 always-available wrappers and the
            ``search_actions``-specific Behaviour guidance is omitted.

            Default True preserves byte-identical SP output relative to
            the pre-fix code so existing LLMReplay fixture keys remain
            valid for callers that have not yet wired the flag (e.g.
            FakeRouterHost-based replay tests that don't set
            ``_search_visible``). Production RouterLoop passes
            ``_search_visible`` which is False when no embedding class is
            configured — that is the path that fixes the N5 hallucination.
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
    #
    # Wrapper-only Identity (= radical simplification):
    # The 3-paragraph Reyn explanation that lives in the legacy preamble
    # is content the LLM can fetch via reyn.source__read on demand;
    # baking it into the SP for every turn is wasteful. Keep only the
    # empirically-mitigated parts (vendor identity leak ~50% → near 0)
    # and a single pointer to where the full content lives.
    #
    # B51 NF-W6-4 / W7-S1 fix (2026-05-23): the legacy preamble carried
    # an inline ``invoke_action(reyn.source__read, README.md)`` example.
    # Chain-replay verified that weak-tier LLMs (= flash-lite) parsed
    # that example as "reyn.source__read is directly callable" and
    # emitted the truncated ``source__read({"path":"README.md"})`` (= no
    # wrapper, no namespace prefix) — observed B50/B51 W6-S2 + W7-S1 at
    # 5/5 baseline rate on the "What is Reyn?" prompt class. The fix
    # routes the LLM through the ``## Capabilities (routing guide)``
    # block below, whose intent-2 path already carries the canonical
    # invoke_action recipe in a structured routing context that flash-
    # lite parses correctly (5/5 → 0/5 truncation in the same N=5
    # diagnostic).
    parts.append(
        "# Identity"
        "\n\n"
        "You are a Reyn agent (open-source LLM workflow OS). "
        "To learn the project's runtime, see the Capabilities routing "
        "guide below — the \"About Reyn itself\" path is the canonical entry."
        "\n\n"
        "**Identity rules (always apply):**"
        "\n"
        "- Lead self-descriptions with \"I am a Reyn agent\"."
        "\n"
        "- MUST NOT identify as Google, OpenAI, Anthropic, or any LLM vendor."
        "\n"
        "- MUST NOT begin with \"I am a large language model\"."
    )
    parts.append("")

    # ── 2. Role ─────────────────────────────────────────────────────────────
    parts.append(
        f"Role: chat router for agent {agent_name} (role: {agent_role})."
    )
    parts.append("")

    # ── 3. Capabilities (routing guide) — FP-0023 Change 2 ─────────────────
    # Merges the old "## What you can do (intent axis)" (internal routing
    # labels) and "## When asked what you can do" (user-facing) into one
    # section with a clear internal-vs-user-facing split.
    parts.append("## Capabilities (routing guide)")
    parts.append("")
    # B23-PRE-1 wrapper-only path: tools= contains only the universal
    # wrappers + hot list direct aliases + plan + ask_user. All
    # per-kind tools (list_skills / read_file / web_search / etc.)
    # are routed via invoke_action(action_name="<category>__<entry>").
    # The "## Action categories" section below covers the 14-category
    # taxonomy. Drop the legacy 5-axis "intent" framing — wrapper-only
    # is binary Action / Reply.
    #
    # FP-0034 §D14: ``search_actions`` is only in tools= when the
    # embedding class is configured.  Build the wrapper name list
    # dynamically so the SP count matches the actual tools= shape and
    # the LLM cannot hallucinate a call to a tool that does not exist
    # (N5 empirical finding: gemini-2.5-flash-lite invented
    # search_actions when not in tools= → unknown_tool dispatcher
    # error → gave up without recovering via list_actions).
    # FP-0034 §D14: ``search_actions`` is only in tools= when the
    # embedding class is configured.  Build the wrapper chain dynamically
    # so the SP routing hint matches the actual tools= shape.
    _wrapper_names = ["list_actions"]
    if search_actions_enabled:
        _wrapper_names.append("search_actions")
    _wrapper_names.extend(["describe_action", "invoke_action"])
    _wrapper_chain = " → ".join(_wrapper_names)

    # V18 — 4-intent multi-step routing (replaces the legacy single-line
    # wrapper introduction). Designed around how a human assistant
    # actually disambiguates incoming requests: classify FIRST, then act,
    # and when classification fails honestly, ask back rather than guess.
    #
    # Intent taxonomy:
    #   1. Conversation        → reply without tools
    #   2. Information question → lookup (Reyn docs / external / training)
    #   3. Task / action       → invoke a catalog action
    #   4. Ambiguous           → ask ONE clarifying question
    #
    # Why this shape (= chain-replay experiments documented at
    # docs/deep-dives/journal/dogfood/known-future-challenges.md):
    #   - "About Reyn itself" lives as a sub-case of intent 2, not its
    #     own top-level intent — keeps SP O(1) regardless of how many
    #     Reyn surfaces get added later.
    #   - Multi-step routing is named explicitly so the LLM does not
    #     treat the classification as a single-shot commitment.
    #   - Ambiguous-ask path matches human-assistant baseline; previous
    #     SP shapes never offered this and the LLM defaulted to guessing.
    parts.extend([
        "Decide what the user wants. Multi-step routing is fine — explore"
        " briefly when the right path is uncertain, but don't loop.",
        "",
        "**Conversation** (\"hi\", \"thanks\", \"who are you?\") → reply"
        " directly, no tools.",
        "",
        "**A question with a substantive answer** — figure out where the"
        " answer lives:",
        "- About Reyn itself (how Reyn works, Reyn's CLI / runtime /"
        " protocols / project conventions):"
        " invoke_action(action_name=\"reyn.source__read\","
        " args={\"path\": \"README.md\"}) → synthesize from README."
        " (README has the overview + curated map of deep-dive paths;"
        " chain to a specific doc if README points there.)",
        "- About external / current information: web__search or"
        " web__fetch.",
        "- Already in your training: answer directly.",
        "",
        "**A task to perform** — pick by target shape:",
        "- Single-target action (= one file, one URL, one skill, one"
        " item): if the action is obvious (file__read for \"read this"
        " file\", reyn.source__read for \"open Reyn doc X\", web__fetch"
        " for a specific URL, invoke_action(skill__X) for an explicit"
        " named skill), invoke directly. Otherwise"
        f" {_wrapper_chain}.",
        "- Multi-target / iteration (= \"do X for each Y\", \"process N"
        " files\", \"run X on every Y\"): decompose with plan into"
        " per-target steps + a final aggregate step. Do NOT invoke a"
        " per-target action directly without decomposition — it loses"
        " the iteration shape and gets stuck on the first item.",
        "",
        "**Ambiguous or missing essential information** → ask ONE"
        " clarifying question instead of guessing.",
        "",
    ])

    # ── 3.4. Environment (CWD context, P7-clean) ─────────────────────────────
    # Tells the LLM where it is running so unqualified references like
    # "this repo" / "this code" / "the codebase" / "ここのコード" map to
    # the workspace at cwd. Without this the LLM defaults to its training
    # prior ("please share the repository URL") even when the user is
    # obviously inside a checked-out repo. P7: no skill-specific strings,
    # only environment facts and routing hints to existing categories.
    if cwd:
        parts.append("## Environment")
        parts.append("")
        parts.append(f"cwd: {cwd}")
        parts.append("")
        parts.append(
            "When the user refers to \"this repo\", \"this code\", \"the codebase\","
            " \"this project\", \"ここ\", or any other unqualified reference to"
            " surrounding source, interpret it as the project at the cwd above."
            " Do NOT ask for a repository URL or path — discover the contents"
            " with list_actions(category=['file']) → invoke_action(file__list, ...)"
            " → invoke_action(file__read, ...) within the cwd's read scope."
        )
        parts.append("")

    # ── 3.5. Universal catalog (FP-0034 §D9, opt-in via action_retrieval) ────
    # When the operator has enabled the universal catalog (= reyn.yaml
    # action_retrieval.universal_wrappers_enabled, default True since
    # PR-3b-iv), prepend a category overview so the LLM knows what
    # qualified names list_actions / describe_action / invoke_action
    # produce and consume.
    #
    # Wrapped in a flag check so LLMReplay tests + callers that don't
    # plumb the flag through keep the legacy SP byte content (= fixture
    # keys stay valid).  Production runtime passes the flag from
    # ChatSession → RouterHostAdapter → RouterLoop → here.
    if universal_wrappers_enabled:
        parts.append("## Action categories")
        parts.append("")
        parts.append(
            "Actions are addressed by qualified name (<category>__<entry>). "
            "Discover via list_actions(category=[...]); describe via "
            "describe_action(action_name=...); execute via "
            "invoke_action(action_name=..., args={...})."
        )
        parts.append("")
        parts.append(
            "- **skill** — project-defined workflows (e.g. skill__code_review)."
        )
        parts.append(
            "- **agent.peer** — peer agents in this network (e.g. agent.peer__alice)."
        )
        parts.append(
            "- **mcp** — MCP server management + tool dispatch."
        )
        parts.append(
            "- **file** — workspace file ops (read/write/delete/list)."
        )
        parts.append(
            "- **web** — web search and content fetch."
        )
        parts.append(
            "- **memory.entry** — persistent memory records; invoke to read body."
        )
        parts.append(
            "- **memory.operation** — memory CRUD (remember_shared / remember_agent / forget)."
        )
        parts.append(
            "- **reyn.source** — Reyn source/docs (read-only)."
        )
        parts.append(
            "- **rag.corpus** — indexed corpora; invoke with `query` for single-source recall."
        )
        parts.append(
            "- **rag.operation** — RAG management (multi-source recall, drop_source)."
        )
        parts.append(
            "- **validation** — DSL linting (lint a skill directory and report issues)."
        )
        parts.append(
            "- **exec** — sandboxed argv execution (only when sandbox backend is enabled)."
        )
        parts.append("")
        # Catalog partiality signal: the function list shown to the LLM is a
        # hot-list of frequently-used + seeded actions. The FULL catalog is
        # larger (= every configured MCP server's tools, every project skill,
        # every static op category). Without this signal, the LLM treats the
        # hot-list as the complete inventory and refuses capability requests
        # whose tool is not pre-loaded. Trace-replay verified pre-fix vs
        # post-fix on sqlite + everything MCP servers: pre-fix the LLM
        # refused; post-fix it calls list_actions to discover the rest
        # and follows up with invoke_action.
        parts.append(
            "The function list visible to you is a HOT-LIST (= a subset of "
            "the full catalog). Whenever the user requests a capability and "
            "no listed tool obviously matches, ALWAYS call `list_actions` "
            "(narrow with `category=[...]` when you know the category) to "
            "discover the rest of the catalog BEFORE refusing. Refusing "
            "without that check is a failure mode — the action you assumed "
            "missing often exists."
        )
        parts.append("")

    # ── 4 & 5. Behaviour (static core) ─────────────────────────────────────
    # FP-0023 Change 1: Static Behaviour rules moved here (before dynamic
    # sections) to maximise cache prefix coverage. The two dynamic conditional
    # blocks (output_language, indexed_sources_section) are emitted later,
    # after the dynamic resource sections, since they vary per session/config.
    parts.append("## Behaviour")
    # Wrapper-only: 5 cross-cutting policies (B23-PRE-1 confirmed policy).
    # Per-tool flow details (post-list MUST, post-describe MUST, spawn-ack,
    # task_completed, agent delegation, plan WHAT/WHEN_NOT) live in each
    # tool's description — Anthropic 1-tool-1-purpose pattern.
    # Policies 1-5 are encoded here as SP cross-cutting rules that apply
    # regardless of which tool was most recently called.
    parts.extend([
        # Policy 1: 3-way intent routing (Action / Plan / Reply)
        "  - Domain task → invoke_action (single tool) OR plan (multi-source"
        " synthesis). Chitchat → Reply.",
        # Policy 2: plan routing signal (multi-source)
        "  - Use plan when the query combines info from multiple independent"
        " sources (e.g. \"compare A and B from two docs\", \"explain X with"
        " code refs from N files\", \"summarise across these sources\")."
        " Use invoke_action for single-tool tasks.",
    ])

    # B23-PRE-1 SP role-separation: spawn-ack / task_completed live in invoke_action.description.
    # Only the cross-cutting errors policy remains (= Behaviour policy 5).
    parts.extend([
        "  - Errors MUST surface verbatim. Never narrate an error as success.",
        "    Optimism bias on errors is the single largest router-narration"
        " failure mode.",
    ])

    # ── FP-0025 D — Plan decomposition Behaviour rule ─────────────────────────
    # B23-PRE-1 SP role-separation: ## Plan decomposition subsection (detail)
    # moved to plan.description. The 2-line intent routing already in the
    # wrapper-only Behaviour header (Action/Plan/Reply 3-way) is the SP
    # cross-cutting policy — sufficient.
    parts.append("")
    # Wrapper-only: plan intent routing already encoded in the 3-way
    # header lines above ("Domain task → invoke_action OR plan ...").
    # Also add "never invent action names" (= cross-cutting policy 3).
    # FP-0034 §D14: only reference search_actions in routing guidance
    # when it is actually in tools= (= search_actions_enabled=True).
    # When not available, omit the search_actions signal entirely so the
    # LLM does not attempt to call a tool that does not exist.
    if search_actions_enabled:
        parts.extend([
            "  - Never invent action names; only use those returned by",
            "    list_actions or search_actions.",
            "  - For semantic / natural-language / keyword queries (= 「探し"
            "たい」 「関連」 「something for X」 「similar to」 「'http' を含む」),",
            "    USE search_actions(query=...). For category enumeration,",
            "    USE list_actions(category=[...]).",
        ])
    else:
        parts.extend([
            "  - Never invent action names; only use those returned by",
            "    list_actions.",
            "  - For category enumeration, USE list_actions(category=[...]).",
        ])

    # B12-R2/B13-R3 V3 ABSOLUTE rule preserved in wrapper vocab (1-line,
    # JA examples dropped — per B23-PRE-1 SP simplification policy).
    # P7-compliant placeholder: <action_name> (= qualified name format).
    parts.append("")
    parts.extend([
        "  ROUTING RULE (ABSOLUTE): When the user message contains an action"
        " name (= valid invoke_action action_name, e.g. skill__code_review),"
        " call invoke_action immediately. NO clarifying questions. NO text replies.",
        "",
    ])

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
            "answering questions about this project. Use web_search only as "
            "a supplementary source when project_context lacks the "
            "information needed."
        )
        parts.append("")

    # ── 7 & 8. Skills / Agents catalog ──────────────────────────────────────
    # Wrapper-only path: skill / agent are 2 of the 13 categories
    # listed in the "## Action categories" section above. Dedicated
    # sections would impose a per-category special-case structure
    # that contradicts FP-0034's uniform-invoke design — so they
    # are omitted here. Resource discovery goes through
    # list_actions(category=[...]).

    # ── 9. Memory ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Memory inline section is dropped in the
    # wrapper-only path. Memory discovery goes through
    # list_actions(category=['memory.entry']) at runtime.

    # ── 10. Indexed sources (ADR-0033 UX gap fix A) ──────────────────────────
    # B23-PRE-1 SP role-separation: ## Indexed sources omitted in wrapper-only
    # path — list_actions(category=['rag.corpus']) discovers at runtime.

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
