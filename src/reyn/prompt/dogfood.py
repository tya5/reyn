"""§H — the dev/dogfood internal eval harness's LLM-judge system prompts.

These are NOT surfaced to an end-user agent — they are the SP of two
LLM-as-judge calls the dogfood harness itself makes (an internal dev tool,
``reyn dogfood publish`` / a reply-verifier), scoped in per the owner's "全て
(all of them)" instruction since they DO reach a real LLM request. Was
sibling to ``judge.py`` (§G, the production ``judge_output`` op's scorer SP,
since removed as a clean-break — the OS-level rubric-scorer op is gone;
scoring is now done via a pipeline ``agent`` step + ``schema`` instead). This
module's own two internal-harness SPs are independent of that removal — they
have their own (similar but not identical) wording, evolve on a dev-tool
cadence, and were never part of the production op surface.

Feeds:
- ``reyn.dev.dogfood.interpretation.generate_interpretation`` — summarises one
  scenario run as a 3-line human-reviewer report.
- ``reyn.dev.dogfood.verifiers.reply._default_judge_fn`` — scores a produced
  reply against a rubric (a direct litellm call with its own header+
  "Rubric:"+rubric seam, dogfood-specific wording).
"""
from __future__ import annotations

# WHEN: always — the sole system prompt of every per-scenario interpretation
#       call (FP-0036 Component G).
# WHERE: reyn.dev.dogfood.interpretation.generate_interpretation — the system
#        message, paired with a user message carrying the scenario + result.
# WHY: a fixed 3-line report shape (match/salient-observation/evidence) so
#      the dogfood publish output is scannable and consistent across runs.
# 日本語訳: 各シナリオ実行の要約(interpretation)呼び出しが常に使う唯一の
#      システムプロンプト。「一致したか/最も顕著な観察/根拠」の3行形式に
#      固定し、reviewer が読みやすい一貫した形にする。
DOGFOOD_INTERPRETATION_SYSTEM_PROMPT = (
    "You read a single dogfood scenario result and write a 3-line summary "
    "for human reviewers. Each line is one sentence.\n"
    "\n"
    "Line 1: Did the run match the scenario's expectations? "
    "(\"matched\" / \"partially matched\" / \"diverged\")\n"
    "Line 2: The most salient observation (reply tone / event presence / "
    "tool usage). One concrete fact.\n"
    "Line 3: If diverged or partially matched, what was missing or wrong. "
    "If matched, what evidence supports it.\n"
    "\n"
    "Use the same language as the scenario input. Stay under 80 chars per "
    "line. Output plain text only — no markdown, no JSON, no quoting."
)


# WHEN: always — the sole system prompt of the reply-verifier's default LLM
#       judge backend (when no test-injected ``judge_fn`` stub is supplied).
# WHERE: reyn.dev.dogfood.verifiers.reply._default_judge_fn — the system
#        message, immediately followed by DOGFOOD_JUDGE_RUBRIC_LABEL_PREFIX +
#        the caller-supplied rubric text (dynamic content, not static OS
#        text — mirrors judge.py's §G split).
# WHY: a strict JSON-only scorer contract ({"score": 0.0-1.0, "reason": ...})
#      so the dogfood harness can parse + threshold the result deterministically.
# 日本語訳: reply-verifier のデフォルト LLM 判定バックエンドが常に使う
#      システムプロンプトの静的部分。JSON のみで score/reason を返す厳格な
#      採点契約を与える。
DOGFOOD_JUDGE_EVALUATOR_HEADER = (
    "You are a strict evaluator. Score the following reply against the rubric.\n"
    'Output ONLY a JSON object: {"score": 0.0-1.0, "reason": "..."}.\n'
    "score must be a float between 0.0 and 1.0 inclusive.\n"
    "reason must be a short explanation of the score.\n\n"
)

# WHEN: always — immediately precedes the caller-supplied rubric text.
# WHERE: reyn.dev.dogfood.verifiers.reply._default_judge_fn — appended after
#        DOGFOOD_JUDGE_EVALUATOR_HEADER.
# WHY: labels the interpolated rubric so the model reads it as the scoring
#      criterion, not as further instructions from the header above.
# 日本語訳: rubric 本文の直前に付くラベル。ヘッダーからの指示と rubric 本文を
#      明確に区切る。
DOGFOOD_JUDGE_RUBRIC_LABEL_PREFIX = "Rubric:\n"


def dogfood_judge_system_prompt(rubric_text: str) -> str:
    """Return the full reply-verifier judge system prompt: the static
    evaluator header + "Rubric:\\n" label + the caller-formatted
    ``rubric_text`` verbatim. Exact copy of the previously inlined f-string
    (``f"...\\n\\nRubric:\\n{rubric_text}"``)."""
    return DOGFOOD_JUDGE_EVALUATOR_HEADER + DOGFOOD_JUDGE_RUBRIC_LABEL_PREFIX + rubric_text
