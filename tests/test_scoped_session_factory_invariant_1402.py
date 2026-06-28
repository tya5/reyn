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

- ``Session(...)`` is constructed ONLY in ``runtime/scoped_session_factory.py``
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
import dataclasses
import inspect
from pathlib import Path

from reyn.runtime.factory_config import SessionFactoryConfig
from reyn.runtime.scoped_session_factory import build_scoped_chat_session

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"

# The ONE module allowed to construct Session directly.
_FACTORY_REL = "runtime/scoped_session_factory.py"

# The drift surface, part 1: the PER-SITE scoped capability params that legitimately
# differ per frontend — they MUST stay required (no default) so a factory cannot
# silently omit one. Plus ``factory_config`` (the #2093 bundle) — also required, so a
# factory cannot omit the uniform-config bundle.
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
    "task_backend",  # #1953 slice R: per-session Task backend (per-frontend scoped — I-5=(A))
    "factory_config",  # #2093: the uniform config bundle — required, can't be omitted
})

# The drift surface, part 2 (#2093): the UNIFORM, config-derived args that every site
# threads identically. They moved OFF build_scoped_chat_session's signature INTO the
# SessionFactoryConfig bundle (built once via from_config) — so a new uniform arg is
# added in ONE place and reaches all five sites. The completeness invariant is now
# "these are SessionFactoryConfig fields" (a new one missing from the bundle = drift).
_BUNDLED_UNIFORM = frozenset({
    # → build_scoped_chat_session (8)
    "sandbox_config",
    "multimodal_config",
    "action_retrieval_config",
    "embedding_config",
    "router_config",  # #1829 S3b
    "retry_config",  # #1835
    "tool_calls_op_loop_skills",
    "chat_tool_use_scheme",  # #1593 PR-2
    # → AgentRegistry — where delegation_capability_default drifted (#2081)
    "delegation_capability_default",
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


def _has_factory_config_kw(call: "ast.Call") -> bool:
    return any(k.arg == "factory_config" for k in call.keywords)


def test_registry_gets_the_bundle_wherever_the_session_factory_does() -> None:
    """Tier 2: #2093 — by-CONSTRUCTION on the AgentRegistry side too. A production
    factory file calls ``build_scoped_chat_session(factory_config=…)``; it MUST also
    pass ``factory_config`` to its ``AgentRegistry(…)`` — otherwise the registry's
    uniform config args (delegation_capability_default — the EXACT arg #2093
    protects) silently default.

    The ``build_scoped_chat_session(factory_config=)`` call is the production-factory
    signal, so the 60+ test/utility ``AgentRegistry`` callers (which never call
    build_scoped_chat_session, and legitimately use the individual params / defaults)
    are untouched. Falsifiable: a factory file that builds the bundle for the session
    but omits it from its AgentRegistry fails here, naming file:line."""
    offenders: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        builds_factory_bundle = False
        registry_calls: list[tuple[int, bool]] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id == "build_scoped_chat_session" and _has_factory_config_kw(node):
                builds_factory_bundle = True
            elif node.func.id == "AgentRegistry":
                registry_calls.append((node.lineno, _has_factory_config_kw(node)))
        if builds_factory_bundle:
            rel = str(py.relative_to(_SRC))
            offenders += [f"{rel}:{ln}" for ln, has_fc in registry_calls if not has_fc]
    assert not offenders, (
        "a production factory passes factory_config to build_scoped_chat_session but "
        "NOT to its AgentRegistry — the registry's uniform config args (incl. "
        "delegation_capability_default) silently default (#2093 drift class): "
        f"{offenders}"
    )


def test_chatsession_constructed_only_in_scoped_factory() -> None:
    """Tier 2: #1402 — within src/reyn, ``Session(...)`` is constructed ONLY
    in scoped_session_factory.py; every frontend routes through
    build_scoped_chat_session so a new scoped capability reaches every path by
    construction. Strictly stronger than the former per-config uniformity
    point-tests (subsumes them): any new/unmigrated/hidden construction site in
    src/ fails this, naming file:line."""
    offenders = [s for s in _chatsession_call_sites() if not s.startswith(_FACTORY_REL + ":")]
    assert not offenders, (
        "Session(...) constructed outside runtime/scoped_session_factory.py — "
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


def test_uniform_config_is_single_sourced_in_the_bundle() -> None:
    """Tier 2: #2093 — the UNIFORM, config-derived factory args are SessionFactoryConfig
    fields (the single mapping point ``from_config``), so a new uniform arg is added in
    ONE place and reaches all five sites. A uniform arg NOT in the bundle re-opens the
    per-arg propagation drift (sandbox_config / delegation_capability_default) — this
    fails, naming it."""
    fields = {f.name for f in dataclasses.fields(SessionFactoryConfig)}
    missing = _BUNDLED_UNIFORM - fields
    assert not missing, (
        f"uniform config args missing from SessionFactoryConfig {sorted(missing)} — "
        "re-opens the per-arg propagation drift; add them as bundle fields + in from_config"
    )
    # the bundle holds ONLY the uniform args (no per-site scoped capability leaked in)
    assert not (fields & _REQUIRED_SCOPED), (
        f"per-site scoped args leaked into the bundle {sorted(fields & _REQUIRED_SCOPED)} — "
        "those legitimately differ per site and must stay explicit params"
    )
