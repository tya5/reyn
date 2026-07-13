"""§B — the scheme-owned tool-use SP slot content (the "R1–R4" families).

Feeds ``reyn.tools.schemes._universal_sp.build_universal_tool_use_slots``,
called by 3 of the 4 built-in tool-use schemes (``universal_category.py``,
``enumerate_all.py``, ``retrieval.py``) to fill the OS-frame's slots. Each
``build_*`` function here is one of the proposal's "R1–R4 slot builders" — a
*parameterized* function (takes the scheme-supplied gating booleans, returns
the exact rendered text for that combination) — mirroring the shape the
byte-identical-extraction plan calls out explicitly for functions like this.
The gating booleans are the SAME ones ``build_universal_tool_use_slots``
already receives; only the slot-content assembly moved here, the decision of
*which slot key gets which value* (and the ``dict`` shape returned to the OS
frame) stays in ``_universal_sp.py``.

Every gated variant inside a slot (e.g. wrapper-on vs wrapper-off phrasing) is
its own named module-level constant — never a single blob with an inline
conditional — so a reviewer reads two labeled candidate texts instead of
reverse-engineering an f-string.
"""
from __future__ import annotations

# =============================================================================
# R1 — "## Capabilities (routing guide)" (slot_pre_environment)
# =============================================================================
# WHEN: always (every scheme that calls build_universal_tool_use_slots).
# WHERE: injected at slot_pre_environment, before "## Environment".
# WHY: FP-0023 Change 2 — the top-level intent-routing decision tree
#      (conversation / question / task). #1977: wrapper vocab
#      (list_actions/search_actions/describe_action/invoke_action) is
#      universal-scheme-only; wrappers-off (enumerate-all) gets flat-call
#      phrasing instead since it cannot call wrappers it was never given.
# 日本語訳: 「## Capabilities (routing guide)」— 会話/質問/タスクを判別する
#      最上位のルーティング決定木。wrapper有効時はwrapper語彙、無効時
#      （enumerate-all）はフラット呼び出し文言になる（#1977）。

CAPABILITIES_HEADER = "## Capabilities (routing guide)"

CAPABILITIES_DECIDE_LINE = (
    "Decide what the user wants. Multi-step routing is fine — explore"
    " briefly when the right path is uncertain, but don't loop."
)

CAPABILITIES_CONVERSATION_LINE = (
    "**Conversation** (\"hi\", \"thanks\", \"who are you?\") → reply"
    " directly, no tools."
)

CAPABILITIES_QUESTION_INTRO_LINE = (
    "**A question with a substantive answer** — figure out where the"
    " answer lives:"
)

ABOUT_REYN_ITSELF_PREFIX = (
    "- About Reyn itself (how Reyn works, Reyn's CLI / runtime /"
    " protocols / project conventions):"
)
ABOUT_REYN_ITSELF_SUFFIX = (
    " → synthesize from README."
    " (README has the overview + curated map of deep-dive paths;"
    " chain to a specific doc if README points there.)"
)

# WHY: the self-call example must use the vocabulary the active scheme can
#      actually invoke — invoke_action(...) under wrappers, the flat qualified
#      call otherwise (#1977).
REYN_SELF_CALL_WRAPPERS_ON = (
    " `invoke_action(action_name=\"reyn_repo__read\","
    " args={\"path\": \"README.md\"})`"
)
REYN_SELF_CALL_WRAPPERS_OFF = " `reyn_repo__read(path=\"README.md\")`"

ABOUT_EXTERNAL_LINE = (
    "- About external / current information: `web__search` or"
    " `web__fetch`."
)

ALREADY_TRAINED_LINE = "- Already in your training: answer directly."

TASK_PERFORM_HEADER = "**A task to perform** — pick by target shape:"

SINGLE_TARGET_PREFIX = (
    "- Single-target action (= one file, one URL, one"
    " item): if the action is obvious (`file__read` for \"read this"
    " file\", `reyn_repo__read` for \"open Reyn doc X\", `web__fetch`"
    " for a specific URL), invoke directly. "
)

