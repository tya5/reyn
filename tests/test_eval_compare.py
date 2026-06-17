"""Tier 2: eval compare regression diff (FP-0007 Component C).

Tests for ``reyn.dev.eval.compare.compute_diff`` (pure-function logic) and the
``reyn eval compare`` CLI subcommand.

No mocks (``unittest.mock`` / ``AsyncMock`` / ``patch``). Uses real instances
and real tmpdir result files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_run_result(
    run_id: str,
    skill_version_hash: str | None,
    cases: list[dict],
    timestamp: str = "2026-05-14T21:30:00Z",
) -> dict:
    """Build a synthetic run result dict in the shape produced by result_loader."""
    return {
        "run_id": run_id,
        "skill_version_hash": skill_version_hash or "unknown",
        "timestamp": timestamp,
        "cases": cases,
    }


def _make_case(case_id: str, score: float) -> dict:
    return {"case_id": case_id, "score": score, "pass": score >= 0.8}


def _seed_result_file(
    results_dir: Path,
    skill_name: str,
    stem: str,
    cases: list[dict],
    skill_version_hash: str | None = None,
) -> Path:
    """Write a JSONL result file in the format produced by ``reyn eval run``."""
    skill_dir = results_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / f"{stem}.jsonl"
    records = []
    for case in cases:
        record = {
            "case_id": case["case_id"],
            "input": {},
            "expected": {},
            "actual": {},
            "pass": case.get("pass", case.get("score", 0.0) >= 0.8),
            "score": case.get("score", 1.0),
            "skill_version_hash": skill_version_hash,
            "tags": [],
            "compare_mode": "exact",
        }
        records.append(record)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )
    return path


# ── test 1: basic delta computation ──────────────────────────────────────────


def test_compute_diff_basic() -> None:
    """Tier 2: 3-case results → per-case deltas computed correctly."""
    from reyn.dev.eval.compare import compute_diff

    baseline = _make_run_result(
        "run_baseline", "hash_a",
        [_make_case("c1", 0.9), _make_case("c2", 0.7), _make_case("c3", 1.0)],
    )
    candidate = _make_run_result(
        "run_candidate", "hash_b",
        [_make_case("c1", 0.7), _make_case("c2", 0.8), _make_case("c3", 0.9)],
    )

    diff = compute_diff(baseline, candidate, threshold=0.05)

    # Validate regressing_cases contains only c1 (delta = -0.2, above threshold)
    regressing_ids = {rc["case_id"] for rc in diff["regressing_cases"]}
    assert "c1" in regressing_ids
    # c3 delta = -0.1 also > threshold=0.05 → should also be in regressing
    assert "c3" in regressing_ids
    # c2 went UP → not regressing
    assert "c2" not in regressing_ids

    # Verify individual deltas
    case_map = {rc["case_id"]: rc for rc in diff["regressing_cases"]}
    assert case_map["c1"]["baseline_score"] == pytest.approx(0.9)
    assert case_map["c1"]["candidate_score"] == pytest.approx(0.7)
    assert case_map["c1"]["delta"] == pytest.approx(-0.2)

    assert diff["summary"]["cases_compared"] == 3


# ── test 2: regression detection straddling threshold ────────────────────────


def test_compute_diff_regression_detection() -> None:
    """Tier 2: cases straddling the threshold → alert set correctly."""
    from reyn.dev.eval.compare import compute_diff

    # c1: delta = -0.06 (BELOW threshold -0.05 → regressing)
    # c2: delta = -0.04 (ABOVE threshold -0.05 → not regressing)
    # c3: delta = +0.10 (improvement)
    baseline = _make_run_result(
        "b", "h1",
        [_make_case("c1", 0.90), _make_case("c2", 0.90), _make_case("c3", 0.50)],
    )
    candidate = _make_run_result(
        "c", "h2",
        [_make_case("c1", 0.84), _make_case("c2", 0.86), _make_case("c3", 0.60)],
    )

    diff = compute_diff(baseline, candidate, threshold=0.05)

    assert diff["alert"] is True
    assert diff["summary"]["regressing_count"] == 1
    regressing_ids = {rc["case_id"] for rc in diff["regressing_cases"]}
    assert regressing_ids == {"c1"}


# ── test 3: no regressions → alert false ─────────────────────────────────────


def test_compute_diff_no_regressions() -> None:
    """Tier 2: all candidate scores >= baseline → alert=false."""
    from reyn.dev.eval.compare import compute_diff

    baseline = _make_run_result(
        "b", "h1",
        [_make_case("c1", 0.5), _make_case("c2", 0.7)],
    )
    candidate = _make_run_result(
        "c", "h2",
        [_make_case("c1", 0.9), _make_case("c2", 0.8)],
    )

    diff = compute_diff(baseline, candidate, threshold=0.05)

    assert diff["alert"] is False
    assert diff["summary"]["regressing_count"] == 0
    assert diff["regressing_cases"] == []


# ── test 4: cases missing in candidate ───────────────────────────────────────


def test_compute_diff_missing_cases_in_candidate() -> None:
    """Tier 2: baseline has case 'x' missing from candidate → reported."""
    from reyn.dev.eval.compare import compute_diff

    baseline = _make_run_result(
        "b", "h1",
        [_make_case("common", 0.8), _make_case("x", 0.9)],
    )
    candidate = _make_run_result(
        "c", "h2",
        [_make_case("common", 0.7)],
    )

    diff = compute_diff(baseline, candidate, threshold=0.05)

    assert "x" in diff["missing_in_candidate"]
    assert diff["summary"]["cases_compared"] == 1  # only "common" is shared


# ── test 5: cases missing in baseline ────────────────────────────────────────


def test_compute_diff_missing_cases_in_baseline() -> None:
    """Tier 2: candidate has case 'y' missing from baseline → reported."""
    from reyn.dev.eval.compare import compute_diff

    baseline = _make_run_result(
        "b", "h1",
        [_make_case("common", 0.8)],
    )
    candidate = _make_run_result(
        "c", "h2",
        [_make_case("common", 0.7), _make_case("y", 0.9)],
    )

    diff = compute_diff(baseline, candidate, threshold=0.05)

    assert "y" in diff["missing_in_baseline"]
    assert diff["summary"]["cases_compared"] == 1


# ── test 6: identical hashes warn ────────────────────────────────────────────


def test_compute_diff_identical_hashes_warns() -> None:
    """Tier 2: same skill_version_hash → warning field set."""
    from reyn.dev.eval.compare import compute_diff

    same_hash = "abc12345"
    baseline = _make_run_result("b", same_hash, [_make_case("c1", 0.8)])
    candidate = _make_run_result("c", same_hash, [_make_case("c1", 0.7)])

    diff = compute_diff(baseline, candidate, threshold=0.05)

    assert diff.get("warning") == "identical skill version"


# ── shared fixture for CLI tests ──────────────────────────────────────────────


@pytest.fixture()
def compare_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a tmpdir with eval-results for 'my_skill' and patch the template."""
    import types

    from reyn.interfaces.cli.commands import eval as _eval_mod

    results_root = tmp_path / ".reyn" / "eval-results"
    template = str(results_root / "{skill}")
    monkeypatch.setattr(_eval_mod, "_RESULTS_DIR_TEMPLATE", template)
    monkeypatch.chdir(tmp_path)

    ns = types.SimpleNamespace()
    ns.results_root = results_root
    ns.template = template
    ns.tmp_path = tmp_path

    def seed(
        skill_name: str,
        stem: str,
        cases: list[dict],
        version_hash: str | None = None,
    ) -> Path:
        return _seed_result_file(results_root, skill_name, stem, cases, version_hash)

    ns.seed = seed
    return ns


