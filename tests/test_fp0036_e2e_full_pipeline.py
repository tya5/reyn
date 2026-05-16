"""Tier 2c: End-to-end pipeline integration test for FP-0036 Dogfood Scenario Framework.

Exercises the full pipeline without a live LLM:
  F1 load_scenario_set → F2 run_scenario_set (injected runner_fn) →
  summary.json written → F2 compare_runs regression detection →
  F4 coverage matrix → F5 replay fixture round-trip.

Skips gracefully (pytest.skip) if any required component hasn't landed yet.

Design note:
  This is classified Tier 2c (multi-component integration, not Tier 3) because
  the LLM is faked via a stub runner_fn callable — not via LLMReplay fixtures
  against the real litellm boundary.  The replay round-trip sub-test (step 8)
  uses LLMReplay directly and is the only part that is Tier 3 adjacent.
"""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# ---------------------------------------------------------------------------
# Guard: skip entire module if required components are absent
# ---------------------------------------------------------------------------

try:
    from reyn.dogfood.scenarios import (
        Scenario,
        ScenarioSet,
        load_scenario_set,
    )
except ImportError as _e:
    pytest.skip(
        f"F1 (reyn.dogfood.scenarios) not yet available: {_e}",
        allow_module_level=True,
    )

try:
    from reyn.dogfood.runner import (
        ScenarioRunResult,
        run_scenario_set,
    )
except ImportError as _e:
    pytest.skip(
        f"F2 (reyn.dogfood.runner) not yet available: {_e}",
        allow_module_level=True,
    )

try:
    from reyn.dogfood.compare import compare_runs
except ImportError as _e:
    pytest.skip(
        f"F2b (reyn.dogfood.compare) not yet available: {_e}",
        allow_module_level=True,
    )

# F3 and F4 are soft-skipped per test (not module-level)

# ---------------------------------------------------------------------------
# Helper: minimal scenario set YAML
# ---------------------------------------------------------------------------

_SCENARIO_SET_YAML = textwrap.dedent("""\
    type: dogfood_scenario_set
    name: fp0036_e2e_test_set
    description: "E2E test fixture for FP-0036 pipeline"
    covers:
      - os-core
    scenarios:
      - id: verified_scenario
        covers: [os-core]
        input: "Hello, world!"
        expected:
          reply:
            kind: substring
            value: "ok"
          events:
            must_emit:
              - {type: skill_run_started, count: ">=1"}

      - id: refuted_scenario
        covers: [os-core]
        input: "Do something"
        expected:
          reply:
            kind: substring
            value: "never present"
""")


# ---------------------------------------------------------------------------
# Deterministic stub runner_fn
# ---------------------------------------------------------------------------

async def _stub_runner_fn(scenario: Scenario) -> ScenarioRunResult:
    """Deterministic runner that returns fixed results per scenario_id.

    verified_scenario → overall verified (all three verifiers: verified)
    refuted_scenario  → overall refuted  (all three verifiers: refuted)

    Note: ScenarioRunResult.__post_init__ recomputes overall_outcome as the
    worst-case of (reply_outcome, events_outcome, artifacts_outcome). To get
    overall_outcome == "verified" all three must be "verified"; likewise for
    "refuted". Using "blocked" for any sub-verifier would pull the overall to
    "blocked" (rank 0 = worst) and make regression detection trivially fail.
    """
    if scenario.id == "verified_scenario":
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="ok",
            events=[{"type": "skill_run_started", "data": {}}],
            artifacts=[],
            reply_outcome="verified",
            events_outcome="verified",
            artifacts_outcome="verified",
        )
    if scenario.id == "refuted_scenario":
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="something else",
            events=[],
            artifacts=[],
            reply_outcome="refuted",
            events_outcome="refuted",
            artifacts_outcome="refuted",
        )
    # Fallback for any unexpected scenario
    return ScenarioRunResult(
        scenario_id=scenario.id,
        reply_text="",
        events=[],
        artifacts=[],
        reply_outcome="inconclusive",
        events_outcome="inconclusive",
        artifacts_outcome="inconclusive",
    )


async def _stub_runner_fn_degraded(scenario: Scenario) -> ScenarioRunResult:
    """Like _stub_runner_fn but with verified_scenario degraded to refuted.

    Used as the 'candidate' run to exercise regression detection.
    verified_scenario: all verifiers → refuted (was all verified in baseline).
    refuted_scenario: unchanged.
    """
    if scenario.id == "verified_scenario":
        # Degraded: verified → refuted on all sub-verifiers so overall = refuted
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="something wrong",
            events=[],
            artifacts=[],
            reply_outcome="refuted",
            events_outcome="refuted",
            artifacts_outcome="refuted",
        )
    if scenario.id == "refuted_scenario":
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="something else",
            events=[],
            artifacts=[],
            reply_outcome="refuted",
            events_outcome="refuted",
            artifacts_outcome="refuted",
        )
    return ScenarioRunResult(
        scenario_id=scenario.id,
        reply_text="",
        events=[],
        artifacts=[],
        reply_outcome="inconclusive",
        events_outcome="inconclusive",
        artifacts_outcome="inconclusive",
    )


