"""Tier 1: Contract tests for the FP-0036 dogfood scenario set YAML loader.

Verifies the public API of ``src/reyn/dogfood/scenarios.py`` including:
  - ScenarioSet / Scenario / ExpectedReply / ExpectedEvents / ExpectedArtifacts /
    OutcomePrediction / EventAssertion / ArtifactAssertion dataclass shapes
  - load_scenario_set() validation and error paths
  - Backward compatibility with long_session_v1.yaml (legacy metadata format)
  - scenario_by_id() accessor
  - is_multi_turn property

Policy: NEVER use MagicMock / AsyncMock / patch. All tests use real instances
or inline YAML fixtures written via tmp_path.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reyn.dogfood import (
    ArtifactAssertion,
    EventAssertion,
    ExpectedArtifacts,
    ExpectedEvents,
    ExpectedReply,
    OutcomePrediction,
    Scenario,
    ScenarioLoadError,
    ScenarioSet,
    load_scenario_set,
)


# ── helpers ───────────────────────────────────────────────────────────────


def write_yaml(tmp_path: Path, content: str, filename: str = "test_set.yaml") -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ── 1. Minimal valid set ──────────────────────────────────────────────────


def test_minimal_valid_set_loads(tmp_path: Path) -> None:
    """Tier 1: Minimal valid YAML (type + name + 1 input scenario) loads without error."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: smoke_minimal
        scenarios:
          - id: s1
            input: "Hello world"
        """,
    )
    ss = load_scenario_set(p)
    assert isinstance(ss, ScenarioSet)
    assert ss.name == "smoke_minimal"
    assert len(ss.scenarios) == 1
    scenario = ss.scenarios[0]
    assert scenario.id == "s1"
    assert scenario.input == "Hello world"
    assert scenario.prompts == []
    assert scenario.expected_reply is None
    assert scenario.expected_events is None
    assert scenario.expected_artifacts is None
    assert scenario.outcome_prediction is None
    assert ss.source_path == str(p.resolve())


# ── 2. Multi-turn set with prompts ────────────────────────────────────────


def test_multi_turn_set_loads(tmp_path: Path) -> None:
    """Tier 1: Scenario with 'prompts' list (multi-turn) loads without error."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: multi_turn_smoke
        scenarios:
          - id: mt1
            kind: research_chain
            prompts:
              - "What is Reyn?"
              - "How does the skill system work?"
              - "What is a Phase?"
        """,
    )
    ss = load_scenario_set(p)
    assert len(ss.scenarios) == 1
    s = ss.scenarios[0]
    assert s.id == "mt1"
    assert s.kind == "research_chain"
    assert s.prompts == [
        "What is Reyn?",
        "How does the skill system work?",
        "What is a Phase?",
    ]
    assert s.input is None


# ── 3. is_multi_turn flag ─────────────────────────────────────────────────


def test_is_multi_turn_flag(tmp_path: Path) -> None:
    """Tier 1: is_multi_turn is True for prompts and False for input."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: flag_test
        scenarios:
          - id: single
            input: "one-shot"
          - id: multi
            prompts:
              - "turn 1"
              - "turn 2"
        """,
    )
    ss = load_scenario_set(p)
    assert ss.scenarios[0].is_multi_turn is False
    assert ss.scenarios[1].is_multi_turn is True


# ── 4. Backward compat: long_session_v1.yaml ─────────────────────────────


def test_long_session_v1_loads(tmp_path: Path) -> None:
    """Tier 1: load_scenario_set accepts long_session_v1.yaml; mixed expected coverage.

    After the S5 back-fill wave (FP-0036 scenario authoring), some scenarios in
    this file have expected blocks and some don't — both shapes must load.
    """
    long_session_path = (
        Path(__file__).parent.parent / "dogfood" / "scenarios" / "long_session_v1.yaml"
    )
    ss = load_scenario_set(long_session_path)
    assert isinstance(ss, ScenarioSet)
    assert ss.name == "long_session_v1"
    assert len(ss.scenarios) == 7
    for s in ss.scenarios:
        # All scenarios are multi-turn (= prompts: [...])
        assert s.is_multi_turn is True
    # At least one scenario has expected back-filled (= S5 wave); at least one
    # remains expected-less (= legacy shape still accepted).
    with_expected = [s for s in ss.scenarios if s.expected_reply is not None]
    without_expected = [s for s in ss.scenarios if s.expected_reply is None]
    assert len(with_expected) >= 1, "expected back-fill applied to at least one scenario"
    assert len(without_expected) >= 1, "legacy expected-less scenarios still load"


# ── 5. type mismatch raises ───────────────────────────────────────────────


def test_type_mismatch_raises(tmp_path: Path) -> None:
    """Tier 1: 'type: eval' raises ScenarioLoadError (not dogfood_scenario_set)."""
    p = write_yaml(
        tmp_path,
        """\
        type: eval
        name: wrong_type
        scenarios:
          - id: s1
            input: "hi"
        """,
    )
    with pytest.raises(ScenarioLoadError, match="type"):
        load_scenario_set(p)


def test_type_absent_without_metadata_raises(tmp_path: Path) -> None:
    """Tier 1: Missing 'type' and no legacy metadata raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        """\
        name: no_type
        scenarios:
          - id: s1
            input: "hi"
        """,
    )
    with pytest.raises(ScenarioLoadError, match="type"):
        load_scenario_set(p)


