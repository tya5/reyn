"""dogfood_long_session.py — long-lived session dogfood driver for Reyn.

Closes the G28 measurement gap: the existing per-run clean_state dogfood
pattern optimises for R1-type attractors (LLM refusal) but CANNOT expose
G12-type attractors (context-bloat empty completions) because it resets
in-memory ChatSession._history out of step with disk. A real production user
never resets; their session grows continuously. This driver mirrors that pattern.

Architecture:
  - Each scenario is a single chat session. Prompts are sent in order to the
    same A2A agent endpoint; history accumulates naturally across turns.
  - Multi-turn history persists server-side (Reyn's ChatSession) — the driver
    does NOT reset between turns.
  - For --n-shot N > 1, each shot uses a distinct agent name (e.g.,
    default-shot1, default-shot2) so each shot gets a truly fresh server-side
    session. The same agent name cannot be reused within a run without risk of
    history contamination.

Usage:
    python scripts/dogfood_long_session.py --scenarios dogfood/scenarios/long_session_v1.yaml
    python scripts/dogfood_long_session.py --scenarios <path> --agent default --turns 5
    python scripts/dogfood_long_session.py --scenarios <path> --n-shot 3 --json --out results.json
    python scripts/dogfood_long_session.py --scenarios <path> --web-url http://localhost:8080

Note on --n-shot:
    Each shot of the same scenario is sent to a different agent endpoint:
      shot 1 → /a2a/agents/<agent>-shot1
      shot 2 → /a2a/agents/<agent>-shot2
    These agents must exist in the Reyn registry (or be auto-created by the
    server). If they don't exist you'll get a JSON-RPC "Unknown agent" error —
    in that case use the default agent (n-shot=1) or pre-create the agents with
    `reyn agent new`.
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

# ── YAML loading (PyYAML expected; stdlib fallback for flat format) ─────────

def _load_yaml(path: Path) -> dict:
    """Load a YAML file. Uses PyYAML if available, raises ImportError otherwise."""
    try:
        import yaml  # type: ignore[import]
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        print(
            "[error] PyYAML not found. Install it with: pip install pyyaml",
            file=sys.stderr,
        )
        raise


# ── HTTP helpers (stdlib urllib — no new deps) ─────────────────────────────

def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, dict | None, str | None]:
    """POST a JSON payload to url. Returns (http_status, parsed_body, error_str)."""
    import urllib.error
    import urllib.request

    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "dogfood_long_session/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
    except TimeoutError:
        return 0, None, "timeout"
    except OSError as exc:
        return 0, None, f"network_error:{exc}"

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return status, None, f"invalid_json: {body_text[:120]}"

    return status, body, None


# ── A2A message/send ────────────────────────────────────────────────────────

def _build_message_send(text: str, message_id: str, rpc_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": message_id,
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def _extract_reply(body: dict) -> tuple[str | None, str | None]:
    """
    Returns (reply_text, error_message).
    reply_text is None on JSON-RPC error or missing result.
    """
    if "error" in body:
        err = body["error"]
        msg = err.get("message", "unknown error")
        code = err.get("code", "?")
        return None, f"jsonrpc_error({code}): {msg}"

    result = body.get("result")
    if result is None:
        return None, "missing result in response"

    # A2A Message envelope: result.parts[0].text
    parts = result.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            t = part.get("text", "")
            if isinstance(t, str):
                chunks.append(t)
    reply_text = "\n".join(chunks)
    response_message_id = result.get("messageId")
    return reply_text, None


# ── B-#2 (B41-NF-W7-2): wait-for-skill-completion semantics ─────────────────
#
# When a turn's A2A POST returns a spawn-ack reply (= the router exited the
# loop on skill spawn before the skill itself completed), the long-session
# driver previously moved on to the next turn immediately. For skills whose
# completion latency exceeds the HTTP timeout (e.g. read_local_files at 69.5s
# in B41 W7-S5), this lost the actual skill output and recorded only the
# 69-char spawn-ack as the turn reply. Subsequent turns then had no skill
# output in their context and degraded into inline-only routing.
#
# Fix: detect spawn-ack reply by substring, then poll the agent's events
# file for ``skill_completion_injected`` (= router completed narration) up
# to a configurable deadline. If found, re-extract the actual narration
# from agent history.jsonl as the turn reply.

_SPAWN_ACK_MARKERS = (
    # Substring-level detection so minor OS wording tweaks don't break the
    # match. See `_SPAWN_ACK_MSG` in src/reyn/runtime/router_loop.py for the
    # canonical en/ja forms.
    "is running in the background",
    "バックグラウンドで実行しています",
)


def _is_spawn_ack(reply_text: str) -> bool:
    """Return True when *reply_text* looks like the router's skill-spawn-ack."""
    if not reply_text:
        return False
    return any(marker in reply_text for marker in _SPAWN_ACK_MARKERS)


