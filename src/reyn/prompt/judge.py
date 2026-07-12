"""§G — the ``judge_output`` op's scorer system prompt (static template).

Feeds ``reyn.core.op_runtime.judge_output`` — the LLM-scorer op that reads the
caller-supplied ``rubric`` and asks the model to score a resolved workspace
value against it. The rubric itself is caller-authored content interpolated
at op-execution time (not LLM-facing OS text this package owns) and stays in
the builder; only the STATIC evaluator-instructions + "Rubric:" label header
that wraps it moves here. ``judge_system_prompt`` reassembles the two exactly
as ``judge_output`` previously inlined via its f-string.
"""
from __future__ import annotations

# WHEN: always — the sole system prompt of every judge_output LLM call.
# WHERE: reyn.core.op_runtime.judge_output — the "3. Build LLM messages" step,
#        as the system message; the caller's `rubric` is appended after
#        RUBRIC_LABEL_PREFIX (dynamic content, not static OS text).
# WHY: P6 (Evaluation lens) — a strict, JSON-only scorer contract
#      ({"score": 0.0-1.0, "reason": ...}) so the OS can parse + threshold the
#      result deterministically; kept domain-agnostic (P7) — no vocabulary
#      about what is being judged, that all lives in the caller's rubric.
# 日本語訳: judge_output の全LLM呼び出しが使う唯一のシステムプロンプトの
#      静的部分。「JSONのみで score/reason を返す」という厳格な採点契約を
#      与える。何を評価するかの語彙は含まず、呼び出し側の rubric に委ねる
#      （ドメイン非依存）。
JUDGE_EVALUATOR_HEADER = (
    "You are a strict evaluator. Score the following output against the rubric.\n"
    'Output ONLY a JSON object: {"score": 0.0-1.0, "reason": "..."}.\n'
    "score must be a float between 0.0 and 1.0 inclusive.\n"
    "reason must be a short explanation of the score.\n\n"
)

# WHEN: always — immediately precedes the caller-supplied rubric text.
# WHERE: reyn.core.op_runtime.judge_output — appended after JUDGE_EVALUATOR_HEADER.
# WHY: labels the interpolated rubric so the model reads it as the scoring
#      criterion, not as further instructions from the header above.
# 日本語訳: 呼び出し側の rubric 本文の直前に付くラベル。ヘッダーからの指示と
#      rubric 本文を明確に区切る。
RUBRIC_LABEL_PREFIX = "Rubric:\n"


def judge_system_prompt(rubric: str) -> str:
    """Return the full judge_output system prompt: the static evaluator
    header + "Rubric:\\n" label + the caller-supplied ``rubric`` verbatim.
    Exact copy of the previously inlined f-string
    (``f"...\\n\\nRubric:\\n{op.rubric}"``)."""
    return JUDGE_EVALUATOR_HEADER + RUBRIC_LABEL_PREFIX + rubric
