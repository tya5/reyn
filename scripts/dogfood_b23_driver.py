#!/usr/bin/env python3
"""Dogfood batch 23 driver — FP-0034 wrapper-only e2e calibration.

Runs one of the 3 batch-23 scenarios in the current directory (intended for
worktree-isolated dispatch). Prints a structured JSON observation to stdout
on the line marked `RESULT:` so a parent driver / agent can parse it.

Scenarios:
- s1: catalog discovery (list_actions -> describe_action -> invoke_action)
- s2: routing_decided P6 event emit (invoke_action file__read)
- s3: exec visibility gating (list_actions category=["exec"])

The scenario prompt is sent to `reyn chat --cui` via stdin (a single user
turn).  `REYN_LLM_TRACE_DUMP` is set so all LLM payloads are captured.
After the chat finishes, the driver inspects:

- trace dump: tool_call sequence, system prompt size, legacy-tool literal
  count
- events log: routing_decided emissions (s2) + any P6 events
- chat reply text content

It emits a 4-outcome verdict (verified / inconclusive / refuted / blocked)
following the prelude's prediction rubric.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCENARIOS = {
    "s1": {
        "name": "Catalog discovery (3-turn)",
        "prompt": "利用可能な skill の一覧を教えて、 その中から code_review を実行してください",
        "expected_path": "list_actions(category=['skill']) -> describe_action(skill__code_review) -> invoke_action(skill__code_review, ...)",
        "prompt_class": "P-explicit",
    },
    "s2": {
        "name": "routing_decided P6 event emit",
        "prompt": "file__read を invoke_action で /etc/hostname に対して使ってください",
        "expected_path": "invoke_action(action_name='file__read', args={'path': '/etc/hostname'}) -> routing_decided event",
        "prompt_class": "P-explicit",
    },
    "s3": {
        "name": "exec visibility gating",
        "prompt": "sandboxed コマンド実行に使える action はありますか",
        "expected_path": "list_actions(category=['exec']) -> empty (sandbox.backend=noop)",
        "prompt_class": "P-natural",
    },
}


def run_chat(prompt: str, trace_file: Path, cwd: Path, timeout: int = 180) -> dict:
    """Pipe a single user turn through `reyn chat --cui`."""
    env = {**os.environ, "REYN_LLM_TRACE_DUMP": str(trace_file)}
    start = time.time()
    proc = subprocess.run(
        ["reyn", "chat", "--cui"],
        input=f"{prompt}\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=env,
    )
    elapsed = time.time() - start
    return {
        "elapsed_s": round(elapsed, 1),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr[-2000:] if proc.stderr else "",
    }


def parse_trace(trace_file: Path) -> dict:
    """Inspect REYN_LLM_TRACE_DUMP for SP, tool calls, replies."""
    if not trace_file.exists():
        return {"error": "trace file missing", "calls": 0}
    system_prompt = ""
    tool_calls_per_turn: list[list[dict]] = []
    text_replies: list[str] = []
    calls = 0
    for raw in trace_file.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = d.get("kind", "")
        if kind == "request":
            calls += 1
            if not system_prompt:
                for m in d.get("messages", []):
                    if m.get("role") == "system":
                        system_prompt = str(m.get("content", ""))
                        break
        elif kind == "response":
            tcs = d.get("tool_calls") or []
            tool_calls_per_turn.append([
                {
                    "name": tc.get("function", {}).get("name") if "function" in tc else tc.get("name"),
                    "args": tc.get("function", {}).get("arguments") if "function" in tc else tc.get("arguments"),
                }
                for tc in tcs
            ])
            text_replies.append(d.get("content") or "")
    flat_tool_calls = [tc for turn in tool_calls_per_turn for tc in turn]
    tool_names = [tc["name"] for tc in flat_tool_calls if tc["name"]]
    sp_len = len(system_prompt)
    legacy_literals = sum(
        system_prompt.count(name)
        for name in [
            "invoke_skill", "list_skills", "describe_skill",
            "list_agents", "describe_agent", "delegate_to_agent",
            "list_mcp_servers", "list_mcp_tools", "call_mcp_tool",
            "describe_mcp_tool", "list_memory", "read_memory_body",
            "remember_shared", "remember_agent", "forget_memory",
            "drop_source",
        ]
    )
    return {
        "calls": calls,
        "sp_chars": sp_len,
        "sp_legacy_literal_count": legacy_literals,
        "tool_names": tool_names,
        "tool_calls": flat_tool_calls,
        "tool_calls_per_turn": tool_calls_per_turn,
        "text_replies": text_replies,
        "final_reply": text_replies[-1] if text_replies else "",
    }


def grep_routing_decided(events_root: Path) -> list[dict]:
    """Walk events log for routing_decided event entries."""
    found = []
    if not events_root.exists():
        return found
    for path in events_root.rglob("*.jsonl"):
        for raw in path.read_text().splitlines():
            if "routing_decided" not in raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            evt_type = d.get("type") or d.get("event_type") or d.get("event")
            if evt_type == "routing_decided":
                found.append({"file": str(path), "event": d})
    return found


def verdict_s1(parse: dict) -> tuple[str, str]:
    names = parse["tool_names"]
    has_list = any(n in ("list_actions", "search_actions") for n in names)
    has_describe = "describe_action" in names
    has_invoke = "invoke_action" in names
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"
    if has_list and has_describe and has_invoke:
        return "verified", "all 3 wrappers called in sequence"
    if has_list and has_invoke and not has_describe:
        return "inconclusive", "skipped describe_action, direct invoke after list"
    if has_invoke and not has_list:
        return "inconclusive", "invoke_action without prior list_actions"
    if has_list and not has_invoke:
        return "inconclusive", "stopped after list_actions, no invoke"
    if any("invoke_skill" in n or "list_skills" in n for n in names):
        return "refuted", "called legacy tool (regression: hide_legacy_tools=true ignored)"
    return "refuted", f"unexpected tool sequence: {names}"


def verdict_s2(parse: dict, events: list[dict]) -> tuple[str, str]:
    names = parse["tool_names"]
    has_invoke_action = "invoke_action" in names
    routing_events = [e for e in events if e["event"].get("type") == "routing_decided"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"
    if has_invoke_action and routing_events:
        return "verified", f"invoke_action called + {len(routing_events)} routing_decided event(s)"
    if has_invoke_action and not routing_events:
        return "refuted", "invoke_action called but no routing_decided event emitted"
    if "read_file" in names or any("file" in (n or "") and "read" in (n or "") for n in names):
        return "refuted", f"called legacy file tool instead of invoke_action: {names}"
    return "refuted", f"invoke_action not called: {names}"


def verdict_s3(parse: dict) -> tuple[str, str]:
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"
    list_calls = [tc for tc in parse["tool_calls"] if tc["name"] == "list_actions"]
    exec_listed = False
    for tc in list_calls:
        args_raw = tc.get("args")
        if isinstance(args_raw, str):
            try:
                args_raw = json.loads(args_raw)
            except json.JSONDecodeError:
                args_raw = {}
        if isinstance(args_raw, dict):
            cat = args_raw.get("category") or args_raw.get("categories")
            if cat is None:
                continue
            if isinstance(cat, str) and cat == "exec":
                exec_listed = True
            elif isinstance(cat, list) and "exec" in cat:
                exec_listed = True
    if exec_listed:
        return "verified", "list_actions(category=['exec']) called"
    if list_calls:
        return "inconclusive", "list_actions called but not with exec category"
    if any(n == "invoke_action" for n in names):
        return "inconclusive", "invoke_action without list_actions(exec) first"
    return "refuted", f"no list_actions call: {names}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=list(SCENARIOS))
    ap.add_argument("--trace-dir", default="/tmp/reyn_b23")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    sc = SCENARIOS[args.scenario]
    cwd = Path(args.cwd).resolve()
    trace_dir = Path(args.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_file = trace_dir / f"{args.scenario}.jsonl"
    if trace_file.exists():
        trace_file.unlink()

    print(f"[{args.scenario}] {sc['name']}", file=sys.stderr)
    print(f"  cwd:    {cwd}", file=sys.stderr)
    print(f"  trace:  {trace_file}", file=sys.stderr)
    print(f"  prompt: {sc['prompt']}", file=sys.stderr)

    chat = run_chat(sc["prompt"], trace_file, cwd, timeout=args.timeout)
    parse = parse_trace(trace_file)
    events_root = cwd / ".reyn" / "events"
    events = grep_routing_decided(events_root)

    if args.scenario == "s1":
        verdict, reason = verdict_s1(parse)
    elif args.scenario == "s2":
        verdict, reason = verdict_s2(parse, events)
    else:
        verdict, reason = verdict_s3(parse)

    result = {
        "scenario": args.scenario,
        "name": sc["name"],
        "prompt": sc["prompt"],
        "prompt_class": sc["prompt_class"],
        "expected_path": sc["expected_path"],
        "verdict": verdict,
        "reason": reason,
        "elapsed_s": chat["elapsed_s"],
        "returncode": chat["returncode"],
        "stderr_tail": chat["stderr"][-400:] if chat["stderr"] else "",
        "trace_file": str(trace_file),
        "llm_calls": parse.get("calls", 0),
        "sp_chars": parse.get("sp_chars", 0),
        "sp_legacy_literal_count": parse.get("sp_legacy_literal_count", -1),
        "tool_names": parse.get("tool_names", []),
        "tool_calls": parse.get("tool_calls", []),
        "tool_calls_per_turn": parse.get("tool_calls_per_turn", []),
        "final_reply": parse.get("final_reply", "")[:1500],
        "routing_decided_events": len([e for e in events if e["event"].get("type") == "routing_decided"]),
        "routing_decided_sample": (events[0]["event"] if events else None),
    }
    print(f"RESULT:{json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
