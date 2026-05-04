"""dogfood_trace.py — consolidate batch observation greps into one tool.

Usage:
    python scripts/dogfood_trace.py [--root .reyn] [--mode summary|full|chain|cost]
                                    [--filter <event_kind>]

    # LLM payload trace modes (requires REYN_LLM_TRACE_DUMP to have been set during dogfood):
    python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-detail <request_id> --trace .reyn/llm_trace.jsonl [--full]
    python scripts/dogfood_trace.py --mode llm-tools-schema <request_id> --trace .reyn/llm_trace.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
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


# ---------------------------------------------------------------------------
# LLM payload trace modes
# ---------------------------------------------------------------------------

def _load_llm_trace(trace_path: Path) -> list[dict]:
    """Load JSONL records from an LLM trace file."""
    return _load_jsonl(trace_path)


def _pair_llm_records(records: list[dict]) -> list[tuple[dict, dict | None]]:
    """Pair request records with their response by request_id.

    Returns list of (request, response_or_None) in timestamp order.
    """
    requests: dict[str, dict] = {}
    responses: dict[str, dict] = {}
    order: list[str] = []

    for rec in records:
        rid = rec.get("request_id", "")
        kind = rec.get("kind", "")
        if kind == "request":
            requests[rid] = rec
            order.append(rid)
        elif kind == "response":
            responses[rid] = rec

    return [(requests[rid], responses.get(rid)) for rid in order if rid in requests]


def _rel_seconds(base_ts: str | None, ts: str | None) -> str:
    """Return relative seconds from base_ts as 'T+Xs' string."""
    if not base_ts or not ts:
        return ts[:19] if ts else "?"
    try:
        from datetime import timezone

        def _p(s: str) -> datetime:
            s_clean = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s_clean)
            except Exception:
                return datetime.strptime(s_clean[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

        delta = _p(ts) - _p(base_ts)
        return f"T+{delta.total_seconds():.1f}s"
    except Exception:
        return ts[:19] if ts else "?"


def mode_llm_payloads(trace_path: Path) -> None:
    """List all LLM request/response pairs from a trace file in time order."""
    if not trace_path.exists():
        print(f"trace file not found: {trace_path}")
        sys.exit(1)

    records = _load_llm_trace(trace_path)
    pairs = _pair_llm_records(records)

    if not pairs:
        print("no LLM request records found in trace file")
        return

    # Determine base timestamp from first request
    base_ts = pairs[0][0].get("timestamp") if pairs else None

    for req, resp in pairs:
        rid = req.get("request_id", "?")[:8]  # short id
        rid_full = req.get("request_id", "?")
        model = req.get("model", "?")
        caller = req.get("caller_hint", "unknown")
        msgs = req.get("messages", [])
        tools = req.get("tools")
        ts_req = req.get("timestamp")
        rel_req = _rel_seconds(base_ts, ts_req)

        tool_count = len(tools) if tools else 0
        print(
            f"[{rel_req}] request_id={rid_full}  model={model}  "
            f"caller={caller}  msgs={len(msgs)}  tools={tool_count}"
        )

        if resp is not None:
            ts_resp = resp.get("timestamp")
            rel_resp = _rel_seconds(base_ts, ts_resp)
            finish = resp.get("finish_reason", "?")
            tcs = resp.get("tool_calls", [])
            usage = resp.get("usage", {})
            tokens_in = (usage.get("prompt_tokens") or "?") if usage else "?"
            tokens_out = (usage.get("completion_tokens") or "?") if usage else "?"
            print(
                f"[{rel_resp}] response_id={rid_full}  finish={finish}  "
                f"tool_calls={len(tcs)}  tokens_in={tokens_in}  tokens_out={tokens_out}"
            )
        else:
            print(f"         response_id={rid_full}  (no response record)")


def _truncate_content(content: str | None, full: bool, head: int = 200, tail: int = 200) -> str:
    """Truncate content for display unless --full is set."""
    if content is None:
        return "(null)"
    if full or len(content) <= head + tail:
        return content
    return f"{content[:head]}\n... [{len(content) - head - tail} chars omitted] ...\n{content[-tail:]}"


def mode_llm_detail(trace_path: Path, request_id: str, full: bool = False) -> None:
    """Pretty-print full payload for a single request_id."""
    if not trace_path.exists():
        print(f"trace file not found: {trace_path}")
        sys.exit(1)

    records = _load_llm_trace(trace_path)
    req: dict | None = None
    resp: dict | None = None

    for rec in records:
        if rec.get("request_id") == request_id:
            if rec.get("kind") == "request":
                req = rec
            elif rec.get("kind") == "response":
                resp = rec

    if req is None:
        print(f"request_id not found: {request_id}")
        sys.exit(1)

    print(f"=== LLM Call Detail: {request_id} ===")
    print(f"  model:       {req.get('model', '?')}")
    print(f"  caller_hint: {req.get('caller_hint', 'unknown')}")
    print(f"  timestamp:   {req.get('timestamp', '?')}")

    sampling = req.get("sampling_params", {})
    if sampling:
        print(f"  sampling:    {json.dumps(sampling, ensure_ascii=False)}")

    tool_choice = req.get("tool_choice")
    if tool_choice is not None:
        print(f"  tool_choice: {tool_choice}")

    tools = req.get("tools")
    if tools:
        names = [t.get("function", {}).get("name", "?") for t in tools if isinstance(t, dict)]
        print(f"  tools ({len(tools)}): {', '.join(names)}" + ("  (use llm-tools-schema for full schema)" if not full else ""))
        if full:
            print("  --- tools schema ---")
            print(json.dumps(tools, indent=2, ensure_ascii=False))
            print("  --- end tools schema ---")
    else:
        print("  tools: (none)")

    print(f"\n  --- messages ({len(req.get('messages', []))}) ---")
    for i, msg in enumerate(req.get("messages", [])):
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, list):
            # Anthropic-style multi-block content
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    print(f"  [{i}] {role}: {_truncate_content(text, full)}")
        elif isinstance(content, str):
            print(f"  [{i}] {role}: {_truncate_content(content, full)}")
        else:
            print(f"  [{i}] {role}: (non-text content)")

    if resp is not None:
        print(f"\n  --- response ---")
        print(f"  timestamp:    {resp.get('timestamp', '?')}")
        print(f"  finish_reason: {resp.get('finish_reason', '?')}")
        usage = resp.get("usage", {})
        if usage:
            print(f"  usage:        prompt_tokens={usage.get('prompt_tokens','?')}  completion_tokens={usage.get('completion_tokens','?')}")
        content = resp.get("content")
        if content:
            print(f"  content: {_truncate_content(content, full)}")
        tool_calls = resp.get("tool_calls", [])
        if tool_calls:
            print(f"  tool_calls ({len(tool_calls)}):")
            for tc in tool_calls:
                fn = tc.get("function", {})
                print(f"    - {fn.get('name','?')}  args={fn.get('arguments','?')[:120]}")
    else:
        print("\n  (no response record found)")


def mode_llm_tools_schema(trace_path: Path, request_id: str) -> None:
    """Pretty-print the full tools schema for a single request_id."""
    if not trace_path.exists():
        print(f"trace file not found: {trace_path}")
        sys.exit(1)

    records = _load_llm_trace(trace_path)
    req: dict | None = None

    for rec in records:
        if rec.get("request_id") == request_id and rec.get("kind") == "request":
            req = rec
            break

    if req is None:
        print(f"request_id not found: {request_id}")
        sys.exit(1)

    tools = req.get("tools")
    if not tools:
        print(f"no tools in request {request_id}")
        return

    print(json.dumps(tools, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="dogfood_trace — consolidated Reyn batch observation tool")
    parser.add_argument("--root", default=".reyn", help="Path to .reyn directory (default: .reyn)")
    parser.add_argument("--mode", choices=["summary", "full", "chain", "cost", "llm-payloads", "llm-detail", "llm-tools-schema"], default="summary")
    parser.add_argument("--filter", dest="filter_kind", default=None, help="Filter by event kind (for --mode full)")
    parser.add_argument("--trace", default=None, help="Path to LLM trace JSONL file (for llm-* modes)")
    parser.add_argument("--full", action="store_true", default=False, help="Show full messages/tools (for llm-detail)")
    parser.add_argument("request_id", nargs="?", default=None, help="request_id for llm-detail / llm-tools-schema")
    args = parser.parse_args()

    # LLM trace modes
    if args.mode == "llm-payloads":
        trace = Path(args.trace) if args.trace else Path(".reyn/llm_trace.jsonl")
        mode_llm_payloads(trace)
        return

    if args.mode == "llm-detail":
        if not args.request_id:
            print("llm-detail requires a request_id argument")
            sys.exit(1)
        trace = Path(args.trace) if args.trace else Path(".reyn/llm_trace.jsonl")
        mode_llm_detail(trace, args.request_id, full=args.full)
        return

    if args.mode == "llm-tools-schema":
        if not args.request_id:
            print("llm-tools-schema requires a request_id argument")
            sys.exit(1)
        trace = Path(args.trace) if args.trace else Path(".reyn/llm_trace.jsonl")
        mode_llm_tools_schema(trace, args.request_id)
        return

    # Event-based modes
    root = Path(args.root)
    if not root.exists():
        print(f"no events found (root not found: {root})")
        sys.exit(0)

    {"summary": mode_summary, "full": mode_full, "chain": mode_chain, "cost": mode_cost}[args.mode](
        *([root, args.filter_kind] if args.mode == "full" else [root])
    )


if __name__ == "__main__":
    main()
