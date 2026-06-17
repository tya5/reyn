"""Tier 2: OS invariant — ``reyn eval run`` + ``reyn eval report`` CLI commands.

Pins the Component-B eval invariants (FP-0007):

1. test_eval_run_executes_each_case       — 3-case dataset → 3 result records written
2. test_eval_run_exact_mode_pass_fail     — exact match → pass, mismatch → fail
3. test_eval_run_threshold_exit_code      — pass rate < threshold → SystemExit(1)
4. test_eval_run_workspace_isolation      — tmp dir used; project .reyn/ not touched
5. test_eval_report_lists_past_results    — result files → descending-order listing

No mocks (`unittest.mock` / `AsyncMock` / `patch`).  Real instances and
direct function-level substitution via pytest ``monkeypatch`` only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_dataset(cases: list[dict]) -> str:
    """Serialise a list of case dicts to JSONL text."""
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in cases)


def _make_run_args(
    skill_name: str,
    dataset_path: str,
    threshold: float = 0.8,
    tags: str | None = None,
) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    ns.dataset = dataset_path
    ns.threshold = threshold
    ns.tags = tags
    ns.model = None
    ns.eval_cmd = "run"
    return ns


def _make_report_args(
    skill_name: str,
    limit: int = 10,
    threshold: float = 0.8,
) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    ns.limit = limit
    ns.threshold = threshold
    ns.eval_cmd = "report"
    return ns


# ── inline deterministic _run_case substitute ────────────────────────────────


def _make_stub_run_case(pass_all: bool):
    """Return a real callable that mimics _run_case with deterministic results.

    ``pass_all=True``  → every case passes, score 1.0
    ``pass_all=False`` → every case fails, score 0.0

    The stub honours the 'expected' / 'input' fields from the case dict so
    the result records match what the real function would return.
    """
    from reyn.interfaces.cli.commands import eval as _eval_mod

    def _stub(case, skill, skill_root, model, session):
        expected = case.get("expected", {})
        actual = expected if pass_all else {"__differs": True}
        passed = pass_all
        score = 1.0 if pass_all else 0.0
        case_id = _eval_mod._make_case_id(case)
        return {
            "case_id": case_id,
            "input": case.get("input", {}),
            "expected": expected,
            "actual": actual,
            "pass": passed,
            "score": score,
            "skill_version_hash": None,
            "tags": case.get("tags") or [],
            "compare_mode": case.get("compare_mode", "exact"),
        }

    return _stub


# ── fixture: eval_workspace ───────────────────────────────────────────────────


@pytest.fixture()
def eval_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a temporary eval workspace.

    Returns a helper namespace with:
      .dataset_path  — write JSONL datasets here
      .results_root  — where eval-results/ lands (.reyn/eval-results/)
      .write_dataset(cases) → Path

    Monkeypatches:
      - CWD → tmp_path   (so .reyn/ resolves under tmp_path)
      - InvocationContext.from_args → minimal stub that avoids reading real reyn.yaml
      - load_dsl_skill    → returns a sentinel object (skill execution is
                            replaced via _run_case monkeypatch in each test)
    """
    import types

    monkeypatch.chdir(tmp_path)

    # Stub InvocationContext so we don't need a live reyn.yaml.
    from reyn.config import ReynConfig, SafetyConfig
    from reyn.interfaces.cli import invocation_context as invocation_mod
    from reyn.llm.model_resolver import ModelResolver

    class _StubSession:
        config = ReynConfig()
        resolver = ModelResolver({})

        @classmethod
        def from_args(cls, _args):
            return cls()

        def model_for(self, args):
            return "standard", "standard"

        def output_language_for(self, args):
            return None

        def safety_for(self, args):
            return SafetyConfig()

        def shell_allowed_for(self, args):
            return False

    monkeypatch.setattr(invocation_mod, "InvocationContext", _StubSession)

    # Stub load_dsl_skill so tests don't need a real skill on disk.
    import reyn.core.compiler as compiler_mod
    sentinel_skill = types.SimpleNamespace(name="test_skill")
    monkeypatch.setattr(compiler_mod, "load_dsl_skill",
                        lambda *a, **kw: sentinel_skill)

    # Stub resolve_skill_path: patch the underlying function that the CLI
    # skill_loader wrapper delegates to (_resolve_skill_path_raw) so that
    # sys.exit(1) is never triggered during tests.
    from reyn.interfaces.cli import skill_loader as sl_mod
    monkeypatch.setattr(
        sl_mod,
        "_resolve_skill_path_raw",
        lambda name: (tmp_path / "skills" / name, tmp_path),
    )

    # Expose helpers via a simple namespace.
    ns = types.SimpleNamespace()
    ns.tmp_path = tmp_path
    ns.results_root = tmp_path / ".reyn" / "eval-results"

    def write_dataset(cases: list[dict]) -> Path:
        p = tmp_path / "golden.jsonl"
        p.write_text(_make_dataset(cases), encoding="utf-8")
        return p

    ns.write_dataset = write_dataset
    return ns


