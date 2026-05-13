"""dogfood_g4_spike.py — G4 spike driver: FP-0011 narration quality across 3 conditions.

Orchestrates multi-condition spike runs with RPD protection and idempotent resume.
Helpers live in scripts/spike_lib/; judge in scripts/spike_judge.py.

Conditions:
  weak-baseline       main branch, model class standard (flash-lite)
  weak-experimental   spike branch + flash-lite
  strong-experimental spike branch + model class strong (gemini-2.5-flash)

RPD constraint: gemini-2.5-flash has 10K req/day. Driver stops before 8K.

Usage:
    python scripts/dogfood_g4_spike.py \\
      --scenarios dogfood/scenarios/fp_0011_narration.yaml \\
      --branch claude/fp-0011-narrator-removal-spike \\
      --phase primary \\
      [--smoke-test] \\
      [--out spike_results/fp_0011/]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── spike_lib on sys.path ────────────────────────────────────────────────────

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from spike_lib.events import (
    count_flash_requests,
    count_llm_calls,
    extract_final_output,
    read_events_since,
    save_run_events,
)
from spike_lib.http import build_message_send, extract_reply, post_json
from spike_lib.state import (
    RPD_ESTIMATED_PER_RUN,
    RPD_HARD_CAP,
    append_run,
    compute_summary,
    load_completed_runs,
    load_rpd_state,
    save_rpd_state,
)
from spike_lib.worktree import (
    ensure_worktree,
    remove_model_override,
    start_web_server,
    stop_web_server,
    wait_for_server,
    worktree_path,
    write_model_override,
)

# ── YAML loading ─────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore[import]
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        print("[error] PyYAML not found. Install with: pip install pyyaml", file=sys.stderr)
        raise


# ── Condition metadata ────────────────────────────────────────────────────────

# Ports: 8081 for main branch, 8082 for spike branch.
# Strong and weak-experimental share the spike branch worktree on 8082 —
# they run sequentially with server restarts between them.
_CONDITION_META: dict[str, dict] = {
    "weak-baseline": {
        "branch": "main",
        "model_class": "standard",
        "port": 8081,
        "is_flash": False,
    },
    "weak-experimental": {
        "branch": None,      # filled at runtime from --branch
        "model_class": "standard",
        "port": 8082,
        "is_flash": False,
    },
    "strong-experimental": {
        "branch": None,      # filled at runtime from --branch
        "model_class": "strong",
        "port": 8082,
        "is_flash": True,
    },
}

STRONG_MODEL_STRING = "openai/gemini-2.5-flash"  # thinking off via model config


# ── Scenarios loader ──────────────────────────────────────────────────────────


def _load_scenarios(path: Path) -> list[dict]:
    if not path.exists():
        print(f"[error] scenarios file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = _load_yaml(path)
    except Exception as exc:
        print(f"[error] could not parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        print("[error] 'scenarios' must be a non-empty list", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(scenarios):
        for field in ("id", "user_prompt"):
            if field not in s:
                print(f"[error] scenario[{i}] missing required field {field!r}", file=sys.stderr)
                sys.exit(1)
    # Filter out disabled scenarios (= enabled: false). Keep their order.
    enabled_scenarios = [s for s in scenarios if s.get("enabled", True)]
    skipped = [s["id"] for s in scenarios if not s.get("enabled", True)]
    if skipped:
        print(f"[driver] skipping disabled scenarios: {skipped}", flush=True)
    if not enabled_scenarios:
        print("[error] all scenarios disabled — nothing to run", file=sys.stderr)
        sys.exit(1)
    return enabled_scenarios


# ── Agent provisioning (Bug 2 fix) ────────────────────────────────────────────


def _ensure_agent(*, worktree: Path, name: str) -> None:
    """Create a Reyn agent in the worktree, REPLACING any existing one.

    The A2A endpoint does NOT auto-create unknown agents (= returns
    JSON-RPC -32602), so the spike driver must pre-provision each
    unique agent name before POSTing.

    Replace semantics: if the agent already exists (= leftover from a
    prior driver run with the same agent name), `reyn agent rm` it
    first so each invocation starts with a fresh ChatSession + empty
    history. The driver's idempotent-resume mechanism (= runs.jsonl)
    handles the "skip completed runs" case BEFORE this is called, so
    by the time _ensure_agent fires the run is genuinely new and
    state-reset is correct.
    """
    import subprocess as _sp
    pythonpath = str(worktree / "src")
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        pythonpath = f"{pythonpath}{os.pathsep}{existing}"
    env = {**os.environ, "PYTHONPATH": pythonpath}

    # rm if exists (idempotent — non-zero on missing is fine)
    _sp.run(
        ["reyn", "agent", "rm", name, "--yes"],
        cwd=str(worktree), env=env, capture_output=True, text=True,
    )
    # Also wipe agent's events dir (rm only handles the agent profile/state)
    import shutil as _shutil
    events_dir = worktree / ".reyn" / "events" / "agents" / name
    if events_dir.exists():
        _shutil.rmtree(events_dir, ignore_errors=True)

    # create fresh
    result = _sp.run(
        ["reyn", "agent", "new", name],
        cwd=str(worktree), env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"[driver] warning: reyn agent new {name!r} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            flush=True,
        )


# ── One-shot runner ───────────────────────────────────────────────────────────


def _run_one_shot(
    *,
    scenario: dict,
    condition: str,
    shot: int,
    port: int,
    reyn_dot: Path,
    agent: str = "default",
    http_timeout: float = 360.0,  # > spike-side A2A timeout (300s)
    llm_call_cap: int = 30,
) -> dict:
    """POST scenario prompt; collect response + events. Returns partial record."""
    user_prompt = scenario["user_prompt"]
    endpoint = f"http://localhost:{port}/a2a/agents/{agent}"
    message_id = uuid.uuid4().hex

    since_ts = time.time()
    t0 = time.time()
    http_status, body, net_err = post_json(endpoint, build_message_send(user_prompt, message_id), http_timeout)
    elapsed = round(time.time() - t0, 2)

    if net_err == "timeout":
        status, narration = "timeout", ""
    elif net_err is not None:
        status, narration = f"http_error:{net_err}", ""
    elif body is None:
        status, narration = "http_error:no_body", ""
    else:
        reply, rpc_err = extract_reply(body)
        if rpc_err is not None:
            status, narration = f"rpc_error:{rpc_err}", ""
        else:
            narration = reply or ""
            if not narration.strip():
                status = "empty_stop"
            elif http_status >= 400:
                status = f"http_{http_status}"
            else:
                status = "ok"

    time.sleep(0.5)  # let events flush to disk
    events = read_events_since(reyn_dot / "events", agent, since_ts)
    calls = count_llm_calls(events)
    flash_reqs = count_flash_requests(events)

    if calls > llm_call_cap:
        status = "cap_exceeded"
        print(
            f"  [cap] {scenario['id']}/{condition}/shot{shot}: "
            f"{calls} calls > cap {llm_call_cap} — cap_exceeded",
            flush=True,
        )

    return {
        "status": status,
        "calls": calls,
        "flash_requests": flash_reqs,
        "narration_text": narration,
        "elapsed_s": elapsed,
        "_events": events,   # popped before writing to JSONL
    }


# ── Judge dispatch ────────────────────────────────────────────────────────────


def _judge(
    *, phase: str, narration: str, scenario: dict, smoke_test: bool,
    events: list[dict],
) -> dict | None:
    if smoke_test or not narration.strip():
        return None
    try:
        from spike_judge import heuristic_grade, judge_narration  # type: ignore[import]
    except ImportError as exc:
        print(f"  [judge] import failed: {exc}", file=sys.stderr)
        return None
    final_output = extract_final_output(events)
    if not final_output:
        print("  [judge] warn: no final_output found in events — field_extraction will be low", file=sys.stderr)
    try:
        if phase == "primary":
            return judge_narration(
                final_output=final_output,
                narration=narration,
                judge_focus=scenario.get("judge_focus") or [],
            )
        return heuristic_grade(final_output=final_output, narration=narration)
    except Exception as exc:
        print(f"  [judge] error: {exc}", file=sys.stderr)
        return None


# ── Blocked record factory ────────────────────────────────────────────────────


def _blocked(*, run_id: str, scenario: dict, condition: str, shot: int,
              branch: str, model_class: str, reason: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": run_id, "scenario": scenario["id"], "condition": condition,
        "shot": shot, "branch": branch, "model_class": model_class,
        "started_at": now, "ended_at": now,
        "status": f"blocked:{reason}",
        "calls": 0, "flash_requests": 0, "narration_text": "",
        "events_path": "", "judge_score": None,
    }


# ── Main orchestration ────────────────────────────────────────────────────────


def run_spike(
    *,
    scenarios: list[dict],
    spike_branch: str,
    conditions: list[str],
    n_shots: int,
    phase: str,
    out_dir: Path,
    project_root: Path,
    smoke_test: bool = False,
    http_timeout: float = 360.0,  # > spike-side A2A timeout (300s)
    llm_call_cap: int = 30,
) -> list[dict]:
    """Drive the full spike matrix. Returns list of completed run records."""

    # Fill experimental branch
    meta = {k: dict(v) for k, v in _CONDITION_META.items()}
    for cond in ("weak-experimental", "strong-experimental"):
        meta[cond]["branch"] = spike_branch

    out_dir.mkdir(parents=True, exist_ok=True)
    completed = load_completed_runs(out_dir)
    rpd = load_rpd_state(out_dir)
    all_records: list[dict] = []

    scenarios_to_run = scenarios[:1] if smoke_test else scenarios
    shot_range = range(1, 2 if smoke_test else n_shots + 1)

    # Group by (branch, port) — each worktree spun once
    branch_groups: dict[tuple[str, int], list[str]] = {}
    for cond in conditions:
        key = (meta[cond]["branch"], meta[cond]["port"])
        branch_groups.setdefault(key, []).append(cond)

    for (branch, port), branch_conds in branch_groups.items():
        wt = worktree_path(branch)
        print(f"\n[driver] == branch={branch!r} port={port} ==", flush=True)

        try:
            wt = ensure_worktree(project_root, branch, wt)
        except RuntimeError as exc:
            # Mark all runs blocked
            for cond in branch_conds:
                for sc in scenarios_to_run:
                    for shot in shot_range:
                        run_id = f"{sc['id']}/{cond}/shot{shot}"
                        if run_id not in completed:
                            rec = _blocked(run_id=run_id, scenario=sc, condition=cond,
                                           shot=shot, branch=branch,
                                           model_class=meta[cond]["model_class"],
                                           reason=str(exc))
                            append_run(out_dir, rec)
                            all_records.append(rec)
            continue

        reyn_dot = wt / ".reyn"

        for cond in branch_conds:
            cm = meta[cond]
            model_class = cm["model_class"]
            is_flash = cm["is_flash"]

            print(f"\n[driver] -- condition={cond!r} model={model_class!r} --", flush=True)

            # RPD pre-flight
            if is_flash and rpd.get("total_flash_requests", 0) + RPD_ESTIMATED_PER_RUN > RPD_HARD_CAP:
                print(
                    f"\n[RPD] Budget exceeded: {rpd['total_flash_requests']} flash requests "
                    f"+ est {RPD_ESTIMATED_PER_RUN} > {RPD_HARD_CAP}. "
                    "Resume tomorrow (delete rpd_state.json to reset).",
                    flush=True,
                )
                sys.exit(0)

            # Inject reyn.local.yaml (api_base + model class). All
            # conditions need api_base pointing at the LiteLLM proxy
            # — the committed reyn.yaml on the branch does not carry
            # this.
            #
            # SAFETY: skip injection when wt == project_root (= operator's
            # main checkout). Their existing reyn.local.yaml is the source
            # of truth; the driver must not clobber it.
            if wt.resolve() != project_root.resolve():
                write_model_override(wt, model_class, STRONG_MODEL_STRING)

            # Pre-create all unique agents BEFORE starting the server, so
            # the AgentRegistry sees them on disk at boot. Creating after
            # boot has shown a race where the runtime check sometimes
            # surfaces 500 instead of finding the freshly-created agent
            # (root cause unidentified; pre-creation sidesteps it).
            agent_names = []
            for sc in scenarios_to_run:
                for shot in shot_range:
                    sc_idx = sc["id"].split("-")[0].lstrip("narr") or sc["id"][:6]
                    cond_short = {
                        "weak-baseline": "wb",
                        "weak-experimental": "we",
                        "strong-experimental": "se",
                    }.get(cond, cond[:6])
                    agent_names.append(f"spike-s{sc_idx}-{cond_short}-sh{shot}")
            for name in agent_names:
                _ensure_agent(worktree=wt, name=name)

            # Start server
            print(f"[driver] starting reyn web --port {port} in {wt}", flush=True)
            server = start_web_server(wt, port, {})
            if not wait_for_server(port, timeout_s=30.0):
                print(f"[driver] server health check failed on port {port}", file=sys.stderr, flush=True)
                stop_web_server(server)
                for sc in scenarios_to_run:
                    for shot in shot_range:
                        run_id = f"{sc['id']}/{cond}/shot{shot}"
                        if run_id not in completed:
                            rec = _blocked(run_id=run_id, scenario=sc, condition=cond,
                                           shot=shot, branch=branch, model_class=model_class,
                                           reason="server_health_check_failed")
                            append_run(out_dir, rec)
                            all_records.append(rec)
                continue

            print(f"[driver] server healthy on port {port}", flush=True)

            for sc in scenarios_to_run:
                for shot in shot_range:
                    run_id = f"{sc['id']}/{cond}/shot{shot}"
                    if run_id in completed:
                        print(f"  [skip] {run_id} (already completed)", flush=True)
                        continue

                    # Per-run RPD check
                    if is_flash and rpd.get("total_flash_requests", 0) + RPD_ESTIMATED_PER_RUN > RPD_HARD_CAP:
                        print("\n[RPD] Budget reached mid-matrix. Stopping.", flush=True)
                        stop_web_server(server)
                        sys.exit(0)

                    started_at = datetime.now(timezone.utc).isoformat()
                    print(f"  [run] {run_id} ...", end="", flush=True)

                    # Per-(scenario, condition, shot) unique agent name —
                    # already pre-created above before server start.
                    cond_short = {
                        "weak-baseline": "wb",
                        "weak-experimental": "we",
                        "strong-experimental": "se",
                    }.get(cond, cond[:6])
                    sc_idx = sc["id"].split("-")[0].lstrip("narr") or sc["id"][:6]
                    agent_name = f"spike-s{sc_idx}-{cond_short}-sh{shot}"
                    result = _run_one_shot(
                        scenario=sc, condition=cond, shot=shot, port=port,
                        reyn_dot=reyn_dot, agent=agent_name,
                        http_timeout=http_timeout,
                        llm_call_cap=llm_call_cap,
                    )
                    # Debug: dump server log on http_error so the operator
                    # can see what crashed on the server side.
                    if result["status"].startswith("http_error"):
                        log_path = getattr(server, "_spike_log_path", None)
                        if log_path is not None:
                            try:
                                content = Path(log_path).read_text(errors="replace")
                                tail = "\n".join(content.splitlines()[-50:])
                                print(
                                    f"  [debug] server log {log_path} (last 50 lines):\n----\n{tail}\n----",
                                    flush=True,
                                )
                            except Exception as exc:
                                print(f"  [debug] could not read {log_path}: {exc}", flush=True)
                    ended_at = datetime.now(timezone.utc).isoformat()
                    print(f" {result['status']} ({result['calls']} calls, {result['elapsed_s']}s)", flush=True)

                    events = result.pop("_events", [])
                    events_path = save_run_events(out_dir, run_id, events)
                    judge_score = _judge(phase=phase, narration=result["narration_text"],
                                         scenario=sc, smoke_test=smoke_test,
                                         events=events)

                    rec: dict[str, Any] = {
                        "run_id": run_id,
                        "scenario": sc["id"],
                        "condition": cond,
                        "shot": shot,
                        "branch": branch,
                        "model_class": model_class,
                        "started_at": started_at,
                        "ended_at": ended_at,
                        **result,
                        "events_path": events_path,
                        "judge_score": judge_score,
                    }
                    append_run(out_dir, rec)
                    completed.add(run_id)
                    all_records.append(rec)

                    # RPD accounting
                    if is_flash and result.get("flash_requests", 0) > 0:
                        rpd["total_flash_requests"] = (
                            rpd.get("total_flash_requests", 0) + result["flash_requests"]
                        )
                        save_rpd_state(out_dir, rpd)
                        print(f"  [RPD] flash total: {rpd['total_flash_requests']}/{RPD_HARD_CAP}", flush=True)

            print(f"[driver] stopping server for condition={cond!r}", flush=True)
            stop_web_server(server)
            time.sleep(1.0)

        # SAFETY: do not clobber operator's reyn.local.yaml.
        if wt.resolve() != project_root.resolve():
            remove_model_override(wt)

    return all_records


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dogfood_g4_spike.py",
        description="G4 spike driver — FP-0011 narration quality across 3 conditions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--scenarios", required=True, metavar="PATH",
                   help="Path to scenarios YAML (dogfood/scenarios/fp_0011_narration.yaml).")
    p.add_argument("--branch", required=True, metavar="BRANCH",
                   help="Spike branch (e.g. claude/fp-0011-narrator-removal-spike).")
    p.add_argument("--phase", default="primary", choices=["primary", "extended"],
                   help="primary=N=3+LLM judge; extended=N=10+heuristic.")
    p.add_argument("--smoke-test", action="store_true", dest="smoke_test",
                   help="Smoke test: 1 scenario × 1 shot × all conditions (~3 runs).")
    p.add_argument("--resume", action="store_true",
                   help="(no-op) Driver always resumes. Delete runs.jsonl to restart fresh.")
    p.add_argument("--out", default="spike_results/fp_0011", metavar="DIR",
                   help="Output directory (default: spike_results/fp_0011/).")
    p.add_argument("--conditions", nargs="+",
                   default=["weak-baseline", "weak-experimental", "strong-experimental"],
                   choices=["weak-baseline", "weak-experimental", "strong-experimental"],
                   help="Conditions to run (default: all 3).")
    p.add_argument("--http-timeout", type=float, default=360.0, metavar="SEC",
                   help="Per-run HTTP timeout in seconds (default: 120).")
    p.add_argument("--llm-call-cap", type=int, default=30, metavar="N",
                   help="Max LLM calls per run before cap_exceeded (default: 30).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    out_dir = Path(args.out)

    n_shots = 1 if args.smoke_test else (3 if args.phase == "primary" else 10)

    print(f"[driver] G4 spike — phase={args.phase!r} shots={n_shots} smoke={args.smoke_test}", flush=True)
    print(f"[driver] spike branch: {args.branch!r}", flush=True)
    print(f"[driver] output: {out_dir}", flush=True)

    scenarios = _load_scenarios(Path(args.scenarios))
    print(f"[driver] {len(scenarios)} scenarios from {args.scenarios}", flush=True)

    records = run_spike(
        scenarios=scenarios,
        spike_branch=args.branch,
        conditions=args.conditions,
        n_shots=n_shots,
        phase=args.phase,
        out_dir=out_dir,
        project_root=project_root,
        smoke_test=args.smoke_test,
        http_timeout=args.http_timeout,
        llm_call_cap=args.llm_call_cap,
    )

    summary = compute_summary(records, phase=args.phase)
    if args.smoke_test:
        out_path = out_dir / "smoke.json"
        out_path.write_text(
            json.dumps({"runs": records, "summary": summary},
                       indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"\n[driver] smoke results -> {out_path}", flush=True)
    else:
        out_path = out_dir / f"summary_{args.phase}.json"
        out_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"\n[driver] summary -> {out_path}", flush=True)

    # Human-readable printout
    print(f"\n{'='*60}", flush=True)
    print(f" G4 spike phase={args.phase}  runs={summary['total_runs']}  "
          f"flash_requests={summary['total_flash_requests']}", flush=True)
    for cond, st in summary["per_condition"].items():
        if not st.get("n"):
            continue
        print(
            f"  {cond:<25} n={st['n']} fe={st['mean_field_extraction']} "
            f"ut={st['mean_utility']} cap={st['cap_exceeded_count']} "
            f"empty={st['empty_stop_count']}",
            flush=True,
        )
    print(f"{'='*60}\n", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
