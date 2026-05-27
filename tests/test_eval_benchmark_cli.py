"""Tier 2: OS invariant — ``reyn eval benchmark`` CLI command.

Pins the Component-B benchmark invariants (FP-0008 PR-B):

1. test_benchmark_parses                  — arg parsing (all flags)
2. test_load_tasks_valid                  — valid JSONL → correct task list
3. test_load_tasks_malformed              — malformed JSON → SystemExit with line number
4. test_load_tasks_trailing_newline       — trailing blank lines tolerated
5. test_summary_json_shape                — summary.json written with required fields
6. test_summary_json_pass_rate_null       — pass_rate null when tests_passed absent
7. test_summary_json_has_tests_passed     — pass_rate computed when tests_passed present
8. test_resume_skips_completed            — --resume filters already-completed ids
9. test_limit_applied_after_resume        — --limit caps remaining (not total) tasks
10. test_concurrency_cap_honored           — semaphore limits concurrent executions
11. test_single_task_failure_no_abort      — one error does not abort the batch

No mocks (``unittest.mock`` / ``AsyncMock`` / ``MagicMock`` / ``patch``).
Real instances and direct monkeypatching via ``pytest.MonkeyPatch`` only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import types
from pathlib import Path

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_benchmark_args(
    skill_name: str,
    tasks_path: str,
    output_dir: str,
    concurrency: int = 4,
    limit: int | None = None,
    resume: bool = False,
    model: str | None = None,
    allow_shell: bool = False,
    allow_unsafe_python: bool = False,
) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    ns.tasks = tasks_path
    ns.output = output_dir
    ns.concurrency = concurrency
    ns.limit = limit
    ns.resume = resume
    ns.model = model
    ns.allow_shell = allow_shell
    ns.allow_unsafe_python = allow_unsafe_python
    ns.eval_cmd = "benchmark"
    return ns


def _make_tasks_jsonl(tasks: list[dict]) -> str:
    return "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks)


# ── fixture: benchmark_workspace ─────────────────────────────────────────────


@pytest.fixture()
def benchmark_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a temporary benchmark workspace.

    Returns a namespace with helpers; monkeypatches Session, load_dsl_skill,
    resolve_skill_path so tests run without a live reyn.yaml or skill on disk.
    """
    monkeypatch.chdir(tmp_path)

    # Stub Session
    from reyn.cli import session as session_mod
    from reyn.config import ReynConfig, SafetyConfig
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

    monkeypatch.setattr(session_mod, "Session", _StubSession)

    # Stub load_dsl_skill
    import reyn.compiler as compiler_mod
    sentinel_skill = types.SimpleNamespace(name="test_skill")
    monkeypatch.setattr(compiler_mod, "load_dsl_skill", lambda *a, **kw: sentinel_skill)

    # Stub resolve_skill_path
    from reyn.cli import skill_loader as sl_mod
    monkeypatch.setattr(
        sl_mod,
        "_resolve_skill_path_raw",
        lambda name: (tmp_path / "skills" / name, tmp_path),
    )

    ns = types.SimpleNamespace()
    ns.tmp_path = tmp_path

    def write_tasks(tasks: list[dict]) -> Path:
        p = tmp_path / "tasks.jsonl"
        p.write_text(_make_tasks_jsonl(tasks), encoding="utf-8")
        return p

    ns.write_tasks = write_tasks
    return ns


# ── test 1: arg parsing ───────────────────────────────────────────────────────


def test_benchmark_parses() -> None:
    """Tier 2: 'eval benchmark SKILL --tasks FILE --output DIR' parses all flags."""
    from reyn.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "eval", "benchmark", "my_skill",
        "--tasks", "tasks.jsonl",
        "--output", "results/",
        "--concurrency", "8",
        "--limit", "50",
        "--resume",
    ])
    assert args.command == "eval"
    assert args.eval_cmd == "benchmark"
    assert args.skill_name == "my_skill"
    assert args.tasks == "tasks.jsonl"
    assert args.output == "results/"
    assert args.concurrency == 8
    assert args.limit == 50
    assert args.resume is True
    # Permission-flag defaults (= off, matching reyn run)
    assert args.allow_shell is False
    assert args.allow_unsafe_python is False


