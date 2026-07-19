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

REPO_ROOT = Path(__file__).parent.parent.parent
BUILTIN_DIR = REPO_ROOT / "src" / "reyn" / "builtin"


class _NoOpEvents:
    """Real ToolContext requires an events sink; a no-op recorder is not a
    faked COLLABORATOR under test (nothing here asserts on events) — same
    shape as ``tests/test_catalog_entries_1593.py``'s fixture."""

    def emit(self, *args, **kwargs) -> None:
        pass


def real_catalog_tool_names() -> "set[str]":
    """The REAL qualified tool names an enumerate-all LLM turn is sent —
    ``catalog_entries(ctx)`` is single-source for every ``mcp__`` /
    ``pipeline__`` / ``plugin_management__`` / ... name (#3026: every
    category is a STATIC operation category enumerated from
    ``universal_dispatch._OPERATION_RULES`` — no operator-state ctx needed
    to produce the NAMES, only to gate availability)."""
    ctx = ToolContext(
        events=_NoOpEvents(), permission_resolver=None, workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(host=None, mcp_servers=None),
    )
    return {entry["name"] for entry in catalog_entries(ctx)}


def qualified_tool_names_referenced(text: str) -> "set[str]":
    """Extract every qualified (`category__verb`) tool-CALL name from a
    SKILL.md body. Extracted, never restated: a hardcoded list would drift
    with the prose it claims to guard and stay green through the exact bug
    (#3090 / #3092) this extraction exists to catch.

    REACH LIMIT (do not read a clean result as "no drift anywhere" in the
    source text): matches only the CALL shape ``verb(`` — a tool name
    mentioned in prose WITHOUT parens, or a non-qualified bare FUNCTION name
    with no ``__`` separator (the exact #3092 drift shape,
    ``run_pipeline_inline(...)`` rather than ``pipeline__run_inline(...)``),
    is invisible to this regex. That is a deliberate precision/reach trade
    (see ``tests/test_fp0063_p4_builtin_rag_skill.py``'s twin extractor for
    why widening it is not the fix: a bare mention can equally be a
    legitimate internal-module reference). #3092's fix is to REWRITE the
    drifted prose to the qualified form, which this extractor then covers
    going forward — not to widen the regex."""
    return set(re.findall(
        r"\b([a-zA-Z][a-zA-Z0-9_]*__[a-zA-Z][a-zA-Z0-9_]*)\(", text,
    ))


def _internal_dispatch_target_names() -> "set[str]":
    """The set of INTERNAL dispatch-target function names (= the RHS of
    ``universal_dispatch._OPERATION_RULES`` / ``_RESOURCE_RULES`` — e.g.
    ``run_pipeline_inline``, ``skill_install_local``, ``mcp_call_tool``) that
    are, by construction, never a name the enumerate-all catalog offers the
    LLM: only the LHS QUALIFIED name (``pipeline__run_inline``,
    ``skill_management__install_local``, ``mcp__call_tool``) is ever put in a
    ``tools=`` payload (see that module's own docstring: "a resource is an
    ARGUMENT to a verb, never a tool of its own" / the #879->#1647 cautionary
    tale). A bare CALL of one of these names in SKILL.md prose is therefore
    always the #3090/#3092 drift shape — the pre-refactor "friendly" host
    function name instead of the qualified catalog name — never a false
    positive on an unrelated identifier, because these strings are reserved
    to routing-internal use by the routing table itself."""
    from reyn.tools.universal_dispatch import _OPERATION_RULES, _RESOURCE_RULES

    return (
        {target for target, _ in _OPERATION_RULES.values()}
        | {target for target, _ in _RESOURCE_RULES.values()}
    )


def bare_internal_dispatch_target_calls_referenced(text: str) -> "set[str]":
    """Extract every CALL-shaped bare identifier in *text* that matches an
    INTERNAL dispatch-target function name (never a name the LLM's ``tools=``
    payload can carry). This complements ``qualified_tool_names_referenced``:
    that extractor only sees ``category__verb(`` shapes and is blind to a
    bare pre-refactor host-function-name call like ``run_pipeline_inline(``
    (no ``__`` separator) — precisely the #3092 drift shape the qualified-only
    extractor could not see even after generalizing it to every builtin
    SKILL.md. Grounded in the SAME single-source routing table
    (``universal_dispatch``) the real catalog is built from, not a
    hand-maintained marker list.

    Restricted to candidates WITHOUT a ``__`` separator: a handful of
    dispatch-target values are SELF-mapped (``plugin_management__install`` ->
    ``plugin_management__install`` — see that routing table's own #3083
    comment: "unlike most other management verbs, there is no separate
    'bare' spelling to alias"), so a qualified-SHAPED candidate is always
    already covered (and correctly validated) by
    ``qualified_tool_names_referenced`` — flagging it again here, against
    the same internal-name set, would false-positive on that legitimate
    self-mapped case."""
    candidates = {
        name for name in re.findall(r"\b([a-z][a-z0-9_]*)\(", text)
        if "__" not in name
    }
    targets = {
        name for name in _internal_dispatch_target_names() if "__" not in name
    }
    return candidates & targets


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
