"""Cost tab — aggregates LLM cost / token data from the events store."""
from __future__ import annotations

import datetime
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from reyn.tui._palette import _TEXT_DIMMEST

from .base import (
    _CORAL,
    _EVENT_PLAN,
    _STATUS_CRITICAL,
    _STATUS_SUCCESS,
    _STATUS_SUCCESS_DARK,
    _TEXT_BODY,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MID,
    _TEXT_NEUTRAL,
    _esc,
    logger,
)

# Budget progress-bar geometry (default / wide-panel values)
_BAR_FILL = "█"
_BAR_EMPTY = "░"
_BAR_WIDTH = 24

# Width thresholds for adaptive layout.
# These are *content* widths (after panel padding is removed — padding
# is 2 cols total, 1 each side per CSS ``padding: 0 1``).
#
#   ≥ _WIDE_THRESHOLD   — full layout (current behaviour unchanged)
#   ≥ _MEDIUM_THRESHOLD — narrow the NAME column; keep tok + cost + calls
#   < _MEDIUM_THRESHOLD — NAME narrowed further; drop cost + calls;
#                         ALWAYS keep token total
#
# Budget bar:
#   ≥ _BAR_THRESHOLD    — full bar + percentage label
#   < _BAR_THRESHOLD    — suppress bar, keep percentage label only
_WIDE_THRESHOLD   = 54  # full layout: name :<24 / :<22 / :<28
_MEDIUM_THRESHOLD = 42  # narrow name: :<18 / :<16 / :<22; still show cost
_BAR_THRESHOLD    = 44  # bar suppressed below this; pct label kept


def _col_widths(content_width: int) -> tuple[int, int, int, int]:
    """Return (agent_name_w, skill_name_w, model_name_w, budget_label_w).

    Degrade order as width shrinks:
      - wide  (≥ _WIDE_THRESHOLD):   24, 22, 28, 22
      - medium (≥ _MEDIUM_THRESHOLD): 18, 16, 22, 18
      - narrow (< _MEDIUM_THRESHOLD): 14, 12, 18, 14
    """
    if content_width >= _WIDE_THRESHOLD:
        return 24, 22, 28, 22
    if content_width >= _MEDIUM_THRESHOLD:
        return 18, 16, 22, 18
    return 14, 12, 18, 14


def _show_cost_calls(content_width: int) -> bool:
    """Return True when the content is wide enough to show cost + call count.

    Below _MEDIUM_THRESHOLD these fields are clipped by _PanelContent and
    add noise without being readable — suppress them proactively.
    """
    return content_width >= _MEDIUM_THRESHOLD


def _new_bucket() -> dict:
    return {"p": 0, "c": 0, "cost": 0.0, "calls": 0,
            "has_cost": False, "call_costs": []}


def _cost_str(bucket: dict) -> str:
    if not bucket["has_cost"]:
        return f"[{_TEXT_DIM}]N/A[/]"
    return f"[{_STATUS_SUCCESS}]${bucket['cost']:.4f}[/]"


def _tok(p: int, c: int) -> str:
    """Render token total + prompt/completion breakdown across two lines.

    At a 33%-width panel (~22 cells of content area), the previous
    single-line format ``{total:,} ({p:,}p + {c:,}c)`` reliably clipped
    the breakdown — e.g. ``3,932 (3,…``. Split into two lines so the
    breakdown wraps under the value column instead of overflowing.
    Large totals (1M+ tokens) may still exceed the breakdown line at
    very narrow widths; accepted trade-off — the total is the
    load-bearing number and stays visible on line 1.
    """
    return (
        f"[{_TEXT_BRIGHT}]{p + c:,}[/]\n"
        f"[{_TEXT_DIM}]      ({p:,}p + {c:,}c)[/]"
    )


def _sparkline(values: list[float], width: int = 32) -> str:
    if not values:
        return ""
    recent = values[-width:]
    max_v = max(recent) or 1
    blocks = "▁▂▃▄▅▆▇█"
    bar = "".join(blocks[min(7, int(v / max_v * 8))] for v in recent)
    return f"[{_CORAL}]{bar}[/]"