# ---------------------------------------------------------------------------
# Main pipeline test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_scenario_load_run_summary(tmp_path: Path):
    """Tier 2c: full pipeline — load → run → summary.json → aggregate counts."""
    # 1. Write scenario set YAML to tmp_path
    yaml_path = tmp_path / "fp0036_e2e_test_set.yaml"
    yaml_path.write_text(_SCENARIO_SET_YAML, encoding="utf-8")

    # 2. Load via F1
    scenario_set = load_scenario_set(yaml_path)
    assert scenario_set.name == "fp0036_e2e_test_set"
    assert len(scenario_set.scenarios) == 2
    ids = {s.id for s in scenario_set.scenarios}
    assert ids == {"verified_scenario", "refuted_scenario"}

    # 3. Run via F2 with injected stub runner_fn (no live LLM)
    storage_dir = tmp_path / "run_baseline"
    run_result = await run_scenario_set(
        scenario_set,
        run_id="baseline-run-001",
        storage_dir=storage_dir,
        runner_fn=_stub_runner_fn,
    )

    # 4. summary.json must exist and contain 4-band counts
    summary_path = storage_dir / "summary.json"
    assert summary_path.exists(), "summary.json not written"
    summary = json.loads(summary_path.read_text())

    assert "run_id" in summary
    assert "set_name" in summary
    assert summary["set_name"] == "fp0036_e2e_test_set"

    # 4-band keys must be present
    for band in ("verified", "inconclusive", "refuted", "blocked"):
        assert band in summary, f"4-band key '{band}' missing from summary.json"

    assert summary["total"] == 2
    # verified_scenario → worst-case of (verified, verified, verified) = verified
    # refuted_scenario → worst-case of (refuted, refuted, refuted) = refuted
    assert summary["total"] == run_result.aggregate()["total"]

    # Per-scenario output.json must exist
    for scenario_id in ("verified_scenario", "refuted_scenario"):
        output_file = storage_dir / "scenarios" / scenario_id / "output.json"
        assert output_file.exists(), f"output.json missing for {scenario_id}"
        output = json.loads(output_file.read_text())
        assert output["scenario_id"] == scenario_id
        assert "overall_outcome" in output


@pytest.mark.asyncio
async def test_compare_runs_detects_regression(tmp_path: Path):
    """Tier 2c: compare_runs detects regression when verified_scenario degrades to refuted."""
    yaml_path = tmp_path / "fp0036_e2e_test_set.yaml"
    yaml_path.write_text(_SCENARIO_SET_YAML, encoding="utf-8")
    scenario_set = load_scenario_set(yaml_path)

    # Baseline run: verified_scenario is best (verified outcomes)
    baseline_dir = tmp_path / "run_baseline"
    baseline = await run_scenario_set(
        scenario_set,
        run_id="baseline-run",
        storage_dir=baseline_dir,
        runner_fn=_stub_runner_fn,
    )

    # Candidate run: verified_scenario degrades to refuted
    candidate_dir = tmp_path / "run_candidate"
    candidate = await run_scenario_set(
        scenario_set,
        run_id="candidate-run",
        storage_dir=candidate_dir,
        runner_fn=_stub_runner_fn_degraded,
    )

    # 5. Compare via F2 compare_runs
    report = compare_runs(baseline, candidate)

    # 6. Regression must be detected: verified_scenario degraded
    assert report.regression_detected, (
        f"Expected regression to be detected. "
        f"baseline_verified_rate={report.baseline_verified_rate}, "
        f"candidate_verified_rate={report.candidate_verified_rate}, "
        f"deltas={report.deltas}"
    )


@pytest.mark.asyncio
async def test_coverage_finds_covers_tags(tmp_path: Path):
    """Tier 2c: compute_coverage maps scenario covers: tags to feature-map paths."""
    try:
        from reyn.dogfood.coverage import compute_coverage, CoverageMatrix
    except ImportError as exc:
        pytest.skip(f"F4 (reyn.dogfood.coverage) not yet available: {exc}")

    # Use a minimal synthetic feature-map.md so this test is self-contained
    feature_map_path = tmp_path / "feature-map.md"
    feature_map_path.write_text(
        textwrap.dedent("""\
            # Feature Map

            ### OS Core

            #### Phase Engine

            | Feature | Notes |
            |---|---|
            | Act/Decide loop | core |
        """),
        encoding="utf-8",
    )

    yaml_path = tmp_path / "fp0036_e2e_test_set.yaml"
    yaml_path.write_text(_SCENARIO_SET_YAML, encoding="utf-8")
    scenario_set = load_scenario_set(yaml_path)

    matrix = compute_coverage([scenario_set], feature_map_path)

    # "os-core" should be in the feature map (it's an ### heading)
    feature_paths = {f.path for f in matrix.features}
    assert "os-core" in feature_paths, (
        f"Expected 'os-core' in feature_paths. Got: {sorted(feature_paths)}"
    )

    # Both scenarios declare covers: [os-core] — coverage_map["os-core"] must
    # have entries for them
    os_core_refs = matrix.coverage_map.get("os-core", [])
    assert len(os_core_refs) == 2, (
        f"Expected 2 scenario refs for 'os-core', got {os_core_refs}"
    )

    scenario_ids = {sid for _, sid in os_core_refs}
    assert "verified_scenario" in scenario_ids
    assert "refuted_scenario" in scenario_ids


