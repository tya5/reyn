"""Agents tab — Rich Tree view of registered agents and their running skills."""
from __future__ import annotations

import json
import time as _time
from datetime import date as _date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Group as RichGroup
from rich.text import Text as RichText
from rich.tree import Tree as RichTree

from reyn.interfaces.tui._palette import _DIVIDER_DIM, _TEXT_DIMMEST

from .base import (
    _CORAL,
    _EVENT_PLAN,
    _STATUS_ERROR,
    _STATUS_READY,
    _STATUS_SUCCESS,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MUTED,
    logger,
)


def _compact_ts(ts: str) -> str:
    """Return a short timestamp string for the recent-skill row.

    ``ts`` is the raw form persisted by the event store (= e.g.
    ``"2026-05-19 07:15:42"`` from ``isoformat()[:19].replace("T", " ")``).
    A 19-char timestamp on every recent row wrapped the entire line to
    4-5 panel rows at the default 33 % panel width; collapsing today's
    date to ``HH:MM:SS`` recovers 11 cells per row and keeps the line on
    a single panel line. Older runs keep the full ``YYYY-MM-DD HH:MM:SS``
    so day-level context is still visible when scrolling history.
    """
    if not ts or len(ts) < 10:
        return ts
    today_iso = _date.today().isoformat()
    if ts.startswith(today_iso):
        # Skip the leading date + the single space, keep ``HH:MM:SS``.
        return ts[11:]
    return ts

if TYPE_CHECKING:
    from reyn.runtime.registry import AgentRegistry


# How many recent completed items to surface per agent. Bumped from 2
# to 5 after dogfood feedback ("直近 N 個のヒストリ対応してたっけ？").
# 5 still keeps the tab readable on a typical 24-row terminal even with
# 2-3 agents.
_RECENT_LIMIT = 5


