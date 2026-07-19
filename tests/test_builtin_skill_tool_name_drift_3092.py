"""Tier 2: OS invariant — generalized builtin SKILL.md tool-name drift gate
(#3092).

The #3090 gate (``tests/test_fp0063_p4_builtin_rag_skill.py``) checked ONE
skill — the RAG plugin's ``build_and_query_rag_corpus`` — against the real
enumerate-all catalog. Every OTHER builtin SKILL.md was unprotected, and
#3092 found exactly the drift that gap predicts: the standing builtins
``draft_judge_revise/SKILL.md`` and ``reyn_cheat_sheet/SKILL.md`` told the
model to call ``run_pipeline_inline(...)`` / ``run_pipeline(name=...)`` —
host FUNCTION names, not the qualified catalog names
(``pipeline__run_inline`` / ``pipeline__run``) an enumerate-all ``tools=``
payload actually carries. This is the SAME #3090 failure shape (a weak model
handed a ``tools=`` payload with no matching entry cannot find the right
tool and loops on the nearest-spelled wrong one), just on a skill the old
gate never looked at.

This file generalizes the check ALONG TWO AXES:

  1. **Coverage**: enumerate EVERY builtin SKILL.md (both the always-on
     ``BUILTIN_SKILLS`` skills and every builtin plugin's skills, via the
     same ``skills/<name>/SKILL.md`` discovery convention the runtime itself
     uses — see ``tests/_support/builtin_skill_tool_names.py``), and require
     every QUALIFIED tool name each one's prose calls to resolve in the REAL
     ``catalog_entries(ctx)`` catalog.
  2. **Detection shape**: the #3090 gate's extractor only matches the
     ``category__verb(`` CALL shape, which is blind to #3092's actual drift
     — a bare pre-refactor host-function-name CALL with no ``__`` separator
     (``run_pipeline_inline(...)``, ``run_pipeline(name=...)``). Regrounding
     below
     (``test_the_qualified_only_check_alone_would_have_missed_the_real_3092_drift``)
     shows the qualified-only extractor stays GREEN on the real pre-fix
     #3092 text — so this file adds a second, complementary check
     (``bare_internal_dispatch_target_calls_referenced``) grounded in the
     SAME routing table (``universal_dispatch._OPERATION_RULES`` /
     ``_RESOURCE_RULES``) the real catalog is built from: a bare CALL of one
     of those routing table's INTERNAL target names is, by construction,
     never a name the LLM's ``tools=`` payload can carry.

Extraction/lookup logic is SHARED with the #3090 RAG-only gate
(``tests/_support/builtin_skill_tool_names.py``) rather than re-implemented
here, so the checks cannot silently diverge.

No mocks: the real builtin SKILL.md files on disk, the real
``catalog_entries``, and the real ``universal_dispatch`` routing table.
"""
from __future__ import annotations

from tests._support.builtin_skill_tool_names import (
    REPO_ROOT,
    bare_internal_dispatch_target_calls_referenced,
    discover_builtin_skill_md_files,
    qualified_tool_names_referenced,
    real_catalog_tool_names,
)

# ---------------------------------------------------------------------------
# 1. discovery is real generalization, not a rename that still finds one file
# ---------------------------------------------------------------------------


def test_discovery_finds_every_builtin_skill_not_just_the_rag_plugin() -> None:
    """Tier 2: (witness) the glob-based discovery surfaces skills from BOTH
    the always-on ``src/reyn/builtin/skills/`` dir AND a builtin plugin's own
    ``skills/`` dir (``src/reyn/builtin/plugins/rag/skills/``) — not only the
    one skill the #3090 gate was scoped to. Regrounds that "generalized"
    means what it claims, not a name change over the same one-file coverage."""
    found = discover_builtin_skill_md_files()
    names = {p.parent.name for p in found}

    # the #3090 gate's only skill — still covered, now via the general path
    assert "build_and_query_rag_corpus" in names
    # the two standing builtins #3092 found drifted — the RAG-only gate
    # never looked at either of these
    assert "draft_judge_revise" in names
    assert "reyn_cheat_sheet" in names
    # the standing builtins live under a DIFFERENT dir than the plugin skill
    # (`skills/` vs `plugins/rag/skills/`) — discovery must span both, not
    # just widen the single-dir glob the old gate hardcoded.
    standing_dir = REPO_ROOT / "src" / "reyn" / "builtin" / "skills"
    plugin_dir = REPO_ROOT / "src" / "reyn" / "builtin" / "plugins" / "rag" / "skills"
    assert any(standing_dir in path.parents for path in found)
    assert any(plugin_dir in path.parents for path in found)

    # every discovered path is a real file, and every one sits under a
    # `skills/<dirname>/SKILL.md` layout (the discovery convention itself)
    for path in found:
        assert path.is_file()
        assert path.name == "SKILL.md"
        assert path.parent.parent.name == "skills"


# ---------------------------------------------------------------------------
# 2. every builtin SKILL.md's tool references resolve in the real catalog
# ---------------------------------------------------------------------------


