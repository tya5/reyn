"""dogfood_trace.py — consolidate batch observation greps into one tool.

Usage:
    python scripts/dogfood_trace.py [--root .reyn] [--mode summary|full|chain|cost]
                                    [--filter <event_kind>]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

IMPORTANT_TOOLS = {
    "invoke_skill", "describe_skill", "list_skills",
    "read_local_files", "delegate_to_agent", "remember_shared",
    "run_skill", "file", "ask_user",
}


def _load_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def _collect_event_files(root: Path) -> list[Path]:
    events_dir = root / "events"
    return sorted(events_dir.rglob("*.jsonl")) if events_dir.exists() else []


def _ts_offset(base: str | None, ts: str | None) -> str:
    if not base or not ts:
        return ts or "?"
    try:
        from datetime import datetime, timezone
        def _p(s: str) -> datetime:
            return datetime.strptime(s.split("+")[0].split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return f"T+{(_p(ts) - _p(base)).total_seconds():.1f}s"
    except Exception:
        return ts[:19] if ts else "?"


def _exc(args: Any, n: int = 50) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s[:n] + ("..." if len(s) > n else "")


def _all_events(root: Path) -> list[dict]:
    evs = []
    for f in _collect_event_files(root):
        evs.extend(_load_jsonl(f))
    evs.sort(key=lambda e: e.get("timestamp", ""))
    return evs


def mode_cost(root: Path) -> None:
    entries: list[dict] = []
    for p in [root / "budget" / "ledger.jsonl", root / "state" / "budget_ledger.jsonl"]:
        entries.extend(_load_jsonl(p))
    if not entries:
        print("no cost ledger found")
        return
    total_usd, total_tokens = 0.0, 0
    per_model: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "tokens": 0, "calls": 0})
    for e in entries:
        usd, tokens, model = e.get("cost_usd", 0.0) or 0.0, e.get("tokens", 0) or 0, e.get("model", "unknown")
        total_usd += usd; total_tokens += tokens
        per_model[model]["usd"] += usd; per_model[model]["tokens"] += tokens; per_model[model]["calls"] += 1
    print(f"=== Cost Summary ===")
    print(f"  Total: ${total_usd:.6f}  |  {total_tokens:,} tokens  |  {len(entries)} calls\n  Per-model:")
    for model, m in sorted(per_model.items(), key=lambda x: -x[1]["usd"]):
        print(f"    {model}: ${m['usd']:.6f}  {m['tokens']:,} tokens  ({m['calls']} calls)")


def mode_full(root: Path, filter_kind: str | None) -> None:
    files = _collect_event_files(root)
    if not files:
        print("no events found"); return
    by_kind: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for f in files:
        for ev in _load_jsonl(f):
            by_kind[ev.get("type", "unknown")].append((f.name, ev))
    for kind in ([filter_kind] if filter_kind else sorted(by_kind)):
        if kind not in by_kind:
            continue
        evs = by_kind[kind]
        print(f"\n── {kind} ({len(evs)}) ──")
        for src, ev in evs:
            ts = ev.get("timestamp", "")[:19]
            line = json.dumps(ev.get("data", {}), ensure_ascii=False)
            print(f"  [{ts}] {src}  {line[:120] + ('...' if len(line) > 120 else '')}")


def mode_chain(root: Path) -> None:
    evs = _all_events(root)
    if not evs:
        print("no events found"); return
    base_ts = evs[0].get("timestamp")
    indent = 0
    print("=== Skill / Tool Chain ===")
    for ev in evs:
        kind, data, ts = ev.get("type", ""), ev.get("data", {}), ev.get("timestamp")
        pad = "  " * indent
        t = _ts_offset(base_ts, ts)
        if kind == "workflow_started":
            print(f"{pad}[{t}] workflow_started: {data.get('skill','?')}  run_id={data.get('run_id','')}")
            indent = min(indent + 1, 6)
        elif kind == "phase_started":
            print(f"{pad}[{t}] phase_started: {data.get('phase','?')}")
        elif kind == "tool_called":
            print(f"{pad}[{t}] tool: {data.get('tool','?')}({_exc(data.get('args',{}))})")
        elif kind == "run_skill_started":
            print(f"{pad}[{t}] run_skill_started: {data.get('skill','?')}")
            indent = min(indent + 1, 6)
        elif kind == "run_skill_completed":
            indent = max(indent - 1, 0); pad = "  " * indent
            print(f"{pad}[{t}] run_skill_completed: {data.get('skill','?')}  status={data.get('status','?')}")
        elif kind == "workflow_finished":
            indent = max(indent - 1, 0); pad = "  " * indent
            print(f"{pad}[{t}] workflow_finished  status={data.get('status','?')}")
        elif kind == "phase_completed":
            print(f"{pad}[{t}] phase_completed: {data.get('phase','?')}  decision={data.get('decision','?')}")
        elif kind in ("control_ir_failed", "phase_retry"):
            print(f"{pad}[{t}] {kind}: {json.dumps(data, ensure_ascii=False)[:80]}")


def mode_summary(root: Path) -> None:
    evs = _all_events(root)
    if not evs:
        print("no events found"); return

    workflows: list[dict] = []
    wf_map: dict[str, dict] = {}
    tool_calls, peer_failures, iv_dispatch, iv_resolve, agent_msgs = [], [], [], [], []

    for ev in evs:
        kind, data, ts = ev.get("type", ""), ev.get("data", {}), ev.get("timestamp", "")
        if kind == "workflow_started":
            wf = {"run_id": data.get("run_id",""), "skill": data.get("skill",""),
                  "entry_phase": data.get("entry_phase",""), "ts": ts, "status": "active", "phases": []}
            workflows.append(wf); wf_map[wf["run_id"]] = wf
        elif kind == "workflow_finished":
            run_id = data.get("run_id", "")
            if run_id in wf_map:
                wf_map[run_id]["status"] = data.get("status", "finished")
            else:
                for wf in reversed(workflows):
                    if wf["status"] == "active":
                        wf["status"] = data.get("status", "finished"); break
        elif kind == "phase_started":
            phase = data.get("phase", "")
            for wf in reversed(workflows):
                if wf["status"] == "active":
                    if phase not in wf["phases"]: wf["phases"].append(phase)
                    break
        elif kind == "tool_called" and data.get("tool") in IMPORTANT_TOOLS:
            tool_calls.append({"tool": data.get("tool"), "args": data.get("args", {}),
                                "caller": data.get("caller_id", data.get("caller_kind", "")), "ts": ts})
        elif kind == "peer_reply_failed_surfaced":
            peer_failures.append({"data": data, "kind": kind})
        elif kind == "chain_peer_discarded":
            peer_failures.append({"data": data, "kind": kind})
        elif kind == "intervention_dispatched":
            iv_dispatch.append(data)
        elif kind == "intervention_resolved":
            iv_resolve.append(data)
        elif kind == "agent_message_sent":
            agent_msgs.append(data)

    print("=" * 60)
    print("DOGFOOD TRACE SUMMARY")
    print("=" * 60)

    print(f"\n[Skill Chain]  ({len(workflows)} workflow(s))")
    for wf in workflows:
        phases_str = " -> ".join(wf["phases"]) or "(no phases recorded)"
        print(f"  [{wf['ts'][:19]}] {wf['skill']} (entry={wf['entry_phase']})  status={wf['status']}")
        print(f"    phases: {phases_str}")
        print(f"    run_id: {wf['run_id']}")

    print(f"\n[Tool Calls]  ({len(tool_calls)} important tool call(s))")
    for i, tc in enumerate(tool_calls, 1):
        print(f"  [{i:2d}] {tc['tool']}({_exc(tc['args'])})  caller={tc['caller']}")

    print(f"\n[Peer Failures / Chain Discards]  ({len(peer_failures)} event(s))")
    for pf in peer_failures:
        d = pf["data"]
        print(f"  {pf['kind']}: peer={d.get('peer', d.get('chain_id',''))}  reason={d.get('reason', d.get('error',''))}")

    print(f"\n[Interventions]  dispatch={len(iv_dispatch)}  resolve={len(iv_resolve)}")
    for d in iv_dispatch:
        print(f"  dispatch: {json.dumps(d, ensure_ascii=False)[:80]}")
    for d in iv_resolve:
        print(f"  resolve:  {json.dumps(d, ensure_ascii=False)[:80]}")

    print(f"\n[Agent Messages]  ({len(agent_msgs)} message(s))")
    for d in agent_msgs:
        src = d.get("from", d.get("agent", "?"))
        text = str(d.get("text", d.get("content", "")))
        print(f"  {src}: {text[:40]}")

    skill_runs_dir = root / "state" / "skill_runs"
    if skill_runs_dir.exists():
        runs = list(skill_runs_dir.iterdir())
        print(f"\n[Skill Run State]  {skill_runs_dir}  ({len(runs)} entr(ies))")
        for r in sorted(runs)[:10]:
            print(f"  {r.name}")
    else:
        print(f"\n[Skill Run State]  {skill_runs_dir} (not found)")

    print()
    mode_cost(root)


def main() -> None:
    parser = argparse.ArgumentParser(description="dogfood_trace — consolidated Reyn batch observation tool")
    parser.add_argument("--root", default=".reyn", help="Path to .reyn directory (default: .reyn)")
    parser.add_argument("--mode", choices=["summary", "full", "chain", "cost"], default="summary")
    parser.add_argument("--filter", dest="filter_kind", default=None, help="Filter by event kind (for --mode full)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"no events found (root not found: {root})")
        sys.exit(0)

    {"summary": mode_summary, "full": mode_full, "chain": mode_chain, "cost": mode_cost}[args.mode](
        *([root, args.filter_kind] if args.mode == "full" else [root])
    )


if __name__ == "__main__":
    main()
