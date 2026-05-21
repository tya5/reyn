"""Measure chat-history pollution effect for issue #352.

Given an input history.jsonl state (= clean, or polluted with refusals)
and a target prompt, runs ``reyn chat`` ``N`` times in isolation and
counts how often the LLM made the expected ``list_actions`` tool call
(= the indicator that the LLM proactively used the discovery gateway
instead of refusing inline).

The script intentionally drives ``reyn chat`` as a subprocess (= same
path the user invokes) so the measurement matches the user-facing
symptom exactly. Each iteration uses a TEMPORARY agent workspace so
parallel measurement is safe and the user's real ``.reyn/agents/<...>``
state stays untouched.

Usage:

    python dogfood/scripts/measure_history_pollution.py \\
        --history-fixture <path-or-empty> \\
        --prompt "List the tables in the sqlite database." \\
        --n 10 \\
        --tool list_actions

``--history-fixture`` accepts:
  - a path to a ``history.jsonl`` to seed each iteration's workspace.
  - the literal string ``empty`` to start from an empty history (=
    matches the "clean" baseline from issue #352).

Output goes to stdout as a structured JSON line. Pipe through ``jq``
for human-readable inspection.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IterationResult:
    iteration: int
    target_tool_called: bool
    tool_calls_observed: list[str] = field(default_factory=list)
    error: str | None = None
    reply_excerpt: str = ""


def _seed_workspace(workspace: Path, agent: str, history_fixture: Path | None) -> None:
    """Materialise a clean workspace with the chosen initial history state."""
    agent_dir = workspace / ".reyn" / "agents" / agent
    state_dir = agent_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Profile is required by ChatSession's load path.
    profile = agent_dir / "profile.yaml"
    if not profile.exists():
        profile.write_text("name: " + agent + "\nrole: ''\n", encoding="utf-8")
    history_path = agent_dir / "history.jsonl"
    if history_fixture is None:
        history_path.write_text("", encoding="utf-8")
    else:
        shutil.copyfile(history_fixture, history_path)


def _parse_events_for_tool_calls(events_dir: Path, target_tool: str) -> tuple[bool, list[str]]:
    """Scan the newest events.jsonl under ``events_dir`` for ``tool_called``
    events. Returns ``(target_hit, all_tool_names)``.
    """
    if not events_dir.exists():
        return False, []
    candidates = sorted(events_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return False, []
    tools: list[str] = []
    hit = False
    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("name") == "tool_called":
                    tool = (ev.get("data") or {}).get("tool", "")
                    if tool:
                        tools.append(tool)
                    if tool == target_tool:
                        hit = True
        except OSError:
            continue
    return hit, tools


def _run_single_iteration(
    *,
    workspace: Path,
    agent: str,
    prompt: str,
    history_fixture: Path | None,
    target_tool: str,
    iteration: int,
    timeout_seconds: int,
) -> IterationResult:
    """Execute one ``reyn chat --cui --no-restore`` cycle and harvest result."""
    _seed_workspace(workspace, agent, history_fixture)
    env = os.environ.copy()
    # Force the chat session into the temp workspace.
    env["REYN_PROJECT_ROOT"] = str(workspace)
    env["HOME"] = str(workspace)  # in case any path falls back to ~
    try:
        proc = subprocess.run(
            ["reyn", "chat", agent, "--cui", "--no-restore"],
            input=prompt + "\n/quit\n",
            text=True, capture_output=True,
            cwd=str(workspace), env=env, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return IterationResult(
            iteration=iteration, target_tool_called=False,
            error=f"timeout after {timeout_seconds}s",
        )
    reply_excerpt = (proc.stdout or "")[-400:]
    events_dir = workspace / ".reyn" / "events"
    hit, tools = _parse_events_for_tool_calls(events_dir, target_tool)
    return IterationResult(
        iteration=iteration,
        target_tool_called=hit,
        tool_calls_observed=tools,
        reply_excerpt=reply_excerpt,
        error=None if proc.returncode == 0 else f"exit_code={proc.returncode}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-fixture", required=True,
        help="Path to a history.jsonl fixture, OR the literal 'empty' for an empty history.",
    )
    parser.add_argument(
        "--prompt", required=True,
        help="User prompt to send. Should be the same across measurement runs.",
    )
    parser.add_argument("--n", type=int, default=10, help="Iteration count.")
    parser.add_argument(
        "--tool", default="list_actions",
        help="Target tool name to count. Default 'list_actions' matches issue #352.",
    )
    parser.add_argument(
        "--agent", default="default",
        help="Agent name inside the temp workspace. Default 'default'.",
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="Per-iteration timeout (seconds).",
    )
    args = parser.parse_args(argv)

    if args.history_fixture == "empty":
        fixture: Path | None = None
    else:
        fixture_path = Path(args.history_fixture).resolve()
        if not fixture_path.exists():
            print(
                f"error: history fixture not found: {fixture_path}",
                file=sys.stderr,
            )
            return 2
        fixture = fixture_path

    results: list[IterationResult] = []
    for i in range(1, args.n + 1):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / ".reyn").mkdir(parents=True, exist_ok=True)
            result = _run_single_iteration(
                workspace=tmp_path,
                agent=args.agent,
                prompt=args.prompt,
                history_fixture=fixture,
                target_tool=args.tool,
                iteration=i,
                timeout_seconds=args.timeout,
            )
            results.append(result)
            print(
                f"# iter {i}/{args.n}: "
                f"target={'HIT' if result.target_tool_called else 'miss'}  "
                f"tools={result.tool_calls_observed[:4]}",
                file=sys.stderr,
            )

    hit_count = sum(1 for r in results if r.target_tool_called)
    summary = {
        "history_fixture": (
            "empty" if fixture is None else str(fixture)
        ),
        "prompt": args.prompt,
        "target_tool": args.tool,
        "n": args.n,
        "target_tool_hit_count": hit_count,
        "target_tool_hit_rate": hit_count / args.n if args.n else 0.0,
        "iterations": [
            {
                "i": r.iteration,
                "hit": r.target_tool_called,
                "tools": r.tool_calls_observed,
                "error": r.error,
            }
            for r in results
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