@pytest.mark.asyncio
async def test_replay_fixture_round_trip(tmp_path: Path):
    """Tier 2c: replay fixture round-trip — F5 scenario_replay_context record then replay.

    Exercises the LLMReplay integration directly:
      1. On first call (fixture absent): record mode activated; parent dir created.
      2. Manually write a fixture JSONL so the second call sees an existing file.
      3. On second call (fixture present): replay mode activated; no MissingFixture.

    Note: LLMReplay.flush() only writes the fixture file when there are pending
    recorded entries (= actual LLM calls were made).  An empty record session
    does NOT create the file, which is correct — recording with zero LLM calls
    means there is nothing to persist.  The test simulates a "post-record"
    fixture by writing the file manually between the two context calls.
    """
    try:
        from reyn.dogfood.replay import scenario_replay_context, fixture_path_for
    except ImportError as exc:
        pytest.skip(f"F5 (reyn.dogfood.replay) not yet available: {exc}")

    try:
        from reyn.testing.replay import LLMReplay
    except ImportError as exc:
        pytest.skip(f"reyn.testing.replay.LLMReplay not available: {exc}")

    fixture_dir = tmp_path / "fixtures"
    set_name = "fp0036_e2e_test_set"
    scenario_id = "verified_scenario"

    # --- First call: record mode (fixture absent) ---
    fpath = fixture_path_for(fixture_dir, set_name, scenario_id)
    assert not fpath.exists(), "Fixture should not exist before record"

    async with scenario_replay_context(fixture_dir, set_name, scenario_id) as replay:
        assert replay.mode == "record"
        # Parent dir must have been created even though no LLM calls happen.
        assert fpath.parent.exists(), "Parent directory must be created in record mode"
        # No LLM calls → no pending entries → flush() writes nothing.

    # The fixture file does NOT exist after an empty record session —
    # flush() only writes when there are pending entries.  Manually seed
    # the fixture with an empty-but-valid JSONL to simulate a post-record state.
    fpath.write_text("", encoding="utf-8")
    assert fpath.exists()

    # --- Second call: replay mode (fixture present) ---
    async with scenario_replay_context(fixture_dir, set_name, scenario_id) as replay2:
        assert replay2.mode == "replay"
        # Replay mode with no LLM calls → no MissingFixture raised
        # (zero calls against an empty fixture is consistent).


@pytest.mark.asyncio
async def test_replay_run_via_runner_seam(tmp_path: Path):
    """Tier 2c: replay_fixture_dir parameter on run_scenario_set activates F5.

    Verifies the seam: when replay_fixture_dir is set, run_scenario_set
    imports replay.replay_run instead of the injected runner_fn.
    """
    try:
        from reyn.dogfood.replay import replay_run  # noqa: F401 — existence check
    except ImportError as exc:
        pytest.skip(f"F5 (reyn.dogfood.replay) not yet available: {exc}")

    yaml_path = tmp_path / "fp0036_e2e_test_set.yaml"
    yaml_path.write_text(_SCENARIO_SET_YAML, encoding="utf-8")
    scenario_set = load_scenario_set(yaml_path)

    fixture_dir = tmp_path / "replay_fixtures"
    storage_dir = tmp_path / "run_replay"

    # Run with replay_fixture_dir — triggers the F5 path in the runner.
    # Results will be inconclusive (MVP stub) but the pipeline must complete
    # without ImportError or crash.
    run_result = await run_scenario_set(
        scenario_set,
        run_id="replay-run-001",
        storage_dir=storage_dir,
        replay_fixture_dir=fixture_dir,
    )

    assert run_result.run_id == "replay-run-001"
    assert len(run_result.scenario_results) == 2

    summary_path = storage_dir / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["total"] == 2


@pytest.mark.asyncio
async def test_scenario_set_aggregate_counts_four_bands(tmp_path: Path):
    """Tier 2c: aggregate() always emits all four 4-band keys regardless of run content."""
    yaml_path = tmp_path / "fp0036_e2e_test_set.yaml"
    yaml_path.write_text(_SCENARIO_SET_YAML, encoding="utf-8")
    scenario_set = load_scenario_set(yaml_path)

    storage_dir = tmp_path / "run_agg"
    run_result = await run_scenario_set(
        scenario_set,
        run_id="agg-test-001",
        storage_dir=storage_dir,
        runner_fn=_stub_runner_fn,
    )

    agg = run_result.aggregate()
    for band in ("verified", "inconclusive", "refuted", "blocked"):
        assert band in agg, f"aggregate() missing band '{band}'"
        assert isinstance(agg[band], int), f"aggregate()['{band}'] must be int"

    assert agg["total"] == 2
    assert agg["total"] == sum(agg[b] for b in ("verified", "inconclusive", "refuted", "blocked"))
    assert "verified_rate" in agg
    assert 0.0 <= agg["verified_rate"] <= 1.0
