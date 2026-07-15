"""§A — the main agent/router OS-frame static prose.

Feeds ``reyn.runtime.router_system_prompt.build_system_prompt`` — the one
OS-frame builder called once per turn by ``RouterLoop`` (#1627 Stage 4: a pure
slot-injector). This module holds the STATIC LLM-facing string content of that
frame (identity, static Behaviour core, the two ambiguity-rule variants, the
memory-guidance bullet, the project_context section labels, the cwd-instruction
sentence, and the output-language directive template); the slot-injection
control flow (which section renders when, in what order, joined how) stays in
``router_system_prompt.py`` and only imports these components.

Byte-identical relocation (SP Phase 1, agent-facing A-D): every string below is
an exact copy of what ``build_system_prompt`` previously inlined — see
``tests/scaffold/test_sp_prompt_package_phase1_byte_identical.py`` for the
before/after golden-diff proof, and ``tests/test_llm_facing_text_english_only.py``
for the permanent CJK-free + tool-name-liveness gates that continue to run
against the post-relocation assembled output.
"""
from __future__ import annotations

from reyn.prompt._types import PromptComponent

# ── Identity preamble ────────────────────────────────────────────────────────
# WHEN: always, first section of every assembled system prompt.
# WHERE: build_system_prompt() → "# Identity" (section 1, static cache-prefix core).
# WHY: vendor-neutral identity rules — the LLM must identify as "a Reyn agent",
#      never as Google/OpenAI/Anthropic or "a large language model". Static so
#      it stays in the Anthropic prompt-cache prefix across turns.
# 日本語訳: 常に描画される静的な自己紹介ルール。「Reyn agent」とだけ名乗り、
#      特定ベンダー名やLLMという表現は使わない。セッション間で不変。
IDENTITY_PREAMBLE = (
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

IDENTITY_PREAMBLE_COMPONENT = PromptComponent(
    name="IDENTITY_PREAMBLE",
    surfaced='build_system_prompt() → "# Identity" (section 1, always, static core)',
    purpose="Vendor-neutral identity rules; keeps the LLM from self-identifying "
    "as a specific vendor/model, cached across turns.",
    text=IDENTITY_PREAMBLE,
    ja="常に描画される静的な自己紹介ルール。ベンダー名やLLMという自称を禁止する。",
)


# ── Role stamp (parameterized — agent_name / agent_role are dynamic) ────────
# WHEN: always, section 2.
# WHERE: build_system_prompt() → one-line role stamp, right after Identity.
# WHY: tells the LLM which agent/role it is running as this turn.
# 日本語訳: 常に描画される1行のロール宣言。どのエージェント/ロールとして
#      動作しているかを伝える。agent_name/agent_role は動的に埋め込まれる。
def role_stamp(agent_name: str, agent_role: str) -> str:
    """Return the "Role: chat router for agent ..." one-liner. Exact copy of
    the f-string previously inlined in ``build_system_prompt``."""
    return f"Role: chat router for agent {agent_name} (role: {agent_role})."


# ── Environment / cwd-instruction sentence ──────────────────────────────────
# WHEN: when `cwd` is provided (## Environment section, inside the cwd clause).
# WHERE: build_system_prompt() → "## Environment" → the sentence following the
#        cwd-idiom HOW clause (the HOW clause itself is scheme-owned via
#        slot_in_environment, see universal_slots.py's build_environment_how_clause).
# WHY: maps unqualified references ("this repo", "here", …, in any language)
#      to the workspace at cwd, and forbids asking for a repo URL/path.
# 日本語訳: cwd指定時、常に描画される環境節の一文。「このリポジトリ」等の
#      無限定な参照を cwd のプロジェクトへ写像し、URL/パスを尋ねることを禁止する。
CWD_REFERENCE_MAPPING_PREFIX = (
    "When the user refers to \"this repo\", \"this code\", \"the codebase\","
    " \"this project\", \"here\" (in any language, including Japanese and"
    " other non-English input), or any other unqualified reference to"
    " surrounding source, interpret it as the project at the cwd above."
    " Do NOT ask for a repository URL or path — "
)

# WHEN: fallback HOW-clause when the OS frame has no scheme-supplied
#       slot_in_environment (e.g. a bare/no-scheme call).
# WHERE: build_system_prompt()'s `_slots.get("slot_in_environment", DEFAULT)`.
# WHY: a generic, scheme-agnostic instruction so the OS frame degrades
#      gracefully even with no tool-use scheme attached.
# 日本語訳: scheme が slot_in_environment を提供しない場合のデフォルトの
#      ファイル探索手順文。scheme非依存の汎用文言。
DEFAULT_CWD_HOW_CLAUSE = (
    "read the contents using your available actions within the cwd's read scope."
)


def cwd_reference_mapping_sentence(cwd_how: str) -> str:
    """Return the full cwd-instruction sentence with the scheme-supplied (or
    default) HOW clause appended. Exact copy of the previously inlined
    concatenation ``CWD_REFERENCE_MAPPING_PREFIX + _cwd_how``."""
    return CWD_REFERENCE_MAPPING_PREFIX + cwd_how


# ── Behaviour static core (3 always-on bullets) ─────────────────────────────
# WHEN: always, "## Behaviour" section, before any scheme-owned slot injection.
# WHERE: build_system_prompt() → "## Behaviour" static core.
# WHY: cross-cutting agent conduct rules — errors surface verbatim (#anti
#      optimism-bias), TASK_COMPLETION anti-fabrication + finish-the-task
#      (#1791 A1), and a no-fabricated-output rule. Static/cacheable, applies
#      regardless of which tool-use scheme or model is active.
# 日本語訳: 常に描画される静的な行動規範3箇条。エラーの誠実な報告・
#      タスク完遂（捏造禁止）・偽の出力の禁止を定める。全モデル・全schemeで不変。
ERRORS_VERBATIM_RULE = (
    "  - Errors MUST surface verbatim. Never narrate an error as success.\n"
    "    Optimism bias on errors is the single largest router-narration"
    " failure mode."
)

TASK_COMPLETION_RULE = (
    "  - Finishing the job: when asked to build, run, or verify something, the"
    " deliverable is a working result backed by REAL tool output — not a"
    " description of one. Do not stop after a stub, a plan, or a single command;"
    " keep working until you have actually produced the requested result, then"
    " report what real execution returned. Only end your turn when the request"
    " is fully resolved or genuinely blocked: never yield half-done to ask"
    " whether to continue, and when an approach fails, try an alternative"
    " before stopping. If a real blocker remains, report it honestly."
)

NO_FABRICATION_RULE = (
    "  - NEVER substitute fabricated output (invented data, file contents, or"
    " tool/command results) for results you could not actually produce. If a tool"
    " or call fails and blocks the real path, say so directly and try an"
    " alternative — reporting a blocker honestly is always better than inventing"
    " a result."
)

BEHAVIOUR_STATIC_CORE = [
    ERRORS_VERBATIM_RULE,
    TASK_COMPLETION_RULE,
    NO_FABRICATION_RULE,
]


# ── Ambiguity / proceed-vs-ask Behaviour rule (2 variants) ──────────────────
# WHEN: always, "## Behaviour" section, right after the static core.
# WHERE: build_system_prompt() → "## Behaviour", gated on `non_interactive`.
# WHY: sp-autonomy-revision — non-interactive (ephemeral/headless, no user to
#      ask) sessions must default HARDER toward proceeding than interactive
#      ones, since there is no one to answer a clarifying question. Promoted
#      to the OS frame (was scheme-owned) so it is scheme-agnostic and reaches
#      every scheme, including CodeAct.
# 日本語訳: 「曖昧な指示への対応（進めるか確認するか）」ルールの2バリアント。
#      non_interactive（確認相手がいない headless/ephemeral セッション）では
#      より強く「進める」方向にデフォルトする。CodeAct を含む全 scheme に適用。
AMBIGUITY_RULE_NON_INTERACTIVE = (
    "  - Ambiguous or missing information: default to proceeding — make the"
    " most reasonable assumption, state it explicitly, and continue. Ask ONE"
    " targeted clarifying question ONLY when the ambiguity is BOTH"
    " consequential (a wrong guess causes real, hard-to-undo work) AND cannot"
    " be resolved from context or by inspecting the workspace. When the user"
    " asks HOW to approach something, or whether to do it, answer first — do"
    " not jump into actions they haven't asked for."
)

AMBIGUITY_RULE_INTERACTIVE = (
    "  - Ambiguous or missing information: prefer proceeding with a stated,"
    " reasonable assumption over asking. Ask ONE targeted clarifying question"
    " ONLY when the ambiguity is BOTH consequential (a wrong guess causes"
    " real, hard-to-undo work) AND cannot be resolved from context or by"
    " inspecting the workspace. When the user asks HOW to approach something,"
    " or whether to do it, answer first — do not jump into actions they"
    " haven't asked for."
)


def ambiguity_rule(*, non_interactive: bool) -> str:
    """Return the ambiguity/proceed-vs-ask Behaviour rule for the given mode.
    Exact copy of the previously inlined conditional expression."""
    return AMBIGUITY_RULE_NON_INTERACTIVE if non_interactive else AMBIGUITY_RULE_INTERACTIVE


# ── Memory-guidance bullet ───────────────────────────────────────────────────
# WHEN: when `memory_index.status == "ok"` (a memory tool is active).
# WHERE: build_system_prompt() → "## Behaviour", after the ambiguity rule.
# WHY: #1791 #3 — save-durable-facts-only hygiene (mirrors Hermes
#      MEMORY_GUIDANCE), gated so the cost is off non-memory agents.
# 日本語訳: memory ツールが有効な場合のみ描画される、記憶保存の指針。
#      恒久的事実のみ保存し、PR番号やコミットSHA等は保存しないよう指示する。
MEMORY_GUIDANCE_BULLET = (
    "  - Memory guidance: save durable facts only (user preferences, recurring"
    " corrections, environment quirks, stable conventions). Do NOT save PR or"
    " issue numbers, commit SHAs, completed-task logs, or anything that will be"
    " stale within a week. Write memories as declarative facts, not instructions"
    " to yourself."
)


# ── Project context (AGENTS.md / REYN.md) section ───────────────────────────
# WHEN: when `project_context` is non-empty.
# WHERE: build_system_prompt() → "## About this project (project_context)".
# WHY: surfaces the operator's AGENTS.md/REYN.md content, and tells the LLM to
#      prefer it over web search for project questions.
# 日本語訳: project_context が空でない場合のみ描画される節。運用者が書いた
#      AGENTS.md/REYN.md の内容を提示し、プロジェクトに関する質問では
#      web検索よりこちらを優先するよう指示する。
PROJECT_CONTEXT_HEADER = "## About this project (project_context)"

PROJECT_CONTEXT_PREFERENCE_NOTE = (
    "Prefer project_context (above) as the primary source when "
    "answering questions about this project. Use `web__search` only as "
    "a supplementary source when project_context lacks the "
    "information needed."
)


# ── Explicit output-language directive (parameterized) ──────────────────────
# WHEN: when `output_language` is set (user configured a fixed reply language).
# WHERE: build_system_prompt() → "## Behaviour", dynamic conditional section.
# WHY: F11 — a concrete language tag is stronger than "match the user's
#      language"; keeps the LLM in that language even on clarifying-question
#      and error-fallback paths.
# 日本語訳: output_language が設定されている場合のみ描画される、返信言語の
#      固定指示。エラーメッセージや確認質問でも言語を切り替えないよう強制する。
def output_language_directive(output_language: str) -> str:
    """Return the "Always reply in language: ..." Behaviour bullet. Exact copy
    of the previously inlined f-string + concatenation."""
    return (
        f"  - Always reply in language: {output_language}."
        "  Do NOT switch language even for error messages or clarifying questions."
    )


# ── Mechanism routing (part x role) — 0060 Addendum C, Layer C ─────────────
# WHEN: always, scheme-independent, static cache-prefix section.
# WHERE: build_system_prompt() -> a dedicated "## Mechanism routing" section,
#        placed in the STATIC block (before "## Behaviour") -- NOT a
#        scheme-owned tool_use_sp slot. It therefore holds across all four
#        tool-use schemes (universal / enumerate / retrieval / codeact): they
#        all funnel through this one OS-frame builder, and none of them can
#        omit or overwrite this section the way a scheme-owned slot could.
# WHY: proposal 0060 Addendum C (C1/C2) -- the model needs a standing map of
#      WHICH mechanism to reach for by role (input / workflow / output), not
#      just a flat action catalog, and hooks (today entirely absent from the
#      SP) need to become visible. C3 (load-bearing): the part x role rows
#      are DERIVED from reyn.core.part_type_registry.PART_TYPE_REGISTRY's
#      ``roles`` frozensets -- NEVER a hand-written parallel table (the
#      #2899 completeness discipline applied at the SP layer). A new marked
#      part-type dropped into reyn.core.part_types auto-appears here with
#      zero edits to this module.
# 日本語訳: 常に描画される、scheme非依存の静的節。「入力/処理/出力」の各役割に
#      どの機構を使うべきかの地図を提示し、これまでSPに一切現れなかった hook を
#      可視化する。表の各行は PART_TYPE_REGISTRY の roles frozenset から導出され、
#      手書きの並行テーブルは禁止（#2899 の完全性規律をSP層に適用）。
MECHANISM_ROUTING_HEADER = "## Mechanism routing (part x role)"

MECHANISM_DECISION_TREE = (
    "Pick the mechanism by what you need, not by habit:\n"
    "  - need INPUT (new data or a reactive trigger) -> hook | mcp | retrieval\n"
    "  - need WORKFLOW (multi-step orchestration) -> skill | pipeline | mcp-step\n"
    "  - need OUTPUT (present, render, or write externally) -> present | render | mcp-write"
)

AUTHOR_VS_REUSE_HEURISTIC = (
    "Reuse before authoring: check the existing catalog (list_actions) for a "
    "part that already covers the need before writing a new one. Author only "
    "when nothing existing fits. Any authored part must be typed, "
    "permissioned, and self-reviewed (an agent step + schema-validated output) "
    "before it is promoted for reuse -- an ungated authored part is a "
    "liability, not a shortcut."
)

# ── present affordance (both directions) -- 0060 F3b ────────────────────────
# WHY: the owner specifically flagged an observed failure mode -- the model
# answers without reading all relevant content, or dumps content into its own
# reply that should have gone to the operator via `present` instead. Both
# directions must be explicit: OUTPUT (show results via present, zero token
# cost) and INPUT (content you must read/act on goes into YOUR OWN context --
# presenting it means you never see it). Kept to two lines (SP is per-turn
# billed); the full present spec (8-component catalog, $bind grammar) lives
# in the reyn cheat sheet skill below, not here.
# 日本語訳: present の使い所を入出力の両方向で明示する（一方向だけだと
# 「読むべき内容を present してしまい自分では見ない」失敗が起きる、というオーナー
# 指摘への対処）。詳細はチートシートskillへ委譲し、ここは2行に収める。
PRESENT_AFFORDANCE_ESSENTIAL = (
    "present affordance -- both directions matter:\n"
    "  - OUTPUT: use present to show RESULTS to the operator (zero token "
    "cost) instead of dumping them into your reply.\n"
    "  - INPUT: content YOU must read or act on (a skill's content, docs, a "
    "file to process) -- read it into your own context; do NOT present it "
    "(presenting it means you never see it)."
)

# ── reyn cheat sheet pointer (0060 Addendum D4/D5e) ─────────────────────────
# WHY: the SP shrinks to a minimal bootstrap (the map + discipline above) plus
# a NAMED pointer to the cheat-sheet skill, which carries the long-tail
# composition know-how (Addendum D1: "concept/reference から漏れる隙間を埋め
# るのが skill"). This name is a load-bearing contract (D5e): a dedicated CI
# gate asserts the named builtin skill actually exists -- see
# test_0060_f3b_builtin_content.py's D5e co-vet pin. Keep this constant's
# skill-name literal in sync with reyn.builtin.registry.BUILTIN_SKILLS' key
# (the gate fails loud on drift; it does not derive one from the other, since
# router_frame.py must not import the builtin registry at prose-authoring
# time -- the gate is what keeps them coherent).
# 日本語訳: SP本体は最小限のブートストラップに留め、詳細な使い方はチートシート
# skillへの名指しポインタに委譲する。この名前は D5e ゲートで存在を保証される
# load-bearing な契約。
REYN_CHEAT_SHEET_SKILL_NAME = "reyn_cheat_sheet"

CHEAT_SHEET_POINTER = (
    f"For reyn-specific usage details (composition idioms, op essentials, "
    f"doc pointers), read the '{REYN_CHEAT_SHEET_SKILL_NAME}' skill."
)

# Cost-discipline (C1): a hard per-part-type-row character cap so the derived
# map stays within the cache-static budget as the meta-registry grows -- the
# frame carries the routing MODEL, never the pull-side catalog.
MAX_PART_TYPE_ROW_CHARS = 100


def _format_part_type_row(spec: object) -> str:
    """Render one derived row -- ``name (category): role, role`` -- from a
    ``PartTypeSpec``-shaped object (duck-typed on ``.name``/``.category``/
    ``.roles`` to avoid a hard import of ``reyn.core.part_type_registry`` at
    module scope). Raises if the row would exceed
    :data:`MAX_PART_TYPE_ROW_CHARS`, so an over-verbose future part-type spec
    fails loud instead of silently bloating every turn's SP as the registry
    grows (0060 Addendum C, co-vet pin 3)."""
    roles = ", ".join(sorted(spec.roles))
    row = f"  - {spec.name} ({spec.category}): {roles}"
    if len(row) > MAX_PART_TYPE_ROW_CHARS:
        raise ValueError(
            f"part-type row for {spec.name!r} is {len(row)} chars, over the "
            f"{MAX_PART_TYPE_ROW_CHARS}-char per-row cache-static budget "
            "(0060 Addendum C) -- shorten its category/name"
        )
    return row


def render_part_role_map(registry: "dict[str, object] | None" = None) -> str:
    """Render the part x role routing map, DERIVED from
    ``reyn.core.part_type_registry.PART_TYPE_REGISTRY`` (0060 Addendum C, C3
    -- the load-bearing decision: never a hand-written parallel table).

    ``registry`` defaults to the live ``PART_TYPE_REGISTRY``; passing a
    different ``dict[str, PartTypeSpec]`` lets tests exercise a
    synthetic/rebuilt registry (the auto-appear and char-budget co-vet
    witnesses) without mutating the shipped one."""
    if registry is None:
        from reyn.core.part_type_registry import PART_TYPE_REGISTRY

        registry = PART_TYPE_REGISTRY
    return "\n".join(
        _format_part_type_row(spec) for _, spec in sorted(registry.items())
    )


def render_mechanism_routing_frame(registry: "dict[str, object] | None" = None) -> str:
    """Full "## Mechanism routing" section: header + the derived part x role
    map + the mechanism-selection decision tree + the author-vs-reuse
    heuristic + the present affordance (both directions) + the reyn cheat
    sheet pointer. This is what ``build_system_prompt`` injects into the
    static cache-prefix (0060 Addendum C, C1) -- scheme-independent, appears
    identically under every tool-use scheme."""
    return "\n\n".join(
        [
            MECHANISM_ROUTING_HEADER,
            render_part_role_map(registry),
            MECHANISM_DECISION_TREE,
            AUTHOR_VS_REUSE_HEURISTIC,
            PRESENT_AFFORDANCE_ESSENTIAL,
            CHEAT_SHEET_POINTER,
        ]
    )
