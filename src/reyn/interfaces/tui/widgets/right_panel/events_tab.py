"""Events tab — renders recent OS events from .reyn/events/*.jsonl."""
from __future__ import annotations

import json
from pathlib import Path

from rich.cells import cell_len

from reyn.interfaces.tui._palette import _TEXT_DIMMEST

from .base import (
    _BORDER_DIM,
    _CORAL,
    _EVENT_INTERVENTION,
    _EVENT_LLM,
    _EVENT_PLAN,
    _EVENT_PLAN_MEMO,
    _EVENT_PLAN_STEP,
    _EVENT_SKILL,
    _EVENT_TOOL,
    _GREEN_DIMMEST,
    _STATUS_CRITICAL,
    _STATUS_ERROR,
    _STATUS_SUCCESS,
    _STATUS_SUCCESS_DIM,
    _TEXT_BODY,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MID,
    _TEXT_MUTED,
    _TEXT_NEUTRAL,
    _esc,
    logger,
)


def _truncate_to_cells(s: str, max_cells: int) -> tuple[str, bool]:
    """Truncate ``s`` to fit within ``max_cells`` display columns.

    Returns ``(truncated_string, was_truncated)``. CJK characters consume
    2 cells each so a naive ``s[:N]`` overshoots the column budget by
    roughly 2× for full-width content. ``rich.cells.cell_len`` is
    East-Asian-Width aware and matches what Textual will actually
    render.
    """
    if cell_len(s) <= max_cells:
        return s, False
    out_chars: list[str] = []
    used = 0
    for ch in s:
        w = cell_len(ch)
        if used + w > max_cells:
            break
        out_chars.append(ch)
        used += w
    return "".join(out_chars), True


def _oneline(s: str) -> str:
    """Collapse any whitespace run (incl. newlines) to a single space.

    Event hints and reply previews land inside a single Rich markup line
    like ``[#777777]<text>[/]``. An embedded ``\\n`` in ``<text>`` splits
    that markup across two rendered lines, leaving the closing ``[/]``
    on its own with no matching open tag — ``Text.from_markup`` raises
    ``MarkupError`` on the orphan close, the Static fallback returns an
    empty string, and the entire Events tab goes blank. Collapsing
    whitespace upstream keeps the rendered output single-line per row.
    """
    if not s:
        return ""
    return " ".join(s.split())