def _budget_bar(used: float, cap: float, *, bar_width: int = _BAR_WIDTH) -> str:
    """Render a `█░` progress bar with colour thresholds.

    ``bar_width`` controls the cell count of the bar segment; pass 0 to
    suppress the bar entirely and emit only the ``[nnn%]`` readout.
    Green below 75 %, coral 75–90 %, red above 90 %. Returns Rich markup.
    """
    ratio = min(1.0, used / cap) if cap > 0 else 0.0
    pct = int(ratio * 100)
    if ratio >= 0.9:
        colour = _STATUS_CRITICAL
    elif ratio >= 0.75:
        colour = _CORAL
    else:
        colour = _STATUS_SUCCESS
    if bar_width > 0:
        filled = round(ratio * bar_width)
        bar = _BAR_FILL * filled + _BAR_EMPTY * (bar_width - filled)
        return f"[{colour}]\\[{bar}][/] [{colour}]{pct:3d}%[/]"
    # No bar — emit percentage only (keeps the readout visible at narrow widths)
    return f"[{colour}]{pct:3d}%[/]"


def _render_budget_caps(
    lines: list[str],
    budget_tracker: Any,
    *,
    content_width: int = 0,
) -> None:
    """Append daily token / cost progress bars when caps are configured.

    Reads `daily_tokens.hard_limit` and `daily_cost_usd.hard_limit` from
    ``budget_tracker.snapshot()["config"]``. Skips silently when caps are
    unset or the snapshot shape changes.

    ``content_width`` controls adaptive rendering: below ``_BAR_THRESHOLD``
    the bar segment is suppressed (= only the ``[nnn%]`` readout is shown)
    to avoid clipping the percentage label at narrow panel widths.
    """
    try:
        snap = budget_tracker.snapshot()
        cfg = snap.get("config")
        if cfg is None:
            return
        daily_tok_used = snap.get("daily_tokens", 0)
        daily_cost_used = snap.get("daily_cost_usd", 0.0)
        tok_cap_cfg = getattr(cfg, "daily_tokens", None)
        cost_cap_cfg = getattr(cfg, "daily_cost_usd", None)
        tok_hard = getattr(tok_cap_cfg, "hard_limit", None) if tok_cap_cfg else None
        cost_hard = getattr(cost_cap_cfg, "hard_limit", None) if cost_cap_cfg else None
        _, _, _, budget_label_w = _col_widths(content_width)
        bar_w = _BAR_WIDTH if content_width >= _BAR_THRESHOLD else 0
        if tok_hard and tok_hard > 0:
            label = f"{daily_tok_used:,} / {int(tok_hard):,}"
            bar = _budget_bar(daily_tok_used, tok_hard, bar_width=bar_w)
            lines.append(
                f"[{_TEXT_DIM}]    tokens  [/][{_TEXT_BODY}]{label:<{budget_label_w}}[/]  {bar}"
            )
        if cost_hard and cost_hard > 0:
            label = f"${daily_cost_used:.4f} / ${cost_hard:.4f}"
            bar = _budget_bar(daily_cost_used, cost_hard, bar_width=bar_w)
            lines.append(
                f"[{_TEXT_DIM}]    cost    [/][{_TEXT_BODY}]{label:<{budget_label_w}}[/]  {bar}"
            )
    except Exception as exc:
        logger.warning("right_panel cost: budget cap render failed: %s", exc)


