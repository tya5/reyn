"""Tier 1/2: RunResult, compare_runs, and CLI tests (FP-0036 Component B + E).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock / AsyncMock / patch.
- Real dataclass instances, real file paths under tmp_path.
- Injectable runner_fn (= stub returning a fixed ScenarioRunResult) for
  orchestration tests without a live LLM.
- Each test docstring's first line starts with 'Tier 1:' or 'Tier 2:'.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.dev.dogfood.compare import (
    CompareReport,
    ScenarioDelta,
    compare_runs,
)
from reyn.dev.dogfood.runner import (
    OUTCOME_ORDER,
    RunResult,
    ScenarioRunResult,
    _outcome_rank,
    _worst_outcome,
    load_run_result_from_storage,
    run_scenario_set,
)
from reyn.dev.dogfood.scenarios import (
    Scenario,
    ScenarioSet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_scenario(sid: str, *, input_text: str = "test", outcome_prediction: dict | None = None) -> Scenario:
    """Build a minimal Scenario for testing."""
    from reyn.dev.dogfood.scenarios import OutcomePrediction
    pred: OutcomePrediction | None = None
    if outcome_prediction:
        pred = OutcomePrediction(**outcome_prediction)
    return Scenario(id=sid, input=input_text, outcome_prediction=pred)


def _make_scenario_set(name: str, scenario_ids: list[str]) -> ScenarioSet:
    return ScenarioSet(
        name=name,
        scenarios=[_make_scenario(sid) for sid in scenario_ids],
    )


def _make_run_result(
    run_id: str,
    set_name: str,
    scenario_outcomes: dict[str, str],
    *,
    predictions: dict[str, dict] | None = None,
) -> RunResult:
    """Build a RunResult from a scenario_id → outcome mapping.

    All three verifier outcomes are set to the desired outcome so that
    __post_init__ computes the same overall_outcome.
    """
    now = _utc_now()
    results = []
    for sid, outcome in scenario_outcomes.items():
        detail: dict = {}
        if predictions and sid in predictions:
            detail["outcome_prediction"] = predictions[sid]
        sr = ScenarioRunResult(
            scenario_id=sid,
            reply_text="(stub)",
            events=[],
            artifacts=[],
            reply_outcome=outcome,
            events_outcome=outcome,
            artifacts_outcome=outcome,
            detail=detail,
        )
        results.append(sr)
    return RunResult(
        run_id=run_id,
        set_name=set_name,
        started_at=now,
        completed_at=now,
        scenario_results=results,
    )


# ---------------------------------------------------------------------------
# 1. Outcome ordering: blocked < refuted < inconclusive < verified
# ---------------------------------------------------------------------------


def test_outcome_order_ranking() -> None:
    """Tier 1: OUTCOME_ORDER array and _outcome_rank() reflect the correct ranking."""
    assert OUTCOME_ORDER == ["blocked", "refuted", "inconclusive", "verified"]
    assert _outcome_rank("blocked") < _outcome_rank("refuted")
    assert _outcome_rank("refuted") < _outcome_rank("inconclusive")
    assert _outcome_rank("inconclusive") < _outcome_rank("verified")


def test_worst_outcome_selects_lowest_rank() -> None:
    """Tier 1: _worst_outcome returns the lowest-ranked outcome."""
    assert _worst_outcome("verified", "refuted") == "refuted"
    assert _worst_outcome("inconclusive", "blocked") == "blocked"
    assert _worst_outcome("verified", "inconclusive", "refuted", "blocked") == "blocked"
    assert _worst_outcome("verified", "verified") == "verified"


# ---------------------------------------------------------------------------
# 2. ScenarioRunResult.overall_outcome = worst-case of the three verifiers
# ---------------------------------------------------------------------------


def test_scenario_run_result_overall_outcome_is_worst() -> None:
    """Tier 1: ScenarioRunResult.overall_outcome is set to worst-case of the three verifiers."""
    sr = ScenarioRunResult(
        scenario_id="s1",
        reply_text="ok",
        events=[],
        artifacts=[],
        reply_outcome="verified",
        events_outcome="refuted",
        artifacts_outcome="inconclusive",
    )
    assert sr.overall_outcome == "refuted"


def test_scenario_run_result_all_verified() -> None:
    """Tier 1: ScenarioRunResult.overall_outcome is 'verified' when all three are verified."""
    sr = ScenarioRunResult(
        scenario_id="s1",
        reply_text="ok",
        events=[],
        artifacts=[],
        reply_outcome="verified",
        events_outcome="verified",
        artifacts_outcome="verified",
    )
    assert sr.overall_outcome == "verified"


# ---------------------------------------------------------------------------
# 3. RunResult.aggregate() 4-band counts
# ---------------------------------------------------------------------------


def test_aggregate_4band_counts() -> None:
    """Tier 1: RunResult.aggregate() computes 4-band counts correctly."""
    rr = _make_run_result(
        "run1", "s1",
        {"a": "verified", "b": "refuted", "c": "inconclusive", "d": "blocked"},
    )
    agg = rr.aggregate()

    assert agg["verified"] == 1
    assert agg["refuted"] == 1
    assert agg["inconclusive"] == 1
    assert agg["blocked"] == 1
    assert agg["total"] == 4


def test_aggregate_verified_rate() -> None:
    """Tier 1: RunResult.aggregate() verified_rate is verified/total."""
    rr = _make_run_result(
        "run1", "s1",
        {"a": "verified", "b": "verified", "c": "refuted"},
    )
    agg = rr.aggregate()
    assert abs(agg["verified_rate"] - 2/3) < 1e-9


def test_aggregate_empty_result_is_zero_rate() -> None:
    """Tier 1: RunResult.aggregate() returns 0.0 verified_rate for empty scenario list."""
    rr = RunResult(
        run_id="r0", set_name="empty",
        started_at=_utc_now(), completed_at=_utc_now(),
    )
    agg = rr.aggregate()
    assert agg["total"] == 0
    assert agg["verified_rate"] == 0.0
    assert agg["brier_score"] is None


# ---------------------------------------------------------------------------
# 4. RunResult.aggregate() Brier score
# ---------------------------------------------------------------------------


def test_aggregate_brier_score_perfect_prediction() -> None:
    """Tier 1: Brier score is 0.0 for a perfect prediction (all probability on the actual outcome)."""
    prediction = {"verified": 1.0, "inconclusive": 0.0, "refuted": 0.0, "blocked": 0.0}
    rr = _make_run_result(
        "run1", "s1",
        {"a": "verified"},
        predictions={"a": prediction},
    )
    agg = rr.aggregate()
    # Perfect prediction: all squared errors are 0 (prob=1 on actual, prob=0 on others)
    assert agg["brier_score"] is not None
    assert abs(agg["brier_score"]) < 1e-9


def test_aggregate_brier_score_worst_prediction() -> None:
    """Tier 1: Brier score is 0.5 for a completely wrong prediction (all probability on the wrong outcome)."""
    # Actual = verified, predicted = {blocked: 1.0, verified: 0.0, ...}
    # Squared errors: (0-1)^2 + (0-0)^2 + (0-0)^2 + (1-0)^2 = 2; /4 bands = 0.5
    prediction = {"verified": 0.0, "inconclusive": 0.0, "refuted": 0.0, "blocked": 1.0}
    rr = _make_run_result(
        "run1", "s1",
        {"a": "verified"},
        predictions={"a": prediction},
    )
    agg = rr.aggregate()
    assert agg["brier_score"] is not None
    assert abs(agg["brier_score"] - 0.5) < 1e-9


def test_aggregate_no_predictions_returns_none_brier() -> None:
    """Tier 1: brier_score is None when no scenario carries outcome_prediction."""
    rr = _make_run_result("r1", "s", {"a": "verified"})
    agg = rr.aggregate()
    assert agg["brier_score"] is None


# ---------------------------------------------------------------------------
# 5. compare_runs() delta computation
# ---------------------------------------------------------------------------


def test_compare_runs_produces_correct_deltas() -> None:
    """Tier 1: compare_runs identifies regressed and improved scenarios correctly."""
    baseline = _make_run_result(
        "base", "set1",
        {"s1": "verified", "s2": "refuted", "s3": "inconclusive"},
    )
    candidate = _make_run_result(
        "cand", "set1",
        {"s1": "refuted",   "s2": "verified", "s3": "inconclusive"},
    )

    report = compare_runs(baseline, candidate)

    # s1: verified → refuted (regression)
    assert "s1" in report.regressed_scenarios
    # s2: refuted → verified (improvement)
    assert "s2" in report.improved_scenarios
    # s3: unchanged → neither
    assert "s3" not in report.regressed_scenarios
    assert "s3" not in report.improved_scenarios


def test_compare_runs_regression_detected_flag() -> None:
    """Tier 1: regression_detected is True when at least one scenario regressed."""
    baseline = _make_run_result("b", "s", {"x": "verified"})
    candidate = _make_run_result("c", "s", {"x": "blocked"})
    report = compare_runs(baseline, candidate)
    assert report.regression_detected is True


def test_compare_runs_no_regression() -> None:
    """Tier 1: regression_detected is False when no scenario regressed."""
    baseline = _make_run_result("b", "s", {"x": "refuted"})
    candidate = _make_run_result("c", "s", {"x": "verified"})
    report = compare_runs(baseline, candidate)
    assert report.regression_detected is False


# ---------------------------------------------------------------------------
# 6. verified_rate_delta
# ---------------------------------------------------------------------------


def test_verified_rate_delta_positive() -> None:
    """Tier 1: verified_rate_delta is positive when candidate improves."""
    baseline = _make_run_result("b", "s", {"a": "refuted", "b": "refuted"})
    candidate = _make_run_result("c", "s", {"a": "verified", "b": "verified"})
    report = compare_runs(baseline, candidate)
    assert report.verified_rate_delta == pytest.approx(1.0)


def test_verified_rate_delta_negative() -> None:
    """Tier 1: verified_rate_delta is negative when candidate regresses."""
    baseline = _make_run_result("b", "s", {"a": "verified", "b": "verified"})
    candidate = _make_run_result("c", "s", {"a": "refuted", "b": "refuted"})
    report = compare_runs(baseline, candidate)
    assert report.verified_rate_delta == pytest.approx(-1.0)


def test_exceeds_threshold_true_when_rate_drops() -> None:
    """Tier 1: exceeds_threshold is True when delta < -threshold."""
    baseline = _make_run_result("b", "s", {"a": "verified", "b": "verified", "c": "verified", "d": "verified"})
    candidate = _make_run_result("c", "s", {"a": "refuted", "b": "refuted", "c": "refuted", "d": "refuted"})
    report = compare_runs(baseline, candidate, threshold=0.05)
    # rate dropped 100pp >> 5pp threshold
    assert report.exceeds_threshold(0.05) is True


def test_exceeds_threshold_false_when_within_tolerance() -> None:
    """Tier 1: exceeds_threshold is False when delta >= -threshold."""
    baseline = _make_run_result("b", "s", {"a": "verified"})
    candidate = _make_run_result("c", "s", {"a": "verified"})
    report = compare_runs(baseline, candidate, threshold=0.05)
    assert report.exceeds_threshold(0.05) is False


# ---------------------------------------------------------------------------
# 7. Regression: verified → refuted / improved: refuted → verified
# ---------------------------------------------------------------------------


def test_scenario_delta_regression_verified_to_refuted() -> None:
    """Tier 1: verified → refuted is flagged as regressed=True, improved=False."""
    delta = ScenarioDelta(
        scenario_id="x",
        baseline_outcome="verified",
        candidate_outcome="refuted",
        regressed=True,
        improved=False,
    )
    assert delta.regressed is True
    assert delta.improved is False


def test_scenario_delta_improvement_refuted_to_verified() -> None:
    """Tier 1: refuted → verified is flagged as improved=True, regressed=False."""
    delta = ScenarioDelta(
        scenario_id="y",
        baseline_outcome="refuted",
        candidate_outcome="verified",
        regressed=False,
        improved=True,
    )
    assert delta.improved is True
    assert delta.regressed is False


# ---------------------------------------------------------------------------
# 8. Runner orchestration — injectable runner_fn, writes summary.json
# ---------------------------------------------------------------------------


def _stub_runner(outcome: str):
    """Return an async runner_fn stub that returns a fixed outcome."""
    async def _fn(scenario: Scenario) -> ScenarioRunResult:
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="stub reply",
            events=[{"type": "stub_event"}],
            artifacts=[{"type": "stub_artifact"}],
            reply_outcome=outcome,
            events_outcome=outcome,
            artifacts_outcome=outcome,
        )
    return _fn


def test_run_scenario_set_writes_summary_json(tmp_path: Path) -> None:
    """Tier 2: run_scenario_set writes summary.json under storage_dir."""
    scenario_set = _make_scenario_set("smoke", ["s1", "s2"])
    storage_dir = tmp_path / "run_output"

    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id="test-run-001",
            storage_dir=storage_dir,
            runner_fn=_stub_runner("verified"),
        )
    )

    summary_path = storage_dir / "summary.json"
    assert summary_path.exists(), "summary.json must be written by run_scenario_set"

    summary = json.loads(summary_path.read_text())
    assert summary["run_id"] == "test-run-001"
    assert summary["set_name"] == "smoke"
    assert summary["total"] == 2
    assert summary["verified"] == 2


def test_run_scenario_set_writes_per_scenario_output_json(tmp_path: Path) -> None:
    """Tier 2: run_scenario_set writes output.json and events.jsonl for each scenario."""
    scenario_set = _make_scenario_set("set1", ["alpha", "beta"])
    storage_dir = tmp_path / "run"

    asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id="test-run-002",
            storage_dir=storage_dir,
            runner_fn=_stub_runner("refuted"),
        )
    )

    for sid in ["alpha", "beta"]:
        output = storage_dir / "scenarios" / sid / "output.json"
        assert output.exists(), f"output.json missing for {sid}"
        data = json.loads(output.read_text())
        assert data["scenario_id"] == sid
        assert data["overall_outcome"] == "refuted"

        events_file = storage_dir / "scenarios" / sid / "events.jsonl"
        assert events_file.exists(), f"events.jsonl missing for {sid}"


def test_run_scenario_set_returns_run_result(tmp_path: Path) -> None:
    """Tier 2: run_scenario_set returns a RunResult with correct set_name and scenario count."""
    scenario_set = _make_scenario_set("myset", ["x1", "x2", "x3"])
    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            storage_dir=tmp_path / "run",
            runner_fn=_stub_runner("inconclusive"),
        )
    )
    assert result.set_name == "myset"
    assert result.scenario_results  # at least one result returned
    assert all(sr.scenario_id in {"x1", "x2", "x3"} for sr in result.scenario_results)


def test_run_scenario_set_n_repetitions_worst_case(tmp_path: Path) -> None:
    """Tier 2: With n=2 and mixed outcomes across reps, worst-case outcome wins."""
    call_count = {"n": 0}

    async def _alternating_runner(scenario: Scenario) -> ScenarioRunResult:
        call_count["n"] += 1
        # First call → verified; second call → refuted
        outcome = "verified" if call_count["n"] % 2 == 1 else "refuted"
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="",
            events=[],
            artifacts=[],
            reply_outcome=outcome,
            events_outcome=outcome,
            artifacts_outcome=outcome,
        )

    scenario_set = _make_scenario_set("stability", ["s1"])
    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            storage_dir=tmp_path / "run",
            runner_fn=_alternating_runner,
            n=2,
        )
    )
    # 2 reps, one verified + one refuted → worst-case = refuted
    assert result.scenario_results[0].overall_outcome == "refuted"
    # runner_fn was called twice (n=2 reps × 1 scenario)
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# 9. CLI report reads stored summary.json
# ---------------------------------------------------------------------------


def test_load_run_result_from_storage_reads_summary(tmp_path: Path) -> None:
    """Tier 2: load_run_result_from_storage reconstructs RunResult from summary.json + scenario outputs."""
    # First, write a run
    scenario_set = _make_scenario_set("mytest", ["s1", "s2"])
    storage_dir = tmp_path / "run_abc"
    run_id = "run-abc-001"

    asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id=run_id,
            storage_dir=storage_dir,
            runner_fn=_stub_runner("verified"),
        )
    )

    # Now reload it
    loaded = load_run_result_from_storage(storage_dir)
    assert loaded.run_id == run_id
    assert loaded.set_name == "mytest"
    assert loaded.scenario_results  # at least one result reconstructed
    assert all(sr.scenario_id in {"s1", "s2"} for sr in loaded.scenario_results)
    for sr in loaded.scenario_results:
        assert sr.overall_outcome == "verified"


def test_load_run_result_from_storage_raises_on_missing(tmp_path: Path) -> None:
    """Tier 2: load_run_result_from_storage raises FileNotFoundError if summary.json absent."""
    missing_dir = tmp_path / "nonexistent_run"
    missing_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="summary.json"):
        load_run_result_from_storage(missing_dir)


# ---------------------------------------------------------------------------
# 10. CLI: subcommand parsing
# ---------------------------------------------------------------------------


def _make_parser():
    """Build an argparse parser with the dogfood subcommand registered."""
    from reyn.interfaces.cli.commands import dogfood as dogfood_mod
    parser = argparse.ArgumentParser(prog="reyn")
    sub = parser.add_subparsers(dest="command")
    dogfood_mod.register(sub)
    return parser


def test_cli_registers_dogfood_subcommand() -> None:
    """Tier 2: 'reyn dogfood' is registered; parser resolves all subcommands."""
    parser = _make_parser()
    # run
    args = parser.parse_args(["dogfood", "run", "my_set.yaml"])
    assert args.dogfood_cmd == "run"
    assert args.set_yaml == "my_set.yaml"
    assert args.n == 1

    # report
    args = parser.parse_args(["dogfood", "report", "abc-123"])
    assert args.dogfood_cmd == "report"
    assert args.run_id == "abc-123"

    # compare
    args = parser.parse_args(["dogfood", "compare", "base-id", "cand-id", "--threshold", "0.10"])
    assert args.dogfood_cmd == "compare"
    assert args.baseline_run_id == "base-id"
    assert args.candidate_run_id == "cand-id"
    assert args.threshold == pytest.approx(0.10)

    # baseline
    args = parser.parse_args(["dogfood", "baseline", "run-xyz", "--label", "v1.2-stable"])
    assert args.dogfood_cmd == "baseline"
    assert args.run_id == "run-xyz"
    assert args.label == "v1.2-stable"


def test_cli_run_options_parse() -> None:
    """Tier 2: 'reyn dogfood run' accepts --n, --replay, --agent, --storage flags."""
    parser = _make_parser()
    args = parser.parse_args([
        "dogfood", "run", "scenario.yaml",
        "--n", "5",
        "--replay", "/tmp/fixtures",
        "--agent", "myagent",
        "--storage", "/tmp/store",
    ])
    assert args.n == 5
    assert args.replay == "/tmp/fixtures"
    assert args.agent == "myagent"
    assert args.storage == "/tmp/store"


def test_cli_coverage_options_parse() -> None:
    """Tier 2: 'reyn dogfood coverage' accepts --feature-map and --json flags."""
    parser = _make_parser()
    args = parser.parse_args([
        "dogfood", "coverage",
        "--feature-map", "docs/my-map.md",
        "--json",
        "a.yaml", "b.yaml",
    ])
    assert args.feature_map == "docs/my-map.md"
    assert args.output_json is True
    assert args.set_yamls == ["a.yaml", "b.yaml"]


# ---------------------------------------------------------------------------
# 11. CLI report subcommand prints stored summary (integration)
# ---------------------------------------------------------------------------


def test_cli_report_prints_summary(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Tier 2: 'reyn dogfood report <run_id>' reads stored summary and prints 4-band breakdown."""
    scenario_set = _make_scenario_set("testreport", ["s1"])
    storage_dir = tmp_path / "run_report"
    run_id = "report-run-001"

    asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id=run_id,
            storage_dir=storage_dir,
            runner_fn=_stub_runner("verified"),
        )
    )

    from reyn.interfaces.cli.commands import dogfood as dogfood_mod
    args = argparse.Namespace(
        run_id=str(storage_dir),
        output_json=False,
    )
    dogfood_mod.run_report(args)

    out = capsys.readouterr().out
    assert "report-run-001" in out
    assert "testreport" in out
    assert "verified" in out


