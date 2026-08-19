"""Reply text verifier (FP-0036 Component C).

Supports four kinds:
  - judge: rubric → judge_fn (LLM judge, injectable for testing)
  - substring: ``value`` must appear in the reply
  - exact: trimmed equality
  - regex: re.search pattern match
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from reyn.prompt.dogfood import dogfood_judge_system_prompt

from .types import VerifierResult

if TYPE_CHECKING:
    from reyn.dev.dogfood.scenarios import ExpectedReply


# ---------------------------------------------------------------------------
# Default LLM judge backend
# ---------------------------------------------------------------------------

# #4893: hand-written, not routed through core.pipeline.schema's
# SchemaRegistry/to_json_schema (0062's own generator) — that registry
# exists to validate named, cross-referencing pipeline field schemas; this
# is 3 flat fields with no refs, and #4883/#4899 (compaction's own
# equivalent, _CHAT_SUMMARY_JSON_SCHEMA) set the precedent of a small
# literal dict for exactly this shape rather than standing up a registry
# for one ad hoc verdict schema.
_JUDGE_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["passed", "score", "reason"],
    "additionalProperties": False,
}

_JUDGE_MODEL = "gemini-2.5-flash-lite"


async def _supports_structured_output(model: str) -> bool:
    """#4893: whether *model* supports schema-constrained JSON generation
    (``litellm.supports_response_schema``) — the SAME capability precheck
    0062's ``AgentStep.schema`` path uses in production
    (``router_loop.py``'s precheck) and #4883/#4899 already ported to
    compaction (``services/compaction/engine.py``'s own
    ``_supports_structured_output``). A third local copy rather than an
    import: both existing copies are module-private (a leading
    underscore), and #4883's own precheck additionally resolves a
    ``ModelSpec`` via ``host.resolve_model_spec`` — machinery this call
    site has no use for, since :data:`_JUDGE_MODEL` is a fixed literal
    string, never resolved via ``class_for_purpose`` (#4206 T1).

    Routed through ``ensure_litellm_ready`` — the sole chokepoint for a
    not-yet-warm or persistently-broken environment (cooldown, the
    background warming thread) — never a second, independent
    ``import litellm`` (CI's ``test_4421_litellm_import_seam.py``
    enforces this repo-wide).

    Never raises: any failure (litellm not importable/not ready yet, the
    capability query itself erroring) degrades to ``False``, which
    :func:`_default_judge_fn` below turns into a
    ``StructuredOutputUnsupportedModelError`` — the SAME outcome as a
    real "not supported" answer. That's deliberate, not an oversight:
    unlike compaction (a context-window backstop — raising there means
    the conversation itself cannot continue), this verifier isn't one.
    ``verify_reply``'s own ``except Exception`` around the judge call
    already turns any raise into ``outcome="inconclusive"``, which ranks
    ABOVE ``"refuted"`` (``runner.py``'s ``OUTCOME_ORDER``) — strictly
    safer than the bug this fixes (a missing/malformed score silently
    defaulting to 0.0, read as a DEFINITIVE false "refuted").
    """
    try:
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready
        litellm = await asyncio.to_thread(ensure_litellm_ready)
    except Exception:  # noqa: BLE001 — capability probe, never the caller's failure
        return False
    if litellm is None or not hasattr(litellm, "supports_response_schema"):
        return False
    try:
        from reyn.llm.llm import proxy_kwargs
        extra = proxy_kwargs()
        precheck_model = (
            model.split("/", 1)[1] if extra.get("api_base") and "/" in model else model
        )
        return bool(litellm.supports_response_schema(precheck_model))
    except Exception:  # noqa: BLE001 — capability probe, never the caller's failure
        return False


def _parse_judge_response(raw: str) -> dict:
    """#4893: parse + validate a judge completion's raw text into the
    ``{"passed": bool, "score": float, "reason": str}`` contract
    ``verify_reply`` consumes. Extracted as a pure function (mirrors
    #4883's own ``_validate_chat_summary_fields`` split) so the parsing
    and the validation floor are each independently testable without a
    real or replayed LLM call.

    Post-parse validation floor even though the primary path requests
    ``json_schema`` (strict): schema-constrained generation guarantees
    the response IS the declared shape when the call succeeds, but never
    that the call reaches this function with strict enforcement actually
    in effect — a stale/incorrect capability-table entry, a provider that
    accepts the request but doesn't fully honor ``strict``, or (on any
    future fallback leg) plain ``json_object`` mode all reach here with
    no such guarantee. The one thing this function must never do is what
    the bug it replaces did: let a missing/malformed ``score`` silently
    become ``0.0`` — that reads to ``verify_reply`` as a definitive
    "refuted", when the true state is "the judge didn't actually answer".
    ``score: None`` in the return routes into ``verify_reply``'s existing
    "judge returned no score" -> ``inconclusive`` branch, unchanged.
    """
    import json

    if raw.startswith("```"):
        m = re.match(r"^```(?:json)?\s*(.*?)```\s*$", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()

    try:
        parsed = json.loads(raw)
    except Exception:
        return {
            "passed": False,
            "score": None,
            "reason": f"LLM response not valid JSON: {raw[:200]}",
        }
    if not isinstance(parsed, dict):
        return {
            "passed": False,
            "score": None,
            "reason": f"LLM response JSON is not an object: {raw[:200]}",
        }

    try:
        score = float(parsed["score"])
    except (KeyError, TypeError, ValueError):
        return {
            "passed": False,
            "score": None,
            "reason": f"LLM response missing/invalid score field: {parsed!r}",
        }
    if not (0.0 <= score <= 1.0):
        return {
            "passed": False,
            "score": None,
            "reason": f"LLM response score out of [0,1] range: {score!r}",
        }

    threshold = 0.7
    return {
        "passed": score >= threshold,
        "score": score,
        "reason": str(parsed.get("reason", "")),
    }


async def _default_judge_fn(rubric: list[str], reply_text: str) -> dict:
    """Invoke litellm directly with the rubric and reply text.

    Returns: {"passed": bool, "score": float | None, "reason": str}
    (``score`` is ``None`` only on a validation failure — see
    :func:`_parse_judge_response`, which routes it into ``verify_reply``'s
    existing "no score" -> ``inconclusive`` handling.)

    Isolated here so tests can inject a stub without touching litellm.
    Contract: score 0.0-1.0, threshold 0.7. This is an independent
    dev-harness scorer (a direct litellm call).
    """
    rubric_text = "\n".join(f"- {item}" for item in rubric)
    # reyn.prompt.dogfood (SP prompt-package, Phase 3 §H) owns the static
    # header + "Rubric:" label; this reassembles them exactly as before.
    system_text = dogfood_judge_system_prompt(rubric_text)
    user_text = f"Reply to evaluate:\n{reply_text}"
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    # #4893: raise on an unsupported model — DELIBERATELY the same policy
    # as 0062 (router_loop.py §2.1's StructuredOutputUnsupportedModelError)
    # and the OPPOSITE of compaction's #4883/#4899 degrade-to-json_object.
    # These aren't the same decision wearing two names: compaction is a
    # context-window backstop (raising means the conversation itself gets
    # stuck), so it degrades. This verifier has no such backstop role —
    # verify_reply's own except-Exception around the judge call already
    # turns a raise into outcome="inconclusive", ranked ABOVE "refuted" in
    # runner.py's OUTCOME_ORDER. So raising here doesn't cost anything a
    # degrade path would have bought, and it rides 0062's already-ratified
    # policy instead of inventing a second one (lead-coder/#4893 thread).
    if not await _supports_structured_output(_JUDGE_MODEL):
        from reyn.runtime.errors import StructuredOutputUnsupportedModelError
        raise StructuredOutputUnsupportedModelError(
            f"dogfood judge model {_JUDGE_MODEL!r} does not support "
            "structured output (litellm.supports_response_schema "
            "returned False) — verify_reply's own except-Exception turns "
            "this into an inconclusive verdict, never a false refutation."
        )

    # #1190 stage (ii): route through the cost chokepoint (purpose=dogfood,
    # recorder=None — eval verifier surface). Stub-injection still
    # intercepts at litellm.acompletion underneath.
    #
    # fallback_without_response_format=False (0062's own text, quoted in
    # engine.py's #4883 comment): "Do NOT catch-classify a provider
    # rejection for this — a raw 400 can't be reliably told apart from
    # transient/other errors." The degrade decision (raise, above) is
    # already made BEFORE this call, based on the precheck, not by
    # catching a rejection and silently retrying differently-shaped.
    from reyn.llm.llm import recorded_acompletion

    response = await recorded_acompletion(
        model=_JUDGE_MODEL,
        messages=messages,
        purpose="dogfood",
        # #4206 T1: a fixed literal model string, never resolved via
        # class_for_purpose — not subject to the ②bounding model-class
        # ceiling axis.
        model_class=None,
        recorder=None,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "dogfood_judge_verdict",
                "schema": _JUDGE_JSON_SCHEMA,
                "strict": True,
            },
        },
        fallback_without_response_format=False,
        extra_kwargs={"timeout": 30.0, "num_retries": 2},
    )

    raw: str = (response.choices[0].message.content or "").strip()
    if not raw:
        return {"passed": False, "score": None, "reason": "LLM response was empty"}
    return _parse_judge_response(raw)


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
