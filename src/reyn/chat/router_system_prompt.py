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


def build_universal_tool_use_slots(
    *,
    universal_wrappers_enabled: bool,
    search_actions_enabled: bool,
    discovery_mandate: bool,
    has_hot_list_aliases: bool,
    non_interactive: bool = False,
) -> "dict[str, str]":
    """Build the four positional tool-use SP slots for the universal-category path.

    Called only when ``tool_use_sp`` is None — i.e. the OS owns the full tool-use
    SP construction (universal / enumerate / retrieval today).  Returns a dict with
    ONLY the non-empty slots so ``build_system_prompt`` can inject each with a
    simple ``if slot_key in _slots`` guard.

    Slots:
      - ``slot_pre_environment``  — R1: ``## Capabilities (routing guide)`` block.
      - ``slot_post_environment`` — R2: ``## Action categories`` + hot-list +
                                    discovery-mandate paragraph (between Environment
                                    and ``## Behaviour``).
      - ``slot_in_behaviour``     — R3: never-invent / search guidance + ROUTING RULE
                                    (inside ``## Behaviour``, after the errors line).
      - ``slot_in_environment``   — the cwd-idiom file-discovery HOW clause injected
                                    inside ``## Environment``.
      - ``slot_post_catalog``     — scheme-owned SP appended at the post-catalog
                                    position (e.g. retrieval's search guidance),
                                    before the context-size signal (#1627 Stage 3).

    Each slot value equals ``"\\n".join(<elements>)`` where ``<elements>`` is the
    exact list that the corresponding inline region would have appended to ``parts``
    — char-identical by construction.

    ``str`` ⇒ ``{"slot_pre_environment": str}`` (back-compat shim for a bare
    replacement string; used internally by ``build_system_prompt``).
    """
    slots: dict[str, str] = {}

    # ── R1: ## Capabilities (routing guide) ──────────────────────────────────
    # Mirrors the inline build from _cap_start through parts.extend([..., ""])
    # in build_system_prompt.  The wrapper-chain and _otherwise construction is
    # duplicated verbatim so the slot content is char-identical.
    _wrapper_names_slot = ["`list_actions`"]
    if search_actions_enabled:
        _wrapper_names_slot.append("`search_actions`")
    _wrapper_names_slot.extend(["`describe_action`", "`invoke_action`"])
    _wrapper_chain_slot = " → ".join(_wrapper_names_slot)

    if discovery_mandate:
        _otherwise_slot = (
            "Otherwise — i.e. for any action that is NOT obvious or a named "
            "skill above — your FIRST tool call MUST be `list_actions` before "
            "reading, writing, or editing anything (the visible tools are "
            "universal wrappers, not the full catalog; do NOT skip it, refuse, "
            f"or guess). Then {_wrapper_chain_slot}. To edit a file you MUST use "
            "`file__edit`, found via `list_actions`."
        )
    else:
        _otherwise_slot = f"Otherwise {_wrapper_chain_slot}."

    _r1: list[str] = []
    _r1.append("## Capabilities (routing guide)")
    _r1.append("")
    _r1.extend([
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
        " `invoke_action(action_name=\"reyn_source__read\","
        " args={\"path\": \"README.md\"})` → synthesize from README."
        " (README has the overview + curated map of deep-dive paths;"
        " chain to a specific doc if README points there.)",
        "- About external / current information: `web__search` or"
        " `web__fetch`.",
        "- Already in your training: answer directly.",
        "",
        "**A task to perform** — pick by target shape:",
        "- Single-target action (= one file, one URL, one skill, one"
        " item): if the action is obvious (`file__read` for \"read this"
        " file\", `reyn_source__read` for \"open Reyn doc X\", `web__fetch`"
        " for a specific URL, `invoke_action`(`skill__X`) for an explicit"
        " named skill), invoke directly. " + _otherwise_slot,
        "- Multi-target / iteration (= \"do X for each Y\", \"process N"
        " files\", \"run X on every Y\"): decompose with plan into"
        " per-target steps + a final aggregate step. Do NOT invoke a"
        " per-target action directly without decomposition — it loses"
        " the iteration shape and gets stuck on the first item.",
        "",
        (
            "**Ambiguous or missing essential information** → there is no"
            " interactive user to ask; state your best assumption and proceed"
            " (do NOT stop to ask a clarifying question)."
            if non_interactive else
            "**Ambiguous or missing essential information** → ask ONE"
            " clarifying question instead of guessing."
        ),
        "",
    ])
    slots["slot_pre_environment"] = "\n".join(_r1)

    # ── R2: ## Action categories + hot-list + discovery-mandate ──────────────
    # Mirrors the universal_wrappers_enabled block and the discovery_mandate
    # paragraph that sit between ## Environment and ## Behaviour.
    # Inside this builder tool_use_sp is always None, so the
    # ``discovery_mandate and tool_use_sp is None`` guard simplifies to
    # ``discovery_mandate``.
    _r2: list[str] = []
    if universal_wrappers_enabled:
        _r2.append("## Action categories")
        _r2.append("")
        _r2.append(
            "Actions are addressed by qualified name (`<category>__<entry>`)."
            " Names in backticks of the form `<category>__<entry>` are invocable action names."
            " Discover via `list_actions(category=[...])`; describe via"
            " `describe_action(action_name=...)`; execute via"
            " `invoke_action(action_name=..., args={...})`."
        )
        _r2.append("")
        _r2.append(
            "- **skill** — project-defined workflows (e.g. skill__code_review)."
        )
        _r2.append(
            "- **multi_agent** — delegate / list / describe peer agents in this network."
        )
        _r2.append(
            "- **mcp** — MCP server management + tool dispatch."
        )
        _r2.append(
            "- **file** — workspace file ops (read/write/delete/list)."
        )
        _r2.append(
            "- **web** — web search and content fetch."
        )
        _r2.append(
            "- **memory_entry** — persistent memory records; invoke to read body."
        )
        _r2.append(
            "- **memory_operation** — memory CRUD (remember_shared / remember_agent / forget)."
        )
        _r2.append(
            "- **reyn_source** — Reyn source/docs (read-only)."
        )
        _r2.append(
            "- **rag_corpus** — indexed corpora; invoke with `query` for single-source recall."
        )
        _r2.append(
            "- **rag_operation** — RAG management (multi-source recall, drop_source)."
        )
        _r2.append(
            "- **validation** — DSL linting (lint a skill directory and report issues)."
        )
        _r2.append(
            "- **exec** — sandboxed argv execution (only when sandbox backend is enabled)."
        )
        _r2.append("")
        if has_hot_list_aliases:
            _r2.append(
                "The function list visible to you is a HOT-LIST (= a subset of "
                "the full catalog). Whenever the user requests a capability and "
                "no listed tool obviously matches, ALWAYS call `list_actions` "
                "(narrow with `category=[...]` when you know the category) to "
                "discover the rest of the catalog BEFORE refusing. Refusing "
                "without that check is a failure mode — the action you assumed "
                "missing often exists."
            )
            _r2.append("")
    if discovery_mandate:
        _r2.append(
            "When no visible tool obviously matches the action you need, "
            "calling list_actions is MANDATORY and comes FIRST — before any "
            "read, write, or edit. Treat the visible list as a subset, never "
            "as complete."
        )
        _r2.append("")
    if _r2:
        slots["slot_post_environment"] = "\n".join(_r2)

    # ── R3: never-invent / search guidance + ROUTING RULE ────────────────────
    # Mirrors the search-guidance block and the ROUTING RULE block inside
    # ## Behaviour (appended after the errors-verbatim lines + blank).
    # Inside this builder tool_use_sp is always None, so:
    #   - ``if tool_use_sp is not None: pass`` → branch never taken.
    #   - ``if tool_use_sp is None: ROUTING RULE`` → always included.
    _r3: list[str] = []
    if search_actions_enabled:
        _r3.extend([
            "  - Never invent action names; only use those returned by",
            "    `list_actions` or `search_actions`.",
            "  - For semantic / natural-language / keyword queries (= 「探し"
            "たい」 「関連」 「something for X」 「similar to」 「'http' を含む」),",
            "    USE `search_actions(query=...)`. For category enumeration,",
            "    USE `list_actions(category=[...])`.",
        ])
    else:
        _r3.extend([
            "  - Never invent action names; only use those returned by",
            "    `list_actions`.",
            "  - For category enumeration, USE `list_actions(category=[...])`.",
        ])
    _r3.append("")
    _r3.extend([
        "  ROUTING RULE (ABSOLUTE): When the user message contains an action"
        " name (= valid `invoke_action` action_name, e.g. `skill__code_review`),"
        " call `invoke_action` immediately. NO clarifying questions. NO text replies.",
        "",
    ])
    slots["slot_in_behaviour"] = "\n".join(_r3)

    # ── R4: cwd-idiom file-discovery HOW clause (slot_in_environment) ────────
    # Scheme-owned HOW tail for the ## Environment cwd block.  The OS keeps only
    # the generic neutral default; universal schemes deliver the list_actions
    # idiom here so the OS stays P7-clean (no scheme-specific strings).
    slots["slot_in_environment"] = (
        "discover the contents with `list_actions(category=['file'])` →"
        " `invoke_action(file__list, ...)` → `invoke_action(file__read, ...)`"
        " within the cwd's read scope."
    )

    return slots


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
    scheme_sp_fragment: str = "",  # #1593 — free-form scheme-owned tool-use SP; OS appends verbatim (P7), default "" = byte-identical named-gate path
    tool_use_sp: "str | None" = None,  # #1618 root-3 — scheme REPLACEMENT for the tool-use SP region; None = OS builds today's (byte-identical), non-None = inject + skip the universal tool-use construction
    context_size_signal: str | None = None,  # #272/#1128 — pre-rendered, appended LAST
    discovery_mandate: bool = False,  # #187 Stage C — weak-tier list_actions-first mandate (3x)
    non_interactive: bool = False,  # #1439 Fix #1 — run-once (no TTY): no user to ask, proceed instead of clarifying
    has_hot_list_aliases: bool = False,  # True when hot_list_n>0 produced direct-alias functions
    environment_info: "dict | None" = None,  # #1479: date/platform/shell/git from get_environment_info()
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
    # is content the LLM can fetch via reyn_source__read on demand;
    # baking it into the SP for every turn is wasteful. Keep only the
    # empirically-mitigated parts (vendor identity leak ~50% → near 0)
    # and a single pointer to where the full content lives.
    #
    # B51 NF-W6-4 / W7-S1 fix (2026-05-23): the legacy preamble carried
    # an inline ``invoke_action(reyn_source__read, README.md)`` example.
    # Chain-replay verified that weak-tier LLMs (= flash-lite) parsed
    # that example as "reyn_source__read is directly callable" and
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

    # #1627 Stage 0: normalise tool_use_sp into a positional slot-map.
    # ``str`` ⇒ back-compat shim (= slot_pre_environment only; R2/R3 absent).
    # ``None`` ⇒ the OS builds all three slots via build_universal_tool_use_slots.
    # ``dict`` ⇒ already a slot-map (future schemes; not used in Stage 0).
    if isinstance(tool_use_sp, str):
        _slots: "dict[str, str]" = {"slot_pre_environment": tool_use_sp}
    elif tool_use_sp is None:
        _slots = build_universal_tool_use_slots(
            universal_wrappers_enabled=universal_wrappers_enabled,
            search_actions_enabled=search_actions_enabled,
            discovery_mandate=discovery_mandate,
            has_hot_list_aliases=has_hot_list_aliases,
            non_interactive=non_interactive,
        )
    else:
        _slots = tool_use_sp  # type: ignore[assignment]  # dict path (Stage 1+)

    # ── 3. Capabilities (routing guide) — FP-0023 Change 2 ─────────────────
    # #1627 Stage 0: the ## Capabilities region (R1) is now delivered via
    # slot_pre_environment.  ``None``-path: OS-built by build_universal_tool_use_slots
    # (char-identical).  ``str``-path: scheme replacement injected verbatim
    # (back-compat shim = byte-identical to the old parts[_cap_start:] = [tool_use_sp]).
    if "slot_pre_environment" in _slots:
        parts.append(_slots["slot_pre_environment"])

    # ── 3.4. Environment (CWD context, P7-clean) ─────────────────────────────
    # Tells the LLM where it is running so unqualified references like
    # "this repo" / "this code" / "the codebase" / "ここのコード" map to
    # the workspace at cwd. Without this the LLM defaults to its training
    # prior ("please share the repository URL") even when the user is
    # obviously inside a checked-out repo. P7: no skill-specific strings,
    # only environment facts and routing hints to existing categories.
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
            # #1627 Stage 1: the cwd semantic mapping ("this repo" → the project at
            # cwd) is OS-level and kept for every scheme.  The HOW clause (how to
            # discover file contents) is scheme-owned — delivered via
            # ``slot_in_environment`` (set unconditionally by
            # build_universal_tool_use_slots, absent from the CodeAct str-shim).
            # OS keeps only the generic neutral default; the slot overrides it for
            # universal schemes (P7-clean: no isinstance / shape-check on slot-map).
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
    # #1627 Stage 0: the ## Action categories block and discovery-mandate
    # paragraph are delivered via slot_post_environment (built by
    # build_universal_tool_use_slots on the None-path; absent on the str-path).
    if "slot_post_environment" in _slots:
        parts.append(_slots["slot_post_environment"])

    # ── 4 & 5. Behaviour (static core) ─────────────────────────────────────
    # FP-0023 Change 1: Static Behaviour rules moved here (before dynamic
    # sections) to maximise cache prefix coverage. The two dynamic conditional
    # blocks (output_language, indexed_sources_section) are emitted later,
    # after the dynamic resource sections, since they vary per session/config.
    parts.append("## Behaviour")
    # Cross-cutting rules that apply regardless of which tool was last called.
    # Routing decisions (intent tree / plan vs invoke / discovery mandate) live
    # in ## Capabilities above — single canonical location, no repetition here.
    # B23-PRE-1 SP role-separation: spawn-ack / task_completed live in invoke_action.description.
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
    # ── R3: never-invent / search guidance + ROUTING RULE ────────────────────
    # #1627 Stage 0: delivered via slot_in_behaviour (built by
    # build_universal_tool_use_slots on the None-path; absent on the str-path).
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
    # Wrapper-only path: skill / agent are 2 of the 13 categories
    # listed in the "## Action categories" section above. Dedicated
    # sections would impose a per-category special-case structure
    # that contradicts FP-0034's uniform-invoke design — so they
    # are omitted here. Resource discovery goes through
    # list_actions(category=[...]).

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
