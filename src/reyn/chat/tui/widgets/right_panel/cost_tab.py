"""Cost tab — aggregates LLM cost / token data from the events store."""
from __future__ import annotations

import datetime
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .base import _CORAL, _esc, logger

# Budget progress-bar geometry
_BAR_FILL = "█"
_BAR_EMPTY = "░"
_BAR_WIDTH = 24


def _new_bucket() -> dict:
    return {"p": 0, "c": 0, "cost": 0.0, "calls": 0,
            "has_cost": False, "call_costs": []}


def _cost_str(bucket: dict) -> str:
    if not bucket["has_cost"]:
        return "[#555555]N/A[/]"
    return f"[#44cc88]${bucket['cost']:.4f}[/]"


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
        f"[#dddddd]{p + c:,}[/]\n"
        f"[#555555]      ({p:,}p + {c:,}c)[/]"
    )


def _sparkline(values: list[float], width: int = 32) -> str:
    if not values:
        return ""
    recent = values[-width:]
    max_v = max(recent) or 1
    blocks = "▁▂▃▄▅▆▇█"
    bar = "".join(blocks[min(7, int(v / max_v * 8))] for v in recent)
    return f"[{_CORAL}]{bar}[/]"


def _budget_bar(used: float, cap: float) -> str:
    """Render a 24-cell `█░` progress bar with colour thresholds.

    Green below 75 %, coral 75–90 %, red above 90 %. Returns Rich markup.
    """
    ratio = min(1.0, used / cap) if cap > 0 else 0.0
    filled = round(ratio * _BAR_WIDTH)
    pct = int(ratio * 100)
    if ratio >= 0.9:
        colour = "#ff4444"
    elif ratio >= 0.75:
        colour = _CORAL
    else:
        colour = "#44cc88"
    bar = _BAR_FILL * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)
    return f"[{colour}]\\[{bar}][/] [{colour}]{pct:3d}%[/]"


