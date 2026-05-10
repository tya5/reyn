#!/usr/bin/env python3
"""Dogfood batch 17 — S1 driver: Empty state UX.

Runs N=3 fresh chat turns from an empty .reyn/ state in the worktree,
observing:
1. CLI smoke: `reyn source list` output
2. Router system prompt: contains ## Indexed sources (0 available) section
3. LLM reply: acknowledges 0 sources, doesn't hallucinate indexed sources

Clears history.jsonl between runs to ensure N=3 independence
(fix for B16-S1-1 pattern).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REYN_ROOT = Path(__file__).parent.parent
WORKTREE = REYN_ROOT / ".claude" / "worktrees" / "agent-a48a9e952f60592d4"
AGENT_DIR = WORKTREE / ".reyn" / "agents" / "default"
TRACE_DIR = Path("/tmp/reyn_s1_traces")
PROMPT = "What can I do? List my available data sources."
N_RUNS = 3


def clean_history() -> None:
    """Wipe history.jsonl so next run is a fresh session."""
    history = AGENT_DIR / "history.jsonl"
    if history.exists():
        history.unlink()
        print(f"  [clean] Removed {history}")


def run_source_list() -> str:
    """Run `reyn source list` from worktree, return stdout."""
    result = subprocess.run(
        ["reyn", "source", "list"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(WORKTREE),
    )
    return result.stdout.strip()


def run_chat_turn(run_id: int) -> dict:
    """Run a single chat turn and return observations."""
    trace_file = TRACE_DIR / f"run_{run_id}.jsonl"
    TRACE_DIR.mkdir(exist_ok=True)

    start = time.time()
    env = {**os.environ, "REYN_LLM_TRACE_DUMP": str(trace_file)}

    result = subprocess.run(
        ["reyn", "chat", "--cui"],
        input=f"{PROMPT}\n",
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(WORKTREE),
        env=env,
    )
    elapsed = time.time() - start

    # Parse trace
    system_prompt_section = ""
    llm_reply = ""
    tool_calls = []
    usage = {}

    if trace_file.exists():
        lines = trace_file.read_text().strip().splitlines()
        for line in lines:
            d = json.loads(line)
            kind = d.get("kind", "")
            if kind == "request" and "messages" in d:
                for m in d["messages"]:
                    if m.get("role") == "system":
                        content = str(m.get("content", ""))
                        idx = content.find("## Indexed sources")
                        if idx >= 0:
                            system_prompt_section = content[idx : idx + 600]
            elif kind == "response":
                llm_reply = d.get("content") or ""
                tool_calls = d.get("tool_calls") or []
                usage = d.get("usage") or {}

    return {
        "run_id": run_id,
        "elapsed": round(elapsed, 1),
        "returncode": result.returncode,
        "llm_reply": llm_reply,
        "tool_calls": tool_calls,
        "usage": usage,
        "system_prompt_indexed_section": system_prompt_section,
        "stderr": result.stderr[:300],
    }


def assess_verdict(obs: dict) -> str:
    """Assess run verdict from observations."""
    reply = obs["llm_reply"].lower()

    # Hallucination check: claims indexed sources exist
    hallucination_signals = [
        "indexed source",
        "data source" in reply and "no" not in reply[:reply.find("data source") + 20 if "data source" in reply else 0],
        "available source",
        "your sources",
        "reyn_docs",
        "memory source",
        "src/",
    ]
    # Check each individually
    hallucinates = False
    for sig in hallucination_signals:
        if isinstance(sig, str) and sig in reply:
            hallucinates = True
            break
        elif isinstance(sig, bool) and sig:
            hallucinates = True
            break

    # No-source acknowledgment signals
    acknowledges_no_sources = any(s in reply for s in [
        "no indexed",
        "don't have any indexed",
        "don't currently have",
        "haven't indexed",
        "no data sources",
        "0 available",
        "currently have no",
        "no sources",
        "index_docs",
        "reyn run index_docs",
    ])

    # System prompt check
    sp_ok = "Indexed sources (0 available)" in obs["system_prompt_indexed_section"]

    # Blocked if no reply at all
    if not obs["llm_reply"] and obs["returncode"] != 0:
        return "blocked"

    if hallucinates:
        return "refuted"

    # Verified: SP has section AND LLM acknowledges no sources
    if sp_ok and acknowledges_no_sources:
        return "verified"

    # Inconclusive: SP ok but LLM neither hallucinates nor acknowledges
    if sp_ok and not hallucinates:
        return "inconclusive"

    return "inconclusive"


def main() -> None:
    print("=" * 60)
    print("S1: Empty state UX — Dogfood Batch 17")
    print(f"Worktree: {WORKTREE}")
    print("=" * 60)

    # Observation 1: CLI smoke test (once, not per-run)
    print("\n[CLI Smoke] Running `reyn source list`...")
    cli_output = run_source_list()
    print(f"  Output: {cli_output!r}")
    cli_has_hint = "No indexed sources" in cli_output and "reyn run index_docs" in cli_output
    print(f"  CLI hint present: {cli_has_hint}")

    results = []
    for run_id in range(1, N_RUNS + 1):
        print(f"\n[Run {run_id}/{N_RUNS}] Cleaning history, then running chat...")
        clean_history()

        obs = run_chat_turn(run_id)
        verdict = assess_verdict(obs)

        print(f"  Elapsed: {obs['elapsed']}s")
        print(f"  LLM reply ({len(obs['llm_reply'])} chars): {obs['llm_reply'][:300]!r}")
        print(f"  Tool calls: {obs['tool_calls']}")
        print(f"  SP section present: {'Indexed sources (0 available)' in obs['system_prompt_indexed_section']}")
        print(f"  Verdict: {verdict}")

        results.append({**obs, "verdict": verdict, "cli_ok": cli_has_hint})

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    verdicts = [r["verdict"] for r in results]
    for v in ("verified", "refuted", "inconclusive", "blocked"):
        count = verdicts.count(v)
        print(f"  {v}: {count}/{N_RUNS}")

    print(f"\nCLI hint: {'PASS' if cli_has_hint else 'FAIL'}")
    print(f"SP section (all runs): {all('Indexed sources (0 available)' in r['system_prompt_indexed_section'] for r in results)}")

    # Write results to JSON for finding doc
    out = {
        "cli_output": cli_output,
        "cli_has_hint": cli_has_hint,
        "runs": results,
    }
    out_path = Path("/tmp/s1_results.json")
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