# ── 6. Duplicate scenario id raises ──────────────────────────────────────


def test_duplicate_scenario_id_raises(tmp_path: Path) -> None:
    """Tier 1: Two scenarios with the same id raise ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: dup_test
        scenarios:
          - id: s1
            input: "first"
          - id: s1
            input: "second"
        """,
    )
    with pytest.raises(ScenarioLoadError, match="duplicate"):
        load_scenario_set(p)


# ── 7. Both input and prompts raises ─────────────────────────────────────


def test_both_input_and_prompts_raises(tmp_path: Path) -> None:
    """Tier 1: Setting both 'input' and 'prompts' raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: exclusive_test
        scenarios:
          - id: s1
            input: "one-shot"
            prompts:
              - "turn 1"
        """,
    )
    with pytest.raises(ScenarioLoadError, match="mutually exclusive"):
        load_scenario_set(p)


# ── 8. Neither input nor prompts raises ──────────────────────────────────


def test_neither_input_nor_prompts_raises(tmp_path: Path) -> None:
    """Tier 1: A scenario with neither 'input' nor 'prompts' raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: empty_test
        scenarios:
          - id: s1
            kind: general_topic
        """,
    )
    with pytest.raises(ScenarioLoadError, match="at least one of"):
        load_scenario_set(p)


# ── 9. ExpectedReply kind=judge requires rubric ───────────────────────────


def test_expected_reply_judge_requires_rubric(tmp_path: Path) -> None:
    """Tier 1: reply kind='judge' with empty rubric raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: rubric_test
        scenarios:
          - id: s1
            input: "hello"
            expected:
              reply:
                kind: judge
                rubric: []
        """,
    )
    with pytest.raises(ScenarioLoadError, match="rubric"):
        load_scenario_set(p)


def test_expected_reply_judge_with_rubric_loads(tmp_path: Path) -> None:
    """Tier 1: reply kind='judge' with non-empty rubric loads correctly."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: rubric_ok
        scenarios:
          - id: s1
            input: "hello"
            expected:
              reply:
                kind: judge
                rubric:
                  - "explains capabilities"
                  - "mentions skills"
        """,
    )
    ss = load_scenario_set(p)
    er = ss.scenarios[0].expected_reply
    assert er is not None
    assert er.kind == "judge"
    assert er.rubric == ["explains capabilities", "mentions skills"]
    assert er.value == ""


# ── 10. ExpectedReply kind=substring/exact/regex requires value ───────────


