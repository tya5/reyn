"""Tier 1: #2681 Bucket B — the 24 genuinely-structured record-read producers (+ topology_create,
punted here from Bucket C's sweep) get REAL structured mappers (a short bounded ``text`` summary +
the record(s) as a ``structured`` attachment), NOT the ``CANONICAL_TODO`` whole-dict fallback.

Owner Decision #1 restricts ``STRUCTURED_PASSTHROUGH`` to the admin-6 (external/protocol payloads
needing verbatim structure); these producers are internal record-reads instead. Real ``to_canonical``
dispatch throughout — no mocks. Spot-checks span memory / mcp / task / cron / catalog / topology, plus
an ``invoke_action`` delegation-vs-fallback pair, a falsify pin (the mapped result must differ from
the naive whole-dict fallback), and a full callable-membership sweep of all 25 producers. The
set-EQUALITY ratchet-ledger gate (``CANONICAL_TODO`` / ``STRUCTURED_PASSTHROUGH`` membership) lives in
``test_fp0056_canonical_coverage_gate.py``; this file pins THIS PR's producer-level behavior.
"""
from __future__ import annotations

import reyn.core.op_runtime as op_runtime  # noqa: F401 — import triggers op-kind self-registration
from reyn.core.offload.canonical import (
    CANONICAL_TODO,
    STRUCTURED_PASSTHROUGH,
    _fallback_structured,
    canonical_declaration,
    extract_canonical_source,
    to_canonical,
)
from reyn.tools import get_default_registry

# Importing the registry forces every router ToolDefinition module to import (and thus
# self-register its canonical declaration) — the same registration-trigger idiom
# test_fp0056_canonical_coverage_gate.py uses.
get_default_registry()


def test_list_memory_summarizes_bounded_text_and_carries_full_records() -> None:
    """Tier 1: list_memory's dispatch-envelope-wrapped bare list (``{"status": "ok", "data": [...]}``
    — the envelope survives because the handler's own return is a non-dict list, so
    ``unwrap_dispatch_envelope`` cannot peel it; verified against the real dispatch chain) canonicalizes
    to a bounded text summary + the full record list as a structured attachment — NOT the whole-dict
    CANONICAL_TODO fallback (whose text is always empty)."""
    records = [{"path": "shared", "count": 3}, {"path": "agent", "count": 1}]
    result = {"status": "ok", "data": records}
    canonical = to_canonical(result, source="list_memory")
    assert canonical["text"] == "2 memory entries."
    assert canonical["attachments"] == [{"kind": "structured", "data": records}]
    assert callable(canonical_declaration("list_memory"))


def test_list_mcp_servers_summarizes_bounded_text_and_carries_full_records() -> None:
    """Tier 1: list_mcp_servers (mcp.py) canonicalizes its ``servers`` list to a bounded, named text
    preview + the full list as structured."""
    servers = [{"name": "acme", "description": "x"}, {"name": "globex", "description": "y"}]
    result = {"servers": servers}
    canonical = to_canonical(result, source="list_mcp_servers")
    assert canonical["text"] == "2 MCP servers: acme, globex."
    assert canonical["attachments"] == [{"kind": "structured", "data": servers}]


def test_task_list_summarizes_bounded_text_and_carries_full_records() -> None:
    """Tier 1: task.list (dual-registered in op_runtime/task.py + tools/task_ops.py) canonicalizes
    its plural ``tasks`` list via the shared ``task_op_to_canonical`` (the list-shaped discriminator
    branch)."""
    tasks = [{"task_id": "t1", "status": "ready"}, {"task_id": "t2", "status": "blocked"}]
    result = {"kind": "task.list", "status": "ok", "tasks": tasks}
    canonical = to_canonical(result, source="task.list")
    assert canonical["text"] == "2 tasks."
    assert canonical["attachments"] == [{"kind": "structured", "data": tasks}]


def test_task_get_summarizes_the_singular_task_record() -> None:
    """Tier 1: task.get (the singular ``{"task": <dict>}`` shape shared by 8 of the 9 migrated task
    ops) names the task id + status; the full ``Task.to_dict()`` record rides in the attachment."""
    task = {"task_id": "t1", "status": "running", "name": "do the thing"}
    result = {"kind": "task.get", "status": "ok", "task": task}
    canonical = to_canonical(result, source="task.get")
    assert canonical["text"] == "task t1: running."
    assert canonical["attachments"] == [{"kind": "structured", "data": task}]


def test_cron_list_summarizes_bounded_text_and_carries_full_records() -> None:
    """Tier 1: cron_list names the job count + source + a bounded name preview."""
    jobs = [{"name": "daily_report"}, {"name": "hourly_ping"}]
    result = {"status": "ok", "source": "live_scheduler", "jobs": jobs}
    canonical = to_canonical(result, source="cron_list")
    assert canonical["text"] == "2 cron jobs (live_scheduler): daily_report, hourly_ping."
    assert canonical["attachments"] == [{"kind": "structured", "data": jobs}]


def test_describe_agent_summarizes_the_single_record() -> None:
    """Tier 1: describe_agent (catalog.py) names the agent + role for its single-record view."""
    agent = {"name": "sb2", "role": "dogfood-coder", "cluster": "reyn"}
    canonical = to_canonical(agent, source="describe_agent")
    assert canonical["text"] == "agent sb2: dogfood-coder."
    assert canonical["attachments"] == [{"kind": "structured", "data": agent}]


