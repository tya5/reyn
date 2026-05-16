"""Tier 1: Contract tests for FP-0036 verifier triad (reply / events / artifacts).

Tests use real instances of the data model classes (from scenarios.py)
and plain async-def stubs for the LLM judge backend.
No MagicMock / AsyncMock / patch used.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from reyn.dogfood.scenarios import (
    ArtifactAssertion,
    EventAssertion,
    ExpectedArtifacts,
    ExpectedEvents,
    ExpectedReply,
)
from reyn.dogfood.verifiers import (
    VerifierResult,
    verify_artifacts,
    verify_events,
    verify_reply,
)

# ===========================================================================
# Helpers / stubs
# ===========================================================================


def _fp(data: dict) -> str:
    """Compute the canonical fingerprint (mirrors artifacts.py)."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _judge_pass(rubric: list[str], reply_text: str) -> dict:
    """Stub judge that always passes (score 1.0)."""
    return {"passed": True, "score": 1.0, "reason": "stub pass"}


async def _judge_fail(rubric: list[str], reply_text: str) -> dict:
    """Stub judge that always fails (score 0.0)."""
    return {"passed": False, "score": 0.0, "reason": "stub fail"}


# ===========================================================================
# verify_reply
# ===========================================================================


class TestVerifyReplySubstring:
    """Tier 1: substring kind contract."""

    @pytest.mark.asyncio
    async def test_substring_present_verified(self):
        """Tier 1: substring value present in reply → verified."""
        expected = ExpectedReply(kind="substring", value="hello")
        result = await verify_reply(expected, "hello world")
        assert result.outcome == "verified"

    @pytest.mark.asyncio
    async def test_substring_absent_refuted(self):
        """Tier 1: substring value absent from reply → refuted."""
        expected = ExpectedReply(kind="substring", value="goodbye")
        result = await verify_reply(expected, "hello world")
        assert result.outcome == "refuted"


class TestVerifyReplyExact:
    """Tier 1: exact kind contract."""

    @pytest.mark.asyncio
    async def test_exact_match_verified(self):
        """Tier 1: exact match (trimmed) → verified."""
        expected = ExpectedReply(kind="exact", value="hello world")
        result = await verify_reply(expected, "  hello world  ")
        assert result.outcome == "verified"

    @pytest.mark.asyncio
    async def test_exact_mismatch_refuted(self):
        """Tier 1: exact mismatch → refuted."""
        expected = ExpectedReply(kind="exact", value="hello world")
        result = await verify_reply(expected, "hello")
        assert result.outcome == "refuted"


class TestVerifyReplyRegex:
    """Tier 1: regex kind contract."""

    @pytest.mark.asyncio
    async def test_regex_matches_verified(self):
        """Tier 1: regex pattern matches reply → verified."""
        expected = ExpectedReply(kind="regex", value=r"\d{3}")
        result = await verify_reply(expected, "code 123 end")
        assert result.outcome == "verified"

    @pytest.mark.asyncio
    async def test_regex_no_match_refuted(self):
        """Tier 1: regex pattern does not match → refuted."""
        expected = ExpectedReply(kind="regex", value=r"\d{3}")
        result = await verify_reply(expected, "no digits here")
        assert result.outcome == "refuted"


class TestVerifyReplyJudge:
    """Tier 1: judge kind contract (LLM backend injected via stub)."""

    @pytest.mark.asyncio
    async def test_judge_pass_verified(self):
        """Tier 1: injected judge returns passed=True → verified."""
        expected = ExpectedReply(kind="judge", rubric=["explains something"])
        result = await verify_reply(expected, "some reply", judge_fn=_judge_pass)
        assert result.outcome == "verified"
        assert result.detail["score"] == 1.0

    @pytest.mark.asyncio
    async def test_judge_fail_refuted(self):
        """Tier 1: injected judge returns passed=False → refuted."""
        expected = ExpectedReply(kind="judge", rubric=["explains something"])
        result = await verify_reply(expected, "some reply", judge_fn=_judge_fail)
        assert result.outcome == "refuted"
        assert result.detail["score"] == 0.0