MULTI_TARGET_LINE = (
    "- Multi-target / iteration (= \"do X for each Y\", \"process N"
    " files\", \"run X on every Y\") or multi-step work worth"
    " tracking: decompose into sub-tasks — `task__create` one per"
    " target/step (use `deps` to order them, plus a final aggregate"
    " task depending on the rest), then track via `task__list` /"
    " `task__update_status`, or delegate a sub-task to another agent"
    " via `task__create`'s `assignee`. Do NOT invoke a per-target"
    " action directly without decomposition — it loses the iteration"
    " shape and gets stuck on the first item."
)

# ── OTHERWISE variants (4: wrappers×discovery_mandate) ──────────────────────
OTHERWISE_WRAPPERS_ON_DISCOVERY_PREFIX = (
    "Otherwise — i.e. for any action that is NOT obvious or a named "
    "action above — your FIRST tool call MUST be `list_actions` before "
    "reading, writing, or editing anything (the visible tools are "
    "universal wrappers, not the full catalog; do NOT skip it, refuse, "
    "or guess). Then "
)
OTHERWISE_WRAPPERS_ON_DISCOVERY_SUFFIX = (
    ". To edit a file you MUST use `file__edit`, found via `list_actions`."
)
OTHERWISE_WRAPPERS_ON_NO_DISCOVERY_PREFIX = "Otherwise "
OTHERWISE_WRAPPERS_ON_NO_DISCOVERY_SUFFIX = "."

OTHERWISE_WRAPPERS_OFF_DISCOVERY = (
    "Otherwise — i.e. for any action that is NOT obvious or a named "
    "action above — call the matching action DIRECTLY by its qualified "
    "`<category>__<entry>` name from your available tools (do NOT "
    "refuse or guess). To edit a file you MUST use `file__edit`."
)
OTHERWISE_WRAPPERS_OFF_NO_DISCOVERY = (
    "Otherwise call the matching action directly by its qualified "
    "`<category>__<entry>` name from your available tools."
)


def build_capabilities_routing_guide(
    *,
    universal_wrappers_enabled: bool,
    search_actions_enabled: bool,
    discovery_mandate: bool,
) -> str:
    """R1: the "## Capabilities (routing guide)" slot content. Exact copy of
    the previously inlined ``_r1`` list-assembly + ``"\\n".join`` in
    ``build_universal_tool_use_slots`` — the gating booleans are the SAME
    ones that function receives; only this assembly moved here."""
    if universal_wrappers_enabled:
        _wrapper_names_slot = ["`list_actions`"]
        if search_actions_enabled:
            _wrapper_names_slot.append("`search_actions`")
        _wrapper_names_slot.extend(["`describe_action`", "`invoke_action`"])
        _wrapper_chain_slot = " → ".join(_wrapper_names_slot)

        if discovery_mandate:
            _otherwise_slot = (
                OTHERWISE_WRAPPERS_ON_DISCOVERY_PREFIX
                + _wrapper_chain_slot
                + OTHERWISE_WRAPPERS_ON_DISCOVERY_SUFFIX
            )
        else:
            _otherwise_slot = (
                OTHERWISE_WRAPPERS_ON_NO_DISCOVERY_PREFIX
                + _wrapper_chain_slot
                + OTHERWISE_WRAPPERS_ON_NO_DISCOVERY_SUFFIX
            )
        _reyn_self_call = REYN_SELF_CALL_WRAPPERS_ON
    else:
        if discovery_mandate:
            _otherwise_slot = OTHERWISE_WRAPPERS_OFF_DISCOVERY
        else:
            _otherwise_slot = OTHERWISE_WRAPPERS_OFF_NO_DISCOVERY
        _reyn_self_call = REYN_SELF_CALL_WRAPPERS_OFF

    _r1: list[str] = []
    _r1.append(CAPABILITIES_HEADER)
    _r1.append("")
    _r1.extend([
        CAPABILITIES_DECIDE_LINE,
        "",
        CAPABILITIES_CONVERSATION_LINE,
        "",
        CAPABILITIES_QUESTION_INTRO_LINE,
        ABOUT_REYN_ITSELF_PREFIX + _reyn_self_call + ABOUT_REYN_ITSELF_SUFFIX,
        ABOUT_EXTERNAL_LINE,
        ALREADY_TRAINED_LINE,
        "",
        TASK_PERFORM_HEADER,
        SINGLE_TARGET_PREFIX + _otherwise_slot,
        MULTI_TARGET_LINE,
        "",
    ])
    return "\n".join(_r1)


