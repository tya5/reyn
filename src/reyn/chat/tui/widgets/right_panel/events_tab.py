"""Events tab — renders recent OS events from .reyn/events/*.jsonl."""
from __future__ import annotations

import json
from pathlib import Path

from .base import _CORAL, _esc, logger

_EVENT_COLORS: dict[str, str] = {
    "phase_started":               "#44cc88",
    "phase_completed":             "#44cc88",
    "control_decided":             "#88ddaa",
    "context_built":               "#335544",
    "llm_called":                  "#ffcc66",
    "llm_response_received":       "#ffcc66",
    "artifact_created":            "#88aaff",
    "artifact_validated":          "#88aaff",
    "validation_error":            "#ff6644",
    "phase_retry":                 "#ff6644",
    "permission_denied":           "#ff4444",
    "router_retry_exhausted":      "#ff4444",
    "tool_failed":                 "#ff6644",
    "tool_called":                 "#cc88ff",
    "tool_returned":               "#cc88ff",
    "mcp_called":                  "#cc88ff",
    "mcp_completed":               "#cc88ff",
    "act_executed":                "#cc88ff",
    "skill_run_spawned":           "#88aaff",
    "skill_run_completed":         "#88aaff",
    "workflow_started":            "#88aaff",
    "workflow_finished":           "#88aaff",
    "agent_message_sent":          "#aaaaaa",
    "agent_request_received":      "#aaaaaa",
    "agent_response_received":     "#aaaaaa",
    "user_message_received":       "#dddddd",
    "chat_started":                "#dddddd",
    "chat_stopped":                "#dddddd",
    "user_intervention_requested": "#ffcc88",
    "user_intervention_received":  "#ffcc88",
    "preprocessor_step_started":   "#555555",
    "preprocessor_step_completed": "#555555",
    "python_step_started":         "#555555",
    "python_step_completed":       "#555555",
    "web_fetch_started":           "#888888",
    "web_fetch_completed":         "#888888",
    "web_search_started":          "#888888",
    "web_search_completed":        "#888888",
    "workspace_updated":           "#555555",
    "compaction_check":            "#555555",
}
_DEFAULT_EVENT_COLOR = "#666666"

# Each tuple is (label, frozenset-of-types). Empty set = show all.
_FILTER_GROUPS: list[tuple[str, frozenset]] = [
    ("all",   frozenset()),
    ("phase", frozenset({
        "phase_started", "phase_completed", "control_decided", "context_built",
        "artifact_created", "artifact_validated",
    })),
    ("llm",   frozenset({"llm_called", "llm_response_received"})),
    ("tool",  frozenset({
        "tool_called", "tool_returned", "tool_failed",
        "mcp_called", "mcp_completed", "act_executed",
    })),
    ("skill", frozenset({
        "skill_run_spawned", "skill_run_completed",
        "workflow_started", "workflow_finished",
        "agent_message_sent", "agent_request_received", "agent_response_received",
    })),
    ("error", frozenset({
        "validation_error", "phase_retry", "permission_denied",
        "router_retry_exhausted", "tool_failed",
    })),
    ("user", frozenset({
        "user_message_received",
        "user_intervention_requested", "user_intervention_received",
        "chat_started", "chat_stopped",
    })),
]

_TAIL_CYCLE: list[int] = [30, 50, 100, 200]


