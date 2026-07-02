"""Tier 2: tools/universal_dispatch.py arg-transformer pure helper contracts.

Each function maps (entry_name, args) → dict for a specific action category.
They are pure transformations with no side effects — the routing table in
universal_dispatch maps action names to these helpers at dispatch time.
"""
from __future__ import annotations

from reyn.tools.universal_dispatch import (
    _mcp_tool_args,
    _multi_agent_delegate_args,
    _multi_agent_list_peers_args,
    _passthrough_args,
    _recall_single_source_args,
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


# ── _recall_single_source_args ────────────────────────────────────────────────


def test_recall_single_source_args_includes_source_from_entry_name() -> None:
    """Tier 2: the entry_name is wrapped as sources=[entry_name]."""
    result = _recall_single_source_args("corpus_a", {"query": "test"})
    assert result["sources"] == ["corpus_a"]


def test_recall_single_source_args_passes_query_and_top_k() -> None:
    """Tier 2: query and top_k from args are forwarded."""
    result = _recall_single_source_args("corpus_a", {"query": "find x", "top_k": 5})
    assert result["query"] == "find x"
    assert result["top_k"] == 5


def test_recall_single_source_args_omits_optional_fields_when_absent() -> None:
    """Tier 2: missing query/top_k are not included in the result dict."""
    result = _recall_single_source_args("corpus_b", {})
    assert "query" not in result
    assert "top_k" not in result
    assert result["sources"] == ["corpus_b"]


# ── _mcp_tool_args ────────────────────────────────────────────────────────────


def test_mcp_tool_args_wraps_entry_name_as_tool() -> None:
    """Tier 2: entry_name is placed under 'tool'."""
    result = _mcp_tool_args("server__my_tool", {"param": "value"})
    assert result["tool"] == "server__my_tool"


def test_mcp_tool_args_wraps_args_as_tool_args() -> None:
    """Tier 2: the args dict is placed under 'tool_args'."""
    result = _mcp_tool_args("server__my_tool", {"x": 1, "y": 2})
    assert result["tool_args"] == {"x": 1, "y": 2}


def test_mcp_tool_args_empty_args_gives_empty_tool_args() -> None:
    """Tier 2: empty args → tool_args is an empty dict."""
    result = _mcp_tool_args("server__tool", {})
    assert result["tool_args"] == {}


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