def render_cost(
    project_root: Path | None,
    budget_tracker: Any = None,
    *,
    content_width: int = 0,
) -> str:
    """Render the cost tab, summarising LLM usage from .reyn/events/*.jsonl.

    ``content_width`` is the available content-area width in columns (i.e.
    the panel width minus horizontal padding). When 0 or unknown the full
    wide-panel layout is used. As the width shrinks the renderer degrades:
    first the name columns narrow, then cost + call-count fields are
    suppressed, while the token total is always kept visible on its own
    line. The budget bar is replaced by a percentage-only readout below
    ``_BAR_THRESHOLD`` content columns.
    """
    lines: list[str] = []
    agent_name_w, skill_name_w, model_name_w, _budget_label_w = _col_widths(
        content_width
    )
    show_extra = _show_cost_calls(content_width)

    if project_root is None:
        lines.append(f"[{_TEXT_DIM}]  (no project root)[/]")
        return "\n".join(lines)

    events_root = project_root / ".reyn" / "events"
    if not events_root.is_dir():
        lines.append(f"[{_TEXT_DIM}]  (no events yet)[/]")
        return "\n".join(lines)

    today_str = datetime.date.today().isoformat()

    today = _new_bucket()
    total = _new_bucket()
    by_agent: dict[str, dict] = defaultdict(_new_bucket)
    # agent → skill → bucket
    by_agent_skill: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(_new_bucket)
    )
    # Per-model bucket (parsed from llm_called events)
    by_model: dict[str, dict] = defaultdict(_new_bucket)
    # Plan-mode (ADR-0022 / 0023). Cost attribution: while we're inside a
    # plan_step (= between plan_step_started and plan_step_{completed,failed}
    # within the same file), each llm_response_received contributes to both
    # the (plan_id) and the (plan_id, step_id) buckets. plan_emitted gives us
    # the human-readable goal so the BY PLAN section shows what the plan was
    # actually trying to do, not just an opaque uuid.
    by_plan: dict[str, dict] = defaultdict(_new_bucket)
    by_plan_step: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(_new_bucket)
    )
    plan_goals: dict[str, str] = {}

    for jsonl in sorted(events_root.rglob("*.jsonl")):
        try:
            rel = jsonl.relative_to(events_root)
            parts = rel.parts
            # agent attribution from path: agents/<name>/skill_runs/...
            if parts[0] == "agents" and len(parts) >= 2:
                agent = parts[1]
            elif parts[0] == "direct":
                agent = "direct"
            else:
                agent = "?"
            # skill name from filename suffix (only for skill_runs files)
            is_skill_run = "skill_runs" in parts
            if is_skill_run:
                stem = jsonl.stem  # e.g. "2026-05-04T120000_skill_router"
                skill = stem.split("_", 1)[1] if "_" in stem else stem
            else:
                skill = "(chat)"

            pending_model: str = "unknown"
            # Plan attribution state (per-file). When inside an active step
            # (= we've seen plan_step_started without a matching completion),
            # llm_response_received also contributes to that plan + step.
            current_plan_id: str | None = None
            current_step_id: str | None = None
            for raw in jsonl.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                    ev_type = ev.get("type")
                    d = ev.get("data") or {}
                    if ev_type == "plan_emitted":
                        pid = str(d.get("plan_id", ""))
                        if pid:
                            plan_goals[pid] = str(d.get("goal", ""))
                        continue
                    if ev_type == "plan_step_started":
                        current_plan_id = str(d.get("plan_id", "")) or None
                        current_step_id = str(d.get("step_id", "")) or None
                        continue
                    if ev_type in ("plan_step_completed", "plan_step_failed"):
                        # Close the active step. Defensive: if plan_id changed
                        # mid-stream (shouldn't happen) we still reset both.
                        current_plan_id = None
                        current_step_id = None
                        continue
                    if ev_type == "llm_called":
                        raw_model = str(d.get("model", "unknown"))
                        # Strip the litellm proxy prefix (e.g.
                        # ``openai/gemini-2.5-flash-lite`` → ``gemini-2.5-flash-lite``)
                        # so the BY MODEL section shows the human-readable
                        # model identifier rather than the routing path.
                        # ``rsplit("/", 1)[-1]`` is idempotent for already-clean
                        # names (no slash → returned verbatim).
                        pending_model = raw_model.rsplit("/", 1)[-1]
                        continue
                    if ev_type != "llm_response_received":
                        continue
                    pt = int(d.get("prompt_tokens", 0) or 0)
                    ct = int(d.get("completion_tokens", 0) or 0)
                    raw_cost = d.get("cost_usd")
                    cost = float(raw_cost) if raw_cost is not None else 0.0
                    has_cost = raw_cost is not None
                    ts = str(ev.get("timestamp", ""))

                    for bucket in (total, by_agent[agent], by_agent_skill[agent][skill]):
                        bucket["p"] += pt; bucket["c"] += ct
                        bucket["cost"] += cost; bucket["calls"] += 1
                        if has_cost:
                            bucket["has_cost"] = True
                        bucket["call_costs"].append(cost)

                    # Per-model accumulation
                    mb = by_model[pending_model]
                    mb["p"] += pt; mb["c"] += ct
                    mb["cost"] += cost; mb["calls"] += 1
                    if has_cost:
                        mb["has_cost"] = True
                    mb["call_costs"].append(cost)

                    # Plan attribution — additive view on top of by_agent/skill,
                    # not a replacement. Same call counts toward both.
                    if current_plan_id:
                        for bucket in (
                            by_plan[current_plan_id],
                            by_plan_step[current_plan_id][current_step_id or "?"],
                        ):
                            bucket["p"] += pt; bucket["c"] += ct
                            bucket["cost"] += cost; bucket["calls"] += 1
                            if has_cost:
                                bucket["has_cost"] = True
                            bucket["call_costs"].append(cost)

                    if ts.startswith(today_str):
                        today["p"] += pt; today["c"] += ct
                        today["cost"] += cost; today["calls"] += 1
                        if has_cost:
                            today["has_cost"] = True
                        today["call_costs"].append(cost)
                except Exception as exc:
                    logger.warning(
                        "right_panel cost: malformed event in %s: %s",
                        jsonl, exc,
                    )
        except Exception as exc:
            logger.warning(
                "right_panel cost: read of %s failed: %s", jsonl, exc,
            )

    # Disclaimer — costs displayed are client-side estimates derived
    # from litellm's pricing DB at LLM-call time. They may diverge from
    # the actual amount billed by the upstream provider (= rate changes
    # between call time and the bill cycle, special pricing tiers,
    # unmetered features, etc.) and from any proxy-side accounting.
    # Split across two short lines so the disclaimer survives narrow
    # panel widths (= 44 cells minimum, see SP1) — a single 62-cell
    # line clipped to ``(litellm estimate — may differ from …)`` and
    # the actionable half ("from actual provider billing") never
    # reached the user.
    lines.append(f"[{_TEXT_DIM}]  (litellm estimate —[/]")
    lines.append(f"[{_TEXT_DIM}]   may differ from actual billing)[/]")
    lines.append("")

    # ── TODAY ────────────────────────────────────────────────────────
    lines.append(f"[bold {_TEXT_BODY}]  TODAY[/]")
    if today["calls"] == 0:
        lines.append(f"[{_TEXT_DIM}]    (no calls today)[/]")
    else:
        lines.append(f"[{_TEXT_DIM}]    tokens  [/]{_tok(today['p'], today['c'])}")
        lines.append(f"[{_TEXT_DIM}]    cost    [/]{_cost_str(today)}")
        lines.append(f"[{_TEXT_DIM}]    calls   [/][{_TEXT_BRIGHT}]{today['calls']}[/]")
        spark = _sparkline(today["call_costs"])
        if spark:
            lines.append(f"[{_TEXT_DIM}]    trend   [/]{spark}")
        # Budget cap progress bars (only when caps are configured)
        if budget_tracker is not None:
            _render_budget_caps(
                lines, budget_tracker, content_width=content_width
            )
    lines.append("")

    # ── ALL TIME ──────────────────────────────────────────────────────
    lines.append(f"[bold {_TEXT_BODY}]  ALL TIME[/]")
    if total["calls"] == 0:
        lines.append(f"[{_TEXT_DIM}]    (no LLM calls)[/]")
    else:
        lines.append(f"[{_TEXT_DIM}]    tokens  [/]{_tok(total['p'], total['c'])}")
        lines.append(f"[{_TEXT_DIM}]    cost    [/]{_cost_str(total)}")
        lines.append(f"[{_TEXT_DIM}]    calls   [/][{_TEXT_BRIGHT}]{total['calls']}[/]")
        spark = _sparkline(total["call_costs"])
        if spark:
            lines.append(f"[{_TEXT_DIM}]    trend   [/]{spark}")
    lines.append("")

    # ── BY AGENT / SKILL ──────────────────────────────────────────────
    lines.append(f"[bold {_TEXT_BODY}]  BY AGENT / SKILL[/]")
    if by_agent_skill:
        for agent in sorted(by_agent_skill):
            ag = by_agent[agent]
            ag_tok = ag["p"] + ag["c"]
            # agent total: name bold white, tok light gray, cost bright green
            ag_cost = (
                f"  [bold {_STATUS_SUCCESS}]${ag['cost']:.4f}[/]"
                if ag["has_cost"] and show_extra else ""
            )
            ag_calls = (
                f"  [{_TEXT_MID}]{ag['calls']}c[/]"                if show_extra else ""
            )
            # name col width adapts to content_width (see _col_widths)
            lines.append(
                f"[bold {_TEXT_BRIGHT}]  {_esc(agent):<{agent_name_w}}[/]"
                f"[{_TEXT_BODY}]{ag_tok:>7,} tok[/]"
                f"{ag_cost}"
                f"{ag_calls}"
            )
            skills = by_agent_skill[agent]
            for skill in sorted(skills):
                m = skills[skill]
                tok_total = m["p"] + m["c"]
                # skill rows: dim name, muted green for cost — clearly subordinate
                cost_part = (
                    f"  [{_STATUS_SUCCESS_DARK}]${m['cost']:.4f}[/]"
                    if m["has_cost"] and show_extra else ""
                )
                calls_part = (
                    f"  [{_TEXT_DIMMEST}]{m['calls']}c[/]"
                    if show_extra else ""
                )
                lines.append(
                    f"[{_TEXT_DIM}]    {_esc(skill):<{skill_name_w}}[/]"
                    f"[{_TEXT_DIM}]{tok_total:>7,} tok[/]"
                    f"{cost_part}"
                    f"{calls_part}"
                )
            lines.append("")
    else:
        lines.append(f"[{_TEXT_DIM}]    (no skill runs yet)[/]")
    lines.append("")

    # ── BY PLAN ──────────────────────────────────────────────────────
    # Only show when plans have actually run (avoid noise when nobody is
    # using plan-mode). Sort plans by descending cost so the expensive
    # ones surface first.
    if by_plan:
        lines.append(f"[bold {_TEXT_BODY}]  BY PLAN[/]")
        sorted_plans = sorted(
            by_plan.items(),
            key=lambda kv: (-kv[1]["cost"], -(kv[1]["p"] + kv[1]["c"])),
        )
        for plan_id, pb in sorted_plans:
            tok_total = pb["p"] + pb["c"]
            cost_part = (
                f"  [bold {_STATUS_SUCCESS}]${pb['cost']:.4f}[/]"
                if pb["has_cost"] and show_extra else ""
            )
            calls_part = (
                f"  [{_TEXT_MID}]{pb['calls']}c[/]"                if show_extra else ""
            )
            short_pid = plan_id[:8]
            goal = plan_goals.get(plan_id, "")
            goal_part = (
                f"  [{_TEXT_NEUTRAL}]{_esc(goal[:32] + ('…' if len(goal) > 32 else ''))}[/]"
                if goal and show_extra else ""
            )
            lines.append(
                f"[bold {_EVENT_PLAN}]  {short_pid:<8}[/]"
                f"  [{_TEXT_BODY}]{tok_total:>7,} tok[/]"
                f"{cost_part}"
                f"{calls_part}"
                f"{goal_part}"
            )
            steps = by_plan_step.get(plan_id, {})
            for step_id in sorted(steps):
                sb = steps[step_id]
                step_tok = sb["p"] + sb["c"]
                step_cost_part = (
                    f"  [{_STATUS_SUCCESS_DARK}]${sb['cost']:.4f}[/]"
                    if sb["has_cost"] and show_extra else ""
                )
                step_calls_part = (
                    f"  [{_TEXT_DIMMEST}]{sb['calls']}c[/]"
                    if show_extra else ""
                )
                lines.append(
                    f"[{_TEXT_DIM}]    {_esc(step_id):<{skill_name_w}}[/]"
                    f"[{_TEXT_DIM}]{step_tok:>7,} tok[/]"
                    f"{step_cost_part}"
                    f"{step_calls_part}"
                )
            lines.append("")
        lines.append("")

    # ── BY MODEL ─────────────────────────────────────────────────────
    lines.append(f"[bold {_TEXT_BODY}]  BY MODEL[/]")
    if by_model:
        sorted_models = sorted(
            by_model.items(),
            key=lambda kv: (-kv[1]["cost"], -(kv[1]["p"] + kv[1]["c"])),
        )
        for model_name, mb in sorted_models:
            tok_total = mb["p"] + mb["c"]
            cost_part = (
                f"  [{_STATUS_SUCCESS}]${mb['cost']:.4f}[/]"
                if mb["has_cost"] and show_extra else ""
            )
            calls_part = (
                f"  [{_TEXT_MID}]{mb['calls']}c[/]"                if show_extra else ""
            )
            lines.append(
                f"[{_TEXT_BODY}]  {_esc(model_name):<{model_name_w}}[/]"
                f"[{_TEXT_DIM}]{tok_total:>7,} tok[/]"
                f"{cost_part}"
                f"{calls_part}"
            )
    else:
        lines.append(f"[{_TEXT_DIM}]    (no model data yet)[/]")

    # Footer hint: ``/budget reset`` only clears per-agent + per-chain
    # counters used by the safety hard-stop rate limiter. The TODAY /
    # ALL TIME / BY MODEL totals shown above are recomputed from the
    # event log on every render, so they survive ``/budget reset``.
    # Users who run reset expecting "all the numbers go to zero" need
    # this distinction surfaced once, where they can see it.
    lines.append("")
    lines.append(
        f"[{_TEXT_DIM}]  /budget reset clears per-agent counters "
        "(daily totals persist — they come from the event log)[/]"
    )

    return "\n".join(lines)


__all__ = ["render_cost"]
