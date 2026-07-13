"""Reviewable package of reyn's tool-facing LLM description strings.

Each ``ToolDefinition.description`` string that used to live inline in its
tool module is (category by category) relocated here as a ``ToolDescription``
record — the exact LLM-facing ``text`` plus review-aid metadata (``surfaced``,
``purpose``, ``ja``) that a reviewer can audit in one place instead of
grepping across ``src/reyn/tools/*.py``.

Phase 1 covered the ``discovery`` category (see ``descriptions.discovery``).
Phase 2 added ``io``, ``memory``, ``mcp``, ``execution``, and ``delegation``
— the bulk categories, mechanical repeats of the Phase 1 pattern. Phase 3
completes tool-LEVEL relocation with the remaining feature-area buckets:
``cron``, ``hooks``, ``presentation``, ``catalog`` (peer-agent browse +
``invoke_action``), ``interactive``, ``context``, ``pipeline`` (launch
verbs), ``pipeline_management`` (install verbs), ``skill`` (install
verbs), ``dev`` (``reyn_repo_*``), and ``task`` (the 12 ``task.*``
control-IR ops — the one module lifted out of a data-tuple rather than
``_X_DESCRIPTION`` constants; see ``descriptions.task``'s docstring).
Each origin tool module keeps a
``_X_DESCRIPTION = descriptions.<bucket>.<name>.text`` alias so no call
site changes — this package is purely a relocation of the string literal,
never a behavior change. Module grouping is CONCEPTUAL (feature area),
not always a literal mirror of ``ToolDefinition.category`` — e.g.
``catalog`` holds ``invoke_action`` (``category="invocation"``) alongside
``list_agents`` / ``describe_agent`` (``category="discovery"``), matching
the ``mcp`` module precedent from Phase 2 (mixed ``category`` values,
grouped by feature).

``ALL`` aggregates every bucket's descriptions into one
``dict[str, ToolDescription]`` keyed by a package-unique entry name (NOT
always the bare tool name — e.g. ``semantic_search_hide_legacy`` shares
``tool_name="semantic_search"`` with the ``semantic_search`` entry, since it
is an alternate, currently-unwired description variant for that same tool;
the 12 ``task`` entries are keyed by their op_kind, e.g. ``"task.create"``).
"""
from __future__ import annotations

from reyn.tools.descriptions import (
    catalog,
    context,
    cron,
    delegation,
    dev,
    discovery,
    execution,
    hooks,
    interactive,
    io,
    mcp,
    memory,
    pipeline,
    pipeline_management,
    presentation,
    skill,
    task,
)
from reyn.tools.descriptions._types import ParamDescription, ToolDescription

ALL: dict[str, ToolDescription] = {
    **discovery.ALL,
    **io.ALL,
    **memory.ALL,
    **mcp.ALL,
    **execution.ALL,
    **delegation.ALL,
    **cron.ALL,
    **hooks.ALL,
    **presentation.ALL,
    **catalog.ALL,
    **interactive.ALL,
    **context.ALL,
    **pipeline.ALL,
    **pipeline_management.ALL,
    **skill.ALL,
    **dev.ALL,
    **task.ALL,
}

# Phase 4 (param-level relocation): each bucket that has at least one
# per-parameter description exposes a ``PARAMS: dict[str, dict[str,
# ParamDescription]]`` — keyed by the SAME entry name as that bucket's
# ``ALL`` (tool_name for nearly every bucket; the ``task`` bucket has no
# PARAMS module yet — its 12 op-kind entries carry no per-param text in
# their origin schemas). ``interactive`` (ask_user) also has none. Buckets
# without a PARAMS module are simply omitted from this merge.
ALL_PARAMS: dict[str, dict[str, ParamDescription]] = {
    **discovery.PARAMS,
    **io.PARAMS,
    **memory.PARAMS,
    **mcp.PARAMS,
    **execution.PARAMS,
    **delegation.PARAMS,
    **cron.PARAMS,
    **hooks.PARAMS,
    **presentation.PARAMS,
    **catalog.PARAMS,
    **context.PARAMS,
    **pipeline.PARAMS,
    **pipeline_management.PARAMS,
    **skill.PARAMS,
    **dev.PARAMS,
}

__all__ = [
    "ParamDescription",
    "ToolDescription",
    "discovery",
    "io",
    "memory",
    "mcp",
    "execution",
    "delegation",
    "cron",
    "hooks",
    "presentation",
    "catalog",
    "interactive",
    "context",
    "pipeline",
    "pipeline_management",
    "skill",
    "dev",
    "task",
    "ALL",
    "ALL_PARAMS",
]
