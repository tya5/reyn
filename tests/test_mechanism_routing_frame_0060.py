"""Tier 2: proposal 0060 Addendum C (Layer C) — the part x role mechanism
routing frame in ``reyn.prompt.router_frame`` / injected by
``reyn.runtime.router_system_prompt.build_system_prompt``.

Three co-vet pins (Addendum C):

1. **Registry-derivation (the crux, mirrors #2899's auto-appear witness)**:
   dropping a new marked part-type into ``reyn.core.part_types`` (touching
   NOTHING in ``render_part_role_map``/``render_mechanism_routing_frame``)
   makes it auto-appear in the rendered frame. Falsify: a hand-written table
   frozen at collection time does NOT pick up the new part-type — the exact
   drift the derivation prevents becomes observable.
2. **Scheme-independence**: the routing frame appears identically under all
   four tool-use schemes' slot-map shapes (universal / enumerate / retrieval
   / codeact), and even with NO scheme attached at all (``tool_use_sp=None``)
   — proving it is not sourced from any scheme-owned slot. Falsify: were the
   frame instead assembled inside a scheme-owned slot value, the bare/no-
   scheme call would be missing it.
3. **Char-budget / cache-static**: each derived part-type row is bounded by
   ``MAX_PART_TYPE_ROW_CHARS``; a synthetic larger registry (more, longer-
   named part-types) stays within a total budget that scales linearly (not
   superlinearly) with the number of part-types, and an over-long row raises
   instead of silently shipping. The frame also lands in the static section
   of the assembled SP, ahead of "## Behaviour" and the dynamic tail.

Real objects only (testing policy): no mocks. The registry-derivation witness
drops a real ``.py`` module into the real ``reyn.core.part_types`` package,
mirroring ``tests/test_part_type_registry_taxonomy_0060.py``.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import reyn.core.part_types as part_types_pkg
from reyn.core.part_type_registry import PART_TYPE_REGISTRY, build_part_type_registry
from reyn.prompt.router_frame import (
    MAX_PART_TYPE_ROW_CHARS,
    MECHANISM_ROUTING_HEADER,
    render_mechanism_routing_frame,
    render_part_role_map,
)
from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

_MARKER_SRC = """\
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name={name!r},
    roles=frozenset({{"input"}}),
    category={category!r},
    registry_ref="reyn.data.skills.registry:build_skill_registry",
    description="a test-injected part-type (0060 Addendum C witness)",
)
"""


# ── Pin 1: registry-derivation (the crux) ────────────────────────────────────


def test_new_marked_part_type_auto_appears_in_routing_frame():
    """Tier 2: drop a new marked part-type into the REAL
    ``reyn.core.part_types`` package, touching NOTHING in
    ``render_part_role_map``/``render_mechanism_routing_frame`` — it
    auto-appears in the rendered frame (mirror of #2899's auto-appear
    witness, applied at the SP layer, 0060 Addendum C C3)."""
    real_dir = Path(part_types_pkg.__file__).parent
    injected = real_dir / "_witness_routing_part.py"
    mod_qual = "reyn.core.part_types._witness_routing_part"
    injected.write_text(
        _MARKER_SRC.format(name="witness_routing_part", category="witness_cat")
    )
    try:
        importlib.invalidate_caches()
        rebuilt = build_part_type_registry()
        assert "witness_routing_part" in rebuilt

        frame = render_mechanism_routing_frame(rebuilt)
        assert "witness_routing_part (witness_cat): input" in frame, (
            "a marker dropped into the real part_types package did not "
            "auto-appear in the derived routing frame"
        )
        # The 5 real ones are still present (drop-in is additive, zero drift).
        for real_name in ("skill", "pipeline", "mcp", "hook", "presentation"):
            assert real_name in frame
    finally:
        injected.unlink(missing_ok=True)
        sys.modules.pop(mod_qual, None)
        importlib.invalidate_caches()


def test_hand_written_table_frozen_at_collection_falsifies_the_new_part_type():
    """Tier 2: FALSIFY — a hand-written table snapshotted BEFORE the new
    part-type is dropped in does NOT pick it up (the drift a hand-maintained
    parallel table would silently carry). This is the negative control that
    proves pin 1's auto-appear is doing real work, not incidental."""
    frozen_table = dict(PART_TYPE_REGISTRY)  # snapshot BEFORE the drop-in

    real_dir = Path(part_types_pkg.__file__).parent
    injected = real_dir / "_witness_routing_part2.py"
    mod_qual = "reyn.core.part_types._witness_routing_part2"
    injected.write_text(
        _MARKER_SRC.format(name="witness_routing_part2", category="witness_cat2")
    )
    try:
        importlib.invalidate_caches()
        rebuilt = build_part_type_registry()
        assert "witness_routing_part2" in rebuilt  # the live re-collect DOES see it

        # But the frozen (hand-list-shaped) snapshot does NOT — this is the
        # drift a hand-written parallel table would exhibit.
        frozen_frame = render_mechanism_routing_frame(frozen_table)
        assert "witness_routing_part2" not in frozen_frame, (
            "a frozen/hand-maintained table unexpectedly reflects a part-type "
            "registered after it was captured — derivation must be live"
        )
    finally:
        injected.unlink(missing_ok=True)
        sys.modules.pop(mod_qual, None)
        importlib.invalidate_caches()


def test_real_registry_five_part_types_all_appear_in_the_shipped_frame():
    """Tier 2: sanity — the shipped frame (default registry, no injection)
    lists all 5 currently-wired part-types, including ``hook`` (C2(3):
    hooks made visible for the first time)."""
    frame = render_mechanism_routing_frame()
    for name in ("skill", "pipeline", "mcp", "hook", "presentation"):
        assert name in frame
    assert "hook (hook): input" in frame


