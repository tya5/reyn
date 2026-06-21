"""Live plan-vs-task behavioral parity harness (#1953 slice P3, the (b) delete gate).

The real-env complement to the deterministic byte-equal proof
(``tests/test_sliceP3_plan_task_byte_equal_parity_1953.py``). On a set of fixed
decompositions, run BOTH execution engines — ``execute_plan`` (the path P4 deletes)
and ``run_task_graph`` (the Task-driven successor) — through the LIVE LLM, N>=3 each,
and assert structural behavioral-equivalence:

  * every unit produced a non-empty result;
  * each dependent unit RECEIVED its prior context (a dropped result-channel shows
    up as a "need more info" failure — the bug live-parity caught at #1953);
  * the synthesized reply is non-empty.

Variance-robust: each engine runs N>=3 times per goal; parity holds only if both
engines pass every run. This is the gate that — together with (a) byte-equal + the
TUI-surface check + the owner carve-out cross-check — clears the P4 plan delete.

Usage::

    python scripts/dogfood_sliceP3_parity.py [--n 3] [--model gemini/gemini-2.5-flash-lite]

Requires a live LLM (GOOGLE_API_KEY). Exit code 0 iff full parity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# The shared FakeRouterHost lives under tests/_support; reuse it for a lightweight
# host whose only override is identity model resolution (so the real model string
# reaches litellm instead of the test stub's "fake-model-" prefix).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _support.router_loop import FakeRouterHost  # noqa: E402

from reyn.runtime.planner import Plan, PlanStep, execute_plan  # noqa: E402
from reyn.runtime.task_graph import (  # noqa: E402
    build_task_graph,
    make_production_run_unit,
    run_task_graph,
)
from reyn.task import InMemoryTaskBackend  # noqa: E402


class _LiveHost(FakeRouterHost):
    def resolve_model(self, name: str) -> str:  # identity — reach the real model
        return name


# Each goal's final step DEPENDS on prior steps and REQUIRES their content, so a
# dropped result-channel surfaces as a "need more info" failure pattern.
_GOALS: list[tuple[str, str, list[dict]]] = [
    ("facts-synthesis", "state and combine facts about 7 and 8", [
        {"id": "s1", "description": "In one sentence, state a fact about the number 7.", "tools": [], "depends_on": []},
        {"id": "s2", "description": "In one sentence, state a fact about the number 8.", "tools": [], "depends_on": []},
        {"id": "s3", "description": "Combine the two prior facts into a single sentence.", "tools": [], "depends_on": ["s1", "s2"]},
    ]),
    ("linear-chain", "pick a color then describe its mood", [
        {"id": "s1", "description": "Name one primary color in one word.", "tools": [], "depends_on": []},
        {"id": "s2", "description": "In one sentence, describe a mood evoked by the color from the prior step.", "tools": [], "depends_on": ["s1"]},
    ]),
]

_FAIL_PATTERNS = (
    "need more info", "tell me what", "what are the prior", "please provide",
    "please tell me", "i don't have", "no prior", "what were the",
)


def _context_received(text: str) -> bool:
    t = (text or "").lower()
    return bool(t.strip()) and not any(p in t for p in _FAIL_PATTERNS)


async def _run_plan(goal: str, steps: list[dict], model: str) -> dict:
    plan = Plan(goal=goal, steps=tuple(
        PlanStep(id=s["id"], description=s["description"],
                 tools=tuple(s["tools"]), depends_on=tuple(s["depends_on"]))
        for s in steps))
    r = await execute_plan(plan, parent_host=_LiveHost(), chain_id="c",
                           budget=None, router_model=model)
    return {
        "units": {s["id"]: r.step_results.get(s["id"], "") for s in steps},
        "final": r.text,
        "dep_ids": [s["id"] for s in steps if s["depends_on"]],
    }


async def _run_task(goal: str, steps: list[dict], model: str) -> dict:
    b = InMemoryTaskBackend()
    pid = await build_task_graph(
        b, goal=goal, assignee="a2a:s", requester="r", steps=steps)
    run_unit = make_production_run_unit(
        _LiveHost(), chain_id="c", router_model=model, budget=None, goal=goal)
    final = await run_task_graph(b, pid, run_unit=run_unit)
    children = {c.name: c for c in await b.list(parent_id=pid)}
    return {
        "units": {s["id"]: (children[s["description"][:120]].result or "") for s in steps},
        "final": final,
        "dep_ids": [s["id"] for s in steps if s["depends_on"]],
    }


def _assess(run: dict) -> tuple[bool, dict]:
    detail = {
        "all_units_nonempty": all((v or "").strip() for v in run["units"].values()),
        "deps_received_context": all(_context_received(run["units"][d]) for d in run["dep_ids"]),
        "final_nonempty": bool((run["final"] or "").strip()),
    }
    return all(detail.values()), detail


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--model", default="gemini/gemini-2.5-flash-lite")
    args = ap.parse_args()

    report: dict = {}
    parity = True
    for name, goal, steps in _GOALS:
        report[name] = {"plan": [], "task": []}
        for i in range(args.n):
            p_ok, p_d = _assess(await _run_plan(goal, steps, args.model))
            t_ok, t_d = _assess(await _run_task(goal, steps, args.model))
            report[name]["plan"].append({"ok": p_ok, **p_d})
            report[name]["task"].append({"ok": t_ok, **t_d})
            print(f"[{name} run{i + 1}] plan={'PASS' if p_ok else 'FAIL'} | "
                  f"task={'PASS' if t_ok else 'FAIL'}")
    print(f"\n=== PARITY SUMMARY (N={args.n} each) ===")
    for name in report:
        p = sum(r["ok"] for r in report[name]["plan"])
        t = sum(r["ok"] for r in report[name]["task"])
        ok = (p == t == args.n)
        parity = parity and ok
        print(f"  {name}: plan {p}/{args.n}, task {t}/{args.n} -> "
              f"parity={'YES' if ok else 'NO'}")
    out = Path("/tmp/sliceP3_parity_report.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport: {out}  |  overall parity: {'YES' if parity else 'NO'}")
    return 0 if parity else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