# =============================================================================
# R2 — "## Action categories" + hot-list + discovery-mandate (slot_post_environment)
# =============================================================================
# WHEN: only when universal_wrappers_enabled (wrappers-off has nothing to
#       enumerate — the full action set is already advertised flat).
# WHERE: injected at slot_post_environment, between "## Environment" and
#        "## Behaviour".
# WHY: per-category one-liners teach the LLM the qualified-name shape and
#      what each category is for; the hot-list-aliases hint and the
#      discovery-mandate paragraph (#187 Stage C, weak-tier gate — see
#      tools/schemes/_discovery.py's tier_wants_discovery_mandate for the
#      policy that decides discovery_mandate) close the "refused because the
#      tool wasn't in the visible hot-list" failure mode.
# 日本語訳: 「## Action categories」節。各カテゴリの一行説明・hot-list
#      ヒント・discovery-mandate 段落（弱モデル向けの発見義務化、#187）。
#      universal_wrappers_enabled のときのみ描画される。
ACTION_CATEGORIES_HEADER = "## Action categories"

ACTION_CATEGORIES_INTRO = (
    "Actions are addressed by qualified name (`<category>__<entry>`)."
    " Names in backticks of the form `<category>__<entry>` are invocable action names."
    " Discover via `list_actions(category=[...])`; describe via"
    " `describe_action(action_name=...)`; execute via"
    " `invoke_action(action_name=..., args={...})`."
)

ACTION_CATEGORIES_LINES = [
    "- **multi_agent** — delegate / list / describe peer agents in this network.",
    "- **mcp** — MCP server management + tool dispatch.",
    "- **file** — workspace file ops (read/write/delete/list).",
    "- **web** — web search and content fetch.",
    "- **memory_entry** — persistent memory records; invoke to read body.",
    "- **memory_operation** — memory CRUD (remember_shared / remember_agent / forget).",
    "- **reyn_repo** — Reyn source/docs (read-only).",
    "- **rag_corpus** — indexed corpora; invoke with `query` for single-source semantic search.",
    "- **rag_operation** — RAG management (multi-source semantic_search, drop_source).",
    "- **exec** — sandboxed argv execution (only when sandbox backend is enabled).",
    (
        "- **task** — dynamically create + manage sub-tasks: decompose a "
        "complex request into trackable units (`task__create` — `deps` order "
        "them, `assignee` delegates to another session), then "
        "`task__update_status` / `task__list` / `task__abort`. Use when a "
        "request needs multi-step tracking or delegation, not a single reply."
    ),
    (
        "- **skill_management** — manage skill definitions: "
        "`skill_management__install_local` to register a local skill directory "
        "(one containing a SKILL.md file) into .reyn/config/skills.yaml; "
        "`skill_management__install_source` to fetch and install a skill from "
        "a git/GitHub URL (shallow-clones to .reyn/skills/<name>/)."
    ),
    (
        "- **pipeline** — launch a registered pipeline: "
        "`pipeline__run` runs a REGISTERED pipeline by name to completion "
        "and returns its final output (synchronous — blocks until done)."
    ),
    (
        "- **pipeline_management** — manage pipeline definitions: "
        "`pipeline_management__install_local` to register a local pipeline "
        "DSL file into .reyn/config/pipelines.yaml; "
        "`pipeline_management__install_source` to fetch and install a "
        "pipeline from a git/GitHub URL (shallow-clones to "
        ".reyn/pipelines/<name>/)."
    ),
    (
        "- **presentation_management** — manage named presentation templates: "
        "`presentation_management__install` to register a named presentation "
        "blueprint (a declarative component tree) into "
        ".reyn/config/presentations.yaml, so a later `present(view=<name>)` "
        "op can render it (proposal 0060 Phase 1 Layer A)."
    ),
]

HOT_LIST_ALIASES_HINT = (
    "The function list visible to you is a HOT-LIST (= a subset of "
    "the full catalog). Whenever the user requests a capability and "
    "no listed tool obviously matches, ALWAYS call `list_actions` "
    "(narrow with `category=[...]` when you know the category) to "
    "discover the rest of the catalog BEFORE refusing. Refusing "
    "without that check is a failure mode — the action you assumed "
    "missing often exists."
)

DISCOVERY_MANDATE_PARAGRAPH = (
    "When no visible tool obviously matches the action you need, "
    "calling list_actions is MANDATORY and comes FIRST — before any "
    "read, write, or edit. Treat the visible list as a subset, never "
    "as complete."
)


