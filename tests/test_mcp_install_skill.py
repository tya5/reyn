"""Tests for the mcp_install stdlib skill.

Covers:
  - Tier 2: skill loads and compiles without errors (contract invariant)
  - Tier 2: preprocessor fetch_server_for_install function (deterministic)
  - Tier 2: skill frontmatter declares mcp_install permission and correct graph
  - Tier 3a: LLM discover-phase decision when registry returns a direct match
    (record via REYN_LLM_RECORD=1 against a live backend; replay thereafter)

The Tier 3 test uses the same replay infrastructure as test_replay_read_local_files.
Fixture is at tests/fixtures/llm/mcp_install/discover_direct.jsonl.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FILESYSTEM_SERVER_JSON = {
    "name": "io.github.modelcontextprotocol/server-filesystem",
    "description": "Filesystem MCP server",
    "version": "0.6.2",
    "repository": {"url": "https://github.com/modelcontextprotocol/servers"},
    "$schema": "https://static.modelcontextprotocol.io/schemas/server.schema.json",
    "packages": [
        {
            "registryType": "npm",
            "identifier": "@modelcontextprotocol/server-filesystem",
            "version": "0.6.2",
            "transport": {"type": "stdio"},
            "environmentVariables": [],
        }
    ],
    "remotes": [],
}

_FILESYSTEM_SEARCH_RESPONSE = {
    "servers": [
        {
            "server": {
                "name": "io.github.modelcontextprotocol/server-filesystem",
                "description": "Filesystem MCP server — read and write local files.",
                "repository": {"url": "https://github.com/modelcontextprotocol/servers"},
                "packages": [{"registryType": "npm", "identifier": "@modelcontextprotocol/server-filesystem"}],
            }
        }
    ],
    "metadata": {"count": 1},
}

_EMPTY_SEARCH_RESPONSE = {
    "servers": [],
    "metadata": {"count": 0},
}


def _patch_registry_get_versions(server_response: dict, status: int = 200):
    """Patch RegistryClient._get for get_server (versions/latest endpoint)."""

    async def _fake_get(self, path: str, params=None):
        if status >= 400:
            from reyn.registry.client import RegistryError
            raise RegistryError(f"HTTP {status}")
        if "/versions/latest" in path:
            return {"server": server_response}
        # search endpoint
        return _FILESYSTEM_SEARCH_RESPONSE

    return mock.patch("reyn.registry.client.RegistryClient._get", _fake_get)


def _patch_registry_search(response_data: dict, status: int = 200):
    """Patch RegistryClient._get for search endpoint."""

    async def _fake_get(self, path: str, params=None):
        if status >= 400:
            from reyn.registry.client import RegistryError
            raise RegistryError(f"HTTP {status}")
        return response_data

    return mock.patch("reyn.registry.client.RegistryClient._get", _fake_get)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tier 2: skill compilation and graph contract
# ---------------------------------------------------------------------------


_SKILL_MD_PATH = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "mcp_install" / "skill.md"
)
_SKILL_ROOT = _SKILL_MD_PATH.parent.parent.parent  # src/reyn/stdlib/


def _load_mcp_install_skill():
    from reyn.compiler.loader import load_dsl_skill
    return load_dsl_skill(_SKILL_MD_PATH, skill_root=_SKILL_ROOT)


def test_mcp_install_skill_loads():
    """Tier 2: mcp_install skill.md loads and compiles without errors."""
    assert _SKILL_MD_PATH.exists(), f"mcp_install skill.md not found: {_SKILL_MD_PATH}"

    skill = _load_mcp_install_skill()
    assert skill is not None
    assert skill.entry_phase == "discover"
    assert "discover" in skill.phases


def test_mcp_install_skill_permission_decl():
    """Tier 2: mcp_install skill declares mcp_install permission (ADR-0029)."""
    skill = _load_mcp_install_skill()

    assert skill.permissions.mcp_install is True, (
        "mcp_install skill must declare permissions.mcp_install: true"
    )


def test_mcp_install_skill_graph_is_single_phase():
    """Tier 2: mcp_install skill graph: discover phase can finish (single-phase skill)."""
    skill = _load_mcp_install_skill()

    # discover phase can finish (single-phase skill)
    assert "discover" in skill.graph.can_finish_phases
    # No further transitions needed for the basic single-phase design
    discover_transitions = skill.graph.transitions.get("discover", [])
    assert isinstance(discover_transitions, list)


def test_mcp_install_skill_final_output_schema():
    """Tier 2: mcp_install skill final_output_schema matches mcp_install_result."""
    skill = _load_mcp_install_skill()

    assert skill.final_output_name == "mcp_install_result"
    # The compiler wraps the artifact schema in {type, data} envelope.
    schema = skill.final_output_schema
    # Dig into the data sub-schema (set by the compiler from artifacts/*.yaml).
    data_schema = schema.get("properties", {}).get("data", {})
    data_props = data_schema.get("properties", {})
    assert "status" in data_props, (
        f"Expected 'status' in data properties, got: {list(data_props.keys())}"
    )
    assert "server_id" in data_props


# ---------------------------------------------------------------------------
# Tier 2: preprocessor — fetch_server_for_install deterministic invariants
# ---------------------------------------------------------------------------


def test_fetch_server_direct_server_id(tmp_path):
    """Tier 2: Explicit server_id (contains '/') triggers direct lookup path."""
    import reyn.registry.cache as cache_mod
    from reyn.stdlib.skills.mcp_install.registry_fetch import fetch_server_for_install

    artifact = {
        "data": {"text": "io.github.modelcontextprotocol/server-filesystem"}
    }

    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        with _patch_registry_get_versions(_FILESYSTEM_SERVER_JSON):
            result = fetch_server_for_install(artifact)

    assert result["source"] == "direct"
    assert result["server_id"] == "io.github.modelcontextprotocol/server-filesystem"


def test_fetch_server_keyword_search_single_result(tmp_path):
    """Tier 2: Natural language input triggers keyword search; single result → source='direct'."""
    import reyn.registry.cache as cache_mod
    from reyn.stdlib.skills.mcp_install.registry_fetch import fetch_server_for_install

    artifact = {"data": {"text": "filesystem MCP server を入れて"}}

    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        with _patch_registry_search(_FILESYSTEM_SEARCH_RESPONSE):
            result = fetch_server_for_install(artifact)

    assert result["source"] == "direct"
    assert result["server_id"] == "io.github.modelcontextprotocol/server-filesystem"


def test_fetch_server_multiple_results_source_search(tmp_path):
    """Tier 2: Multiple search results → source='search' with candidates list."""
    import reyn.registry.cache as cache_mod
    from reyn.stdlib.skills.mcp_install.registry_fetch import fetch_server_for_install

    multi_response = {
        "servers": [
            {
                "server": {
                    "name": "io.github.foo/mcp-server-a",
                    "description": "Server A",
                    "repository": {"url": "https://github.com/foo/a"},
                    "packages": [{"registryType": "npm", "identifier": "@foo/a"}],
                }
            },
            {
                "server": {
                    "name": "io.github.bar/mcp-server-b",
                    "description": "Server B",
                    "repository": {"url": "https://github.com/bar/b"},
                    "packages": [{"registryType": "npm", "identifier": "@bar/b"}],
                }
            },
        ],
        "metadata": {"count": 2},
    }

    artifact = {"data": {"text": "some mcp server"}}

    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        with _patch_registry_search(multi_response):
            result = fetch_server_for_install(artifact)

    assert result["source"] == "search"
    assert result["server_id"] == ""
    assert len(result["candidates"]) == 2


def test_fetch_server_empty_results_source_not_found(tmp_path):
    """Tier 2: Empty search results → source='not_found'."""
    import reyn.registry.cache as cache_mod
    from reyn.stdlib.skills.mcp_install.registry_fetch import fetch_server_for_install

    artifact = {"data": {"text": "obscure mcp server nobody knows"}}

    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        with _patch_registry_search(_EMPTY_SEARCH_RESPONSE):
            result = fetch_server_for_install(artifact)

    assert result["source"] == "not_found"
    assert result["server_id"] == ""
    assert result["candidates"] == []


def test_fetch_server_registry_error_source_error(tmp_path):
    """Tier 2: Registry unreachable → source='error', no exception raised."""
    import reyn.registry.cache as cache_mod
    from reyn.stdlib.skills.mcp_install.registry_fetch import fetch_server_for_install

    artifact = {"data": {"text": "github mcp"}}

    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        with _patch_registry_search({}, status=503):
            result = fetch_server_for_install(artifact)

    assert result["source"] == "error"


def test_fetch_server_empty_text_source_error():
    """Tier 2: Empty input text → source='error', no HTTP call made."""
    from reyn.stdlib.skills.mcp_install.registry_fetch import fetch_server_for_install

    call_count = 0

    async def _fake_get(self, path, params=None):
        nonlocal call_count
        call_count += 1
        return {}

    artifact = {"data": {"text": ""}}

    with mock.patch("reyn.registry.client.RegistryClient._get", _fake_get):
        result = fetch_server_for_install(artifact)

    assert result["source"] == "error"
    assert call_count == 0  # No HTTP call for empty input


def test_fetch_server_looks_like_server_id_with_slash():
    """Tier 2: _looks_like_server_id returns True for registry-style identifiers."""
    from reyn.stdlib.skills.mcp_install.registry_fetch import _looks_like_server_id

    assert _looks_like_server_id("io.github.foo/bar-mcp") is True
    assert _looks_like_server_id("ai.smithery/smithery-ai-slack") is True
    assert _looks_like_server_id("filesystem MCP server") is False
    assert _looks_like_server_id("https://github.com/foo/bar") is False  # URL excluded


# ---------------------------------------------------------------------------
# Tier 3a: LLM discover-phase decision — direct server_id path
# ---------------------------------------------------------------------------


@pytest.mark.replay("fixtures/llm/mcp_install/discover_direct.jsonl")
def test_discover_phase_direct_server_id_emits_mcp_install_op():
    """Tier 3a: discover phase emits mcp_install op when server_id is known.

    Scenario: preprocessor resolved the server_id directly (source='direct').
    The LLM should emit a decide turn with a mcp_install control_ir op and
    finish with an mcp_install_result artifact.
    """
    from reyn.llm.llm import call_llm
    from reyn.schemas.models import (
        CandidateOutput,
        ContextFrame,
        ControlIROpSpec,
        ExecutionState,
        PhaseConstraints,
    )
    from reyn.testing.replay import REPLAY_DATETIME

    MODEL = "openai/gemini-2.5-flash-lite"

    candidate_finish = CandidateOutput(
        next_phase="end",
        control_type="finish",
        schema_name="mcp_install_result",
        artifact_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "server_id": {"type": "string"},
                "server_name": {"type": "string"},
                "scope": {"type": "string"},
                "installed_path": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["status", "server_id"],
        },
        description="Return installation result",
    )

    mcp_install_op_spec = ControlIROpSpec(
        kind="mcp_install",
        description=(
            "Install an MCP server from the registry. "
            "Fetches server.json, gates via permission resolver, "
            "prompts for secrets, and writes the server entry to the "
            "appropriate scope config file (local / project / user)."
        ),
        example={
            "kind": "mcp_install",
            "server_id": "io.github.modelcontextprotocol/server-filesystem",
            "scope": "local",
        },
    )

    frame = ContextFrame(
        current_phase="discover",
        current_phase_role="mcp_installer",
        instructions=(
            "The MCP registry has already been queried by the OS preprocessor. "
            "Use the data in data.registry — do NOT call web_fetch or search yourself. "
            "data.registry.source is 'direct' and server_id is set. "
            "Emit a mcp_install op with the server_id and scope='local', "
            "then finish with the mcp_install_result artifact using the op result."
        ),
        candidate_outputs=[candidate_finish],
        finish_criteria=["mcp_install op emitted and result confirmed"],
        constraints=PhaseConstraints(),
        available_control_ops=[mcp_install_op_spec],
        op_catalog=[],
        output_language="ja",
        model=MODEL,
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "filesystem の MCP server をインストールして",
                "registry": {
                    "server_id": "io.github.modelcontextprotocol/server-filesystem",
                    "candidates": [
                        {
                            "name": "io.github.modelcontextprotocol/server-filesystem",
                            "description": "Filesystem MCP server",
                            "repo_url": "https://github.com/modelcontextprotocol/servers",
                        }
                    ],
                    "source": "direct",
                    "query": "filesystem",
                },
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = asyncio.run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="mcp_install",
            skill_description="Install an MCP server from the registry",
            phase_role="mcp_installer",
        )
    )

    data = result.data

    # The LLM should either:
    # (a) emit an act turn with mcp_install ops, OR
    # (b) emit a decide turn finishing with mcp_install_result
    # Both are valid depending on whether it pre-emits the op.
    assert data["type"] in ("act", "decide"), (
        f"Expected 'act' or 'decide' turn, got {data['type']!r}"
    )

    if data["type"] == "act":
        # Act turn: must contain mcp_install op
        ops = data.get("ops", [])
        kinds = [op.get("kind") for op in ops]
        assert "mcp_install" in kinds, (
            f"Act turn must include mcp_install op, got kinds: {kinds}"
        )
    else:
        # Decide (finish) turn: artifact must be mcp_install_result
        ctrl = data["control"]
        assert ctrl["type"] == "finish"
        assert data["artifact"]["type"] == "mcp_install_result"
