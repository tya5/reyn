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