def _make_compare_args(
    skill_name: str,
    baseline: str | None = None,
    candidate: str | None = None,
    threshold: float = 0.05,
    output_format: str = "text",
) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    ns.baseline = baseline
    ns.candidate = candidate
    ns.threshold = threshold
    ns.output_format = output_format
    ns.eval_cmd = "compare"
    return ns


# ── test 7: CLI text output ───────────────────────────────────────────────────


def test_cli_compare_command_text_output(
    compare_workspace, capsys
) -> None:
    """Tier 2: CLI text output contains expected summary and ALERT headers."""
    from reyn.interfaces.cli.commands import eval as _eval_mod

    # Seed two runs: baseline has higher scores → candidate regresses
    compare_workspace.seed(
        "my_skill", "20260513T100000Z",
        [_make_case("login_test", 0.85), _make_case("signup_test", 0.80)],
        version_hash="hashA",
    )
    compare_workspace.seed(
        "my_skill", "20260514T100000Z",
        [_make_case("login_test", 0.53), _make_case("signup_test", 0.61)],
        version_hash="hashB",
    )

    args = _make_compare_args("my_skill", threshold=0.05, output_format="text")

    with pytest.raises(SystemExit) as exc_info:
        _eval_mod._run_compare(args)

    # Regression → should exit 1
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    out = captured.out
    assert "Summary:" in out
    assert "ALERT:" in out
    assert "my_skill" in out