def _event_hint(ev: dict) -> str:
    """Return a short plain-text annotation of the most useful data fields."""
    t = ev.get("type", "")
    d = ev.get("data") or {}

    if t == "phase_started":
        return d.get("phase", "")
    if t == "phase_completed":
        nxt = d.get("next") or "finish"
        conf = d.get("confidence", 0)
        return f"{d.get('phase', '')} → {nxt} ({conf:.0%})"
    if t == "control_decided":
        nxt = d.get("next_phase") or ""
        suffix = f" → {nxt}" if nxt else ""
        return f"{d.get('phase', '')}: {d.get('decision', '')}{suffix}"
    if t == "llm_called":
        return f"{d.get('phase', '')} [{d.get('model', '')}]"
    if t == "llm_response_received":
        pt = d.get("prompt_tokens", 0)
        ct = d.get("completion_tokens", 0)
        cost = d.get("cost_usd", 0)
        return f"{pt}+{ct}t ${cost:.4f}"
    if t == "artifact_created":
        return f"{d.get('artifact_type', '')} @ {d.get('phase', '')}"
    if t == "artifact_validated":
        errors = d.get("errors") or []
        at = d.get("artifact_type", "")
        return f"{at} ✗ {len(errors)} err" if errors else f"{at} ✓"
    if t == "validation_error":
        return f"{d.get('phase', '')}: {str(d.get('error', ''))[:35]}"
    if t == "phase_retry":
        return f"attempt {d.get('attempt', '?')}/{d.get('max_retries', '?')}: {str(d.get('error', ''))[:25]}"
    if t == "permission_denied":
        return f"{d.get('kind', '')} {d.get('path', '')}"
    if t in ("tool_called", "tool_returned"):
        return d.get("tool", "")
    if t == "tool_failed":
        return f"{d.get('tool', '')}: {str(d.get('message', ''))[:25]}"
    if t in ("mcp_called", "mcp_completed"):
        suffix = " ✗" if d.get("is_error") else ""
        return f"{d.get('server', '')}.{d.get('tool', '')}{suffix}"
    if t == "workflow_started":
        run_id = str(d.get("run_id", ""))[:8]
        return f"{d.get('skill', '')} [{run_id}]"
    if t == "workflow_finished":
        conf = d.get("confidence", 0)
        return f"{d.get('skill', '')} ({conf:.0%})"
    if t == "skill_run_spawned":
        return d.get("skill", "")
    if t == "skill_run_completed":
        return f"{d.get('skill', '')} [{d.get('status', '')}]"
    if t == "agent_message_sent":
        return f"{d.get('from_agent', '')} → {d.get('to_agent', '')}"
    if t in ("agent_request_received", "agent_response_received"):
        return d.get("from_agent", "")
    if t == "user_message_received":
        text = str(d.get("text", ""))
        return text[:40] + ("…" if len(text) > 40 else "")
    if t == "user_intervention_requested":
        return str(d.get("question", ""))[:40]
    if t == "user_intervention_received":
        return str(d.get("answer", ""))[:40]
    if t == "web_fetch_started":
        return str(d.get("url", ""))[:45]
    if t == "web_fetch_completed":
        return f"HTTP {d.get('status_code', '')} {d.get('content_length', '')}b"
    if t == "web_search_started":
        return str(d.get("query", ""))[:40]
    if t == "web_search_completed":
        return f"{d.get('result_count', '')} results"
    return ""


def _load_chain_replies(project_root: Path) -> dict[str, str]:
    """Return {chain_id: last_agent_reply_text} from all agents' history files."""
    replies: dict[str, str] = {}
    agents_dir = project_root / ".reyn" / "agents"
    if not agents_dir.is_dir():
        return replies
    for hist in agents_dir.glob("*/history.jsonl"):
        try:
            for raw in hist.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    m = json.loads(raw)
                    if m.get("role") == "agent":
                        cid = (m.get("meta") or {}).get("chain_id")
                        if cid:
                            replies[cid] = m.get("text", "")
                except Exception as exc:
                    logger.warning(
                        "right_panel events: malformed history.jsonl line in %s: %s",
                        hist, exc,
                    )
        except Exception as exc:
            logger.warning(
                "right_panel events: read of %s failed: %s", hist, exc,
            )
    return replies


