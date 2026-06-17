"""Reply text verifier (FP-0036 Component C).

Supports four kinds:
  - judge: rubric → judge_fn (LLM judge, injectable for testing)
  - substring: ``value`` must appear in the reply
  - exact: trimmed equality
  - regex: re.search pattern match
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .types import VerifierResult

if TYPE_CHECKING:
    from reyn.dev.dogfood.scenarios import ExpectedReply


# ---------------------------------------------------------------------------
# Default LLM judge backend
# ---------------------------------------------------------------------------


async def _default_judge_fn(rubric: list[str], reply_text: str) -> dict:
    """Invoke litellm directly with the rubric and reply text.

    Returns: {"passed": bool, "score": float, "reason": str}

    Isolated here so tests can inject a stub without touching litellm.
    The judge contract mirrors judge_output.py: score 0.0–1.0, threshold 0.7.
    """
    import json

    rubric_text = "\n".join(f"- {item}" for item in rubric)
    system_text = (
        "You are a strict evaluator. Score the following reply against the rubric.\n"
        'Output ONLY a JSON object: {"score": 0.0-1.0, "reason": "..."}.\n'
        "score must be a float between 0.0 and 1.0 inclusive.\n"
        "reason must be a short explanation of the score.\n\n"
        f"Rubric:\n{rubric_text}"
    )
    user_text = f"Reply to evaluate:\n{reply_text}"
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    # #1190 stage (ii): route through the cost chokepoint (purpose=dogfood,
    # recorder=None — eval verifier surface). The response_format fallback is
    # absorbed by the chokepoint. Stub-injection still intercepts at
    # litellm.acompletion underneath.
    from reyn.llm.llm import recorded_acompletion

    response = await recorded_acompletion(
        model="gemini-2.5-flash-lite",
        messages=messages,
        purpose="dogfood",
        recorder=None,
        response_format={"type": "json_object"},
        fallback_without_response_format=True,
        extra_kwargs={"timeout": 30.0, "num_retries": 2},
    )

    raw: str = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        m = re.match(r"^```(?:json)?\s*(.*?)```\s*$", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()

    try:
        parsed = json.loads(raw)
    except Exception:
        return {"passed": False, "score": 0.0, "reason": f"LLM response not valid JSON: {raw[:200]}"}

    score = float(parsed.get("score", 0.0))
    threshold = 0.7
    return {
        "passed": score >= threshold,
        "score": score,
        "reason": str(parsed.get("reason", "")),
    }


# ---------------------------------------------------------------------------
# Public verifier
# ---------------------------------------------------------------------------


async def verify_reply(
    expected: "ExpectedReply | None",
    reply_text: str,
    *,
    judge_fn: Callable[[list[str], str], Awaitable[dict]] | None = None,
) -> VerifierResult:
    """Score reply_text against expected.

    Parameters
    ----------
    expected:
        The ExpectedReply declared in the scenario. ``None`` → blocked.
    reply_text:
        The actual reply produced by the scenario run.
    judge_fn:
        Optional injection seam for the LLM judge backend. Signature:
        ``async (rubric: list[str], reply_text: str) -> {"passed": bool, "score": float, ...}``.
        Defaults to _default_judge_fn (real litellm call). Tests supply a stub.

    Returns
    -------
    VerifierResult with outcome:
      verified     — assertion passed
      refuted      — assertion failed
      inconclusive — reply is empty or judge returned indeterminate result
      blocked      — no expected provided (= cannot evaluate)
    """
    if expected is None:
        return VerifierResult(outcome="blocked", detail={"reason": "no expected reply declared"})

    if not reply_text or not reply_text.strip():
        return VerifierResult(
            outcome="inconclusive",
            detail={"reason": "reply_text is empty", "kind": expected.kind},
        )

    kind = expected.kind

    if kind == "substring":
        if expected.value in reply_text:
            return VerifierResult(
                outcome="verified",
                detail={"kind": "substring", "value": expected.value},
            )
        return VerifierResult(
            outcome="refuted",
            detail={
                "kind": "substring",
                "value": expected.value,
                "reason": "substring not found in reply",
            },
        )

    if kind == "exact":
        if reply_text.strip() == expected.value.strip():
            return VerifierResult(
                outcome="verified",
                detail={"kind": "exact", "value": expected.value},
            )
        return VerifierResult(
            outcome="refuted",
            detail={
                "kind": "exact",
                "expected": expected.value.strip(),
                "actual": reply_text.strip(),
                "reason": "reply does not exactly match expected value",
            },
        )

    if kind == "regex":
        if re.search(expected.value, reply_text):
            return VerifierResult(
                outcome="verified",
                detail={"kind": "regex", "pattern": expected.value},
            )
        return VerifierResult(
            outcome="refuted",
            detail={
                "kind": "regex",
                "pattern": expected.value,
                "reason": "regex pattern did not match reply",
            },
        )

    if kind == "judge":
        _judge = judge_fn if judge_fn is not None else _default_judge_fn
        try:
            result = await _judge(expected.rubric, reply_text)
        except Exception as exc:
            return VerifierResult(
                outcome="inconclusive",
                detail={"kind": "judge", "reason": f"judge_fn raised: {exc}"},
            )
        passed = bool(result.get("passed", False))
        score = result.get("score")
        reason = result.get("reason", "")
        if score is None:
            # Indeterminate result from judge
            return VerifierResult(
                outcome="inconclusive",
                detail={"kind": "judge", "reason": "judge returned no score", "raw": result},
            )
        if passed:
            return VerifierResult(
                outcome="verified",
                detail={"kind": "judge", "score": score, "reason": reason},
            )
        return VerifierResult(
            outcome="refuted",
            detail={
                "kind": "judge",
                "score": score,
                "reason": reason,
                "rubric": expected.rubric,
            },
        )

    # Unknown kind — treat as inconclusive (defensive; loader already validates)
    return VerifierResult(
        outcome="inconclusive",
        detail={"reason": f"unknown reply kind: {kind!r}"},
    )
