"""The ``enumerate-all`` presentation's Exposure, shared by both of its cells.

``enumerate-all`` is the one scheme with two registered cells today
(``tool_calls`` and ``content_fence``), which makes it the place where the
Exposure/Encoder seam either holds or does not: **one** exposure builder feeds
both cells, and everything the two cells disagree about is carried in the
``ExposureDeviation`` they each declare rather than in two divergent code paths.

The two declarations below keep exactly today's values. They are not a proposal:

- over ``tool_calls`` the cell exposes the base tools plus the catalog, minus
  ``mcp__call_tool`` — a zero-capability catalog wrapper whose native equivalent
  (``call_mcp_tool``) is already in the base tools, so leaving it in showed the
  model the same action twice in two argument shapes;
- over ``content_fence`` the cell exposes the catalog **alone**, so base tools
  such as ``delegate_to_agent`` are not callable from the code-API, and it
  applies the session's effective contextual narrowing to what it renders.

Whether that base-tools asymmetry is intended is not recorded anywhere and is
tracked as #3381. Changing it here would add callables to CodeAct's system
prompt — a behaviour change — so the values stay put and the difference is
simply stated. Settling #3381 is then a change of a value in this file.
"""
from __future__ import annotations

from typing import Any

from reyn.tools.exposure import Exposure, ExposureDeviation, descriptors_from_entries
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate

TOOL_CALLS_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=True,
    excluded_names=frozenset({"mcp__call_tool"}),
    applies_contextual_narrowing=False,
    rationale=(
        "Base tools + catalog. ``mcp__call_tool`` is excluded because the base "
        "tools already carry its native equivalent ``call_mcp_tool``, and this "
        "scheme's contract is that it never presents wrappers. Contextual "
        "narrowing is applied by the OS to the ``tools=`` payload after "
        "presentation, so the exposure does not apply it a second time."
    ),
)

CONTENT_FENCE_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=False,
    excluded_names=frozenset(),
    applies_contextual_narrowing=True,
    rationale=(
        "Catalog only — base tools are NOT rendered into the code-API, so the "
        "two enumerate-all cells do not share an exposure set (#3381: no record "
        "states this was decided, and it is not fixed here because adding those "
        "callables would change what the model is shown). Contextual narrowing "
        "is applied here because nothing downstream narrows a code-API string; "
        "it is defence in depth, the real gate being the per-call one in "
        "``execute``."
    ),
)


def _entry_name(entry: dict) -> str:
    body = entry.get("function")
    return str((body if isinstance(body, dict) else entry).get("name", ""))


def build_enumerate_all_exposure(
    *,
    catalog_entries: "list[dict]",
    available: Any,
    layer_ctx: Any,
    ops: Any,
    deviation: ExposureDeviation,
) -> Exposure:
    """Build the transport-neutral exposure for one ``enumerate-all`` cell.

    ``catalog_entries`` is passed in already awaited: the ``content_fence`` cell
    needs the same untouched list for its dispatchable catalog, and enumerating
    the live catalog twice would be both a second await and a second answer."""
    entries = list(catalog_entries)
    if deviation.includes_base_tools:
        entries = list(ops.base_tools(available, layer_ctx)) + entries

    # The executor's universe: every name this cell can dispatch, BEFORE the
    # exclusion and the narrowing below. A fact, so it belongs here; the
    # identifier map derived from it is an encoding, so it does not.
    dispatchable_names = tuple(n for n in (_entry_name(e) for e in entries) if n)

    exposed = [e for e in entries if _entry_name(e) not in deviation.excluded_names]
    if deviation.applies_contextual_narrowing:
        contextual = (available or {}).get("contextual_permission")
        if contextual is not None:
            from reyn.security.permissions.effective import tool_contextually_denied

            exposed = [
                e for e in exposed if not tool_contextually_denied(contextual, _entry_name(e))
            ]

    return Exposure(
        descriptors=descriptors_from_entries(exposed),
        sp_facts={
            # enumerate-all never has universal wrappers, so
            # ``search_actions_enabled`` is ``bool(search_visible)`` directly —
            # NOT the universal ``sv if univ else True``, whose fallback-to-True
            # branch only applies when wrappers are off for another reason.
            "universal_wrappers_enabled": False,
            "search_actions_enabled": bool(layer_ctx.get("search_visible", False)),
            "discovery_mandate": tier_wants_discovery_mandate(layer_ctx.get("router_model")),
            "has_hot_list_aliases": bool((available or {}).get("hot_list_aliases")),
            "non_interactive": bool(layer_ctx.get("non_interactive", False)),
            "available_skills": layer_ctx.get("available_skills"),
        },
        dispatchable_names=dispatchable_names,
        deviation=deviation,
    )


__all__ = [
    "CONTENT_FENCE_EXPOSURE_DEVIATION",
    "TOOL_CALLS_EXPOSURE_DEVIATION",
    "build_enumerate_all_exposure",
]