def test_every_builtin_skill_tool_reference_resolves_in_the_real_catalog() -> None:
    """Tier 2: (#3092) for EVERY builtin SKILL.md — not only the RAG plugin's
    — every qualified tool name its prose tells the model to call must be a
    name the REAL enumerate-all catalog (``catalog_entries(ctx)``) actually
    enumerates. A drift here is the #3090 failure mode: the skill hands the
    model a tool call that does not exist in its ``tools=`` payload, which it
    cannot debug and reliably cannot recover from."""
    real_names = real_catalog_tool_names()
    skill_files = discover_builtin_skill_md_files()
    assert skill_files, "fixture invariant: at least one builtin SKILL.md must exist"

    failures: dict[str, list[str]] = {}
    for path in skill_files:
        referenced = qualified_tool_names_referenced(
            path.read_text(encoding="utf-8"),
        )
        missing = sorted(name for name in referenced if name not in real_names)
        if missing:
            failures[str(path.relative_to(REPO_ROOT))] = missing

    assert not failures, (
        f"builtin SKILL.md file(s) reference tool name(s) absent from the "
        f"real enumerate-all catalog: {failures}"
    )


def test_at_least_one_builtin_skill_actually_calls_a_qualified_tool() -> None:
    """Tier 2: (fixture invariant) the positive test above is vacuously green
    if no skill references any qualified tool at all. Confirm the corpus of
    builtin SKILL.md files collectively references at least one — so the
    check above is exercising real extraction, not iterating an empty set."""
    skill_files = discover_builtin_skill_md_files()
    total_referenced = set()
    for path in skill_files:
        total_referenced |= qualified_tool_names_referenced(
            path.read_text(encoding="utf-8"),
        )
    assert total_referenced, (
        "fixture invariant: at least one builtin SKILL.md must reference a "
        "qualified tool name"
    )


# ---------------------------------------------------------------------------
# 3. no builtin skill calls an INTERNAL dispatch-target name bare (the
#    ACTUAL #3092 drift shape — no `__`, so axis-2 above)
# ---------------------------------------------------------------------------


def test_no_builtin_skill_calls_an_internal_dispatch_target_name_bare() -> None:
    """Tier 2: (#3092) no builtin SKILL.md may CALL a bare, non-qualified
    INTERNAL dispatch-target function name (``run_pipeline_inline(...)``,
    ``skill_install_local(...)``, ...) — these are, by construction, never a
    name the enumerate-all catalog offers the LLM (only the qualified LHS
    name is). This is the check axis 1's ``category__verb(`` extractor
    cannot perform (no ``__`` present), and it is the ACTUAL shape #3092's
    real drift took on ``draft_judge_revise`` / ``reyn_cheat_sheet``."""
    skill_files = discover_builtin_skill_md_files()
    assert skill_files, "fixture invariant: at least one builtin SKILL.md must exist"

    failures: dict[str, list[str]] = {}
    for path in skill_files:
        hits = bare_internal_dispatch_target_calls_referenced(
            path.read_text(encoding="utf-8"),
        )
        if hits:
            failures[str(path.relative_to(REPO_ROOT))] = sorted(hits)

    assert not failures, (
        f"builtin SKILL.md file(s) CALL an internal (non-LLM-facing) "
        f"dispatch-target name bare: {failures}"
    )


def test_the_qualified_only_check_alone_would_have_missed_the_real_3092_drift() -> None:
    """Tier 2: (regrounding) the REAL pre-fix #3092 drift text —
    ``run_pipeline_inline(...)`` / ``run_pipeline(name=...)``, taken verbatim
    from the drifted ``draft_judge_revise`` / ``reyn_cheat_sheet`` SKILL.md
    bodies before this PR's fix — produces an EMPTY set under
    ``qualified_tool_names_referenced`` (no ``__`` separator, so axis-1's
    regex never even sees it). Without axis-2
    (``bare_internal_dispatch_target_calls_referenced``), a generalized gate
    built ONLY on the #3090 qualified-name extractor would have stayed GREEN
    on this exact drift — the failure this whole file exists to close.
    Axis-2 DOES catch it."""
    drifted_text = (
        "Launch it with:\n\n```\n"
        "run_pipeline_inline(\n"
        '  definition="<the two documents above>",\n'
        "  input={draft: \"...\"},\n"
        ")\n```\n\n"
        "invoke by name: `run_pipeline(name=\"flagship.research_and_report\", "
        'input={"query": "..."})`\n'
    )

    assert qualified_tool_names_referenced(drifted_text) == set()
    assert bare_internal_dispatch_target_calls_referenced(drifted_text) == {
        "run_pipeline_inline", "run_pipeline",
    }


# ---------------------------------------------------------------------------
# 4. the gate is load-bearing: reinjecting the #3092 drift shape goes RED
# ---------------------------------------------------------------------------


def test_gate_is_not_vacuous_reinjecting_drift_goes_red() -> None:
    """Tier 2: (regrounding, strip-falsify) inject a qualified-shaped tool
    name that does NOT exist in the real catalog (the #3090/#3092 shape —
    a plausible-looking ``category__verb`` name that drifted from the
    catalog) into a real skill's text and confirm the check goes RED.
    Without this, an over-permissive ``real_names`` set or a vacuous regex
    would keep the positive test green through any drift and make the whole
    gate decorative — the exact hazard the RAG-only gate's own regrounding
    test (``test_tool_name_catalog_check_is_not_vacuous``) already guards
    against for its one skill; this pins the same property for the
    generalized gate."""
    real_skill_path = discover_builtin_skill_md_files()[0]
    drifted_text = (
        real_skill_path.read_text(encoding="utf-8")
        + '\npipeline__run_ghost(name="x")\n'
    )

    referenced = qualified_tool_names_referenced(drifted_text)
    real_names = real_catalog_tool_names()
    missing = sorted(name for name in referenced if name not in real_names)
    assert missing == ["pipeline__run_ghost"]
