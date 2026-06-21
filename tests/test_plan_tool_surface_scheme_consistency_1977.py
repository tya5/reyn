"""Tier 2: plan/SP tool-surface stays consistent with the active scheme (#1977).

The universal-wrapper vocab (`invoke_action` / `list_actions` / `describe_action`
/ `search_actions`) is universal-scheme-only. Under a wrappers-off scheme
(enumerate-all, the default) the system-prompt slot builder AND the `plan` tool
description used to instruct that vocab anyway → the model wrote `invoke_action`
into plan steps the enumerate catalog rejects → `plan_invalid` every time. The SP
slot builder now gates the vocab on `universal_wrappers_enabled`; the plan tool
description is scheme-neutral ("name tools from your available list"). Tier line
first.
"""
from __future__ import annotations

import ast

from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

_WRAPPER_VOCAB = ("invoke_action", "list_actions", "describe_action", "search_actions")


def _slots_text(**kw: object) -> str:
    return " ".join(build_universal_tool_use_slots(**kw).values())  # type: ignore[arg-type]


def test_wrappers_off_sp_has_zero_wrapper_vocab():
    """Tier 2: with wrappers OFF (enumerate-all) the SP contains NONE of the
    universal wrapper vocab — the model is told to call actions directly. (RED
    pre-fix: the slots leaked the vocab regardless of scheme.)"""
    for discovery in (True, False):
        for search in (True, False):
            for hot in (True, False):
                txt = _slots_text(
                    universal_wrappers_enabled=False,
                    search_actions_enabled=search,
                    discovery_mandate=discovery,
                    has_hot_list_aliases=hot,
                    non_interactive=False,
                )
                leaked = [v for v in _WRAPPER_VOCAB if v in txt]
                assert leaked == [], (discovery, search, hot, leaked)


def test_wrappers_on_sp_retains_wrapper_vocab():
    """Tier 2: with wrappers ON (universal-category) the SP still instructs the
    wrapper vocab — the gate is conditional, not a blanket removal (the ON path
    is unchanged)."""
    txt = _slots_text(
        universal_wrappers_enabled=True,
        search_actions_enabled=True,
        discovery_mandate=True,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    assert "invoke_action" in txt
    assert "list_actions" in txt


def test_plan_description_scheme_neutral():
    """Tier 2: the `plan` tool description does NOT hard-code `invoke_action` — it
    points at the available tools list (scheme-neutral). RED pre-fix (the desc
    hard-coded invoke_action + a worked example)."""
    from reyn.tools.plan import PLAN

    desc = PLAN.parameters["properties"]["steps_json"]["description"]
    assert "invoke_action" not in desc
    assert "available tools list" in desc


def _steps_json_desc(path: str) -> str | None:
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "steps_json" and isinstance(v, ast.Dict):
                    for kk, vv in zip(v.keys, v.values):
                        if isinstance(kk, ast.Constant) and kk.value == "description":
                            return ast.literal_eval(vv)
    return None


def test_plan_description_copies_match():
    """Tier 2: the plan `steps_json` description in tools/plan.py and the fallback
    mirror in runtime/router_tools.py are identical — the mirror invariant the
    stale source comment claimed (it had drifted; both are now re-pointed). A
    permanent invariant (not a refactor scaffold)."""
    import reyn.runtime.router_tools as _rt
    import reyn.tools.plan as _p

    a = _steps_json_desc(_p.__file__)
    b = _steps_json_desc(_rt.__file__)
    assert a is not None and b is not None
    assert a == b
    assert "invoke_action" not in a
