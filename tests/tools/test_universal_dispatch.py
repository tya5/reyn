"""Tier 2: universal_dispatch — the catalog's action-membership contract.

Tests for ``src/reyn/tools/universal_dispatch.py`` covering:
  1. membership (``is_known_action`` / ``require_known_action``) — the check
     the catalog wrappers resolve an ``action_name`` against.
  2. ``UnknownActionError`` carrying ``action_name`` / ``reason`` /
     ``suggestions`` per §D12.
  3. ``suggest_similar_names`` ranking via difflib (= deterministic, no LLM,
     no embeddings).
  4. the category ↔ action inventory (``action_names_for_category`` /
     ``category_of`` / ``KNOWN_ACTION_NAMES``).
  5. the per-action schema contract: every action is a registered tool, and a
     representative caller-args payload covers that tool's required schema.

#3429 shrank this file by removing what it can no longer test. Until then this
module was a name→name ROUTING table: an action was addressed by a
``<category>__<verb>`` spelling that resolved to a flat registry name, with a
per-action arg transformer in between. Roughly half the file pinned that
mapping — ``resolve_invoke_action("file__read") == "read_file"`` and the two
surviving transformers — and those tests are deleted rather than adapted,
because "X routes to X" is not a claim. What survives is everything that was
about the ACTION rather than about the second name, plus §5, which was the
file's real regression guard (the PR #246 key mismatch) and is strictly easier
to state now: the sample's keys are the handler's keys, with nothing in between.

No mocks. No private-state assertions.
"""

from __future__ import annotations

from typing import Any

import pytest

from reyn.tools import get_default_registry
from reyn.tools.universal_catalog import CATEGORIES
from reyn.tools.universal_dispatch import (
    KNOWN_ACTION_NAMES,
    KNOWN_ACTION_NAMES_SORTED,
    UnknownActionError,
    action_names_for_category,
    category_of,
    is_known_action,
    require_known_action,
    suggest_similar_names,
)

# ── 1. Membership ────────────────────────────────────────────────────────


def test_a_registered_catalog_action_is_known() -> None:
    """Tier 2: a name in the membership table passes and is returned as-is."""
    assert is_known_action("read_file")
    assert require_known_action("read_file") == "read_file"


def test_a_registered_tool_outside_the_catalog_is_not_an_action() -> None:
    """Tier 2: membership is the CATALOG's set, not the registry's.

    ``present`` is a live registered tool that the catalog does not browse, so
    ``invoke_action`` must not accept it — the wrappers are the catalog's
    dispatch surface, and a tool outside the catalog is reached directly."""
    assert get_default_registry().lookup("present") is not None
    assert not is_known_action("present")


def test_the_wrappers_are_not_actions() -> None:
    """Tier 2: ``invoke_action`` cannot invoke itself (nor its siblings)."""
    for wrapper in ("list_actions", "search_actions", "describe_action", "invoke_action"):
        assert not is_known_action(wrapper)


def test_no_action_name_carries_the_catalog_separator() -> None:
    """Tier 2: #3429 — the abolished spelling is absent from the action set.

    The repo-wide, registry-derived form of this gate is
    ``tests/tools/test_no_qualified_tool_names_3429.py``; this is the module-local
    arm, so a table edit fails here first."""
    offenders = sorted(n for n in KNOWN_ACTION_NAMES if "__" in n)
    assert offenders == [], f"qualified action name(s) reintroduced: {offenders}"


# ── 2. UnknownActionError (§D12) ─────────────────────────────────────────


def test_unknown_action_raises_with_name_and_reason() -> None:
    """Tier 2: the error carries the offending name and a reason."""
    with pytest.raises(UnknownActionError) as exc_info:
        require_known_action("not_a_real_action")
    assert exc_info.value.action_name == "not_a_real_action"
    assert exc_info.value.reason


def test_unknown_action_error_carries_suggestions() -> None:
    """Tier 2: a near-miss name yields §D12 recovery suggestions."""
    with pytest.raises(UnknownActionError) as exc_info:
        require_known_action("read_fil")
    assert "read_file" in exc_info.value.suggestions


def test_unknown_action_error_message_includes_suggestions() -> None:
    """Tier 2: the suggestions are in the message the LLM reads, not only in
    the attribute a caller would have to know to unpack."""
    with pytest.raises(UnknownActionError) as exc_info:
        require_known_action("web_serch")
    assert "web_search" in str(exc_info.value)


# ── 3. suggest_similar_names (D12 suggestion engine) ─────────────────────


def test_suggest_similar_names_finds_close_match() -> None:
    """Tier 2: typo near a known name returns the correct suggestion."""
    assert "read_file" in suggest_similar_names("reed_file")