def _wait_for_skill_completion(
    events_path: Path,
    *,
    since_ts: float,
    deadline_s: float,
    poll_interval_s: float = 0.5,
) -> bool:
    """Poll *events_path* for a ``skill_completion_injected`` event newer than
    *since_ts*, returning True once observed or False after *deadline_s*.

    The events file is append-only JSONL written by the chat session; the
    driver's role here is read-only observation. Returns False on file
    absence (= agent may not have emitted any events yet — caller decides
    whether to keep waiting).
    """
    end_ts = time.time() + deadline_s
    while time.time() < end_ts:
        events = _read_events(events_path) if events_path.exists() else []
        for ev in events:
            if ev.get("type") != "skill_completion_injected":
                continue
            ev_ts = ev.get("timestamp")
            if ev_ts is None:
                continue
            # Events timestamps are ISO-8601 with timezone; convert to unix.
            try:
                ev_ts_unix = _iso_to_unix(ev_ts)
            except ValueError:
                continue
            if ev_ts_unix >= since_ts:
                return True
        time.sleep(poll_interval_s)
    return False


def _iso_to_unix(iso_ts: str) -> float:
    """Convert an ISO-8601 timestamp (with timezone) to unix seconds."""
    # ``datetime.fromisoformat`` handles "+09:00" style offsets natively in
    # 3.11+. The events log writer uses ``isoformat()`` on a tz-aware datetime.
    import datetime as _dt
    return _dt.datetime.fromisoformat(iso_ts).timestamp()


def _read_latest_assistant_text(history_path: Path) -> str | None:
    """Return the text of the last ``role=assistant`` entry in *history_path*.

    Used post-skill-completion to recover the router's narration that was
    produced after the original HTTP response returned with spawn-ack only.
    Returns None when no assistant entry is present or the file is missing.
    """
    if not history_path.exists():
        return None
    try:
        text = history_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    latest: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("role") != "assistant":
            continue
        content = rec.get("content")
        if isinstance(content, str) and content.strip():
            latest = content
    return latest


def _history_path(reyn_root: Path, agent_name: str) -> Path:
    return reyn_root / "agents" / agent_name / "history.jsonl"


# ── Events log reader ───────────────────────────────────────────────────────

def _events_dir(reyn_root: Path, agent_name: str) -> Path:
    return reyn_root / "events" / "agents" / agent_name / "chat"


def _latest_events_file(agent_name: str, reyn_root: Path) -> Path | None:
    """Return the most recent JSONL events file for the agent, or None."""
    d = _events_dir(reyn_root, agent_name)
    if not d.exists():
        return None
    # Month dirs sorted: YYYY-MM
    month_dirs = sorted(d.iterdir(), reverse=True)
    for month_dir in month_dirs:
        if not month_dir.is_dir():
            continue
        files = sorted(month_dir.glob("*.jsonl"), reverse=True)
        if files:
            return files[0]
    return None


def _read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
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


def _budget_ledger_path(reyn_root: Path) -> Path:
    return reyn_root / "state" / "budget_ledger.jsonl"


def _read_budget_entries(agent_name: str, reyn_root: Path, since_ts: float) -> list[dict]:
    """Read budget_ledger entries for agent_name written at or after since_ts (unix)."""
    ledger_path = _budget_ledger_path(reyn_root)
    entries: list[dict] = []
    if not ledger_path.exists():
        return entries
    try:
        text = ledger_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("agent") != agent_name:
            continue
        # Parse timestamp
        ts_str = rec.get("ts", "")
        try:
            if ts_str.endswith("+09:00"):
                dt = datetime.fromisoformat(ts_str)
            else:
                dt = datetime.fromisoformat(ts_str)
            ts = dt.timestamp()
        except (ValueError, OSError):
            ts = 0.0
        if ts >= since_ts:
            entries.append(rec)
    return entries


# ── Percentile helper ───────────────────────────────────────────────────────

def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * pct / 100.0)))
    return s[idx]


# ── Core: run one scenario ──────────────────────────────────────────────────

