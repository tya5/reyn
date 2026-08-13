"""The ``category`` presentation's Exposure, shared by both of its cells.

``category`` (the shipped ``universal-category`` scheme) exists to keep the
LLM-visible surface **small and constant**: the router's ``build_tools`` composes
the base tools with a handful of catalog *wrappers* (``list_actions`` /
``describe_action`` / ``invoke_action``), and every one of the catalog's actions
is reached *through* those wrappers rather than by being shown. That fold is the
whole point of the scheme — the exposed set does not grow when the catalog does.

★ **The fold happens here, in the Exposure, so no encoder can undo it.** Both of
this scheme's cells build their exposure from ``ops.present`` — the wrapper
composition — and never from ``ops.catalog_entries`` (the M-action enumeration
the ``enumerate-all`` presentation exposes). An encoder renders what it is given;
if a cell reached past this builder for the flat catalog, the ``content_fence``
cell's code-API would list every action and the scheme would have no reason to
exist. ``tests/tools/test_tool_use_category_content_fence_3376.py`` asserts the
function count is invariant under catalog growth, which is the falsifiable form
of that sentence.

The two cells differ in exactly one declared value — see the deviations below.
"""
from __future__ import annotations

from typing import Any

from reyn.tools.exposure import Exposure, ExposureDeviation, descriptors_from_entries
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate

_SHARED_RATIONALE_PREFIX = (
    "Base tools + the catalog WRAPPERS, as ``build_tools`` composes them — never "
    "the flat catalog. Both category cells expose the same set; they differ only "
    "in whether the narrowing below is applied here. "
)

TOOL_CALLS_EXPOSURE_DEVIATION = ExposureDeviation(
    includes_base_tools=True,
    excluded_names=frozenset(),
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
    excluded_names=frozenset(),
    applies_contextual_narrowing=True,
    rationale=(
        _SHARED_RATIONALE_PREFIX
        + "Contextual narrowing IS applied here because this cell's whole surface "
        "is a rendered code-API string and nothing downstream narrows a string — "
        "the OS's post-presentation filter runs over ``tools=``, which this "
        "transport does not have. Same reasoning as the enumerate-all "
        "content_fence cell. Defence in depth either way: the real gate is the "
        "per-call one in ``execute``."
    ),
)


def _entry_name(entry: dict) -> str:
    body = entry.get("function")
    return str((body if isinstance(body, dict) else entry).get("name", ""))


def build_category_exposure(
    *,
    present_entries: "list[dict]",
    available: Any,
    layer_ctx: Any,
    deviation: ExposureDeviation,
) -> Exposure:
    """Build the transport-neutral exposure for one ``category`` cell.

    ``present_entries`` is ``advertised_entries(ops.present(...).tools_channel)``
    — the router's
    universal-category presentation, the already-folded wrapper composition. It
    is passed in already computed because the ``content_fence`` cell needs the
    same untouched list for its dispatchable catalog, and calling ``build_tools``
    twice would be both duplicated work and a second answer. It is the ONLY
    source of the exposed set here; see the module docstring for why reaching for
    the flat catalog instead would dissolve the scheme."""
    if not deviation.includes_base_tools:
        # A declaration that cannot be honoured must not pass silently. ``ops.present``
        # composes the base tools and the wrappers in ONE ``build_tools`` call, so this
        # cell has no seam at which base tools could be dropped — unlike enumerate-all,
        # which composes them itself and can therefore declare either value. Refusing
        # keeps the field a checked claim rather than a decorative one.
        raise ValueError(
            "the category presentation cannot exclude base tools: ops.present "
            "composes them with the wrappers in a single build_tools call, so "
            "includes_base_tools=False has no seam to act at."
        )

    entries = list(present_entries)

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

    # The 5 slot-builder inputs, derived from the raw FACTS in layer_ctx (the OS
    # supplies facts; the scheme computes policy). #1627 Stage 1 — the EXACT
    # formulas the OS computed for the None-path (router_loop.py):
    #
    #   universal_wrappers_enabled = layer_ctx["univ_enabled"]
    #   search_actions_enabled     = sv if univ else True   ← CRITICAL subtlety
    #   discovery_mandate          = tier_wants_discovery_mandate(router_model)
    #   non_interactive            = layer_ctx["non_interactive"]
    univ: bool = bool(layer_ctx.get("univ_enabled", False))
    sv: bool = bool(layer_ctx.get("search_visible", True))
    return Exposure(
        descriptors=descriptors_from_entries(exposed),
        sp_facts={
            "universal_wrappers_enabled": univ,
            "search_actions_enabled": sv if univ else True,  # the formula, unchanged
            "discovery_mandate": tier_wants_discovery_mandate(layer_ctx.get("router_model")),
            "non_interactive": bool(layer_ctx.get("non_interactive", False)),
            # #1791 A2: non-Claude operational-steering policy from the raw family
            # fact (Claude excluded — it doesn't need the hygiene reminders).
            "non_claude": layer_ctx.get("router_model_family") != "claude",
            # #2548 PR-A: skill registry snapshot from the OS layer_ctx →
            # rendered into the ## Skills block (slot_post_skills).
            "available_skills": layer_ctx.get("available_skills"),
        },
        dispatchable_names=dispatchable_names,
        deviation=deviation,
    )


__all__ = [
    "CONTENT_FENCE_EXPOSURE_DEVIATION",
    "TOOL_CALLS_EXPOSURE_DEVIATION",
    "build_category_exposure",
]
