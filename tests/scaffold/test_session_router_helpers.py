# scaffold: triggered_by="ChatSession から BudgetGateway / MemoryService /
#                          RouterHostAdapter を抽出する follow-up PR"
# scaffold: removed_by="抽出 PR が session-level Tier 2 を再エンコード後、
#                       同 PR で削除"
"""Scaffolding tests for ChatSession internal router helpers.

These tests were Tier 4 in the steady state per the testing policy
(`docs/ja/contributing/testing.md`):
- Use `unittest.mock.patch.object()` to fake `_build_agent`, `_invoke_narrator`,
  and other private session methods.
- Assert directly on private state and helper return values
  (`_get_file_permissions_for_router`, `_get_mcp_servers_for_router`).

They exist as bounded-life characterisation tests during the ChatSession
refactor sequence (PR-refactor-session-1 wave 1+2 already landed; future
extracts of BudgetGateway / MemoryService / RouterHostAdapter will replace
this coverage with public-surface Tier 2 tests, at which point this file
is removed in the same PR as the extraction).

Original docstring (preserved for context):
Tests for Wave 3 F1 — ChatSession router helper methods.

These helpers are added to ChatSession for RouterLoop to consume.
They wrap existing op_runtime file/MCP handlers and expose skill invocation
in an awaitable form.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from reyn.chat.session import ChatSession
from reyn.kernel.runtime import RunResult
from reyn.permissions.permissions import PermissionResolver


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_session(tmp_path: Path, *, mcp_servers: dict | None = None,
                  permission_resolver: PermissionResolver | None = None) -> ChatSession:
    return ChatSession(
        agent_name="test_agent",
        mcp_servers=mcp_servers,
        permission_resolver=permission_resolver,
    )


def _run(coro):
    return asyncio.run(coro)


# ── _run_skill_awaitable ──────────────────────────────────────────────────────


def test_run_skill_awaitable_returns_final_output(tmp_path, monkeypatch):
    """_run_skill_awaitable should run skill, narrate, and return data dict."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    expected_data = {"summary": "all done"}
    fake_result = RunResult(data=expected_data, status="finished")

    async def fake_agent_run(skill, artifact, *, output_language, chain_id):
        return fake_result

    # Stub narrator to return None (falls back to raw dump)
    async def fake_narrator(skill_name, status, result, state_subdir):
        return "narrated reply"

    with patch("reyn.chat.session.resolve_skill_path") as mock_resolve, \
         patch("reyn.chat.session.load_dsl_skill") as mock_load, \
         patch.object(session, "_build_agent") as mock_build_agent, \
         patch.object(session, "_invoke_narrator", new=fake_narrator):

        mock_resolve.return_value = (tmp_path, tmp_path)
        mock_load.return_value = MagicMock()

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=fake_result)
        mock_build_agent.return_value = mock_agent

        result = _run(session._run_skill_awaitable(
            {"skill": "some_skill", "input": {"type": "test_input", "data": {}}},
            chain_id="abc123",
        ))

    assert result["status"] == "finished"
    assert result["data"] == expected_data


def test_run_skill_awaitable_returns_error_on_invalid_spec(tmp_path, monkeypatch):
    """Invalid spec (missing skill name) returns error dict, not exception."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    result = _run(session._run_skill_awaitable({"input": {"type": "x", "data": {}}}, chain_id="c1"))
    assert result["status"] == "error"
    assert "error" in result["data"]


def test_run_skill_awaitable_respects_allowlist(tmp_path, monkeypatch):
    """Skill not in allowlist is refused with error dict."""
    monkeypatch.chdir(tmp_path)
    session = ChatSession(agent_name="test_agent", allowed_skills=["allowed_skill"])

    result = _run(session._run_skill_awaitable(
        {"skill": "forbidden_skill", "input": {"type": "x", "data": {}}},
        chain_id="c1",
    ))
    assert result["status"] == "error"
    assert "allowed_skills" in result["data"]["error"]


# ── _run_remember ──────────────────────────────────────────────────────────────


def test_run_remember_shared_writes_file_and_index(tmp_path, monkeypatch):
    """_run_remember with layer=shared writes body + regenerates MEMORY.md."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    result = _run(session._run_remember(
        layer="shared",
        slug="project_test",
        name="Test Memory",
        description="A test memory entry",
        type="project",
        body="This is the body of the memory.",
    ))

    assert result.get("saved") == "project_test"
    assert result.get("layer") == "shared"

    body_file = tmp_path / ".reyn" / "memory" / "project_test.md"
    assert body_file.exists(), "body file should be created"

    content = body_file.read_text(encoding="utf-8")
    assert "Test Memory" in content
    assert "A test memory entry" in content
    assert "This is the body of the memory." in content

    index_file = tmp_path / ".reyn" / "memory" / "MEMORY.md"
    assert index_file.exists(), "MEMORY.md should be regenerated"
    index_content = index_file.read_text(encoding="utf-8")
    assert "project_test" in index_content