_EVENT_COLORS: dict[str, str] = {
    "phase_started":               _STATUS_SUCCESS,
    "phase_completed":             _STATUS_SUCCESS,
    "control_decided":             _STATUS_SUCCESS_DIM,
    "context_built":               _GREEN_DIMMEST,
    "llm_called":                  _EVENT_LLM,
    "llm_response_received":       _EVENT_LLM,
    "llm_request":                 _EVENT_LLM,  # #1669 — non-message call params
    "artifact_created":            _EVENT_SKILL,
    "artifact_validated":          _EVENT_SKILL,
    "validation_error":            _STATUS_ERROR,
    "phase_retry":                 _STATUS_ERROR,
    "permission_denied":           _STATUS_CRITICAL,
    "router_retry_exhausted":      _STATUS_CRITICAL,
    "tool_failed":                 _STATUS_ERROR,
    "tool_called":                 _EVENT_TOOL,
    "tool_returned":               _EVENT_TOOL,
    "mcp_called":                  _EVENT_TOOL,
    "mcp_completed":               _EVENT_TOOL,
    "mcp_failed":                  _STATUS_ERROR,
    "mcp_server_installed":        _EVENT_TOOL,
    "act_executed":                _EVENT_TOOL,
    "skill_run_spawned":           _EVENT_SKILL,
    "skill_run_completed":         _EVENT_SKILL,
    "skill_completion_injected":   _EVENT_SKILL,   # FP-0012: router narration trigger
    "workflow_started":            _EVENT_SKILL,
    "workflow_finished":           _EVENT_SKILL,
    "workflow_aborted":            _STATUS_ERROR,
    "agent_message_sent":          _TEXT_BODY,
    "agent_request_received":      _TEXT_BODY,
    "agent_response_received":     _TEXT_BODY,
    "user_message_received":       _TEXT_BRIGHT,
    "chat_started":                _TEXT_BRIGHT,
    "chat_stopped":                _TEXT_BRIGHT,
    "user_intervention_requested": _EVENT_INTERVENTION,
    "user_intervention_received":  _EVENT_INTERVENTION,
    # Issue #261 — routing-decision event family (amber sibling so
    # delegation / self-answer audit trail reads as same UX category
    # as the user_intervention_* pair).
    "intervention_routed":         _EVENT_INTERVENTION,
    "postprocessor_step_failed":    _STATUS_ERROR,
    "tool_executed":               _EVENT_TOOL,
    "web_fetch_started":           _TEXT_MUTED,
    "web_search_started":          _TEXT_MUTED,
    "web_search_completed":        _TEXT_MUTED,
    "web_search_failed":           _STATUS_ERROR,
    "phase_failed":                _STATUS_CRITICAL,
    "skill_run_failed":            _STATUS_ERROR,
    "run_skill_started":           _EVENT_SKILL,
    "control_ir_failed":           _STATUS_ERROR,
    "control_ir_skipped":          _TEXT_DIM,
    "normalization_error":         _STATUS_ERROR,
    "compaction_failed":           _STATUS_ERROR,
    "memory_deleted":              _TEXT_DIM,
    "memory_saved":                _TEXT_DIM,
    "workspace_updated":           _TEXT_DIM,
    "compaction_check":            _TEXT_DIM,
    # Internal routing / multi-agent housekeeping — very dim so they don't
    # crowd the visible window when many chains fire in a session.
    "chain_peer_discarded":        _BORDER_DIM,
    # Plan-mode (ADR-0022 / 0023 / 0024 / 0025) — orange family so a plan's
    # forensic events stand out from the blue skill_run / workflow events
    # while still reading as a sibling concept.
    "plan_emitted":                _EVENT_PLAN,
    "plan_step_started":           _EVENT_PLAN_STEP,
    "plan_step_completed":         _EVENT_PLAN_STEP,
    "plan_step_failed":            _STATUS_ERROR,
    "plan_step_memoized":          _EVENT_PLAN_MEMO,
    "plan_step_memo_failed":       _EVENT_PLAN_MEMO,
    "plan_aggregated":             _EVENT_PLAN,
    "plan_run_interrupted":        _STATUS_CRITICAL,
    # RAG (ADR-0033) — yellow-blue family. embed_progress is high-volume / chatty
    # so dim it. recall_embed_failed and index_dropped surface state changes.
    "recall_embed_failed":         _STATUS_ERROR,
    "embed_progress":              _TEXT_NEUTRAL,
    "index_dropped":               _EVENT_SKILL,
    # Safety / budget (FP-0003 / FP-0004 / FP-0005) — yellow for warnings,
    # red for hard stops, green for user-granted extensions, grey for resets.
    "safety_limit_checkpoint":     _EVENT_INTERVENTION,
    "loop_limit_exceeded":         _STATUS_CRITICAL,
    "phase_budget_exceeded":       _STATUS_CRITICAL,
    "chain_timeout":               _EVENT_PLAN,
    "chain_timeout_extended":      _STATUS_SUCCESS_DIM,
    "budget_warn":                 _EVENT_LLM,
    "budget_exceeded":             _STATUS_CRITICAL,
    "budget_extended":             _STATUS_SUCCESS_DIM,
    "budget_reset":                _TEXT_DIM,
}
_DEFAULT_EVENT_COLOR = _TEXT_NEUTRAL