class TestVerifyReplyEdgeCases:
    """Tier 1: empty reply and missing expected."""

    @pytest.mark.asyncio
    async def test_empty_reply_inconclusive(self):
        """Tier 1: empty reply_text + non-empty expected → inconclusive."""
        expected = ExpectedReply(kind="substring", value="hello")
        result = await verify_reply(expected, "")
        assert result.outcome == "inconclusive"

    @pytest.mark.asyncio
    async def test_whitespace_only_reply_inconclusive(self):
        """Tier 1: whitespace-only reply + non-empty expected → inconclusive."""
        expected = ExpectedReply(kind="exact", value="something")
        result = await verify_reply(expected, "   ")
        assert result.outcome == "inconclusive"

    @pytest.mark.asyncio
    async def test_none_expected_blocked(self):
        """Tier 1: expected=None → blocked."""
        result = await verify_reply(None, "any reply")
        assert result.outcome == "blocked"


# ===========================================================================
# verify_events
# ===========================================================================


def _evt(type_: str, **data) -> dict:
    """Build a minimal event dict."""
    return {"type": type_, "data": data}


class TestVerifyEventsMustEmit:
    """Tier 1: must_emit contract."""

    def test_must_emit_single_type_met(self):
        """Tier 1: must_emit single type present in events → verified."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="skill_run_completed")]
        )
        events = [_evt("skill_run_completed")]
        result = verify_events(expected, events)
        assert result.outcome == "verified"

    def test_must_emit_type_missing_refuted(self):
        """Tier 1: must_emit type not present in events → refuted."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="skill_run_completed")]
        )
        events = [_evt("skill_run_spawned")]
        result = verify_events(expected, events)
        assert result.outcome == "refuted"
        assert any(f["type"] == "skill_run_completed" for f in result.detail["failures"])

    def test_must_emit_count_gte2_met(self):
        """Tier 1: must_emit count '>=2' satisfied when 2+ events present → verified."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="tool_called", count=">=2")]
        )
        events = [_evt("tool_called"), _evt("tool_called"), _evt("tool_called")]
        result = verify_events(expected, events)
        assert result.outcome == "verified"

    def test_must_emit_count_gte2_unmet(self):
        """Tier 1: must_emit count '>=2' unmet when only 1 event → refuted."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="tool_called", count=">=2")]
        )
        events = [_evt("tool_called")]
        result = verify_events(expected, events)
        assert result.outcome == "refuted"

    def test_must_emit_payload_subset_match_verified(self):
        """Tier 1: must_emit payload subset matches event data → verified."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="phase_entered", payload={"phase": "analyse"})]
        )
        events = [_evt("phase_entered", phase="analyse", extra="ignored")]
        result = verify_events(expected, events)
        assert result.outcome == "verified"

    def test_must_emit_payload_subset_mismatch_refuted(self):
        """Tier 1: must_emit payload subset doesn't match event data → refuted."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="phase_entered", payload={"phase": "analyse"})]
        )
        events = [_evt("phase_entered", phase="revise")]
        result = verify_events(expected, events)
        assert result.outcome == "refuted"

    def test_must_emit_status_shorthand_verified(self):
        """Tier 1: must_emit status shorthand expands to payload.status match → verified."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="skill_run_completed", status="success")]
        )
        events = [_evt("skill_run_completed", status="success")]
        result = verify_events(expected, events)
        assert result.outcome == "verified"

    def test_must_emit_status_shorthand_mismatch_refuted(self):
        """Tier 1: must_emit status shorthand mismatches event status → refuted."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="skill_run_completed", status="success")]
        )
        events = [_evt("skill_run_completed", status="error")]
        result = verify_events(expected, events)
        assert result.outcome == "refuted"