def _recent_skill_runs_for_agent(
    project_root: Path | None,
    agent_name: str,
    running_run_ids: set[str],
    limit: int = _RECENT_LIMIT,
) -> list[dict]:
    """Return up to ``limit`` recently-completed skill runs for ``agent_name``.

    Each entry: ``skill_name``, ``run_id`` (8-char prefix), ``status``,
    ``duration_s``, ``ts`` (ISO string of completion).

    Source layout (as of 2026-05): ::

        .reyn/events/agents/<name>/skill_runs/<YYYY-MM>/<isots>_<skill>.jsonl

    The file name is ``<isots-no-tz>_<skill_name>.jsonl`` — there's no
    run_id in the filename, so we pull it out of the FIRST event in
    the file (``workflow_started.data.run_id``). The LAST event tells
    us the terminal type:

      * ``workflow_finished``  → status "ok"
      * ``workflow_aborted``   → status "aborted"
      * (anything else)        → fall back to the event type as a label

    ``rglob`` (not ``glob``) so we recurse into the YYYY-MM subdirs.
    """
    out: list[dict] = []
    if project_root is None:
        return out
    skill_dir = (
        project_root / ".reyn" / "events"
        / "agents" / agent_name / "skill_runs"
    )
    if not skill_dir.is_dir():
        return out

    # Collect candidate files newest-first by mtime. rglob to walk the
    # YYYY-MM subdirectories. Reading mtime up front avoids parsing
    # files we won't display.
    files: list[tuple[float, Path]] = []
    for jsonl in skill_dir.rglob("*.jsonl"):
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    files.sort(reverse=True)

    for _mtime, jsonl in files:
        if len(out) >= limit:
            break
        # Filename: "<isots>_<skill_name>.jsonl". The skill name itself
        # may contain underscores (web_search_display, chat_compactor,
        # etc.), so split only ONCE — the head is the timestamp, the
        # tail is the entire skill name.
        stem = jsonl.stem
        if "_" not in stem:
            continue
        start_iso, skill_name = stem.split("_", 1)

        # Read the file once: keep the first event (for run_id) and the
        # last event (for completion timestamp + terminal type).
        first_event: dict | None = None
        last_event: dict | None = None
        try:
            for raw in jsonl.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                if first_event is None:
                    first_event = ev
                last_event = ev
        except OSError as exc:
            logger.warning(
                "right_panel agents: read of %s failed: %s", jsonl, exc,
            )
            continue
        if first_event is None or last_event is None:
            continue

        # run_id lives in workflow_started.data.run_id; fall back to
        # last event if that's missing for any reason.
        run_id = ""
        for ev in (first_event, last_event):
            data = ev.get("data") or {}
            rid = data.get("run_id", "")
            if rid:
                run_id = str(rid)
                break
        if run_id and run_id in running_run_ids:
            continue

        ev_type = last_event.get("type", "")
        ts = str(last_event.get("timestamp", ""))
        # Terminal-type → status mapping. Includes legacy
        # `skill_run_completed` shape for forward-compat with future
        # event renames; new code emits workflow_finished/aborted.
        # When the LAST event is NOT a known terminal type, the run
        # never finished cleanly — typical causes are session crash,
        # SIGKILL, exception escaping the OS layer, or stdio fd
        # corruption mid-LLM-call. We mark these as "stuck" so the
        # display can distinguish them from genuine aborts.
        if ev_type in ("workflow_finished", "skill_run_completed"):
            status = "ok"
            stuck_at = ""
        elif ev_type in ("workflow_aborted", "skill_run_failed"):
            status = "aborted"
            stuck_at = ""
        else:
            status = "stuck"
            stuck_at = ev_type or "unknown"

        # Duration — both timestamps include timezone offsets in the
        # current event format (e.g. "2026-05-09T08:44:43.210059+09:00").
        # The filename's ts is ALSO local time but without a tz suffix,
        # so parse it as naive and pretend it matches the event tz.
        duration_s = 0.0
        if start_iso and ts:
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(start_iso)
                t1_str = ts
                # `datetime.fromisoformat` accepts the +HH:MM suffix
                # natively; drop fractional microseconds beyond 6 digits
                # if present (= some platforms emit nanoseconds).
                t1 = datetime.fromisoformat(t1_str)
                # Normalise to naive for the diff if mismatched.
                if t0.tzinfo is None and t1.tzinfo is not None:
                    t1 = t1.replace(tzinfo=None)
                duration_s = max(0.0, (t1 - t0).total_seconds())
            except Exception:
                duration_s = 0.0

        # ``run_id`` here is e.g. "20260508T234443Z_chat_compactor"; the
        # leading 8 chars ("20260508") are date-only and identical across
        # runs of the same skill on the same day, which makes the agents
        # tab unreadable. Use the time chunk (after the "T") so each
        # entry's badge is genuinely unique within a tab refresh.
        rid_compact = run_id
        if "T" in run_id:
            rid_compact = run_id.split("T", 1)[1][:6]
        out.append({
            "skill_name": skill_name or "?",
            "run_id": (rid_compact or stem)[:8],
            # Full run_id (= as it appears in workflow_started.data.run_id).
            # Needed by the orchestrator to look up triggered_by from the
            # session-local map keyed on the full id.
            "run_id_full": run_id or "",
            "status": status,
            # Last event type when status == "stuck"; lets the renderer
            # show "(stuck @ llm_called)" instead of just "(llm_called)".
            "stuck_at": stuck_at,
            "duration_s": duration_s,
            "ts": ts[:19].replace("T", " "),
            # Carry the absolute path so the preview pane can re-read
            # the jsonl on demand (= without holding all events in
            # memory across panel refreshes).
            "jsonl_path": jsonl,
        })
    return out


