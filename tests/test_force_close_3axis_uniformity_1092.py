"""Tier 2: 3-axis force-close commonization (#1092 PR-F3 — wave closeout).

The cumulative-axis force-close (#1092) lands on three RouterLoop axes. F3 pins
that they share ONE turn_budget service and degrade UNIFORMLY, and articulates
each axis's deliberate failure-mode difference:

- **chat** (session.py): REACTIVE handoff at the retry_loop terminal (F2b) — a
  live conversation must not be proactively truncated.
- **phase** (phase_executor.py): PROACTIVE force-close + OS-internal re-entry
  (C2/D2) — task execution with a goal wraps up and continues.
- **plan** (planner.py): step-scoped, fresh `history=[]` per step, NO force-close
  — bounded steps backstopped by the FP-0031 step retry (verified independent of
  chat's handoff; activation tracked separately).

Commonization invariants pinned here:
1. The two ACTIVATION axes (chat, phase) build their engine through the SHARED
   helper, and through ``try_build_*`` (graceful None on a sub-viable model), NOT
   ``build_default_*`` (which raises) — so a small-context model degrades
   uniformly across axes instead of one axis crashing.
2. The by-construction floor (progress_margin > 0) holds per axis for a viable
   model, and ``try_build`` yields None (degrade) for a sub-viable one.
3. plan does NOT wire the turn_budget engine (the articulated third failure-mode).
"""
from __future__ import annotations

import ast
from pathlib import Path

import reyn.chat.planner as _planner_mod
import reyn.chat.session as _session_mod
import reyn.core.kernel.phase_executor as _phase_mod
from reyn.services.turn_budget import (
    try_build_default_turn_budget_engine,
)


def _called_names(module) -> set[str]:
    """Names invoked as Call(func=Name(...)) anywhere in a module's source."""
    tree = ast.parse(Path(module.__file__).read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


# ── invariant 1: activation axes share the graceful helper ───────────────────


def test_chat_and_phase_activate_via_try_build_not_build_default() -> None:
    """Tier 2: chat (session) and phase (phase_executor) both activate force-close
    through ``try_build_default_turn_budget_engine`` (graceful) and NEITHER calls
    the raising ``build_default_*`` in its activation path — uniform graceful
    degradation across the two activation axes (the F3 phase retrofit)."""
    for mod in (_session_mod, _phase_mod):
        called = _called_names(mod)
        assert "try_build_default_turn_budget_engine" in called, mod.__name__
        assert "build_default_turn_budget_engine" not in called, (
            f"{mod.__name__} must degrade gracefully (try_build), not raise "
            f"(build_default) — uniform with the other axis"
        )


def test_plan_wires_turn_budget_engine() -> None:
    """Tier 2: #1285 (#1092 plan-axis activation) — plan NOW builds a turn_budget
    engine and wires it into ``_PlanStepHost``, so a long plan step force-closes
    (PR1 FLOOR: the bounded wrap-up consolidation becomes the step output; PR2
    re-enters the same step from it). Uses ``try_build`` (not ``build_default``)
    so a small-context model that cannot satisfy the by-construction floor
    DEGRADES to None (force-close inert) — uniform with phase (PR-F3) + chat (F1):
    all three axes now wire the engine. (Was deferred at #1092 wave time; this
    test flipped when #1285 landed.)"""
    called = _called_names(_planner_mod)
    assert "try_build_default_turn_budget_engine" in called


# ── invariant 2: by-construction floor holds per axis ────────────────────────


def test_by_construction_floor_holds_for_viable_models_all_axes() -> None:
    """Tier 2: the shared helper yields a viable engine (progress_margin > 0 — the
    by-construction force-close floor) for representative chat/phase models, and
    None (degrade) for a sub-viable small-context one — the same gate on every
    axis (no per-axis drift)."""
    for model in ("gpt-4o-mini", "gpt-3.5-turbo"):
        eng = try_build_default_turn_budget_engine(model, use_chars4=True)
        assert eng is not None
        assert eng.budget.progress_margin > 0


def test_sub_viable_model_degrades_uniformly(monkeypatch) -> None:
    """Tier 2: a sub-viable (small-context) model yields None on the shared helper
    — the uniform degrade path both chat (F1) and phase (F3 retrofit) now take,
    instead of an axis-specific construction crash."""
    monkeypatch.setattr(
        "reyn.llm.model_budget.get_max_input_tokens", lambda model, **kw: 2000
    )
    assert try_build_default_turn_budget_engine("tiny", use_chars4=True) is None
