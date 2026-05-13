"""FP-0011 + FP-0012 N≥10 retest driver — main HEAD only, 2-tier matrix.

Differs from `dogfood_g4_spike.py`:
  - **Main HEAD only** — no spike branch, no main-vs-spike comparison.
    The features under test (= FP-0011 anti-optimism rule + FP-0012 async
    invoke_skill + spawn-ack/completion narration) are LANDED on main as
    of commit `3aa5d9f`. The retest measures behaviour on production code.
  - **2-tier matrix** — `weak` (gemini-2.5-flash-lite) and `strong`
    (gemini-2.5-flash, thinking on). Per-scenario `tiers:` filter selects
    which tiers each scenario runs against.
  - **N=10 default** for primary phase, per memory
    `feedback_pre_conclusion_observation_checklist.md` Q5 ("N/N or 100%
    claims must be from direct inspection of all N, not extrapolation").
  - **Scenario-driven dimensions** — each scenario's `dimensions:` field
    lists which judge dimensions (D1..D5) apply, so the per-shot judge
    invocations are scoped.

Reuses `spike_lib/` helpers verbatim (= worktree, http, events, state),
plus `spike_judge.py` for narration grading.

Usage:
    python scripts/dogfood_fp_retest.py \\
      --scenarios dogfood/scenarios/fp_0011_0012_retest.yaml \\
      [--smoke-test] \\
      [--n-shots 10] \\
      [--tiers weak strong]

Idempotent resume: completed runs are skipped via runs.jsonl in --out dir.
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

# Add scripts/ to path so spike_lib + spike_judge are importable.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from spike_lib.events import (  # noqa: E402
    count_flash_requests,
    count_llm_calls,
    extract_final_output,
    read_events_since,
    save_run_events,
)
from spike_lib.http import build_message_send, extract_reply, post_json  # noqa: E402
from spike_lib.state import (  # noqa: E402
    RPD_ESTIMATED_PER_RUN,
    RPD_HARD_CAP,
    append_run,
    load_completed_runs,
    load_rpd_state,
    save_rpd_state,
)
from spike_lib.worktree import (  # noqa: E402
    ensure_worktree,
    remove_model_override,
    start_web_server,
    stop_web_server,
    wait_for_server,
    worktree_path,
    write_model_override,
)

# ── Constants ────────────────────────────────────────────────────────────────


# Tier metadata: model_class → resolves via reyn.local.yaml override.
_TIER_META: dict[str, dict] = {
    "weak": {
        "model_class": "standard",
        "port": 8083,           # distinct from FP-0011 spike (8081/8082)
        "is_flash": False,      # flash-lite has separate quota
    },
    "strong": {
        "model_class": "strong",
        "port": 8084,           # distinct port — sequential server starts
        "is_flash": True,
    },
}

STRONG_MODEL_STRING = "openai/gemini-2.5-flash"


def _load_yaml(path: Path) -> dict:
    import yaml
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_scenarios(path: Path) -> tuple[list[dict], dict]:
    if not path.exists():
        print(f"[error] scenarios file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = _load_yaml(path)
    except Exception as exc:
        print(f"[error] could not parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    metadata = data.get("metadata") or {}
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        print("[error] 'scenarios' must be a non-empty list", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(scenarios):
        for field in ("id", "user_prompt"):
            if field not in s:
                print(
                    f"[error] scenario[{i}] missing required field {field!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
    enabled_scenarios = [s for s in scenarios if s.get("enabled", True)]
    skipped = [s["id"] for s in scenarios if not s.get("enabled", True)]
    if skipped:
        print(f"[driver] skipping disabled scenarios: {skipped}", flush=True)
    if not enabled_scenarios:
        print("[error] all scenarios disabled — nothing to run", file=sys.stderr)
        sys.exit(1)
    return enabled_scenarios, metadata


def _filter_tier(scenarios: list[dict], tier: str) -> list[dict]:
    """Per-scenario `tiers:` filter — defaults to both."""
    out: list[dict] = []
    for s in scenarios:
        tiers = s.get("tiers", ["weak", "strong"])
        if tier in tiers:
            out.append(s)
    return out


# ── Agent provisioning (= reused from dogfood_g4_spike.py Bug 2 fix) ─────────


def _ensure_agent(*, worktree: Path, name: str) -> None:
    import shutil as _shutil
    import subprocess as _sp

    pythonpath = str(worktree / "src")
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        pythonpath = f"{pythonpath}{os.pathsep}{existing}"
    env = {**os.environ, "PYTHONPATH": pythonpath}

    _sp.run(
        ["reyn", "agent", "rm", name, "--yes"],
        cwd=str(worktree), env=env, capture_output=True, text=True,
    )
    events_dir = worktree / ".reyn" / "events" / "agents" / name
    if events_dir.exists():
        _shutil.rmtree(events_dir, ignore_errors=True)
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


# ── One-shot runner ──────────────────────────────────────────────────────────


def _run_one_shot(
    *,
    scenario: dict,
    tier: str,
    shot: int,
    port: int,
    reyn_dot: Path,
    agent: str,
    http_timeout: float = 360.0,
    llm_call_cap: int = 30,
) -> dict:
    user_prompt = scenario["user_prompt"]
    endpoint = f"http://localhost:{port}/a2a/agents/{agent}"
    message_id = uuid.uuid4().hex

    since_ts = time.time()
    t0 = time.time()
    http_status, body, net_err = post_json(
        endpoint, build_message_send(user_prompt, message_id), http_timeout,
    )
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

    time.sleep(0.5)
    events = read_events_since(reyn_dot / "events", agent, since_ts)
    calls = count_llm_calls(events)
    flash_reqs = count_flash_requests(events)

    if calls > llm_call_cap:
        status = "cap_exceeded"
        print(
            f"  [cap] {scenario['id']}/{tier}/shot{shot}: "
            f"{calls} calls > cap {llm_call_cap}",
            flush=True,
        )

    return {
        "status": status,
        "calls": calls,
        "flash_requests": flash_reqs,
        "narration_text": narration,
        "elapsed_s": elapsed,
        "_events": events,
    }


# ── Judge dispatch ───────────────────────────────────────────────────────────


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


def _blocked(*, run_id: str, scenario: dict, tier: str, shot: int,
             reason: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": run_id, "scenario": scenario["id"], "tier": tier,
        "condition": tier,  # back-compat with FP-0011 records
        "shot": shot, "branch": "main",
        "model_class": _TIER_META[tier]["model_class"],
        "started_at": now, "ended_at": now,
        "status": f"blocked:{reason}",
        "calls": 0, "flash_requests": 0, "narration_text": "",
        "events_path": "", "judge_score": None,
    }


# ── Main orchestration ──────────────────────────────────────────────────────


def run_retest(
    *,
    scenarios: list[dict],
    tiers: list[str],
    n_shots: int,
    phase: str,
    out_dir: Path,
    project_root: Path,
    smoke_test: bool = False,
    http_timeout: float = 360.0,
    llm_call_cap: int = 30,
) -> list[dict]:
    """Drive the retest matrix on a single main-HEAD worktree."""

    out_dir.mkdir(parents=True, exist_ok=True)
    completed = load_completed_runs(out_dir)
    rpd = load_rpd_state(out_dir)
    all_records: list[dict] = []

    shot_range = range(1, 2 if smoke_test else n_shots + 1)

    # Single worktree from main HEAD. Use a stable branch name token so
    # `worktree_path` produces a predictable /tmp path.
    branch = "main"
    wt = worktree_path("fp-retest-main")
    print(f"\n[driver] == main retest worktree {wt} ==", flush=True)

    try:
        # ensure_worktree handles the operator-on-branch case via --detach.
        wt = ensure_worktree(project_root, branch, wt)
    except RuntimeError as exc:
        # Mark every run blocked.
        for sc in (scenarios[:1] if smoke_test else scenarios):
            for tier in tiers:
                if sc not in _filter_tier([sc], tier):
                    continue
                for shot in shot_range:
                    run_id = f"{sc['id']}/{tier}/shot{shot}"
                    if run_id not in completed:
                        rec = _blocked(
                            run_id=run_id, scenario=sc, tier=tier,
                            shot=shot, reason=str(exc),
                        )
                        append_run(out_dir, rec)
                        all_records.append(rec)
        return all_records

    reyn_dot = wt / ".reyn"

    for tier in tiers:
        tm = _TIER_META[tier]
        port = tm["port"]
        model_class = tm["model_class"]
        is_flash = tm["is_flash"]

        # Filter scenarios by per-scenario tiers spec.
        tier_scenarios_full = _filter_tier(scenarios, tier)
        tier_scenarios = tier_scenarios_full[:1] if smoke_test else tier_scenarios_full
        if not tier_scenarios:
            print(f"[driver] tier={tier!r}: no scenarios apply, skipping", flush=True)
            continue

        print(f"\n[driver] -- tier={tier!r} model={model_class!r} port={port} --", flush=True)

        if is_flash and rpd.get("total_flash_requests", 0) + RPD_ESTIMATED_PER_RUN > RPD_HARD_CAP:
            print(
                f"\n[RPD] Budget exceeded: {rpd['total_flash_requests']} + est "
                f"{RPD_ESTIMATED_PER_RUN} > {RPD_HARD_CAP}. "
                "Resume tomorrow (delete rpd_state.json to reset).",
                flush=True,
            )
            sys.exit(0)

        if wt.resolve() != project_root.resolve():
            write_model_override(wt, model_class, STRONG_MODEL_STRING)

        # Pre-create agents (Bug 2 fix from FP-0011 spike).
        # Reyn agent name max 32 chars / [a-z0-9_-] starting with [a-z0-9].
        # Use scenario index (= position in tier_scenarios) instead of full id
        # so the name fits the limit.
        agent_names = []
        for sc_idx, sc in enumerate(tier_scenarios):
            for shot in shot_range:
                agent_names.append(
                    f"retest-{tier[:1]}-sc{sc_idx:02d}-sh{shot:02d}"
                )
        for name in agent_names:
            _ensure_agent(worktree=wt, name=name)

        print(f"[driver] starting reyn web --port {port} in {wt}", flush=True)
        server = start_web_server(wt, port, {})
        if not wait_for_server(port, timeout_s=30.0):
            print(
                f"[driver] server health check failed on port {port}",
                file=sys.stderr, flush=True,
            )
            stop_web_server(server)
            for sc in tier_scenarios:
                for shot in shot_range:
                    run_id = f"{sc['id']}/{tier}/shot{shot}"
                    if run_id not in completed:
                        rec = _blocked(
                            run_id=run_id, scenario=sc, tier=tier,
                            shot=shot, reason="server_health_check_failed",
                        )
                        append_run(out_dir, rec)
                        all_records.append(rec)
            continue

        print(f"[driver] server healthy on port {port}", flush=True)

        for sc in tier_scenarios:
            for shot in shot_range:
                run_id = f"{sc['id']}/{tier}/shot{shot}"
                if run_id in completed:
                    print(f"  [skip] {run_id} (already completed)", flush=True)
                    continue

                if is_flash and rpd.get("total_flash_requests", 0) + RPD_ESTIMATED_PER_RUN > RPD_HARD_CAP:
                    print("\n[RPD] Budget reached mid-matrix. Stopping.", flush=True)
                    stop_web_server(server)
                    sys.exit(0)

                started_at = datetime.now(timezone.utc).isoformat()
                print(f"  [run] {run_id} ...", end="", flush=True)

                # Same agent-name scheme as pre-creation block above.
                sc_idx_pos = tier_scenarios.index(sc)
                agent_name = (
                    f"retest-{tier[:1]}-sc{sc_idx_pos:02d}-sh{shot:02d}"
                )
                result = _run_one_shot(
                    scenario=sc, tier=tier, shot=shot, port=port,
                    reyn_dot=reyn_dot, agent=agent_name,
                    http_timeout=http_timeout, llm_call_cap=llm_call_cap,
                )
                if result["status"].startswith("http_error"):
                    log_path = getattr(server, "_spike_log_path", None)
                    if log_path is not None:
                        try:
                            content = Path(log_path).read_text(errors="replace")
                            tail = "\n".join(content.splitlines()[-50:])
                            print(
                                f"  [debug] server log {log_path} (last 50 lines):\n"
                                f"----\n{tail}\n----",
                                flush=True,
                            )
                        except Exception as exc:
                            print(f"  [debug] could not read {log_path}: {exc}", flush=True)
                ended_at = datetime.now(timezone.utc).isoformat()
                print(
                    f" {result['status']} ({result['calls']} calls, "
                    f"{result['elapsed_s']}s)",
                    flush=True,
                )

                events = result.pop("_events", [])
                events_path = save_run_events(out_dir, run_id, events)
                judge_score = _judge(
                    phase=phase, narration=result["narration_text"],
                    scenario=sc, smoke_test=smoke_test, events=events,
                )

                rec: dict[str, Any] = {
                    "run_id": run_id,
                    "scenario": sc["id"],
                    "tier": tier,
                    "condition": tier,  # back-compat with FP-0011 records
                    "shot": shot,
                    "branch": "main",
                    "model_class": model_class,
                    "dimensions": sc.get("dimensions") or [],
                    "expected_skill": sc.get("expected_skill"),
                    "expected_status": sc.get("expected_status"),
                    "judge_focus": sc.get("judge_focus") or [],
                    "started_at": started_at,
                    "ended_at": ended_at,
                    **result,
                    "events_path": events_path,
                    "judge_score": judge_score,
                }
                append_run(out_dir, rec)
                completed.add(run_id)
                all_records.append(rec)

                if is_flash and result.get("flash_requests", 0) > 0:
                    rpd["total_flash_requests"] = (
                        rpd.get("total_flash_requests", 0)
                        + result["flash_requests"]
                    )
                    save_rpd_state(out_dir, rpd)
                    print(
                        f"  [RPD] flash total: {rpd['total_flash_requests']}/"
                        f"{RPD_HARD_CAP}",
                        flush=True,
                    )

        print(f"[driver] stopping server for tier={tier!r}", flush=True)
        stop_web_server(server)
        time.sleep(1.0)

    if wt.resolve() != project_root.resolve():
        remove_model_override(wt)

    return all_records


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dogfood_fp_retest.py",
        description=(
            "FP-0011 + FP-0012 N≥10 retest driver. Main HEAD only, "
            "2-tier matrix (weak / strong)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--scenarios", required=True, metavar="PATH",
        help="Path to scenarios YAML "
             "(default: dogfood/scenarios/fp_0011_0012_retest.yaml).",
    )
    p.add_argument(
        "--phase", default="primary", choices=["primary", "extended"],
        help="primary=LLM judge; extended=heuristic only.",
    )
    p.add_argument(
        "--n-shots", type=int, default=10, metavar="N",
        help="Shots per (scenario, tier). Default 10 per "
             "feedback_pre_conclusion_observation_checklist discipline.",
    )
    p.add_argument(
        "--smoke-test", action="store_true", dest="smoke_test",
        help="Smoke: 1 scenario × 1 shot per tier (~2 runs).",
    )
    p.add_argument(
        "--out", default="spike_results/fp_0011_0012_retest", metavar="DIR",
        help="Output directory.",
    )
    p.add_argument(
        "--tiers", nargs="+", default=["weak", "strong"],
        choices=["weak", "strong"],
        help="Which tiers to run (default: both).",
    )
    p.add_argument(
        "--http-timeout", type=float, default=360.0, metavar="SEC",
        help="Per-run HTTP timeout in seconds (default: 360).",
    )
    p.add_argument(
        "--llm-call-cap", type=int, default=30, metavar="N",
        help="Max LLM calls per run before cap_exceeded (default: 30).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    scenarios_path = (project_root / args.scenarios).resolve()
    out_dir = (project_root / args.out).resolve()

    n_shots = 1 if args.smoke_test else args.n_shots
    print(
        f"[driver] FP retest — phase={args.phase!r} "
        f"shots={n_shots} smoke={args.smoke_test} tiers={args.tiers}",
        flush=True,
    )

    scenarios, metadata = _load_scenarios(scenarios_path)
    print(
        f"[driver] loaded {len(scenarios)} scenarios from "
        f"{scenarios_path.relative_to(project_root)}",
        flush=True,
    )

    records = run_retest(
        scenarios=scenarios,
        tiers=list(args.tiers),
        n_shots=n_shots,
        phase=args.phase,
        out_dir=out_dir,
        project_root=project_root,
        smoke_test=args.smoke_test,
        http_timeout=args.http_timeout,
        llm_call_cap=args.llm_call_cap,
    )

    if args.smoke_test:
        out_path = out_dir / "smoke.json"
        out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        print(f"\n[driver] smoke results -> {out_path}", flush=True)
    else:
        out_path = out_dir / "primary.json"
        out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        print(f"\n[driver] primary results -> {out_path}", flush=True)
        # Also dump a per-tier rollup for quick visual inspection.
        rollup = out_dir / "rollup.json"
        per_tier: dict[str, dict] = {}
        for r in records:
            t = r.get("tier", "?")
            t_summary = per_tier.setdefault(t, {"n": 0, "ok": 0, "errors": 0,
                                                "by_status": {}})
            t_summary["n"] += 1
            st = r.get("status") or "?"
            if st == "ok":
                t_summary["ok"] += 1
            else:
                t_summary["errors"] += 1
            t_summary["by_status"][st] = t_summary["by_status"].get(st, 0) + 1
        rollup.write_text(json.dumps(per_tier, indent=2, ensure_ascii=False))
        print(f"[driver] rollup -> {rollup}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
