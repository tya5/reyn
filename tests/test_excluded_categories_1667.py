"""Tier 2: #1667 — explicit per-session category exclusion at the catalog SOURCE.

``reyn_source`` (Reyn's own self-help read/list/glob/grep surface) is default-IN
for the general/interactive agent, but a foot-gun in external-repo task contexts
(SWE-bench/eval on /testbed): the weak model misselects ``reyn_source__grep`` over
``file__*`` and searches Reyn's source instead of the repo. The fix is an EXPLICIT
opt-out: the task-agent path sets ``excluded_categories={"reyn_source"}``, threaded
to ``RouterCallerState`` and applied at ``_enumerate_category`` — the single
catalog source. Because ``catalog_entries`` / ``list_actions`` / ``describe`` /
dispatch all derive from ``_enumerate_category``, the category vanishes UNIFORMLY
from every scheme (codeact / enumerate / retrieval) — which a top-level
``exclude_tools`` name filter cannot reach (reyn_source ops are never top-level
tools). The interactive agent leaves it empty and keeps reyn_source (self-help
preserved). Orthogonal to ``exclude_tools`` (a separate param, by design).

Real ToolContext + RouterCallerState (no mocks of collaborators).
"""
from __future__ import annotations

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _enumerate_category, catalog_entries


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


def _ctx(excluded: "frozenset[str] | set[str]" = frozenset()) -> ToolContext:
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            available_skills=[],
            mcp_servers=None,
            excluded_categories=frozenset(excluded),
        ),
    )


def test_excluded_category_dropped_from_catalog_entries() -> None:
    """Tier 2: #1667 — with excluded_categories={"reyn_source"}, NO reyn_source__*
    action appears in catalog_entries (every scheme's flat list), while file__*
    survives. This is the misselection-disappears proof."""
    names = {e["name"] for e in catalog_entries(_ctx(excluded={"reyn_source"}))}
    assert not any(n.startswith("reyn_source__") for n in names), (
        "excluded reyn_source category must vanish from catalog_entries"
    )
    assert any(n.startswith("file__") for n in names), (
        "non-excluded categories (file) must remain"
    )


def test_unset_keeps_reyn_source() -> None:
    """Tier 2: #1667 — the interactive default (empty excluded_categories) KEEPS
    reyn_source (self-help capability preserved — the owner's default-IN). This is
    the don't-kill-the-feature proof."""
    names = {e["name"] for e in catalog_entries(_ctx())}
    assert any(n.startswith("reyn_source__") for n in names), (
        "with no exclusion, reyn_source must remain (interactive self-help)"
    )


def test_enumerate_category_skip_is_at_the_source() -> None:
    """Tier 2: #1667 — the skip is at _enumerate_category (the single source feeding
    catalog_entries + list_actions + describe + dispatch), so an excluded category
    enumerates empty while a sibling static category does not."""
    ctx = _ctx(excluded={"reyn_source"})
    assert _enumerate_category("reyn_source", ctx) == []
    assert _enumerate_category("file", ctx), "a non-excluded static category still enumerates"


def test_other_category_exclusion_is_generic() -> None:
    """Tier 2: #1667 — the mechanism is generic (P7: the excluded set is caller
    data, not a hardcoded reyn_source). Excluding "web" drops web__* and leaves
    reyn_source__*."""
    names = {e["name"] for e in catalog_entries(_ctx(excluded={"web"}))}
    assert not any(n.startswith("web__") for n in names)
    assert any(n.startswith("reyn_source__") for n in names)