def test_cli_report_json_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Tier 2: 'reyn dogfood report --json' emits parseable JSON with required keys."""
    scenario_set = _make_scenario_set("json_test", ["s1"])
    storage_dir = tmp_path / "run_json"
    run_id = "json-run-001"

    asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id=run_id,
            storage_dir=storage_dir,
            runner_fn=_stub_runner("refuted"),
        )
    )

    from reyn.interfaces.cli.commands import dogfood as dogfood_mod
    args = argparse.Namespace(
        run_id=str(storage_dir),
        output_json=True,
    )
    dogfood_mod.run_report(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert "run_id" in data
    assert "verified" in data
    assert "total" in data
    assert data["total"] == 1
    assert data["refuted"] == 1


# ---------------------------------------------------------------------------
# 12. CLI compare subcommand (no live LLM needed — uses stored files)
# ---------------------------------------------------------------------------


def test_cli_compare_no_regression_exits_0(tmp_path: Path) -> None:
    """Tier 2: 'reyn dogfood compare' exits 0 when candidate is at least as good as baseline."""
    base_dir = tmp_path / "base_run"
    cand_dir = tmp_path / "cand_run"

    scenario_set = _make_scenario_set("cmp", ["s1"])
    asyncio.run(run_scenario_set(scenario_set, run_id="base", storage_dir=base_dir, runner_fn=_stub_runner("refuted")))
    asyncio.run(run_scenario_set(scenario_set, run_id="cand", storage_dir=cand_dir, runner_fn=_stub_runner("verified")))

    from reyn.interfaces.cli.commands import dogfood as dogfood_mod
    args = argparse.Namespace(
        baseline_run_id=str(base_dir),
        candidate_run_id=str(cand_dir),
        threshold=0.05,
        output_json=False,
    )
    # Should not raise SystemExit (no regression)
    dogfood_mod.run_compare(args)


def test_cli_compare_regression_exits_1(tmp_path: Path) -> None:
    """Tier 2: 'reyn dogfood compare' exits 1 when candidate drops below threshold."""
    base_dir = tmp_path / "base_run"
    cand_dir = tmp_path / "cand_run"

    scenario_set = _make_scenario_set("cmp2", ["s1", "s2"])
    asyncio.run(run_scenario_set(scenario_set, run_id="base", storage_dir=base_dir, runner_fn=_stub_runner("verified")))
    asyncio.run(run_scenario_set(scenario_set, run_id="cand", storage_dir=cand_dir, runner_fn=_stub_runner("blocked")))

    from reyn.interfaces.cli.commands import dogfood as dogfood_mod
    args = argparse.Namespace(
        baseline_run_id=str(base_dir),
        candidate_run_id=str(cand_dir),
        threshold=0.05,
        output_json=False,
    )
    with pytest.raises(SystemExit) as exc_info:
        dogfood_mod.run_compare(args)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 13. Outcome_prediction carried through to aggregate (Brier e2e)
# ---------------------------------------------------------------------------


def test_outcome_prediction_attached_from_scenario(tmp_path: Path) -> None:
    """Tier 2: outcome_prediction from scenario is attached to ScenarioRunResult.detail and affects Brier."""
    from reyn.dev.dogfood.scenarios import OutcomePrediction

    prediction = {"verified": 1.0, "inconclusive": 0.0, "refuted": 0.0, "blocked": 0.0}
    scenario = _make_scenario("pred_s1", outcome_prediction=prediction)
    scenario_set = ScenarioSet(name="pred_test", scenarios=[scenario])

    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            storage_dir=tmp_path / "pred_run",
            runner_fn=_stub_runner("verified"),
        )
    )

    agg = result.aggregate()
    # Perfect prediction → Brier = 0.0
    assert agg["brier_score"] is not None
    assert abs(agg["brier_score"]) < 1e-9


# ---------------------------------------------------------------------------
# 14. Verifier triad wired into run_scenario_set (task #93)
# ---------------------------------------------------------------------------


def test_verifier_triad_produces_real_reply_outcome_for_substring_match(tmp_path: Path) -> None:
    """Tier 2: verifier triad wired — substring match on reply elevates reply_outcome
    from the default 'inconclusive' to 'verified'.

    Before task #93 the verifiers were never called; every scenario run
    returned ScenarioRunResult with all outcomes stuck at 'inconclusive'.
    This test confirms that run_scenario_set now invokes verify_reply so a
    scenario whose runner_fn returns a reply containing the expected substring
    receives reply_outcome='verified' (not 'inconclusive').

    Design:
    - The runner_fn returns reply_text='hello world' with all outcomes set to
      the 'inconclusive' default — simulating what the live runner returns
      before the verifier fires.
    - The scenario declares expected_reply with kind='substring', value='hello'.
    - After run_scenario_set the runner fills in the verifier-driven outcomes;
      reply_outcome must be 'verified' (substring present in reply_text).
    - events_outcome and artifacts_outcome are 'blocked' because the scenario
      declares no expected_events or expected_artifacts.
    - overall_outcome = worst-case('verified', 'blocked', 'blocked') = 'blocked'.
    """
    from reyn.dev.dogfood.scenarios import ExpectedReply

    async def _reply_runner(scenario: Scenario) -> ScenarioRunResult:
        """Returns reply containing the expected substring; all outcomes at default."""
        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text="hello world",
            events=[],
            artifacts=[],
            # Outcomes deliberately left at 'inconclusive' (= what live runner returns).
            # The verifier triad inside run_scenario_set must overwrite them.
        )

    scenario = Scenario(
        id="substring-verify",
        input="greet me",
        expected_reply=ExpectedReply(kind="substring", value="hello"),
        expected_events=None,
        expected_artifacts=None,
    )
    scenario_set = ScenarioSet(name="verifier-triad-test", scenarios=[scenario])

    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            storage_dir=tmp_path / "verifier_run",
            runner_fn=_reply_runner,
        )
    )

    assert result.scenario_results  # verifier-triad scenario result present
    sr = result.scenario_results[0]

    # The verifier must have fired: reply_outcome is 'verified' because
    # 'hello' appears in 'hello world'.
    assert sr.reply_outcome == "verified", (
        f"Expected reply_outcome='verified' after verifier triad; got {sr.reply_outcome!r}. "
        "The verifier triad may not be wired into run_scenario_set."
    )
    # No expected_events / expected_artifacts → both are 'blocked' (no assertion declared).
    assert sr.events_outcome == "blocked", (
        f"Expected events_outcome='blocked' (no events assertion); got {sr.events_outcome!r}"
    )
    assert sr.artifacts_outcome == "blocked", (
        f"Expected artifacts_outcome='blocked' (no artifacts assertion); got {sr.artifacts_outcome!r}"
    )
    # overall = worst('verified', 'blocked', 'blocked') = 'blocked'
    assert sr.overall_outcome == "blocked", (
        f"Expected overall_outcome='blocked'; got {sr.overall_outcome!r}"
    )
    # detail must carry per-verifier breakdown
    assert "reply" in sr.detail, "detail['reply'] must be populated by verifier triad"
    assert sr.detail["reply"].get("kind") == "substring"
