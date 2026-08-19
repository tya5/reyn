"""Tier 1: ``_default_judge_fn``'s own contract (#4893).

#4893 (owner-directed, mirroring #4883/#4899's structured-output pattern
onto the LAST literal ``json_object`` call site outside compaction):
schema-constrained generation (``json_schema``, strict) when the model
supports it, and a post-parse validation floor that never lets a
missing/malformed ``score`` silently become ``0.0`` — that read as a
DEFINITIVE false "refuted" to ``verify_reply``'s consumer
(``runner.py``'s ``OUTCOME_ORDER`` ranks ``refuted`` below
``inconclusive``), which is exactly the bug this closes.

Deliberately the OPPOSITE policy from compaction's #4883/#4899 on an
unsupported model: compaction degrades to ``json_object`` (a
context-window backstop — raising would leave the conversation stuck).
This verifier raises ``StructuredOutputUnsupportedModelError``, mirroring
0062's own ratified policy (``router_loop.py`` §2.1) directly, because
``verify_reply``'s own ``except Exception`` around the judge call already
turns any raise into ``outcome="inconclusive"`` — there is no backstop
role here for a degrade path to protect.

``_supports_structured_output`` and ``_parse_judge_response`` are tested
directly against real litellm (no mock/patch — module-level capability
lookups and pure JSON parsing, not a network call) and real JSON strings,
per CLAUDE.md's real-instances-or-Fake rule.
"""
from __future__ import annotations

import pytest

from reyn.dev.dogfood.verifiers.reply import (
    _default_judge_fn,
    _parse_judge_response,
    _supports_structured_output,
)
from reyn.runtime.errors import StructuredOutputUnsupportedModelError

# ===========================================================================
# _supports_structured_output — real litellm capability table, no mocking
# ===========================================================================


@pytest.mark.asyncio
async def test_known_supported_model_returns_true() -> None:
    """Tier 1: the dogfood judge's own fixed model
    (gemini-2.5-flash-lite) is a real, currently-supported model in
    litellm's capability table — the primary path this verifier takes on
    every call in a healthy environment."""
    assert await _supports_structured_output("gemini-2.5-flash-lite") is True


@pytest.mark.asyncio
async def test_unrecognized_model_returns_false_not_raise() -> None:
    """Tier 1: an unrecognized model name returns False (litellm's own
    real answer for a model it has no capability entry for), never
    raises — the precheck itself must never be the caller's failure."""
    assert await _supports_structured_output("not-a-real-model-xyz-12345") is False


# ===========================================================================
# _parse_judge_response — pure function, real JSON strings, no LLM call
# ===========================================================================


def test_well_formed_response_parses() -> None:
    """Tier 1: a valid {passed, score, reason} object parses through
    unchanged, threshold applied."""
    result = _parse_judge_response('{"passed": true, "score": 0.9, "reason": "good"}')
    assert result == {"passed": True, "score": 0.9, "reason": "good"}


def test_score_below_threshold_is_not_passed() -> None:
    """Tier 1: score below 0.7 threshold -> passed=False, score preserved
    (not coerced to None — this is a real, valid, low score)."""
    result = _parse_judge_response('{"passed": false, "score": 0.2, "reason": "bad"}')
    assert result["passed"] is False
    assert result["score"] == 0.2


def test_fenced_code_block_is_stripped() -> None:
    """Tier 1: a ```json ... ``` fence around the JSON is stripped before
    parsing — matches models that ignore the "no markdown" instruction."""
    raw = '```json\n{"passed": true, "score": 1.0, "reason": "ok"}\n```'
    result = _parse_judge_response(raw)
    assert result == {"passed": True, "score": 1.0, "reason": "ok"}


def test_missing_score_field_is_inconclusive_not_refuted() -> None:
    """Tier 1: THE bug this PR fixes. A missing ``score`` key must not
    silently become 0.0 (= definitively refuted). ``score: None`` routes
    into ``verify_reply``'s existing "judge returned no score" ->
    inconclusive branch."""
    result = _parse_judge_response('{"passed": false, "reason": "no score field"}')
    assert result["score"] is None


def test_malformed_json_is_inconclusive_not_refuted() -> None:
    """Tier 1: unparseable JSON must not silently become a 0.0/refuted
    verdict either — same inconclusive routing as a missing field."""
    result = _parse_judge_response("not json at all")
    assert result["score"] is None


def test_non_numeric_score_is_inconclusive_not_refuted() -> None:
    """Tier 1: a score field present but not coercible to a number
    (the type: ignore-style "silently becomes 0.0" trap) must also route
    to inconclusive, not a false refutation."""
    result = _parse_judge_response('{"passed": false, "score": "not a number", "reason": "x"}')
    assert result["score"] is None


def test_out_of_range_score_is_inconclusive_not_refuted() -> None:
    """Tier 1: a syntactically valid but out-of-contract score (the
    contract is 0.0-1.0, per this module's own docstring) is also not a
    trustworthy verdict — routes to inconclusive rather than being taken
    at face value."""
    result = _parse_judge_response('{"passed": true, "score": 5.0, "reason": "x"}')
    assert result["score"] is None


def test_response_is_not_a_json_object() -> None:
    """Tier 1: valid JSON that isn't an object (e.g. a bare array) must
    not crash on ``.get`` / ``[...]`` access — routes to inconclusive."""
    result = _parse_judge_response("[1, 2, 3]")
    assert result["score"] is None


# ===========================================================================
# _default_judge_fn — raise-on-unsupported-model policy (real function,
# monkeypatching only the model-capability precheck it calls — the LLM
# completion call itself is never reached on this path, so nothing about
# litellm needs faking here).
# ===========================================================================


@pytest.mark.asyncio
async def test_unsupported_model_raises_structured_output_error(monkeypatch) -> None:
    """Tier 1: the raise-on-unsupported policy itself — #4893's actual
    design decision, mirroring 0062 rather than compaction's #4883/#4899
    degrade. Verified by forcing the real precheck function to return
    False (a legitimate real return value of that function, not a faked
    collaborator) and asserting the specific exception type."""
    import reyn.dev.dogfood.verifiers.reply as reply_mod

    async def _always_unsupported(model: str) -> bool:
        return False

    monkeypatch.setattr(reply_mod, "_supports_structured_output", _always_unsupported)

    with pytest.raises(StructuredOutputUnsupportedModelError):
        await _default_judge_fn(["some rubric item"], "some reply")


@pytest.mark.asyncio
async def test_verify_reply_turns_the_raise_into_inconclusive_not_refuted() -> None:
    """Tier 1: the end-to-end contract this whole design leans on —
    ``verify_reply``'s own ``except Exception`` around the judge call
    already exists (pre-#4893) and must turn ANY judge_fn raise into
    outcome="inconclusive", never "refuted". This is what makes
    raise-on-unsupported safe instead of a regression."""
    from reyn.dev.dogfood.scenarios import ExpectedReply
    from reyn.dev.dogfood.verifiers import verify_reply

    async def _raising_judge(rubric: list[str], reply_text: str) -> dict:
        raise StructuredOutputUnsupportedModelError("model doesn't support it")

    expected = ExpectedReply(kind="judge", rubric=["x"])
    result = await verify_reply(expected, "some reply", judge_fn=_raising_judge)
    assert result.outcome == "inconclusive"