def run_scenario(
    scenario: dict,
    agent_name: str,
    web_url: str,
    max_turns: int,
    timeout: float,
    reyn_root: Path,
    turn_sleep: float = 0.5,
    skill_completion_timeout: float = 180.0,
) -> dict:
    """
    Execute one scenario: send prompts in order to the A2A endpoint,
    record per-turn metrics, harvest events after all turns.

    Returns a dict with keys:
      scenario_id, agent_name, prompts, turns (list of per-turn dicts),
      events_file, token_entries, error
    """
    scenario_id: str = scenario.get("id", "unknown")
    prompts: list[str] = scenario.get("prompts", [])
    if not prompts:
        return {
            "scenario_id": scenario_id,
            "agent_name": agent_name,
            "prompts": [],
            "turns": [],
            "error": "no prompts defined",
        }

    prompts = prompts[:max_turns]
    endpoint_url = f"{web_url.rstrip('/')}/a2a/agents/{agent_name}"
    start_wall = time.time()

    turns: list[dict] = []

    for turn_idx, prompt_text in enumerate(prompts, start=1):
        message_id = uuid.uuid4().hex
        rpc_id = turn_idx
        payload = _build_message_send(prompt_text, message_id, rpc_id)

        t0 = time.time()
        http_status, body, net_err = _post_json(endpoint_url, payload, timeout=timeout)
        elapsed = time.time() - t0

        turn_rec: dict[str, Any] = {
            "turn": turn_idx,
            "prompt": prompt_text,
            "message_id": message_id,
            "elapsed_s": round(elapsed, 3),
        }

        if net_err == "timeout":
            turn_rec.update({"status": "timeout", "reply_len": 0, "empty": True, "error": "timeout"})
        elif net_err is not None:
            turn_rec.update({"status": "error", "reply_len": 0, "empty": True, "error": net_err})
        elif body is None:
            turn_rec.update({"status": "error", "reply_len": 0, "empty": True, "error": "no body"})
        else:
            reply_text, rpc_error = _extract_reply(body)
            if rpc_error is not None:
                turn_rec.update({
                    "status": "rpc_error",
                    "reply_len": 0,
                    "empty": True,
                    "error": rpc_error,
                    "http_status": http_status,
                })
            else:
                reply_text = reply_text or ""
                # B-#2 (B41-NF-W7-2) wait-for-skill-completion: when the
                # router returned a spawn-ack (= it exited the loop before
                # the spawned skill finished), poll the events log for
                # ``skill_completion_injected`` and re-read the router
                # narration from the agent's history. Falls back to the
                # original spawn-ack reply on timeout so the turn metric is
                # never lost.
                if _is_spawn_ack(reply_text):
                    events_file_for_wait = _latest_events_file(agent_name, reyn_root)
                    completed = False
                    if events_file_for_wait is not None:
                        completed = _wait_for_skill_completion(
                            events_file_for_wait,
                            since_ts=t0,
                            deadline_s=skill_completion_timeout,
                        )
                    if completed:
                        # Brief settle window for the router narration turn
                        # to land in history.jsonl after the
                        # skill_completion_injected event fires.
                        time.sleep(0.5)
                        narrated = _read_latest_assistant_text(
                            _history_path(reyn_root, agent_name)
                        )
                        if narrated and narrated.strip() and not _is_spawn_ack(narrated):
                            reply_text = narrated
                            turn_rec["spawn_ack_resolved"] = True
                        else:
                            turn_rec["spawn_ack_resolved"] = False
                            turn_rec["spawn_ack_resolved_reason"] = (
                                "no narrated assistant message found"
                            )
                    else:
                        turn_rec["spawn_ack_resolved"] = False
                        turn_rec["spawn_ack_resolved_reason"] = (
                            f"timeout after {skill_completion_timeout}s "
                            "waiting for skill_completion_injected"
                        )
                empty = not reply_text.strip()
                turn_rec.update({
                    "status": "ok" if http_status < 400 else f"http_{http_status}",
                    "reply_len": len(reply_text),
                    "empty": empty,
                    "http_status": http_status,
                })

        turns.append(turn_rec)

        if turn_idx < len(prompts):
            time.sleep(turn_sleep)

    # ── harvest events + budget ledger ───────────────────────────────────
    events_file_path = _latest_events_file(agent_name, reyn_root)
    events_file = str(events_file_path) if events_file_path else None

    token_entries: list[dict] = []
    try:
        token_entries = _read_budget_entries(agent_name, reyn_root, since_ts=start_wall)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not read budget ledger for {agent_name!r}: {exc}", file=sys.stderr)

    tool_sequence: list[str] = []
    empty_stop_events = 0
    if events_file_path:
        try:
            evs = _read_events(events_file_path)
            for ev in evs:
                if ev.get("type") == "tool_called":
                    tool_name = (ev.get("data") or {}).get("tool")
                    if tool_name:
                        tool_sequence.append(tool_name)
                # Look for empty completion signals
                if ev.get("type") in ("compaction_check",):
                    pass  # informational only
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] could not read events for {agent_name!r}: {exc}", file=sys.stderr)

    return {
        "scenario_id": scenario_id,
        "description": scenario.get("description", ""),
        "kind": scenario.get("kind", ""),
        "agent_name": agent_name,
        "prompts": prompts,
        "turns": turns,
        "events_file": events_file,
        "token_entries": token_entries,
        "tool_sequence": tool_sequence,
        "empty_stop_events": empty_stop_events,
    }


