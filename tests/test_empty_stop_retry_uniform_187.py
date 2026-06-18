"""Tier 2: #187 — the empty-stop retry is UNIFORM and always-on at every site.

owner decision (2026-06-07): a content-less empty stop (finish_reason=stop, no
content, no tool_calls) is recovered by a single content-neutral ``"resume"``
continuation turn, applied UNIFORMLY at all RouterLoop construction sites
(chat / plan-step / agent op-loop) — no per-site or per-tier directive
differentiation. The previous per-site directives (chat "write your reply" /
plan "step report", both ending "Do not call another tool") were unevidenced
differentiation, and that anti-invoke framing was itself suspect on the agent
path (real-task: a content-less stop is 67% premature; "resume" recovers the
next action — invoke 11/12). Iterate per-site ONLY on measured evidence.

Pinned invariants:

- The single shared ``EMPTY_STOP_RETRY_DIRECTIVE`` is verbatim ``"resume"``.
- Every RouterLoop(...) construction in the three threading modules
  (session.py / planner.py / phase_executor.py) passes BOTH
  ``empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE`` (the shared name,
  not an inlined string or a reintroduced per-site constant) AND
  ``empty_stop_retry_auto=True`` (always-on; env-gate retired). Verified by AST
  so a later refactor that drops the kwarg, inlines a literal, or reintroduces
  per-site differentiation fails loudly — the same construction-wiring-guard
  genre as ``test_routerloop_convergence_entrypoint_parity_1092``.

The behavioural proof (auto=True fires the retry WITHOUT the env var) lives in
``test_router_loop_empty_stop_retry.test_auto_flag_fires_retry_without_env_var``;
the live chat-construction proof lives in
``test_chat_router_empty_stop_directive_wired``. This file pins the static
uniform-wiring invariant across all three sites.

References:
- #1402 autonomous-construction class: phase_executor reuses chat-shaped
  construction; the agent-path directive was previously dropped (None) → the
  retry was unreachable on the agent op-loop (construction-forwarding gap).
- #1092↔#187 reconciliation: agent op-loop content-less-stop = premature
  empty-stop; nudge-once is bounded so op-loop convergence + FD2 finish are
  preserved (a legitimate clean end emits content or is FD2-terminated).
"""
from __future__ import annotations

import ast
from pathlib import Path

import reyn.core.kernel.phase_executor as pe_mod
import reyn.runtime.planner as planner_mod
import reyn.runtime.services.router_loop_driver as loop_driver_mod
import reyn.runtime.session as session_mod  # used for src-root path only
from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE

_DIRECTIVE_NAME = "EMPTY_STOP_RETRY_DIRECTIVE"
# RouterLoop construction moved from session.py → router_loop_driver.py (PR-3).
_WIRING_MODULES = (loop_driver_mod, planner_mod, pe_mod)


# ---------------------------------------------------------------------------
# Directive content — verbatim "resume"
# ---------------------------------------------------------------------------


def test_shared_directive_is_verbatim_resume() -> None:
    """Tier 2: #187 — the single shared empty-stop directive is verbatim
    ``"resume"`` (content-neutral, the patch-verified winner). Pin the exact
    string: elaboration is a separate patch-verify experiment, not the landing
    default, and per-site content is forbidden by the owner decision."""
    assert EMPTY_STOP_RETRY_DIRECTIVE == "resume"


# ---------------------------------------------------------------------------
# Cross-site construction-wiring invariant (AST) — uniform + always-on
# ---------------------------------------------------------------------------


def _routerloop_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RouterLoop"
    ]


def _wires_uniform_empty_stop(call: ast.Call) -> bool:
    """True if this RouterLoop(...) passes the shared directive name +
    empty_stop_retry_auto=True (uniform always-on)."""
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    directive = kw.get("empty_stop_retry_directive")
    auto = kw.get("empty_stop_retry_auto")
    directive_ok = (
        isinstance(directive, ast.Name) and directive.id == _DIRECTIVE_NAME
    )
    auto_ok = isinstance(auto, ast.Constant) and auto.value is True
    return directive_ok and auto_ok


def test_every_routerloop_site_wires_uniform_resume_always_on() -> None:
    """Tier 2: #187 — every RouterLoop construction in session.py / planner.py /
    phase_executor.py passes ``empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE``
    AND ``empty_stop_retry_auto=True``. Falsifiable: drop the kwarg, inline a
    literal, or reintroduce a per-site constant at any site → this fails, naming
    the offending module. Guards both the construction-forwarding gap (agent
    path was None) and the owner no-per-site-differentiation decision."""
    offenders: list[str] = []
    for mod in _WIRING_MODULES:
        # Read the ACTUAL loaded module file (not a guessed path) so the test
        # tracks editable-install / worktree drift faithfully.
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        calls = _routerloop_calls(tree)
        if not calls:
            offenders.append(f"{mod.__name__}: no RouterLoop(...) construction found")
            continue
        for call in calls:
            if not _wires_uniform_empty_stop(call):
                offenders.append(
                    f"{mod.__name__}:{getattr(call, 'lineno', '?')} "
                    "RouterLoop(...) does not pass "
                    "empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE + "
                    "empty_stop_retry_auto=True"
                )
    assert not offenders, (
        "every RouterLoop construction site must wire the uniform always-on "
        "empty-stop retry (#187 owner decision). Offenders: " + str(offenders)
    )


def test_no_per_site_directive_constants_reintroduced() -> None:
    """Tier 2: #187 — the retired per-site directive constants must NOT be
    reintroduced (owner: no per-site differentiation). A new
    ``_*_EMPTY_STOP_RETRY_DIRECTIVE`` assignment anywhere in src is a regression
    toward the differentiation the uniform decision removed."""
    src = Path(session_mod.__file__).resolve().parents[1]
    retired = (
        "_CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE",
        "_PLAN_STEP_EMPTY_STOP_RETRY_DIRECTIVE",
        "_AGENT_EMPTY_STOP_RETRY_DIRECTIVE",
    )
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in retired:
                offenders.append(f"{py.relative_to(src.parent)}:{node.lineno}")
    assert not offenders, (
        "retired per-site empty-stop directive constants reintroduced "
        f"(use the shared EMPTY_STOP_RETRY_DIRECTIVE instead): {sorted(set(offenders))}"
    )
