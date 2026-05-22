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
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IterationResult:
    iteration: int
    target_tool_called: bool
    tool_calls_observed: list[str] = field(default_factory=list)
    error: str | None = None
    reply_excerpt: str = ""


def _seed_agent_history(project_root: Path, agent: str, history_fixture: Path | None) -> None:
    """Reset the chosen agent's history + state under ``project_root/.reyn/``.

    Uses an actual agent slot inside the real project workspace so the
    ``reyn.local.yaml`` MCP config is found (= isolated temp workspaces
    would not see ``reyn.yaml`` / ``reyn.local.yaml`` at the repo root).
    Callers should pass a dedicated agent name (= ``pollution_test``)
    distinct from ``default`` to avoid clobbering the user's chat state.
    """
    agent_dir = project_root / ".reyn" / "agents" / agent
    state_dir = agent_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    profile = agent_dir / "profile.yaml"
    if not profile.exists():
        profile.write_text("name: " + agent + "\nrole: ''\n", encoding="utf-8")
    history_path = agent_dir / "history.jsonl"
    if history_fixture is None:
        history_path.write_text("", encoding="utf-8")
    else:
        shutil.copyfile(history_fixture, history_path)
    # Wipe any leftover snapshot so --no-restore actually starts fresh.
    snapshot = state_dir / "snapshot.json"
    if snapshot.exists():
        snapshot.unlink()
    # Wipe leftover skill snapshots too — they can leak skill-run state
    # across iterations and pollute the measurement.
    skills_dir = state_dir / "skills"
    if skills_dir.exists():
        shutil.rmtree(skills_dir)


def _parse_events_for_tool_calls_in_files(
    event_files: set[Path], target_tool: str,
) -> tuple[bool, list[str]]:
    """Scan a set of events.jsonl paths for ``tool_called`` events.
    Returns ``(target_hit, all_tool_names)``.
    """
    tools: list[str] = []
    hit = False
    for path in sorted(event_files, key=lambda p: p.stat().st_mtime if p.exists() else 0):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                # Reyn events use ``type`` as the discriminator.
                ev_type = ev.get("type") or ev.get("name")
                if ev_type == "tool_called":
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
    project_root: Path,
    agent: str,
    prompt: str,
    history_fixture: Path | None,
    target_tool: str,
    iteration: int,
    timeout_seconds: int,
    model_class: str | None = None,
) -> IterationResult:
    """Execute one ``reyn chat --cui --no-restore`` cycle and harvest result.

    Runs inside the real project workspace (= ``project_root``) so the
    repo-level ``reyn.yaml`` / ``reyn.local.yaml`` MCP config is picked
    up. Each iteration re-seeds ``project_root/.reyn/agents/<agent>/``
    before the run to remove any state from the previous iteration.
    """
    _seed_agent_history(project_root, agent, history_fixture)
    # Track events.jsonl files that existed BEFORE this iteration so we
    # can identify which ones were created by THIS run.
    events_root = project_root / ".reyn" / "events" / "agents" / agent
    pre_existing = set(
        events_root.rglob("*.jsonl")
    ) if events_root.exists() else set()
    cmd = ["reyn", "chat", agent, "--cui", "--no-restore"]
    if model_class:
        cmd.extend(["--model", model_class])
    try:
        proc = subprocess.run(
            cmd,
            input=prompt + "\n/quit\n",
            text=True, capture_output=True,
            cwd=str(project_root), timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return IterationResult(
            iteration=iteration, target_tool_called=False,
            error=f"timeout after {timeout_seconds}s",
        )
    reply_excerpt = (proc.stdout or "")[-400:]
    # Only count events from files created during this iteration.
    new_event_files = (
        set(events_root.rglob("*.jsonl")) - pre_existing
        if events_root.exists() else set()
    )
    hit, tools = _parse_events_for_tool_calls_in_files(
        new_event_files, target_tool,
    )
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
        "--agent", default="pollution_test",
        help=(
            "Agent name within the project workspace. Default "
            "'pollution_test' to avoid clobbering the user's 'default' agent."
        ),
    )
    parser.add_argument(
        "--project-root", default=".",
        help=(
            "Project root that holds reyn.yaml / reyn.local.yaml / .reyn/. "
            "Defaults to the current working directory."
        ),
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="Per-iteration timeout (seconds).",
    )
    parser.add_argument(
        "--model", default=None,
        help=(
            "Model class to pass to ``reyn chat --model``. Default unset "
            "(= uses reyn.yaml's top-level ``model:`` setting). Useful "
            "for A/B comparing weak vs strong model behaviour on the "
            "same fixture."
        ),
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

    project_root = Path(args.project_root).resolve()
    if not (project_root / ".reyn").exists():
        print(
            f"error: {project_root} does not contain .reyn/; "
            "pass --project-root pointing at a Reyn project root.",
            file=sys.stderr,
        )
        return 2

    results: list[IterationResult] = []
    for i in range(1, args.n + 1):
        result = _run_single_iteration(
            project_root=project_root,
            agent=args.agent,
            prompt=args.prompt,
            history_fixture=fixture,
            target_tool=args.tool,
            iteration=i,
            timeout_seconds=args.timeout,
            model_class=args.model,
        )
        results.append(result)
        print(
            f"# iter {i}/{args.n}: "
            f"target={'HIT' if result.target_tool_called else 'miss'}  "
            f"tools={result.tool_calls_observed[:4]}",
            file=sys.stderr,
        )

    hit_count = sum(1 for r in results if r.target_tool_called)
    # Refusal = the LLM produced a final reply with NO tool calls (=
    # text-only refusal pattern). This is the actual symptom of #352
    # in-context-learning trap, independent of which specific tool the
    # LLM "should" have called.
    refusal_count = sum(1 for r in results if not r.tool_calls_observed)
    any_tool_count = sum(1 for r in results if r.tool_calls_observed)
    summary = {
        "history_fixture": (
            "empty" if fixture is None else str(fixture)
        ),
        "prompt": args.prompt,
        "target_tool": args.tool,
        "n": args.n,
        "target_tool_hit_count": hit_count,
        "target_tool_hit_rate": hit_count / args.n if args.n else 0.0,
        # Refusal-rate metric for #352 in-context-learning measurement.
        "refusal_count": refusal_count,
        "refusal_rate": refusal_count / args.n if args.n else 0.0,
        "any_tool_call_count": any_tool_count,
        "any_tool_call_rate": any_tool_count / args.n if args.n else 0.0,
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