def test_run_remember_agent_uses_agent_path(tmp_path, monkeypatch):
    """_run_remember with layer=agent writes to the agent-scoped memory dir."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    result = _run(session._run_remember(
        layer="agent",
        slug="feedback_test",
        name="Agent Feedback",
        description="Agent-scoped feedback",
        type="feedback",
        body="Remember this for agent only.",
    ))

    assert result.get("saved") == "feedback_test"
    assert result.get("layer") == "agent"

    body_file = (
        tmp_path / ".reyn" / "agents" / "test_agent" / "memory" / "feedback_test.md"
    )
    assert body_file.exists(), "agent-scoped body file should be created"

    index_file = (
        tmp_path / ".reyn" / "agents" / "test_agent" / "memory" / "MEMORY.md"
    )
    assert index_file.exists(), "agent MEMORY.md should be regenerated"


# ── _read_memory_body ──────────────────────────────────────────────────────────


def test_read_memory_body_returns_content(tmp_path, monkeypatch):
    """Pre-write a memory body, then read it back."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    # Pre-write directly
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    body_file = mem_dir / "my_slug.md"
    body_file.write_text("---\nname: My Entry\n---\nsome content", encoding="utf-8")

    result = _run(session._read_memory_body(layer="shared", slug="my_slug"))

    assert result.get("layer") == "shared"
    assert result.get("slug") == "my_slug"
    assert "some content" in result.get("content", "")


def test_read_memory_body_returns_error_when_missing(tmp_path, monkeypatch):
    """Reading a non-existent slug returns {"error": ...}."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    result = _run(session._read_memory_body(layer="shared", slug="nonexistent"))
    assert "error" in result


# ── _run_forget ────────────────────────────────────────────────────────────────


def test_run_forget_deletes_and_regenerates(tmp_path, monkeypatch):
    """Create a memory entry then forget it — file deleted, index regenerated."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    # First remember
    _run(session._run_remember(
        layer="shared",
        slug="to_delete",
        name="Delete Me",
        description="Ephemeral",
        type="project",
        body="Temporary body.",
    ))

    body_file = tmp_path / ".reyn" / "memory" / "to_delete.md"
    assert body_file.exists()

    result = _run(session._run_forget(layer="shared", slug="to_delete"))

    assert result.get("deleted") == "to_delete"
    assert result.get("layer") == "shared"
    assert not body_file.exists(), "body file should be deleted"


def test_run_forget_returns_error_when_not_found(tmp_path, monkeypatch):
    """Forgetting a slug that doesn't exist returns {"error": ...}."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    result = _run(session._run_forget(layer="shared", slug="ghost_slug"))
    assert "error" in result


# ── file ops ──────────────────────────────────────────────────────────────────


def test_file_read_under_permission_succeeds(tmp_path, monkeypatch):
    """Read a file within project root — always allowed."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    test_file = tmp_path / "hello.txt"
    test_file.write_text("hello world", encoding="utf-8")

    result = _run(session._file_read("hello.txt"))
    assert result.get("content") == "hello world"
    assert result.get("path") == "hello.txt"


def test_file_read_outside_permission_returns_error(tmp_path, monkeypatch):
    """Reading a path outside project root with no permission returns error."""
    monkeypatch.chdir(tmp_path)
    # No PermissionResolver means only CWD is allowed
    session = _make_session(tmp_path)

    # /tmp/nonexistent_outside is outside CWD; Workspace will raise PermissionError
    result = _run(session._file_read("/nonexistent_outside_path/file.txt"))
    assert "error" in result


