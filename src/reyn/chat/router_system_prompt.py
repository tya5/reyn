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
    hide_legacy_tools: bool = False,  # FP-0034 B23-PRE-1 (Phase 4 preview)
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
        mcp_servers: optional list of ``{"name": ..., "description": ...}``.
            When non-empty, an MCP servers section is rendered.
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
        hide_legacy_tools: B23-PRE-1 / Phase 4 preview. When True, render
            the SP for wrapper-only e2e: drop per-kind tool enumerations
            (list_skills / invoke_skill / read_file / delegate_to_agent /
            etc.), simplify Capabilities to point at universal wrappers,
            collapse intent axes (Memory access / Save / Forget) into
            invoke_action paths, remove the ``## Skills`` / ``## Agents``
            catalog sections (Action categories now covers them), and
            rewrite Agent delegation + spawn-ack + ABSOLUTE rule wording
            to ``invoke_action`` / ``<action_name>``. Default False keeps
            the legacy SP byte-identical (= 0 LLMReplay fixture re-records,
            17 hard string pins preserved verbatim). Only meaningful when
            ``universal_wrappers_enabled`` is also True (= the LLM must
            have the wrapper tools available); when False, the legacy
            per-kind tools must remain in tools= so this flag is ignored.
    """
    skill_section = _render_skills(available_skills)
    agent_section = _render_agents(available_agents)
    memory_section = _render_memory(memory_index)
    file_section = _render_files(file_permissions)
    mcp_section = _render_mcp(mcp_servers, hide_legacy_tools=hide_legacy_tools)

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
    # Without this, an agent with an empty `role:` falls back to the
    # underlying LLM's baseline identity ("I am a large language model
    # trained by Google", etc.) when asked "tell me about yourself" — a
    # devastating first-touch experience that breaks the Reyn brand.
    # This preamble is OS-provided (not user-editable, distinct from
    # `project_context` / REYN.md which is operator-owned) and tells the
    # agent what Reyn is so identity questions get a meaningful reply.
    #
    # Wording-strength note: weak LLMs (= gemini-2.5-flash-lite class)
    # have a strong "tell me about yourself" attractor toward
    # vendor-of-origin replies even with a Reyn-identity preamble in
    # front. The explicit MUST NOT lines below are the empirical
    # mitigation that pushes the rate from ~50% leak to near-zero on
    # this attractor. Keep the imperative form; soft phrasing
    # ("you are Reyn — not the vendor") was insufficient.
    if hide_legacy_tools:
        # Wrapper-only Identity (= radical simplification):
        # The 3-paragraph Reyn explanation that lives in the legacy preamble
        # is content the LLM can fetch via reyn.source__read on demand;
        # baking it into the SP for every turn is wasteful. Keep only the
        # empirically-mitigated parts (vendor identity leak ~50% → near 0)
        # and a single pointer to where the full content lives.
        parts.append(
            "# Identity"
            "\n\n"
            "You are a Reyn agent (open-source LLM workflow OS). "
            "For details: invoke_action(action_name=\"reyn.source__read\", "
            "args={\"path\": \"README.md\"})."
            "\n\n"
            "**Identity rules (always apply):**"
            "\n"
            "- Lead self-descriptions with \"I am a Reyn agent\"."
            "\n"
            "- MUST NOT identify as Google, OpenAI, Anthropic, or any LLM vendor."
            "\n"
            "- MUST NOT begin with \"I am a large language model\"."
        )
    else:
        parts.append(
            "# Identity"
            "\n\n"
            "You are a Reyn agent — running on the Reyn agent OS, an "
            "open-source LLM workflow runtime that constrains the model with "
            "explicit phase graphs, JSON-schema-validated outputs, and per-"
            "skill permission scopes. Reyn's design value is predictability: "
            "every run is replayable from an append-only event log, every "
            "decision goes through a typed contract. The user is talking to "
            "you via Reyn's chat interface."
            "\n\n"
            "**Identity rules (always apply):**"
            "\n"
            "- When the user asks who you are or what you are, you ARE a Reyn "
            "agent. Lead the answer with that fact."
            "\n"
            "- You MUST NOT identify yourself as a model from Google, OpenAI, "
            "Anthropic, or any other LLM vendor. The underlying model is an "
            "implementation detail; the user is not asking about it."
            "\n"
            "- You MUST NOT begin a self-description with phrases like "
            "\"I am a large language model\" — start with \"I am a Reyn "
            "agent\" instead."
            "\n\n"
            "**Explaining Reyn (deep-dive entry point):**"
            "\n"
            "- When the user asks how Reyn works or wants to understand any "
            "part of Reyn's design / concepts / implementation: FIRST check "
            "the 'Indexed sources' section below. If an indexed source's "
            "description mentions concepts / design / docs / architecture / "
            "Reyn, use the `recall` tool with that source — semantic search "
            "across indexed chunks is the right answer for 'what is X?', "
            "'explain X', 'how does X work?' style questions when an indexed "
            "source covers the topic."
            "\n"
            "- ONLY if no indexed source covers Reyn (= the 'Indexed sources' "
            "section is absent / empty / unrelated topics): fall back to "
            "`reyn_src_read('README.md')` for an overview and curated map of "
            "paths under `reyn_src_*` (architecture, skill DSL, source code, "
            "ADRs)."
            "\n"
            "- Do NOT reach for `web_search` to learn about Reyn — `recall` "
            "(when indexed) or `reyn_src_*` (otherwise) is the authoritative "
            "source. Web search is for things outside Reyn."
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
    # Risk mitigated: internal labels leaking into user replies is prevented
    # by the explicit "do NOT use these labels in user replies" directive.
    # The user_capabilities list is still built dynamically below (section 13)
    # and referenced here so the prompt stays consistent.
    has_file_read = bool(
        file_permissions and file_permissions.get("read")
    )
    has_file_write = bool(
        file_permissions and file_permissions.get("write")
    )
    has_mcp = bool(mcp_servers)

    parts.append("## Capabilities (routing guide)")
    parts.append("")
    if hide_legacy_tools:
        # B23-PRE-1 wrapper-only path: tools= contains only the 4 universal
        # wrappers + hot list direct aliases + plan + ask_user. All
        # per-kind tools (list_skills / read_file / web_search / etc.)
        # are routed via invoke_action(action_name="<category>__<entry>").
        # The "## Action categories" section below covers the 13-category
        # taxonomy. Drop the legacy 5-axis "intent" framing — wrapper-only
        # is binary Action / Reply.
        parts.extend([
            "4 universal wrappers: list_actions / search_actions / describe_action"
            " / invoke_action. Frequent actions also appear as direct aliases in"
            " tools list — call them directly when you know the exact qualified name.",
            "",
            "For chitchat or self-questions, reply without tools.",
            "",
        ])
    else:
        parts.append(
            "Internal routing axes — do NOT use these labels in user replies:"
        )
        parts.append("- Action — run external work")
        parts.append(
            "           skills:  list_skills / describe_skill / invoke_skill"
        )
        parts.append(
            "           agents:  list_agents / describe_agent / delegate_to_agent"
        )
        if has_file_read:
            parts.append(
                "           files:   list_directory / read_file"
            )
        # `reyn_src_*` is always present — it serves Reyn's own source/docs,
        # not user files, and so has no permission-protected content. Used
        # for "explain how Reyn works" / "summarize Reyn's README" queries.
        parts.append(
            "           reyn:    reyn_src_list / reyn_src_read"
        )
        # FP-0022: web_fetch is now always in the catalog; approval via 4-layer
        # PermissionResolver at handler level. web_search is Tier 1 read-only.
        parts.append(
            "           web:     web_search / web_fetch"
        )
        if has_mcp:
            parts.append(
                "           mcp:     list_mcp_servers / list_mcp_tools / call_mcp_tool / describe_mcp_tool"
            )
        # NOTE: renamed from "Recall" to "Memory access" (B17-S5-3 fix) to avoid
        # vocabulary collision with the `recall` indexed-search tool (ADR-0033).
        # The word "recall" in user input must map to the `recall` tool, not here.
        parts.append("- Memory access — read persisted facts (= memory, NOT indexed sources)")
        parts.append("           tools: list_memory / read_memory_body")
        parts.append("- Save — persist new facts")
        if has_file_write:
            parts.append(
                "         tools: remember_shared / remember_agent / write_file"
            )
        else:
            parts.append("         tools: remember_shared / remember_agent")
        parts.append("- Forget — delete persisted facts")
        if has_file_write:
            parts.append("           tools: forget_memory / delete_file")
        else:
            parts.append("           tools: forget_memory")
        parts.append("- Reply — answer directly (no tool)")
        parts.append("")
        parts.append(
            "When a user asks what you can do, answer in plain user-facing terms —"
        )
        parts.append(
            "never with the routing labels (Action / Memory access / Save / Forget / Reply)."
        )
        parts.append(
            'Honest answer: "I can run skills, search your documents, remember things."'
        )
        parts.append(
            "Do NOT say \"Your intent is Action\" or use any routing label in replies."
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
            "- **mcp.server** — MCP server resources; invoke to list this server's tools."
        )
        parts.append(
            "- **mcp.tool** — individual MCP tools (mcp.tool__<server>.<tool>)."
        )
        parts.append(
            "- **mcp.operation** — MCP server management ops (e.g. drop_server)."
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
            "- **exec** — sandboxed argv execution (only when sandbox backend is enabled)."
        )
        parts.append("")
        if not hide_legacy_tools:
            parts.append(
                "Unknown action_name returns an error with suggestions — "
                "use list_actions(category=[...]) to discover the correct name."
            )
            parts.append("")

    # ── 4 & 5. Behaviour (static core) ─────────────────────────────────────
    # FP-0023 Change 1: Static Behaviour rules moved here (before dynamic
    # sections) to maximise cache prefix coverage. The two dynamic conditional
    # blocks (output_language, indexed_sources_section) are emitted later,
    # after the dynamic resource sections, since they vary per session/config.
    #
    # Audit result (FP-0023 Change 1 pre-check):
    #   Truly static: intent-decision rule, skill routing bullets 1–4,
    #   memory-access rule, spawn-ack block, task_completed narration,
    #   parallel/sequential tool_calls rule, never-invent rule,
    #   memory-writes rule, delegate_to_agent rule (Change 4, new),
    #   plan decomposition rule (FP-0025 D, new), ABSOLUTE ROUTING RULE.
    #   Catalog-dependent (must stay dynamic / after dynamic):
    #     - output_language conditional
    #     - indexed_sources_section conditional (recall disambiguation + JA
    #       examples from Change 5)
    parts.append("## Behaviour")
    if hide_legacy_tools:
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
    else:
        parts.append(
            "  - First decide intent (Action / Memory access / Save / Forget / Reply),"
        )
        parts.append("    then pick tools from that group.")
    # Behaviour rules — re-balanced from B5-H1 partial revert of e90c0f2.
    # History: F3+F9 (batch 1) added reply restriction + explicit-skill hint;
    # B2-H1 (batch 2) added post-describe_skill commit obligation;
    # B3-H1+M3 (batch 3) added post-list_skills commit obligation.
    # e90c0f2 over-consolidated to 2 rules; weak LLM (gemini-2.5-flash-lite)
    # de-prioritised multi-sentence MUSTs inside a single bullet → B5-H1
    # regression (specialist empty reply after list_skills).
    # Fix: restore individual bullets (1 bullet = 1 MUST) per feedback_prompt_design.
    # "engage the skill ecosystem" jargon removed; duplicate list+invoke hints merged.
    #
    # B9-NEW-3 / B10-NEW-2 fix (B11-R3): text-reply non-determinism.
    # Root cause: weak LLM classified Japanese multi-verb input ("review して改善案を出して")
    # as "requires clarification" (Reply intent) instead of Action, even when the
    # skill name is explicitly visible in the Available skills list above.
    # Structural fix (per feedback_reyn_care_boundary: pre-call structural environment):
    #   1. When skill name is in Available skills, allow direct invoke_skill (skip list_skills).
    #      The mandatory list_skills hop created an extra decision round that weak LLMs
    #      exploited to fall through to Reply intent.
    #   2. Explicit rule: additional entity names in the message are skill inputs, not
    #      clarification triggers. Prevents the "but I need more info" text-reply escape.
    #   3. Tighten Reply restriction: "clarifications back to the user" is permitted ONLY
    #      when no skill name from the Available skills list appears in the user message.
    #
    # B23-PRE-1 SP role-separation: wrapper-only path emits only the 5 cross-
    # cutting policies. Per-tool routing bullets (post-list MUST, post-describe
    # MUST, direct-invoke hint, memory-access rule) moved to tool descriptions.
    if not hide_legacy_tools:
        # Bullet 1 (F3+F9 — chitchat restriction, domain → Action):
        parts.append(
            "  - Reply directly only for chitchat and questions about yourself."
        )
        parts.append(
            "    Domain tasks → Action. Do NOT ask clarifying questions if the user"
        )
        parts.append(
            "    message contains a skill name (= valid invoke_skill enum value)."
        )
        # Bullet 2 (F3+F9+B3-H1+M3+B11-R3 — explicit-skill direct path):
        # Post category-only retry (2026-05-07): inline skill list removed,
        # now refers to `invoke_skill.name` enum which is the structural source
        # of truth. LLM sees the enum in the tool schema.
        parts.append(
            "  - If the user names a skill (= matches invoke_skill's name enum),"
        )
        parts.append(
            "    call invoke_skill directly (skip list_skills). Any other entities"
        )
        parts.append(
            "    in the user message are inputs to the skill, NOT reasons to clarify."
        )
        # Bullet 2b (discovery path when skill name is unknown):
        parts.append(
            "  - If the user describes a domain task without naming a skill,"
        )
        parts.append(
            "    call list_skills(path) first to discover, then invoke_skill."
        )
        # Bullet 3 (B3-H1+M3 — post-list_skills MUST):
        parts.append(
            "  - After list_skills reveals at least one matching skill, you MUST"
        )
        parts.append(
            "    call describe_skill or invoke_skill. Do NOT reply directly."
        )
        # Bullet 4 (B2-H1 — post-describe_skill MUST):
        parts.append(
            "  - After describe_skill, you MUST call invoke_skill or explain in text"
        )
        parts.append("    why not; never stop silently after investigation.")
        parts.append(
            "  - For Memory access, answer from the Memory section's inlined descriptions;"
        )
        parts.append(
            "    use read_memory_body only when a description is too vague."
        )
        parts.append(
            "  - (list_memory is available for hierarchical browsing if needed.)"
        )
    # ── invoke_skill: spawn-ack + completion narration (FP-0012) ───────────────
    # Skills now run asynchronously in the background. invoke_skill returns
    # ``{status: "spawned", run_id, chain_id, note}`` IMMEDIATELY (= the task
    # is running but the result isn't here yet). When the skill finishes the
    # OS injects a ``[task_completed]`` user message into the conversation
    # carrying the structured result; that's the LLM's cue to narrate.
    #
    # The anti-optimism rule (originally FP-0011 Component B) is preserved on
    # the completion side: errors MUST be surfaced verbatim, never narrated
    # as success.
    #
    # FP-0023 Change 3: Numbered priority block replaces flat MUST list.
    # Dogfood shows /tasks compliance was most fragile with flat listing.
    # Explicit Priority 1 / Priority 2 / ... concentrates LLM attention on
    # the non-negotiable constraint (= /tasks link) before the secondary ones.
    #
    # 2026-05-11 N=10 retest findings (= R-SP-TASKS-POINTER-MUST +
    # R-SP-NO-FABRICATE-AT-SPAWN-ACK):
    #   - Soft "Mention /tasks" wording produced only 3/60 (= 5%) compliance.
    #     Strengthened to MUST so the LLM cannot omit it from the spawn-ack
    #     reply — `/tasks` is the user's only affordance to inspect in-flight
    #     work, so omitting it strands them.
    #   - 1/10 mcp-search shots fabricated server details (names + URLs) AT
    #     spawn-ack time, before the skill had executed. Added a peer
    #     anti-fabrication rule to the existing anti-optimism rule below:
    #     the spawn-ack carries no skill output, so any field that would come
    #     from the skill's result is by definition fabricated if the LLM emits
    #     it now.
    #
    # B23-PRE-1 SP role-separation: spawn-ack Priority 1-4 block and
    # task_completed narration have been moved to invoke_action.description
    # (SPAWN-ACK HANDLING / TASK_COMPLETED HANDLING sections) per Anthropic
    # 1-tool-1-purpose pattern. The SP Behaviour cross-cutting policy for
    # errors survives as policy 5 ("Errors must surface verbatim").
    if hide_legacy_tools:
        # Wrapper-only: spawn-ack / task_completed live in invoke_action.description.
        # Only the cross-cutting errors policy remains (= Behaviour policy 5).
        parts.extend([
            "  - Errors MUST surface verbatim. Never narrate an error as success.",
            "    Optimism bias on errors is the single largest router-narration"
            " failure mode.",
        ])
    else:
        parts.append(
            "  - When invoke_skill returns {status: \"spawned\", chain_id, run_id, note}:"
        )
        parts.append("    the skill is running in the background.")
        parts.append("")
        parts.append(
            "    Priority 1 (non-negotiable): Your reply MUST include `/tasks` as the"
        )
        parts.append(
            "      user's way to check progress. This is non-negotiable — the user has no"
        )
        parts.append(
            "      other way to track in-flight tasks. Omitting `/tasks` from the"
        )
        parts.append(
            "      spawn-ack reply is a hard failure."
        )
        parts.append(
            "    Priority 2: Keep your reply to 1–2 sentences. You MUST NOT pre-fill"
        )
        parts.append(
            "      the user with information the skill is supposed to produce."
        )
        parts.append(
            "      The spawn-ack envelope carries ONLY {status, run_id, chain_id, note} —"
        )
        parts.append(
            "      no results, no names, no URLs, no scores, no fields from the skill's"
        )
        parts.append(
            "      output schema. Any such content in the spawn-ack reply is"
        )
        parts.append(
            "      fabrication by construction (the skill has not executed)."
        )
        parts.append(
            "    Priority 3: Do NOT call invoke_skill again for the same request (it's already"
        )
        parts.append("      running).")
        parts.append(
            "    Priority 4: Do NOT ask follow-up questions while the skill is running;"
        )
        parts.append("      wait for the [task_completed] message.")
        parts.append("")
        parts.append(
            "  - When you see a user message starting with [task_completed]: a"
        )
        parts.append(
            "    background skill finished. Read the `status` and `result` fields"
        )
        parts.append(
            "    from that message and narrate in 1-2 sentences. Extract the"
        )
        parts.append(
            "    user-relevant fields — do not echo the raw JSON. Status guidance:"
        )
        parts.append(
            '      * "finished"             — confirm completion; if applicable, hint at the next step.'
        )
        parts.append(
            '      * "loop_limit_exceeded"  — say the skill ran out of phase budget; suggest re-running'
        )
        parts.append(
            "        with higher safety.loop.max_phase_visits."
        )
        parts.append(
            '      * "error" / any non-"finished" status, OR result.error is present —'
        )
        parts.append(
            "        your reply MUST surface the specific error verbatim. Do NOT"
        )
        parts.append(
            "        narrate as success. Quote the error message in user-friendly"
        )
        parts.append(
            "        form (translate to output_language if set, but keep the failure"
        )
        parts.append(
            "        signal explicit) and suggest the most likely fix. Optimism bias"
        )
        parts.append(
            "        on errors is the single largest router-narration failure mode."
        )
    # ── FP-0023 Change 4 — delegate_to_agent Behaviour rule ─────────────────
    # delegate_to_agent appeared in the tool list but had no Behaviour rule.
    # Vendor prompt-writing guides (Anthropic, OpenAI) advise placing usage
    # guidance in the SP, not only in the tool schema. Added here alongside
    # the invoke_skill rules so delegation has the same structural support.
    #
    # B23-PRE-1 SP role-separation: ## Agent delegation subsection moved to
    # invoke_action.description (AGENT DELEGATION section). Legacy path
    # preserved byte-identical.
    parts.append("")
    if not hide_legacy_tools:
        parts.append("  ## Agent delegation")
        parts.append("")
        parts.append("  When a user task requires a peer agent (not a skill):")
        parts.append(
            "    call delegate_to_agent(to=<agent_name>, request=<user_query>)"
        )
        parts.append("")
        parts.append("  Use this when:")
        parts.append(
            "    - The task is outside available skills but matches a peer agent's role"
        )
        parts.append("    - The user explicitly addresses a named agent")
        parts.append("")
        parts.append(
            "  Do NOT delegate tasks that can be solved with available skills."
        )
        parts.append(
            "  Acknowledge the delegation in 1 sentence."
        )
    # ── FP-0025 D — Plan decomposition Behaviour rule ─────────────────────────
    # plan tool previously relied solely on its schema description for when-to-use
    # guidance. No Behaviour rule reinforced or constrained this, unlike
    # invoke_skill and delegate_to_agent. Added here for parity.
    #
    # B23-PRE-1 SP role-separation: ## Plan decomposition subsection (detail)
    # moved to plan.description. The 2-line intent routing already in the
    # wrapper-only Behaviour header (Action/Plan/Reply 3-way) is the SP
    # cross-cutting policy — sufficient. Legacy path preserved byte-identical.
    parts.append("")
    if hide_legacy_tools:
        # Wrapper-only: plan intent routing already encoded in the 3-way
        # header lines above ("Domain task → invoke_action OR plan ...").
        # Also add "never invent action names" (= cross-cutting policy 3).
        parts.extend([
            "  - Never invent action names; only use those returned by",
            "    list_actions or search_actions.",
            "  - For semantic / natural-language queries (= 「探したい」 「関連」 "
            "「something for X」 「similar to」), USE search_actions(query=...).",
            "    For exact category enumeration or substring lookup of a known",
            "    keyword, USE list_actions(category=[...]) or"
            " list_actions(filter='...').",
        ])
    else:
        parts.append("  ## Plan decomposition")
        parts.append("")
        parts.append(
            "  Use the `plan` tool when the query requires combining information from"
        )
        parts.append(
            "  multiple independent sources (e.g. \"compare A and B from two docs\","
        )
        parts.append(
            "  \"explain X with code references from N files\", \"summarise across these"
        )
        parts.append(
            "  sources\"). Each step should gather one piece of information; the OS"
        )
        parts.append("  synthesises the final reply.")
        parts.append("")
        parts.append("  Do NOT use `plan` for:")
        parts.append("    - Single-tool lookups or single-source narrations")
        parts.append("    - Chitchat or conversational replies")
        parts.append("    - Queries that invoke_skill handles end-to-end")
        parts.append("    - Queries answerable in one router reply without tools")
        parts.append("")
        parts.append(
            "  - Use parallel tool_calls when discovery / fetches are independent."
        )
        parts.append(
            "  - For sequential dependencies, one tool_call per round."
        )
        parts.append(
            "  - Never invent skill / agent / slug names; only use those returned"
        )
        parts.append("    by list_*.")
        parts.append(
            '  - Memory writes (Save) via remember_*. Triggers: "remember", "覚えて",'
        )
        parts.append('    "save", "from now on", "treat as".')
    # B12-R2 / B13-R3 V3 wording fix (ABSOLUTE rule + JA examples):
    # Baseline (R3 fix only): 40-50% text-reply non-compliance.
    # V3 combined (ABSOLUTE keyword + JA examples): ~5% (1/20, N=20 measurement).
    # Mechanism: (1) ABSOLUTE keyword raises implicit weight above LLM's
    # clarification-seeking instinct; (2) JA examples reduce translation
    # ambiguity for JA-input users; (3) explicit NEVER list closes the
    # "I need more info" text-reply escape hatch.
    # P7 compliance: examples use <skill_name> placeholder, not hardcoded names.
    parts.append("")
    if hide_legacy_tools:
        # B12-R2/B13-R3 V3 ABSOLUTE rule preserved in wrapper vocab (1-line,
        # JA examples dropped — per B23-PRE-1 SP simplification policy).
        # P7-compliant placeholder: <action_name> (= qualified name format).
        parts.extend([
            "  ROUTING RULE (ABSOLUTE): When the user message contains an action"
            " name (= valid invoke_action action_name, e.g. skill__code_review),"
            " call invoke_action immediately. NO clarifying questions. NO text replies.",
            "",
        ])
    else:
        parts.append(
            "  ROUTING RULE (ABSOLUTE): When the user message contains a skill"
        )
        parts.append(
            "  name (= valid invoke_skill enum value), call invoke_skill"
        )
        parts.append(
            "  immediately. NO clarifying questions. NO text replies. Examples:"
        )
        parts.append(
            "    「<skill_name> で <target> を review して」 → invoke_skill(name=<skill_name>)"
        )
        parts.append(
            "    「<skill_name> で <X> を作って」 → invoke_skill(name=<skill_name>)"
        )
        parts.append("")

    # ==========================================================================
    # DYNAMIC — varies per session / configuration
    # Sections 6–13: project_context, Skills, Agents, Memory, Indexed sources,
    # Files, MCP, + dynamic Behaviour conditionals (output_language,
    # indexed_sources disambiguation + JA examples).
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

    # ── 7. Skills catalog ────────────────────────────────────────────────────
    # category-only catalog (= O(1) SP scaling、 industry-aligned per
    # Anthropic Tool Search Tool / OpenAI namespaces / MCP-Zero hierarchical
    # patterns). The previous design inlined skill names + truncated
    # descriptions for hallucination defense (RETRO-H1+H2)、 but this scaled
    # O(N_skills) and was duplicate of the `invoke_skill.name` enum
    # constraint already in tool schema (= structural defense at build_tools).
    # Now: SP describes only the **category catalog** (= what kinds of
    # resources exist)、 actual names lazy-fetched via list_skills.
    # Hallucination defense: schema enum (= invoke_skill rejects unknown
    # name) + Behaviour rule "Never invent names; only use those returned by
    # list_*". Verified by 2026-05-07 N=10 dogfood post-G12-envelope-fix.
    skill_count = len(available_skills)
    if hide_legacy_tools:
        # Wrapper-only path: skill / agent are 2 of the 13 categories
        # listed in the "## Action categories" section above. Dedicated
        # sections would impose a per-category special-case structure
        # that contradicts FP-0034's uniform-invoke design — so they
        # are omitted here. Resource discovery goes through
        # list_actions(category=[...]).
        pass
    else:
        if skill_count > 0:
            parts.append(
                f"## Skills ({skill_count} available) — categories: {skill_section}"
            )
            parts.append(
                "  Call list_skills(path) to browse names + descriptions, then"
            )
            parts.append(
                "  describe_skill(name) for full schema or invoke_skill(name, input)"
            )
            parts.append("  to run. Skill names are validated by schema enum.")
        else:
            parts.append("## Skills — (none available in this session)")
        parts.append("")

        # ── 8. Agents catalog ────────────────────────────────────────────────────
        parts.append("## Agents (resource axis, clusters)")
        parts.append(f"  {agent_section}")
        parts.append("")

    # ── 9. Memory ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Memory inline section is dropped in the
    # wrapper-only path. Memory discovery goes through
    # list_actions(category=['memory.entry']) at runtime.
    if hide_legacy_tools:
        # Wrapper-only: Memory section omitted — list_actions discovers entries.
        pass
    else:
        parts.append(
            "## Memory (entries inlined — answer recall queries from these "
            "descriptions; use read_memory_body for full content if vague)"
        )
        for line in memory_section:
            parts.append(f"  {line}")
        parts.append("")

    # ── 10. Indexed sources (ADR-0033 UX gap fix A) ──────────────────────────
    # Injected verbatim from SourceManifest.format_for_prompt() which already
    # renders the empty-state getting-started hint when 0 sources exist.
    # Placed after Memory (conceptually similar recall stores) and before
    # Files / MCP (distinct resource axes). When None, the section is omitted
    # entirely for backward compat (tests + non-chat paths).
    # B23-PRE-1 SP role-separation: ## Indexed sources omitted in wrapper-only
    # path — list_actions(category=['rag.corpus']) discovers at runtime.
    if not hide_legacy_tools and indexed_sources_section is not None:
        parts.append(indexed_sources_section)
        parts.append("")

    # ── 11. Files ────────────────────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## Files section omitted in wrapper-only
    # path — permission scope communicated via file.* category at runtime.
    if not hide_legacy_tools and file_section:
        parts.append("## Files (resource axis — permission-scoped)")
        for line in file_section:
            parts.append(f"  {line}")
        parts.append("")

    # ── 12. MCP servers and tools ────────────────────────────────────────────
    # B23-PRE-1 SP role-separation: ## MCP servers section omitted in wrapper-
    # only path — list_actions(category=['mcp.server','mcp.tool']) discovers.
    if not hide_legacy_tools and mcp_section:
        parts.append("## MCP servers and tools (resource axis)")
        for line in mcp_section:
            parts.append(f"  {line}")
        parts.append("")

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

    # ── Vocabulary disambiguation rules (B17-S1-1 + B17-S5-3 fix) ──────────
    # Two collisions fixed here:
    #   1. "recall" in user input → LLM mapped to "Recall" (memory) intent.
    #      Fix: renamed intent to "Memory access"; add explicit rule that
    #      "recall" word → indexed-search tool, not memory.
    #   2. "data sources" in user input → LLM mapped to memory layers only,
    #      ignoring the Indexed sources section.
    #      Fix: explicit rule to list BOTH memory AND indexed sources.
    # These rules are only relevant when RAG (indexed sources) is wired up.
    # Without indexed_sources_section, the `recall` tool is not available and
    # the disambiguation rules would reference a non-existent section.
    #
    # B23-PRE-1 SP role-separation: the dynamic disambiguation block
    # (recall/data sources rules + JA table) is moved to per-tool
    # descriptions (rag.operation__recall.description, remember_shared
    # .description). Wrapper-only path skips it entirely.
    if not hide_legacy_tools and indexed_sources_section is not None:
        parts.append(
            "  - The word 'recall' in user input refers to the `recall` tool"
        )
        parts.append(
            "    (= indexed document search). Do NOT map it to list_memory or"
        )
        parts.append(
            "    read_memory_body. For memory retrieval, the intent label is"
        )
        parts.append(
            "    'Memory access', not 'Recall'."
        )
        parts.append(
            "  - When user asks about 'data sources', 'available information',"
        )
        parts.append(
            "    or 'what can I search', list BOTH memory entries (Memory section)"
        )
        parts.append(
            "    AND indexed sources (Indexed sources section). They are different"
        )
        parts.append(
            "    storage layers. Do NOT report only memory as 'your data sources'."
        )
        parts.append(
            "  - When user says 'search', 'find in docs', 'lookup', use the `recall`"
        )
        parts.append(
            "    tool to query indexed sources. Do NOT use list_memory / read_memory_body"
        )
        parts.append(
            "    for these queries."
        )
        # NOTE (batch 19 self-audit, post `1c5856d` revert): a previous
        # iteration added "for 'how is X implemented?' prefer recall over
        # reyn_src_read" guidance here, motivated by S6 batch-18 0/3 refuted.
        # The fix was reverted because the scenario design itself was the
        # flaw: "How is recall implemented?" is a code-reading query (= the
        # answer lives in source files, NOT in the indexed concept docs),
        # and reyn_src_read's description claims this exact use case
        # ("how does Reyn / how does Reyn's X work?"). Adding generic
        # "prefer recall" guidance here actively conflicted with that
        # specialised tool description and was the wrong layer to fix.
        # Real R-RAG-srcread evidence requires a scenario where indexed
        # docs semantically cover the prompt topic; until then, treat
        # affordance-bias as hypothesis only. See
        # docs/deep-dives/journal/dogfood/2026-05-10-batch-19-rag-attractor-fix-retest/
        # retrospective.md for the full audit.
        # ── Empty-state indexed sources guidance (B17-S1-1 fix) ─────────────
        # When 0 indexed sources are available, the LLM must actively tell the
        # user how to add them instead of silently defaulting to memory.
        parts.append(
            "  - If 0 indexed sources are available AND the user asks about data"
        )
        parts.append(
            "    sources or what they can do: explicitly tell them to run"
        )
        parts.append(
            "    `reyn run index_docs '{\"source\":\"<name>\",\"path\":\"<glob>\""
            ",\"description\":\"<text>\"}'`"
        )
        parts.append(
            "    to enable indexed retrieval. Do NOT answer with memory-only."
        )
        # ── FP-0023 Change 5 — JA recall/memory disambiguation examples ──────
        # Extends the EN disambiguation block above with JA-specific examples.
        # Dogfood measurement (B12-R2) showed JA examples reduced non-compliance
        # from ~50% to ~5% for routing rules. Same technique applied here.
        # Placed inside the indexed_sources_section guard because these examples
        # reference recall/remember_* which are only meaningful when RAG is live.
        parts.append("")
        parts.append("  Japanese input disambiguation:")
        parts.append(
            "    - 「思い出して」「前回の話」「あのとき言ってた〜」"
        )
        parts.append(
            "        → recall (indexed search) — if indexed sources exist"
        )
        parts.append(
            "        → list_memory / read_memory_body — if no indexed sources"
        )
        parts.append(
            "    - 「覚えて」「メモして」「記録して」「保存して」「忘れないで」"
        )
        parts.append(
            "        → remember_shared or remember_agent (memory write)"
        )
        parts.append(
            "    - 「忘れて」「削除して」「消して」(about a memory entry)"
        )
        parts.append(
            "        → forget_memory"
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


def _render_mcp(
    mcp_servers: list[dict] | None,
    *,
    hide_legacy_tools: bool = False,
) -> list[str]:
    """Return lines for the MCP servers and tools section, or [] when nothing to render.

    FP-0032: renders a flat list of available mcp_tools (dotted form
    <server>.<tool>) alongside each server, mirroring the "Available skills"
    flat list pattern. When a server's ``tools`` list is absent (= not yet
    async-enumerated), falls back to the prior server-only line with a hint
    to use list_mcp_tools (legacy) or list_actions(category=['mcp.tool'])
    (wrapper-only).
    """
    if not mcp_servers:
        return []
    if hide_legacy_tools:
        discovery_hint = (
            "(use list_actions(category=['mcp.tool']) to discover tools)"
        )
    else:
        discovery_hint = "(use list_mcp_tools to see mcp_tools)"
    lines: list[str] = []
    for server in mcp_servers:
        name = server.get("name", "(unnamed)")
        desc = server.get("description") or "(no description)"
        tools = server.get("tools") or []
        if tools:
            lines.append(f"- {name}: {desc}")
            for t in tools:
                tool_name = t.get("name", "(unnamed)")
                tool_desc = t.get("description") or ""
                dotted = f"{name}.{tool_name}"
                if tool_desc:
                    lines.append(f"    {dotted}  — {tool_desc}")
                else:
                    lines.append(f"    {dotted}")
        else:
            lines.append(f"- {name}: {desc}  {discovery_hint}")
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
