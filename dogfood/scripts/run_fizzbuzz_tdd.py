#!/usr/bin/env python3
"""TDD iteration scenario driver — FizzBuzz with edge-case tests.

Sets up an isolated workspace from ``dogfood/fixtures/fizzbuzz_tdd/``,
pipes ``task.md`` into ``reyn chat --cui`` as a single user turn, and
afterwards runs ``pytest -q`` in the workspace to verify the agent's
implementation.

Per-run observations (printed as RESULT:<json>):

  - ``iterations``: number of pytest invocations the agent did (= proxy
    for fix loop depth). Counted by scanning the LLM trace for tool
    calls that look like ``pytest``.
  - ``write_calls``: number of write-to-fizzbuzz.py tool calls.
  - ``pytest_verdict``: PASS / FAIL based on the final ``pytest -q`` run
    by the driver (not the agent). Source of truth.
  - ``failed_tests``: list of failing test ids on the driver's final run.

Aggregates across N runs:

  - Pass rate (= verified / N)
  - Mean iterations
  - Mean write_calls
  - Fix-type distribution (= attractor classification possible if N>=5)

Usage:
    python dogfood/scripts/run_fizzbuzz_tdd.py [--n 3] [--timeout 240]
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
FIXTURE_DIR = REPO_ROOT / "dogfood" / "fixtures" / "fizzbuzz_tdd"


def setup_workspace(target: Path) -> str:
    """Copy fixture files into ``target`` and return a single-line task prompt.

    The prompt MUST be single-line because ``reyn chat --cui`` reads stdin
    line-by-line and would otherwise treat each newline as a separate user
    turn. So we hand the agent a pointer to ``task.md`` and let it read.

    Also copies the project's ``reyn.local.yaml`` if present so the LiteLLM
    ``api_base`` (= proxy URL the model alias depends on) is available in
    the workspace.
    """
    for name in ("fizzbuzz.py", "test_fizzbuzz.py", "task.md", "reyn.yaml"):
        shutil.copy2(FIXTURE_DIR / name, target / name)
    project_local = REPO_ROOT / "reyn.local.yaml"
    if project_local.exists():
        shutil.copy2(project_local, target / "reyn.local.yaml")
    return (
        "The current directory contains task.md, test_fizzbuzz.py, and fizzbuzz.py. "
        "Step 1: file__read path='task.md'. "
        "Step 2: file__read path='test_fizzbuzz.py'. "
        "Step 3: file__write path='fizzbuzz.py' with your implementation. "
        "Step 4: invoke_action 'exec__sandboxed_exec' with args "
        "{\"command\": [\"python\", \"-m\", \"pytest\", \"test_fizzbuzz.py\", \"-q\"]} "
        "to verify your implementation. "
        "Step 5: If any test fails, read the failure message and go back to step 3 (file__write a fix). "
        "Step 6: When all tests pass, reply 'all tests pass' and stop. "
        "Do not ask the user clarifying questions; act on the instructions directly."
    )


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


def parse_trace(trace_file: Path) -> dict:
    """Count pytest invocations + fizzbuzz.py writes in the trace."""
    if not trace_file.exists():
        return {"requests": 0, "iterations": 0, "write_calls": 0,
                "tool_calls": 0, "trace_missing": True}
    requests = 0
    tool_calls = 0
    iterations = 0
    write_calls = 0
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
                # iteration = each shell-class invocation that ran pytest.
                # Tool surface: invoke_action(action_name="exec__sandboxed_exec",
                # args={"argv": [..., "pytest", ...]}). Catch both the direct
                # name and the embedded action_name in args.
                blob_l = args_blob.lower()
                if "pytest" in blob_l and (
                    "sandboxed_exec" in blob_l
                    or "sandboxed_exec" in name
                    or "shell" in name
                ):
                    iterations += 1
                elif "pytest" in name:
                    iterations += 1
                if "fizzbuzz.py" in args_blob and (
                    "write" in name or "edit" in name or "str_replace" in name
                ):
                    write_calls += 1
    return {
        "requests": requests,
        "iterations": iterations,
        "write_calls": write_calls,
        "tool_calls": tool_calls,
    }


def run_pytest_verdict(cwd: Path) -> dict:
    """Driver's authoritative pytest verdict — runs after the agent stops."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_fizzbuzz.py", "-q",
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


def run_one(idx: int, timeout: int, keep_workspace: bool) -> dict:
    """One full trial: workspace setup → chat → pytest verdict."""
    workspace = Path(tempfile.mkdtemp(prefix=f"fizzbuzz_tdd_r{idx}_"))
    try:
        prompt = setup_workspace(workspace)
        trace_file = workspace / "llm_trace.jsonl"
        chat = run_chat(prompt, workspace, trace_file, timeout)
        trace = parse_trace(trace_file)
        verdict = run_pytest_verdict(workspace)
        final_impl = (workspace / "fizzbuzz.py").read_text()
        return {
            "run": idx,
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
    ap.add_argument("--n", type=int, default=3, help="number of runs")
    ap.add_argument("--timeout", type=int, default=240, help="per-run timeout (s)")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="don't delete tmp workspaces (debug)")
    args = ap.parse_args()

    results = []
    for i in range(1, args.n + 1):
        print(f"[run {i}/{args.n}] starting ...", flush=True)
        r = run_one(i, args.timeout, args.keep_workspace)
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
