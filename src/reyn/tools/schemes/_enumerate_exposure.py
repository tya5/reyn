"""The ``enumerate-all`` presentation's Exposure, shared by both of its cells.

``enumerate-all`` means: show the model every usable action, flat. **One**
exposure builder feeds both of its cells (``tool_calls`` and ``content_fence``),
and everything the two cells disagree about is carried in the
``ExposureDeviation`` they each declare rather than in two divergent code paths.

Both cells expose the **same set** — the base tools plus the catalog, minus
``mcp_call_tool`` (a zero-capability catalog wrapper whose native equivalent
``call_mcp_tool`` is already among the base tools, so leaving it in showed the
model the same action twice in two argument shapes). They differ in exactly one
declared value, ``applies_contextual_narrowing``, and that difference has a
transport reason: the ``content_fence`` cell's whole surface is a rendered
string, and the OS's post-presentation ``apply_contextual_visibility`` pass acts
on a ``tools=`` payload this transport does not have.

#3381 (settled here): the ``content_fence`` cell used to expose the catalog
**alone**, so no base tool was callable from the code-API. Nothing anywhere
recorded that as a decision — and the docs described the cell as "the same full
flat catalog as ``enumerate-all``", which it was not. Measured before changing
it: ``build_tools`` can emit 33 base tools; 18 of the 33 were already reachable
through the catalog under a SECOND, ``<category>__<verb>`` spelling of the same
``ToolDefinition``, and 15 — the spawn / topology / present / render_template /
compact tools and the MCP resource+prompt family — had no catalog route at all
and were simply unreachable from a code-API turn.

#3428 (settled after that): those 18 operations were then advertised **twice**,
in both cells and in both retrieval cells too. #3429 removed the second spelling
outright, so the two rows now carry the SAME name and
``without_duplicate_names`` (``reyn.tools.exposure``) keeps the first — at every
site where a base+catalog population is composed. #3419 shipped the duplication
on the grounds that no measurement said duplicate rows cost anything; a
declaration the model is shown on every turn costs its own bytes on every turn,
which is arithmetic rather than an open measurement.
"""
from __future__ import annotations

from typing import Any

from reyn.tools.exposure import (
    Exposure,
    ExposureDeviation,
    descriptors_from_entries,
    without_duplicate_names,
)
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate

_SHARED_RATIONALE_PREFIX = (
    "Base tools + catalog. ``mcp_call_tool`` is excluded because the base tools "
    "already carry its native equivalent ``call_mcp_tool``, and this scheme's "
    "contract is that it never presents wrappers. Both enumerate-all cells "
    "expose this same set (#3381); they differ only in whether the narrowing "
    "below is applied here. "
)

TOOL_CALLS_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=True,
    excluded_names=frozenset({"mcp_call_tool"}),
    applies_contextual_narrowing=False,
    rationale=(
        _SHARED_RATIONALE_PREFIX
        + "Contextual narrowing is NOT applied here because the OS applies "
        "``apply_contextual_visibility`` to the ``tools=`` payload after "
        "presentation (#3378); doing it here as well would be a second pass over "
        "the same decision."
    ),
)

CONTENT_FENCE_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=True,
    excluded_names=frozenset({"mcp_call_tool"}),
    applies_contextual_narrowing=True,
    rationale=(
        _SHARED_RATIONALE_PREFIX
        + "Contextual narrowing IS applied here because this cell's whole surface "
        "is a rendered code-API string and nothing downstream narrows a string — "
        "the OS's post-presentation filter runs over ``tools=``, which this "
        "transport does not have. Defence in depth either way: the real gate is "
        "the per-call one in ``execute``."
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
) -> "tuple[Exposure, list[dict]]":
    """Build one ``enumerate-all`` cell's ``(exposure, dispatchable_entries)``.

    ``catalog_entries`` is passed in already awaited: enumerating the live
    catalog twice would be both a second await and a second answer.

    ★ The **composed** entry list is returned alongside the exposure rather than
    recomposed by the caller. It is what the ``content_fence`` cell hands over as
    ``Presentation.dispatchable_catalog``, and the encoder derives the code-API's
    identifiers from ``Exposure.dispatchable_names`` while ``execute`` derives the
    sandbox stub names from that catalog: if the two were composed at two places,
    a cell could render a function the executor has no stub for and the OS gate
    would answer ``unknown_tool``. One composition, one answer."""
    entries = list(catalog_entries)
    if deviation.includes_base_tools:
        entries = list(ops.base_tools(available, layer_ctx)) + entries

    # The executor's universe: every name this cell can dispatch, BEFORE the
    # exclusion and the narrowing below. A fact, so it belongs here; the
    # identifier map derived from it is an encoding, so it does not.
    dispatchable_names = tuple(n for n in (_entry_name(e) for e in entries) if n)

    exposed = without_duplicate_names(
        [e for e in entries if _entry_name(e) not in deviation.excluded_names]
    )
    if deviation.applies_contextual_narrowing:
        contextual = (available or {}).get("contextual_permission")
        if contextual is not None:
            from reyn.security.permissions.effective import tool_contextually_denied

            exposed = [
                e for e in exposed if not tool_contextually_denied(contextual, _entry_name(e))
            ]

    exposure = Exposure(
        descriptors=descriptors_from_entries(exposed),
        sp_facts={
            # enumerate-all never has universal wrappers, so
            # ``search_actions_enabled`` is ``bool(search_visible)`` directly —
            # NOT the universal ``sv if univ else True``, whose fallback-to-True
            # branch only applies when wrappers are off for another reason.
            "universal_wrappers_enabled": False,
            "search_actions_enabled": bool(layer_ctx.get("search_visible", False)),
            "discovery_mandate": tier_wants_discovery_mandate(layer_ctx.get("router_model")),
            "non_interactive": bool(layer_ctx.get("non_interactive", False)),
            "available_skills": layer_ctx.get("available_skills"),
        },
        dispatchable_names=dispatchable_names,
        deviation=deviation,
    )
    return exposure, entries


__all__ = [
    "CONTENT_FENCE_EXPOSURE_DEVIATION",
    "TOOL_CALLS_EXPOSURE_DEVIATION",
    "build_enumerate_all_exposure",
]
