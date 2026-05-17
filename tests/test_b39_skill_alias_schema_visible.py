"""Tier 2: B39 structural verification — 4 stdlib skills now appear in ARS with non-empty schema.

B36/B37/B38 retros all flagged the same hot-list coverage gap:
``skill__index_docs``, ``skill__read_local_files``, ``skill__direct_llm``, and
``skill__eval`` were absent from the ARS because their catalogue entries lacked
``input_schema`` (= the D2-full condition ``properties non-empty`` skipped them).

This test file verifies the B39 fix end-to-end:

1. ``enumerate_available_skills`` returns non-empty ``input_schema.properties``
   for all 4 skills.
2. A real ``skill_meta_map`` built from that catalogue includes all 4 as
   ``skill__<name>`` entries.
3. ``_collect_all_session_ars_entries`` with that skill_meta_map produces
   non-empty ``properties`` for each of the 4 qualified names.

No mocks: all assertions use the real ``enumerate_available_skills`` and
``_collect_all_session_ars_entries`` with real stdlib DSL files on disk.
"""
from __future__ import annotations

import pytest

from reyn.chat.router_loop import _collect_all_session_ars_entries
from reyn.chat.session import enumerate_available_skills


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

TARGET_SKILLS = ["index_docs", "read_local_files", "direct_llm", "eval"]
TARGET_QUALIFIED = {f"skill__{n}" for n in TARGET_SKILLS}


def _build_skill_meta_map() -> dict[str, dict]:
    """Build a skill_meta_map from the real stdlib catalogue."""
    skills = enumerate_available_skills(exclude=set())
    meta_map: dict[str, dict] = {}
    for s in skills:
        if not isinstance(s, dict) or "name" not in s:
            continue
        if "input_schema" not in s:
            continue
        qn = f"skill__{s['name']}"
        meta_map[qn] = {
            "description": s.get("description", ""),
            "input_schema": s["input_schema"],
            "input_wrapped": bool(s.get("input_wrapped", True)),
        }
    return meta_map


# ---------------------------------------------------------------------------
# (a) All 4 target skills appear in enumerate_available_skills with input_schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", TARGET_SKILLS)
def test_enumerate_available_skills_has_input_schema(skill_name: str) -> None:
    """Tier 2: enumerate_available_skills returns non-empty input_schema for each target skill.

    Pre-B39: skill__direct_llm and skill__read_local_files were missing
    input_schema entirely; skill__index_docs and skill__eval were already
    working.  All 4 are asserted here for explicit regression coverage.
    """
    skills = enumerate_available_skills(exclude=set())
    by_name = {s["name"]: s for s in skills if isinstance(s, dict)}
    if skill_name not in by_name:
        pytest.skip(f"skill '{skill_name}' not found in stdlib catalogue")
    entry = by_name[skill_name]
    assert "input_schema" in entry, (
        f"skill '{skill_name}' catalogue entry must have input_schema after B39 fix"
    )
    props = entry["input_schema"].get("properties") or {}
    assert props, (
        f"skill '{skill_name}' input_schema.properties must be non-empty after B39 fix"
    )


# ---------------------------------------------------------------------------
# (b) skill_meta_map built from catalogue includes all 4 qualified names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qualified_name", sorted(TARGET_QUALIFIED))
def test_skill_meta_map_includes_target(qualified_name: str) -> None:
    """Tier 2: skill_meta_map built from the catalogue includes each target qualified name.

    The skill_meta_map is the intermediate dict that feeds _build_hot_list_aliases
    and _collect_all_session_ars_entries.  All 4 target skills must be present
    with non-empty input_schema.
    """
    meta_map = _build_skill_meta_map()
    assert qualified_name in meta_map, (
        f"skill_meta_map must include '{qualified_name}' after B39 fix"
    )
    schema = meta_map[qualified_name].get("input_schema") or {}
    props = schema.get("properties") or {}
    assert props, (
        f"skill_meta_map['{qualified_name}'].input_schema.properties must be non-empty"
    )


# ---------------------------------------------------------------------------
# (c) _collect_all_session_ars_entries with real catalogue includes all 4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qualified_name", sorted(TARGET_QUALIFIED))
def test_collect_all_session_ars_entries_includes_target(qualified_name: str) -> None:
    """Tier 2: _collect_all_session_ars_entries includes each target skill with non-empty props.

    This is the structural verification that the B39 fix unblocks the hot-list
    coverage gap.  Pre-B39 fix, skill__direct_llm and skill__read_local_files
    were absent from this function's output because their catalogue entries
    lacked input_schema, and the function filters out actions without non-empty
    properties (see router_loop.py source 2: ``if not props: continue``).
    """
    meta_map = _build_skill_meta_map()
    ars_entries = _collect_all_session_ars_entries(skill_meta_map=meta_map)
    entry_dict = {qn: props for qn, props in ars_entries}

    assert qualified_name in entry_dict, (
        f"'{qualified_name}' must appear in _collect_all_session_ars_entries after B39 fix; "
        f"present skill entries: {sorted(k for k in entry_dict if k.startswith('skill__'))}"
    )
    props = entry_dict[qualified_name]
    assert props, (
        f"_collect_all_session_ars_entries['{qualified_name}'] must have non-empty properties"
    )


# ---------------------------------------------------------------------------
# (d) Backwards-compat: existing callers of direct_llm / read_local_files
#     still work (input artifact is still user_message)
# ---------------------------------------------------------------------------


def test_direct_llm_input_artifact_is_user_message() -> None:
    """Tier 2: direct_llm catalogue entry still reports user_message as input_artifact.

    Adding user_message.yaml to direct_llm/artifacts/ must not change the
    input_artifact name exposed by the catalogue (it must still read from the
    entry phase frontmatter, which says ``input: user_message``).
    Backwards-compat: any caller already passing user_message continues to work.
    """
    skills = enumerate_available_skills(exclude=set())
    by_name = {s["name"]: s for s in skills if isinstance(s, dict)}
    if "direct_llm" not in by_name:
        pytest.skip("direct_llm not found")
    entry = by_name["direct_llm"]
    input_artifact = entry.get("input_artifact", "")
    assert "user_message" in input_artifact, (
        f"direct_llm input_artifact must still include 'user_message'; got {input_artifact!r}"
    )


def test_read_local_files_input_artifact_is_user_message() -> None:
    """Tier 2: read_local_files catalogue entry still reports user_message as input_artifact.

    Same backwards-compat check as direct_llm.
    """
    skills = enumerate_available_skills(exclude=set())
    by_name = {s["name"]: s for s in skills if isinstance(s, dict)}
    if "read_local_files" not in by_name:
        pytest.skip("read_local_files not found")
    entry = by_name["read_local_files"]
    input_artifact = entry.get("input_artifact", "")
    assert "user_message" in input_artifact, (
        f"read_local_files input_artifact must still include 'user_message'; got {input_artifact!r}"
    )