def build_action_categories_slot(
    *,
    universal_wrappers_enabled: bool,
    has_hot_list_aliases: bool,
    discovery_mandate: bool,
) -> "str | None":
    """R2: the "## Action categories" slot content, or ``None`` when the slot
    is empty (wrappers-off — nothing to enumerate). Exact copy of the
    previously inlined ``_r2`` list-assembly."""
    _r2: list[str] = []
    if universal_wrappers_enabled:
        _r2.append(ACTION_CATEGORIES_HEADER)
        _r2.append("")
        _r2.append(ACTION_CATEGORIES_INTRO)
        _r2.append("")
        for _line in ACTION_CATEGORIES_LINES:
            _r2.append(_line)
        _r2.append("")
        if has_hot_list_aliases:
            _r2.append(HOT_LIST_ALIASES_HINT)
            _r2.append("")
    if discovery_mandate and universal_wrappers_enabled:
        _r2.append(DISCOVERY_MANDATE_PARAGRAPH)
        _r2.append("")
    return "\n".join(_r2) if _r2 else None


# =============================================================================
# R3 — never-invent / search guidance + ROUTING RULE (slot_in_behaviour)
# =============================================================================
# WHEN: always; content varies on universal_wrappers_enabled/search_actions_enabled;
#       the non_claude addendum is additionally gated on `non_claude`.
# WHERE: injected at slot_in_behaviour, inside "## Behaviour" after the static core.
# WHY: closes the "invented a plausible-looking action name" failure mode, and
#      the ABSOLUTE routing rule (an action name in the user message ⇒ call it
#      immediately, no clarifying text) removes a common latency/refusal path.
#      #1791 A2: non_claude carries extra operational hygiene for non-Claude
#      models (verify-before-acting, check-dependencies, be-concise).
# 日本語訳: 「アクション名を捏造しない」ガイダンスと絶対ルーティング規則。
#      non_claude が真の場合、非Claudeモデル向けの運用衛生規則を追加する。
NEVER_INVENT_WRAPPERS_ON_SEARCH_ON = (
    "  - Never invent action names; only use those returned by\n"
    "    `list_actions` or `search_actions`.\n"
    "  - For semantic / natural-language / keyword queries (e.g.\n"
    "    'find X', 'related to', 'something for X', 'similar to',\n"
    "    'contains http' — the query may be in any language,\n"
    "    including Japanese and other non-English input),\n"
    "    USE `search_actions(query=...)`. For category enumeration,\n"
    "    USE `list_actions(category=[...])`."
)

NEVER_INVENT_WRAPPERS_ON_SEARCH_OFF = (
    "  - Never invent action names; only use those returned by\n"
    "    `list_actions`.\n"
    "  - For category enumeration, USE `list_actions(category=[...])`."
)

NEVER_INVENT_WRAPPERS_OFF = (
    "  - Never invent action names; only call tools that appear in your\n"
    "    available tools list (the names shown to you)."
)

ROUTING_RULE_WRAPPERS_ON = (
    "  ROUTING RULE (ABSOLUTE): When the user message contains an action"
    " name (= valid `invoke_action` action_name, e.g. `mcp__brave__search`),"
    " call `invoke_action` immediately. NO clarifying questions. NO text replies."
)

ROUTING_RULE_WRAPPERS_OFF = (
    "  ROUTING RULE (ABSOLUTE): When the user message contains an action"
    " name (e.g. `mcp__brave__search`), call that action directly by its"
    " name immediately. NO clarifying questions. NO text replies."
)

NON_CLAUDE_HYGIENE_LINES = [
    (
        "  - Verify before acting: read/inspect file contents and project"
        " structure before changing them; never guess at file contents."
    ),
    (
        "  - Check dependencies: do not assume a library is available — confirm"
        " it is declared (manifest / imports) before relying on it."
    ),
    (
        "  - Be concise: keep explanatory text brief — a few sentences, not"
        " paragraphs; favor actions and results over narration."
    ),
]


