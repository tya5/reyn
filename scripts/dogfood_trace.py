"""dogfood_trace.py — consolidate batch observation greps into one tool.

Usage:
    python scripts/dogfood_trace.py [--root .reyn] [--mode summary|full|chain|cost]
                                    [--filter <event_kind>]

    # LLM payload trace modes (requires REYN_LLM_TRACE_DUMP to have been set during dogfood):
    python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-detail <request_id> --trace .reyn/llm_trace.jsonl [--full]
    python scripts/dogfood_trace.py --mode llm-tools-schema <request_id> --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode llm-context <request_id> --trace .reyn/llm_trace.jsonl

    # Multiple trace files (merged chronologically):
    python scripts/dogfood_trace.py --mode llm-payloads --trace a.jsonl --trace b.jsonl
    python scripts/dogfood_trace.py --mode llm-payloads --trace a.jsonl,b.jsonl

    # Time-travel replay modes (new):
    python scripts/dogfood_trace.py --mode replay --trace <path> [--scope step|phase|skill_run]
    python scripts/dogfood_trace.py --mode replay --trace <path> --at run_xyz:copy_to_work:3
    python scripts/dogfood_trace.py --mode compare --before <trace_a> --after <trace_b> [--scope phase]

    # Multi-file replay (= operational shortcut, no concat needed):
    python scripts/dogfood_trace.py --mode replay \\
        --wal .reyn/state/wal.jsonl --trace .reyn/llm_trace.jsonl
    python scripts/dogfood_trace.py --mode compare \\
        --before .reyn/state/wal.jsonl --before .reyn/llm_trace.jsonl \\
        --after .reyn/state/wal.jsonl.bak --after .reyn/llm_trace.jsonl.bak
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
    print(f"content_len:       {len(content) if content else 0}")
    if content:
        print("--- content ---")
        print(content)
        print("--- /content ---")
    tcs = resp.get("tool_calls") or []
    if tcs:
        print(f"tool_calls:        {len(tcs)}")
        for i, tc in enumerate(tcs):
            fn = tc.get("function", {})
            print(
                f"  [{i}] {fn.get('name')}({fn.get('arguments', '')})"
            )
    else:
        print("tool_calls:        (none)")
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


# ---------------------------------------------------------------------------
# Time-travel: replay + compare modes
# ---------------------------------------------------------------------------

def mode_replay(trace_paths: list[str], at: str | None, scope: str) -> None:
    """Walk a recorded trace and print step frames.

    *trace_paths* is the merged list of WAL + LLM trace inputs (from
    ``--wal`` and ``--trace``).  Single-file callers pass a list of one.

    When ``at`` is given (``run_id:phase:step_idx``), jump to that specific
    checkpoint and print its frame only.  Otherwise, walk all frames at the
    requested zoom level.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from reyn.replay import Checkpoint, ReplayEngine

    try:
        engine = ReplayEngine(trace_paths)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if at:
        try:
            cp = Checkpoint.parse(at)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            frame = engine.seek(cp)
        except KeyError:
            print(f"checkpoint not found: {at}", file=sys.stderr)
            sys.exit(1)
        _print_step_frame(frame, show_llm=True)
        return

    # Full walk.
    scope_val: str = scope or "step"
    frames = list(engine.walk(scope=scope_val))  # type: ignore[arg-type]
    if not frames:
        print("no frames found in trace")
        return

    label = trace_paths[0] if len(trace_paths) == 1 else f"{len(trace_paths)} files"
    print(f"=== Replay: {label}  scope={scope_val}  frames={len(frames)} ===\n")
    for i, frame in enumerate(frames, 1):
        print(f"[{i}/{len(frames)}]  {frame.checkpoint}")
        _print_step_frame(frame, show_llm=(i == 1 or frame.llm_payload is not None))