# ── test 1: each case produces a result record ───────────────────────────────


def test_eval_run_executes_each_case(
    eval_workspace, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Tier 2: 3-case dataset → 3 result records written to the results file."""
    from reyn.interfaces.cli.commands import eval as _eval_mod

    cases = [
        {"input": {"q": "a"}, "expected": {"answer": "a"}, "tags": ["smoke"]},
        {"input": {"q": "b"}, "expected": {"answer": "b"}, "tags": ["smoke"]},
        {"input": {"q": "c"}, "expected": {"answer": "c"}, "tags": ["regression"]},
    ]
    dataset_path = eval_workspace.write_dataset(cases)

    # Replace _run_case with a real stub (all pass).
    monkeypatch.setattr(_eval_mod, "_run_case", _make_stub_run_case(pass_all=True))

    args = _make_run_args("test_skill", str(dataset_path), threshold=0.0)

    _eval_mod._run_golden(args)

    # A result file should have been written.
    result_files = list(eval_workspace.results_root.joinpath("test_skill").glob("*.jsonl"))
    assert result_files, f"No result file found; looked in {eval_workspace.results_root}"

    records = [
        json.loads(line)
        for line in result_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Every input case must produce a result record.
    assert records, "No records written"
    case_ids = {r["case_id"] for r in records}
    assert len(case_ids) == len(cases), (
        f"Expected one record per case; got case_ids={case_ids}"
    )


# ── test 2: exact mode pass and fail ─────────────────────────────────────────


def test_eval_run_exact_mode_pass_fail(
    eval_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: exact-mode compare — matching expected → pass, differing → fail."""
    from reyn.interfaces.cli.commands.eval import _compare

    matching = {"summary": "hello"}
    differing = {"summary": "world"}
    expected = {"summary": "hello"}

    passed_match, score_match = _compare(matching, expected, "exact")
    passed_diff, score_diff = _compare(differing, expected, "exact")

    assert passed_match is True
    assert score_match == 1.0
    assert passed_diff is False
    assert score_diff == 0.0


# ── test 3: threshold exit code ───────────────────────────────────────────────


def test_eval_run_threshold_exit_code(
    eval_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: pass rate below threshold → SystemExit(1) (CI gate)."""
    from reyn.interfaces.cli.commands import eval as _eval_mod

    cases = [
        {"input": {"q": "a"}, "expected": {"answer": "a"}, "tags": ["smoke"]},
        {"input": {"q": "b"}, "expected": {"answer": "b"}, "tags": ["smoke"]},
    ]
    dataset_path = eval_workspace.write_dataset(cases)

    # All cases FAIL → pass rate = 0%  <  threshold = 0.8
    monkeypatch.setattr(_eval_mod, "_run_case", _make_stub_run_case(pass_all=False))

    args = _make_run_args("test_skill", str(dataset_path), threshold=0.8)

    with pytest.raises(SystemExit) as exc_info:
        _eval_mod._run_golden(args)

    assert exc_info.value.code == 1


# ── test 4: workspace isolation ───────────────────────────────────────────────


def test_eval_run_workspace_isolation(tmp_path: Path) -> None:
    """Tier 2: _isolated_workspace changes CWD to a temp dir; original CWD restored.

    The .reyn/ that Agent creates during a skill run must land inside the
    temp dir, not in the original project CWD. This is the core isolation
    invariant: eval runs never pollute the project's .reyn/.
    """
    from reyn.interfaces.cli.commands.eval import _isolated_workspace

    # Record initial CWD.
    original = Path.cwd()

    reyn_in_tmp: Path | None = None

    with _isolated_workspace() as tmp_dir:
        inside_cwd = Path.cwd().resolve()
        tmp_dir_resolved = tmp_dir.resolve()
        original_resolved = original.resolve()
        # CWD must have changed to the tmp dir.
        assert inside_cwd == tmp_dir_resolved
        assert inside_cwd != original_resolved

        # Simulate what Agent does: create .reyn/ relative to CWD.
        reyn_dir = Path(".reyn")
        reyn_dir.mkdir(parents=True, exist_ok=True)
        reyn_in_tmp = (tmp_dir_resolved / ".reyn").resolve()

    # After the context manager exits, CWD is restored.
    assert Path.cwd().resolve() == original.resolve()

    # .reyn/ created during the run is inside the tmp dir (now deleted), not in
    # the original CWD.
    project_reyn = (original / ".reyn").resolve()
    assert reyn_in_tmp != project_reyn, (
        ".reyn/ from eval run must not equal the project's .reyn/ directory"
    )


# ── test 5: report lists past results in descending order ────────────────────


def test_eval_report_lists_past_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Tier 2: eval report reads result files and lists them newest-first."""
    from reyn.interfaces.cli.commands import eval as _eval_mod

    # Set up result directory with two timestamped JSONL files.
    results_dir = tmp_path / ".reyn" / "eval-results" / "my_skill"
    results_dir.mkdir(parents=True)

    # Older run: 1/2 passing (50%)
    older_path = results_dir / "20260513T142200Z.jsonl"
    older_path.write_text(
        "\n".join([
            json.dumps({"case_id": "c1", "pass": True}),
            json.dumps({"case_id": "c2", "pass": False}),
        ]),
        encoding="utf-8",
    )

    # Newer run: 2/2 passing (100%)
    newer_path = results_dir / "20260514T213000Z.jsonl"
    newer_path.write_text(
        "\n".join([
            json.dumps({"case_id": "c1", "pass": True}),
            json.dumps({"case_id": "c2", "pass": True}),
        ]),
        encoding="utf-8",
    )

    # Point the results dir template at our tmp path by patching CWD.
    monkeypatch.chdir(tmp_path)

    # Patch the results dir template so it resolves to our temp results dir.
    monkeypatch.setattr(
        _eval_mod,
        "_RESULTS_DIR_TEMPLATE",
        str(tmp_path / ".reyn" / "eval-results" / "{skill}"),
    )

    args = _make_report_args("my_skill", limit=10, threshold=0.8)
    _eval_mod._run_report(args)

    captured = capsys.readouterr()
    out = captured.out

    # Both entries must appear.
    assert "my_skill" in out

    # Newer run (100%) must appear before older run (50%) in output.
    pos_newer = out.find("2026-05-14")
    pos_older = out.find("2026-05-13")
    assert pos_newer != -1, "Newer result (2026-05-14) not found in output"
    assert pos_older != -1, "Older result (2026-05-13) not found in output"
    assert pos_newer < pos_older, (
        "Newer result must appear before older result (descending order)"
    )


# ── argparse integration ──────────────────────────────────────────────────────


def test_eval_run_parses() -> None:
    """Tier 2: 'eval run SKILL --dataset FILE' is a valid CLI invocation."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "run", "my_skill", "--dataset", "golden.jsonl"])
    assert args.command == "eval"
    assert args.eval_cmd == "run"
    assert args.skill_name == "my_skill"
    assert args.dataset == "golden.jsonl"
    assert args.threshold == pytest.approx(0.8)


def test_eval_report_parses() -> None:
    """Tier 2: 'eval report SKILL' is a valid CLI invocation."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "report", "my_skill"])
    assert args.command == "eval"
    assert args.eval_cmd == "report"
    assert args.skill_name == "my_skill"


def test_eval_spec_parses() -> None:
    """Tier 2: 'eval spec FILE' is a valid CLI invocation (legacy path preserved)."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "spec", "reyn/local/my_app/eval.md"])
    assert args.command == "eval"
    assert args.eval_cmd == "spec"
    assert args.spec == "reyn/local/my_app/eval.md"
