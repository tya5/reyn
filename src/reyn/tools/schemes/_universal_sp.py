"""Scheme-layer tool-use SP builder for the universal-category path (#1627 Stage 4).

``build_universal_tool_use_slots`` was relocated from
``reyn.runtime.router_system_prompt`` (OS layer) to here (scheme layer) as part of
Stage 4 — the final step of making ``build_system_prompt`` a pure slot-injector
with ZERO tool-use vocab. The OS builds the OS-frame; the scheme owns the
tool-use SP content.

P7-clean location: ``_universal_sp`` carries universal-category tool-use strings;
the OS ``router_system_prompt`` must NOT import from here (the dependency arrow
is scheme→ not OS←scheme).

Callers: universal_category.py / enumerate_all.py / retrieval.py — all scheme-
layer modules. No OS module imports this.
"""
from __future__ import annotations

from reyn.prompt.universal_slots import (
    build_action_categories_slot,
    build_behaviour_slot,
    build_capabilities_routing_guide,
    build_environment_how_clause,
    build_skills_slot,
)


def build_universal_tool_use_slots(
    *,
    universal_wrappers_enabled: bool,
    search_actions_enabled: bool,
    discovery_mandate: bool,
    has_hot_list_aliases: bool,
    non_interactive: bool = False,  # sp-autonomy-revision: accepted for backward compat only —
    # the ambiguity/proceed-vs-ask fork this used to gate moved to the OS-frame
    # ``build_system_prompt(non_interactive=...)`` Behaviour rule (scheme-
    # agnostic, reaches CodeAct too); no longer read in this function's body.
    non_claude: bool = False,  # #1791 A2: non-Claude operational-steering hygiene in slot_in_behaviour
    available_skills: "list | None" = None,  # #2548 PR-A: SkillEntry list for ## Skills block
) -> "dict[str, str]":
    """Build the four positional tool-use SP slots for the universal-category path.

    Called by each scheme's ``build_presentation`` to fill the slot-map they
    pass as ``tool_use_sp`` to ``build_system_prompt``. Returns a dict with ONLY
    the non-empty slots so ``build_system_prompt`` can inject each with a simple
    ``if slot_key in _slots`` guard.

    Slots:
      - ``slot_pre_environment``  — R1: ``## Capabilities (routing guide)`` block.
      - ``slot_post_environment`` — R2: ``## Action categories`` + hot-list +
                                    discovery-mandate paragraph (between Environment
                                    and ``## Behaviour``).
      - ``slot_in_behaviour``     — R3: never-invent / search guidance + ROUTING RULE
                                    (inside ``## Behaviour``, after the errors line).
      - ``slot_in_environment``   — the cwd-idiom file-discovery HOW clause injected
                                    inside ``## Environment``.
      - ``slot_post_catalog``     — scheme-owned SP appended at the post-catalog
                                    position (e.g. retrieval's search guidance),
                                    before the context-size signal (#1627 Stage 3).
      - ``slot_post_skills``      — the ``## Skills`` block (#2548 PR-A), rendered
                                    from ``available_skills`` at a DEDICATED position
                                    so retrieval's ``slot_post_catalog`` overwrite
                                    cannot clobber it.

    Each slot value equals ``"\\n".join(<elements>)`` where ``<elements>`` is the
    exact list that the corresponding inline region would have appended to ``parts``
    — char-identical by construction.
    """
    slots: dict[str, str] = {}

    # ── R1: ## Capabilities (routing guide) ──────────────────────────────────
    # Content moved to reyn.prompt.universal_slots.build_capabilities_routing_guide
    # (byte-identical relocation, SP Phase 1 §B) — the gating booleans are the
    # SAME ones this function receives; only the assembly moved.
    slots["slot_pre_environment"] = build_capabilities_routing_guide(
        universal_wrappers_enabled=universal_wrappers_enabled,
        search_actions_enabled=search_actions_enabled,
        discovery_mandate=discovery_mandate,
    )

    # ── R2: ## Action categories + hot-list + discovery-mandate ──────────────
    # Content moved to reyn.prompt.universal_slots.build_action_categories_slot.
    _r2_slot = build_action_categories_slot(
        universal_wrappers_enabled=universal_wrappers_enabled,
        has_hot_list_aliases=has_hot_list_aliases,
        discovery_mandate=discovery_mandate,
    )
    if _r2_slot is not None:
        slots["slot_post_environment"] = _r2_slot

    # ── R3: never-invent / search guidance + ROUTING RULE ────────────────────
    # Content moved to reyn.prompt.universal_slots.build_behaviour_slot.
    slots["slot_in_behaviour"] = build_behaviour_slot(
        universal_wrappers_enabled=universal_wrappers_enabled,
        search_actions_enabled=search_actions_enabled,
        non_claude=non_claude,
    )

    # ── R4: cwd-idiom file-discovery HOW clause (slot_in_environment) ────────
    # Content moved to reyn.prompt.universal_slots.build_environment_how_clause.
    slots["slot_in_environment"] = build_environment_how_clause(
        universal_wrappers_enabled=universal_wrappers_enabled,
    )

    # ── slot_post_skills: ## Skills block (#2548 PR-A) ──────────────────────
    # Content moved to reyn.prompt.universal_slots.build_skills_slot.
    _skills_slot = build_skills_slot(available_skills)
    if _skills_slot is not None:
        slots["slot_post_skills"] = _skills_slot

    return slots

