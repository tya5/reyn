"""Worker prompt generator + worktree setup for dogfood batch dispatch.

Reads a YAML batch config (see ``dogfood_batch_config.py``) and emits:
  - one markdown worker prompt per worker (= the same shape past
    sandbox_2 sessions hand-rolled in chat at dispatch time);
  - prepares git worktrees + reyn.local.yaml under each worker's
    ``worktree`` path (= ready for ``reyn web`` start).

Usage::

    python scripts/dogfood_batch_dispatch.py --config batch.yaml [--prompts-dir <dir>]

  Without ``--prompts-dir`` the prompts are printed to stdout (one per
  worker, separated by ``--- WORKER N ---`` markers). With the flag,
  one file per worker is written to that directory.

Worktree + reyn.local.yaml setup is OPT-IN via ``--setup-worktrees``:
  - Without the flag, the script only emits prompts (= dry run, no
    filesystem mutation outside the prompts dir).
  - With the flag, git worktrees are created at the configured paths +
    reyn.local.yaml copied from the repo root + ``models.strong``
    forced to flash-lite (= feedback_no_strong_model compliance).

Per-batch past-batch verdict citation: the prompts include the
worker's prior verdicts pulled from the most recent ``past_batches``
entry (= the one listed FIRST in the config). This eliminates the
manual paste step that previously took ~10-15 min per batch dispatch.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# scripts/ is on the script's path naturally; this import works for
# both ``python scripts/dogfood_batch_dispatch.py`` invocation styles.
sys.path.insert(0, str(Path(__file__).parent))

from dogfood_batch_config import (  # noqa: E402
    BatchConfig,
    WorkerSpec,
    load_batch_config,
)


def _past_verdicts_for_worker(
    config: BatchConfig, worker_name: str,
) -> dict[str, dict[str, int]]:
    """Pull per-batch verdicts for ``worker_name`` from each past batch's
    aggregate.json. Skip past batches that don't have the worker listed.

    Returns a dict mapping past-batch name → {V, I, R, B} counts.
    """
    out: dict[str, dict[str, int]] = {}
    for pb in config.past_batches:
        path = Path(pb.aggregate_path)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        workers = data.get("workers") or {}
        # past aggregate keys typically look like "W1_chat_router_smoke";
        # match by the leading "W<n>" prefix to tolerate the suffix.
        match = None
        for key, val in workers.items():
            if key.startswith(worker_name + "_") or key == worker_name:
                match = val
                break
        if match is None:
            continue
        out[pb.name] = {
            "V": int(match.get("v") or match.get("V") or 0),
            "I": int(match.get("i") or match.get("I") or 0),
            "R": int(match.get("r") or match.get("R") or 0),
            "B": int(match.get("b") or match.get("B") or 0),
        }
    return out


def render_worker_prompt(
    config: BatchConfig,
    worker: WorkerSpec,
    repo_root: Path | None = None,
) -> str:
    """Compose the markdown worker prompt.

    Matches the structure the sandbox_2 session has been hand-rolling
    for B42 / B43: setup commands, run instructions, past-batch
    verdict citation, deliverable spec, hard caps.
    """
    env_block = " ".join(
        f"{k}={v!s}" for k, v in sorted(config.batch.env_vars.items())
    )
    past_table = _past_verdicts_for_worker(config, worker.name)
    if past_table:
        past_rows = "\n".join(
            f"| {pb_name} | {counts['V']}/{worker.n_scenarios} | "
            f"{counts['I']} | {counts['R']} | {counts['B']} |"
            for pb_name, counts in past_table.items()
        )
        past_section = (
            "## Past-batch verdicts (= primary data, cite verbatim)\n\n"
            "| Batch | V | I | R | B |\n"
            "|-------|---|---|---|---|\n"
            f"{past_rows}\n"
        )
    else:
        past_section = (
            "## Past-batch verdicts\n\n"
            "(no past batches found in config for this worker)\n"
        )
    tool_uses_cap = config.batch.hard_caps.get("tool_uses", 50)
    wall_cap = config.batch.hard_caps.get("wall_clock_min", 15)
    # B45 carry-over fix (2026-05-22): the deliverable path is now emitted
    # as an absolute filesystem path against the main-repo root, with an
    # explicit "from MAIN repo CWD" line in the prompt. Pre-fix, the path
    # was relative (= config.journal_dir/workers/...) and sub-agents
    # running from the worker's worktree CWD wrote to the worktree-relative
    # location — the main repo never saw the results JSON without a
    # post-batch `cp` step. Observed B44/B45/B47 W2/W3/W7.
    repo_root_abs = repo_root.resolve() if repo_root else Path.cwd().resolve()
    deliverable_rel = (
        f"{config.journal_dir}/workers/results-worker-"
        f"{worker.name.lstrip('W')}.json"
    )
    deliverable_path = str(repo_root_abs / deliverable_rel)

    return f"""{config.batch.name} worker {worker.name} — run `{worker.scenario_set_path}` ({worker.n_scenarios} scenarios), emit results JSON.

## Setup