def _recent_plans_for_agent(
    project_root: Path | None,
    agent_name: str,
    running_plan_ids: set[str],
    limit: int = _RECENT_LIMIT,
) -> list[dict]:
    """Return up to ``limit`` recently-finished plans for ``agent_name``.

    Reads plan_aggregated / plan_run_interrupted events from the agent's
    chat events log (= where forensic plan_* events land — see planner.py).
    Skips plans whose plan_id is still in ``running_plan_ids`` so the
    "RECENT" section is strictly past tense.
    """
    out: list[dict] = []
    if project_root is None:
        return out
    agent_dir = project_root / ".reyn" / "events" / "agents" / agent_name
    if not agent_dir.is_dir():
        return out

    # Newest-first scan across all the agent's event files. Plans usually
    # finish in the same chat-events file they started in; iterate the most
    # recently modified first so we hit recent completions quickly.
    files: list[tuple[float, Path]] = []
    for jsonl in agent_dir.rglob("*.jsonl"):
        if "skill_runs" in jsonl.parts:
            continue  # skill files don't carry plan events
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    files.sort(reverse=True)

    # We track the most recent plan_emitted (for goal) and plan_aggregated /
    # plan_run_interrupted (for completion + counts) per plan_id.
    seen: set[str] = set()
    candidates: list[dict] = []  # newest-first
    plan_goals: dict[str, str] = {}

    for _mtime, jsonl in files:
        if len(candidates) >= limit:
            break
        # Read the file once; collect plan_emitted goals first, then walk
        # backwards through the lines to find the most recent terminal
        # event(s) for unseen plan_ids.
        raw_lines: list[str] = []
        try:
            raw_lines = jsonl.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning(
                "right_panel agents: read of %s failed: %s", jsonl, exc,
            )
            continue
        # Forward pass: capture goals.
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("type") == "plan_emitted":
                d = ev.get("data") or {}
                pid = str(d.get("plan_id", ""))
                if pid:
                    plan_goals[pid] = str(d.get("goal", ""))
        # Reverse pass: pick terminal events (newest first within file).
        for raw in reversed(raw_lines):
            if len(candidates) >= limit:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            ev_type = ev.get("type", "")
            if ev_type not in ("plan_aggregated", "plan_run_interrupted"):
                continue
            d = ev.get("data") or {}
            pid = str(d.get("plan_id", ""))
            if not pid or pid in seen or pid in running_plan_ids:
                continue
            seen.add(pid)
            candidates.append({
                "plan_id": pid[:8],
                "goal": plan_goals.get(pid, ""),
                "ts": str(ev.get("timestamp", ""))[:19].replace("T", " "),
                "status": (
                    "ok" if ev_type == "plan_aggregated"
                    and (d.get("n_failed", 0) or 0) == 0
                    else "interrupted" if ev_type == "plan_run_interrupted"
                    else "partial"
                ),
                "n_completed": int(d.get("n_completed", 0) or 0),
                "n_failed": int(d.get("n_failed", 0) or 0),
                "exc_type": str(d.get("exc_type", "")),
            })
    return candidates


def _plans_for_agent(registry: "AgentRegistry", name: str) -> list[dict]:
    """Inspect the loaded session and return a list of plan-summary dicts.

    Each entry has: ``plan_id`` (8-char prefix), ``goal`` (≤48 chars),
    ``done`` (completed step count), ``failed`` (failed step count),
    ``total`` (total step count), ``status`` (running / paused).

    Defensive — main hasn't been rebased into this branch yet, so
    ``running_plans`` / ``get_plan_registry`` may not exist on the session.
    Returns ``[]`` for any failure path so the agents tab keeps rendering.
    """
    out: list[dict] = []
    try:
        session = registry.get_session(name)  # type: ignore[attr-defined]
    except Exception:
        return out
    if session is None:
        return out

    running = getattr(session, "running_plans", None) or {}
    plan_reg = None
    getter = getattr(session, "get_plan_registry", None)
    if callable(getter):
        try:
            plan_reg = getter()
        except Exception as exc:
            logger.warning(
                "right_panel agents: get_plan_registry(%s) failed: %s",
                name, exc,
            )
            plan_reg = None

    # plan_ids = union of in-flight tasks + every persisted snapshot. The
    # snapshot side covers paused / interrupted plans that have no live
    # task but still have recovery state on disk.
    seen: set[str] = set()
    plan_ids: list[str] = []
    for pid in running.keys():
        if pid not in seen:
            plan_ids.append(pid)
            seen.add(pid)
    if plan_reg is not None:
        try:
            for pid in plan_reg.list_active():
                if pid not in seen:
                    plan_ids.append(pid)
                    seen.add(pid)
        except Exception as exc:
            logger.warning(
                "right_panel agents: plan_registry.list_active(%s) failed: %s",
                name, exc,
            )

    for plan_id in plan_ids:
        snap = None
        if plan_reg is not None:
            try:
                snap = plan_reg.get(plan_id)
            except Exception:
                snap = None
        goal = getattr(snap, "goal", "") if snap is not None else ""
        step_results = getattr(snap, "step_results", {}) if snap is not None else {}
        step_failures = getattr(snap, "step_failures", {}) if snap is not None else {}
        steps_serialized = (
            getattr(snap, "steps_serialized", []) if snap is not None else []
        )
        total = len(steps_serialized) if steps_serialized else (
            len(step_results) + len(step_failures)
        )
        task = running.get(plan_id)
        is_running = task is not None and not task.done()
        out.append({
            "plan_id": plan_id[:8],
            # Full plan_id retained alongside the 8-char display form so
            # the orchestrator can build the "running set" used to keep
            # recent_plans from double-listing in-flight runs.
            "plan_id_full": plan_id,
            "goal": goal,
            "done": len(step_results),
            "failed": len(step_failures),
            "total": total,
            "status": "running" if is_running else "paused",
        })
    return out


