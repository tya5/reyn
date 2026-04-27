"""
LLM-as-judge: scores artifact content against natural language criteria.
"""
from __future__ import annotations
import json
import os
import re
import litellm
from .eval_models import CriterionResult, PASS_THRESHOLD
from .pricing import TokenUsage

_SYSTEM = """\
You are a strict, objective evaluator for AI agent workflow outputs.
Given an artifact produced by a phase and a list of evaluation criteria,
score each criterion independently on a scale of 0.0 to 1.0.

Scoring guide:
  1.0 — criterion is fully and clearly satisfied
  0.8 — criterion is mostly satisfied with minor gaps
  0.6 — criterion is partially satisfied (threshold for passing)
  0.4 — criterion is attempted but significantly lacking
  0.0 — criterion is not satisfied at all

Rules:
- Be strict. Do not give high scores out of charity.
- Base scores only on what is actually present in the artifact.
- Keep reasons short (one sentence), specific, and evidence-based.
- Respond with JSON only. No markdown, no explanation outside JSON.

Response format:
{
  "scores": [
    {"criterion": "<exact criterion text>", "score": 0.0, "reason": "<one sentence>"},
    ...
  ]
}
"""


def judge_artifact(
    model: str,
    artifact: dict,
    criteria: list[str],
    context: str = "",
) -> tuple[list[CriterionResult], TokenUsage]:
    """
    Score an artifact against a list of criteria using an LLM judge.
    Never raises — errors are captured as score=0.0 results.
    Returns (results, token_usage).
    """
    if not criteria:
        return [], TokenUsage()

    criteria_block = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    context_line = f"context: {context}\n" if context else ""
    user = (
        f"{context_line}"
        f"artifact:\n{json.dumps(artifact, ensure_ascii=False, indent=2)}\n\n"
        f"criteria:\n{criteria_block}"
    )

    api_base = os.environ.get("LITELLM_API_BASE")
    extra = {"api_base": api_base, "custom_llm_provider": "openai"} if api_base else {}

    try:
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                **extra,
            )
        except Exception:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                **extra,
            )

        usage = TokenUsage()
        try:
            u = resp.usage
            if u:
                usage = TokenUsage(
                    prompt_tokens=int(u.prompt_tokens or 0),
                    completion_tokens=int(u.completion_tokens or 0),
                )
        except Exception:
            pass

        raw = resp.choices[0].message.content or "{}"
        # Strip markdown fences if present
        stripped = raw.strip()
        m = re.match(r"^```(?:json)?\s*(.*?)```\s*$", stripped, re.DOTALL)
        if m:
            stripped = m.group(1).strip()
        # Repair trailing commas and invalid escape sequences
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        stripped = re.sub(r'\\([^"\\/bfnrtu])', r'\\\\\1', stripped)
        data = json.loads(stripped)
        scores = data.get("scores", [])

        results: list[CriterionResult] = []
        for i, criterion in enumerate(criteria):
            entry = scores[i] if i < len(scores) else {}
            score_val = min(1.0, max(0.0, float(entry.get("score", 0.0))))
            reason = entry.get("reason", "no reason returned")
            results.append(CriterionResult(criterion=criterion, score=score_val, reason=reason))
        return results, usage

    except Exception as exc:
        return [
            CriterionResult(criterion=c, score=0.0, reason=f"judge error: {exc}")
            for c in criteria
        ], TokenUsage()