def _render_budget_caps(lines: list[str], budget_tracker: Any) -> None:
    """Append daily token / cost progress bars when caps are configured.

    Reads `daily_tokens.hard_limit` and `daily_cost_usd.hard_limit` from
    ``budget_tracker.snapshot()["config"]``. Skips silently when caps are
    unset or the snapshot shape changes.
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
        if tok_hard and tok_hard > 0:
            label = f"{daily_tok_used:,} / {int(tok_hard):,}"
            bar = _budget_bar(daily_tok_used, tok_hard)
            lines.append(
                f"[#555555]    tokens  [/][#aaaaaa]{label:<22}[/]  {bar}"
            )
        if cost_hard and cost_hard > 0:
            label = f"${daily_cost_used:.4f} / ${cost_hard:.4f}"
            bar = _budget_bar(daily_cost_used, cost_hard)
            lines.append(
                f"[#555555]    cost    [/][#aaaaaa]{label:<22}[/]  {bar}"
            )
    except Exception as exc:
        logger.warning("right_panel cost: budget cap render failed: %s", exc)


def render_cost(
    project_root: Path | None,
    budget_tracker: Any = None,
) -> str:
    """Render the cost tab, summarising LLM usage from .reyn/events/*.jsonl."""
    lines: list[str] = []

    if project_root is None:
        lines.append("[#555555]  (no project root)[/]")
        return "\n".join(lines)

    events_root = project_root / ".reyn" / "events"
    if not events_root.is_dir():
        lines.append("[#555555]  (no events yet)[/]")
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

    # Disclaimer line — costs displayed are client-side estimates derived
    # from litellm's pricing DB at LLM-call time. They may diverge from
    # the actual amount billed by the upstream provider (= rate changes
    # between call time and the bill cycle, special pricing tiers,
    # unmetered features, etc.) and from any proxy-side accounting.
    # Surfacing this once at the top of the Cost tab prevents the user
    # from treating the displayed cents as a billing-authoritative source.
    lines.append(
        "[#555555]  (litellm estimate — may differ from actual provider billing)[/]"
    )
    lines.append("")

    # ── TODAY ────────────────────────────────────────────────────────
    lines.append("[bold #aaaaaa]  TODAY[/]")
    if today["calls"] == 0:
        lines.append("[#555555]    (no calls today)[/]")
    else:
        lines.append(f"[#555555]    tokens  [/]{_tok(today['p'], today['c'])}")
        lines.append(f"[#555555]    cost    [/]{_cost_str(today)}")
        lines.append(f"[#555555]    calls   [/][#dddddd]{today['calls']}[/]")
        spark = _sparkline(today["call_costs"])
        if spark:
            lines.append(f"[#555555]    trend   [/]{spark}")
        # Budget cap progress bars (only when caps are configured)
        if budget_tracker is not None:
            _render_budget_caps(lines, budget_tracker)
    lines.append("")

    # ── ALL TIME ──────────────────────────────────────────────────────
    lines.append("[bold #aaaaaa]  ALL TIME[/]")
    if total["calls"] == 0:
        lines.append("[#555555]    (no LLM calls)[/]")
    else:
        lines.append(f"[#555555]    tokens  [/]{_tok(total['p'], total['c'])}")
        lines.append(f"[#555555]    cost    [/]{_cost_str(total)}")
        lines.append(f"[#555555]    calls   [/][#dddddd]{total['calls']}[/]")
        spark = _sparkline(total["call_costs"])
        if spark:
            lines.append(f"[#555555]    trend   [/]{spark}")
    lines.append("")

    # ── BY AGENT / SKILL ──────────────────────────────────────────────
    lines.append("[bold #aaaaaa]  BY AGENT / SKILL[/]")
    if by_agent_skill:
        for agent in sorted(by_agent_skill):
            ag = by_agent[agent]
            ag_tok = ag["p"] + ag["c"]
            # agent total: name bold white, tok light gray, cost bright green
            ag_cost = (
                f"  [bold #44cc88]${ag['cost']:.4f}[/]"
                if ag["has_cost"] else ""
            )
            # name col = 26 chars (2 indent + 24) to align with skill rows (4 + 22)
            lines.append(
                f"[bold #dddddd]  {_esc(agent):<24}[/]"
                f"[#aaaaaa]{ag_tok:>7,} tok[/]"
                f"{ag_cost}"
                f"  [#777777]{ag['calls']}c[/]"
            )
            skills = by_agent_skill[agent]
            for skill in sorted(skills):
                m = skills[skill]
                tok_total = m["p"] + m["c"]
                # skill rows: dim name, muted green for cost — clearly subordinate
                cost_part = (
                    f"  [#2d7a4f]${m['cost']:.4f}[/]"
                    if m["has_cost"] else ""
                )
                lines.append(
                    f"[#555555]    {_esc(skill):<22}[/]"
                    f"[#555555]{tok_total:>7,} tok[/]"
                    f"{cost_part}"
                    f"  [#444444]{m['calls']}c[/]"
                )
            lines.append("")
    else:
        lines.append("[#555555]    (no skill runs yet)[/]")
    lines.append("")

    # ── BY PLAN ──────────────────────────────────────────────────────
    # Only show when plans have actually run (avoid noise when nobody is
    # using plan-mode). Sort plans by descending cost so the expensive
    # ones surface first.
    if by_plan:
        lines.append("[bold #aaaaaa]  BY PLAN[/]")
        sorted_plans = sorted(
            by_plan.items(),
            key=lambda kv: (-kv[1]["cost"], -(kv[1]["p"] + kv[1]["c"])),
        )
        for plan_id, pb in sorted_plans:
            tok_total = pb["p"] + pb["c"]
            cost_part = (
                f"  [bold #44cc88]${pb['cost']:.4f}[/]"
                if pb["has_cost"] else ""
            )
            short_pid = plan_id[:8]
            goal = plan_goals.get(plan_id, "")
            goal_part = (
                f"  [#666666]{_esc(goal[:32] + ('…' if len(goal) > 32 else ''))}[/]"
                if goal else ""
            )
            lines.append(
                f"[bold #ff9944]  {short_pid:<8}[/]"
                f"  [#aaaaaa]{tok_total:>7,} tok[/]"
                f"{cost_part}"
                f"  [#777777]{pb['calls']}c[/]"
                f"{goal_part}"
            )
            steps = by_plan_step.get(plan_id, {})
            for step_id in sorted(steps):
                sb = steps[step_id]
                step_tok = sb["p"] + sb["c"]
                step_cost_part = (
                    f"  [#2d7a4f]${sb['cost']:.4f}[/]"
                    if sb["has_cost"] else ""
                )
                lines.append(
                    f"[#555555]    {_esc(step_id):<22}[/]"
                    f"[#555555]{step_tok:>7,} tok[/]"
                    f"{step_cost_part}"
                    f"  [#444444]{sb['calls']}c[/]"
                )
            lines.append("")
        lines.append("")

    # ── BY MODEL ─────────────────────────────────────────────────────
    lines.append("[bold #aaaaaa]  BY MODEL[/]")
    if by_model:
        sorted_models = sorted(
            by_model.items(),
            key=lambda kv: (-kv[1]["cost"], -(kv[1]["p"] + kv[1]["c"])),
        )
        for model_name, mb in sorted_models:
            tok_total = mb["p"] + mb["c"]
            cost_part = (
                f"  [#44cc88]${mb['cost']:.4f}[/]"
                if mb["has_cost"] else ""
            )
            lines.append(
                f"[#aaaaaa]  {_esc(model_name):<28}[/]"
                f"[#555555]{tok_total:>7,} tok[/]"
                f"{cost_part}"
                f"  [#777777]{mb['calls']}c[/]"
            )
    else:
        lines.append("[#555555]    (no model data yet)[/]")

    return "\n".join(lines)


__all__ = ["render_cost"]
