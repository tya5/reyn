#!/usr/bin/env python3
"""Iteration-loop dogfood driver — TDD / bug-planted / similar.

Sets up an isolated workspace from ``dogfood/fixtures/<scenario>/``,
pipes a directive prompt into ``reyn chat --cui`` as a single user turn,
and afterwards runs ``pytest -q`` in the workspace to verify the agent's
work.

Each scenario folder must contain:

  - ``task.md``      — natural-language instructions for the agent
  - ``test_*.py``    — pytest tests (one file expected)
  - ``<impl>.py``    — implementation file the agent must edit
  - ``reyn.yaml``    — model alias + sandbox + permissions for the workspace

Built-in scenarios (= difficulty curve, gemini-2.5-flash-lite baseline):

  | scenario                       | N  | pass | mean_iter | dominant defect    |
  |--------------------------------|----|------|-----------|--------------------|
  | ``fizzbuzz_tdd``               | 5  | 80%  | 1.6       | early-bail         |
  | ``fizzbuzz_bug_planted``       | 5  | 100% | 2.2       | —                  |
  | ``fizzbuzz_5bugs_interleaved`` | 10 | 40%  | 1.7       | early-bail +       |
  |                                |    |      |           | zero-special-case  |

  - ``fizzbuzz_tdd``: failing tests + empty stub. Agent reads tests +
    writes impl; iteration only fires when first guess misses an
    edge case.
  - ``fizzbuzz_bug_planted``: 3 independent bugs (zero special-case +
    positive-only guard + int-return-on-default). Each bug fails a
    distinct test, agent fixes via diagnose-then-write.
  - ``fizzbuzz_5bugs_interleaved``: 5 bugs that mask each other —
    fixing the order-of-check bug surfaces the typo it hid, fixing
    the positive-only guard surfaces the int-return bug for negatives,
    etc. Forces real iteration: 60% non-pass rate exposes two
    attractor patterns (early-bail, zero-special-case "obvious-idiom
    preservation").

Per-run observations:

  - ``iterations``  — pytest invocations the agent issued
  - ``write_calls`` — edits to the implementation file
  - ``pytest_verdict`` — driver's authoritative pytest pass/fail
  - ``failed_tests`` — failing test ids on the driver's final run

Aggregates: pass_rate, mean_iterations, mean_writes, mean_requests.

Usage:
    python dogfood/scripts/run_dogfood_iterate.py \\
        --scenario fizzbuzz_bug_planted [--n 5] [--timeout 300]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "dogfood" / "fixtures"


def setup_workspace(scenario: str, target: Path) -> str:
    """Copy the scenario's fixture files into ``target`` and return the
    scenario-specific directive prompt.

    The fixture must contain a ``prompt.txt`` whose **first line** is the
    single-line directive sent to the agent.  ``reyn chat --cui`` reads
    stdin line-by-line and would otherwise split a multiline prompt into
    many turns.

    Also copies the project's ``reyn.local.yaml`` if present so the LiteLLM
    ``api_base`` (= proxy URL the model alias depends on) is available in
    the workspace.
    """
    fixture_dir = FIXTURES_ROOT / scenario
    if not fixture_dir.is_dir():
        raise FileNotFoundError(
            f"scenario {scenario!r} not found under {FIXTURES_ROOT}",
        )
    prompt_path = fixture_dir / "prompt.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"scenario {scenario!r} missing prompt.txt",
        )
    for entry in fixture_dir.iterdir():
        if entry.is_file():
            shutil.copy2(entry, target / entry.name)
    project_local = REPO_ROOT / "reyn.local.yaml"
    if project_local.exists():
        shutil.copy2(project_local, target / "reyn.local.yaml")
    # First non-empty line is the directive (= single-line invariant).
    for line in prompt_path.read_text().splitlines():
        if line.strip():
            return line.strip()
    raise ValueError(f"{prompt_path} is empty")


def run_chat(prompt: str, cwd: Path, trace_file: Path, timeout: int) -> dict:
    """Pipe the prompt through ``reyn chat --cui`` once."""
    env = {**os.environ, "REYN_LLM_TRACE_DUMP": str(trace_file)}
    start = time.time()
    proc = subprocess.run(
        ["reyn", "chat", "--cui"],
        input=f"{prompt}\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=env,
    )
    return {
        "elapsed_s": round(time.time() - start, 1),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-1200:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-1200:] if proc.stderr else "",
    }


def parse_trace(trace_file: Path, impl_filename: str) -> dict:
    """Count pytest invocations + ``impl_filename`` writes in the trace."""
    if not trace_file.exists():
        return {"requests": 0, "iterations": 0, "write_calls": 0,
                "tool_calls": 0, "trace_missing": True}
    requests = 0
    tool_calls = 0
    iterations = 0
    write_calls = 0
    impl_l = impl_filename.lower()
    for raw in trace_file.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = d.get("kind", "")
        if kind == "request":
            requests += 1
        elif kind == "response":
            for tc in d.get("tool_calls") or []:
                tool_calls += 1
                # OpenAI-style: {id, type, function: {name, arguments}};
                # arguments is a JSON string. Anthropic-style: {name, input}.
                fn = tc.get("function") or {}
                name = (fn.get("name") or tc.get("name") or "").lower()
                args_blob = (
                    fn.get("arguments")
                    or json.dumps(tc.get("input") or {})
                    or ""
                )
                args_blob = str(args_blob)
                blob_l = args_blob.lower()
                # iteration = each shell-class invocation that ran pytest.
                # Tool surface: invoke_action(action_name="exec__sandboxed_exec",
                # args={"argv": [..., "pytest", ...]}). Catch both the direct
                # name and the embedded action_name in args.
                if "pytest" in blob_l and (
                    "sandboxed_exec" in blob_l
                    or "sandboxed_exec" in name
                    or "shell" in name
                ):
                    iterations += 1
                elif "pytest" in name:
                    iterations += 1
                # write = file write to the scenario's implementation file.
                # Match both direct (file__write) and wrapped
                # (invoke_action(action_name="file__write", ...)) surfaces.
                if impl_l in blob_l and (
                    "file__write" in blob_l
                    or "file__write" in name
                    or "write" in name
                    or "edit" in name
                    or "str_replace" in name
                ):
                    write_calls += 1
    return {
        "requests": requests,
        "iterations": iterations,
        "write_calls": write_calls,
        "tool_calls": tool_calls,
    }


def run_pytest_verdict(cwd: Path, test_file: str) -> dict:
    """Driver's authoritative pytest verdict — runs after the agent stops."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-q",
         "--tb=line", "--no-header"],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    failed_tests = [
        line.strip() for line in combined.splitlines()
        if " FAILED" in line or line.startswith("FAILED")
    ]
    return {
        "verdict": "PASS" if proc.returncode == 0 else "FAIL",
        "returncode": proc.returncode,
        "failed_tests": failed_tests[:10],
        "pytest_tail": combined[-600:],
    }