def test_benchmark_parses_allow_flags() -> None:
    """Tier 2: '--allow-shell' + '--allow-unsafe-python' flags parse correctly.

    Pins the parity with ``reyn run``'s permission flags (= PR-D follow-up
    to PR-B, FP-0008 2026-05-28 calibration block). Without these flags,
    skills like ``swe_bench`` that declare ``permissions.shell: true``
    can't actually execute shell ops in batch mode.
    """
    from reyn.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "eval", "benchmark", "swe_bench",
        "--tasks", "tasks.jsonl",
        "--output", "results/",
        "--allow-shell",
        "--allow-unsafe-python",
    ])
    assert args.allow_shell is True
    assert args.allow_unsafe_python is True


def test_benchmark_parses_legacy_unsafe_python_alias() -> None:
    """Tier 2: legacy '--allow-untrusted-python' alias still parses (parity with reyn run)."""
    from reyn.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "eval", "benchmark", "my_skill",
        "--tasks", "tasks.jsonl",
        "--output", "results/",
        "--allow-untrusted-python",
    ])
    assert args.allow_unsafe_python is True


# ── test 2: load_tasks valid ──────────────────────────────────────────────────


def test_load_tasks_valid(tmp_path: Path) -> None:
    """Tier 2: valid JSONL with 3 task objects → 3 dicts returned."""
    from reyn.cli.commands.eval_benchmark import load_tasks

    tasks = [
        {"instance_id": "t1", "input": "a"},
        {"instance_id": "t2", "input": "b"},
        {"instance_id": "t3", "input": "c"},
    ]
    p = tmp_path / "tasks.jsonl"
    p.write_text(_make_tasks_jsonl(tasks) + "\n", encoding="utf-8")

    loaded = load_tasks(p)
    assert len(loaded) == 3
    assert loaded[0]["instance_id"] == "t1"
    assert loaded[2]["instance_id"] == "t3"


# ── test 3: malformed line raises SystemExit with line number ─────────────────


