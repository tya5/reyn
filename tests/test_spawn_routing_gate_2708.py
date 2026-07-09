"""Tier 2: OS invariant — #2708 P3-item3 spawn-axis user-reaching completeness gate.

The construction-axis gate (#1402 / #2708 P1) made a Session un-buildable without a declared
present sink. This is the SAME three mechanisms generalized to the SPAWN axis, so a spawn whose
child reaches the user (``present`` / ``ask_user``) cannot silently fail to route to the parent:

1. **Required-kwarg forcing** — ``presentation_consumer`` + ``intervention_bridge`` are REQUIRED,
   no-default keyword-only params of the three spawn seams (``AgentRegistry.spawn_session`` /
   ``.spawn_session_recorded`` / ``session_api.spawn_ephemeral_session``). Pinned by
   ``inspect.signature`` (the #1402 ``_REQUIRED_SCOPED`` mechanism) — a default re-opens
   silent-omission drift.
2. **AST guard** — every ``src/reyn`` call to a spawn seam passes BOTH routing kwargs
   explicitly, and never a bare literal ``None`` (which would bypass the ``ReviewedNA`` ratchet).
   A new spawn site that omits the decision is a PR-time CI failure, naming file:line — the same
   orphan-impossible-by-construction model as ``test_present_sink_ast_guard_2708``.
3. **NA-ratchet** — ``_REVIEWED_SELF_BOUND_SPAWN_SITES`` is the reviewed frozenset of sites where
   self-binding to the factory default (``None``/``None``) is genuinely correct; ``ReviewedNA``
   refuses any other site, so a new spawn site cannot silently join the self-bound set (the
   FP-0056 admin-6 equality model, spawn axis).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session_api import spawn_ephemeral_session
from reyn.runtime.spawn_routing import (
    _REVIEWED_SELF_BOUND_SPAWN_SITES,
    ReviewedNA,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"

# The three spawn seams whose calls must declare a routing decision. ``spawn_session_recorded`` /
# ``spawn_ephemeral_session`` are unambiguous names; ``spawn_session`` collides with
# ``RouterHostAdapter.spawn_session`` (a forwarding method reached as ``self.host.spawn_session``),
# excluded below.
_SEAM_NAMES = frozenset({
    "spawn_session", "spawn_session_recorded", "spawn_ephemeral_session",
})
_REQUIRED_ROUTING_KWARGS = ("presentation_consumer", "intervention_bridge")


def _is_host_adapter_spawn_session(func: ast.AST) -> bool:
    """True for ``<x>.host.spawn_session`` — the RouterHostAdapter forwarding method (NOT a
    registry spawn seam), which has no routing kwargs and forwards to ``spawn_session_recorded``
    (itself guarded). Excluded so the guard does not false-positive on it."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "spawn_session"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "host"
    )


def _spawn_seam_calls() -> list[tuple[str, ast.Call]]:
    """Every ``src/reyn`` Call to one of the three spawn seams (host-adapter forward excluded)."""
    out: list[tuple[str, ast.Call]] = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name not in _SEAM_NAMES:
                continue
            if _is_host_adapter_spawn_session(func):
                continue
            out.append((f"{rel}:{node.lineno}", node))
    return out


def test_spawn_seam_calls_declare_both_routing_kwargs() -> None:
    """Tier 2: every spawn-seam call in src/reyn passes BOTH presentation_consumer= and
    intervention_bridge= explicitly (completeness-by-construction). A new spawn site that omits
    the routing decision fails here, naming file:line — the orphan/hang-impossible guard."""
    calls = _spawn_seam_calls()
    assert calls, "no spawn-seam calls found in src/reyn — the guard's target moved?"
    offenders: list[str] = []
    for loc, call in calls:
        passed = {k.arg for k in call.keywords if k.arg is not None}
        # A ``**kwargs`` splat (k.arg is None) forwards everything — treat as compliant.
        has_splat = any(k.arg is None for k in call.keywords)
        missing = [k for k in _REQUIRED_ROUTING_KWARGS if k not in passed]
        if missing and not has_splat:
            offenders.append(f"{loc} (missing {missing})")
    assert not offenders, (
        "spawn-seam call(s) omit a required routing kwarg — a child's present/ask_user would "
        "silently self-bind (orphan outbox / origin-pin hang). Declare a runtime/spawn_routing "
        f"decision (BridgeToParent / SelfDeliveringWithDrain / AuditOnlyNoSurface / ReviewedNA): "
        f"{offenders}"
    )


