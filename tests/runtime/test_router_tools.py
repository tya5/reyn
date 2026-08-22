"""Unit tests for src/reyn/runtime/router_tools.py (PR35 Wave 1 Task A).

No LLM needed — all tests are pure Python, < 1 second total.
"""


from reyn.runtime.router_tools import build_tools

# ── Fixtures / helpers ────────────────────────────────────────────────────────

SAMPLE_AGENTS = [
    {"name": "researcher", "role": "Research agent"},
    {"name": "editor", "role": "Editorial agent"},
]

FORBIDDEN_KEYS = {"oneOf", "anyOf", "additionalProperties", "format"}

EXPECTED_TOOL_NAMES = [
    "list_agents",
    "describe_agent",
    "list_memory",
    "read_memory_body",
    "spawn_session",  # #2103 S1bc / #2120: router-only spawn primitive (unconditional)
    "spawn_agent",     # #2103 B-tool: router-only org-design spawn primitive
    "create_topology",  # #2103 C1: router-only org-wiring primitive
    "send_to_session",  # proposal 0067 P5 (#3978): router-only delivery primitive
    "run_prompt",  # proposal 0067 P4d (#3978): router-only sync run+collect primitive
    "remember_shared",
    "remember_agent",
    "forget_memory",
    # web_search is always exposed (E1) — read-only public search.
    "web_search",
    # web_fetch is always exposed (E2) — FP-0022: catalog-level gate removed;
    # authorization now at handler level via PermissionResolver._approve().
    "web_fetch",
    # #1449: read_tool_result (the former E3) retired — its same-host read
    # folded into read_file; web_fetch's preview points there now.
    # reyn_repo_* are always exposed (F1, F2) — they read Reyn's own
    # public OSS repo, not the user's files, so no permission gate.
    "reyn_repo_list",
    "reyn_repo_read",
    # FP-0066 P1b: semantic_search / drop_source / index_update (former H1-H3,
    # ADR-0033 Phase 1 / FP-0057 Phase 2a / #3222) are retired — the
    # agent-facing layer-1 in-core RAG tools.
    # present + render_template (#2692, part of the #2688 sweep) — always exposed
    # so chat can reach the existing present-layer ops (read-authority is enforced
    # at op-exec, not by catalog exclusion; the pipeline surface opens from the same
    # single registration).
    "present",
    "render_template",
    # describe_session (#5012-A) — always exposed, unconditional (like
    # present/render_template above): no natural gating condition, cheap
    # read-only introspection relevant on any turn.
    "describe_session",
    # compact (#272/#1128) is NOT in the baseline — it is visibility-gated
    # (compact_visible) and only appears when the window is filling, paired
    # with the context-size signal (see test_compact_visible_gates_tool).
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
# #5066: was 3 names (D1-D3 only) — measured against router_tools.py's own
# ``if mcp_servers:`` block (the SAME gate covers all of D1-D11/D1b, one
# unconditional branch, not per-tool), the full MCP-gated set is 12, not 3.
# The missing 9 (D1b, D4-D11) were added over time (FP-0032's D4
# describe_mcp_tool, #4686's D1b list_mcp_subscriptions, the MCP resources/
# prompts additions D5-D11) without this constant ever being updated —
# exactly the "structurally green on addition" shape #5066 reports: a
# disjoint/superset check against a STALE, too-small set still passes
# whether or not the new tools are correctly gated, because it never looks
# at them at all.
MCP_TOOL_NAMES = {
    "list_mcp_servers", "list_mcp_subscriptions", "list_mcp_tools",
    "call_mcp_tool", "describe_mcp_tool", "list_mcp_resources",
    "list_mcp_resource_templates", "read_mcp_resource",
    "subscribe_mcp_resource", "unsubscribe_mcp_resource",
    "list_mcp_prompts", "get_mcp_prompt",
}

SAMPLE_MCP_SERVERS = [{"name": "fs", "description": "Filesystem MCP server"}]


def test_build_tools_returns_expected_baseline_tools():
    """Tier 2: No file / MCP extras: 12 baseline (delegate_to_agent retired,
    #3978 P6 — was 13) + web_search (E1, always on)
    + web_fetch (E2, FP-0022: always on, handler-level approval)
    + read_tool_result (E3, B49 Step 2 v6 fix: lazy-expand half of the
    preview-driven design, surfaced for router-side use)
    + reyn_repo_list + reyn_repo_read (F1/F2, always on)
    + plan (G1, always on). FP-0066 P1b retired the former H1-H3 RAG tools
    (semantic_search / drop_source / index_update). compact
    (#272/#1128) is visibility-gated (off by default), so the unconfigured
    baseline is exactly EXPECTED_TOOL_NAMES. All file-class tools and MCP
    remain gated.
    """
    tools = build_tools(SAMPLE_AGENTS)
    assert _tool_names(tools) == EXPECTED_TOOL_NAMES, (
        f"Expected tools {EXPECTED_TOOL_NAMES}, got {_tool_names(tools)}"
    )


def test_tool_order_is_deterministic():
    """Tier 2: build_tools() returns tools in a stable, deterministic order."""
    tools_a = build_tools(SAMPLE_AGENTS)
    tools_b = build_tools(SAMPLE_AGENTS)
    assert _tool_names(tools_a) == _tool_names(tools_b)
    assert _tool_names(tools_a) == EXPECTED_TOOL_NAMES


def test_compact_visible_gates_tool():
    """Tier 2: #272/#1128 — `compact` is exposed only when compact_visible=True
    (window filling), and absent by default (ample window). Mirrors the
    search_actions §D14 visibility gate; keeps tools= stable on ample-window
    turns (and LLMReplay fixtures keyed on tools byte-stable).
    """
    default = _tool_names(build_tools(SAMPLE_AGENTS))
    assert "compact" not in default, "compact must be hidden when the window is ample"

    gated = _tool_names(build_tools(SAMPLE_AGENTS, compact_visible=True))
    assert "compact" in gated, "compact must appear when the window is filling"
    # Enabling the gate only adds compact — no other tool churn.
    assert set(gated) - set(default) == {"compact"}


def test_no_forbidden_schema_keywords():
    """Tier 2: No tool schema contains Gemini-forbidden keywords."""
    tools = build_tools(SAMPLE_AGENTS)
    for tool in tools:
        fn = tool["function"]
        for key, _val, _depth in _walk_dict(fn.get("parameters", {})):
            assert key not in FORBIDDEN_KEYS, (
                f"Tool '{fn['name']}' contains forbidden schema key '{key}'"
            )


def test_nested_objects_max_depth_1():
    """Tier 2: No object's properties may themselves contain nested objects.

    In the parameters dict:
      - depth-0 'properties' key is the top-level parameter list → OK
      - depth-1 'properties' key would be a nested object's inner fields → NOT OK
    """
    tools = build_tools(SAMPLE_AGENTS)
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
    """Tier 1: Every tool has type, function.name, function.description, and function.parameters."""
    tools = build_tools(SAMPLE_AGENTS)
    for tool in tools:
        assert tool.get("type") == "function", (
            f"Tool missing 'type: function': {tool}"
        )
        fn = tool.get("function", {})
        assert "name" in fn, f"Tool missing function.name: {tool}"
        assert "description" in fn, f"Tool missing function.description: {tool}"
        assert "parameters" in fn, f"Tool missing function.parameters: {tool}"


def test_remember_type_enum():
    """Tier 1: remember_shared and remember_agent must both expose the canonical type enum."""
    tools = build_tools(SAMPLE_AGENTS)
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
    """Tier 1: read_memory_body and forget_memory must both expose the canonical layer enum."""
    tools = build_tools(SAMPLE_AGENTS)
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
    """Tier 2: No file_permissions kwarg → all file-class tools absent.

    Per the design contract: file_* tools touch the user's project
    files, which sit behind the operator's permission boundary. The
    chat router does NOT auto-grant file access just because the OS
    dispatch layer would permit it — surfacing the tool implies the
    operator opted in.

    For "explain Reyn itself" use cases, the always-on `reyn_repo_*`
    tools cover the gap (= reading Reyn's own OSS repo, not user
    files).
    """
    tools = build_tools(SAMPLE_AGENTS)
    names = set(_tool_names(tools))
    assert names.isdisjoint(FILE_TOOL_NAMES), (
        f"Expected no file tools, but found: {names & FILE_TOOL_NAMES}"
    )
    # reyn_repo_* DO show up (= unconditional, by design).
    assert "reyn_repo_list" in names
    assert "reyn_repo_read" in names


def test_file_read_only_tools_present():
    """Tier 2: read scope only → list_directory and read_file present; write tools absent."""
    tools = build_tools(SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": []},
    )
    names = set(_tool_names(tools))
    assert "list_directory" in names, "list_directory missing with read scope"
    assert "read_file" in names, "read_file missing with read scope"
    assert "write_file" not in names, "write_file must be absent with read-only scope"
    assert "delete_file" not in names, "delete_file must be absent with read-only scope"


def test_file_full_tools_present():
    """Tier 2: Both read and write scope → all 4 file tools present."""
    tools = build_tools(SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
    )
    names = set(_tool_names(tools))
    missing = FILE_TOOL_NAMES - names
    assert not missing, f"Missing file tools with full permissions: {missing}"


# ── MCP tool permission-gating tests ──────────────────────────────────────────


def test_mcp_tools_omitted_when_no_servers():
    """Tier 2: No mcp_servers kwarg → all MCP tools absent."""
    tools = build_tools(SAMPLE_AGENTS)
    names = set(_tool_names(tools))
    assert names.isdisjoint(MCP_TOOL_NAMES), (
        f"Expected no MCP tools, but found: {names & MCP_TOOL_NAMES}"
    )


def test_mcp_tools_present_when_servers_configured():
    """Tier 2: mcp_servers non-empty → all 3 MCP tools present."""
    tools = build_tools(SAMPLE_AGENTS, mcp_servers=SAMPLE_MCP_SERVERS)
    names = set(_tool_names(tools))
    missing = MCP_TOOL_NAMES - names
    assert not missing, f"Missing MCP tools when servers configured: {missing}"


# ── Total count test ──────────────────────────────────────────────────────────


EXPECTED_FULL_TOOL_NAMES = sorted(
    EXPECTED_TOOL_NAMES + list(FILE_TOOL_NAMES) + list(MCP_TOOL_NAMES)
)


def test_total_tool_count_with_full_permissions():
    """Tier 2: Full file + MCP permissions → the tool set is EXACTLY the
    baseline + file + MCP union — no fewer (a removed tool), no more (an
    added tool this test was never updated to cover).

    #5066: the three former ``missing_X = X - names; assert not missing_X``
    checks only ever look ONE direction (does ``names`` cover ``X``) — they
    never check the REVERSE (does ``X`` cover ``names``), so a tool ADDED to
    ``build_tools`` and never added to ``EXPECTED_FULL_TOOL_NAMES`` (or one
    of the three sets it is built from) makes this test's name and docstring
    ("total tool count" / "N tools total") false while the assertions stay
    green — structurally green on addition, the exact defect this issue
    reports. Measured directly (not asserted from memory): the real total
    with full permissions is **35** — 19 baseline (``EXPECTED_TOOL_NAMES``)
    + 4 file C1-C4 + 12 MCP D1-D11/D1b (``MCP_TOOL_NAMES`` — see that
    constant's own #5066 comment for why it grew from 3 to 12).
    ``delegate_to_agent`` (#3978 P6) and the H1-H3 RAG tools (FP-0066 P1b)
    are retired, not counted; ``web_fetch_allowed`` is kept for backward
    compat but is now a no-op (web_fetch is always on, FP-0022).

    Real equality (``==`` on the two sorted name lists), not a bare
    ``len()`` — CLAUDE.md's test-review question 3 ("who would miss this
    test if it were gone"): a headline count alone would tell a future
    reader THAT something changed, never WHICH tool, reproducing the exact
    "don't know what to touch" pain #5043 already recorded."""
    tools = build_tools(SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=SAMPLE_MCP_SERVERS,
        web_fetch_allowed=True,
    )
    names = sorted(_tool_names(tools))
    expected = EXPECTED_FULL_TOOL_NAMES
    assert names == expected, (
        f"tool set changed — extra: {sorted(set(names) - set(expected))}, "
        f"missing: {sorted(set(expected) - set(names))}"
    )


# ── Gemini-safe schema checks apply to new tools too ──────────────────────────


def test_no_forbidden_schema_keywords_full_permissions():
    """Tier 2: new file+MCP tools must also pass Gemini-safe schema check."""
    tools = build_tools(SAMPLE_AGENTS,
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
    """Tier 2: new file+MCP tools must also satisfy max depth-1 object nesting."""
    tools = build_tools(SAMPLE_AGENTS,
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


# ── FP-0066 P1b: semantic_search / drop_source / index_update retired ──────
#
# The former B17-S6-1 / B17-S8-2 / #3222 wiring tests for these 3 layer-1
# agent-facing RAG tools are removed along with the tools themselves — see
# docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md §9.
# test_returns_external_content_flagset_1822.py / test_universal_catalog.py /
# tests/core/test_op_semantic_search.py + test_op_index_update.py (OS-internal op
# level, kept) cover the surviving invariants.


def test_session_spawn_in_dispatch_registry():
    """Tier 2: spawn_session is in RouterLoop.REGISTRY_DISPATCH_TOOLS for runtime dispatch.

    #2120 fix (tui live-probe): spawn_session was registered + floored + advertised
    (build_tools B2b) but NOT dispatch-routed → the LLM called it and hit
    {"error": "unhandled tool: spawn_session"} (the advertised-but-not-dispatched class,
    same as read_tool_result / recall). Without this membership the bare name (no "__")
    falls through to the unhandled-tool branch.
    """
    from reyn.runtime.router_loop import RouterLoop
    # Paired with the send_to_session sentinel (a sibling router-only peer
    # primitive, proposal 0067 P5 #4101) so the test pins the shared dispatch
    # family, not a spawn_session-only fluke. delegate_to_agent — the
    # original sentinel here — retired in #3978 P6.
    assert "send_to_session" in RouterLoop.REGISTRY_DISPATCH_TOOLS
    assert "spawn_session" in RouterLoop.REGISTRY_DISPATCH_TOOLS, (
        "'spawn_session' missing from RouterLoop.REGISTRY_DISPATCH_TOOLS"
    )
