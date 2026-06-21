"""Tier 2: #1989 — strip a provider function-calling namespace prefix from tool names.

A weak model (Gemini) sometimes echoes its ``default_api`` function-calling
namespace onto a tool name — both as a ``plan`` step-tools VALUE
(``default_api.invoke_action`` / ``default_api.web__search``, the reported
``plan_invalid``) and, latently, as an actual function-call NAME. ``reyn`` tool
names are dot-free (qualified use ``__``, bare verbs single underscores), so
stripping a leading ``<namespace>.`` is safe for every provider (a no-op when
absent). The shared helper is applied at BOTH surfaces (validator + dispatch).

Falsification:
- the helper strips a known prefix and is a no-op for a dot-free / bare name;
- the plan validator accepts a ``default_api.``-prefixed step tool + stores the
  bare name (RED pre-#1989 = ``plan_invalid``); a genuinely-unknown tool still
  rejects (no over-acceptance);
- the dispatch resolver strips the prefix so the call hits the catalog / salvage.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.runtime.planner import PlanValidationError, parse_and_validate_plan
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.universal_catalog import strip_provider_tool_namespace

# ── 1. the helper ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("default_api.invoke_action", "invoke_action"),      # bare verb under namespace
    ("default_api.web__search", "web__search"),          # qualified under namespace
    ("invoke_action", "invoke_action"),                  # bare, no prefix → no-op
    ("web__search", "web__search"),                      # qualified, no prefix → no-op
    ("file__read", "file__read"),                        # dot-free legit name → untouched
    ("", ""),                                            # empty → no-op
])
def test_strip_provider_tool_namespace(raw, expected):
    """Tier 2: strips a known provider namespace prefix; a no-op otherwise (so it
    is safe to apply unconditionally — reyn names are dot-free)."""
    assert strip_provider_tool_namespace(raw) == expected


def test_strip_does_not_touch_a_non_leading_or_unknown_namespace():
    """Tier 2: only a LEADING known prefix is stripped — a name that merely
    contains the token, or an unknown provider namespace, is left intact (no
    over-strip)."""
    assert strip_provider_tool_namespace("skill__default_api.thing") == "skill__default_api.thing"
    assert strip_provider_tool_namespace("functions.invoke_action") == "functions.invoke_action"


# ── 2. the plan validator (the reported surface) ────────────────────────────


def _plan_args(tools_step1, tools_step2):
    return {
        "goal": "do the thing",
        "steps": [
            {"id": "s1", "description": "first", "tools": tools_step1, "depends_on": []},
            {"id": "s2", "description": "second", "tools": tools_step2, "depends_on": ["s1"]},
        ],
    }


def test_validator_accepts_namespaced_tool_and_stores_bare_name():
    """Tier 2: a ``default_api.``-prefixed step tool validates (RED pre-#1989 =
    plan_invalid) AND the stored step carries the BARE name downstream."""
    plan = parse_and_validate_plan(
        _plan_args(["default_api.invoke_action"], ["default_api.web__search"]),
        allowed_tool_names={"invoke_action", "web__search"},
    )
    assert plan.steps[0].tools == ("invoke_action",)
    assert plan.steps[1].tools == ("web__search",)


def test_validator_still_accepts_bare_names_unaffected():
    """Tier 2: a bare-name plan is unaffected — the strip is purely additive (a
    no-op for names without a provider prefix)."""
    plan = parse_and_validate_plan(
        _plan_args(["invoke_action"], ["web__search"]),
        allowed_tool_names={"invoke_action", "web__search"},
    )
    assert plan.steps[0].tools == ("invoke_action",)
    assert plan.steps[1].tools == ("web__search",)


def test_validator_still_rejects_genuinely_unknown_tool():
    """Tier 2: a real unknown tool (not a namespace artifact) still rejects — the
    strip does not over-accept."""
    with pytest.raises(PlanValidationError):
        parse_and_validate_plan(
            _plan_args(["invoke_action"], ["totally_made_up_tool"]),
            allowed_tool_names={"invoke_action", "web__search"},
        )


# ── 3. the dispatch resolver (the fix-class extension) ──────────────────────


class _RecordingEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, type: str, **data: Any) -> None:
        self.events.append((type, dict(data)))


class _ResolveShim:
    """Minimal RouterLoop surface to exercise the real ``_resolve_tool_call`` +
    ``_maybe_salvage_qualified_direct_call`` (bound from RouterLoop — no dup)."""

    chain_id = "test-chain"
    _resolve_tool_call = RouterLoop._resolve_tool_call
    _maybe_salvage_qualified_direct_call = RouterLoop._maybe_salvage_qualified_direct_call

    def __init__(self, catalog) -> None:
        self._catalog = catalog

        class _Host:
            events = _RecordingEvents()
        self.host = _Host()


def _tc(name, arguments="{}"):
    return {"function": {"name": name, "arguments": arguments}}


def test_resolve_strips_namespace_so_bare_name_hits_catalog():
    """Tier 2: ``default_api.invoke_action`` as a call NAME → stripped → hits the
    catalog directly (RED pre-#1989 = unknown_tool)."""
    shim = _ResolveShim(catalog={"invoke_action": object()})
    name, args = shim._resolve_tool_call(_tc("default_api.invoke_action", '{"x": 1}'))
    assert name == "invoke_action"
    assert args == {"x": 1}


def test_resolve_strips_namespace_then_salvages_qualified_call_name():
    """Tier 2: ``default_api.web__search`` as a call NAME → stripped → ``web__search``
    (not in catalog, has ``__``) → salvaged to invoke_action(action_name=...)."""
    shim = _ResolveShim(catalog={"invoke_action": object()})
    name, args = shim._resolve_tool_call(_tc("default_api.web__search", '{"query": "q"}'))
    assert name == "invoke_action"
    assert args == {"action_name": "web__search", "args": {"query": "q"}}


def test_resolve_bare_name_is_unchanged():
    """Tier 2: a normal bare call name is unaffected (no-op strip)."""
    shim = _ResolveShim(catalog={"invoke_action": object()})
    name, _ = shim._resolve_tool_call(_tc("invoke_action"))
    assert name == "invoke_action"