# ── Pin 2: scheme-independence ───────────────────────────────────────────────


def _sp_universal() -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    return build_system_prompt(
        agent_name="test", agent_role="tester", available_agents=[],
        memory_index={}, tool_use_sp=slots,
    )


def _sp_enumerate() -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=False,
        search_actions_enabled=True,
        discovery_mandate=True,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    return build_system_prompt(
        agent_name="test", agent_role="tester", available_agents=[],
        memory_index={}, tool_use_sp=slots,
    )


def _sp_retrieval() -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=False,
        search_actions_enabled=True,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    slots["slot_post_catalog"] = (
        "## Search\nCall search_actions with a natural-language query."
    )
    return build_system_prompt(
        agent_name="test", agent_role="tester", available_agents=[],
        memory_index={}, tool_use_sp=slots,
    )


def _sp_codeact() -> str:
    # CodeAct's str-shim path (only slot_pre_environment) + its free-form
    # code-API delivered via scheme_sp_fragment (#1618 REPLACE channel).
    return build_system_prompt(
        agent_name="test", agent_role="tester", available_agents=[],
        memory_index={}, tool_use_sp="## Code API\nCall tools via tool(name, **args).",
        scheme_sp_fragment="def file__read(path: str) -> str: ...",
    )


def _sp_bare_no_scheme() -> str:
    # No scheme attached at all — bare OS frame.
    return build_system_prompt(
        agent_name="test", agent_role="tester", available_agents=[],
        memory_index={}, tool_use_sp=None,
    )


def test_routing_frame_appears_under_all_four_schemes_and_bare_frame():
    """Tier 2: the routing frame appears in the SP under every tool-use
    scheme's slot shape (universal / enumerate / retrieval / codeact) AND
    with no scheme attached at all (0060 Addendum C, C1: OS-frame, not a
    scheme-owned slot)."""
    for label, sp in (
        ("universal", _sp_universal()),
        ("enumerate", _sp_enumerate()),
        ("retrieval", _sp_retrieval()),
        ("codeact", _sp_codeact()),
        ("bare/no-scheme", _sp_bare_no_scheme()),
    ):
        assert MECHANISM_ROUTING_HEADER in sp, (
            f"mechanism routing frame missing under the {label} scheme shape"
        )


def test_routing_frame_present_with_zero_scheme_supplied_content():
    """Tier 2: FALSIFY anchor for scheme-independence — with ``tool_use_sp=None``
    (the empty slot-map path, ``_slots == {}``), the routing frame still
    renders. Were it instead assembled from a scheme-owned slot value (e.g.
    injected only via ``slot_post_catalog``), this bare call would be missing
    it — exactly the regression pin 2 guards against."""
    sp = _sp_bare_no_scheme()
    assert MECHANISM_ROUTING_HEADER in sp
    assert "need INPUT (new data or a reactive trigger) -> hook | mcp | retrieval" in sp


# ── Pin 3: char-budget / cache-static ─────────────────────────────────────────


@dataclass(frozen=True)
class _FakeSpec:
    name: str
    roles: frozenset
    category: str


def test_per_part_type_row_is_bounded_and_raises_when_exceeded():
    """Tier 2: a pathologically long name/category raises instead of silently
    shipping an over-budget row (falsify: without the cap, an arbitrarily
    long row would render unbounded)."""
    huge = _FakeSpec(
        name="x" * 200, roles=frozenset({"input"}), category="y" * 200,
    )
    with pytest.raises(ValueError, match="cache-static budget"):
        render_part_role_map({"huge": huge})


def test_synthetic_larger_registry_stays_within_linear_budget():
    """Tier 2: as the meta-registry grows (a synthetic 50-part-type
    registry), the derived map's total size scales LINEARLY with the
    number of part-types (bounded per-row cost, C1's cost-discipline pin) —
    not superlinearly. Each individual row also respects the per-row cap."""
    part_count = 50
    synthetic = {
        f"part_{i}": _FakeSpec(
            name=f"part_{i}", roles=frozenset({"workflow"}), category="synthetic_cat",
        )
        for i in range(part_count)
    }
    rendered = render_part_role_map(synthetic)
    lines = rendered.splitlines()
    # One derived row per part-type — every synthetic name shows up, and
    # nothing else does (a superlinear design, e.g. an O(N^2) all-pairs
    # table, would not have this 1:1 correspondence).
    assert {line.strip().split()[1] for line in lines} == set(synthetic)
    for line in lines:
        assert len(line) <= MAX_PART_TYPE_ROW_CHARS
    # Linear bound: total length must not exceed part_count * per-row cap
    # (+ small constant slack for joining newlines) — derived from the
    # synthetic registry's own size, not a hardcoded magic number.
    per_row_budget = MAX_PART_TYPE_ROW_CHARS
    assert len(rendered) <= part_count * per_row_budget + part_count


def test_routing_frame_sits_in_the_static_section_before_behaviour():
    """Tier 2: the routing frame is placed ahead of "## Behaviour" in the
    assembled SP — i.e. in the static cache-prefix block, not the dynamic
    tail (0060 Addendum C, C1)."""
    sp = _sp_universal()
    routing_idx = sp.index(MECHANISM_ROUTING_HEADER)
    behaviour_idx = sp.index("## Behaviour")
    assert routing_idx < behaviour_idx, (
        "mechanism routing frame must precede '## Behaviour' (static prefix, "
        "not the dynamic tail)"
    )