def test_suggest_similar_names_returns_empty_when_no_match() -> None:
    """Tier 2: completely unrelated input returns no suggestions.

    Uses an underscore-free string: difflib similarity keys partly on the
    ``_`` characters shared by action names, so an unrelated input that happens
    to contain underscores can spuriously clear the cutoff."""
    assert suggest_similar_names("xyzqwertycompletelyunrelatedstring123") == []


def test_suggest_similar_names_respects_top_k() -> None:
    """Tier 2: top_k caps the suggestion count."""
    assert suggest_similar_names("read_file", top_k=1) == ["read_file"]


def test_suggest_similar_names_custom_candidates() -> None:
    """Tier 2: caller-supplied candidates override the static catalogue.

    The catalog handlers pass an availability-aware pool this way, so a
    category the caller excluded is never suggested."""
    candidates = ["alpha_thing", "beta_thing", "gamma_thing"]
    assert "alpha_thing" in suggest_similar_names("alfa_thing", candidates=candidates)


def test_suggest_similar_names_empty_candidates_returns_empty() -> None:
    """Tier 2: empty candidate list returns empty result."""
    assert suggest_similar_names("read_file", candidates=[]) == []


# ── 4. Category ↔ action inventory ───────────────────────────────────────


def test_sorted_inventory_is_sorted_and_deduped() -> None:
    """Tier 2: the sorted view is stable and has no duplicates."""
    names = KNOWN_ACTION_NAMES_SORTED
    assert list(names) == sorted(names)
    assert len(set(names)) == len(names)


def test_every_category_offers_at_least_one_action() -> None:
    """Tier 2: a category in ``CATEGORIES`` with no members is a category the
    LLM can filter by and always get nothing from — the #2032 bug class."""
    empty = [c for c in CATEGORIES if not action_names_for_category(c)]
    assert empty == [], f"categories with no actions: {empty}"


def test_action_names_for_category_file() -> None:
    """Tier 2: the file category's §D20 verb set."""
    assert set(action_names_for_category("file")) == {
        "read_file", "write_file", "delete_file", "list_directory",
        "grep_files", "glob_files", "edit_file",
    }


def test_action_names_for_category_memory_operation() -> None:
    """Tier 2: #3026 — the memory category's full verb set: write
    (remember/forget) PLUS the read+list halves that replaced the per-entry
    actions."""
    assert set(action_names_for_category("memory_operation")) == {
        "remember_shared", "remember_agent", "forget_memory",
        "list_memory", "read_memory_body",
    }


def test_action_names_for_category_mcp() -> None:
    """Tier 2: #879 collapsed surface + the 2026-05-25 install 3-verb split."""
    assert set(action_names_for_category("mcp")) == {
        "mcp_search_registry", "mcp_install_registry", "mcp_install_package",
        "mcp_install_local", "list_mcp_servers", "list_mcp_tools",
        "mcp_call_tool", "mcp_drop_server",
    }


def test_action_names_for_category_exec_is_single_entry() -> None:
    """Tier 2: exec is a single-entry category. #4932 (2026-08-19): the
    former D14-ext visibility gate (hide when sandbox_backend is
    None/noop) is retired — exec always enumerates now; what the
    enumeration layer still derives from sandbox_backend is an
    isolation-disclosure text suffix, not a hide/show decision."""
    assert action_names_for_category("exec") == ("exec",)


def test_action_names_for_a_collapsed_category_raises() -> None:
    """Tier 2: #3026 — a collapsed category is not a category at all any more;
    asking for it is a programming error, not an empty result."""
    with pytest.raises(ValueError, match="unknown category"):
        action_names_for_category("memory_entry")


def test_action_names_for_unknown_category_raises() -> None:
    """Tier 2: invalid category to the introspection helper raises."""
    with pytest.raises(ValueError, match="unknown category"):
        action_names_for_category("not_a_category")


def test_category_of_round_trips_with_the_membership_table() -> None:
    """Tier 2: every action's ``category_of`` names a category that lists it —
    the two views cannot disagree."""
    for name in KNOWN_ACTION_NAMES_SORTED:
        cat = category_of(name)
        assert cat is not None, f"{name!r} has no category"
        assert name in action_names_for_category(cat)


def test_category_of_a_non_action_is_none() -> None:
    """Tier 2: a registered tool outside the catalog has no category here."""
    assert category_of("present") is None