def test_file_write_and_read_roundtrip(tmp_path, monkeypatch):
    """Write then read a file through the helper methods."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    write_result = _run(session._file_write("testdir/note.txt", "hello"))
    assert write_result.get("written") is True

    read_result = _run(session._file_read("testdir/note.txt"))
    assert read_result.get("content") == "hello"


def test_file_delete_removes_file(tmp_path, monkeypatch):
    """_file_delete removes a created file."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    (tmp_path / "to_del.txt").write_text("x", encoding="utf-8")
    result = _run(session._file_delete("to_del.txt"))
    assert result.get("deleted") is True
    assert not (tmp_path / "to_del.txt").exists()


# ── _get_file_permissions_for_router ──────────────────────────────────────────


def test_get_file_permissions_for_router_shape(tmp_path, monkeypatch):
    """Verify _get_file_permissions_for_router returns expected dict shape."""
    monkeypatch.chdir(tmp_path)
    perm = PermissionResolver(
        config_permissions={"file.read": "allow", "file.write": "allow"},
        project_root=tmp_path,
    )
    session = _make_session(tmp_path, permission_resolver=perm)

    result = session._get_file_permissions_for_router()
    assert result is not None
    assert "read" in result
    assert "write" in result
    assert isinstance(result["read"], list)
    assert isinstance(result["write"], list)
    assert "*" in result["read"]
    assert "*" in result["write"]


def test_get_file_permissions_for_router_returns_none_when_no_perm(tmp_path, monkeypatch):
    """Returns None when no PermissionResolver is set."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, permission_resolver=None)
    assert session._get_file_permissions_for_router() is None


def test_get_file_permissions_for_router_returns_none_when_no_file_perm(tmp_path, monkeypatch):
    """Returns None when resolver has no file.read / file.write config."""
    monkeypatch.chdir(tmp_path)
    perm = PermissionResolver(config_permissions={}, project_root=tmp_path)
    session = _make_session(tmp_path, permission_resolver=perm)
    assert session._get_file_permissions_for_router() is None


# ── _get_mcp_servers_for_router ───────────────────────────────────────────────


def test_get_mcp_servers_for_router_filters_by_permission(tmp_path, monkeypatch):
    """_get_mcp_servers_for_router returns entries for configured servers."""
    monkeypatch.chdir(tmp_path)
    mcp_servers = {
        "server_a": {"url": "http://localhost:3001/mcp", "description": "Server A"},
        "server_b": {"url": "http://localhost:3002/mcp", "description": "Server B"},
    }
    session = _make_session(tmp_path, mcp_servers=mcp_servers)

    result = session._get_mcp_servers_for_router()
    assert isinstance(result, list)
    names = {item["name"] for item in result}
    assert "server_a" in names
    assert "server_b" in names
    # Check description is included
    a_entry = next(item for item in result if item["name"] == "server_a")
    assert a_entry["description"] == "Server A"


def test_get_mcp_servers_for_router_empty_when_none(tmp_path, monkeypatch):
    """Returns [] when no MCP servers configured."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    assert session._get_mcp_servers_for_router() == []


# ── _make_router_op_context PermissionDecl populate (PR36 Layer 3b) ──────────


def test_make_router_op_context_populates_file_perms(tmp_path, monkeypatch):
    """PermissionDecl.file_read/file_write populated from agent config."""
    monkeypatch.chdir(tmp_path)
    perm = PermissionResolver(
        config_permissions={"file.read": [{"path": "src", "scope": "recursive"}],
                            "file.write": []},
        project_root=tmp_path,
    )
    session = _make_session(tmp_path, permission_resolver=perm)

    ctx = session._make_router_op_context()

    decl = ctx.permission_decl
    assert decl.file_read, "file_read should be non-empty"
    assert any(e["path"] == "src" for e in decl.file_read), (
        f"expected 'src' in file_read, got {decl.file_read}"
    )
    assert decl.file_write == [], f"expected empty file_write, got {decl.file_write}"


def test_make_router_op_context_populates_mcp_servers(tmp_path, monkeypatch):
    """PermissionDecl.mcp populated from configured MCP servers."""
    monkeypatch.chdir(tmp_path)
    mcp_servers = {
        "my_server": {"url": "http://localhost:3001/mcp", "description": "Test"},
    }
    session = _make_session(tmp_path, mcp_servers=mcp_servers)

    ctx = session._make_router_op_context()

    assert "my_server" in ctx.permission_decl.mcp, (
        f"expected 'my_server' in decl.mcp, got {ctx.permission_decl.mcp}"
    )


