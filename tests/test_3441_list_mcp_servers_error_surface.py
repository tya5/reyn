"""Tier 2: #3441 — list_mcp_servers error surface + shared error-detection helper.

Bug (tools/mcp.py): unlike its 4 siblings, ``_handle_list_mcp_servers`` handed the
LLM ``{"servers": [{"error": "cancelled"}]}`` verbatim on an adapter-layer failure
(unresolved-config — see ``RouterHostAdapter._mcp_resolve_server_config``, which
returns the sentinel ``[{"error": "..."}]`` rather than raising; #3447 additionally
moved the Cancelled/MCPFault catch for a gateway-call failure up to
``_call_mcp_list`` in this same module, reproducing the identical sentinel one
layer up) — a failure disguised as a one-entry successful server listing. The
other four ``list_mcp_*`` handlers already detected this sentinel, but via TWO
different implementations (``_handle_list_mcp_tools`` checked every entry `t` in
its name-rewrite loop; the other three checked only ``result[0]`` with an
``isinstance(..., Mapping)`` guard).

Fix: add the missing arm to ``_handle_list_mcp_servers`` AND consolidate all five
call sites onto one shared helper, ``_mcp_list_error(result)`` — chosen to follow
the ``result[0]`` + isinstance form (the 3-of-4 majority), not the per-element
loop form. Rationale (measured, not assumed): the sentinel is *always* a single-
element list — both producers (``_mcp_resolve_server_config``'s early-return and
``_mcp_list_via_gateway``'s except branches) construct exactly one dict at index
0 — so scanning every element (as the old ``_handle_list_mcp_tools`` loop did)
adds no coverage the majority form lacks, while carrying a real (if narrow) risk
the majority form does not: a legitimate later list entry that happens to carry
an "error" key would falsely trip the per-element loop but not the ``result[0]``
check. The tools handler's loop existed only because its #879 name-rewrite already
needed to iterate every entry — not because the loop form was a deliberate choice
about error detection.

Verification (per issue #3441's ★ checklist):
  1. Each of the 5 handlers gets its own arm (a per-handler Fake host), because a
     helper-level contract test does not prove any given production call site
     actually reached the helper.
  2. Negative examples for the "normal listing" arms use out-of-band shapes (a
     plain non-Mapping entry, an empty list) rather than a not-yet-registered
     legal value.
"""
from __future__ import annotations

import asyncio

from reyn.tools.mcp import (
    _handle_list_mcp_prompts,
    _handle_list_mcp_resource_templates,
    _handle_list_mcp_resources,
    _handle_list_mcp_servers,
    _handle_list_mcp_tools,
    _mcp_list_error,
)
from reyn.tools.types import RouterCallerState, ToolContext


def _run(coro):
    return asyncio.run(coro)


def _tool_ctx(host) -> ToolContext:
    return ToolContext(
        caller_kind="router",
        events=None,
        permission_resolver=None,
        workspace=None,
        router_state=RouterCallerState(host=host),
    )


# ---------------------------------------------------------------------------
# Unit coverage for the shared helper itself
# ---------------------------------------------------------------------------


def test_mcp_list_error_detects_sentinel():
    """Tier 2: the shared sentinel — a single-element list with an "error"
    key at index 0 — is detected and its message returned."""
    assert _mcp_list_error([{"error": "cancelled"}]) == "cancelled"


def test_mcp_list_error_none_for_normal_listing():
    """Tier 2: a normal (non-error) listing returns None — out-of-band
    negative example: entries with real listing shape, not an unregistered
    error variant."""
    assert _mcp_list_error([{"name": "search", "description": "d"}]) is None


def test_mcp_list_error_none_for_empty_list():
    """Tier 2: an empty list (e.g. no resource_templates registered) is a
    normal result, not an error — must not be misdetected."""
    assert _mcp_list_error([]) is None


def test_mcp_list_error_none_for_non_mapping_first_entry():
    """Tier 2: a malformed/non-dict first entry is not mistaken for the
    error sentinel (guards the isinstance check itself)."""
    assert _mcp_list_error(["not-a-dict"]) is None


# ---------------------------------------------------------------------------
# Per-handler Fake hosts — real stub objects, not unittest.mock
# ---------------------------------------------------------------------------


class _ServersHost:
    def __init__(self, result):
        self._result = result

    async def mcp_list_servers(self):
        return self._result


class _ToolsHost:
    def __init__(self, result):
        self._result = result

    async def mcp_list_tools(self, server: str):
        return self._result


class _ResourcesHost:
    def __init__(self, result):
        self._result = result

    async def mcp_list_resources(self, server: str):
        return self._result


class _TemplatesHost:
    def __init__(self, result):
        self._result = result

    async def mcp_list_resource_templates(self, server: str):
        return self._result


