"""Tier 2: tools/universal_dispatch.py arg-transformer pure helper contracts.

Each function maps (entry_name, args) → dict for a specific action category.
They are pure transformations with no side effects — the routing table in
universal_dispatch maps action names to these helpers at dispatch time.

#3026 deleted the ``_semantic_search_single_source_args`` / ``_mcp_tool_args``
(and ``_read_memory_body_args`` / ``_pipeline_run_args``) shapers along with the
resource categories they served, so their cases are removed here rather than
rewritten: the behaviour they pinned — currying a resource id out of the
qualified name — is exactly what #3026 removed. The equivalent capability is now
an ordinary argument on a verb, covered by
``tests/test_resource_collapse_invariant_3026.py``.
"""
from __future__ import annotations

from reyn.tools.universal_dispatch import (
    _multi_agent_delegate_args,
    _multi_agent_list_peers_args,
    _passthrough_args,
)

# ── _multi_agent_list_peers_args ──────────────────────────────────────────────


def test_multi_agent_list_peers_args_uses_cluster() -> None:
    """Tier 2: 'cluster' arg is mapped to the 'path' key."""
    result = _multi_agent_list_peers_args("list_peers", {"cluster": "writers"})
    assert result == {"path": "writers"}


def test_multi_agent_list_peers_args_uses_path_fallback() -> None:
    """Tier 2: 'path' is used when 'cluster' is absent."""
    result = _multi_agent_list_peers_args("list_peers", {"path": "reviewers"})
    assert result == {"path": "reviewers"}


def test_multi_agent_list_peers_args_defaults_to_empty_string() -> None:
    """Tier 2: no cluster or path arg → path defaults to '' (list all)."""
    result = _multi_agent_list_peers_args("list_peers", {})
    assert result == {"path": ""}


# ── _multi_agent_delegate_args ────────────────────────────────────────────────


def test_multi_agent_delegate_args_renames_message_to_request() -> None:
    """Tier 2: 'message' key is renamed to 'request' for legacy compat."""
    result = _multi_agent_delegate_args("delegate", {"to": "agent1", "message": "hello"})
    assert result == {"to": "agent1", "request": "hello"}


def test_multi_agent_delegate_args_keeps_request_key() -> None:
    """Tier 2: 'request' key passes through unchanged."""
    result = _multi_agent_delegate_args("delegate", {"to": "agent1", "request": "world"})
    assert result == {"to": "agent1", "request": "world"}


def test_multi_agent_delegate_args_other_keys_pass_through() -> None:
    """Tier 2: keys other than 'message' are carried to the output unchanged."""
    result = _multi_agent_delegate_args("delegate", {"to": "a", "request": "r", "extra": 1})
    assert result["extra"] == 1


# ── _passthrough_args ─────────────────────────────────────────────────────────


def test_passthrough_args_returns_copy_of_args() -> None:
    """Tier 2: args are returned as-is (identity transform)."""
    original = {"a": 1, "b": "two"}
    result = _passthrough_args("anything", original)
    assert result == original


def test_passthrough_args_does_not_mutate_input() -> None:
    """Tier 2: the original args mapping is not mutated."""
    original = {"key": "val"}
    result = _passthrough_args("x", original)
    result["new_key"] = "added"
    assert "new_key" not in original
