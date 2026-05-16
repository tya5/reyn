#!/usr/bin/env python3
"""Dogfood batch 24 driver — FP-0034 universal catalog extended calibration.

Runs one of the 7 batch-24 scenarios in the current directory (intended for
worktree-isolated dispatch). Prints a structured JSON observation to stdout
on the line marked `RESULT:` so a parent driver / agent can parse it.

Scenarios:
- s1a: Catalog discovery — parallel-tolerant (list_actions + invoke_action parallel OK if error surface)
- s1b: Catalog discovery — sequential connector (list_actions single turn, next-turn SEQ)
- s2:  routing_decided P6 event emit (invoke_action file__read)
- s3_noop: exec visibility — sandbox.backend=noop empty variant
- s3_auto: exec visibility — sandbox.backend=auto path
- s4_hot_cold: Hot list cold start direct alias rate
- s5_search: search_actions semantic via natural query

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
    "s1a": {  # P-explicit-AND, parallel-tolerant
        "name": "Catalog discovery — parallel-tolerant",
        "prompt": "利用可能な skill の一覧を教えて、 その中から code_review を実行してください",
        "expected_path": "list_actions + invoke_action (parallel OK if error correctly surfaced)",
        "prompt_class": "P-explicit-AND",
    },
    "s1b": {  # P-explicit-SEQ baseline
        "name": "Catalog discovery — sequential connector",
        "prompt": "利用可能な skill を確認してください。 その後、 もし code_review があれば実行してください",
        "expected_path": "list_actions → (next turn) describe_action or invoke_action",
        "prompt_class": "P-explicit-SEQ",
    },
    "s2": {  # 既存
        "name": "routing_decided P6 event emit",
        "prompt": "file__read を invoke_action で /etc/hostname に対して使ってください",
        "expected_path": "invoke_action(file__read, /etc/hostname) → routing_decided event",
        "prompt_class": "P-explicit",
    },
    "s3_noop": {  # explicit noop override
        "name": "exec visibility — sandbox.backend=noop empty variant",
        "prompt": "sandboxed コマンド実行に使える action はありますか",
        "expected_path": "list_actions(category=['exec']) → empty (gating active)",
        "prompt_class": "P-natural",
    },
    "s3_auto": {  # default auto
        "name": "exec visibility — sandbox.backend=auto path",
        "prompt": "sandboxed コマンド実行に使える action はありますか",
        "expected_path": "list_actions(category=['exec']) → [exec__sandboxed_exec] → describe",
        "prompt_class": "P-natural",
    },
    "s4_hot_cold": {  # hot list cold start
        "name": "Hot list cold start direct alias rate",
        "prompt": "memory に何を覚えていますか",
        "expected_path": "list_actions(category=['memory.entry']) or direct hot alias call",
        "prompt_class": "P-natural",
    },
    "s5_search": {  # search_actions semantic
        "name": "search_actions semantic via natural query",
        "prompt": "現在使えるアクションの中から、 文字列処理関連のものを探したいです",
        "expected_path": "search_actions(query='文字列処理') → result inspect → ?",
        "prompt_class": "P-natural-semantic",
    },
}


def run_chat(prompt: str, trace_file: Path, cwd: Path, agent_name: str | None = None, timeout: int = 180) -> dict:
    """Pipe a single user turn through `reyn chat --cui [<agent_name>]`."""
    env = {**os.environ, "REYN_LLM_TRACE_DUMP": str(trace_file)}
    cmd = ["reyn", "chat", "--cui"]
    if agent_name:
        cmd.append(agent_name)
    start = time.time()
    proc = subprocess.run(
        cmd,
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
    finish_reasons: list[str] = []
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
            finish_reasons.append(d.get("finish_reason") or "")
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
        "finish_reasons": finish_reasons,
        "first_turn_finish_reason": finish_reasons[0] if finish_reasons else "",
        "first_turn_tool_names": [tc["name"] for tc in (tool_calls_per_turn[0] if tool_calls_per_turn else []) if tc["name"]],
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


def _parse_list_action_categories(tool_calls: list[dict]) -> list[list]:
    """Extract category arguments from all list_actions calls."""
    result = []
    for tc in tool_calls:
        if tc.get("name") != "list_actions":
            continue
        args_raw = tc.get("args")
        if isinstance(args_raw, str):
            try:
                args_raw = json.loads(args_raw)
            except json.JSONDecodeError:
                args_raw = {}
        if isinstance(args_raw, dict):
            cat = args_raw.get("category") or args_raw.get("categories")
            result.append(cat)
        else:
            result.append(None)
    return result


def _has_exec_category(list_action_cats: list) -> bool:
    """Return True if any list_actions call used category=['exec'] or category='exec'."""
    for cat in list_action_cats:
        if cat is None:
            continue
        if isinstance(cat, str) and cat == "exec":
            return True
        if isinstance(cat, list) and "exec" in cat:
            return True
    return False


def verdict_s1a(parse: dict) -> tuple[str, str]:
    """s1a: parallel-tolerant.

    Verified if:
      - list_actions AND invoke_action both called (parallel or sequential)
      - AND if parallel (both in turn 0), final_reply surfaces an error or
        acknowledges the parallelism was resolved

    Inconclusive if list+invoke called but error not surfaced in reply.
    Refuted if legacy tools called.
    """
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"

    has_list = any(n in ("list_actions", "search_actions") for n in names)
    has_invoke = "invoke_action" in names

    if any("invoke_skill" in (n or "") or "list_skills" in (n or "") for n in names):
        return "refuted", "called legacy tool (regression: hide_legacy_tools=true ignored)"

    if not has_list and not has_invoke:
        return "refuted", f"neither list_actions nor invoke_action called: {names}"

    if not has_list:
        return "inconclusive", "invoke_action without prior list_actions — discovery step skipped"

    if not has_invoke:
        return "inconclusive", "list_actions called but no invoke_action — stopped after discovery"

    # Both called. Check if they were dispatched in the same turn (parallel).
    first_turn_names = parse.get("first_turn_tool_names", [])
    parallel_dispatch = (
        any(n in ("list_actions", "search_actions") for n in first_turn_names)
        and "invoke_action" in first_turn_names
    )

    if parallel_dispatch:
        # Parallel is accepted only if the reply surfaces the error/resolution.
        final_reply = parse.get("final_reply", "").lower()
        error_surface_keywords = ["error", "エラー", "not found", "見つかり", "失敗", "could not", "unknown"]
        error_surfaced = any(kw in final_reply for kw in error_surface_keywords)
        if error_surfaced:
            return "verified", "parallel_dispatch_with_error_surface = verified (list+invoke same turn, error in reply)"
        return "inconclusive", "parallel dispatch detected but error not clearly surfaced in final reply"

    # Sequential: list before invoke — straightforwardly verified.
    return "verified", "list_actions then invoke_action called sequentially"


def verdict_s1b(parse: dict) -> tuple[str, str]:
    """s1b: sequential connector.

    Verified if Turn 1 has ONLY list_actions (finish_reason=tool_calls implies
    the model is pausing for tool result before deciding next step).
    Inconclusive if both list and invoke in turn 1, or if neither called.
    Refuted if legacy tools or direct invoke without list.
    """
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"

    if any("invoke_skill" in (n or "") or "list_skills" in (n or "") for n in names):
        return "refuted", "called legacy tool (regression)"

    first_turn_names = parse.get("first_turn_tool_names", [])
    first_turn_finish = parse.get("first_turn_finish_reason", "")

    has_list_t1 = any(n in ("list_actions", "search_actions") for n in first_turn_names)
    has_invoke_t1 = "invoke_action" in first_turn_names

    if has_list_t1 and not has_invoke_t1 and first_turn_finish == "tool_calls":
        return "verified", "Turn 1: list_actions only with finish_reason=tool_calls — sequential pause confirmed"

    if has_list_t1 and not has_invoke_t1:
        # list only but finish_reason unclear
        return "inconclusive", f"Turn 1: list_actions only but finish_reason='{first_turn_finish}' (expected tool_calls)"

    if has_list_t1 and has_invoke_t1:
        return "inconclusive", "Turn 1: list+invoke parallel (SEQ connector not respected)"

    if "invoke_action" in names and not any(n in ("list_actions", "search_actions") for n in names):
        return "refuted", "invoke_action without any list_actions — discovery skipped entirely"

    if not first_turn_names:
        return "inconclusive", f"Turn 1: no tool calls; names across all turns: {names}"

    return "refuted", f"unexpected Turn 1 tool sequence: {first_turn_names}"


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


def verdict_s3_noop(parse: dict) -> tuple[str, str]:
    """s3_noop: expect list_actions(category=['exec']) called and result is empty.

    Verified: list_actions with exec category called. The empty-result check is
    inferred from the final_reply (LLM should say nothing available).
    Refuted: any exec-related action directly invoked, or legacy tools called.
    Inconclusive: list_actions called but not with exec category.
    """
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"

    list_calls = [tc for tc in parse["tool_calls"] if tc.get("name") == "list_actions"]
    list_action_cats = _parse_list_action_categories(parse["tool_calls"])
    exec_listed = _has_exec_category(list_action_cats)

    final_reply = parse.get("final_reply", "").lower()
    empty_ack_keywords = ["ありません", "見つかり", "empty", "none", "not available", "no action", "利用できません"]
    empty_acked = any(kw in final_reply for kw in empty_ack_keywords)

    if exec_listed and empty_acked:
        return "verified", "list_actions(category=['exec']) called + reply acknowledges empty result"
    if exec_listed:
        return "inconclusive", "list_actions(category=['exec']) called but reply does not clearly ack empty result"
    if list_calls:
        return "inconclusive", "list_actions called but not with exec category"
    if any(n == "invoke_action" for n in names):
        return "refuted", "invoke_action called without list_actions(exec) — gating not observed"
    return "refuted", f"no list_actions call: {names}"


def verdict_s3_auto(parse: dict) -> tuple[str, str]:
    """s3_auto: same as b23 verdict_s3 — list_actions with exec category called is sufficient."""
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"

    list_action_cats = _parse_list_action_categories(parse["tool_calls"])
    exec_listed = _has_exec_category(list_action_cats)
    list_calls = [tc for tc in parse["tool_calls"] if tc.get("name") == "list_actions"]

    if exec_listed:
        return "verified", "list_actions(category=['exec']) called"
    if list_calls:
        return "inconclusive", "list_actions called but not with exec category"
    if any(n == "invoke_action" for n in names):
        return "inconclusive", "invoke_action without list_actions(exec) first"
    return "refuted", f"no list_actions call: {names}"


def verdict_s4_hot_cold(parse: dict) -> tuple[str, str]:
    """s4_hot_cold: observe whether hot alias is called directly vs list_actions first.

    Verified-hot: invoke_action called directly with a memory.entry alias in Turn 1
      (no prior list_actions) — hot list is operational.
    Verified-cold: list_actions(category=['memory.entry'] or category=['memory']) called
      first, then invoke — cold start path.
    Inconclusive: list_actions called but not with memory category, or no clear path.
    Refuted: legacy memory tools called.
    """
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"

    if any("list_memory" in (n or "") or "read_memory_body" in (n or "") for n in names):
        return "refuted", "called legacy memory tools"

    list_action_cats = _parse_list_action_categories(parse["tool_calls"])
    memory_listed = False
    for cat in list_action_cats:
        if cat is None:
            continue
        if isinstance(cat, str) and cat.startswith("memory"):
            memory_listed = True
        elif isinstance(cat, list) and any(str(c).startswith("memory") for c in cat):
            memory_listed = True

    first_turn_names = parse.get("first_turn_tool_names", [])
    has_invoke_t1 = "invoke_action" in first_turn_names
    has_list_t1 = "list_actions" in first_turn_names

    # Hot path: invoke in Turn 1 with no prior list_actions
    if has_invoke_t1 and not has_list_t1:
        # Check if invoke args reference a memory action
        t1_invokes = [
            tc for tc in (parse["tool_calls_per_turn"][0] if parse.get("tool_calls_per_turn") else [])
            if tc.get("name") == "invoke_action"
        ]
        memory_action_ref = False
        for inv in t1_invokes:
            args_raw = inv.get("args")
            if isinstance(args_raw, str):
                if "memory" in args_raw.lower():
                    memory_action_ref = True
            elif isinstance(args_raw, dict):
                action_name = str(args_raw.get("action_name", "")).lower()
                if "memory" in action_name:
                    memory_action_ref = True
        if memory_action_ref:
            return "verified", "hot-path: invoke_action with memory action in Turn 1 (no prior list_actions) — hot alias operational"
        return "inconclusive", "invoke_action in Turn 1 but action name does not reference memory — cannot confirm hot alias"

    if memory_listed:
        return "verified", "cold-path: list_actions(category='memory.*') called — cold start discovery path"

    list_calls = [tc for tc in parse["tool_calls"] if tc.get("name") == "list_actions"]
    if list_calls:
        return "inconclusive", f"list_actions called but not with memory category: cats={list_action_cats}"

    return "refuted", f"neither hot alias nor list_actions(memory) path observed: {names}"


def verdict_s5_search(parse: dict) -> tuple[str, str]:
    """s5_search: search_actions semantic via natural query.

    Verified: search_actions called (LLM chose semantic search over brute list).
    Inconclusive: list_actions with no category filter called (all-results fetch — not semantic).
    Refuted: invoke_action directly without prior search or list.
    """
    names = parse["tool_names"]
    if not parse["calls"]:
        return "blocked", "no LLM calls captured"

    has_search = "search_actions" in names
    list_action_cats = _parse_list_action_categories(parse["tool_calls"])
    list_calls = [tc for tc in parse["tool_calls"] if tc.get("name") == "list_actions"]

    if has_search:
        # Check if a meaningful query was passed
        search_calls = [tc for tc in parse["tool_calls"] if tc.get("name") == "search_actions"]
        queries = []
        for sc in search_calls:
            args_raw = sc.get("args")
            if isinstance(args_raw, str):
                try:
                    args_raw = json.loads(args_raw)
                except json.JSONDecodeError:
                    args_raw = {}
            if isinstance(args_raw, dict):
                q = args_raw.get("query", "")
                queries.append(q)
        query_str = "; ".join(queries)
        return "verified", f"search_actions called with query={query_str!r}"

    # list_actions with no category (or empty category) = full-fetch, not semantic
    no_category_list = any(cat is None or cat == [] or cat == "" for cat in list_action_cats)
    if list_calls and no_category_list:
        return "inconclusive", "list_actions(no-category) called — brute full-list, not semantic search_actions"

    if list_calls:
        return "inconclusive", f"list_actions called with specific category (not semantic): cats={list_action_cats}"

    if "invoke_action" in names:
        return "refuted", "invoke_action without search or list — skipped discovery entirely"

    return "refuted", f"no search_actions or list_actions call: {names}"


VERDICT_DISPATCH = {
    "s1a": lambda parse, events: verdict_s1a(parse),
    "s1b": lambda parse, events: verdict_s1b(parse),
    "s2": lambda parse, events: verdict_s2(parse, events),
    "s3_noop": lambda parse, events: verdict_s3_noop(parse),
    "s3_auto": lambda parse, events: verdict_s3_auto(parse),
    "s4_hot_cold": lambda parse, events: verdict_s4_hot_cold(parse),
    "s5_search": lambda parse, events: verdict_s5_search(parse),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Dogfood batch 24 driver — FP-0034 extended calibration")
    ap.add_argument("scenario", choices=list(SCENARIOS))
    ap.add_argument("--trace-dir", default="/tmp/reyn_b24")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--agent-name", default=None,
                    help="reyn agent name to attach to; auto-created if missing")
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
    print(f"  agent:  {args.agent_name or '(default)'}", file=sys.stderr)
    print(f"  trace:  {trace_file}", file=sys.stderr)
    print(f"  prompt: {sc['prompt']}", file=sys.stderr)

    # Auto-create agent in this cwd if a name is given and the profile dir is absent.
    if args.agent_name:
        agent_dir = cwd / ".reyn" / "agents" / args.agent_name
        if not agent_dir.exists():
            subprocess.run(
                ["reyn", "agent", "new", args.agent_name],
                cwd=str(cwd),
                check=True,
                capture_output=True,
                text=True,
            )

    chat = run_chat(sc["prompt"], trace_file, cwd, agent_name=args.agent_name, timeout=args.timeout)
    parse = parse_trace(trace_file)
    events_root = cwd / ".reyn" / "events"
    events = grep_routing_decided(events_root)

    verdict_fn = VERDICT_DISPATCH[args.scenario]
    verdict, reason = verdict_fn(parse, events)

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
        "finish_reasons": parse.get("finish_reasons", []),
        "first_turn_tool_names": parse.get("first_turn_tool_names", []),
        "first_turn_finish_reason": parse.get("first_turn_finish_reason", ""),
        "final_reply": parse.get("final_reply", "")[:1500],
        "routing_decided_events": len([e for e in events if e["event"].get("type") == "routing_decided"]),
        "routing_decided_sample": (events[0]["event"] if events else None),
    }
    print(f"RESULT:{json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