# Each tuple is (label, frozenset-of-types). Empty set = show all.
_FILTER_GROUPS: list[tuple[str, frozenset]] = [
    ("all",   frozenset()),
    ("phase", frozenset({
        "phase_started", "phase_completed", "phase_failed",
        "control_decided", "context_built",
        "artifact_created", "artifact_validated",
        "control_ir_failed", "control_ir_skipped",
    })),
    ("llm",   frozenset({"llm_called", "llm_response_received"})),
    # #1669 — non-message LLM call params (reasoning_effort / temperature /
    # extra_body / …). A DEDICATED group (not lumped into "llm") so the owner can
    # ISOLATE just the request-param events while verifying a model's call params
    # (e.g. GPT-5.4) — one-per-LLM-call, so independent toggle-ability matters.
    ("request", frozenset({"llm_request"})),
    ("tool",  frozenset({
        "tool_called", "tool_returned", "tool_failed", "tool_executed",
        "mcp_called", "mcp_completed", "mcp_failed",
        "mcp_server_installed", "act_executed",
        "web_fetch_started", "web_search_started",
        "web_search_completed", "web_search_failed",
    })),
    # RAG (ADR-0033) — embed / recall / index lifecycle. Separate group so a
    # noisy embed_progress stream can be filtered in/out independently.
    ("rag", frozenset({
        "recall_embed_failed", "embed_progress", "index_dropped",
    })),
    # Safety / budget (FP-0003 / FP-0004 / FP-0005). Limit checkpoints, loop
    # caps, chain timeouts, budget warns / extensions / resets — every event
    # the operator needs to debug "why did it stop / extend / warn".
    ("safety", frozenset({
        "safety_limit_checkpoint", "loop_limit_exceeded",
        "phase_budget_exceeded", "chain_timeout", "chain_timeout_extended",
        "budget_warn", "budget_exceeded", "budget_extended", "budget_reset",
    })),
    ("skill", frozenset({
        "skill_run_spawned", "skill_run_completed", "skill_run_failed",
        "skill_completion_injected", "run_skill_started",
        "workflow_started", "workflow_finished", "workflow_aborted",
        "agent_message_sent", "agent_request_received", "agent_response_received",
    })),
    # Plan-mode group — every forensic plan_* event the runtime emits to the
    # events log (WAL-only types like plan_started/plan_completed live in
    # state_log.jsonl and don't appear here).
    ("plan", frozenset({
        "plan_emitted", "plan_aggregated", "plan_run_interrupted",
        "plan_step_started", "plan_step_completed", "plan_step_failed",
        "plan_step_memoized", "plan_step_memo_failed",
    })),
    ("error", frozenset({
        "validation_error", "phase_retry", "permission_denied",
        "router_retry_exhausted", "tool_failed", "mcp_failed",
        "plan_step_failed", "plan_step_memo_failed", "plan_run_interrupted",
        "loop_limit_exceeded", "phase_budget_exceeded", "budget_exceeded",
        "recall_embed_failed", "postprocessor_step_failed", "workflow_aborted",
        "phase_failed", "skill_run_failed", "control_ir_failed",
        "web_search_failed", "normalization_error", "compaction_failed",
        # W13 A#8: add events with hard-stop / terminal semantics that
        # were missing from the error filter.
        # ``safety_limit_checkpoint`` can carry a hard-stop signal
        #   (allow_continue=False) and belongs beside budget_exceeded.
        # ``chain_timeout`` is a timeout-induced terminal failure.
        # ``chain_peer_discarded`` signals a peer chain was dropped —
        #   relevant when debugging multi-agent errors.
        "safety_limit_checkpoint",
        "chain_timeout",
        "chain_peer_discarded",
    })),
    ("user", frozenset({
        "user_message_received",
        "user_intervention_requested", "user_intervention_received",
        "intervention_routed",  # issue #261 — routing decisions
        "chat_started", "chat_stopped",
    })),
    # Internal routing / housekeeping events — useful for debugging multi-agent
    # chains and budget resets, but very noisy in normal sessions.
    ("internal", frozenset({
        "chain_peer_discarded",
        "compaction_check", "compaction_failed",
        "budget_reset",
        "workspace_updated",
        "memory_saved", "memory_deleted",
        "control_ir_skipped",
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
    if t == "llm_request":
        # #1669: model + purpose + the salient non-message params inline; the
        # FULL param set is in the Space→JSON preview. Params live under the
        # nested ``params`` key (#1669 schema); absent keys are simply omitted
        # (no ``=None`` noise). tools_count is top-level.
        params = d.get("params") or {}
        bits = [str(d.get("model", "")), str(d.get("purpose", ""))]
        re_ = params.get("reasoning_effort")
        if re_ is not None:
            bits.append(f"reasoning_effort={re_}")
        temp = params.get("temperature")
        if temp is not None:
            bits.append(f"temp={temp}")
        tc = d.get("tools_count")
        if tc:
            bits.append(f"tools={tc}")
        return " · ".join(b for b in bits if b)
    if t == "artifact_created":
        return f"{d.get('artifact_type', '')} @ {d.get('phase', '')}"
    if t == "artifact_validated":
        errors = d.get("errors") or []
        at = d.get("artifact_type", "")
        return f"{at} ✗ {len(errors)} err" if errors else f"{at} ✓"
    if t == "validation_error":
        err, was_trunc = _truncate_to_cells(str(d.get("error", "")), 35)
        return f"{d.get('phase', '')}: {err}" + ("…" if was_trunc else "")
    if t == "phase_retry":
        err, was_trunc = _truncate_to_cells(str(d.get("error", "")), 25)
        return (
            f"attempt {d.get('attempt', '?')}/{d.get('max_retries', '?')}: {err}"
            + ("…" if was_trunc else "")
        )
    if t == "permission_denied":
        return f"{d.get('kind', '')} {d.get('path', '')}"
    if t in ("tool_called", "tool_returned"):
        return d.get("tool", "")
    if t == "tool_failed":
        msg, was_trunc = _truncate_to_cells(str(d.get("message", "")), 25)
        return f"{d.get('tool', '')}: {msg}" + ("…" if was_trunc else "")
    if t in ("mcp_called", "mcp_completed"):
        suffix = " ✗" if d.get("is_error") else ""
        return f"{d.get('server', '')}.{d.get('tool', '')}{suffix}"
    if t == "mcp_failed":
        err, was_trunc = _truncate_to_cells(str(d.get("error", "")), 25)
        return f"{d.get('server', '')}.{d.get('tool', '')}: {err}" + ("…" if was_trunc else "")
    if t == "mcp_server_installed":
        scope = d.get("scope", "")
        scope_part = f" ({scope})" if scope else ""
        return f"{d.get('server_id', '')}{scope_part}"
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
    if t == "skill_completion_injected":
        run_id = str(d.get("run_id", ""))[:8]
        return f"{d.get('skill', '')} [{run_id}] status={d.get('status', '')}"
    if t == "workflow_aborted":
        reason, was_trunc = _truncate_to_cells(str(d.get("reason", "")), 40)
        return reason + ("…" if was_trunc else "")
    if t == "agent_message_sent":
        return f"{d.get('from_agent', '')} → {d.get('to_agent', '')}"
    if t in ("agent_request_received", "agent_response_received"):
        return d.get("from_agent", "")
    if t == "user_message_received":
        text = str(d.get("text", ""))
        truncated, was_trunc = _truncate_to_cells(text, 40)
        return truncated + ("…" if was_trunc else "")
    if t == "user_intervention_requested":
        question, was_trunc = _truncate_to_cells(str(d.get("question", "")), 40)
        return question + ("…" if was_trunc else "")
    if t == "user_intervention_received":
        answer, was_trunc = _truncate_to_cells(str(d.get("answer", "")), 40)
        return answer + ("…" if was_trunc else "")
    if t == "intervention_routed":
        # Issue #261: surface the Phase 4 routing decision. Format
        # ``<route> <iv_kind> [<iv_id>]`` — short enough to fit the
        # events tab's narrow column, distinct enough to debug
        # "why did this prompt go self_answer instead of user_channel"
        # in a follow-up routing-policy expansion.
        route = str(d.get("route", "?"))
        iv_kind = str(d.get("iv_kind", "?"))
        iv_id_short = str(d.get("iv_id", ""))[:8]
        return f"{route} {iv_kind} [{iv_id_short}]"
    if t in ("sandboxed_exec_started", "sandboxed_exec_completed"):
        argv = d.get("argv") or []
        cmd = " ".join(str(a) for a in argv[:3])
        suffix = "…" if len(argv) > 3 else ""
        backend = d.get("backend", "")
        rc = d.get("returncode")
        rc_part = f" rc={rc}" if rc is not None else ""
        return f"[{backend}] {cmd}{suffix}{rc_part}"
    if t == "web_fetch_started":
        url, was_trunc = _truncate_to_cells(str(d.get("url", "")), 45)
        return url + ("…" if was_trunc else "")
    if t == "web_fetch_completed":
        return f"HTTP {d.get('status_code', '')} {d.get('content_length', '')}b"
    if t == "web_search_started":
        query, was_trunc = _truncate_to_cells(str(d.get("query", "")), 40)
        return query + ("…" if was_trunc else "")
    if t == "web_search_completed":
        return f"{d.get('result_count', '')} results"
    # Plan-mode (ADR-0022 / 0023 / 0024 / 0025). plan_id is a stable suffix —
    # show only the leading 8 chars so it stays readable; pair with step_id
    # where present so events from the same plan visually thread together.
    if t == "plan_emitted":
        plan_id = str(d.get("plan_id", ""))[:8]
        n = d.get("n_steps", 0)
        goal = str(d.get("goal", ""))
        goal = goal[:32] + ("…" if len(goal) > 32 else "")
        return f"[{plan_id}] {n} steps · {goal}"
    if t == "plan_step_started":
        plan_id = str(d.get("plan_id", ""))[:8]
        step_id = d.get("step_id", "")
        deps = d.get("depends_on") or []
        dep_part = f" ← {','.join(deps)}" if deps else ""
        return f"[{plan_id}] {step_id}{dep_part}"
    if t == "plan_step_completed":
        plan_id = str(d.get("plan_id", ""))[:8]
        step_id = d.get("step_id", "")
        nbytes = d.get("content_len", 0)
        return f"[{plan_id}] {step_id} · {nbytes}b"
    if t == "plan_step_failed":
        plan_id = str(d.get("plan_id", ""))[:8]
        step_id = d.get("step_id", "")
        err = str(d.get("error", ""))[:30]
        return f"[{plan_id}] {step_id} ✗ {err}"
    if t == "plan_step_memoized":
        plan_id = str(d.get("plan_id", ""))[:8]
        step_id = d.get("step_id", "")
        nbytes = d.get("content_len", 0)
        return f"[{plan_id}] {step_id} · replay ({nbytes}b)"
    if t == "plan_step_memo_failed":
        plan_id = str(d.get("plan_id", ""))[:8]
        step_id = d.get("step_id", "")
        err = str(d.get("error", ""))[:30]
        return f"[{plan_id}] {step_id} · replay ✗ {err}"
    if t == "plan_aggregated":
        plan_id = str(d.get("plan_id", ""))[:8]
        ok = d.get("n_completed", 0)
        bad = d.get("n_failed", 0)
        rlen = d.get("result_len", 0)
        return f"[{plan_id}] {ok} ok / {bad} fail · {rlen}b"
    if t == "plan_run_interrupted":
        plan_id = str(d.get("plan_id", ""))[:8]
        exc_type = d.get("exc_type", "")
        return f"[{plan_id}] interrupted ({exc_type})"
    # RAG (ADR-0033)
    if t == "recall_embed_failed":
        q = str(d.get("query", ""))[:30]
        err = str(d.get("error", ""))[:20]
        return f"{q!r}: {err}"
    if t == "embed_progress":
        emb = d.get("embedded", 0)
        skp = d.get("skipped", 0)
        tot = d.get("total", 0)
        pct = d.get("pct", 0)
        return f"{emb}+{skp}/{tot} ({pct}%)"
    if t == "index_dropped":
        n = d.get("chunks_dropped", 0)
        return f"{d.get('source', '')} · {n} chunks"
    # Safety / budget (FP-0003 / FP-0004 / FP-0005)
    if t == "safety_limit_checkpoint":
        cont = "→ continue" if d.get("allow_continue") else "✗ stop"
        ext = d.get("extension")
        ext_part = f" (+{ext})" if ext else ""
        return f"{d.get('kind', '')}: {cont}{ext_part}"
    if t == "loop_limit_exceeded":
        return f"{d.get('phase', '')}: {d.get('visit_count', '?')}/{d.get('max', '?')}"
    if t == "phase_budget_exceeded":
        return f"{d.get('phase', '')}: {str(d.get('reason', ''))[:30]}"
    if t == "chain_timeout":
        wait = str(d.get("waiting_on", ""))[:25]
        return f"{wait} ({d.get('timeout_seconds', '?')}s)"
    if t == "chain_timeout_extended":
        wait = str(d.get("waiting_on", ""))[:25]
        return f"{wait} +{d.get('extension_seconds', '?')}s"
    if t in ("budget_warn", "budget_exceeded"):
        dim = d.get("dimension", "")
        skill = d.get("skill", "")
        skill_part = f" @ {skill}" if skill else ""
        return f"{dim}{skill_part}"
    if t == "budget_extended":
        dim = d.get("hard_dimension") or d.get("dimension", "")
        granted = d.get("granted", "?")
        return f"{dim} +{granted}"
    if t == "budget_reset":
        return ""
    # Postprocessor (parallel to preprocessor)
    if t in ("postprocessor_step_started", "postprocessor_step_completed"):
        return f"[{d.get('step_index', '?')}] {d.get('step_type', '')}"
    if t == "postprocessor_step_failed":
        return (
            f"[{d.get('step_index', '?')}] {d.get('step_type', '')}: "
            f"{str(d.get('error', ''))[:25]}"
        )
    if t == "postprocessor_step_memoized":
        return f"[{d.get('step_index', '?')}] {d.get('step_type', '')} · replay"
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
                    # Issue #383: role rename "agent" → "assistant".
                    # Tolerate both for pre-#383 history.jsonl entries.
                    if m.get("role") in ("assistant", "agent"):
                        cid = (m.get("meta") or {}).get("chain_id")
                        if cid:
                            # Pre-#383 entries carry "text"; post-#383
                            # entries carry "content" (str or list-of-parts).
                            content = m.get("content")
                            if isinstance(content, str):
                                reply_text = content
                            elif isinstance(content, list):
                                # extract first text part
                                reply_text = next(
                                    (p.get("text", "") for p in content
                                     if isinstance(p, dict) and p.get("type") == "text"),
                                    "",
                                )
                            else:
                                reply_text = m.get("text", "")
                            replies[cid] = reply_text
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
    filelist_cache: list | None = None,
) -> list[dict]:
    """Read all events/.jsonl with file-mtime caching (perf).

    Each .jsonl file is parsed once and re-parsed only when its mtime
    changes — events panel re-renders every 2 s and the events directory
    grows monotonically with each turn, so re-reading every file every
    refresh adds up.

    ``filelist_cache`` is an optional mutable list used as a cheap TTL
    cache for the rglob directory walk itself (expensive once the events
    directory has hundreds of files).  Format: ``[timestamp, [Path…]]``.
    The walk is repeated at most once every 10 s.
    """
    import time as _time

    events_root = project_root / ".reyn" / "events"
    if not events_root.is_dir():
        return []
    all_events: list[dict] = []
    seen: set[Path] = set()
    # --- filelist TTL cache (10 s) ---
    _now = _time.monotonic()
    if (
        filelist_cache is not None
        and len(filelist_cache) == 2
        and _now - filelist_cache[0] < 10.0
    ):
        jsonl_files: list[Path] = filelist_cache[1]
    else:
        jsonl_files = sorted(events_root.rglob("*.jsonl"))
        if filelist_cache is not None:
            filelist_cache.clear()
            filelist_cache.extend([_now, jsonl_files])
    for jsonl in jsonl_files:
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
    filelist_cache: list | None = None,
    chain_isolate: str | None = None,
    verbose: bool = False,
) -> tuple[str, list[dict], list[int]]:
    """Render the recent-events list for the events tab.

    Returns ``(rendered_markup, visible_events, event_ys)`` so the
    orchestrator can drive the cursor + Enter→preview integration
    without re-parsing files. ``event_ys[i]`` is the 0-based line index
    (within ``rendered_markup``) of event ``visible_events[i]``'s
    headline row — used by ``_scroll_events_into_view`` to position
    the viewport precisely even when chain-switch blank lines and
    multi-line ``user_message_received`` rows have pushed the actual
    row off the naive ``y = 1 + cursor`` projection (A-F3, wave-8).

    Consecutive events sharing a chain_id are visually grouped (a blank
    line separates chain switches). The row at index ``cursor`` is
    highlighted with a coral ▶ prefix.

    When ``verbose=False`` (the default), events with
    ``type == "compaction_check"`` are filtered out of the visible list
    — these fire on every chat turn with outcomes like
    ``too_few_turns`` / ``below_min_batch`` / ``below_threshold`` /
    ``already_running`` (= "didn't compact, just checking") and clutter
    the tab without conveying actionable information. The actual
    compaction lifecycle (``compaction_started`` / ``compaction_completed``
    / ``compaction_failed``) is always shown. Pass ``verbose=True`` to
    restore the full unfiltered view.
    """
    if project_root is None:
        return f"[{_TEXT_DIM}]  (no project root)[/]", [], []

    events_root = project_root / ".reyn" / "events"
    if not events_root.is_dir():
        return f"[{_TEXT_DIM}]  (no events yet)[/]", [], []

    if cache is None:
        cache = {}
    all_events = _load_events_cached(project_root, cache, filelist_cache)

    filter_name, filter_set = _FILTER_GROUPS[event_filter_idx]
    tail = _TAIL_CYCLE[event_tail_idx]

    if filter_set:
        visible = [ev for ev in all_events if ev.get("type") in filter_set]
    else:
        visible = list(all_events)

    # Chain isolation (wave-11 A#2): when a chain_id is pinned, restrict
    # the visible list to just that chain. Empty result is rare — the
    # cursor's own chain seeds the isolate, so at least one matching
    # event is guaranteed — but the post-isolation list might shrink
    # below 1 if cache has rotated. Treated like any empty result.
    if chain_isolate:
        visible = [
            ev for ev in visible
            if ((ev.get("data") or {}).get("chain_id") or "") == chain_isolate
        ]

    # Compaction-check noise suppression: when verbose=False (the
    # default), hide compaction_check events from the visible list.
    # They fire on every chat turn but the vast majority carry
    # "not triggered" outcomes — the actual lifecycle is covered by
    # compaction_started / compaction_completed / compaction_failed.
    # Track the suppressed count so the footer can surface the toggle.
    n_compaction_check_hidden = 0
    if not verbose:
        filtered_visible: list[dict] = []
        for ev in visible:
            if ev.get("type") == "compaction_check":
                n_compaction_check_hidden += 1
            else:
                filtered_visible.append(ev)
        visible = filtered_visible

    # File-path iteration order doesn't match wall-clock: the events root
    # holds agents/<id>/events/*.jsonl alongside direct/<id>/skill_runs/*
    # and rglob walks alphabetically, so a stale ``direct/`` test run lands
    # at the end of ``all_events`` and dominates the tail window even
    # though those events are from yesterday. Sort by event timestamp
    # (ISO-8601 strings sort chronologically) so ``visible[-tail:]``
    # reflects recency rather than filesystem layout.
    visible.sort(key=lambda ev: ev.get("timestamp", ""))

    if not visible:
        # Two distinct empty states:
        #   1. `all_events` empty → pool itself is empty (no events logged yet).
        #      The existing "no matching events" wording is already meaningful;
        #      there's nothing to cycle to that would help.
        #   2. `all_events` non-empty but filter excluded everything → user
        #      cycled to a filter (e.g. `phase`, `rag`) whose event types
        #      haven't fired this session. They need a reminder of HOW to
        #      cycle to a different filter (the `[f]` header hint is only
        #      visible when the panel is wider than the filter name).
        if not all_events:
            return f"[{_TEXT_DIM}]  (no matching events)[/]", [], []
        # Split the two hints across separate lines so each survives the
        # 44-cell minimum panel width independently. The single-line
        # form was ~47 cells and clipped to ``press [f] to cycle filter
        # · [t] for ta…`` at narrow widths, hiding the ``[t]`` tail-size
        # discoverability cue entirely.
        return (
            f"[{_TEXT_DIM}]  (no events matching filter: [/]"
            f"[bold {_TEXT_BODY}]{_esc(filter_name)}[/][{_TEXT_DIM}])[/]\n"
            f"[{_TEXT_DIM}]  press [/][bold {_TEXT_BODY}]\\[f][/]"
            f"[{_TEXT_DIM}] to cycle filter[/]\n"
            f"[{_TEXT_DIM}]  press [/][bold {_TEXT_BODY}]\\[t][/]"
            f"[{_TEXT_DIM}] for tail size[/]"
        ), [], []

    # Newest-first window — also returned to the caller for cursor / preview
    windowed = list(visible[-tail:])[::-1]
    if cursor >= len(windowed):
        cursor = max(0, len(windowed) - 1)

    chain_replies = _load_chain_replies(project_root)

    lines: list[str] = []
    event_ys: list[int] = []
    # Wave-11 A#2 — chain isolation header. When active, surface the
    # isolated chain_id (truncated to 8-char prefix for readability)
    # + the unset key cue. This adds 2 visual rows above the event
    # list so ``event_ys`` indices still align to ``lines`` because
    # ``event_ys.append(len(lines))`` is computed AFTER the header
    # rows are appended (= each event row gets the post-header y).
    if chain_isolate:
        short = chain_isolate[:8] + ("…" if len(chain_isolate) > 8 else "")
        lines.append(
            f"  [#88aacc]⛓ chain isolated:[/] [bold {_CORAL}]{_esc(short)}[/]"
        )
        lines.append(
            f"  [{_TEXT_DIM}]  press [/][bold {_TEXT_BODY}]\\[i][/]"
            f"[{_TEXT_DIM}] to clear isolation[/]"
        )
    prev_chain: str | None = None
    for i, ev in enumerate(windowed):
        ev_type = ev.get("type", "?")
        data = ev.get("data") or {}
        chain_id = data.get("chain_id") or ""
        # Chain grouping — blank line between chain switches
        if prev_chain is not None and prev_chain != chain_id:
            lines.append("")
        prev_chain = chain_id

        # Use HH:MM:SS only — the full ISO timestamp consumed 19 chars on the
        # narrow panel and the event-type was pushed off-screen behind an `…`.
        # Date is rarely needed at-a-glance for events tab consumers; the
        # full timestamp is still in the JSON preview opened with Space.
        raw_ts = str(ev.get("timestamp", ""))
        # "2026-05-17T09:54:42…" — the time block lives in chars 11-19
        ts = _esc(raw_ts[11:19] if len(raw_ts) >= 19 else raw_ts)
        color = _EVENT_COLORS.get(ev_type, _DEFAULT_EVENT_COLOR)
        hint = _esc(_oneline(_event_hint(ev)))
        hint_part = f"  [{_TEXT_DIM}]{hint}[/]" if hint else ""
        cursor_prefix = (
            f"[bold {_CORAL}]▶ [/]" if i == cursor else "  "
        )
        # Record the y of the headline row BEFORE appending — chain
        # switches add a blank line above, and user_message_received
        # rows add a ``↳`` reply line below, both of which drift the
        # arithmetic ``1 + cursor`` projection. Same idiom as
        # memory_tab / agents_tab.
        event_ys.append(len(lines))
        lines.append(
            f"{cursor_prefix}[{_TEXT_DIMMEST}]{ts}[/]  [{color}]{_esc(ev_type)}[/]{hint_part}"
        )
        if ev_type == "user_message_received":
            cid = data.get("chain_id")
            if cid:
                reply = chain_replies.get(cid)
                if reply is None:
                    lines.append(f"[{_TEXT_DIMMEST}]       ↳ [/][{_TEXT_DIM}](awaiting…)[/]")
                else:
                    # Cell-width truncate (not ``[:72]``) so CJK / wide
                    # characters obey the 40-cell preview budget instead
                    # of overflowing to ~80 cells and wrapping the
                    # ``↳`` line across 2-3 panel rows. 40 cells matches
                    # the ``_event_hint`` cap used elsewhere in this
                    # file and fits the 36-col panel min with the 7-cell
                    # ``↳`` indent. Reply collapses to one line first
                    # so an embedded newline doesn't orphan the `[/]`
                    # and crash ``Text.from_markup`` (= the whole tab
                    # would render blank — see ``_oneline`` docstring).
                    truncated, was_truncated = _truncate_to_cells(
                        _oneline(reply), 40,
                    )
                    short = _esc(truncated) + ("…" if was_truncated else "")
                    lines.append(f"[{_TEXT_DIMMEST}]       ↳ [/][{_TEXT_MID}]{short}[/]")

    del filter_name
    # Dim footer hint — always appended after the event rows so the user
    # can discover the [d] docs shortcut from the events tab itself.
    lines.append(f"[{_TEXT_DIM}]  ? press \\[d] for events.md reference[/]")
    # Compaction-check suppression footer: when at least 1 event was
    # hidden, surface a dim hint so the user can discover the [v] toggle.
    if n_compaction_check_hidden > 0:
        lines.append(
            f"[{_TEXT_DIMMEST}]  ↩ {n_compaction_check_hidden} compaction_check hidden"
            " (\\[v] to show)[/]"
        )
    return "\n".join(lines), windowed, event_ys


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