class TestVerifyEventsMustNotEmit:
    """Tier 1: must_not_emit contract."""

    def test_must_not_emit_absent_verified(self):
        """Tier 1: must_not_emit type absent from events → verified."""
        expected = ExpectedEvents(
            must_not_emit=[EventAssertion(type="permission_denied")]
        )
        events = [_evt("skill_run_completed")]
        result = verify_events(expected, events)
        assert result.outcome == "verified"

    def test_must_not_emit_present_refuted(self):
        """Tier 1: must_not_emit type present in events → refuted."""
        expected = ExpectedEvents(
            must_not_emit=[EventAssertion(type="permission_denied")]
        )
        events = [_evt("skill_run_completed"), _evt("permission_denied")]
        result = verify_events(expected, events)
        assert result.outcome == "refuted"
        assert any(f["check"] == "must_not_emit" for f in result.detail["failures"])


class TestVerifyEventsSequence:
    """Tier 1: ordered subsequence contract."""

    def test_sequence_in_order_verified(self):
        """Tier 1: required sequence appears as ordered subsequence → verified."""
        expected = ExpectedEvents(sequence=["a", "b", "c"])
        events = [_evt("a"), _evt("x"), _evt("b"), _evt("y"), _evt("c")]
        result = verify_events(expected, events)
        assert result.outcome == "verified"

    def test_sequence_out_of_order_refuted(self):
        """Tier 1: required sequence order violated → refuted."""
        expected = ExpectedEvents(sequence=["a", "b", "c"])
        events = [_evt("a"), _evt("c"), _evt("b")]
        result = verify_events(expected, events)
        assert result.outcome == "refuted"
        assert any(f["check"] == "sequence" for f in result.detail["failures"])


class TestVerifyEventsEdgeCases:
    """Tier 1: edge cases."""

    def test_empty_events_with_assertions_inconclusive(self):
        """Tier 1: empty events list + assertions exist → inconclusive."""
        expected = ExpectedEvents(must_emit=[EventAssertion(type="something")])
        result = verify_events(expected, [])
        assert result.outcome == "inconclusive"

    def test_none_expected_blocked(self):
        """Tier 1: expected=None → blocked."""
        result = verify_events(None, [_evt("something")])
        assert result.outcome == "blocked"


class TestVerifyEventsCountComparators:
    """Tier 1: all count comparator forms."""

    @pytest.mark.parametrize("count_str,actual,should_pass", [
        ("==2", 2, True),
        ("==2", 3, False),
        (">=2", 2, True),
        (">=2", 1, False),
        ("<=3", 3, True),
        ("<=3", 4, False),
        ("<3", 2, True),
        ("<3", 3, False),
        (">1", 2, True),
        (">1", 1, False),
        ("3", 3, True),   # bare integer → ==N
        ("3", 4, False),
    ])
    def test_count_comparator_forms(self, count_str, actual, should_pass):
        """Tier 1: all count comparator forms (==N, >=N, <=N, <N, >N, N) behave correctly."""
        expected = ExpectedEvents(
            must_emit=[EventAssertion(type="ev", count=count_str)]
        )
        events = [_evt("ev") for _ in range(actual)]
        result = verify_events(expected, events)
        expected_outcome = "verified" if should_pass else "refuted"
        assert result.outcome == expected_outcome, (
            f"count={count_str!r}, actual={actual}, expected outcome={expected_outcome!r}, "
            f"got={result.outcome!r}"
        )


# ===========================================================================
# verify_artifacts
# ===========================================================================


def _art(skill: str | None = None, **data) -> dict:
    """Build a minimal artifact dict.

    Pass ``type="..."`` explicitly in data if needed. All other keyword
    arguments become the inner data payload.
    """
    a: dict = {}
    if skill is not None:
        a["skill"] = skill
    # Pull out "type" from data if caller supplied it as a kwarg
    if "type" in data:
        a["type"] = data.pop("type")
    a["data"] = data
    return a


