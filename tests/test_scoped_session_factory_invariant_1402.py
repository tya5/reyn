"""Tier 2: #1402 — scoped Session construction is single-sourced (src-wide).

Multiple frontends (chat-CLI / web-deps A2A / mcp-serve / dogfood / chainlit)
built a Session with overlapping-but-divergent scoped wiring. A scoped
capability hand-added to one factory silently leaked from the others — the
forwarding-gap class (sibling to base_dir #1410 / permission-zone #1415 /
exec-seam #1419 / empty-stop #1424). (The issue named "3 factories"; a flow-trace
found 5 — the under-count is the heart of why completeness-by-construction
matters.) The fix: every frontend routes through ``build_scoped_chat_session``,
whose scoped params are required (completeness-by-construction).

This is a PERMANENT drift guard (not scaffold), and it is **src-WIDE**: it
subsumes (strictly stronger) the former per-config uniformity point-tests
(test_session_factory_{sandbox,multimodal}_config_uniform), which checked
"every known Session() call site passes config X / no unknown call sites".

Pinned invariants:

- ``Session(...)`` is constructed ONLY in ``chat/scoped_session_factory.py``
  anywhere in ``src/reyn`` — every other module routes through
  ``build_scoped_chat_session`` (falsifiable: a new/unmigrated/HIDDEN
  construction site anywhere in src/ fails this, naming file:line; this is what
  caught the dogfood/chainlit under-count).
- The scoped capability + per-session config params are REQUIRED keyword-only
  args on ``build_scoped_chat_session`` (no defaults) — so a new factory cannot
  silently omit one (completeness-by-construction). A scoped param gaining a
  default would re-introduce silent-omission drift → this fails.

Cf. [[feedback_multi_callsite_wiring_audit]] / PR #412 precedent (AST-walk
construction-wiring invariant).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from reyn.chat.scoped_session_factory import build_scoped_chat_session

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"

# The ONE module allowed to construct Session directly.
_FACTORY_REL = "chat/scoped_session_factory.py"

# The drift surface: scoped capability + per-session config params that MUST stay
# required so no factory can silently omit one.
_REQUIRED_SCOPED = frozenset({
    # scoped capability (per-frontend; explicit even when None/off)
    "environment_backend",
    "sandbox_backend",
    "workspace_base_dir",
    "workspace_state_dir",
    "exclude_tools",
    "excluded_categories",  # #1667 catalog category opt-out (per-frontend scoped)
    "agent_id",
    "router_max_iterations",
    "non_interactive",  # #1439 Fix #1: run-once SP autonomy flag (per-frontend scoped)
    "eager_embedding_build",
    "allowed_mcp",
    # per-session config (should be UNIFORM across factories)
    "sandbox_config",
    "multimodal_config",
    "action_retrieval_config",
    "embedding_config",
    "tool_calls_op_loop_skills",
    "chat_tool_use_scheme",  # #1593 PR-2 per-layer chat scheme selector
})


def _chatsession_call_sites() -> list[str]:
    sites: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Session"
            ):
                sites.append(f"{rel}:{node.lineno}")
    return sites


def test_chatsession_constructed_only_in_scoped_factory() -> None:
    """Tier 2: #1402 — within src/reyn, ``Session(...)`` is constructed ONLY
    in scoped_session_factory.py; every frontend routes through
    build_scoped_chat_session so a new scoped capability reaches every path by
    construction. Strictly stronger than the former per-config uniformity
    point-tests (subsumes them): any new/unmigrated/hidden construction site in
    src/ fails this, naming file:line."""
    offenders = [s for s in _chatsession_call_sites() if not s.startswith(_FACTORY_REL + ":")]
    assert not offenders, (
        "Session(...) constructed outside chat/scoped_session_factory.py — "
        "re-opens the #1402 drift class; route through build_scoped_chat_session: "
        f"{offenders}"
    )


def test_scoped_factory_is_the_sole_constructor() -> None:
    """Tier 2: #1402 — positive guard: scoped_session_factory.py DOES construct
    Session (the chokepoint exists and is used, not merely imported)."""
    factory_sites = [s for s in _chatsession_call_sites() if s.startswith(_FACTORY_REL + ":")]
    assert factory_sites, (
        "scoped_session_factory.py must construct Session (the single "
        "chokepoint) — none found"
    )


def test_scoped_params_are_required_no_default() -> None:
    """Tier 2: #1402 — the scoped capability + per-session config params are
    required keyword-only args (no default), so a new factory cannot silently
    omit one. A scoped param gaining a default would re-introduce
    silent-omission drift (completeness-by-construction)."""
    sig = inspect.signature(build_scoped_chat_session)
    for pname in _REQUIRED_SCOPED:
        assert pname in sig.parameters, f"{pname} missing from build_scoped_chat_session"
        p = sig.parameters[pname]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, f"{pname} must be keyword-only"
        assert p.default is inspect.Parameter.empty, (
            f"{pname} must be REQUIRED (no default) — a default re-opens "
            "silent-omission drift (#1402 completeness-by-construction)"
        )