# ── 5. Per-action schema contract (regression guard) ─────────────────────
#
# The mcp.tool routing regression (PR #246) escaped because the resolver
# emitted ``tool`` while the target handler read ``mcp_tool_name``, and the
# existing test happened to PIN the buggy shape. #3429 removed the resolver, so
# a key mismatch of that exact shape can no longer be introduced — but the
# contract this section generalised is still worth pinning, per action:
#
#   (a) the action exists in get_default_registry() (catches rename / removal
#       of a tool without a membership-table update),
#   (b) a representative caller-args payload covers the tool's required schema
#       (catches an action whose advertised requirements the canonical LLM
#       invocation shape does not satisfy).
#
# Adding an action without a sample here fails the coverage test below, forcing
# the author to declare an explicit contract for it.

_ACTION_CONTRACT_SAMPLES: list[tuple[str, dict[str, Any]]] = [
    # multi_agent
    # #3429: ``path`` is declared REQUIRED by ``list_agents``' schema and the
    # sample now says so. It used to be ``{}`` because the qualified spelling's
    # transform supplied a default the schema never advertised — the same
    # undeclared-capability shape as the ``cluster`` alias it also accepted.
    # (The zero-arg "list all clusters" behaviour is unaffected: the handler
    # itself defaults an absent ``path`` to ``""``.)
    ("list_agents", {"path": ""}),
    ("describe_agent", {"name": "planner"}),
    # #3896 (owner ruling, option 1): spawn_session gained a catalog route so
    # exclusive-wrapper mode doesn't lose the capability entirely — see
    # llm_reachability.py's "Exclusive-wrapper mode" section.
    ("spawn_session", {"request": "investigate the failing test"}),
    # delegate_to_agent retired, proposal 0067 P6 (#3978) — run_prompt/
    # send_to_session are router-only tools (not invoke_action/catalog
    # actions), so they need no contract sample here.
    # #3026: the verbs that replaced the memory_entry / rag_corpus resource
    # actions. ``read`` uses a NON-DEFAULT layer so a regression to the old
    # hard-coded ``shared`` fails the contract here.
    ("list_memory", {"path": ""}),
    ("read_memory_body", {"layer": "agent", "slug": "pref_dates"}),
    ("pipeline_list", {}),
    # #3429: there is no transform layer, so a sample's keys ARE the keys the
    # handler receives; each must already cover the target's required schema.
    # Issue #879 collapsed mcp surface + 2026-05-25 install 3-verb split.
    ("mcp_search_registry",  {"text": "github related"}),
    ("mcp_install_registry", {"server_id": "io.github.org/mcp-foo"}),
    ("mcp_install_package",  {"kind": "pypi", "identifier": "mcp-server-time"}),
    ("mcp_install_local",    {"name": "weather", "command": "python",
                                "args": ["/tmp/weather_mcp.py"]}),
    ("list_mcp_servers",     {}),
    ("list_mcp_tools",       {"server": "brave"}),
    # ``tool`` is the MCP server's own <server>__<tool> identifier — an
    # ARGUMENT VALUE in a namespace reyn does not own, not a reyn tool name.
    ("mcp_call_tool",        {"tool": "brave__search", "tool_args": {"q": "reyn"}}),
    ("mcp_drop_server",      {"server": "brave"}),
    ("read_file",   {"path": "a.txt"}),
    ("write_file",  {"path": "a.txt", "content": "x"}),
    ("delete_file", {"path": "a.txt"}),
    ("list_directory",   {"path": "."}),
    ("grep_files",   {"pattern": "x"}),
    ("glob_files",   {"pattern": "*.py"}),
    ("edit_file",   {"path": "a", "old_string": "b", "new_string": "c"}),
    ("web_search",  {"query": "x"}),
    ("web_fetch",   {"url": "https://x"}),
    ("remember_shared",
     {"slug": "s", "name": "n", "description": "d", "type": "user", "body": "b"}),
    ("remember_agent",
     {"slug": "s", "name": "n", "description": "d", "type": "user", "body": "b"}),
    ("forget_memory", {"layer": "shared", "slug": "s"}),
    ("reyn_repo_read", {"path": "a"}),
    ("reyn_repo_list", {"path": "."}),
    ("reyn_repo_glob", {"pattern": "*.py"}),
    ("reyn_repo_grep", {"pattern": "x"}),
    # FP-0066 P1b: rag_operation__* rows retired along with the category.
    ("exec",       {"argv": ["echo", "hi"]}),
    # skill_management category (#2548 PR-C) — install_local requires path.
    ("skill_install_local",  {"path": "/tmp/my-skill"}),
    # skill_management category (#2548 PR-D) — install_source requires source URL.
    ("skill_install_source", {"source": "https://github.com/user/skill-repo"}),
    # skill_management category (#2971) — list takes no args (the result is
    # already scoped to the session's visible set).
    ("skill_list",           {}),
    # skill_management category (FP-0066 P0, #3247) — load requires the
    # skill's SKILL.md path.
    ("load_skill",           {"path": "/tmp/my-skill/SKILL.md"}),
    # pipeline_management category — install_local requires path.
    ("pipeline_install_local",  {"path": "/tmp/my-pipeline.yaml"}),
    # pipeline_management category — install_source requires source URL.
    ("pipeline_install_source", {"source": "https://github.com/user/pipeline-repo"}),
    # presentation_management category (proposal 0060 Phase 1 Layer A / A8) —
    # install requires name + blueprint (no source/git-fetch counterpart).
    ("presentation_install_local",
     {"name": "status_card", "blueprint": {"component": "text", "text": "hi"}}),
    # pipeline category — unified launch verb (proposal 0067 P7, #3978: 4
    # names -> 1). exactly one of name=/definition= (validated in the
    # handler, not the schema, so ``required`` names neither); input and
    # collect are both optional (collect defaults to "attached"). This
    # sample uses the REGISTERED (name=) form — IS-4's ad-hoc definition=
    # form is covered by its own dedicated tests, not the catalog contract
    # pin (one sample per action name here, not one per param combination).
    ("run_pipeline", {"name": "my_pipeline", "input": {"topic": "x"}}),
    # task category (proposal 0067 P4, #3978) — describe/cancel require the
    # task's handle; list_tasks takes an optional kind filter.
    ("describe_task", {"task_id": "pipeline-my_pipeline-abc123"}),
    ("list_tasks", {}),
    ("cancel_task", {"task_id": "pipeline-my_pipeline-abc123"}),
    # plugin_management category (ADR 0064 P2, #3083) — install requires
    # source (a {kind, ...} object); uninstall requires name.
    ("install_plugin",
     {"source": {"kind": "builtin", "name": "rag"}}),
    ("uninstall_plugin", {"name": "rag"}),
    # plugin_management category (#3202 symptom 3) — list takes no args
    # (mirrors skill_list above: the result is the whole
    # BUILTIN_PLUGINS-advertised set, nothing here for the caller to filter by).
    ("list_plugins", {}),
    # knowledge category (FP-0066 P3c, #3247 firm §3/§5) — search requires
    # a non-empty `query` string (search_knowledge's sole required param;
    # `limit` is optional). Mirrors mcp_search_registry's shape above
    # (a single required free-text query key).
    ("search_knowledge", {"query": "widgets"}),
    # embedding category (#3465, FP-0057 Phase 1) — embed requires a
    # non-empty `texts` array; `embedding_model` is optional (default
    # "standard").
    ("embed", {"texts": ["widgets"]}),
    # hooks category (#3465) — emit_hook_event requires `event_name`;
    # `payload` is optional.
    ("emit_hook_event", {"event_name": "deploy_ready"}),
    # hooks category (#3465) — hooks_add requires `on` + `message`.
    ("hooks_add", {"on": "turn_end", "message": "hi"}),
]