def render_agents(
    registry: "AgentRegistry | None",
    exec_state: dict[str, dict],
    *,
    project_root: Path | None = None,
    cursor: int = 0,
) -> tuple[Any, list[dict], list[int]]:
    """Return ``(renderable, flat_items, item_ys)`` for the agents tab.

    ``project_root`` is optional — when provided, the RECENT subsection
    surfaces the last few completed skill runs and finished plans by reading
    `.reyn/events/agents/<name>/`. When omitted, the renderer degrades to
    just running + idle context.

    ``cursor`` is an index into ``flat_items``. The matching row gets a
    coral ``▶ `` prefix (= same selection idiom as docs / events / memory
    tabs). Out-of-range cursors are silently clamped by the orchestrator.

    ``flat_items`` is an ordered list of selectable rows, one entry per
    running skill / running plan / recent skill / recent plan. Each entry
    carries enough metadata for the preview pane to build a detail view
    without re-reading the registry.

    ``item_ys`` is the 0-indexed y-coordinate (line number) of each item's
    row in the rendered output, so the orchestrator can scroll the cursor
    into view as j/k cycles past the visible window. Tracked here because
    RichTree's guide-line layout makes the y-coord impossible to predict
    arithmetically from the cursor index alone — running skills add 1-2
    lines, recent plans with a goal add 2 lines, etc.
    """
    flat_items: list[dict] = []
    item_ys: list[int] = []
    # Line counter for tracking item y-coords as we build the renderable.
    # Each tree-node add() = 1 line; blank between agent blocks = 1 line.
    y_counter = 0

    if registry is None:
        return f"[{_TEXT_DIM}]  (no registry)[/]", flat_items, item_ys

    try:
        names = registry.list_names()
    except Exception as exc:
        logger.warning("right_panel agents: registry.list_names() failed: %s", exc)
        return f"[{_TEXT_DIM}]  (registry unavailable)[/]", flat_items, item_ys

    if not names:
        return f"[{_TEXT_DIM}]  (no agents)[/]", flat_items, item_ys

    try:
        attached = registry.attached_name
    except Exception as exc:
        logger.warning("right_panel agents: registry.attached_name unavailable: %s", exc)
        attached = None
    try:
        loaded = set(registry.loaded_names())
    except Exception as exc:
        logger.warning("right_panel agents: registry.loaded_names() failed: %s", exc)
        loaded = set()
    # Wave-10 follow-up H-F12: pin the attached agent to the top of the
    # list. ``registry.list_names()`` returns sorted-alphabetical, so on
    # a registry with 5+ agents the attached agent (= the one the user
    # is currently chatting with, marked ``▶`` coral) could appear at
    # position 3-5 and require j-key navigation to find. The "daily
    # contact" agent should be immediately visible without scrolling —
    # other TUI surfaces (= tmux session list, IDE active-tab) follow
    # the same convention. The sort key keeps alphabetical order for
    # the non-attached agents (= secondary sort) so the rest of the
    # list still reads predictably.
    if attached:
        names = sorted(names, key=lambda n: (n != attached, n))
    now = _time.monotonic()

    agent_trees: list[Any] = []

    for agent_idx, name in enumerate(names):
        # Blank separator between agent blocks (matches the interleave below).
        if agent_idx > 0:
            y_counter += 1
        # Root label of this agent's RichTree is one line. Record its y
        # for cursor navigation BEFORE bumping y_counter past it — j/k
        # in the panel uses item_ys / flat_items to land the cursor on
        # selectable rows, and the agent-name row is the most natural
        # selectable target (= "this is the agent I want to act on").
        agent_label_y = y_counter
        y_counter += 1
        is_attached = name == attached
        in_loaded = name in loaded

        # ── running skills ─────────────────────────────────────────
        agent_skills = [
            (rid, info)
            for rid, info in exec_state.items()
            if info.get("agent_name") == name
        ]

        agent_plans = _plans_for_agent(registry, name)

        # ── agent label ────────────────────────────────────────────
        # Three-state semantics, not two:
        #   ● running  (green)  — at least one skill / plan in flight
        #   ◐ ready    (amber)  — session loaded but nothing in flight
        #   ○ idle     (grey)   — session not loaded
        # Old behaviour collapsed "loaded" and "actively executing" into
        # a single "running" badge, which made an idle-but-loaded agent
        # show "● running" alongside the idle-context tail (last/↳),
        # confusing the user about whether anything was actually
        # happening.
        has_work = bool(agent_skills) or bool(agent_plans)
        if has_work:
            status_glyph, status_text, status_style = (
                "● ", "running", _STATUS_SUCCESS,
            )
        elif in_loaded:
            status_glyph, status_text, status_style = (
                "◐ ", "ready", _STATUS_READY,
            )
        else:
            status_glyph, status_text, status_style = (
                "○ ", "idle", _TEXT_DIM,
            )
        # Apply ``reverse`` text-style on the cursor row so it stands out
        # without adding a column-prefix shift (= the attached ``▶ ``
        # marker already occupies column 0). Same non-color shape-cue
        # pattern intervention chip focus uses (PR #181).
        is_cursor = len(flat_items) == cursor
        cursor_suffix = " reverse" if is_cursor else ""
        label = RichText()
        label.append(
            "▶ " if is_attached else "  ",
            style=_TEXT_DIM + cursor_suffix,
        )
        label.append(
            name,
            style=("bold " + _CORAL if is_attached else _TEXT_BRIGHT) + cursor_suffix,
        )
        label.append("  ", style="" + cursor_suffix)
        label.append(
            status_glyph + status_text, style=status_style + cursor_suffix,
        )
        flat_items.append({
            "kind": "agent",
            "name": name,
            "attached": is_attached,
            "loaded": in_loaded,
        })
        item_ys.append(agent_label_y)

        tree = RichTree(label, guide_style=_DIVIDER_DIM)

        def _cursor_prefix(idx: int) -> tuple[str, str]:
            """Return (prefix, name_style) for selectable item ``idx``.

            Highlighted row gets a coral '▶ ' marker; everything else
            gets two spaces so the column alignment is preserved.
            """
            if idx == cursor:
                return ("▶ ", "bold " + _CORAL)
            return ("  ", "")

        if agent_skills:
            # Topological order: roots first, then children. Issue #210
            # nests sub-skill rows under their parent's RichTree node when
            # the parent is also a currently-running skill (= still in
            # ``exec_state``). Parent finished / parent on a different
            # agent → render the child as a root row to avoid an
            # orphaned tree branch pointing at nothing. The OS only
            # stamps ``parent_run_id`` for **direct** parents (no
            # multi-hop lineage), so a single roots-then-children pass
            # is sufficient.
            agent_skill_ids = {rid for rid, _info in agent_skills}
            nodes_by_run_id: dict[str, Any] = {}

            def _emit_skill_row(
                run_id: str, info: dict, parent_node: Any,
            ) -> None:
                nonlocal y_counter
                elapsed = int(now - info.get("start_time", now))
                pfx, name_style = _cursor_prefix(len(flat_items))
                skill_label = RichText()
                skill_label.append(pfx, style=_CORAL)
                # Colour-grade the elapsed counter the same way
                # SkillActivityRow does (≥30s amber, ≥60s red) so a
                # slow / stuck skill stands out at a glance.
                if elapsed >= 60:
                    elapsed_style = f"bold {_STATUS_ERROR}"
                elif elapsed >= 30:
                    elapsed_style = "bold #ffaa44"  # palette-candidate: warning amber — no foundation token yet
                else:
                    elapsed_style = _TEXT_MUTED
                skill_label.append(f"{elapsed:3d}s  ", style=elapsed_style)
                skill_label.append(
                    info.get("skill_name", "?"),
                    style=name_style or _TEXT_BRIGHT,
                )
                # Wave-7 Topic C-F2: surface plan-step attribution as a
                # ``[plan N/M]`` badge after the skill name so the
                # agents tab matches the conv pane SkillActivityRow's
                # persistent plan badge (wave-7 PR #418). Source is the
                # ``_skill_exec`` snapshot, populated when
                # ``_update_skill_exec`` parses ``detail: plan N/M``
                # traces from ChatEventForwarder.
                # F-I (#427 follow-up): plain coral instead of dim, so the
                # badge is readable at a glance — the prior ``dim {coral}``
                # blended into surrounding text and was easy to miss.
                plan_n_done = info.get("plan_n_done")
                plan_n_total = info.get("plan_n_total")
                # Wave-10 follow-up H-F4: ``plan_n_done is not None``
                # rather than truthiness. The pre-fix ``if plan_n_done
                # and plan_n_total`` treats ``plan_n_done == 0`` (=
                # plan just activated, step 1 of N not yet completed)
                # as a hide signal, so the badge "pops in" mid-run
                # instead of being consistent with the conv-pane
                # SkillActivityRow badge that DOES show "plan 0/5" from
                # the start of a plan. The ``plan_n_total`` check
                # remains truthy (= a zero-total plan is degenerate
                # noise; suppressing it stays correct).
                if plan_n_done is not None and plan_n_total:
                    skill_label.append(
                        f"  [plan {plan_n_done}/{plan_n_total}]",
                        style=_CORAL,
                    )
                skill_node = parent_node.add(skill_label)
                nodes_by_run_id[run_id] = skill_node
                item_ys.append(y_counter)
                # Track the y advance: one for the skill row, plus one for
                # the optional phase child below.
                y_counter += 1

                phase = info.get("phase", "")
                if phase:
                    visits = info.get("phase_visits", 1)
                    phase_label = RichText()
                    phase_label.append(phase, style=_TEXT_DIM)
                    if visits > 1:
                        phase_label.append(f"  v{visits}", style=_TEXT_DIMMEST)
                    skill_node.add(phase_label)
                    y_counter += 1
                flat_items.append({
                    "kind": "running_skill",
                    "agent": name,
                    "run_id": run_id,
                    "skill_name": info.get("skill_name", "?"),
                    "phase": phase,
                    "phase_visits": info.get("phase_visits", 1),
                    "elapsed_s": elapsed,
                    # User message that kicked off this run — populated
                    # by ``ReynTUIApp._update_skill_exec`` on first trace.
                    # Empty string when unknown (= e.g. session restored
                    # from disk, or skill spawned by a non-chat caller).
                    "triggered_by": info.get("triggered_by", ""),
                    # Issue #210: surface parent linkage in flat_items so
                    # the preview pane / future actions can route by it.
                    "parent_run_id": info.get("parent_run_id", ""),
                    # Wave-7 Topic C-F2: plan-step attribution carried
                    # so preview / future cursor actions can show "this
                    # skill is plan step N/M".
                    "plan_n_done": info.get("plan_n_done"),
                    "plan_n_total": info.get("plan_n_total"),
                })

            # Pass 1: root skills (no parent OR parent not on this agent
            # / already finished).
            roots: list[tuple[str, dict]] = []
            children: list[tuple[str, dict]] = []
            for run_id, info in agent_skills:
                parent_id = info.get("parent_run_id", "")
                if parent_id and parent_id in agent_skill_ids:
                    children.append((run_id, info))
                else:
                    roots.append((run_id, info))
            for run_id, info in roots:
                _emit_skill_row(run_id, info, tree)
            # Pass 2: children nest under their parent's skill_node.
            for run_id, info in children:
                parent_node = nodes_by_run_id.get(
                    info.get("parent_run_id", ""), tree,
                )
                _emit_skill_row(run_id, info, parent_node)

        # Plan-mode (ADR-0022 / 0023). Surfaced as a sibling of running
        # skills — same agent can simultaneously run skills + plans.
        # Coloured orange (#ff9944) to match the events-tab plan_* family.
        if agent_plans:
            for p in agent_plans:
                pfx, _ = _cursor_prefix(len(flat_items))
                plan_label = RichText()
                plan_label.append(pfx, style=_CORAL)
                plan_label.append("plan ", style=_TEXT_MUTED)
                plan_label.append(p["plan_id"], style=_EVENT_PLAN)
                plan_label.append(
                    f"  {p['done']}/{p['total']}",
                    style=_TEXT_BRIGHT,
                )
                if p["failed"]:
                    plan_label.append(
                        f"  ({p['failed']} failed)", style=_STATUS_ERROR,
                    )
                plan_label.append(
                    f"  {p['status']}",
                    style=_STATUS_SUCCESS if p["status"] == "running" else _STATUS_READY,
                )
                plan_node = tree.add(plan_label)
                item_ys.append(y_counter)
                y_counter += 1
                if p["goal"]:
                    goal = p["goal"][:60] + ("…" if len(p["goal"]) > 60 else "")
                    plan_node.add(RichText(goal, style=_TEXT_DIM))
                    y_counter += 1
                flat_items.append({
                    "kind": "running_plan",
                    "agent": name,
                    "plan_id": p["plan_id"],
                    # Wave-10 follow-up H-F6: carry the FULL plan_id so
                    # ``_build_running_plan_bundle`` (= the ``c``
                    # copy-bundle path) can hand the user the
                    # canonical identifier instead of the 8-char
                    # display prefix. Pre-fix the bundle contained
                    # only ``plan_id`` (= prefix), causing the copied
                    # payload to disagree with the full UUID in the
                    # events log and breaking cross-reference. The
                    # ``recent_plan`` flat_item already carried this
                    # via ``**p`` splat; this aligns the
                    # ``running_plan`` shape.
                    "plan_id_full": p.get("plan_id_full", p["plan_id"]),
                    "goal": p["goal"],
                    "done": p["done"],
                    "total": p["total"],
                    "failed": p["failed"],
                    "status": p["status"],
                })

        # ── recently completed (skills + plans) ────────────────────
        # Always shown when project_root is supplied — gives the user
        # at-a-glance context about "what just happened" even while a new
        # skill/plan is running. Skipped silently when project_root is
        # missing (= test harnesses) or both lists are empty.
        running_run_ids = {rid for rid, _info in agent_skills}
        # Full plan_ids — must match the FULL id we'll see in event
        # ``data.plan_id`` so ``_recent_plans_for_agent`` can dedup
        # against currently-running plans correctly. Earlier code
        # used ``p["plan_id"]`` (= 8-char display prefix) which never
        # matched, leaving running plans visible in both sections.
        running_plan_ids = {
            p.get("plan_id_full", p["plan_id"]) for p in agent_plans
        }
        # Plan ids in agent_plans are 8-char prefixes; expand to a guard
        # set that also catches full-length matches against the same
        # prefix space.
        recent_skills = _recent_skill_runs_for_agent(
            project_root, name, running_run_ids,
        )
        recent_plans = _recent_plans_for_agent(
            project_root, name, running_plan_ids,
        )
        if recent_skills or recent_plans:
            recent_node = tree.add(
                RichText("recent", style="#777777")  # palette-candidate: mid-dim label — no foundation token yet
            )
            # The "recent" header occupies its own line.
            y_counter += 1
            for s in recent_skills:
                pfx, _ = _cursor_prefix(len(flat_items))
                line = RichText()
                line.append(pfx, style=_CORAL)
                # 3-colour glyph based on terminal status:
                #   ok      → green ✓
                #   aborted → red ✗  (= explicit failure event)
                #   stuck   → amber ⊘ (= no terminal event; likely a
                #                       crashed / killed prior session)
                status = s["status"]
                if status == "ok":
                    glyph, glyph_style = "✓ ", _STATUS_SUCCESS
                elif status == "stuck":
                    glyph, glyph_style = "⊘ ", "#ffaa44"  # palette-candidate: warning amber — no foundation token yet
                else:
                    glyph, glyph_style = "✗ ", _STATUS_ERROR
                line.append(glyph, style=glyph_style)
                line.append(s["skill_name"], style="#bbbbbb")  # palette-candidate: near-bright label — no foundation token yet
                if s["duration_s"] > 0:
                    line.append(f"  {s['duration_s']:.1f}s", style=_TEXT_DIM)
                if status == "stuck":
                    line.append(
                        f"  (stuck @ {s.get('stuck_at', '?')})",
                        style="#aa8844",  # palette-candidate: warning ochre — no foundation token yet
                    )
                elif status != "ok":
                    line.append(f"  ({status})", style="#aa6655")  # palette-candidate: muted error — no foundation token yet
                if s["ts"]:
                    line.append(f"  {_compact_ts(s['ts'])}", style=_TEXT_DIMMEST)
                recent_node.add(line)
                item_ys.append(y_counter)
                y_counter += 1
                flat_items.append({
                    "kind": "recent_skill",
                    "agent": name,
                    **s,
                })
            for p in recent_plans:
                pfx, _ = _cursor_prefix(len(flat_items))
                line = RichText()
                line.append(pfx, style=_CORAL)
                ok = p["status"] == "ok"
                line.append("✓ " if ok else "✗ ", style=_STATUS_SUCCESS if ok else _STATUS_ERROR)
                line.append("plan ", style=_TEXT_MUTED)
                line.append(p["plan_id"], style=_EVENT_PLAN)
                line.append(
                    f"  {p['n_completed']}/{p['n_completed'] + p['n_failed']}",
                    style="#bbbbbb",  # palette-candidate: near-bright label — no foundation token yet
                )
                if p["status"] == "interrupted" and p["exc_type"]:
                    line.append(f"  {p['exc_type']}", style="#aa6655")  # palette-candidate: muted error — no foundation token yet
                elif p["status"] == "partial":
                    line.append(f"  ({p['n_failed']} failed)", style="#aa6655")  # palette-candidate: muted error — no foundation token yet
                if p["ts"]:
                    line.append(f"  {_compact_ts(p['ts'])}", style=_TEXT_DIMMEST)
                node = recent_node.add(line)
                item_ys.append(y_counter)
                y_counter += 1
                if p["goal"]:
                    goal = p["goal"][:60] + ("…" if len(p["goal"]) > 60 else "")
                    node.add(RichText(goal, style=_TEXT_DIM))
                    y_counter += 1
                flat_items.append({
                    "kind": "recent_plan",
                    "agent": name,
                    **p,
                })

        if not agent_skills and not agent_plans:
            # idle: last activity + message count + recent user snippet
            try:
                last = registry.last_activity_at(name)
                ts_str = last.strftime("%Y-%m-%d %H:%M") if last else None
            except Exception as exc:
                logger.warning(
                    "right_panel agents: registry.last_activity_at(%s) failed: %s",
                    name, exc,
                )
                ts_str = None
            try:
                msg_count = registry.message_count(name)
            except Exception:
                msg_count = 0
            try:
                snippet = registry.recent_user_message(name)
            except Exception:
                snippet = ""
            if ts_str:
                count_part = (
                    f"  ·  {msg_count} message{'s' if msg_count != 1 else ''}"
                    if msg_count > 0 else ""
                )
                tree.add(RichText(
                    f"last: {ts_str}{count_part}", style=_TEXT_DIM,
                ))
                y_counter += 1
                if snippet:
                    # Collapse all whitespace runs to single spaces FIRST,
                    # then truncate. The user's last message may be a
                    # multi-line paste (= e.g. they grabbed the agents
                    # tree via `c` and pasted it back into chat); without
                    # this, the embedded newlines + tree-drawing chars
                    # made the snippet look like a nested sub-tree under
                    # the "↳" node.
                    flattened = " ".join(snippet.split())
                    _max = 60
                    short = (
                        flattened if len(flattened) <= _max
                        else flattened[:_max - 1] + "…"
                    )
                    line2 = RichText()
                    line2.append("↳ ", style=_TEXT_DIM)
                    line2.append(short, style=_TEXT_DIMMEST)
                    tree.add(line2)
                    y_counter += 1

        agent_trees.append(tree)

    # interleave blank lines between agent blocks
    items: list[Any] = []
    for i, tree in enumerate(agent_trees):
        if i > 0:
            items.append(RichText(""))
        items.append(tree)
    return RichGroup(*items), flat_items, item_ys


__all__ = ["render_agents"]
