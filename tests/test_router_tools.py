"""Unit tests for src/reyn/chat/router_tools.py (PR35 Wave 1 Task A).

No LLM needed — all tests are pure Python, < 1 second total.
"""


from reyn.chat.router_tools import build_tools

# ── Fixtures / helpers ────────────────────────────────────────────────────────

SAMPLE_SKILLS = [
    {"name": "article_writer", "description": "Write articles"},
    {"name": "web_search", "description": "Search the web"},
    {"name": "summarizer", "description": "Summarise text"},
]

SAMPLE_AGENTS = [
    {"name": "researcher", "role": "Research agent"},
    {"name": "editor", "role": "Editorial agent"},
]

FORBIDDEN_KEYS = {"oneOf", "anyOf", "additionalProperties", "format"}

EXPECTED_TOOL_NAMES = [
    "list_skills",
    "describe_skill",
    "list_agents",
    "describe_agent",
    "list_memory",
    "read_memory_body",
    "invoke_skill",
    "delegate_to_agent",
    "remember_shared",
    "remember_agent",
    "forget_memory",
    # web_search is always exposed (E1) — read-only public search.
    "web_search",
    # web_fetch is always exposed (E2) — FP-0022: catalog-level gate removed;
    # authorization now at handler level via PermissionResolver._approve().
    "web_fetch",
    # read_tool_result (E3) — companion to web_fetch preview path. Added in
    # B49 Step 2 v6 fix (2026-05-22): registered in tools/__init__.py but
    # build_tools() was never surfacing it, so the lazy-expand half of the
    # preview-driven design was undeployed for router-side use.
    "read_tool_result",
    # plan (G1) — always exposed; LLM opts in for complex queries.
    "plan",
    # reyn_src_* are always exposed (F1, F2) — they read Reyn's own
    # public OSS repo, not the user's files, so no permission gate.
    "reyn_src_list",
    "reyn_src_read",
    # recall + drop_source (H1/H2, ADR-0033 Phase 1) — always exposed
    # when the ToolRegistry contains them (B17-S6-1 / B17-S8-2 fix).
    "recall",
    "drop_source",
]