def test_spawn_seam_calls_never_pass_bare_literal_none() -> None:
    """Tier 2: no spawn-seam call passes a bare literal ``None`` for a routing kwarg — self-binding
    must go through ``ReviewedNA(site)`` (whose ratchet validates the site), never a raw None that
    dodges the reviewed frozenset. Forwarded variables / routing-decision attributes are fine."""
    offenders: list[str] = []
    for loc, call in _spawn_seam_calls():
        for kw in call.keywords:
            if kw.arg in _REQUIRED_ROUTING_KWARGS and (
                isinstance(kw.value, ast.Constant) and kw.value.value is None
            ):
                offenders.append(f"{loc} ({kw.arg}=None)")
    assert not offenders, (
        "spawn-seam call(s) pass a bare literal None routing kwarg — this bypasses the ReviewedNA "
        "ratchet. Use ReviewedNA(<reviewed-site>).presentation_consumer / .intervention_bridge (or a "
        f"real routing decision) so a new self-bound site can't silently join the reviewed set: {offenders}"
    )


def test_all_three_spawn_seams_require_routing_kwargs_no_default() -> None:
    """Tier 2: the #1402 mechanism on the spawn axis — presentation_consumer + intervention_bridge
    are REQUIRED, keyword-only, no-default on all three spawn seams. A default re-opens
    silent-omission drift (a spawn site could omit the decision)."""
    seams = [
        AgentRegistry.spawn_session,
        AgentRegistry.spawn_session_recorded,
        spawn_ephemeral_session,
    ]
    for fn in seams:
        sig = inspect.signature(fn)
        for pname in _REQUIRED_ROUTING_KWARGS:
            assert pname in sig.parameters, f"{fn.__qualname__} missing {pname}"
            p = sig.parameters[pname]
            assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{fn.__qualname__}.{pname} must be keyword-only"
            )
            assert p.default is inspect.Parameter.empty, (
                f"{fn.__qualname__}.{pname} must be REQUIRED (no default) — a default re-opens "
                "silent-omission drift (#2708 P3-item3 completeness-by-construction)"
            )


def test_reviewed_self_bound_spawn_sites_is_the_reviewed_frozenset() -> None:
    """Tier 2: the reviewed self-bound spawn sites are EXACTLY the reviewed set (transport-native
    reuse, two crash-recovery re-wakes, /session new). A new self-bound spawn site must be added
    here deliberately — the equality ratchet blocks a silent self-bind. The LLM ``session_spawn``
    tool is deliberately NOT a member (co-vet must-fix): it is LLM-initiated + backgrounded, so a
    self-bound child would hit the origin-pin ask_user hang — it routes ``BridgeToParent`` instead."""
    assert _REVIEWED_SELF_BOUND_SPAWN_SITES == frozenset({
        "runtime/registry.py::resolve_session",
        "runtime/registry.py::restore_all",
        "runtime/registry.py::_rewake_pipeline_runs",
        "interfaces/slash/session.py::session_cmd",
    })


def test_reviewed_na_refuses_non_reviewed_site() -> None:
    """Tier 2: ReviewedNA is refused for a spawn site NOT in the reviewed frozenset (a new site
    trying to self-bind without review) — the spawn-axis NA-ratchet."""
    with pytest.raises(ValueError):
        ReviewedNA("runtime/session_api.py::run_agent_step")
    with pytest.raises(ValueError):
        ReviewedNA("some/new/site.py::brand_new_spawn")
    # The LLM session_spawn tool is NOT reviewed-self-bound (co-vet must-fix: it routes
    # BridgeToParent, not ReviewedNA) — constructing ReviewedNA for it must be refused.
    with pytest.raises(ValueError):
        ReviewedNA("runtime/services/router_host_adapter.py::spawn_session")


def test_reviewed_na_yields_self_bound_pair_for_reviewed_site() -> None:
    """Tier 2: each reviewed site constructs a ReviewedNA whose resolved routing is the self-bound
    (None/None) pair — the factory default, genuinely correct for that reviewed site."""
    for site in _REVIEWED_SELF_BOUND_SPAWN_SITES:
        routing = ReviewedNA(site)
        assert routing.site == site
        assert routing.presentation_consumer is None
        assert routing.intervention_bridge is None