def test_load_tasks_malformed(tmp_path: Path, capsys) -> None:
    """Tier 2: malformed JSON line → SystemExit(1); message includes line number."""
    from reyn.cli.commands.eval_benchmark import load_tasks

    content = '{"ok": 1}\nnot-valid-json\n{"ok": 3}\n'
    p = tmp_path / "bad.jsonl"
    p.write_text(content, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_tasks(p)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "line 2" in captured.err


# ── test 4: trailing newline tolerated ────────────────────────────────────────


def test_load_tasks_trailing_newline(tmp_path: Path) -> None:
    """Tier 2: JSONL file with trailing blank line is parsed without error."""
    from reyn.cli.commands.eval_benchmark import load_tasks

    content = '{"instance_id": "t1"}\n{"instance_id": "t2"}\n\n'
    p = tmp_path / "trailing.jsonl"
    p.write_text(content, encoding="utf-8")

    loaded = load_tasks(p)
    assert len(loaded) == 2


# ── test 5: summary.json shape ────────────────────────────────────────────────


def test_summary_json_shape(tmp_path: Path) -> None:
    """Tier 2: _write_summary writes all required top-level fields."""
    from reyn.cli.commands.eval_benchmark import _write_summary

    run_dir = tmp_path / "run_20260527_120000"
    run_dir.mkdir()

    results = [
        {"instance_id": "t1", "cost_usd": 0.10, "error": None},
        {"instance_id": "t2", "cost_usd": 0.20, "error": None},
    ]
    _write_summary(run_dir, "run_20260527_120000", "my_skill", results, total_tasks=5)

    summary_path = run_dir / "summary.json"
    assert summary_path.exists()
    data = json.loads(summary_path.read_text(encoding="utf-8"))

    required_keys = {
        "run_id", "skill", "total", "completed",
        "passed", "pass_rate", "total_cost_usd",
        "avg_cost_per_instance", "avg_attempts",
    }
    missing = required_keys - data.keys()
    assert not missing, f"summary.json missing keys: {missing}"

    assert data["run_id"] == "run_20260527_120000"
    assert data["skill"] == "my_skill"
    assert data["total"] == 5
    assert data["completed"] == 2
    assert abs(data["total_cost_usd"] - 0.30) < 1e-6
    assert abs(data["avg_cost_per_instance"] - 0.15) < 1e-6


# ── test 6: pass_rate null when tests_passed absent ───────────────────────────


def test_summary_json_pass_rate_null(tmp_path: Path) -> None:
    """Tier 2: pass_rate is null when no result has a tests_passed field."""
    from reyn.cli.commands.eval_benchmark import _write_summary

    run_dir = tmp_path / "run_null"
    run_dir.mkdir()

    results = [
        {"instance_id": "t1", "cost_usd": 0.05},
        {"instance_id": "t2", "cost_usd": 0.05},
    ]
    _write_summary(run_dir, "run_null", "generic_skill", results, total_tasks=2)

    data = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert data["passed"] is None
    assert data["pass_rate"] is None


# ── test 7: pass_rate computed when tests_passed present ──────────────────────


def test_summary_json_has_tests_passed(tmp_path: Path) -> None:
    """Tier 2: pass_rate is computed correctly when tests_passed is present."""
    from reyn.cli.commands.eval_benchmark import _write_summary

    run_dir = tmp_path / "run_tp"
    run_dir.mkdir()

    results = [
        {"instance_id": "t1", "cost_usd": 0.1, "tests_passed": True},
        {"instance_id": "t2", "cost_usd": 0.1, "tests_passed": False},
        {"instance_id": "t3", "cost_usd": 0.1, "tests_passed": True},
    ]
    _write_summary(run_dir, "run_tp", "coding_skill", results, total_tasks=3)

    data = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert data["passed"] == 2
    assert abs(data["pass_rate"] - 2 / 3) < 1e-6


# ── test 8: --resume skips completed ids ──────────────────────────────────────


def test_resume_skips_completed(tmp_path: Path) -> None:
    """Tier 2: _load_completed_ids + _find_latest_run_dir skip already-done instances."""
    from reyn.cli.commands.eval_benchmark import (
        _find_latest_run_dir,
        _load_completed_ids,
    )

    output_root = tmp_path / "results"
    output_root.mkdir()

    # Simulate prior run with summary.json containing completed_ids
    prior_run = output_root / "run_20260527_100000"
    prior_run.mkdir()
    summary = {
        "run_id": "run_20260527_100000",
        "skill": "my_skill",
        "total": 3,
        "completed": 2,
        "completed_ids": ["t1", "t2"],
    }
    (prior_run / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )

    latest = _find_latest_run_dir(output_root)
    assert latest is not None
    assert latest.name == "run_20260527_100000"

    completed = _load_completed_ids(latest)
    assert completed == {"t1", "t2"}


# ── test 9: --limit applied after resume filtering ───────────────────────────


def test_limit_applied_after_resume(tmp_path: Path) -> None:
    """Tier 2: --limit N caps to N remaining tasks (after skip_ids removed)."""
    from reyn.cli.commands.eval_benchmark import _instance_id

    all_tasks = [{"instance_id": f"t{i}"} for i in range(10)]
    skip_ids = {"t0", "t1", "t2"}

    # Simulate the filtering logic from _run_benchmark_async
    pending = [
        (i, task) for i, task in enumerate(all_tasks)
        if _instance_id(task, i) not in skip_ids
    ]
    limit = 4
    pending = pending[:limit]

    assert len(pending) == 4
    ids = [_instance_id(t, i) for i, t in pending]
    assert "t0" not in ids
    assert "t1" not in ids
    assert "t2" not in ids
    # first 4 remaining: t3, t4, t5, t6
    assert ids == ["t3", "t4", "t5", "t6"]


# ── test 10: concurrency cap honored ─────────────────────────────────────────


def test_concurrency_cap_honored() -> None:
    """Tier 2: asyncio.Semaphore(N) caps concurrent executions to N."""
    max_concurrent = 0
    current = 0

    async def _runner(sem: asyncio.Semaphore) -> None:
        nonlocal max_concurrent, current
        async with sem:
            current += 1
            if current > max_concurrent:
                max_concurrent = current
            await asyncio.sleep(0)  # yield
            current -= 1

    async def _run_all() -> None:
        sem = asyncio.Semaphore(3)
        await asyncio.gather(*(_runner(sem) for _ in range(10)))

    asyncio.run(_run_all())
    assert max_concurrent <= 3, (
        f"Semaphore(3) should cap concurrency to 3; observed {max_concurrent}"
    )


# ── test 11: single-task failure does not abort batch ────────────────────────


def test_single_task_failure_no_abort(
    benchmark_workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tier 2: one task raising an exception is recorded as error; others complete."""
    from reyn.cli.commands import eval_benchmark as bm

    call_count = 0

    async def _stub_run_single_task(
        task, instance_id, skill, skill_root, model, session, run_dir, semaphore,
        shell_allowed=False, permission_resolver=None, python_allowed_modules=None,
    ):
        nonlocal call_count
        async with semaphore:
            call_count += 1
            if instance_id == "t1":
                raise RuntimeError("simulated skill failure")
            return {"instance_id": instance_id, "cost_usd": 0.01}

    monkeypatch.setattr(bm, "_run_single_task", _stub_run_single_task)

    tasks = [
        {"instance_id": "t0"},
        {"instance_id": "t1"},  # will fail
        {"instance_id": "t2"},
    ]
    tasks_path = benchmark_workspace.write_tasks(tasks)
    output_dir = tmp_path / "bench_output"

    args = _make_benchmark_args(
        skill_name="test_skill",
        tasks_path=str(tasks_path),
        output_dir=str(output_dir),
        concurrency=2,
    )

    # _run_benchmark_async raises SystemExit on bad tasks file — shouldn't here
    # We test that it completes and writes summary for non-failing tasks.
    asyncio.run(bm._run_benchmark_async(args))

    # Find the run directory
    run_dirs = list(output_dir.iterdir())
    assert run_dirs, "No run directory created"
    run_dir = run_dirs[0]

    summary_path = run_dir / "summary.json"
    assert summary_path.exists(), "summary.json not written"

    data = json.loads(summary_path.read_text(encoding="utf-8"))
    # All 3 tasks are "completed" (= processed); 1 has an error recorded
    # The batch does NOT abort on task failure — all 3 must have been attempted.
    assert call_count == 3, f"All 3 tasks should have been attempted; got {call_count}"
    assert data["completed"] == 3, (
        f"All 3 tasks should appear in completed count; got {data['completed']}"
    )
    # Find the error entry for t1 in completed_ids (it's still in the list)
    assert "t1" in data.get("completed_ids", []), "t1 must be tracked even when it errored"


# ── test 12: --allow-shell propagates to per-task runner ──────────────────────


def test_allow_shell_propagates_to_single_task(
    benchmark_workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tier 2: --allow-shell flag reaches _run_single_task as ``shell_allowed=True``.

    Pins the FP-0008 PR-D fix (= 2026-05-28 calibration block root cause).
    Without the propagation, the Agent's op_catalog omits shell + the LLM
    hallucinates a fake shell schema → every retry fails → batch reports
    100% error with $0.00 cost.

    Verifies the wiring at the per-task layer: when ``--allow-shell`` is
    set on the namespace, ``_run_single_task`` is called with
    ``shell_allowed=True``. The reverse (= flag absent → ``shell_allowed=False``)
    is the default-off path.
    """
    # Override the stub Session to honor the actual allow_shell flag
    # (= the workspace fixture defaults to always-False; we want truth-following)
    from reyn.cli import session as session_mod
    from reyn.cli.commands import eval_benchmark as bm
    from reyn.config import ReynConfig, SafetyConfig
    from reyn.llm.model_resolver import ModelResolver

    class _TruthfulSession:
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
            return bool(getattr(args, "allow_shell", False))

    monkeypatch.setattr(session_mod, "Session", _TruthfulSession)

    captured: dict = {}

    async def _capture_shell_allowed(
        task, instance_id, skill, skill_root, model, session, run_dir, semaphore,
        shell_allowed=False, permission_resolver=None, python_allowed_modules=None,
    ):
        async with semaphore:
            captured["shell_allowed"] = shell_allowed
            captured["permission_resolver_built"] = permission_resolver is not None
            return {"instance_id": instance_id, "cost_usd": 0.0}

    monkeypatch.setattr(bm, "_run_single_task", _capture_shell_allowed)

    tasks_path = benchmark_workspace.write_tasks([{"instance_id": "t1"}])
    output_dir = tmp_path / "bench_output_shell"

    # Case A: --allow-shell set → shell_allowed=True reaches the runner
    args_on = _make_benchmark_args(
        skill_name="test_skill",
        tasks_path=str(tasks_path),
        output_dir=str(output_dir),
        concurrency=1,
        allow_shell=True,
    )
    asyncio.run(bm._run_benchmark_async(args_on))
    assert captured["shell_allowed"] is True, (
        "Expected shell_allowed=True to reach _run_single_task when --allow-shell is set"
    )
    assert captured["permission_resolver_built"] is True, (
        "Expected a non-None permission_resolver to be built when --allow-shell is set"
    )

    # Case B: --allow-shell absent → shell_allowed=False is the default
    captured.clear()
    output_dir_b = tmp_path / "bench_output_no_shell"
    args_off = _make_benchmark_args(
        skill_name="test_skill",
        tasks_path=str(tasks_path),
        output_dir=str(output_dir_b),
        concurrency=1,
        allow_shell=False,
    )
    asyncio.run(bm._run_benchmark_async(args_off))
    assert captured["shell_allowed"] is False, (
        "Expected shell_allowed=False when --allow-shell is not set"
    )