@pytest.mark.parametrize("action_name,caller_args", _ACTION_CONTRACT_SAMPLES)
def test_action_is_registered_and_sample_args_cover_required_schema(
    action_name: str, caller_args: dict[str, Any],
) -> None:
    """Tier 2: every action is a live tool whose required schema a canonical
    caller payload satisfies.

    ``caller_args`` reaches the handler unchanged (#3429), so this is a direct
    statement about what the model must send. Samples reflect what the catalog
    wrappers instruct the LLM to supply; they are NOT exhaustive coverage of
    every arg shape, they pin the canonical one so a drift on either side fails
    here rather than as a KeyError in production.
    """
    target = get_default_registry().lookup(action_name)
    assert target is not None, (
        f"action {action_name!r} is in the membership table but not in the "
        f"registry"
    )
    required = set(target.parameters.get("required", []))
    produced = set(caller_args.keys())
    missing = required - produced
    assert not missing, (
        f"canonical caller args for {action_name!r} are {sorted(produced)} "
        f"but the tool requires {sorted(required)}; missing: {sorted(missing)}"
    )


def test_contract_samples_cover_every_action() -> None:
    """Tier 2: every action has a contract sample.

    Adding an action without a sample would silently bypass the contract pin
    above. This fails the moment a new action is introduced without an explicit
    sample declaration."""
    sample_names = {name for name, _ in _ACTION_CONTRACT_SAMPLES}
    missing = KNOWN_ACTION_NAMES - sample_names
    assert not missing, f"actions without a contract sample: {sorted(missing)}"
    extra = sample_names - KNOWN_ACTION_NAMES
    assert not extra, f"contract samples for names that are not actions: {sorted(extra)}"