def test_topology_create_summarizes_the_created_record() -> None:
    """Tier 1: topology_create (punted here from Bucket C's sweep — success echoes the FULL created
    config, a genuine record, not a status ack) names the topology + kind + member count."""
    result = {
        "status": "created", "name": "org1", "kind": "team",
        "members": ["a", "b"], "leader": "a", "profiles": {},
    }
    canonical = to_canonical(result, source="topology_create")
    assert canonical["text"] == "topology org1 (team): 2 members."
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]


def test_invoke_action_delegates_to_the_target_mapper_when_target_is_dict_shaped() -> None:
    """Tier 1: invoke_action's OWN declaration is a defensive fallback — when the delegated target's
    handler returns a dict (the common case), canonicalization dispatches through the TARGET's own
    mapper via the ``_canonical_source`` tag ``_handle_invoke_action`` injects, not invoke_action's
    own declaration."""
    target_result = {"name": "sb2", "role": "dogfood-coder", "_canonical_source": "describe_agent"}
    source, cleaned = extract_canonical_source(target_result)
    assert source == "describe_agent"
    canonical = to_canonical(cleaned, source=source)
    assert canonical["text"] == "agent sb2: dogfood-coder."


def test_invoke_action_own_mapper_covers_the_non_dict_delegated_target_case() -> None:
    """Tier 1: invoke_action's OWN mapper fires when the delegated target's handler returns a
    NON-DICT value (e.g. list_memory / list_agents invoked via invoke_action) — the tag-injection
    guard (``isinstance(result, dict)``) skips a non-dict return, so the OUTER invoke_action tag
    survives and this mapper receives the still-wrapped dispatch envelope, same shape
    ``memory_list_to_canonical`` sees directly."""
    records = [{"path": "shared", "count": 3}]
    result = {"status": "ok", "data": records}
    canonical = to_canonical(result, source="invoke_action")
    assert canonical["text"] == "invoke_action: 1 record(s)."
    assert canonical["attachments"] == [{"kind": "structured", "data": records}]


def test_falsify_mapped_result_differs_from_the_naive_whole_dict_fallback() -> None:
    """Tier 1: falsify — the new mapper's output must differ from what the OLD ``CANONICAL_TODO``
    whole-dict fallback would have produced (``_fallback_structured``: empty text, the whole result as
    ONE structured blob). If list_memory's declaration ever regressed back to ``CANONICAL_TODO``, the
    final assertion here goes RED (``real == naive``) — a real behavioral diff, not an assumption.
    (Manually cp-verified too: reverting the ``LIST_MEMORY`` declaration to ``CANONICAL_TODO`` and
    re-running this module does turn this assertion RED — see the PR description.)"""
    result = {"status": "ok", "data": [{"path": "shared", "count": 3}]}
    naive = _fallback_structured(result)
    real = to_canonical(result, source="list_memory")
    assert naive["text"] == "" and naive["attachments"] == [{"kind": "structured", "data": result}]
    assert real["text"] != ""
    assert real != naive


def test_all_bucket_b_producers_are_real_callables_not_todo_or_passthrough() -> None:
    """Tier 1: every #2681 Bucket B producer (24 + topology_create) resolves to a real callable
    mapper — never ``CANONICAL_TODO`` (the ratcheted debt marker this burn-down removes) and never
    ``STRUCTURED_PASSTHROUGH`` (the admin-6 opt-in this PR does not touch, owner decision #1). A
    regression back to either sentinel fails this loop by name."""
    bucket_b = [
        "list_memory",
        "list_actions", "search_actions", "describe_action", "invoke_action",
        "list_agents", "describe_agent",
        "describe_mcp_tool", "list_mcp_servers", "list_mcp_tools",
        "list_mcp_resources", "list_mcp_resource_templates", "list_mcp_prompts",
        "mcp_search_registry",
        "cron_list",
        "topology_create",
        "task.create", "task.update_status", "task.get", "task.list",
        "task.add_dependency", "task.remove_dependency", "task.repoint_dependency",
        "task.abort", "task.assign",
    ]
    for sid in bucket_b:
        decl = canonical_declaration(sid)
        assert callable(decl), f"{sid!r} is not a real mapper: {decl!r}"
        assert decl is not CANONICAL_TODO, f"{sid!r} regressed back to CANONICAL_TODO"
        assert decl is not STRUCTURED_PASSTHROUGH, f"{sid!r} must not be STRUCTURED_PASSTHROUGH"


def test_admin_6_structured_passthrough_is_untouched_by_this_pr() -> None:
    """Tier 1: this PR does not touch the admin-6 ``STRUCTURED_PASSTHROUGH`` set (owner decision #1)
    — re-pinned here as a local sanity check alongside the Bucket B producers above. The canonical
    membership-EQUALITY gate (no more, no less than these 6) lives in
    ``test_fp0056_canonical_coverage_gate.py::test_structured_passthrough_membership_is_exactly_the_admin_6``."""
    admin_6 = (
        "mcp_install", "mcp_drop_server", "skill_install", "pipeline_install",
        "mcp_subscribe_resource", "mcp_unsubscribe_resource",
    )
    for sid in admin_6:
        assert canonical_declaration(sid) is STRUCTURED_PASSTHROUGH
