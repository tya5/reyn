"""Tier 2: OS invariant — generalized builtin SKILL.md tool-name drift gate
(#3092).

The #3090 gate (``tests/plugins/test_fp0063_p4_builtin_rag_skill.py``) checked ONE
skill — the RAG plugin's ``build-and-query-rag-corpus`` — against the real
enumerate-all catalog. Every OTHER builtin SKILL.md was unprotected, and
#3092 found exactly the drift that gap predicts: the standing builtins
``draft-judge-revise/SKILL.md`` and ``reyn-cheat-sheet/SKILL.md`` told the
model to call ``run_pipeline_inline(...)`` / ``run_pipeline(name=...)`` —
host FUNCTION names, not the qualified catalog names
(``run_pipeline_inline`` / ``run_pipeline``) an enumerate-all ``tools=``
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
  2. **Detection shape**: two complementary checks, both grounded in live
     tables rather than marker lists. (a) a ``<a>__<b>(`` CALL is drift by
     construction, because #3429 abolished that spelling; (b) a CALL of an
     OS-INTERNAL op kind — one no tool answers to, derived as
     ``OP_KIND_MODEL_MAP`` minus the registry minus the catalog — is prose
     telling the model to call something it will never be offered.

**#3429 collapsed #3092's own drift class.** #3092 found
``draft-judge-revise`` / ``reyn-cheat-sheet`` telling the model to call
``run_pipeline_inline(...)`` — the host FUNCTION name, when the catalog
advertised only ``pipeline__run_inline``. That gap existed because a tool had
two names and the skill author picked the wrong one. There is one name now,
and it is ``run_pipeline_inline``, so that exact prose is correct. Check (b)
keeps the general shape of the failure covered for the names that ARE still
internal-only.

Extraction/lookup logic is SHARED with the #3090 RAG-only gate
(``tests/_support/builtin_skill_tool_names.py``) rather than re-implemented
here, so the checks cannot silently diverge.

No mocks: the real builtin SKILL.md files on disk, the real
``catalog_entries``, and the real ``universal_dispatch`` routing table.
"""
from __future__ import annotations

from tests._support.builtin_skill_tool_names import (
    REPO_ROOT,
    bare_os_internal_calls_referenced,
    discover_builtin_skill_md_files,
    os_internal_op_kind_names,
    qualified_tool_calls_referenced,
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
    assert "build-and-query-rag-corpus" in names
    # the two standing builtins #3092 found drifted — the RAG-only gate
    # never looked at either of these
    assert "draft-judge-revise" in names
    assert "reyn-cheat-sheet" in names
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


def test_no_builtin_skill_calls_a_qualified_tool_name() -> None:
    """Tier 2: (#3092/#3429) no builtin SKILL.md CALLs an ``<a>__<b>`` name.

    Until #3429 this test asked the opposite question — that each such name
    RESOLVED in the real catalog — because the qualified spelling was the one
    the catalog advertised. The spelling is abolished, so any hit is prose
    handing the model a tool call that will not exist in its ``tools=``
    payload, which is the #3090 failure mode it cannot debug or recover
    from."""
    skill_files = discover_builtin_skill_md_files()
    assert skill_files, "fixture invariant: at least one builtin SKILL.md must exist"

    failures: dict[str, list[str]] = {}
    for path in skill_files:
        hits = qualified_tool_calls_referenced(path.read_text(encoding="utf-8"))
        if hits:
            failures[str(path.relative_to(REPO_ROOT))] = sorted(hits)

    assert not failures, (
        f"builtin SKILL.md file(s) CALL a qualified tool name, abolished in "
        f"#3429: {failures}"
    )


def test_at_least_one_builtin_skill_actually_calls_a_real_catalog_tool() -> None:
    """Tier 2: (fixture invariant) the checks here are vacuously green if no
    skill tells the model to call any tool at all. Confirm the corpus
    collectively CALLs at least one real catalog action — so the extraction is
    running against prose that actually contains tool calls."""
    import re

    real_names = real_catalog_tool_names()
    called: set[str] = set()
    for path in discover_builtin_skill_md_files():
        body = path.read_text(encoding="utf-8")
        called |= {
            n for n in re.findall(r"\b([a-z][a-z0-9_]*)\(", body) if n in real_names
        }
    assert called, (
        "fixture invariant: no builtin SKILL.md CALLs any real catalog tool, "
        "so the drift checks are iterating prose with nothing to check"
    )


# ---------------------------------------------------------------------------
# 3. no builtin skill calls an INTERNAL dispatch-target name bare (the
#    ACTUAL #3092 drift shape — no `__`, so axis-2 above)
# ---------------------------------------------------------------------------


def test_no_builtin_skill_calls_an_os_internal_op_kind() -> None:
    """Tier 2: (#3092/#3429) no builtin SKILL.md may CALL an OS-INTERNAL op
    kind — a name in ``OP_KIND_MODEL_MAP`` that no registered tool and no
    catalog action answers to (``sandboxed_exec``, ``semantic_search``,
    ``index_query``, the ``skill_install`` / ``plugin_install`` op kinds behind
    the install verbs). Such a name can never appear in the LLM's ``tools=``
    payload, so prose telling the model to call it is the #3090 failure mode.

    #3429 re-grounded the set this reads. It used to be "the RHS of the
    qualified→flat routing table" — every dispatch target was internal-only
    because only the qualified LHS was advertised, which is why
    ``run_pipeline_inline(`` was #3092's real drift. That table is gone and
    those targets ARE the advertised names, so the class is re-derived from
    the op-kind layer, where internal-only names still genuinely exist."""
    skill_files = discover_builtin_skill_md_files()
    assert skill_files, "fixture invariant: at least one builtin SKILL.md must exist"

    failures: dict[str, list[str]] = {}
    for path in skill_files:
        hits = bare_os_internal_calls_referenced(path.read_text(encoding="utf-8"))
        if hits:
            failures[str(path.relative_to(REPO_ROOT))] = sorted(hits)

    assert not failures, (
        f"builtin SKILL.md file(s) CALL an OS-internal op kind the LLM is "
        f"never offered: {failures}"
    )


def test_the_os_internal_name_set_is_non_empty_and_excludes_real_tools() -> None:
    """Tier 2: (regrounding) the internal-name set the check above reads is
    neither empty (which would make it vacuous) nor polluted with names the
    LLM legitimately calls (which would make it a false-positive machine).

    Both halves matter: a derivation bug that returned ``set()`` would keep
    the check green through any drift, and one that leaked ``read_file`` in
    would fail every skill that correctly teaches it."""
    internal = os_internal_op_kind_names()
    assert "sandboxed_exec" in internal, (
        "the op kind behind the `exec` TOOL is the canonical internal-only "
        "name; if it is absent the derivation is wrong"
    )
    assert internal.isdisjoint(real_catalog_tool_names()), (
        f"internal-name set overlaps the real catalog: "
        f"{sorted(internal & real_catalog_tool_names())}"
    )


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

    assert qualified_tool_calls_referenced(drifted_text) == {"pipeline__run_ghost"}

    # …and the OS-internal arm goes RED on its own drift shape.
    assert bare_os_internal_calls_referenced(
        real_skill_path.read_text(encoding="utf-8") + "\nsandboxed_exec(argv=[])\n",
    ) == {"sandboxed_exec"}
