"""Scheme-layer tool-use SP builder for the universal-category path (#1627 Stage 4).

``build_universal_tool_use_slots`` was relocated from
``reyn.runtime.router_system_prompt`` (OS layer) to here (scheme layer) as part of
Stage 4 — the final step of making ``build_system_prompt`` a pure slot-injector
with ZERO tool-use vocab. The OS builds the OS-frame; the scheme owns the
tool-use SP content.

P7-clean location: ``_universal_sp`` carries universal-category tool-use strings;
the OS ``router_system_prompt`` must NOT import from here (the dependency arrow
is scheme→ not OS←scheme).

Callers: universal_category.py / enumerate_all.py / retrieval.py — all scheme-
layer modules. No OS module imports this.
"""
from __future__ import annotations


def build_universal_tool_use_slots(
    *,
    universal_wrappers_enabled: bool,
    search_actions_enabled: bool,
    discovery_mandate: bool,
    has_hot_list_aliases: bool,
    non_interactive: bool = False,
) -> "dict[str, str]":
    """Build the four positional tool-use SP slots for the universal-category path.

    Called by each scheme's ``build_presentation`` to fill the slot-map they
    pass as ``tool_use_sp`` to ``build_system_prompt``. Returns a dict with ONLY
    the non-empty slots so ``build_system_prompt`` can inject each with a simple
    ``if slot_key in _slots`` guard.

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
    """
    slots: dict[str, str] = {}

    # ── R1: ## Capabilities (routing guide) ──────────────────────────────────
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
    slots["slot_in_environment"] = (
        "discover the contents with `list_actions(category=['file'])` →"
        " `invoke_action(file__list, ...)` → `invoke_action(file__read, ...)`"
        " within the cwd's read scope."
    )

    return slots