def test_make_router_op_context_empty_when_no_perms(tmp_path, monkeypatch):
    """Minimal session with no permissions yields empty (not None) decl lists."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    ctx = session._make_router_op_context()

    decl = ctx.permission_decl
    assert decl.file_read == [], f"expected [], got {decl.file_read}"
    assert decl.file_write == [], f"expected [], got {decl.file_write}"
    assert decl.mcp == [], f"expected [], got {decl.mcp}"


def test_router_file_read_blocked_by_decl(tmp_path, monkeypatch):
    """File read outside allowed scope is blocked when decl restricts paths.

    Layer 3a + 3b integration test: PermissionResolver.is_read_allowed gates
    reads outside CWD even without Layer 3a op_runtime changes.
    """
    monkeypatch.chdir(tmp_path)
    # Session with read scope limited to "src" — /etc/passwd is outside
    perm = PermissionResolver(
        config_permissions={"file.read": [{"path": str(tmp_path / "src"), "scope": "recursive"}]},
        project_root=tmp_path,
        interactive=False,
    )
    session = _make_session(tmp_path, permission_resolver=perm)

    result = _run(session._file_read("/etc/passwd"))
    # Expect an error result — op_runtime should deny the read
    assert "error" in result, (
        f"Expected error for out-of-scope read, got: {result}"
    )


# ── _memory_path / _memory_dir ────────────────────────────────────────────────


def test_memory_path_shared(tmp_path, monkeypatch):
    """_memory_path with layer=shared returns .reyn/memory/<slug>.md."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    path = session._memory_path("shared", "my_slug")
    assert ".reyn/memory/my_slug.md" in path.replace("\\", "/")


def test_memory_path_agent(tmp_path, monkeypatch):
    """_memory_path with layer=agent returns agent-scoped path."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    path = session._memory_path("agent", "my_slug")
    assert "agents/test_agent/memory/my_slug.md" in path.replace("\\", "/")


# ── PR37: allowed_mcp ─────────────────────────────────────────────────────────


def test_allowed_mcp_loaded_from_profile(tmp_path, monkeypatch):
    """Profile.yaml with allowed_mcp: [filesystem] is parsed into AgentProfile."""
    monkeypatch.chdir(tmp_path)
    import yaml
    from reyn.chat.profile import AgentProfile

    agent_dir = tmp_path / ".reyn" / "agents" / "test_agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    profile_path = agent_dir / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump({
            "name": "test_agent",
            "role": "test role",
            "created_at": "2026-05-02T00:00:00+00:00",
            "allowed_skills": ["read_local_files"],
            "allowed_mcp": ["filesystem"],
        }, allow_unicode=True),
        encoding="utf-8",
    )

    profile = AgentProfile.load(agent_dir)
    assert profile.allowed_mcp == ["filesystem"]


def test_allowed_mcp_in_permission_decl(tmp_path, monkeypatch):
    """ChatSession constructed with allowed_mcp=[filesystem] sets it in the PermissionDecl."""
    monkeypatch.chdir(tmp_path)
    session = ChatSession(
        agent_name="test_agent",
        allowed_mcp=["filesystem"],
    )
    ctx = session._make_router_op_context()
    assert ctx.permission_decl.allowed_mcp == ["filesystem"]


def test_allowed_mcp_none_means_no_restriction(tmp_path, monkeypatch):
    """allowed_mcp=None means no per-agent restriction; decl.allowed_mcp is None."""
    monkeypatch.chdir(tmp_path)
    session = ChatSession(agent_name="test_agent")
    ctx = session._make_router_op_context()
    assert ctx.permission_decl.allowed_mcp is None


def test_allowed_mcp_all_string_normalized_to_none(tmp_path, monkeypatch):
    """Profile with allowed_mcp: all normalizes to None (no restriction)."""
    monkeypatch.chdir(tmp_path)
    import yaml
    from reyn.chat.profile import AgentProfile

    agent_dir = tmp_path / ".reyn" / "agents" / "test_agent2"
    agent_dir.mkdir(parents=True, exist_ok=True)
    profile_path = agent_dir / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump({
            "name": "test_agent2",
            "role": "",
            "created_at": "2026-05-02T00:00:00+00:00",
            "allowed_mcp": "all",
        }, allow_unicode=True),
        encoding="utf-8",
    )

    profile = AgentProfile.load(agent_dir)
    assert profile.allowed_mcp is None, (
        "allowed_mcp='all' should normalize to None (no restriction)"
    )