def mode_compare(
    before_paths: list[str], after_paths: list[str], scope: str
) -> None:
    """Walk two traces and print side-by-side diffs.

    Each side accepts a list of paths (= merged WAL + LLM trace inputs).
    Events diff, state diff, and LLM payload diff are shown for each frame
    where a difference is detected.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from reyn.replay import compare

    scope_val: str = scope or "step"
    try:
        diff_frames = list(
            compare(before_paths, after_paths, scope=scope_val)  # type: ignore[arg-type]
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    total = len(diff_frames)
    with_diff = sum(1 for f in diff_frames if f.has_diff)
    print(
        f"=== Compare  scope={scope_val}  frames={total}  "
        f"with_diff={with_diff} ===\n"
    )
    before_label = before_paths[0] if len(before_paths) == 1 else f"{len(before_paths)} files"
    after_label = after_paths[0] if len(after_paths) == 1 else f"{len(after_paths)} files"
    print(f"  before: {before_label}")
    print(f"  after:  {after_label}\n")

    for i, df in enumerate(diff_frames, 1):
        b_cp = str(df.before.checkpoint) if df.before else "(absent)"
        a_cp = str(df.after.checkpoint) if df.after else "(absent)"
        header = f"[{i}/{total}]  before={b_cp}  after={a_cp}"
        if not df.has_diff:
            print(f"{header}  (no diff)")
            continue

        print(header)
        if df.events_diff:
            print("  events_diff:")
            for ch in df.events_diff.get("changes", []):
                print(
                    f"    {ch['kind']}: before={ch['before_count']}  "
                    f"after={ch['after_count']}"
                )
        if df.state_diff:
            print("  state_diff:")
            for ch in df.state_diff.get("changed", []):
                print(f"    {ch['key']}: {ch['before']!r} → {ch['after']!r}")
            for k in df.state_diff.get("added", []):
                print(f"    +{k}")
            for k in df.state_diff.get("removed", []):
                print(f"    -{k}")
        if df.llm_diff:
            print("  llm_diff:")
            if "model_changed" in df.llm_diff:
                mc = df.llm_diff["model_changed"]
                print(f"    model: {mc['before']!r} → {mc['after']!r}")
            if "prompt_diff" in df.llm_diff:
                pd = df.llm_diff["prompt_diff"]
                print(
                    f"    prompt: len {pd['before_len']} → {pd['after_len']} (changed)"
                )
            if "response_diff" in df.llm_diff:
                rd = df.llm_diff["response_diff"]
                print(
                    f"    response: len {rd['before_len']} → {rd['after_len']} (changed)"
                )
            if "tool_calls_diff" in df.llm_diff:
                tc = df.llm_diff["tool_calls_diff"]
                print(f"    tool_calls: {tc['before']} → {tc['after']}")
        print()


def _print_step_frame(frame: object, *, show_llm: bool = True) -> None:
    """Pretty-print a single StepFrame."""
    from reyn.replay.model import StepFrame
    if not isinstance(frame, StepFrame):
        return
    print(f"  checkpoint:  {frame.checkpoint}")
    print(f"  events ({len(frame.events)}):")
    for ev in frame.events:
        kind = ev.get("kind", "?")
        seq = ev.get("seq", "")
        ts = (ev.get("ts") or ev.get("timestamp") or "")[:19]
        print(f"    [{seq}] {kind}  {ts}")
    if frame.state_snapshot:
        print("  state_snapshot:")
        for k, v in frame.state_snapshot.items():
            print(f"    {k}: {v!r}")
    if show_llm and frame.llm_payload:
        model = frame.llm_payload.get("model", "?")
        msgs = frame.llm_payload.get("messages", [])
        print(f"  llm_payload: model={model}  msgs={len(msgs)}")
        if frame.llm_result:
            finish = frame.llm_result.get("finish_reason", "?")
            print(f"  llm_result:  finish_reason={finish}")
    print()


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
            "llm-context",
            # New time-travel modes:
            "replay", "compare",
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
            "For llm-* modes: can be specified multiple times or comma-separated. "
            "For replay mode: combined with --wal entries to feed the engine."
        ),
    )
    parser.add_argument(
        "--wal",
        action="append",
        default=[],
        help=(
            "Path to WAL JSONL file (= .reyn/state/wal.jsonl). "
            "For replay mode only.  Combine with --trace to feed both files "
            "without concat.  Can be specified multiple times or comma-separated."
        ),
    )
    parser.add_argument("--full", action="store_true", default=False, help="Show full messages/tools (for llm-detail)")
    parser.add_argument("request_id", nargs="?", default=None, help="request_id for llm-detail / llm-tools-schema")
    # Time-travel options.
    parser.add_argument(
        "--at",
        default=None,
        help="Checkpoint to jump to (for --mode replay). Format: run_id:phase:step_idx",
    )
    parser.add_argument(
        "--scope",
        default="step",
        choices=["step", "phase", "skill_run"],
        help="Zoom level for replay / compare (default: step)",
    )
    parser.add_argument(
        "--before",
        action="append",
        default=[],
        help=(
            "Path to 'before' trace JSONL (for --mode compare). "
            "Can be specified multiple times to feed both WAL and LLM trace "
            "files without concat (e.g. --before .reyn/state/wal.jsonl "
            "--before .reyn/llm_trace.jsonl)."
        ),
    )
    parser.add_argument(
        "--after",
        action="append",
        default=[],
        help=(
            "Path to 'after' trace JSONL (for --mode compare). "
            "Can be specified multiple times — same merging rules as --before."
        ),
    )
    args = parser.parse_args()

    # ── Time-travel modes (new) ────────────────────────────────────────────
    if args.mode == "replay":
        trace_paths = _resolve_trace_paths(args.trace) + _resolve_trace_paths(args.wal)
        if not trace_paths:
            print(
                "--mode replay requires --trace <path> and/or --wal <path>",
                file=sys.stderr,
            )
            sys.exit(1)
        mode_replay(trace_paths, at=args.at, scope=args.scope)
        return

    if args.mode == "compare":
        before_paths = _resolve_trace_paths(args.before)
        after_paths = _resolve_trace_paths(args.after)
        if not before_paths or not after_paths:
            print(
                "--mode compare requires --before <path> and --after <path> "
                "(repeat each flag to merge WAL + LLM trace)",
                file=sys.stderr,
            )
            sys.exit(1)
        mode_compare(before_paths, after_paths, scope=args.scope)
        return

    # ── LLM trace modes ───────────────────────────────────────────────────
    if args.mode in ("llm-payloads", "llm-detail", "llm-tools-schema", "llm-context"):
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