@pytest.mark.parametrize("kind", ["substring", "exact", "regex"])
def test_expected_reply_non_judge_requires_value(tmp_path: Path, kind: str) -> None:
    """Tier 1: reply kind=substring/exact/regex without 'value' raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        f"""\
        type: dogfood_scenario_set
        name: value_test
        scenarios:
          - id: s1
            input: "hello"
            expected:
              reply:
                kind: {kind}
        """,
    )
    with pytest.raises(ScenarioLoadError, match="value"):
        load_scenario_set(p)


@pytest.mark.parametrize("kind", ["substring", "exact", "regex"])
def test_expected_reply_non_judge_with_value_loads(tmp_path: Path, kind: str) -> None:
    """Tier 1: reply kind=substring/exact/regex with non-empty value loads correctly."""
    p = write_yaml(
        tmp_path,
        f"""\
        type: dogfood_scenario_set
        name: value_ok
        scenarios:
          - id: s1
            input: "hello"
            expected:
              reply:
                kind: {kind}
                value: "expected text"
        """,
    )
    ss = load_scenario_set(p)
    er = ss.scenarios[0].expected_reply
    assert er is not None
    assert er.kind == kind
    assert er.value == "expected text"


# ── 11. EventAssertion count comparator parsing ───────────────────────────


@pytest.mark.parametrize(
    "count_str, expected_stored",
    [
        ("==2", "==2"),
        (">=1", ">=1"),
        ("<=5", "<=5"),
        ("<3", "<3"),
        (">0", ">0"),
        ("1", "1"),    # bare integer — accepted, stored as-is
        ("10", "10"),
    ],
)
def test_event_assertion_count_valid(tmp_path: Path, count_str: str, expected_stored: str) -> None:
    """Tier 1: Valid count comparator forms are parsed and stored correctly."""
    p = write_yaml(
        tmp_path,
        f"""\
        type: dogfood_scenario_set
        name: count_test
        scenarios:
          - id: s1
            input: "hello"
            expected:
              events:
                must_emit:
                  - type: skill_run_spawned
                    count: "{count_str}"
        """,
    )
    ss = load_scenario_set(p)
    ee = ss.scenarios[0].expected_events
    assert ee is not None
    assert len(ee.must_emit) == 1
    assert ee.must_emit[0].count == expected_stored


# ── 12. EventAssertion malformed count raises ─────────────────────────────


@pytest.mark.parametrize("bad_count", [">=", "abc", "1.5", "!3", "~2"])
def test_event_assertion_malformed_count_raises(tmp_path: Path, bad_count: str) -> None:
    """Tier 1: Malformed count comparator raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        f"""\
        type: dogfood_scenario_set
        name: bad_count
        scenarios:
          - id: s1
            input: "hello"
            expected:
              events:
                must_emit:
                  - type: skill_run_spawned
                    count: "{bad_count}"
        """,
    )
    with pytest.raises(ScenarioLoadError, match="count"):
        load_scenario_set(p)


# ── 13. OutcomePrediction sum validation ─────────────────────────────────


def test_outcome_prediction_bad_sum_raises(tmp_path: Path) -> None:
    """Tier 1: outcome_prediction summing to 0.9 raises ScenarioLoadError."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: brier_test
        scenarios:
          - id: s1
            input: "hello"
            outcome_prediction:
              verified: 0.5
              inconclusive: 0.2
              refuted: 0.1
              blocked: 0.1
        """,
    )
    with pytest.raises(ScenarioLoadError, match="sum"):
        load_scenario_set(p)


def test_outcome_prediction_sum_one_ok(tmp_path: Path) -> None:
    """Tier 1: outcome_prediction summing to 1.0 loads without error."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: brier_ok
        scenarios:
          - id: s1
            input: "hello"
            outcome_prediction:
              verified: 0.7
              inconclusive: 0.2
              refuted: 0.05
              blocked: 0.05
        """,
    )
    ss = load_scenario_set(p)
    op = ss.scenarios[0].outcome_prediction
    assert op is not None
    assert isinstance(op, OutcomePrediction)
    assert abs(op.verified - 0.7) < 1e-9
    assert abs(op.inconclusive - 0.2) < 1e-9
    assert abs(op.refuted - 0.05) < 1e-9
    assert abs(op.blocked - 0.05) < 1e-9


def test_outcome_prediction_sum_within_tolerance_ok(tmp_path: Path) -> None:
    """Tier 1: outcome_prediction summing to 1.0005 (within 0.001 tolerance) loads ok."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: brier_tolerance
        scenarios:
          - id: s1
            input: "hello"
            outcome_prediction:
              verified: 0.70025
              inconclusive: 0.20025
              refuted: 0.05
              blocked: 0.05
        """,
    )
    # 0.70025 + 0.20025 + 0.05 + 0.05 = 1.0005, within tolerance
    ss = load_scenario_set(p)
    assert ss.scenarios[0].outcome_prediction is not None


# ── 14. scenario_by_id lookup ─────────────────────────────────────────────