# ── test 8: CLI JSON output ───────────────────────────────────────────────────


def test_cli_compare_command_json_output(
    compare_workspace, capsys
) -> None:
    """Tier 2: --format json produces parseable JSON with required keys."""
    from reyn.interfaces.cli.commands import eval as _eval_mod

    compare_workspace.seed(
        "my_skill", "20260513T100000Z",
        [_make_case("c1", 0.9)],
        version_hash="hashA",
    )
    compare_workspace.seed(
        "my_skill", "20260514T100000Z",
        [_make_case("c1", 0.5)],
        version_hash="hashB",
    )

    args = _make_compare_args("my_skill", threshold=0.05, output_format="json")

    with pytest.raises(SystemExit):
        _eval_mod._run_compare(args)

    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert "skill" in data
    assert "baseline" in data
    assert "candidate" in data
    assert "summary" in data
    assert "regressing_cases" in data
    assert "alert" in data
    assert data["skill"] == "my_skill"


# ── test 9: exit code semantics ───────────────────────────────────────────────


def test_cli_compare_exit_code_alert(
    compare_workspace, capsys
) -> None:
    """Tier 2: exit 1 when regressions present, exit 0 when none."""
    from reyn.interfaces.cli.commands import eval as _eval_mod

    # --- setup: 3 runs; older ones improve, newest regresses ---
    # Run 1 (baseline): c1=0.9
    compare_workspace.seed(
        "my_skill", "20260513T100000Z",
        [_make_case("c1", 0.9)],
        version_hash="hashA",
    )
    # Run 2 (candidate): c1=0.5 → REGRESSION
    compare_workspace.seed(
        "my_skill", "20260514T100000Z",
        [_make_case("c1", 0.5)],
        version_hash="hashB",
    )

    # Test exit 1 (regression)
    args = _make_compare_args("my_skill", threshold=0.05, output_format="text")
    with pytest.raises(SystemExit) as exc_info:
        _eval_mod._run_compare(args)
    assert exc_info.value.code == 1

    capsys.readouterr()  # flush

    # --- setup a new skill with no regression ---
    compare_workspace.seed(
        "good_skill", "20260513T100000Z",
        [_make_case("c1", 0.5)],
        version_hash="hashA",
    )
    compare_workspace.seed(
        "good_skill", "20260514T100000Z",
        [_make_case("c1", 0.9)],
        version_hash="hashB",
    )

    # Test exit 0 (no regression)
    args2 = _make_compare_args("good_skill", threshold=0.05, output_format="text")
    # Should NOT raise SystemExit (or exit 0)
    try:
        _eval_mod._run_compare(args2)
        raised = None
    except SystemExit as e:
        raised = e

    if raised is not None:
        assert raised.code == 0


# ── test 10: auto-baseline picks previous-version run ────────────────────────


def test_cli_compare_auto_baseline_picks_previous_version(
    compare_workspace, capsys
) -> None:
    """Tier 2: 3 runs (hash A, A, B); candidate=most-recent (hash B);
    baseline auto-picks the most recent hash-A run.
    """
    from reyn.dev.eval.result_loader import load_runs_for_skill
    from reyn.interfaces.cli.commands import eval as _eval_mod

    # Run 1 (oldest, hash A): c1=0.9
    compare_workspace.seed(
        "ver_skill", "20260511T100000Z",
        [_make_case("c1", 0.9)],
        version_hash="hashA_old",
    )
    # Run 2 (middle, hash A): c1=0.85
    compare_workspace.seed(
        "ver_skill", "20260512T100000Z",
        [_make_case("c1", 0.85)],
        version_hash="hashA",
    )
    # Run 3 (newest, hash B): c1=0.70 — this is the candidate
    compare_workspace.seed(
        "ver_skill", "20260513T100000Z",
        [_make_case("c1", 0.70)],
        version_hash="hashB",
    )

    all_runs = load_runs_for_skill("ver_skill", compare_workspace.template)
    # newest first → run3, run2, run1
    assert all_runs[0]["run_id"] == "20260513T100000Z"  # candidate
    assert all_runs[1]["run_id"] == "20260512T100000Z"  # most recent hash-A run

    args = _make_compare_args("ver_skill", threshold=0.05, output_format="text")
    with pytest.raises(SystemExit):
        _eval_mod._run_compare(args)

    captured = capsys.readouterr()
    out = captured.out

    # Baseline must be the most recent hash-A run (run2 = 20260512T100000Z)
    assert "20260512T100000Z" in out
    # Candidate must be run3
    assert "20260513T100000Z" in out