# ── Stats aggregation ───────────────────────────────────────────────────────

def _compute_scenario_stats(result: dict) -> dict:
    turns = result.get("turns", [])
    n = len(turns)
    if n == 0:
        return {"total_turns": 0, "empty_count": 0, "empty_rate": 0.0}

    empty_count = sum(1 for t in turns if t.get("empty"))
    latencies = [t["elapsed_s"] for t in turns if "elapsed_s" in t]
    token_entries = result.get("token_entries", [])
    total_tokens = sum(e.get("tokens", 0) for e in token_entries)

    return {
        "total_turns": n,
        "ok_count": sum(1 for t in turns if t.get("status") == "ok"),
        "empty_count": empty_count,
        "empty_rate": round(empty_count / n, 3),
        "latency_p50": round(_percentile(latencies, 50), 2),
        "latency_p90": round(_percentile(latencies, 90), 2),
        "total_tokens": total_tokens,
        "llm_calls": len(token_entries),
    }


def _compute_summary_stats(results: list[dict]) -> dict:
    """Aggregate across all scenario results."""
    all_turns: list[dict] = []
    for r in results:
        all_turns.extend(r.get("turns", []))

    n_scenarios = len(results)
    n_turns = len(all_turns)
    n_empty = sum(1 for t in all_turns if t.get("empty"))
    latencies = [t["elapsed_s"] for t in all_turns if "elapsed_s" in t]

    # Empty rate by turn position (1-indexed)
    max_turn_pos = max((t.get("turn", 0) for t in all_turns), default=0)
    empty_by_position: dict[int, dict] = {}
    for pos in range(1, max_turn_pos + 1):
        at_pos = [t for t in all_turns if t.get("turn") == pos]
        empty_at = [t for t in at_pos if t.get("empty")]
        empty_by_position[pos] = {
            "total": len(at_pos),
            "empty": len(empty_at),
            "rate": round(len(empty_at) / len(at_pos), 3) if at_pos else 0.0,
        }

    # Token growth: first vs last token entry across scenarios
    all_token_entries: list[dict] = []
    for r in results:
        all_token_entries.extend(r.get("token_entries", []))

    return {
        "n_scenarios": n_scenarios,
        "n_turns": n_turns,
        "n_empty": n_empty,
        "empty_rate": round(n_empty / n_turns, 3) if n_turns else 0.0,
        "latency_p50": round(_percentile(latencies, 50), 2),
        "latency_p90": round(_percentile(latencies, 90), 2),
        "empty_by_turn_position": empty_by_position,
        "total_llm_calls": len(all_token_entries),
        "total_tokens": sum(e.get("tokens", 0) for e in all_token_entries),
    }


# ── Text output formatter ───────────────────────────────────────────────────

_CHECK = "✓"
_WARN = "⚠ EMPTY"
_ERR = "✗ ERR"


def _status_icon(turn: dict) -> str:
    if turn.get("empty"):
        if turn.get("status") in ("timeout", "error", "rpc_error"):
            return _ERR
        return _WARN
    return _CHECK


def _format_scenario_block(result: dict, stats: dict) -> list[str]:
    sid = result["scenario_id"]
    n = stats["total_turns"]
    lines: list[str] = [f"\n━━━ {sid} ({n} turns) ━━━"]

    for t in result.get("turns", []):
        icon = _status_icon(t)
        reply_len = t.get("reply_len", 0)
        elapsed = t.get("elapsed_s", 0.0)
        err = t.get("error", "")
        err_str = f"  [{err}]" if err else ""
        lines.append(f"  Turn {t['turn']}: {reply_len} chars in {elapsed:.1f}s {icon}{err_str}")

    empty_pct = int(stats["empty_rate"] * 100)
    empty_str = f"{empty_pct}% ({stats['empty_count']}/{n})"
    tok_str = f"{stats['total_tokens']} tokens / {stats['llm_calls']} calls"
    lines.append(
        f"  Stats: empty_rate={empty_str}, latency_p50={stats['latency_p50']}s, {tok_str}"
    )
    return lines


