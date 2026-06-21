"""Tier 2: #1984 — _PlanStepHost family-gates recognize qualified tool names.

Post-#1657 the default (enumerate-all) scheme flat-lists actions by their
QUALIFIED ``<category>__<entry>`` names, so a plan step's ``tools`` carry e.g.
``skill__code_review`` / ``file__read``. The `_PlanStepHost` per-family plumbing
keyed only on the LEGACY names (``invoke_skill`` / ``read_file`` / …), so a
default-mode step was silenced of its family data — most importantly its
``available_skills`` resolved to ``[]``, which makes the universal catalog drop
the **skill** resource category → ``skill__*`` uncallable from the step.

Falsification:
- a qualified-name step now plumbs each of the 6 families (RED pre-fix);
- a legacy-name step still plumbs (byte-identical — purely additive);
- an unrelated step still silences (the narrowing is preserved);
- the catalog-level proof: a ``skill__x`` step's host feeds ``rs.available_skills``
  so the universal-catalog skill category enumerates ``skill__x`` (RED pre-fix —
  the skill was absent = uncallable).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.runtime.planner import _PlanStepHost
from reyn.tools import universal_catalog
from reyn.tools.types import ToolContext


class _FakeParent:
    """A real (non-mock) parent host exposing the family data the narrow host
    plumbs through when the step asks for that family."""

    def list_available_skills(self):
        return [{"name": "code_review", "description": "review code"}]

    def list_available_agents(self):
        return [{"name": "helper", "description": "a helper agent"}]

    def get_memory_index(self):
        return {"status": "ok", "content": "index"}

    def get_mcp_servers(self):
        return [{"name": "srv", "description": "a server"}]

    def get_file_permissions(self):
        return {"read": ["/repo"]}

    def get_web_fetch_allowed(self):
        return True


def _host(tools):
    return _PlanStepHost(
        plan=None, step=SimpleNamespace(tools=tools), prior_results={},
        parent=_FakeParent(),
    )


# (family, qualified tool, legacy tool, probe → truthy-when-plumbed)
_FAMILIES = [
    ("skill",  "skill__code_review",            "invoke_skill",      lambda h: h.list_available_skills()),
    ("agent",  "multi_agent__delegate",         "delegate_to_agent", lambda h: h.list_available_agents()),
    ("memory", "memory_operation__list_memory", "list_memory",       lambda h: h.get_memory_index().get("status") == "ok"),
    ("file",   "file__read",                    "read_file",         lambda h: h.get_file_permissions()),
    ("mcp",    "mcp__call_tool",                "call_mcp_tool",      lambda h: h.get_mcp_servers()),
    ("web",    "web__fetch",                    "web_fetch",          lambda h: h.get_web_fetch_allowed()),
]


@pytest.mark.parametrize("family,qualified,legacy,probe", _FAMILIES, ids=[f[0] for f in _FAMILIES])
def test_qualified_name_step_plumbs_family(family, qualified, legacy, probe):
    """Tier 2: a default-mode step naming the QUALIFIED tool plumbs its family
    (RED pre-#1984 — qualified names didn't match the legacy gate)."""
    assert probe(_host([qualified])), f"qualified {qualified!r} did not plumb {family}"


@pytest.mark.parametrize("family,qualified,legacy,probe", _FAMILIES, ids=[f[0] for f in _FAMILIES])
def test_legacy_name_step_still_plumbs(family, qualified, legacy, probe):
    """Tier 2: a legacy-name step still plumbs — the fix is purely additive
    (legacy plans byte-identical)."""
    assert probe(_host([legacy])), f"legacy {legacy!r} stopped plumbing {family}"


@pytest.mark.parametrize("family,qualified,legacy,probe", _FAMILIES, ids=[f[0] for f in _FAMILIES])
def test_unrelated_step_silences_family(family, qualified, legacy, probe):
    """Tier 2: an unrelated step (no member of the family) still silences it — the
    per-step narrowing (small LLM calls) is preserved."""
    assert not probe(_host(["compact_context"])), f"{family} leaked into an unrelated step"


def test_skill_step_makes_skill_callable_in_universal_catalog():
    """Tier 2: #1984 catalog-level proof — a ``skill__x`` step's host feeds
    ``rs.available_skills``, so the universal-catalog skill category enumerates
    ``skill__code_review`` (i.e. it is CALLABLE from the step). RED pre-fix: the
    host returned ``[]`` → the skill category enumerated empty → uncallable."""
    host = _host(["skill__code_review"])
    # The chain under test: the (fixed) host predicate feeds rs.available_skills
    # exactly as RouterLoop._build_router_caller_state does (router_loop.py:3930).
    rs = SimpleNamespace(
        available_skills=host.list_available_skills(),
        excluded_categories=frozenset(),
    )
    ctx = ToolContext(events=None, permission_resolver=None, workspace=None,
                      caller_kind="router", router_state=rs)

    entries = universal_catalog._enumerate_category("skill", ctx)
    qualified = [e["qualified_name"] for e in entries]
    assert "skill__code_review" in qualified, (
        "the skill is absent from the step catalog (uncallable) — the host "
        "plumbing did not feed rs.available_skills"
    )


def test_skill_absent_when_host_silences_is_the_pre_fix_state():
    """Tier 2: the same chain with an empty ``available_skills`` (the pre-#1984
    starved state) enumerates no skill — pinning that the catalog faithfully
    reflects the host plumbing (so the proof above is meaningful, not vacuous)."""
    rs = SimpleNamespace(available_skills=[], excluded_categories=frozenset())
    ctx = ToolContext(events=None, permission_resolver=None, workspace=None,
                      caller_kind="router", router_state=rs)
    entries = universal_catalog._enumerate_category("skill", ctx)
    assert "skill__code_review" not in [e["qualified_name"] for e in entries]
