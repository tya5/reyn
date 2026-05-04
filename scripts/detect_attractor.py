"""detect_attractor.py — machine-detect LLM attractor / rule violation from trace dump.

Reads a JSONL trace file produced by REYN_LLM_TRACE_DUMP and applies heuristic
checks to each request/response pair to flag attractor patterns observed during
dogfood (e.g. RETRO-H4: empty response despite injected MUST rule).

Usage:
    python scripts/detect_attractor.py --trace <jsonl_path>
    python scripts/detect_attractor.py --trace <jsonl_path> --heuristics stop_must,enum,tool_name
    python scripts/detect_attractor.py --trace <jsonl_path> --output-format json
    python scripts/detect_attractor.py --trace <jsonl_path> --summary-only
    python scripts/detect_attractor.py --trace <jsonl_path> --filter-caller router
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# MUST rule keyword pattern (Heuristic 1)
# Matches lines containing action-directive language in English or Japanese.
# Precision-tuned to avoid matching passive descriptive text.
# ---------------------------------------------------------------------------
_MUST_PATTERN = re.compile(
    r"(?:MUST|must call|must use|should call|should use|必ず|してください|を呼び出|を使用してください)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Heuristic names (canonical)
# ---------------------------------------------------------------------------
HEURISTIC_STOP_MUST = "stop_with_must_rule"
HEURISTIC_ENUM = "enum_violation"
HEURISTIC_TOOL_NAME = "tool_name_hallucinate"

ALL_HEURISTICS = [HEURISTIC_STOP_MUST, HEURISTIC_ENUM, HEURISTIC_TOOL_NAME]


# ---------------------------------------------------------------------------
# JSONL loading
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read trace file: {exc}", file=sys.stderr)
        sys.exit(1)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


# ---------------------------------------------------------------------------
# Pairing: request + response by request_id
# ---------------------------------------------------------------------------


def _pair_records(records: list[dict]) -> list[tuple[dict, dict | None]]:
    """Return (request, response_or_None) pairs in timestamp order."""
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


# ---------------------------------------------------------------------------
# Relative timestamp helper
# ---------------------------------------------------------------------------


def _rel_seconds(base_ts: str | None, ts: str | None) -> str:
    if not base_ts or not ts:
        return ts[:19] if ts else "?"
    try:
        def _parse(s: str) -> datetime:
            s_clean = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s_clean)
            except Exception:
                return datetime.strptime(s_clean[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )

        delta = _parse(ts) - _parse(base_ts)
        return f"T+{delta.total_seconds():.1f}s"
    except Exception:
        return ts[:19] if ts else "?"


# ---------------------------------------------------------------------------
# System-prompt extraction helpers
# ---------------------------------------------------------------------------


def _extract_system_text(messages: list[dict]) -> str:
    """Return concatenated text content of all system-role messages."""
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts)


def _find_must_lines(text: str) -> list[str]:
    """Return lines from *text* that match _MUST_PATTERN (de-duped, max 2)."""
    seen: set[str] = set()
    result: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        if _MUST_PATTERN.search(stripped):
            seen.add(stripped)
            result.append(stripped)
            if len(result) >= 2:
                break
    return result


# ---------------------------------------------------------------------------
# Response emptiness check (Heuristic 1)
# ---------------------------------------------------------------------------


def _is_empty_response(resp: dict) -> bool:
    """Return True if the response carries no meaningful output."""
    finish = resp.get("finish_reason")
    if finish != "stop":
        return False
    usage = resp.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    if completion_tokens is not None and completion_tokens == 0:
        return True
    content = resp.get("content")
    tool_calls = resp.get("tool_calls") or []
    if not tool_calls and (content is None or content == ""):
        return True
    return False


# ---------------------------------------------------------------------------
# Tool schema helpers for Heuristic 2 and 3
# ---------------------------------------------------------------------------


def _extract_tool_names(tools: list[dict]) -> set[str]:
    names: set[str] = set()
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get("name")
        if name:
            names.add(name)
    return names


def _find_enum_for_arg(tools: list[dict], tool_name: str, arg_name: str) -> list[Any] | None:
    """Return the enum list for *arg_name* in the tool definition of *tool_name*, or None."""
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name") != tool_name:
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        prop = props.get(arg_name) or {}
        enum = prop.get("enum")
        if enum is not None:
            return enum
    return None


# ---------------------------------------------------------------------------
# Heuristic implementations
# ---------------------------------------------------------------------------


def _check_stop_must(req: dict, resp: dict | None) -> dict | None:
    """Heuristic 1: empty response despite MUST rule in system prompt."""
    if resp is None:
        return None
    if not _is_empty_response(resp):
        return None
    messages = req.get("messages") or []
    system_text = _extract_system_text(messages)
    must_lines = _find_must_lines(system_text)
    if not must_lines:
        return None
    usage = resp.get("usage") or {}
    return {
        "heuristic": HEURISTIC_STOP_MUST,
        "evidence": {
            "finish_reason": resp.get("finish_reason"),
            "completion_tokens": usage.get("completion_tokens"),
            "must_rule_excerpts": must_lines,
        },
    }


def _check_enum_violation(req: dict, resp: dict | None) -> dict | None:
    """Heuristic 2: tool call argument violates enum constraint in the request schema."""
    if resp is None:
        return None
    tools = req.get("tools") or []
    if not tools:
        return None
    tool_calls = resp.get("tool_calls") or []
    if not tool_calls:
        return None

    # For each tool call, inspect its arguments for enum violations.
    for tc in tool_calls:
        fn = tc.get("function") or {}
        tool_name = fn.get("name", "")
        raw_args = fn.get("arguments", "")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}

        for arg_name, arg_value in args.items():
            enum = _find_enum_for_arg(tools, tool_name, arg_name)
            if enum is None:
                continue
            if arg_value not in enum:
                field_path = f"tools[?].function.parameters.properties.{arg_name}"
                return {
                    "heuristic": HEURISTIC_ENUM,
                    "evidence": {
                        "tool_name": tool_name,
                        "field_path": field_path,
                        "expected_enum": enum,
                        "actual_value": arg_value,
                    },
                }
    return None


def _check_tool_name_hallucinate(req: dict, resp: dict | None) -> dict | None:
    """Heuristic 3: tool_call names a function not present in the request tools list."""
    if resp is None:
        return None
    tools = req.get("tools") or []
    if not tools:
        return None
    tool_calls = resp.get("tool_calls") or []
    if not tool_calls:
        return None

    available = _extract_tool_names(tools)
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        if name and name not in available:
            return {
                "heuristic": HEURISTIC_TOOL_NAME,
                "evidence": {
                    "hallucinated_name": name,
                    "available_names": sorted(available),
                },
            }
    return None


# ---------------------------------------------------------------------------
# Detection runner
# ---------------------------------------------------------------------------

_HEURISTIC_FNS = {
    HEURISTIC_STOP_MUST: _check_stop_must,
    HEURISTIC_ENUM: _check_enum_violation,
    HEURISTIC_TOOL_NAME: _check_tool_name_hallucinate,
}


def detect(
    pairs: list[tuple[dict, dict | None]],
    *,
    heuristics: list[str],
    filter_caller: str | None,
) -> list[dict]:
    """Run heuristics on all pairs and return detection records."""
    base_ts: str | None = pairs[0][0].get("timestamp") if pairs else None
    detections: list[dict] = []

    for req, resp in pairs:
        caller = req.get("caller_hint", "")
        if filter_caller and caller != filter_caller:
            continue
        req_id = req.get("request_id", "")
        ts = req.get("timestamp")
        rel = _rel_seconds(base_ts, ts)

        for h_name in heuristics:
            fn = _HEURISTIC_FNS.get(h_name)
            if fn is None:
                continue
            result = fn(req, resp)
            if result is not None:
                detections.append({
                    "request_id": req_id,
                    "timestamp": ts,
                    "rel_time": rel,
                    "caller": caller,
                    **result,
                })

    return detections


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _summary_counts(detections: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in detections:
        h = d["heuristic"]
        counts[h] = counts.get(h, 0) + 1
    return counts


def _format_pretty(
    trace_path: Path,
    pairs: list[tuple[dict, dict | None]],
    detections: list[dict],
    *,
    summary_only: bool,
) -> str:
    total = len(pairs)
    n_det = len(detections)
    pct = round(n_det / total * 100) if total else 0
    counts = _summary_counts(detections)

    lines: list[str] = []
    lines.append("=== Attractor Detection Report ===")
    lines.append(f"Trace file: {trace_path}")
    lines.append(f"Total LLM calls: {total}")
    lines.append(f"Detected attractors: {n_det} ({pct}%)")

    if counts:
        lines.append("")
        lines.append("By heuristic:")
        for h_name in ALL_HEURISTICS:
            c = counts.get(h_name, 0)
            if c:
                h_pct = round(c / total * 100) if total else 0
                lines.append(f"  {h_name + ':':30s} {c} ({h_pct}%)")
    else:
        lines.append("  (none)")

    if summary_only:
        return "\n".join(lines)

    if detections:
        lines.append("")
        lines.append("=== Detail ===")
        for d in detections:
            rel = d.get("rel_time", "?")
            caller = d.get("caller", "unknown")
            h = d["heuristic"]
            req_id = d.get("request_id", "?")
            lines.append("")
            lines.append(f"[{rel}  {caller}] {h} (request_id={req_id})")
            ev = d.get("evidence", {})
            if h == HEURISTIC_STOP_MUST:
                excerpts = ev.get("must_rule_excerpts", [])
                finish = ev.get("finish_reason", "?")
                ct = ev.get("completion_tokens", "?")
                for ex in excerpts:
                    lines.append(f"  MUST rule found: \"{ex}\"")
                lines.append(f"  Response: finish={finish}, completion_tokens={ct}")
            elif h == HEURISTIC_ENUM:
                lines.append(f"  Tool: {ev.get('tool_name','?')}")
                lines.append(f"  Field: {ev.get('field_path','?')}")
                enum = ev.get("expected_enum", [])
                actual = ev.get("actual_value")
                lines.append(f"  Expected enum: {enum}")
                lines.append(f"  Actual value:  {actual!r}")
            elif h == HEURISTIC_TOOL_NAME:
                lines.append(f"  Hallucinated name: \"{ev.get('hallucinated_name','?')}\"")
                avail = ev.get("available_names", [])
                avail_str = json.dumps(avail, ensure_ascii=False)
                lines.append(f"  Available names: {avail_str}")

    return "\n".join(lines)


def _format_json(
    trace_path: Path,
    pairs: list[tuple[dict, dict | None]],
    detections: list[dict],
    *,
    summary_only: bool,
) -> str:
    total = len(pairs)
    counts = _summary_counts(detections)
    output: dict[str, Any] = {
        "trace_file": str(trace_path),
        "total_calls": total,
        "summary": {h: counts.get(h, 0) for h in ALL_HEURISTICS},
    }
    if not summary_only:
        output["detections"] = [
            {
                "request_id": d.get("request_id"),
                "timestamp": d.get("timestamp"),
                "rel_time": d.get("rel_time"),
                "caller": d.get("caller"),
                "heuristic": d["heuristic"],
                "evidence": d.get("evidence", {}),
            }
            for d in detections
        ]
    return json.dumps(output, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public module API (importable by other scripts)
# ---------------------------------------------------------------------------


def detect_all_attractors(
    records: list[dict],
    *,
    heuristics: list[str] | None = None,
    filter_caller: str | None = None,
) -> list[dict]:
    """Detect attractors in *records* and return a list of detection dicts.

    This is the module-level public API intended for import by other scripts
    (e.g. llm_replay.py) so they can reuse heuristic logic without subprocess
    invocation.

    Args:
        records: Raw JSONL records (a list of dicts with ``kind`` field).
        heuristics: Canonical heuristic names to run.  ``None`` runs all.
        filter_caller: If set, only inspect records whose ``caller_hint``
            matches this value exactly.

    Returns:
        List of detection dicts, each containing at minimum:
        ``request_id``, ``timestamp``, ``rel_time``, ``caller``,
        ``heuristic``, ``evidence``.
    """
    active = heuristics if heuristics is not None else list(ALL_HEURISTICS)
    pairs = _pair_records(records)
    return detect(pairs, heuristics=active, filter_caller=filter_caller)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_HEURISTIC_ALIASES = {
    "stop_must": HEURISTIC_STOP_MUST,
    "stop_with_must_rule": HEURISTIC_STOP_MUST,
    "enum": HEURISTIC_ENUM,
    "enum_violation": HEURISTIC_ENUM,
    "tool_name": HEURISTIC_TOOL_NAME,
    "tool_name_hallucinate": HEURISTIC_TOOL_NAME,
}


def _parse_heuristics(raw: str | None) -> list[str]:
    if not raw:
        return list(ALL_HEURISTICS)
    names: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if not part:
            continue
        canonical = _HEURISTIC_ALIASES.get(part)
        if canonical is None:
            print(
                f"warning: unknown heuristic '{part}'; valid: {', '.join(_HEURISTIC_ALIASES)}",
                file=sys.stderr,
            )
            continue
        if canonical not in names:
            names.append(canonical)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(
        description="detect_attractor — machine-detect LLM rule violations from trace dump"
    )
    parser.add_argument("--trace", required=True, help="Path to JSONL trace file (required)")
    parser.add_argument(
        "--heuristics",
        default=None,
        metavar="LIST",
        help=(
            "Comma-separated list of heuristics to run "
            "(default: all). Values: stop_must, enum, tool_name"
        ),
    )
    parser.add_argument(
        "--output-format",
        choices=["pretty", "json"],
        default="pretty",
        help="Output format: pretty (default) or json",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        default=False,
        help="Print summary counts only, not per-detection detail",
    )
    parser.add_argument(
        "--filter-caller",
        default=None,
        metavar="NAME",
        help="Only inspect records where caller_hint matches NAME",
    )
    args = parser.parse_args()

    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    heuristics = _parse_heuristics(args.heuristics)
    if not heuristics:
        print("error: no valid heuristics specified", file=sys.stderr)
        sys.exit(1)

    records = _load_jsonl(trace_path)
    pairs = _pair_records(records)

    if not pairs:
        print("no LLM request records found in trace file")
        sys.exit(0)

    detections = detect(pairs, heuristics=heuristics, filter_caller=args.filter_caller)

    if args.output_format == "json":
        print(_format_json(trace_path, pairs, detections, summary_only=args.summary_only))
    else:
        print(_format_pretty(trace_path, pairs, detections, summary_only=args.summary_only))


if __name__ == "__main__":
    main()