def _tool_names(tools: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _walk_dict(d: dict, depth: int = 0):
    """Yield (key, value, depth) for every key in d and its nested dicts."""
    for k, v in d.items():
        yield k, v, depth
        if isinstance(v, dict):
            yield from _walk_dict(v, depth + 1)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    yield from _walk_dict(item, depth + 1)


def _max_object_nesting(params: dict) -> int:
    """Return the maximum depth at which a properties key appears inside params.

    params is the top-level parameters dict (depth 0).
    properties at the top level (params['properties']) is depth 1.
    A field inside that having its own 'properties' would be depth 2, etc.
    We enforce that only depth-1 properties exist (no nested objects).
    """
    max_depth = 0
    for k, _v, depth in _walk_dict(params):
        if k == "properties":
            max_depth = max(max_depth, depth)
    return max_depth


# ── Tests ─────────────────────────────────────────────────────────────────────


FILE_TOOL_NAMES = {"list_directory", "read_file", "write_file", "delete_file"}
FILE_READ_TOOL_NAMES = {"list_directory", "read_file"}
FILE_WRITE_TOOL_NAMES = {"write_file", "delete_file"}
MCP_TOOL_NAMES = {"list_mcp_servers", "list_mcp_tools", "call_mcp_tool"}

SAMPLE_MCP_SERVERS = [{"name": "fs", "description": "Filesystem MCP server"}]


def test_build_tools_returns_19_tools_when_no_extras():
    """No file / MCP extras: 11 baseline + web_search (E1, always on)
    + web_fetch (E2, FP-0022: always on, handler-level approval)
    + read_tool_result (E3, B49 Step 2 v6 fix: lazy-expand half of the
    preview-driven design, surfaced for router-side use)
    + reyn_src_list + reyn_src_read (F1/F2, always on)
    + plan (G1, always on) + recall + drop_source (H1/H2, always on).
    All file-class tools and MCP remain gated, so 19 total at the
    unconfigured baseline.
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    assert len(tools) == 19, f"Expected 19 tools, got {len(tools)}"


def test_tool_order_is_deterministic():
    tools_a = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    tools_b = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    assert _tool_names(tools_a) == _tool_names(tools_b)
    assert _tool_names(tools_a) == EXPECTED_TOOL_NAMES


def test_no_forbidden_schema_keywords():
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    for tool in tools:
        fn = tool["function"]
        for key, _val, _depth in _walk_dict(fn.get("parameters", {})):
            assert key not in FORBIDDEN_KEYS, (
                f"Tool '{fn['name']}' contains forbidden schema key '{key}'"
            )


def test_nested_objects_max_depth_1():
    """No object's properties may themselves contain nested objects.

    In the parameters dict:
      - depth-0 'properties' key is the top-level parameter list → OK
      - depth-1 'properties' key would be a nested object's inner fields → NOT OK
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    for tool in tools:
        fn = tool["function"]
        params = fn.get("parameters", {})
        # Find max depth of any 'properties' key
        max_depth = _max_object_nesting(params)
        assert max_depth <= 1, (
            f"Tool '{fn['name']}' has nested object properties at depth "
            f"{max_depth} (max allowed: 1)"
        )


def test_required_fields_present_per_tool():
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    for tool in tools:
        assert tool.get("type") == "function", (
            f"Tool missing 'type: function': {tool}"
        )
        fn = tool.get("function", {})
        assert "name" in fn, f"Tool missing function.name: {tool}"
        assert "description" in fn, f"Tool missing function.description: {tool}"
        assert "parameters" in fn, f"Tool missing function.parameters: {tool}"


def test_remember_type_enum():
    """remember_shared and remember_agent must both expose the canonical type enum."""
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    tool_map = {t["function"]["name"]: t for t in tools}

    expected_enum = ["user", "feedback", "project", "reference"]

    for tool_name in ("remember_shared", "remember_agent"):
        params = tool_map[tool_name]["function"]["parameters"]
        type_field = params["properties"]["type"]
        assert "enum" in type_field, (
            f"{tool_name}.parameters.properties.type missing 'enum'"
        )
        assert type_field["enum"] == expected_enum, (
            f"{tool_name} type enum mismatch: got {type_field['enum']}, "
            f"expected {expected_enum}"
        )


def test_layer_enum():
    """read_memory_body and forget_memory must both expose the canonical layer enum."""
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    tool_map = {t["function"]["name"]: t for t in tools}

    expected_enum = ["shared", "agent"]

    for tool_name in ("read_memory_body", "forget_memory"):
        params = tool_map[tool_name]["function"]["parameters"]
        layer_field = params["properties"]["layer"]
        assert "enum" in layer_field, (
            f"{tool_name}.parameters.properties.layer missing 'enum'"
        )
        assert layer_field["enum"] == expected_enum, (
            f"{tool_name} layer enum mismatch: got {layer_field['enum']}, "
            f"expected {expected_enum}"
        )


# ── File tool permission-gating tests ─────────────────────────────────────────


def test_file_tools_omitted_when_no_permissions():
    """No file_permissions kwarg → all file-class tools absent.

    Per the design contract: file_* tools touch the user's project
    files, which sit behind the operator's permission boundary. The
    chat router does NOT auto-grant file access just because the OS
    dispatch layer would permit it — surfacing the tool implies the
    operator opted in.

    For "explain Reyn itself" use cases, the always-on `reyn_src_*`
    tools cover the gap (= reading Reyn's own OSS repo, not user
    files).
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    names = set(_tool_names(tools))
    assert names.isdisjoint(FILE_TOOL_NAMES), (
        f"Expected no file tools, but found: {names & FILE_TOOL_NAMES}"
    )
    # reyn_src_* DO show up (= unconditional, by design).
    assert "reyn_src_list" in names
    assert "reyn_src_read" in names


def test_file_read_only_tools_present():
    """read scope only → list_directory and read_file present; write tools absent."""
    tools = build_tools(
        SAMPLE_SKILLS,
        SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": []},
    )
    names = set(_tool_names(tools))
    assert "list_directory" in names, "list_directory missing with read scope"
    assert "read_file" in names, "read_file missing with read scope"
    assert "write_file" not in names, "write_file must be absent with read-only scope"
    assert "delete_file" not in names, "delete_file must be absent with read-only scope"


def test_file_full_tools_present():
    """Both read and write scope → all 4 file tools present."""
    tools = build_tools(
        SAMPLE_SKILLS,
        SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
    )
    names = set(_tool_names(tools))
    missing = FILE_TOOL_NAMES - names
    assert not missing, f"Missing file tools with full permissions: {missing}"


# ── MCP tool permission-gating tests ──────────────────────────────────────────


def test_mcp_tools_omitted_when_no_servers():
    """No mcp_servers kwarg → all MCP tools absent."""
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    names = set(_tool_names(tools))
    assert names.isdisjoint(MCP_TOOL_NAMES), (
        f"Expected no MCP tools, but found: {names & MCP_TOOL_NAMES}"
    )


def test_mcp_tools_present_when_servers_configured():
    """mcp_servers non-empty → all 3 MCP tools present."""
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS, mcp_servers=SAMPLE_MCP_SERVERS)
    names = set(_tool_names(tools))
    missing = MCP_TOOL_NAMES - names
    assert not missing, f"Missing MCP tools when servers configured: {missing}"


# ── Total count test ──────────────────────────────────────────────────────────


def test_total_tool_count_with_full_permissions():
    """Full file + MCP permissions → 11 baseline + 4 file C1-C4
    + 3 web E1+E2+E3 (web_search + web_fetch always on since FP-0022;
    read_tool_result added in B49 Step 2 v6 fix) + 4 MCP D1-D4
    + 2 reyn_src F1-F2 + 1 plan G1
    + 2 RAG H1-H2 (recall + drop_source) = 27 tools total.
    FP-0032: D4 describe_mcp_tool added alongside D1-D3.
    web_fetch_allowed param is kept for backward compat but now a no-op.
    """
    tools = build_tools(
        SAMPLE_SKILLS,
        SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=SAMPLE_MCP_SERVERS,
        web_fetch_allowed=True,
    )
    assert len(tools) == 27, f"Expected 27 tools with full permissions, got {len(tools)}"


# ── Gemini-safe schema checks apply to new tools too ──────────────────────────


def test_no_forbidden_schema_keywords_full_permissions():
    """new file+MCP tools must also pass Gemini-safe schema check."""
    tools = build_tools(
        SAMPLE_SKILLS,
        SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=SAMPLE_MCP_SERVERS,
    )
    for tool in tools:
        fn = tool["function"]
        for key, _val, _depth in _walk_dict(fn.get("parameters", {})):
            assert key not in FORBIDDEN_KEYS, (
                f"Tool '{fn['name']}' contains forbidden schema key '{key}'"
            )


def test_nested_objects_max_depth_1_full_permissions():
    """new file+MCP tools must also satisfy max depth-1 object nesting."""
    tools = build_tools(
        SAMPLE_SKILLS,
        SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=SAMPLE_MCP_SERVERS,
    )
    for tool in tools:
        fn = tool["function"]
        params = fn.get("parameters", {})
        max_depth = _max_object_nesting(params)
        assert max_depth <= 1, (
            f"Tool '{fn['name']}' has nested object properties at depth "
            f"{max_depth} (max allowed: 1)"
        )


# ── B17-S6-1 / B17-S8-2 fix: recall + drop_source wiring tests ───────────────


def test_recall_in_build_tools():
    """Tier 2: recall ToolDefinition is exposed in build_tools() for router LLM.

    B17-S6-1 fix: RECALL was registered in ToolRegistry but missing from
    build_tools(), so the LLM could not see or call it (S5/S6 blocked).
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    tool_names = [t["function"]["name"] for t in tools]
    assert "recall" in tool_names, (
        f"'recall' missing from build_tools() output; got: {tool_names}"
    )


def test_drop_source_in_build_tools():
    """Tier 2: drop_source ToolDefinition is exposed in build_tools() for router LLM.

    B17-S8-2 fix: DROP_SOURCE was registered in ToolRegistry but missing from
    build_tools(), so the LLM could not see or call it (S8 blocked).
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    tool_names = [t["function"]["name"] for t in tools]
    assert "drop_source" in tool_names, (
        f"'drop_source' missing from build_tools() output; got: {tool_names}"
    )


def test_recall_in_dispatch_registry():
    """Tier 2: recall is in RouterLoop._REGISTRY_DISPATCH_TOOLS for runtime dispatch.

    B17-S6-1 fix: without this, dispatch_tool would fall through to the
    legacy if/elif tree and return {"error": "unhandled tool: recall"}.
    _REGISTRY_DISPATCH_TOOLS is a class attribute on RouterLoop.
    """
    from reyn.chat.router_loop import RouterLoop
    assert "recall" in RouterLoop._REGISTRY_DISPATCH_TOOLS, (
        "'recall' missing from RouterLoop._REGISTRY_DISPATCH_TOOLS"
    )


def test_drop_source_in_dispatch_registry():
    """Tier 2: drop_source is in RouterLoop._REGISTRY_DISPATCH_TOOLS for runtime dispatch.

    B17-S8-2 fix: without this, dispatch_tool would fall through to the
    legacy if/elif tree and return {"error": "unhandled tool: drop_source"}.
    _REGISTRY_DISPATCH_TOOLS is a class attribute on RouterLoop.
    """
    from reyn.chat.router_loop import RouterLoop
    assert "drop_source" in RouterLoop._REGISTRY_DISPATCH_TOOLS, (
        "'drop_source' missing from RouterLoop._REGISTRY_DISPATCH_TOOLS"
    )