def _format_summary(summary: dict, agent: str, web_url: str, n_shot: int) -> list[str]:
    n_s = summary["n_scenarios"]
    lines: list[str] = [
        "",
        "═" * 60,
        f" Summary: {n_s} scenarios, {summary['n_turns']} turns total, "
        f"{summary['n_empty']} empty ({int(summary['empty_rate']*100)}%), "
        f"latency p50={summary['latency_p50']}s",
        " Empty by turn position:",
    ]
    for pos, info in sorted(summary["empty_by_turn_position"].items()):
        pct = int(info["rate"] * 100)
        lines.append(f"   turn_{pos}: {info['empty']}/{info['total']} ({pct}%)")

    lines.append(
        f" LLM calls: {summary['total_llm_calls']}, total tokens: {summary['total_tokens']}"
    )
    lines.append("═" * 60)
    return lines


def print_text_report(
    results: list[dict],
    summary: dict,
    agent: str,
    web_url: str,
    n_shot: int,
    out_path: Path | None,
) -> None:
    n_s = summary["n_scenarios"]
    header_lines = [
        f"=== Long-session dogfood: {n_s} scenarios × N={n_shot} shot{'s' if n_shot > 1 else ''} ===",
        f"Target: {web_url.rstrip('/')}/a2a/agents/{agent}",
    ]

    scenario_lines: list[str] = []
    for result in results:
        stats = _compute_scenario_stats(result)
        scenario_lines.extend(_format_scenario_block(result, stats))

    summary_lines = _format_summary(summary, agent, web_url, n_shot)

    all_lines = header_lines + scenario_lines + summary_lines
    output = "\n".join(all_lines) + "\n"

    if out_path:
        out_path.write_text(output, encoding="utf-8")
        print(output)
    else:
        print(output)


# ── Main ────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dogfood_long_session.py",
        description="Long-lived session dogfood driver — closes the G28 measurement gap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--scenarios", required=True, metavar="PATH",
        help="Path to scenarios YAML file.",
    )
    p.add_argument(
        "--agent", default="default", metavar="NAME",
        help="Target agent name (default: default). For --n-shot > 1, each shot "
             "uses <agent>-shot<N>.",
    )
    p.add_argument(
        "--web-url", default="http://localhost:8080", metavar="URL",
        help="A2A endpoint base URL (default: http://localhost:8080).",
    )
    p.add_argument(
        "--turns", type=int, default=None, metavar="N",
        help="Max turns per scenario (default: read from scenario file, or 10).",
    )
    p.add_argument(
        "--n-shot", type=int, default=1, metavar="N",
        help="Replicate each scenario N times. Each shot uses a distinct agent "
             "(<agent>-shot1 ... <agent>-shotN). Agents must exist in the registry.",
    )
    p.add_argument(
        "--out", metavar="PATH",
        help="Write output to this file (text or JSON depending on --json).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit structured JSON instead of human-readable text.",
    )
    p.add_argument(
        "--timeout", type=float, default=90.0, metavar="SEC",
        help="Per-turn HTTP timeout in seconds (default: 90).",
    )
    p.add_argument(
        "--reyn-root", default=".reyn", metavar="PATH",
        help="Path to .reyn workspace root, for reading events (default: .reyn).",
    )
    p.add_argument(
        "--turn-sleep", type=float, default=0.5, metavar="SEC",
        help="Sleep between turns in seconds (default: 0.5). Simulates human pace.",
    )
    p.add_argument(
        "--skill-completion-timeout", type=float, default=180.0, metavar="SEC",
        help=(
            "When the A2A POST returns a skill spawn-ack, wait up to this many "
            "seconds for the background skill's completion narration before "
            "moving to the next turn (default: 180). Setting to 0 disables the "
            "wait (= record the spawn-ack reply verbatim, restoring pre-B41 "
            "behavior). B41-NF-W7-2."
        ),
    )
    return p.parse_args(argv)


