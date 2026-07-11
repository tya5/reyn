"""reyn AG-UI extension profile — the ``reyn.*`` namespace registry.

The AG-UI standard surface renders the interoperable core (text / tool / run /
error / state) for any generic client. Beyond that core, reyn names its own
vocabulary under a **reyn-owned namespace** in two ways:

- as the ``name`` of a ``CUSTOM`` event (chrome with no standard analog — trace
  lines, the ``present`` render model, the intervention-answer axis); and
- as the ``toolName`` of the HITL intervention **frontend-tool**
  ``TOOL_CALL_START`` (a standard event, not a ``CUSTOM`` one).

This module is the single registry that formalizes that namespace into a
documented, tested **extension profile**: every ``reyn.*`` name reyn emits, its
value schema, and what it means.

Three namespaces:

- ``reyn.display.<kind>`` — a reyn display frame with no standard AG-UI analog.
  A ``CUSTOM`` ``name``; ``value`` is ``{"text": <the display line text>}``.
  (``presentation`` also carries its render-node model on the ``_reyn`` block's
  ``meta.nodes``, inert on the wire.) Closed member set (below).
- ``reyn.event.<etype>`` — a reyn chat-event (working-indicator axis) with no
  standard AG-UI analog. A ``CUSTOM`` ``name``; ``value`` is the event's data
  object. Closed member set (below).
- ``reyn.intervention.<kind>`` — the HITL frontend-tool ``toolName``. ``<kind>``
  is the intervention kind (``ask_user`` / ``permission.*`` / …), caller-supplied,
  so this is an **open namespace** profiled at the prefix level with a fixed value
  schema (:data:`OPEN_NAMESPACES`), not a closed member set.

**Non-circular completeness gate.** ``tests/test_agui_profile_completeness.py``
enumerates the reyn-mapped vocabulary *from the source vocabulary* — display kinds
+ ``renderer_chat_events`` (encoded through the codec, ``CUSTOM`` names collected)
AND the intervention frontend-tool encoder's real ``toolName`` — and asserts each
emitted ``reyn.*`` name is profiled (a closed-member entry or an open-namespace
prefix). An unprofiled name is RED — doc-drift is designed out, the same discipline
as the P1/P2 completeness gates. The gate reads the codec's output, never this
registry, so it is not comparing the profile to itself.
"""
from __future__ import annotations

from dataclasses import dataclass

# Namespace prefixes (the ``name`` before the terminal ``.<kind>`` / ``.<etype>``).
DISPLAY_NS = "reyn.display"
EVENT_NS = "reyn.event"
INTERVENTION_NS = "reyn.intervention"

# Human-readable namespace summaries — enumerated in the profile doc section.
NAMESPACES: dict[str, str] = {
    DISPLAY_NS: "a reyn display frame with no standard AG-UI analog (CUSTOM name; value: {text})",
    EVENT_NS: "a reyn chat-event with no standard AG-UI analog (CUSTOM name; value: the event data object)",
    INTERVENTION_NS: "the HITL intervention frontend-tool toolName (open namespace; args: {prompt, detail, choices, suggestions})",
}

# Open namespaces profiled at the PREFIX level (the terminal segment is
# caller-supplied, so there is no closed member set). Maps the ``<prefix>.`` a
# name must start with → its fixed value schema. ``reyn.intervention.`` is the
# HITL frontend-tool ``toolName`` (``reyn.intervention.<kind>``); the trailing dot
# is part of the key so a bare ``reyn.intervention`` does not spuriously match.
OPEN_NAMESPACES: dict[str, str] = {
    f"{INTERVENTION_NS}.": "args: {prompt, detail, choices, suggestions}; toolCallId = intervention id",
}


@dataclass(frozen=True)
class CustomName:
    """One profiled ``reyn.*`` Custom name: its namespace + ``value`` schema."""

    name: str
    namespace: str
    value_schema: str
    summary: str


def _entries(*entries: CustomName) -> "dict[str, CustomName]":
    return {e.name: e for e in entries}


# The concrete, emitted Custom names. Keyed by the exact ``name`` the codec puts
# on the wire; the completeness gate binds this set to the codec's emitted
# vocabulary (a new Custom-mapped kind/etype with no entry here fails CI).
CUSTOM_PROFILE: dict[str, CustomName] = _entries(
    # ── reyn.display.<kind> — display frames with no standard AG-UI analog ──
    CustomName("reyn.display.trace", DISPLAY_NS, "{text: str}", "a reyn tool/step trace line"),
    CustomName(
        "reyn.display.intervention", DISPLAY_NS, "{text: str}",
        "an intervention prompt is displayed (the reyn client draws it natively; the answer round-trip rides the reyn.intervention.* frontend-tool)",
    ),
    CustomName(
        "reyn.display.presentation", DISPLAY_NS, "{text: str}",
        "a present op's text; the render-node model rides the _reyn block's meta.nodes, inert on the wire",
    ),
    CustomName("reyn.display.nodes", DISPLAY_NS, "{text: str}", "a raw render-node display line"),
    CustomName(
        "reyn.display.user", DISPLAY_NS, "{text: str}",
        "a user-authored line echoed live to the scrollback (backlog user turns ride the standard messages array)",
    ),
    CustomName(
        "reyn.display.system", DISPLAY_NS, "{text: str}",
        "a reyn chrome line — a persisted lifecycle/status marker (compaction / budget / cost-warn) or the operator's 'answered:' echo; reyn-private, no standard AG-UI analog",
    ),
    CustomName(
        "reyn.display.tool_call_started", DISPLAY_NS, "{text: str}",
        "a tool-call start trace line",
    ),
    CustomName(
        "reyn.display.tool_call_completed", DISPLAY_NS, "{text: str}",
        "a tool-call completion trace line",
    ),
    CustomName(
        "reyn.display.tool_call_failed", DISPLAY_NS, "{text: str}",
        "a tool-call failure trace line",
    ),
    # ── reyn.event.<etype> — chat-events with no standard AG-UI analog ──
    CustomName(
        "reyn.event.user_answered_intervention", EVENT_NS, "the event data object",
        "the user answered an intervention (working-indicator axis)",
    ),
)


def profiled_names() -> "frozenset[str]":
    """The set of ``reyn.*`` Custom names the extension profile documents."""
    return frozenset(CUSTOM_PROFILE)


def is_profiled(name: str) -> bool:
    """True iff *name* is profiled — a closed :data:`CUSTOM_PROFILE` member OR a
    name under a profiled :data:`OPEN_NAMESPACES` prefix (the intervention
    frontend-tool). An unprofiled name is skipped by a reyn client, so ``False``
    is the generic ignore-unknown case, not fatal."""
    if name in CUSTOM_PROFILE:
        return True
    return any(name.startswith(prefix) for prefix in OPEN_NAMESPACES)


__all__ = [
    "DISPLAY_NS",
    "EVENT_NS",
    "INTERVENTION_NS",
    "NAMESPACES",
    "OPEN_NAMESPACES",
    "CustomName",
    "CUSTOM_PROFILE",
    "profiled_names",
    "is_profiled",
]