def _scenario_files(scenario: str) -> tuple[str, str]:
    """Return ``(test_filename, impl_filename)`` for ``scenario``."""
    fixture_dir = FIXTURES_ROOT / scenario
    test_files = sorted(fixture_dir.glob("test_*.py"))
    if not test_files:
        raise FileNotFoundError(f"no test_*.py in {fixture_dir}")
    test_file = test_files[0].name
    # impl = first non-test, non-config .py
    impl_files = [
        p.name for p in fixture_dir.glob("*.py")
        if not p.name.startswith("test_")
    ]
    if not impl_files:
        raise FileNotFoundError(f"no impl .py in {fixture_dir}")
    return test_file, impl_files[0]


def run_one(
    scenario: str, idx: int, timeout: int, keep_workspace: bool,
) -> dict:
    """One full trial: workspace setup → chat → pytest verdict."""
    test_file, impl_file = _scenario_files(scenario)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"{scenario}_r{idx}_"),
    )
    try:
        prompt = setup_workspace(scenario, workspace)
        trace_file = workspace / "llm_trace.jsonl"
        chat = run_chat(prompt, workspace, trace_file, timeout)
        trace = parse_trace(trace_file, impl_file)
        verdict = run_pytest_verdict(workspace, test_file)
        final_impl = (workspace / impl_file).read_text()
        return {
            "run": idx,
            "scenario": scenario,
            "workspace": str(workspace),
            "chat": chat,
            "trace": trace,
            **verdict,
            "final_impl_tail": final_impl[-400:],
        }
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="fizzbuzz_tdd",
                    help="fixture folder under dogfood/fixtures/")
    ap.add_argument("--n", type=int, default=3, help="number of runs")
    ap.add_argument("--timeout", type=int, default=240, help="per-run timeout (s)")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="don't delete tmp workspaces (debug)")
    args = ap.parse_args()

    print(f"[scenario] {args.scenario}", flush=True)
    results = []
    for i in range(1, args.n + 1):
        print(f"[run {i}/{args.n}] starting ...", flush=True)
        r = run_one(args.scenario, i, args.timeout, args.keep_workspace)
        results.append(r)
        print(
            f"[run {i}/{args.n}] verdict={r['verdict']} "
            f"iterations={r['trace']['iterations']} "
            f"writes={r['trace']['write_calls']} "
            f"requests={r['trace']['requests']} "
            f"elapsed={r['chat']['elapsed_s']}s",
            flush=True,
        )

    n = len(results)
    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    mean_iter = sum(r["trace"]["iterations"] for r in results) / max(n, 1)
    mean_writes = sum(r["trace"]["write_calls"] for r in results) / max(n, 1)
    mean_requests = sum(r["trace"]["requests"] for r in results) / max(n, 1)

    summary = {
        "scenario": args.scenario,
        "n": n,
        "pass_rate": round(n_pass / max(n, 1), 2),
        "n_pass": n_pass,
        "n_fail": n - n_pass,
        "mean_iterations": round(mean_iter, 2),
        "mean_write_calls": round(mean_writes, 2),
        "mean_llm_requests": round(mean_requests, 2),
    }
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\n=== RUNS ===")
    for r in results:
        compact = {
            "run": r["run"],
            "verdict": r["verdict"],
            "iterations": r["trace"]["iterations"],
            "writes": r["trace"]["write_calls"],
            "requests": r["trace"]["requests"],
            "tool_calls": r["trace"]["tool_calls"],
            "elapsed_s": r["chat"]["elapsed_s"],
            "failed_tests": r["failed_tests"],
        }
        print(json.dumps(compact, ensure_ascii=False))


if __name__ == "__main__":
    main()
