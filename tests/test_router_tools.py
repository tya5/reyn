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
    # File read tools are unconditional — aligned with the OS-level
    # default-grant on paths within the project root. See router_tools.py:C
    # for the rationale.
    "list_directory",
    "read_file",
    # web_search is always exposed (E1) — read-only public search.
    "web_search",
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


def test_build_tools_returns_14_tools_when_no_extras():
    """No file.write / MCP / web_fetch extras: 11 baseline + web_search (E1)
    + list_directory + read_file (unconditional read access aligned with
    the OS-level default-grant). Write-class file tools and MCP/web_fetch
    remain gated, so 14 total at the unconfigured baseline.
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    assert len(tools) == 14, f"Expected 14 tools, got {len(tools)}"


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


def test_file_read_tools_unconditional_when_no_permissions():
    """No file_permissions kwarg → write-class file tools absent, but
    read-class tools (list_directory, read_file) are unconditional.

    Aligned with the OS-level default-grant in
    `permissions._in_default_read_zone`: paths within project root are
    always readable, so hiding the tools while the underlying op accepts
    the call was a wiring inconsistency. With this contract, fresh
    `reyn init` projects can answer "summarize the README" without any
    file-permission setup.
    """
    tools = build_tools(SAMPLE_SKILLS, SAMPLE_AGENTS)
    names = set(_tool_names(tools))
    # Read tools always present.
    assert FILE_READ_TOOL_NAMES.issubset(names), (
        f"Expected read tools always present, missing: "
        f"{FILE_READ_TOOL_NAMES - names}"
    )
    # Write tools gated.
    assert names.isdisjoint(FILE_WRITE_TOOL_NAMES), (
        f"Expected no write tools without declaration, but found: "
        f"{names & FILE_WRITE_TOOL_NAMES}"
    )


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
    """Full file + MCP + web permissions → 11 + 4 + 2 + 3 = 20 tools total
    (11 baseline, 4 file C1-C4, 2 web E1+E2, 3 MCP D1-D3)."""
    tools = build_tools(
        SAMPLE_SKILLS,
        SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=SAMPLE_MCP_SERVERS,
        web_fetch_allowed=True,
    )
    assert len(tools) == 20, f"Expected 20 tools with full permissions, got {len(tools)}"


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