```bash
cd {worker.worktree}
rm -rf .reyn/agents .reyn/state .reyn/events .reyn/action_index .reyn/llm_trace.jsonl 2>/dev/null
{env_block} \\
  REYN_LLM_TRACE_DUMP=$(pwd)/.reyn/llm_trace.jsonl \\
  nohup /Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2/venv/bin/reyn web \\
    --port {worker.port} --log-level warning > /tmp/b{config.batch.name.lstrip('B')}-{worker.name.lstrip('W')}-server.log 2>&1 &
sleep 4
curl -s -o /dev/null -w "%{{http_code}}\\n" http://localhost:{worker.port}/health  # must be 200
```

## Run scenarios

For each of the {worker.n_scenarios} scenarios in `{worker.scenario_set_path}`:
1. Create a fresh agent: `reyn agent new {worker.agent_prefix}<i>` (i=1..{worker.n_scenarios})
2. POST the scenario's user prompt to `http://localhost:{worker.port}/a2a/agents/{worker.agent_prefix}<i>` (JSON-RPC `message/send`)
3. Capture reply text + http_status + events.jsonl path

## Verdict rules

- **V (verified)**: rubric items all met + events_pass=true
- **I (inconclusive)**: rubric partial OR skill failed for env reason (allow-unsafe-python etc.)
- **R (refuted)**: rubric not met
- **B (blocked)**: scenario didn't run end-to-end

{past_section}

## User params (= hold constant for apples-to-apples)

{json.dumps(config.batch.user_params, indent=2)}

## Deliverable

Write the results JSON at the **absolute path** below — sub-agents running from a worker worktree CWD must use this exact path so the main repo receives the file (= worktree-relative paths leave the result inside the worktree and the aggregator never sees it). The MAIN repo CWD is `{repo_root_abs}`.

Write `{deliverable_path}` with this shape:

```json
{{
  "batch": "{config.batch.name}", "worker": {worker.name.lstrip('W')},
  "head": "{config.batch.head}", "date": "{config.batch.date}",
  "scenario_set": "{worker.scenario_set}",
  "env_vars": {json.dumps(config.batch.env_vars)},
  "user_params": {json.dumps(config.batch.user_params)},
  "verdicts": {{"V": ?, "I": ?, "R": ?, "B": ?}},
  "vs_past": {{...}},
  "scenarios": [{{...}}, ...],
  "new_findings": [...]
}}
```

## Hard caps (= feedback_subagent_scope_bounding)

- **≤{tool_uses_cap} tool uses**
- **≤{wall_cap} min wall-clock**
- 1 deliverable: results-worker-{worker.name.lstrip('W')}.json
- NO findings.md, NO retrospective (= main agent aggregates)
- NO strong model use (= verify reyn.local.yaml strong=flash-lite)

When done, report in <100 words: V/I/R/B counts + 1-line per-scenario verdict + 1-line vs-past delta. Then STOP.
"""


def setup_worktree(worker: WorkerSpec, head: str, repo_root: Path) -> None:
    """Create the git worktree at the configured path and prep
    reyn.local.yaml with flash-lite-only tiers.

    Idempotent: if the worktree already exists, leaves it in place
    (= caller can re-run dispatch without re-cloning).
    """
    wt = Path(worker.worktree)
    if not wt.is_dir():
        subprocess.run(
            ["git", "worktree", "add", str(wt), head],
            cwd=repo_root, check=True,
        )
    # Copy reyn.local.yaml + force flash-lite on all tiers
    src = repo_root / "reyn.local.yaml"
    if not src.is_file():
        return  # nothing to copy
    dst = wt / "reyn.local.yaml"
    shutil.copy(src, dst)
    text = dst.read_text()
    # Force strong tier to flash-lite (= feedback_no_strong_model)
    text = text.replace(
        "strong:   openai/gemini-2.5-flash\n",
        "strong:   openai/gemini-2.5-flash-lite\n",
    )
    dst.write_text(text)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--config", required=True, type=Path,
        help="YAML batch config (see dogfood_batch_config.py).",
    )
    p.add_argument(
        "--prompts-dir", type=Path, default=None,
        help="Write one prompt file per worker into this directory. "
        "Without it, prompts print to stdout.",
    )
    p.add_argument(
        "--setup-worktrees", action="store_true",
        help="Create git worktrees + copy reyn.local.yaml. Without "
        "this flag the script is dry-run (= prompts only).",
    )
    p.add_argument(
        "--repo-root", type=Path, default=Path.cwd(),
        help="Repo root for worktree creation (default: cwd).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    config = load_batch_config(args.config)
    if args.setup_worktrees:
        for w in config.workers:
            setup_worktree(w, config.batch.head, args.repo_root)
            print(f"[setup] worktree ready: {w.worktree}", file=sys.stderr)
    if args.prompts_dir:
        args.prompts_dir.mkdir(parents=True, exist_ok=True)
        for w in config.workers:
            prompt = render_worker_prompt(config, w, repo_root=args.repo_root)
            (args.prompts_dir / f"worker-{w.name}.md").write_text(prompt)
            print(f"[prompt] {w.name} → {args.prompts_dir}/worker-{w.name}.md",
                  file=sys.stderr)
    else:
        for w in config.workers:
            print(f"--- WORKER {w.name} ---")
            print(render_worker_prompt(config, w, repo_root=args.repo_root))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
