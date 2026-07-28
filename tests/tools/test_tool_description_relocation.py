"""Tier 1: tool-description package relocation invariants (Phase 1: discovery).

Phase 1 of the tool-description package refactor (`src/reyn/tools/descriptions/`)
relocated each ``discovery``-category ToolDefinition's ``description`` string
into a reviewable ``ToolDescription`` record, with the origin tool module
aliasing its ``_X_DESCRIPTION`` constant to ``descriptions.discovery.NAME.text``.
That refactor is long done; the checks below are the invariants that survive it.

**#3383 stage 2 (policy compliance).** This file used to also carry a
byte-identical golden-file comparison against a committed
``_pre_migration_tool_schemas_baseline.json`` fixture — a snapshot-test shape
``docs/deep-dives/contributing/testing.md`` forbids outside ``tests/scaffold/``.
It outlived the refactor it was written to validate, but it earned an
extension while deciding what to do about that: it caught a real product
defect (#3383 — litellm's Gemini/Vertex provider transform mutates the
``tools[]`` payload it is handed IN PLACE, and ``render_for_router`` used to
return a SHALLOW copy, so one Gemini/Vertex call permanently rewrote reyn's
canonical schema for every later render, of any tool, for any provider).

#3385 then asserted that invariant DIRECTLY — the stronger, policy-compliant
form — in
``tests/test_tool_schema_3383_process_history_independence.py``, at all four
seams a canonical schema can escape by reference (``render_for_router``,
``build_tools``, ``describe_action``, the hot-list alias path), plus an AST
gate forbidding a new shallow-copy call site outside the projection helper.

Measured before removing the golden file here (not inferred): the file and its
fixture were deleted locally and the full suite run. Result: 9646 passed, 0
failed, 36 skipped — nothing else in the suite depended on the byte-identical
baseline comparison. Every property it covered (tool-set stability, exact
rendered-schema content, and the #3383 aliasing defect specifically) is either
inherently re-verified by the ordinary tests below (tool-set / registry
completeness) or already covered more strongly and directly by #3385's
per-seam assertions. So the fixture is removed, not regenerated — the
regenerate-on-drift instruction that used to live here was itself the hazard:
the fixture had already been regenerated ~7 times chasing schedule-dependent
failures before stage 1 (#3383's closing analysis) established the fixture
was right and the product was wrong.

Companion checks remaining:
  - liveness: every ``ToolDescription.tool_name`` in
    ``reyn.tools.descriptions.ALL`` resolves to a real registered tool.
  - completeness: every entry has non-empty surfaced/purpose/ja/text.
"""
from __future__ import annotations

from reyn.tools import get_default_registry
from reyn.tools.descriptions import ALL as ALL_DESCRIPTIONS


def test_description_registry_entries_resolve_to_real_tools() -> None:
    """Tier 1: every ToolDescription.tool_name in ALL is a registered tool.

    Catches a package entry left behind for a renamed/deleted tool (the
    inverse of the completeness check below).
    """
    registry = get_default_registry()
    registered_names = set(registry.names())
    for key, desc in ALL_DESCRIPTIONS.items():
        assert desc.tool_name in registered_names, (
            f"descriptions.ALL[{key!r}].tool_name={desc.tool_name!r} does not "
            "resolve to any registered ToolDefinition"
        )


def test_description_registry_entries_are_complete() -> None:
    """Tier 1: every ToolDescription has non-empty surfaced/purpose/text/ja.

    A blank review-aid field would silently defeat the "auditable in one
    place" purpose of the package.
    """
    for key, desc in ALL_DESCRIPTIONS.items():
        assert desc.tool_name.strip(), f"{key!r}: empty tool_name"
        assert desc.surfaced.strip(), f"{key!r}: empty surfaced"
        assert desc.purpose.strip(), f"{key!r}: empty purpose"
        assert desc.text.strip(), f"{key!r}: empty text"
        assert desc.ja.strip(), f"{key!r}: empty ja"


def test_reyn_repo_read_states_its_path_scope_and_the_alternative() -> None:
    """Tier 1: #3213 item 4 — ``reyn_repo_read``'s own text names its path scope.

    Live e2e (#3210) observed the model defaulting to ``reyn_repo_read`` (repo-root
    scoped) for a ``~/.reyn/...`` path, getting an out-of-scope failure, then
    fabricating file content instead of retrying with ``read_file``. The cheap,
    safe mitigation (tool-selection itself is out of scope for #3213) is for the
    tool's OWN description to name the boundary and the alternative — so a model
    that reads the description before guessing has the answer already, and one
    that fails anyway sees "use read_file" close to the error site.
    """
    from reyn.tools.descriptions.dev import reyn_repo_read

    text_lower = reyn_repo_read.text.lower()
    assert "read_file" in text_lower, (
        "reyn_repo_read's description does not name the alternative tool "
        "(read_file) for out-of-scope paths"
    )
    assert "home" in text_lower or "~/" in reyn_repo_read.text, (
        "reyn_repo_read's description does not call out paths under the "
        "user's home directory as out of scope"
    )