class TestVerifyArtifactsPresent:
    """Tier 1: present=True assertions."""

    def test_present_skill_filter_matches_verified(self):
        """Tier 1: present=True + skill filter matches → verified."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(skill="direct_llm", present=True)])
        artifacts = [_art(skill="direct_llm", key="val")]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "verified"

    def test_present_skill_filter_no_match_refuted(self):
        """Tier 1: present=True + skill filter not matched → refuted."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(skill="direct_llm", present=True)])
        artifacts = [_art(skill="other_skill", key="val")]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "refuted"

    def test_present_type_filter_matches_verified(self):
        """Tier 1: present=True + type filter matches → verified."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(type="summary_result", present=True)])
        artifacts = [{"type": "summary_result", "data": {"content": "text"}}]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "verified"

    def test_present_type_filter_no_match_refuted(self):
        """Tier 1: present=True + type filter not matched → refuted."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(type="summary_result", present=True)])
        artifacts = [{"type": "other_type", "data": {}}]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "refuted"


class TestVerifyArtifactsAbsent:
    """Tier 1: present=False assertions."""

    def test_absent_skill_not_in_artifacts_verified(self):
        """Tier 1: present=False + skill not found → verified."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(skill="direct_llm", present=False)])
        artifacts = [_art(skill="other_skill")]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "verified"

    def test_absent_skill_in_artifacts_refuted(self):
        """Tier 1: present=False + skill found → refuted."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(skill="direct_llm", present=False)])
        artifacts = [_art(skill="direct_llm")]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "refuted"
        assert any(f["check"] == "absent" for f in result.detail["failures"])


class TestVerifyArtifactsFingerprint:
    """Tier 1: fingerprint matching."""

    def test_fingerprint_match_verified(self):
        """Tier 1: fingerprint matches normalised artifact data → verified."""
        data = {"answer": 42, "text": "hello"}
        fp = _fp(data)
        expected = ExpectedArtifacts(
            items=[ArtifactAssertion(skill="my_skill", present=True, fingerprint=fp)]
        )
        artifacts = [{"skill": "my_skill", "data": data}]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "verified"

    def test_fingerprint_mismatch_refuted(self):
        """Tier 1: fingerprint set but artifact data differs → refuted."""
        data = {"answer": 42}
        fp = _fp(data)
        wrong_data = {"answer": 99}
        expected = ExpectedArtifacts(
            items=[ArtifactAssertion(skill="my_skill", present=True, fingerprint=fp)]
        )
        artifacts = [{"skill": "my_skill", "data": wrong_data}]
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "refuted"
        assert any(f["check"] == "fingerprint" for f in result.detail["failures"])

    def test_fingerprint_set_no_matching_artifact_refuted(self):
        """Tier 1: fingerprint set but no matching artifact at all → refuted."""
        data = {"answer": 42}
        fp = _fp(data)
        expected = ExpectedArtifacts(
            items=[ArtifactAssertion(skill="my_skill", present=True, fingerprint=fp)]
        )
        artifacts = []  # no artifacts
        result = verify_artifacts(expected, artifacts)
        assert result.outcome == "refuted"


class TestVerifyArtifactsEdgeCases:
    """Tier 1: edge cases."""

    def test_empty_artifacts_assertion_present_refuted(self):
        """Tier 1: empty artifacts list + present=True assertion → refuted."""
        expected = ExpectedArtifacts(items=[ArtifactAssertion(skill="any_skill", present=True)])
        result = verify_artifacts(expected, [])
        assert result.outcome == "refuted"

    def test_none_expected_blocked(self):
        """Tier 1: expected=None → blocked."""
        result = verify_artifacts(None, [_art(skill="direct_llm")])
        assert result.outcome == "blocked"

    def test_no_assertions_empty_artifacts_verified(self):
        """Tier 1: no assertions declared → vacuously verified."""
        expected = ExpectedArtifacts(items=[])
        result = verify_artifacts(expected, [])
        assert result.outcome == "verified"
