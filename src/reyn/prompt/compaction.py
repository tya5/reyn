"""§E — the compaction service's LLM-driven system-prompt strings.

Feeds ``reyn.services.compaction.engine.CompactionEngine`` (main compaction
call + the overshoot re-summarize pass). Both prompts here are static string
constants — no runtime value is interpolated into either of them — so the text
lives here verbatim; the engine module imports these constants back and uses
them as-is. (Contrast ``dogfood.py``, whose scorer SP interpolates a
caller-supplied rubric and so keeps only the static header/label here.)
"""
from __future__ import annotations

# WHEN: always, main compaction call (CompactionEngine._compact / the
#       overflow-retry loop) — the primary rolling-summary system prompt.
# WHERE: reyn.services.compaction.engine — measured once at engine init
#        (T_comp_SP) and sent as the sole system message of the compaction call.
# WHY: PR-N6 — strengthened with an immutable-base + verbatim-preservation
#      contract so the LLM appends to (never rewrites) previous_summary, and
#      preserves file paths / line numbers / commit hashes / decision ids
#      verbatim rather than paraphrasing them away.
# 日本語訳: 通常のロールング要約呼び出しで使う主システムプロンプト。
#      previous_summary を不変の土台として扱い追記のみ許可し、パス・行番号・
#      コミットハッシュ・決定識別子等は要約せず逐語的に保持させる。
COMPACTION_SYSTEM_PROMPT = """\
You are summarising a chunk of chat history into a structured rolling summary.

CRITICAL — previous_summary handling:
Treat `previous_summary` as an IMMUTABLE BASE. You MUST NOT re-summarise,
rephrase, or modify any content already present in `previous_summary`.
Your only task is to APPEND new information from `new_turns` to it.
If `previous_summary` is null, start fresh from `new_turns`.

Fold the new_turns into the previous_summary (or start fresh if null).
Produce a JSON object with these keys:
  topic_arc         — 1-3 sentences on the current topic. Update when topic shifts.
  decisions         — array of bullet strings for choices made. Drop oldest minor ones if over cap.
  pending           — array of open items (questions, tasks, follow-ups). Remove resolved items.
  session_user_facts — array of user attributes learned this session, not yet in memory. Drop oldest if over cap.
  artifacts_referenced — array of files/PRs/commits/issues in scope. Drop ones no longer relevant.

Retention rules:
- Never drop architectural decisions or items labelled as final.
- Match the user's language for free-text fields.
- Include tool-activity items (file edits, web fetches) only when they inform the reply going forward.
- Do NOT transcribe raw quotes unless they are the verbatim text of a decision or pending item.

VERBATIM PRESERVATION (do NOT paraphrase or omit):
- File paths (e.g. src/reyn/runtime/session.py)
- Line numbers (e.g. line 4916)
- Commit hashes (e.g. a26c3e9c)
- Decision identifiers (e.g. PR-N6, FP-0008, issue #1035)
- Temporal markers (e.g. 2026-05-29, v8)
- Exit codes and error codes

section_token_caps gives soft per-section token budgets. Trim the LEAST IMPORTANT items first when over budget.
Output ONLY the JSON object — no explanation, no markdown fences.
"""


# WHEN: only on the overshoot path (T2) — the produced topic_arc exceeds its
#       body_budget after the main compaction call.
# WHERE: reyn.services.compaction.engine — the controlled re-compression pass,
#        distinct from COMPACTION_SYSTEM_PROMPT (#271: a different contract —
#        this one REQUIRES re-summarising, the main pass forbids it).
# WHY: LLM-judgment loss (preserve decision-relevant, drop least essential)
#      replaces the blind char-cut of hard_truncate; the deterministic floor
#      (T3) still applies after this pass.
# 日本語訳: 要約が予算を超過した場合(T2)のみ使う再圧縮専用プロンプト。
#      通常パスと異なり再要約を明示的に許可・要求する。決定的な下限カットは
#      このLLM圧縮の後段でなお適用される。
RESUMMARIZE_SYSTEM_PROMPT = """\
You are compressing a single rolling-summary narrative (the `topic_arc`) that
overshot its token budget. Rewrite it to fit within the target budget.

You MAY re-compress, rephrase, and drop content — this is an explicit
re-summarisation pass (unlike the main compaction step, here re-summarising is
REQUIRED to shrink the text).

Rules:
- Preserve the MOST decision-relevant content; drop the least essential.
- Keep VERBATIM: file paths, line numbers, commit hashes, decision identifiers
  (PR-N6, FP-0008, issue #1035), temporal markers, exit/error codes.
- Match the original language.
- Output ONLY the rewritten narrative text — no JSON, no markdown, no preamble.
"""