def _load_scenarios_file(path: Path) -> tuple[list[dict], int]:
    """Load and validate the scenarios YAML. Returns (scenarios_list, default_turns)."""
    if not path.exists():
        print(f"[error] scenarios file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = _load_yaml(path)
    except Exception as exc:
        print(f"[error] could not parse YAML at {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"[error] scenarios YAML must be a mapping, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)

    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        print("[error] scenarios YAML must contain a non-empty 'scenarios' list", file=sys.stderr)
        sys.exit(1)

    for i, s in enumerate(scenarios):
        if not isinstance(s, dict):
            print(f"[error] scenario[{i}] must be a mapping", file=sys.stderr)
            sys.exit(1)
        if "id" not in s:
            print(f"[error] scenario[{i}] missing required field 'id'", file=sys.stderr)
            sys.exit(1)
        if "prompts" not in s or not isinstance(s["prompts"], list):
            print(f"[error] scenario {s.get('id')!r} missing 'prompts' list", file=sys.stderr)
            sys.exit(1)

    meta = data.get("metadata") or {}
    default_turns = meta.get("default_turns", 10)
    if not isinstance(default_turns, int):
        default_turns = 10

    return scenarios, default_turns


def _check_server(web_url: str, agent: str, timeout: float) -> bool:
    """Quick health check: try to GET /a2a/agents. Returns True if reachable."""
    list_url = f"{web_url.rstrip('/')}/a2a/agents"
    try:
        import urllib.request
        with urllib.request.urlopen(
            urllib.request.Request(list_url, headers={"User-Agent": "dogfood_long_session/1.0"}),
            timeout=min(timeout, 10.0),
        ) as resp:
            return resp.getcode() == 200
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    scenarios_path = Path(args.scenarios)
    reyn_root = Path(args.reyn_root)
    out_path = Path(args.out) if args.out else None

    # Load scenarios
    scenarios, default_turns = _load_scenarios_file(scenarios_path)
    max_turns = args.turns if args.turns is not None else default_turns

    # Server reachability check
    if not _check_server(args.web_url, args.agent, args.timeout):
        print(
            f"[error] reyn web server not reachable at {args.web_url}. "
            "Start it with: reyn web",
            file=sys.stderr,
        )
        print(
            "        Continuing anyway — each turn will record status=error.",
            file=sys.stderr,
        )

    # Build agent names for n-shot
    n_shot = max(1, args.n_shot)
    if n_shot == 1:
        agent_names = [args.agent]
    else:
        agent_names = [f"{args.agent}-shot{i}" for i in range(1, n_shot + 1)]

    print(
        f"[info] {len(scenarios)} scenarios × {n_shot} shot(s) × ≤{max_turns} turns",
        file=sys.stderr,
    )
    print(f"[info] target: {args.web_url}/a2a/agents/<agent>", file=sys.stderr)

    # Run
    all_results: list[dict] = []

    for shot_idx, agent_name in enumerate(agent_names, start=1):
        if n_shot > 1:
            print(f"[info] === shot {shot_idx}/{n_shot} (agent={agent_name}) ===", file=sys.stderr)

        for s_idx, scenario in enumerate(scenarios, start=1):
            sid = scenario.get("id", f"scenario_{s_idx}")
            print(f"[info]   {sid} ...", file=sys.stderr, end="", flush=True)

            result = run_scenario(
                scenario=scenario,
                agent_name=agent_name,
                web_url=args.web_url,
                max_turns=max_turns,
                timeout=args.timeout,
                reyn_root=reyn_root,
                turn_sleep=args.turn_sleep,
                skill_completion_timeout=args.skill_completion_timeout,
            )
            result["shot"] = shot_idx
            all_results.append(result)

            stats = _compute_scenario_stats(result)
            empty_rate_pct = int(stats["empty_rate"] * 100)
            print(
                f" done ({stats['total_turns']} turns, empty={empty_rate_pct}%)",
                file=sys.stderr,
            )

    summary = _compute_summary_stats(all_results)

    # Output
    if args.json:
        output_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scenarios_file": str(scenarios_path),
            "agent": args.agent,
            "web_url": args.web_url,
            "n_shot": n_shot,
            "max_turns": max_turns,
            "results": all_results,
            "summary": summary,
        }
        output_text = json.dumps(output_data, indent=2, ensure_ascii=False) + "\n"
        if out_path:
            out_path.write_text(output_text, encoding="utf-8")
        else:
            sys.stdout.write(output_text)
    else:
        print_text_report(
            results=all_results,
            summary=summary,
            agent=args.agent,
            web_url=args.web_url,
            n_shot=n_shot,
            out_path=out_path,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