def _load_events_cached(
    project_root: Path,
    cache: dict[Path, tuple[float, list[dict]]],
) -> list[dict]:
    """Read all events/.jsonl with file-mtime caching (perf).

    Each .jsonl file is parsed once and re-parsed only when its mtime
    changes — events panel re-renders every 2 s and the events directory
    grows monotonically with each turn, so re-reading every file every
    refresh adds up.
    """
    events_root = project_root / ".reyn" / "events"
    if not events_root.is_dir():
        return []
    all_events: list[dict] = []
    seen: set[Path] = set()
    for jsonl in sorted(events_root.rglob("*.jsonl")):
        seen.add(jsonl)
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        cached = cache.get(jsonl)
        if cached is not None and cached[0] == mtime:
            all_events.extend(cached[1])
            continue
        parsed: list[dict] = []
        try:
            for raw in jsonl.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed.append(json.loads(raw))
                except Exception as exc:
                    logger.warning(
                        "right_panel events: malformed event JSON in %s: %s",
                        jsonl, exc,
                    )
        except Exception as exc:
            logger.warning("right_panel events: read of %s failed: %s", jsonl, exc)
            continue
        cache[jsonl] = (mtime, parsed)
        all_events.extend(parsed)
    # Drop cache entries for files that no longer exist
    for stale in [p for p in cache if p not in seen]:
        cache.pop(stale, None)
    return all_events


def render_events(
    project_root: Path | None,
    event_filter_idx: int,
    event_tail_idx: int,
    *,
    cursor: int = 0,
    cache: dict | None = None,
) -> tuple[str, list[dict]]:
    """Render the recent-events list for the events tab.

    Returns ``(rendered_markup, visible_events)`` so the orchestrator can
    drive the cursor + Enter→preview integration without re-parsing files.
    Consecutive events sharing a chain_id are visually grouped (a blank
    line separates chain switches). The row at index ``cursor`` is
    highlighted with a coral ▶ prefix.
    """
    if project_root is None:
        return "[#555555]  (no project root)[/]", []

    events_root = project_root / ".reyn" / "events"
    if not events_root.is_dir():
        return "[#555555]  (no events yet)[/]", []

    if cache is None:
        cache = {}
    all_events = _load_events_cached(project_root, cache)

    filter_name, filter_set = _FILTER_GROUPS[event_filter_idx]
    tail = _TAIL_CYCLE[event_tail_idx]

    if filter_set:
        visible = [ev for ev in all_events if ev.get("type") in filter_set]
    else:
        visible = all_events

    if not visible:
        return "[#555555]  (no matching events)[/]", []

    # Newest-first window — also returned to the caller for cursor / preview
    windowed = list(visible[-tail:])[::-1]
    if cursor >= len(windowed):
        cursor = max(0, len(windowed) - 1)

    chain_replies = _load_chain_replies(project_root)

    lines: list[str] = []
    prev_chain: str | None = None
    for i, ev in enumerate(windowed):
        ev_type = ev.get("type", "?")
        data = ev.get("data") or {}
        chain_id = data.get("chain_id") or ""
        # Chain grouping — blank line between chain switches
        if prev_chain is not None and prev_chain != chain_id:
            lines.append("")
        prev_chain = chain_id

        ts = _esc(str(ev.get("timestamp", ""))[:19].replace("T", " "))
        color = _EVENT_COLORS.get(ev_type, _DEFAULT_EVENT_COLOR)
        hint = _esc(_event_hint(ev))
        hint_part = f"  [#555555]{hint}[/]" if hint else ""
        cursor_prefix = (
            f"[bold {_CORAL}]▶ [/]" if i == cursor else "  "
        )
        lines.append(
            f"{cursor_prefix}[#444444]{ts}[/]  [{color}]{_esc(ev_type)}[/]{hint_part}"
        )
        if ev_type == "user_message_received":
            cid = data.get("chain_id")
            if cid:
                reply = chain_replies.get(cid)
                if reply is None:
                    lines.append("[#444444]       ↳ [/][#555555](awaiting…)[/]")
                else:
                    short = _esc(reply[:72]) + ("…" if len(reply) > 72 else "")
                    lines.append(f"[#444444]       ↳ [/][#777777]{short}[/]")

    del filter_name
    return "\n".join(lines), windowed


__all__ = [
    "render_events",
    "_load_events_cached",
    "_FILTER_GROUPS",
    "_TAIL_CYCLE",
    "_EVENT_COLORS",
    "_DEFAULT_EVENT_COLOR",
    "_event_hint",
    "_load_chain_replies",
]
