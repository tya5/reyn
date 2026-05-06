"""Cost tab — aggregates LLM cost / token data from the events store."""
from __future__ import annotations

import datetime
import json
from collections import defaultdict
from pathlib import Path

from .base import _CORAL, _esc, logger


def _new_bucket() -> dict:
    return {"p": 0, "c": 0, "cost": 0.0, "calls": 0,
            "has_cost": False, "call_costs": []}


def _cost_str(bucket: dict) -> str:
    if not bucket["has_cost"]:
        return "[#555555]N/A[/]"
    return f"[#44cc88]${bucket['cost']:.4f}[/]"


def _tok(p: int, c: int) -> str:
    return f"[#dddddd]{p + c:,}[/] [#555555]({p:,}p + {c:,}c)[/]"


def _sparkline(values: list[float], width: int = 32) -> str:
    if not values:
        return ""
    recent = values[-width:]
    max_v = max(recent) or 1
    blocks = "▁▂▃▄▅▆▇█"
    bar = "".join(blocks[min(7, int(v / max_v * 8))] for v in recent)
    return f"[{_CORAL}]{bar}[/]"


def render_cost(project_root: Path | None) -> str:
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
            for raw in jsonl.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                    ev_type = ev.get("type")
                    d = ev.get("data") or {}
                    if ev_type == "llm_called":
                        pending_model = str(d.get("model", "unknown"))
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
            # pending_model retained for symmetry with future per-call attribution
            del pending_model
        except Exception as exc:
            logger.warning(
                "right_panel cost: read of %s failed: %s", jsonl, exc,
            )

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

    return "\n".join(lines)


__all__ = ["render_cost"]