def build_behaviour_slot(
    *,
    universal_wrappers_enabled: bool,
    search_actions_enabled: bool,
    non_claude: bool,
) -> str:
    """R3: the slot_in_behaviour content. Exact copy of the previously
    inlined ``_r3`` list-assembly."""
    _r3: list[str] = []
    if universal_wrappers_enabled:
        if search_actions_enabled:
            _r3.append(NEVER_INVENT_WRAPPERS_ON_SEARCH_ON)
        else:
            _r3.append(NEVER_INVENT_WRAPPERS_ON_SEARCH_OFF)
    else:
        _r3.append(NEVER_INVENT_WRAPPERS_OFF)
    _r3.append("")
    if universal_wrappers_enabled:
        _r3.append(ROUTING_RULE_WRAPPERS_ON)
        _r3.append("")
    else:
        _r3.append(ROUTING_RULE_WRAPPERS_OFF)
        _r3.append("")
    if non_claude:
        for _line in NON_CLAUDE_HYGIENE_LINES:
            _r3.append(_line)
        _r3.append("")
    return "\n".join(_r3)


# =============================================================================
# R4 — cwd-idiom file-discovery HOW clause (slot_in_environment)
# =============================================================================
# WHEN: always; content depends only on universal_wrappers_enabled.
# WHERE: injected inside "## Environment"'s cwd clause (the OS-frame's
#        CWD_REFERENCE_MAPPING sentence, see router_frame.py).
# WHY: wrapper-vocab vs flat-call phrasing for "how do I look at this repo's
#      files" — must match whichever vocabulary the active scheme can call.
# 日本語訳: cwd内のファイル探索手順（HOW節）。wrapper有効/無効で
#      呼び出し語彙が変わる（#1977）。
ENVIRONMENT_HOW_WRAPPERS_ON = (
    "discover the contents with `list_actions(category=['file'])` →"
    " `invoke_action(file__list, ...)` → `invoke_action(file__read, ...)`"
    " within the cwd's read scope."
)

ENVIRONMENT_HOW_WRAPPERS_OFF = (
    "discover the contents with `file__list(...)` → `file__read(...)`"
    " within the cwd's read scope."
)


def build_environment_how_clause(*, universal_wrappers_enabled: bool) -> str:
    """R4: the slot_in_environment content. Exact copy of the previously
    inlined ternary expression."""
    return ENVIRONMENT_HOW_WRAPPERS_ON if universal_wrappers_enabled else ENVIRONMENT_HOW_WRAPPERS_OFF


# =============================================================================
# Skills block (slot_post_skills, #2548 PR-A)
# =============================================================================
# WHEN: when at least one skill is enabled=True + auto_invoke=True.
# WHERE: injected at the DEDICATED slot_post_skills position (separate from
#        slot_post_catalog so retrieval's overwrite of slot_post_catalog
#        cannot clobber the Skills block).
# WHY: teaches the LLM the Skills menu semantics — read a skill's file only
#      when the current task matches it; do not preload/apply irrelevant skills.
# 日本語訳: 「## Skills」節。有効な skill が1つ以上ある場合のみ描画され、
#      「関連するときだけ読む」というメニュー的な使い方を指示する。
SKILLS_HEADER = "## Skills"

SKILLS_INTRO_LINES = [
    (
        "Skills are reusable, task-specific instruction sets."
        " Each entry is `name — description [file]`."
    ),
    (
        "The description tells you when a skill applies; the full instructions"
        " live in its file. When the"
    ),
    (
        "current task matches a skill, read its file to load the instructions"
        " (and any files it references),"
    ),
    (
        "then follow them for that task. This list is a menu: read a skill only"
        " when it is relevant — do not"
    ),
    (
        "preload or apply skills that do not fit the task. If none apply,"
        " proceed normally."
    ),
]


def build_skills_slot(available_skills: "list | None") -> "str | None":
    """Skills block: the slot_post_skills content, or ``None`` when there is
    no enabled+auto_invoke skill. Exact copy of the previously inlined
    ``_s_lines`` list-assembly."""
    if not available_skills:
        return None
    _sp_skills = [
        s for s in available_skills
        if getattr(s, "enabled", True) and getattr(s, "auto_invoke", True)
    ]
    if not _sp_skills:
        return None
    _s_lines: list[str] = []
    _s_lines.append(SKILLS_HEADER)
    _s_lines.append("")
    for _line in SKILLS_INTRO_LINES:
        _s_lines.append(_line)
    _s_lines.append("")
    for _sk in _sp_skills:
        _s_lines.append(f"- {_sk.name} — {_sk.description} [{_sk.path}]")
    return "\n".join(_s_lines)
