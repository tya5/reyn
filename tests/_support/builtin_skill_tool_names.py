"""Shared SKILL.md <-> catalog tool-name drift-detection helpers (#3092).

Single source for the extraction/lookup logic the #3090 RAG-only gate
(``tests/test_fp0063_p4_builtin_rag_skill.py``) originated and #3092
generalizes to every builtin SKILL.md
(``tests/test_builtin_skill_tool_name_drift_3092.py``). Factored out here so
the two test files share ONE regex / ONE catalog lookup rather than two
independent copies that could silently drift from each other (the same
duplication hazard the extraction functions themselves guard against for
SKILL.md prose).

No mocks: ``real_catalog_tool_names`` builds a real ``ToolContext`` and calls
the real ``catalog_entries`` — the single source ``list_actions`` /
``describe_action`` / the live ``tools=`` payload all agree against (#1455).
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import catalog_entries
from tests._support.paths import REPO_ROOT

BUILTIN_DIR = REPO_ROOT / "src" / "reyn" / "builtin"


class _NoOpEvents:
    """Real ToolContext requires an events sink; a no-op recorder is not a
    faked COLLABORATOR under test (nothing here asserts on events) — same
    shape as ``tests/test_catalog_entries_1593.py``'s fixture."""

    def emit(self, *args, **kwargs) -> None:
        pass


def real_catalog_tool_names() -> "set[str]":
    """The REAL tool names an enumerate-all LLM turn is sent —
    ``catalog_entries(ctx)`` is single-source for every one of them (#3026:
    every category is a STATIC operation category enumerated from the
    membership table — no operator-state ctx needed to produce the NAMES, only
    to gate availability)."""
    ctx = ToolContext(
        events=_NoOpEvents(), permission_resolver=None, workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(host=None, mcp_servers=None),
    )
    return {entry["name"] for entry in catalog_entries(ctx)}


def qualified_tool_calls_referenced(text: str) -> "set[str]":
    """Extract every ``<a>__<b>(`` tool-CALL name from a SKILL.md body.

    #3429 inverted this extractor's verdict. It used to find the QUALIFIED
    spelling and check that each hit RESOLVED in the real catalog — the
    spelling was correct and the risk was a stale one. The spelling is
    abolished, so any hit is drift by construction: prose telling the model to
    call a name the OS no longer answers to.

    REACH LIMIT (do not read a clean result as "no drift anywhere"): matches
    only the CALL shape, so a name mentioned in prose without parens is
    invisible. That is a deliberate precision/reach trade — a bare mention can
    equally be a legitimate internal-module reference."""
    return set(re.findall(
        r"\b([a-zA-Z][a-zA-Z0-9_]*__[a-zA-Z][a-zA-Z0-9_]*)\(", text,
    ))


def os_internal_op_kind_names() -> "set[str]":
    """Control-IR OP KINDS that no tool answers to — names the LLM's ``tools=``
    payload can never carry, DERIVED as ``OP_KIND_MODEL_MAP`` minus every
    registered tool name and every catalog action.

    #3429 re-grounded this set. It used to be "the RHS of the qualified→flat
    routing table": every dispatch TARGET was an internal name because only the
    qualified LHS was ever advertised, which made ``run_pipeline_inline(`` in
    SKILL.md prose the #3092 drift shape. The routing table is gone and those
    targets ARE the advertised names now, so that particular drift class cannot
    occur — but the general one can, because the op-runtime layer still has
    kinds with no tool of the same name (``sandboxed_exec``, ``semantic_search``,
    ``index_query``, the ``plugin_install`` / ``skill_install`` op kinds behind
    the install verbs). A CALL of one of those in SKILL.md prose is prose
    telling the model to call something it will never be offered.

    (#3141 still applies: this does NOT exclude fenced code blocks, so a
    dual-use name such as ``semantic_search`` shown in a legitimate Python-step
    example could still trip the check that reads this set.)"""
    from reyn.schemas.models import OP_KIND_MODEL_MAP
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

    return (
        set(OP_KIND_MODEL_MAP)
        - set(get_default_registry().names())
        - set(KNOWN_ACTION_NAMES)
    )


def bare_os_internal_calls_referenced(text: str) -> "set[str]":
    """Extract every CALL-shaped bare identifier in *text* that names an
    OS-internal op kind the LLM can never be offered.

    Grounded in the live ``OP_KIND_MODEL_MAP`` + registry, not a
    hand-maintained marker list — a new op kind without a matching tool is
    covered the moment it is declared."""
    candidates = set(re.findall(r"\b([a-z][a-z0-9_]*)\(", text))
    return candidates & os_internal_op_kind_names()


def discover_builtin_skill_md_files() -> "list[Path]":
    """Enumerate every builtin SKILL.md via the SAME ``skills/<name>/SKILL.md``
    discovery convention ``reyn.builtin.registry`` (standing ``BUILTIN_SKILLS``)
    and the plugin manifest's empty-``entries`` convention (a builtin plugin's
    own ``skills/*/SKILL.md``) both already use — see
    ``src/reyn/builtin/registry.py``'s module docstring and
    ``src/reyn/plugins/manifest.py``'s discovery-by-dirname convention.

    A recursive glob, not a hand-maintained name list ([[coverage migration:
    enumerate from registry, not marker subset]]) — a new standing builtin
    skill OR a new builtin plugin's skill is covered the moment its SKILL.md
    lands under ``src/reyn/builtin/**/skills/<name>/SKILL.md``, no separate
    registration step in this test suite required."""
    return sorted(BUILTIN_DIR.glob("**/skills/*/SKILL.md"))
