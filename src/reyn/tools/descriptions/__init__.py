"""Reviewable package of reyn's tool-facing LLM description strings.

Each ``ToolDefinition.description`` string that used to live inline in its
tool module is (category by category) relocated here as a ``ToolDescription``
record — the exact LLM-facing ``text`` plus review-aid metadata (``surfaced``,
``purpose``, ``ja``) that a reviewer can audit in one place instead of
grepping across ``src/reyn/tools/*.py``.

Phase 1 covers the ``discovery`` category (see ``descriptions.discovery``).
Later phases add the remaining categories; each origin tool module keeps a
``_X_DESCRIPTION = descriptions.<category>.<name>.text`` alias so no call
site changes — this package is purely a relocation of the string literal,
never a behavior change.

``ALL`` aggregates every category's descriptions into one
``dict[str, ToolDescription]`` keyed by a package-unique entry name (NOT
always the bare tool name — e.g. ``semantic_search_hide_legacy`` shares
``tool_name="semantic_search"`` with the ``semantic_search`` entry, since it
is an alternate, currently-unwired description variant for that same tool).
"""
from __future__ import annotations

from reyn.tools.descriptions import discovery
from reyn.tools.descriptions._types import ToolDescription

ALL: dict[str, ToolDescription] = {
    **discovery.ALL,
}

__all__ = [
    "ToolDescription",
    "discovery",
    "ALL",
]