def test_scenario_by_id_found(tmp_path: Path) -> None:
    """Tier 1: scenario_by_id returns the correct scenario when id exists."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: lookup_test
        scenarios:
          - id: s1
            input: "first"
          - id: s2
            input: "second"
        """,
    )
    ss = load_scenario_set(p)
    s = ss.scenario_by_id("s2")
    assert s is not None
    assert s.id == "s2"
    assert s.input == "second"


def test_scenario_by_id_unknown_returns_none(tmp_path: Path) -> None:
    """Tier 1: scenario_by_id returns None for an unknown id."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: lookup_none
        scenarios:
          - id: s1
            input: "only"
        """,
    )
    ss = load_scenario_set(p)
    assert ss.scenario_by_id("nonexistent") is None


# ── 15. ArtifactAssertion optional fields ────────────────────────────────


def test_artifact_assertion_optional_fields_load(tmp_path: Path) -> None:
    """Tier 1: ArtifactAssertion with optional skill/type/present/fingerprint loads correctly."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: artifact_test
        scenarios:
          - id: s1
            input: "hello"
            expected:
              artifacts:
                - skill: direct_llm
                  present: true
                - type: plan_artifact
                  present: false
                  fingerprint: "abc123def456"
                - skill: eval_skill
                  type: eval_result
        """,
    )
    ss = load_scenario_set(p)
    ea = ss.scenarios[0].expected_artifacts
    assert ea is not None
    assert isinstance(ea, ExpectedArtifacts)
    items = ea.items
    assert len(items) == 3

    assert items[0].skill == "direct_llm"
    assert items[0].type is None
    assert items[0].present is True
    assert items[0].fingerprint is None

    assert items[1].skill is None
    assert items[1].type == "plan_artifact"
    assert items[1].present is False
    assert items[1].fingerprint == "abc123def456"

    assert items[2].skill == "eval_skill"
    assert items[2].type == "eval_result"
    assert items[2].present is True


# ── full-featured scenario smoke ──────────────────────────────────────────


def test_full_featured_scenario_loads(tmp_path: Path) -> None:
    """Tier 1: A scenario with all optional fields set loads without error."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: full_test
        description: Full-feature scenario set
        covers:
          - chat-router/intent-routing
          - stdlib-skill/direct_llm
        scenarios:
          - id: full_s1
            covers:
              - chat-router/intent-routing
            input: "こんにちは、何ができますか?"
            kind: greeting
            expected:
              reply:
                kind: judge
                rubric:
                  - explains capabilities at high level
                  - mentions chat / skills / agents
              events:
                must_emit:
                  - type: skill_run_spawned
                    count: ">=1"
                  - type: skill_run_completed
                    status: success
                must_not_emit:
                  - type: permission_denied
                sequence:
                  - skill_run_spawned
                  - skill_run_completed
              artifacts:
                - skill: direct_llm
                  present: true
            outcome_prediction:
              verified: 0.7
              inconclusive: 0.2
              refuted: 0.05
              blocked: 0.05
        """,
    )
    ss = load_scenario_set(p)
    assert ss.name == "full_test"
    assert ss.description == "Full-feature scenario set"
    assert ss.covers == ["chat-router/intent-routing", "stdlib-skill/direct_llm"]

    s = ss.scenarios[0]
    assert s.id == "full_s1"
    assert s.covers == ["chat-router/intent-routing"]
    assert s.input == "こんにちは、何ができますか?"
    assert s.kind == "greeting"
    assert s.is_multi_turn is False

    er = s.expected_reply
    assert er is not None
    assert er.kind == "judge"
    assert "explains capabilities at high level" in er.rubric

    ee = s.expected_events
    assert ee is not None
    assert len(ee.must_emit) == 2
    assert ee.must_emit[0].type == "skill_run_spawned"
    assert ee.must_emit[0].count == ">=1"
    assert ee.must_emit[1].type == "skill_run_completed"
    assert ee.must_emit[1].status == "success"
    assert len(ee.must_not_emit) == 1
    assert ee.must_not_emit[0].type == "permission_denied"
    assert ee.sequence == ["skill_run_spawned", "skill_run_completed"]

    ea = s.expected_artifacts
    assert ea is not None
    assert ea.items[0].skill == "direct_llm"
    assert ea.items[0].present is True

    op = s.outcome_prediction
    assert op is not None
    assert abs(op.verified - 0.7) < 1e-9


