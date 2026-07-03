"""dogfood_trace.py — consolidate batch observation greps into one tool.

Usage:
    python scripts/dogfood_trace.py [--root .reyn] [--mode summary|full|chain|cost]
                                    [--filter <event_kind>]

    # LLM payload trace modes (requires REYN_LLM_TRACE_DUMP to have been set during dogfood):
    python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-detail <request_id> --trace .reyn/llm_trace.jsonl [--full]
    python scripts/dogfood_trace.py --mode llm-tools-schema <request_id> --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-context <request_id> --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-advertised-ops <request_id> --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-emitted-ops <request_id> --trace .reyn/llm_trace.jsonl [--root .reyn]

    # Multiple trace files (merged chronologically):
    python scripts/dogfood_trace.py --mode llm-payloads --trace a.jsonl --trace b.jsonl
    python scripts/dogfood_trace.py --mode llm-payloads --trace a.jsonl,b.jsonl
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
    print("=== Cost Summary ===")
    print(f"  Total: ${total_usd:.6f}  |  {total_tokens:,} tokens  |  {len(entries)} calls\n  Per-model:")
    for model, m in sorted(per_model.items(), key=lambda x: -x[1]["usd"]):
        print(f"    {model}: ${m['usd']:.6f}  {m['tokens']:,} tokens  ({m['calls']} calls)")

    # Plan-mode memo savings (ADR-0023 + ADR-0025) — count from events log.
    evs = _all_events(root)
    step_memo_hits = sum(1 for e in evs if e.get("type") == "plan_step_memoized")
    llm_memo_hits = sum(1 for e in evs if e.get("type") == "plan_step_llm_memoized")
    if step_memo_hits or llm_memo_hits:
        n_calls = len(entries)
        avg_usd = (total_usd / n_calls) if n_calls else 0.0
        avg_tokens = (total_tokens / n_calls) if n_calls else 0
        saved_usd = llm_memo_hits * avg_usd
        saved_tokens = llm_memo_hits * avg_tokens
        print("\nPlan-mode memo savings (ADR-0023 + ADR-0025):")
        print(f"  step-result memoizations:  {step_memo_hits} events  (= step memo replay)")
        print(f"  LLM-call memoizations:     {llm_memo_hits} events  (= sub-loop LLM memo replay)")
        print(f"  estimated saved cost:      ${saved_usd:.4f}   (= {llm_memo_hits} LLM-call hits × ${avg_usd:.5f} avg)")
        print(f"  estimated saved tokens:    ~{int(saved_tokens):,}    (= {llm_memo_hits} hits × {int(avg_tokens):,} avg per call)")


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


def _load_llm_trace_files(paths: list[str]) -> list[dict]:
    """Load JSONL records from multiple LLM trace files and merge in timestamp order.

    Each record gets a ``_source_file`` field set to the basename of the file
    it was loaded from.  Records are returned sorted by their ``timestamp``
    field so that cross-file chronological inspection is possible.
    """
    all_records: list[dict] = []
    for path_str in paths:
        p = Path(path_str)
        if not p.exists():
            print(f"trace file not found: {p}", file=sys.stderr)
            sys.exit(1)
        records = _load_llm_trace(p)
        source = p.name
        for rec in records:
            rec = dict(rec)
            rec["_source_file"] = source
            all_records.append(rec)
    all_records.sort(key=lambda r: r.get("timestamp", ""))
    return all_records


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


def mode_llm_payloads(records: list[dict], multi_file: bool = False) -> None:
    """List all LLM request/response pairs from merged records in time order.

    ``records`` must already be sorted by timestamp (as returned by
    ``_load_llm_trace_files``).  When ``multi_file`` is True a ``[file]``
    annotation is appended to each request line so the caller can tell which
    dump the record came from.
    """
    pairs = _pair_llm_records(records)

    if not pairs:
        print("no LLM request records found in trace file")
        return

    # Determine base timestamp from the first record in the merged list
    # (not just from the first request pair, so T+ is consistent across files)
    first_ts = records[0].get("timestamp") if records else None
    base_ts = first_ts or (pairs[0][0].get("timestamp") if pairs else None)

    for req, resp in pairs:
        rid_full = req.get("request_id", "?")
        model = req.get("model", "?")
        caller = req.get("caller_hint", "unknown")
        msgs = req.get("messages", [])
        tools = req.get("tools")
        ts_req = req.get("timestamp")
        rel_req = _rel_seconds(base_ts, ts_req)

        tool_count = len(tools) if tools else 0
        file_tag = f"  [file={req.get('_source_file', '?')}]" if multi_file else ""
        print(
            f"[{rel_req}] request_id={rid_full}  model={model}  "
            f"caller={caller}  msgs={len(msgs)}  tools={tool_count}{file_tag}"
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


def mode_llm_detail(records: list[dict], request_id: str, full: bool = False) -> None:
    """Pretty-print full payload for a single request_id.

    Searches across all records (which may originate from multiple files).
    If the same ``request_id`` appears in more than one source file, all hits
    are displayed in order with the ``_source_file`` annotated.
    """
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
    if req.get("_source_file"):
        print(f"  source_file: {req['_source_file']}")

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
        print("\n  --- response ---")
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


def mode_llm_context(records: list[dict], request_id: str) -> None:
    """Render the LLM context (= system + messages + tools + response) for
    one request_id in a human-readable layout.

    Distinct from ``llm-detail`` which truncates message contents — this
    mode emits the FULL untruncated payload so an operator (or a
    code-writing agent) can scan the actual prompt the LLM saw without
    having to write per-trace inspector scripts. Origin: dogfood found
    that reading the raw JSONL via ad-hoc Python burned analyst attention
    and missed structural bugs (= history duplication caused by a
    slicing off-by-one was invisible until the trace was formatted into
    indexed message rows). Adding this mode makes the formatted view a
    one-command operation so future debugging starts from the right
    grain.

    The output mirrors what dogfood batch retrospectives consistently
    end up writing manually: numbered messages, role headers,
    tool_call lines, tool_call_id annotations, separators, then the
    response with finish_reason / token counts. Searches across all
    records (handles multi-file traces).
    """
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

    messages = req.get("messages", []) or []
    tools = req.get("tools", []) or []

    print(f"REQUEST_ID: {req.get('request_id', '?')}")
    print(f"MODEL:      {req.get('model', '?')}")
    print(f"messages:   {len(messages)} entries")
    print(f"tools:      {len(tools)} entries")
    if req.get("_source_file"):
        print(f"source:     {req['_source_file']}")
    print()
    print("=" * 72)
    print("MESSAGES (= what the LLM sees)")
    print("=" * 72)
    print()

    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                x.get("text", "") for x in content if isinstance(x, dict)
            )
        tcs = m.get("tool_calls") or []
        tcid = m.get("tool_call_id", "")

        header = f"--- [{i:2d}] role={role}"
        if tcid:
            header += f"  tool_call_id={tcid}"
        header += " ---"
        print(header)
        if content:
            print(content)
        for tc in tcs:
            fn = tc.get("function", {})
            print(
                f"  TOOL_CALL: {fn.get('name')}({fn.get('arguments', '')})"
            )
        print()

    print("=" * 72)
    print(f"TOOLS ({len(tools)} total)")
    print("=" * 72)
    print()
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name", "?")
        desc = (fn.get("description") or "").splitlines()[0] if fn.get(
            "description"
        ) else ""
        if len(desc) > 100:
            desc = desc[:97] + "..."
        print(f"  - {name}: {desc}" if desc else f"  - {name}")

    print()
    print("=" * 72)
    print("RESPONSE")
    print("=" * 72)
    print()
    if resp is None:
        print("  (no response record found)")
        return
    print(f"finish_reason:     {resp.get('finish_reason')!r}")
    content = resp.get("content")
    print(f"content:           {content!r}")
    print(f"content_len:       {len(content) if content else 0}")
    tcs = resp.get("tool_calls") or []
    if tcs:
        print(f"tool_calls:        {len(tcs)}")
        for i, tc in enumerate(tcs):
            fn = tc.get("function", {})
            print(
                f"  [{i}] {fn.get('name')}({fn.get('arguments', '')})"
            )
    else:
        print(f"tool_calls:        {tcs!r}")
    usage = resp.get("usage") or {}
    if isinstance(usage, dict):
        print(
            f"prompt_tokens:     "
            f"{usage.get('prompt_tokens', '?')}"
        )
        print(
            f"completion_tokens: "
            f"{usage.get('completion_tokens', '?')}"
        )

    # Provider-specific fields surfaced by _extract_provider_response_fields.
    # Skip silently when absent (= older trace format without the extension).
    extras: dict[str, object] = {}
    for key in (
        "provider_specific_fields",
        "vertex_ai_safety_results",
        "vertex_ai_grounding_metadata",
        "vertex_ai_citation_metadata",
        "vertex_ai_url_context_metadata",
        "system_fingerprint",
        "service_tier",
        "completion_tokens_details",
    ):
        if key in resp and resp[key] not in (None, [], {}):
            extras[key] = resp[key]
    if extras:
        print()
        print("--- provider-specific ---")
        for k, v in extras.items():
            # Pretty-print dicts; show repr for primitives.
            if isinstance(v, (dict, list)):
                rendered = json.dumps(v, ensure_ascii=False)
                if len(rendered) > 300:
                    rendered = rendered[:297] + "..."
                print(f"  {k}: {rendered}")
            else:
                print(f"  {k}: {v!r}")


def mode_llm_tools_schema(records: list[dict], request_id: str) -> None:
    """Pretty-print the full tools schema for a single request_id.

    Searches across all records (which may originate from multiple files).
    """
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


def _extract_available_control_ops(req: dict) -> "list[dict] | None":
    """Pull the ``available_control_ops`` array out of a request.

    The Control IR op catalog the OS advertises to the LLM is serialised
    *inside a message content string* (not a top-level request field), so
    a plain ``req.get`` does not reach it. This helper scans message
    contents for the embedded JSON array and parses it defensively.

    Returns the parsed list, or ``None`` when the request does not carry
    an ``available_control_ops`` block (= phase/skill that advertises no
    control ops, or a non-phase request). Never raises.
    """
    messages = req.get("messages", []) or []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                x.get("text", "") for x in content if isinstance(x, dict)
            )
        if not isinstance(content, str) or "available_control_ops" not in content:
            continue
        # The content is itself a JSON object (the ContextFrame payload).
        # Try a structured parse first; fall back to locating the array.
        try:
            obj = json.loads(content)
            ops = obj.get("available_control_ops")
            if isinstance(ops, list):
                return ops
        except (json.JSONDecodeError, AttributeError):
            pass
        # Fallback: brace-match the array literal after the key.
        key = '"available_control_ops"'
        idx = content.find(key)
        start = content.find("[", idx)
        if start == -1:
            continue
        depth = 0
        for end in range(start, len(content)):
            ch = content[end]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        ops = json.loads(content[start : end + 1])
                        if isinstance(ops, list):
                            return ops
                    except json.JSONDecodeError:
                        break
                    break
    return None


def _op_field_summary(op: dict) -> "tuple[bool, bool, list[str], list[str], str]":
    """Return (has_description, has_example, fields, required, fields_source).

    Defensive against schema-shape variation across skills: looks for an
    argument schema under common keys (``schema`` / ``parameters`` /
    ``arguments`` / ``input_schema``) and reads ``properties`` + ``required``
    when present; otherwise treats remaining top-level keys as fields.
    """
    has_desc = bool(op.get("description"))
    has_example = bool(op.get("example") or op.get("示例") or op.get("examples"))

    schema = None
    for k in ("schema", "parameters", "arguments", "input_schema", "args"):
        if isinstance(op.get(k), dict):
            schema = op[k]
            break

    fields: list[str] = []
    required: list[str] = []
    fields_source = "none"
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            fields = list(props.keys())
            fields_source = "schema.properties"
        req_list = schema.get("required")
        if isinstance(req_list, list):
            required = [str(r) for r in req_list]
    else:
        # No formal arg schema (the common case — op specs are advertised as
        # {kind, description, example}, not JSON Schema). The structured
        # signal for "which fields does this op take" then lives in the
        # ``example`` object's keys (minus ``kind``). This is what answers
        # "is op X advertised with field Y?" (FP-0008 #1133).
        ex = op.get("example")
        if isinstance(ex, dict):
            fields = [k for k in ex.keys() if k != "kind"]
            fields_source = "example"
        else:
            meta_keys = {"kind", "description", "example", "examples", "示例"}
            fields = [k for k in op.keys() if k not in meta_keys]
            fields_source = "top-level"

    return has_desc, has_example, fields, required, fields_source


def mode_llm_advertised_ops(records: list[dict], request_id: str) -> None:
    """Structured summary of the Control IR ops advertised to the LLM.

    For the given request, extract ``available_control_ops`` and print one
    line per op: ``kind | desc=Y/N | example=Y/N | fields=[...] | required=[...]``.

    Origin: answering "is op X advertised? is field Y presented as required?"
    previously required hand-parsing nested JSON out of the prompt payload
    (FP-0008 #1133 co-sign investigation). This makes it a one-command
    structured read. Generic — works for any skill's request.
    """
    req: dict | None = None
    for rec in records:
        if rec.get("request_id") == request_id and rec.get("kind") == "request":
            req = rec
            break

    if req is None:
        print(f"request_id not found: {request_id}")
        sys.exit(1)

    ops = _extract_available_control_ops(req)
    if ops is None:
        print(f"available_control_ops: not present in request {request_id}")
        print("(= this phase/skill advertises no control ops, or non-phase request)")
        return

    print(f"available_control_ops ({len(ops)} entries) for request {request_id}:")
    print()
    for op in ops:
        if not isinstance(op, dict):
            print(f"  (non-dict entry: {op!r})")
            continue
        kind = op.get("kind", "?")
        has_desc, has_example, fields, required, fsrc = _op_field_summary(op)
        print(
            f"  kind={kind:<16} "
            f"desc={'Y' if has_desc else 'N'}  "
            f"example={'Y' if has_example else 'N'}  "
            f"fields={fields} (from {fsrc})  "
            f"required={required}"
        )


# CANONICAL contract: tya5/reyn#1135 issue comment (single source of truth,
# supersedes all broker v-FINAL-N ticks). The model's rejected raw output is
# carried by a single NEW additive event `phase_output_validation_failed`
# (existing validation_error/phase_failed etc. are UNCHANGED). failure_kind is
# an explicit enum field on the event (not derived from the event name).
_VALIDATION_FAIL_EVENT = "phase_output_validation_failed"
_JSON_DECODE_FAIL_EVENT = "llm_output_json_decode_failed"


def _event_type(e: dict) -> str:
    """Return an event's type, tolerating both on-disk conventions.

    The P6 events log writes the type under ``"type"`` (``EventLog.emit`` ->
    ``Event(type=...)``); some WAL/legacy records use ``"kind"``. Match either
    so the read-side finds real events (the events log) AND hand-written
    fixtures. (Field is top-level — NOT under ``data``, which carries the
    skill-level ``failure_kind`` enum, a different thing.)
    """
    return e.get("type") or e.get("kind") or ""


def _op_kind_keys(op: dict) -> "tuple[str, list[str]]":
    """Return (kind, sorted-non-kind-keys) for one emitted control_ir op."""
    kind = op.get("kind", "?")
    keys = sorted(k for k in op.keys() if k != "kind")
    return kind, keys


def _resolve_raw_output(ev: dict, state_dir: "Path | None") -> "str | None":
    """Return the raw model output for a phase_output_validation_failed event.

    Reads ``raw_output`` inline; if absent and ``raw_output_ref`` is set,
    dereferences via ``read_offloaded``. Per canonical contract (#1135) the ref
    is **state_dir-RELATIVE**, so the absolute path is ``state_dir / ref`` and
    the boundary check uses ``base_dir=state_dir``. Defensive: returns None on
    any miss/error rather than raising.
    """
    data = ev.get("data", ev)
    inline = data.get("raw_output")
    if isinstance(inline, str) and inline:
        return inline
    ref = data.get("raw_output_ref")
    if not (isinstance(ref, str) and ref) or state_dir is None:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from reyn.services.offload import read_offloaded
        content, found = read_offloaded(str(state_dir / ref), base_dir=state_dir)
        return content if found else None
    except Exception:
        return None


def mode_llm_emitted_ops(records: list[dict], request_id: str, root: "Path | None") -> None:
    """Structured summary of the LLM output the model EMITTED, across 3 sources.

      1. The LLM response for *request_id* — its ``ops`` array (the ops emitted
         on a successful parse), reduced to ``{kind, keys}``.
      2. ``phase_output_validation_failed`` events (POST-parse, #1137): the
         model emitted parseable JSON whose ops were REJECTED by validation —
         ``raw_output`` (inline) / ``raw_output_ref`` (state_dir-relative
         offload) reduced to ``{kind, keys}`` + the explicit ``failure_kind``.
      3. ``llm_output_json_decode_failed`` events (PRE-parse, #1141 sibling):
         the model emitted UNPARSEABLE JSON — no ops to extract, so show
         ``failure_kind`` + ``error`` + the (position-aware-truncated) raw
         slice verbatim. This is the classification signal for "weak-model
         malformed" vs "OS-repairable" decode failures.

    Sources 2 & 3 come from the events log (``--root``); their on-disk type
    field is ``type`` (matched via ``_event_type``), and they are NOT keyed by
    request_id, so all such events under --root are listed.

    Pairs with ``llm-advertised-ops``: advertised (what the OS offered) vs
    emitted (what the model produced), for decide-turn failure classification.
    """
    # Source 1: response ops for this request_id (from the LLM trace).
    resp: dict | None = None
    for rec in records:
        if rec.get("request_id") == request_id and rec.get("kind") == "response":
            resp = rec
            break

    print(f"emitted control_ir ops for request {request_id}:")
    print()
    if resp is None:
        print("  (no response record for this request_id in trace)")
    else:
        content = resp.get("content", "")
        ops = None
        if isinstance(content, str):
            try:
                ops = (json.loads(content) or {}).get("ops")
            except (json.JSONDecodeError, AttributeError):
                ops = None
        if not isinstance(ops, list) or not ops:
            print("  response: no ops emitted (empty / non-act response)")
        else:
            print(f"  response: {len(ops)} op(s)")
            for op in ops:
                if isinstance(op, dict):
                    kind, keys = _op_kind_keys(op)
                    print(f"    kind={kind:<16} keys={keys}")
                else:
                    print(f"    (non-dict op: {op!r})")

    # Sources 2 & 3 live in the events log (--root), not the LLM trace, and are
    # NOT request_id-keyed (events carry agent_id/run_id, not request_id), so we
    # list all such events. The on-disk type field is "type" (EventLog.emit ->
    # Event(type=...)); matching is via _event_type() which tolerates type/kind.
    if root is None:
        return
    state_dir = root
    all_evs = _all_events(root)

    # Source 2: phase_output_validation_failed (POST-parse, #1137). raw_output is
    # parseable JSON whose ops were rejected by validation -> reduce to {kind,keys}.
    fail_events = [e for e in all_evs if _event_type(e) == _VALIDATION_FAIL_EVENT]
    raw_bearing = [
        e for e in fail_events
        if (d := e.get("data", e)).get("raw_output") or d.get("raw_output_ref")
    ]
    print()
    if not raw_bearing:
        print(f"  {_VALIDATION_FAIL_EVENT} events with raw_output: none")
    else:
        print(f"  {_VALIDATION_FAIL_EVENT} events with raw model output ({len(raw_bearing)}):")
        for e in raw_bearing:
            d = e.get("data", e)
            failure_kind = d.get("failure_kind", "?")  # explicit enum field (canonical #1135)
            phase = d.get("phase", "?")
            raw = _resolve_raw_output(e, state_dir)
            ops_summary = "?"
            if raw:
                try:
                    parsed = json.loads(raw)
                    ops = parsed.get("ops") if isinstance(parsed, dict) else None
                    if isinstance(ops, list):
                        ops_summary = ", ".join(
                            f"{_op_kind_keys(o)[0]}{_op_kind_keys(o)[1]}"
                            for o in ops if isinstance(o, dict)
                        ) or "(no ops in raw)"
                    else:
                        ops_summary = "(raw not act-shaped)"
                except (json.JSONDecodeError, AttributeError):
                    ops_summary = "(raw unparseable)"
            else:
                ops_summary = "(raw_output_ref unresolved)"
            print(f"    failure_kind={failure_kind}  phase={phase}  emitted={ops_summary}")

    # Source 3: llm_output_json_decode_failed (PRE-parse, #1141 sibling). raw_output
    # is by definition UNPARSEABLE JSON, so there are no {kind,keys} to extract —
    # surface failure_kind + error + the (position-aware-truncated) raw slice
    # verbatim, which is the diagnostic for weak-model-malformed vs OS-repairable.
    decode_events = [e for e in all_evs if _event_type(e) == _JSON_DECODE_FAIL_EVENT]
    print()
    if not decode_events:
        print(f"  {_JSON_DECODE_FAIL_EVENT} events: none")
        return
    print(f"  {_JSON_DECODE_FAIL_EVENT} events ({len(decode_events)}):")
    for e in decode_events:
        d = e.get("data", e)
        failure_kind = d.get("failure_kind", "?")
        error = d.get("error", "?")
        raw = d.get("raw_output")
        raw_head = (raw[:200] + " …") if isinstance(raw, str) and len(raw) > 200 else raw
        print(f"    failure_kind={failure_kind}  error={error}")
        print(f"      raw_output: {raw_head!r}")


def _resolve_trace_paths(raw: list[str]) -> list[str]:
    """Expand a list of raw --trace values into a flat list of file paths.

    Each value may itself be a comma-separated list of paths.
    """
    paths: list[str] = []
    for v in raw:
        paths.extend(p.strip() for p in v.split(",") if p.strip())
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="dogfood_trace — consolidated Reyn batch observation tool")
    parser.add_argument("--root", default=".reyn", help="Path to .reyn directory (default: .reyn)")
    parser.add_argument(
        "--mode",
        choices=[
            "summary", "full", "chain", "cost",
            "llm-payloads", "llm-detail", "llm-tools-schema",
            "llm-context", "llm-advertised-ops", "llm-emitted-ops",
        ],
        default="summary",
    )
    parser.add_argument("--filter", dest="filter_kind", default=None, help="Filter by event kind (for --mode full)")
    parser.add_argument(
        "--trace",
        action="append",
        default=[],
        help=(
            "Path to LLM/WAL trace JSONL file. "
            "For llm-* modes: can be specified multiple times or comma-separated."
        ),
    )
    parser.add_argument("--full", action="store_true", default=False, help="Show full messages/tools (for llm-detail)")
    parser.add_argument("request_id", nargs="?", default=None, help="request_id for llm-detail / llm-tools-schema")
    args = parser.parse_args()

    # ── LLM trace modes ───────────────────────────────────────────────────
    if args.mode in ("llm-payloads", "llm-detail", "llm-tools-schema", "llm-context", "llm-advertised-ops", "llm-emitted-ops"):
        trace_paths = _resolve_trace_paths(args.trace)
        if not trace_paths:
            trace_paths = [".reyn/llm_trace.jsonl"]
        multi_file = len(trace_paths) > 1
        records = _load_llm_trace_files(trace_paths)

        if args.mode == "llm-payloads":
            mode_llm_payloads(records, multi_file=multi_file)
            return

        if args.mode == "llm-detail":
            if not args.request_id:
                print("llm-detail requires a request_id argument")
                sys.exit(1)
            mode_llm_detail(records, args.request_id, full=args.full)
            return

        if args.mode == "llm-tools-schema":
            if not args.request_id:
                print("llm-tools-schema requires a request_id argument")
                sys.exit(1)
            mode_llm_tools_schema(records, args.request_id)
            return

        if args.mode == "llm-advertised-ops":
            if not args.request_id:
                print("llm-advertised-ops requires a request_id argument")
                sys.exit(1)
            mode_llm_advertised_ops(records, args.request_id)
            return

        if args.mode == "llm-emitted-ops":
            if not args.request_id:
                print("llm-emitted-ops requires a request_id argument")
                sys.exit(1)
            # Validation-fail events live in the events log (--root), separate
            # from the LLM trace. Pass root when it exists so the rejected-op
            # half can read them; None otherwise (response-ops half still works).
            emit_root = Path(args.root) if Path(args.root).exists() else None
            mode_llm_emitted_ops(records, args.request_id, emit_root)
            return

        if args.mode == "llm-context":
            if not args.request_id:
                print("llm-context requires a request_id argument")
                sys.exit(1)
            mode_llm_context(records, args.request_id)
            return

    # ── Event-based modes ─────────────────────────────────────────────────
    root = Path(args.root)
    if not root.exists():
        print(f"no events found (root not found: {root})")
        sys.exit(0)

    {"summary": mode_summary, "full": mode_full, "chain": mode_chain, "cost": mode_cost}[args.mode](
        *([root, args.filter_kind] if args.mode == "full" else [root])
    )


if __name__ == "__main__":
    main()
