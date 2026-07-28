"""Tier 1: a tool's rendered schema never depends on what ran earlier in the process.

#3383. ``ToolDefinition.render_for_router`` used to return ``dict(self.parameters)``
— a SHALLOW copy, leaving every nested sub-schema (``properties``, each ``oneOf``
variant) aliased to the module-level constant the definition was built from.
litellm's provider transforms rewrite the ``tools[]`` payload they are handed IN
PLACE (``_build_vertex_schema`` is documented as returning "the input parameters,
modified in place"; ``add_object_type`` injects ``type: object`` into any node
without one, ``_remove_additional_properties`` deletes
``additionalProperties: false``). So ONE Gemini/Vertex call permanently rewrote
reyn's canonical schema, and every later render — for ANY provider — served the
corrupted shape: ``plugin_management__install``'s ``source`` variants lost
``additionalProperties`` and grew ``"type": "object"`` inside their ``kind``
const, and ``presentation_install_local``'s untyped ``blueprint`` became
``type: object``. Observed as a schedule-dependent baseline-fixture failure,
but the defect is in the product: the schema the LLM receives was a function of
process history.

These tests assert the invariant directly, at each of the three seams where a
canonical schema leaves its ToolDefinition: the router payload
(``render_for_router``), the ``build_tools`` payload (``ToolSpec``), and the
``describe_action`` tool result. Each drives the defect by CONSTRUCTING the
precondition — mutating the handed-out payload in place, exactly as a provider
transform does — rather than depending on a provider being reachable or on a
particular test order.

Enumerated from the live registry, not a hand-picked subset: any future tool
whose schema has nested structure is covered on the day it is registered.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from reyn.runtime.router_tools import build_tools
from reyn.tools import get_default_registry
from reyn.tools.types import ToolContext
from reyn.tools.universal_catalog import _handle_describe_action

_SENTINEL_KEY = "__injected_by_a_provider_transform__"


def _mutate_in_place(node: Any) -> int:
    """Rewrite every dict in ``node`` the way a provider transform does.

    Injects a key into each dict and deletes one existing key — the two shapes
    litellm's Gemini/Vertex path applies (``add_object_type`` /
    ``_remove_additional_properties``). Returns the number of dicts touched so a
    caller can reject a vacuous run.
    """
    touched = 0
    if isinstance(node, dict):
        touched += 1
        for value in list(node.values()):
            touched += _mutate_in_place(value)
        existing = [k for k in node if k != _SENTINEL_KEY]
        node[_SENTINEL_KEY] = "object"
        if existing:
            del node[existing[0]]
    elif isinstance(node, list):
        for item in node:
            touched += _mutate_in_place(item)
    return touched


def test_render_for_router_survives_in_place_mutation_of_a_handed_out_payload() -> None:
    """Tier 1: mutating a render_for_router() payload cannot alter the next render."""
    registry = get_default_registry()
    pristine = {t.name: deepcopy(t.render_for_router()) for t in registry}

    total_touched = 0
    for tool in registry:
        payload = tool.render_for_router()
        total_touched += _mutate_in_place(payload)
        assert payload != pristine[tool.name], (
            f"{tool.name}: the mutation helper did not change the payload — "
            "this run would prove nothing"
        )

    assert total_touched > len(pristine), (
        "expected the mutation to reach nested sub-schemas, not just the top "
        f"level (touched={total_touched}, tools={len(pristine)})"
    )

    for tool in get_default_registry():
        assert tool.render_for_router() == pristine[tool.name], (
            f"{tool.name}: render_for_router() changed after an EARLIER payload "
            "was mutated in place — the canonical schema escaped by reference, "
            "so the schema the LLM receives depends on process history (#3383)"
        )


def test_build_tools_survives_in_place_mutation_of_a_handed_out_payload() -> None:
    """Tier 1: mutating a build_tools() payload cannot alter the next build_tools()."""
    agents = [{"name": "researcher", "role": "Research agent"}]
    pristine = deepcopy(build_tools(agents))

    payload = build_tools(agents)
    touched = _mutate_in_place(payload)
    assert touched > len(pristine), (
        f"mutation did not reach nested sub-schemas (touched={touched})"
    )
    assert payload != pristine, "the mutation helper did not change the payload"

    assert build_tools(agents) == pristine, (
        "build_tools() changed after an EARLIER payload was mutated in place — "
        "a ToolSpec / ToolDefinition schema escaped by reference (#3383)"
    )


def test_describe_action_survives_in_place_mutation_of_its_input_schema() -> None:
    """Tier 1: mutating describe_action's input_schema cannot alter the definition."""
    # events=None: describe_action is a pure registry read and emits nothing, so
    # no event sink is needed (and none is faked).
    ctx = ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )
    action = "plugin_management__install"
    pristine = deepcopy(asyncio.run(_handle_describe_action({"action_name": action}, ctx)))
    assert pristine["input_schema"], f"{action}: describe_action returned no input_schema"

    handed_out = asyncio.run(_handle_describe_action({"action_name": action}, ctx))
    touched = _mutate_in_place(handed_out["input_schema"])
    assert touched > 1, f"mutation did not reach nested sub-schemas (touched={touched})"

    again = asyncio.run(_handle_describe_action({"action_name": action}, ctx))
    assert again["input_schema"] == pristine["input_schema"], (
        "describe_action's input_schema changed after an EARLIER result was "
        "mutated in place — the canonical schema escaped by reference (#3383)"
    )
    assert get_default_registry().lookup(action) is not None
    assert _SENTINEL_KEY not in str(get_default_registry().lookup(action).parameters), (
        "the ToolDefinition's own parameters were rewritten by a mutation of a "
        "describe_action result (#3383)"
    )