# ── file-not-found raises ─────────────────────────────────────────────────


def test_file_not_found_raises(tmp_path: Path) -> None:
    """Tier 1: load_scenario_set raises ScenarioLoadError for a non-existent path."""
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ScenarioLoadError, match="not found"):
        load_scenario_set(missing)


# ── legacy backward compat: metadata.name fallback ───────────────────────


def test_legacy_user_prompt_field_aliases_input(tmp_path: Path) -> None:
    """Tier 1: Legacy single-turn YAML with 'user_prompt:' loads as input.

    The pre-FP-0036 dogfood/scenarios/fp_0011_*.yaml files use ``user_prompt``
    as the single-turn input field. The loader accepts it as an alias for
    ``input`` so legacy files stay runnable.
    """
    p = write_yaml(
        tmp_path,
        """\
        metadata:
          name: legacy_user_prompt_set
        scenarios:
          - id: legacy_one
            user_prompt: "Build a skill for X"
        """,
    )
    ss = load_scenario_set(p)
    assert ss.name == "legacy_user_prompt_set"
    assert ss.scenarios[0].id == "legacy_one"
    assert ss.scenarios[0].input == "Build a skill for X"
    assert ss.scenarios[0].is_multi_turn is False


def test_legacy_metadata_name_used_as_set_name(tmp_path: Path) -> None:
    """Tier 1: Legacy format with 'metadata.name' sets ScenarioSet.name correctly."""
    p = write_yaml(
        tmp_path,
        """\
        metadata:
          name: legacy_set
          description: a legacy scenario set
          version: 1
        scenarios:
          - id: leg1
            kind: general_topic
            prompts:
              - "What is Reyn?"
        """,
    )
    ss = load_scenario_set(p)
    assert ss.name == "legacy_set"
    assert ss.description == "a legacy scenario set"
    assert ss.scenarios[0].id == "leg1"
    assert ss.scenarios[0].is_multi_turn is True
    assert ss.scenarios[0].expected_reply is None


# ── top-level name overrides metadata.name ────────────────────────────────


def test_top_level_name_overrides_metadata_name(tmp_path: Path) -> None:
    """Tier 1: Top-level 'name' takes precedence over 'metadata.name'."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: top_level_name
        metadata:
          name: metadata_name
        scenarios:
          - id: s1
            input: "hello"
        """,
    )
    ss = load_scenario_set(p)
    assert ss.name == "top_level_name"


# ── set-level covers ──────────────────────────────────────────────────────


def test_set_level_covers_parsed(tmp_path: Path) -> None:
    """Tier 1: Set-level 'covers' tags are parsed into the ScenarioSet."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: covers_test
        covers:
          - os-core/phase-engine
          - stdlib-skill/direct_llm
        scenarios:
          - id: s1
            input: "hello"
        """,
    )
    ss = load_scenario_set(p)
    assert ss.covers == ["os-core/phase-engine", "stdlib-skill/direct_llm"]


# ── EventAssertion payload subset semantics (data model) ─────────────────


def test_event_assertion_payload_stored(tmp_path: Path) -> None:
    """Tier 1: EventAssertion.payload dict is stored as-is for verifier subset matching."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: payload_test
        scenarios:
          - id: s1
            input: "hello"
            expected:
              events:
                must_emit:
                  - type: skill_run_completed
                    payload:
                      skill_name: direct_llm
                      status: success
        """,
    )
    ss = load_scenario_set(p)
    ea = ss.scenarios[0].expected_events
    assert ea is not None
    assert ea.must_emit[0].payload == {"skill_name": "direct_llm", "status": "success"}


# ── events sequence ───────────────────────────────────────────────────────


def test_events_sequence_parsed(tmp_path: Path) -> None:
    """Tier 1: events.sequence is stored as a list of event type strings."""
    p = write_yaml(
        tmp_path,
        """\
        type: dogfood_scenario_set
        name: seq_test
        scenarios:
          - id: s1
            input: "hello"
            expected:
              events:
                sequence:
                  - skill_run_spawned
                  - skill_run_completed
                  - workspace_written
        """,
    )
    ss = load_scenario_set(p)
    ee = ss.scenarios[0].expected_events
    assert ee is not None
    assert ee.sequence == ["skill_run_spawned", "skill_run_completed", "workspace_written"]