class _PromptsHost:
    def __init__(self, result):
        self._result = result

    async def mcp_list_prompts(self, server: str):
        return self._result


# ---------------------------------------------------------------------------
# Arm 1/5 — list_mcp_servers (the fixed handler)
# ---------------------------------------------------------------------------


def test_list_mcp_servers_surfaces_error():
    """Tier 2: #3441's core fix — a Session-layer failure surfaces as a
    top-level {"error": ...}, not {"servers": [{"error": ...}]}."""
    host = _ServersHost([{"error": "cancelled"}])
    result = _run(_handle_list_mcp_servers({}, _tool_ctx(host)))

    assert result == {"error": "cancelled"}
    assert "servers" not in result


def test_list_mcp_servers_normal_result_unaffected():
    """Tier 2: the error-detection arm must not affect the successful path."""
    host = _ServersHost([{"name": "s1", "description": "server 1"}])
    result = _run(_handle_list_mcp_servers({}, _tool_ctx(host)))

    assert result == {"servers": [{"name": "s1", "description": "server 1"}]}


# ---------------------------------------------------------------------------
# Arm 2/5 — list_mcp_tools (already had error surface; now via shared helper)
# ---------------------------------------------------------------------------


def test_list_mcp_tools_surfaces_error_via_shared_helper():
    """Tier 2: _handle_list_mcp_tools still surfaces the sentinel after the
    consolidation onto _mcp_list_error."""
    host = _ToolsHost([{"error": "connection refused"}])
    result = _run(_handle_list_mcp_tools({"server": "web-search"}, _tool_ctx(host)))

    assert result == {"error": "connection refused"}
    assert "mcp_tools" not in result


def test_list_mcp_tools_normal_result_still_rewrites_names():
    """Tier 2: the #879 name-rewrite loop still runs on the normal path
    after the error check was pulled out of it."""
    host = _ToolsHost([{"name": "search", "description": "d", "inputSchema": {}}])
    result = _run(_handle_list_mcp_tools({"server": "web-search"}, _tool_ctx(host)))

    assert [t["name"] for t in result["mcp_tools"]] == ["web-search__search"]


# ---------------------------------------------------------------------------
# Arm 3/5 — list_mcp_resources
# ---------------------------------------------------------------------------


def test_list_mcp_resources_surfaces_error_via_shared_helper():
    """Tier 2: _handle_list_mcp_resources still surfaces the sentinel after
    consolidation."""
    host = _ResourcesHost([{"error": "MCP server 'x' not configured"}])
    result = _run(_handle_list_mcp_resources({"server": "x"}, _tool_ctx(host)))

    assert result == {"error": "MCP server 'x' not configured"}


def test_list_mcp_resources_normal_result_unaffected():
    """Tier 2: normal path is unchanged by the consolidation."""
    host = _ResourcesHost([{"uri": "resource://greeting", "name": "greeting"}])
    result = _run(_handle_list_mcp_resources({"server": "srv"}, _tool_ctx(host)))

    assert result == {"resources": [{"uri": "resource://greeting", "name": "greeting"}]}


# ---------------------------------------------------------------------------
# Arm 4/5 — list_mcp_resource_templates
# ---------------------------------------------------------------------------


def test_list_mcp_resource_templates_surfaces_error_via_shared_helper():
    """Tier 2: _handle_list_mcp_resource_templates still surfaces the
    sentinel after consolidation."""
    host = _TemplatesHost([{"error": "timeout"}])
    result = _run(_handle_list_mcp_resource_templates({"server": "srv"}, _tool_ctx(host)))

    assert result == {"error": "timeout"}


def test_list_mcp_resource_templates_empty_is_not_error():
    """Tier 2: an empty templates list stays a normal (non-error) result."""
    host = _TemplatesHost([])
    result = _run(_handle_list_mcp_resource_templates({"server": "srv"}, _tool_ctx(host)))

    assert result == {"resource_templates": []}


# ---------------------------------------------------------------------------
# Arm 5/5 — list_mcp_prompts
# ---------------------------------------------------------------------------


def test_list_mcp_prompts_surfaces_error_via_shared_helper():
    """Tier 2: _handle_list_mcp_prompts still surfaces the sentinel after
    consolidation."""
    host = _PromptsHost([{"error": "cancelled"}])
    result = _run(_handle_list_mcp_prompts({"server": "srv"}, _tool_ctx(host)))

    assert result == {"error": "cancelled"}


def test_list_mcp_prompts_normal_result_unaffected():
    """Tier 2: normal path is unchanged by the consolidation."""
    host = _PromptsHost([{"name": "greet"}])
    result = _run(_handle_list_mcp_prompts({"server": "srv"}, _tool_ctx(host)))

    assert result == {"prompts": [{"name": "greet"}]}
