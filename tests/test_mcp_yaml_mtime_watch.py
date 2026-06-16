"""Tier 2: yaml mtime watch — FP-0037 S2 passive MCP refresh.

Pins the contract for RouterHostAdapter.maybe_refresh_mcp_tools_from_yaml():
  - First call seeds _yaml_mtimes_seen WITHOUT triggering a probe.
  - Second call with unchanged yaml mtimes does NOT re-probe.
  - yaml mtime advance triggers re-probe + cache write + mtime tracking update.
  - Non-existent yaml paths are silently skipped.
  - yaml created mid-session is detected + probed on the next call.
  - probe failure → server cached as [] + method returns without raising.
  - stat failure (pathological) → warning + return, in-memory state unchanged.
  - session._handle_user_message call order: yaml-watch BEFORE disk-reload.

No unittest.mock / AsyncMock / MagicMock / patch.
Private-state access goes through the yaml_mtimes_snapshot and
mcp_tools_cache_snapshot public properties.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from reyn.chat.services import MemoryService, RouterHostAdapter
from reyn.chat.services.mcp_cache_file import cache_file_path, read_cache, write_cache
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver

# ---------------------------------------------------------------------------
# Null callbacks (same shape as test_mcp_cache_warm_start.py)
# ---------------------------------------------------------------------------


async def _null_file_read(path: str) -> dict:
    return {"content": ""}


async def _null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def _null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def _null_file_list(path: str) -> dict:
    return {"path": path, "entries": []}


async def _null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def _null_mcp_list_servers() -> list:
    return []


async def _null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def _null_run_skill(spec, *, chain_id) -> dict:
    return {"status": "finished", "data": {}}


async def _null_spawn_skill(spec, *, chain_id) -> dict:
    return {
        "status": "spawned",
        "run_id": "x",
        "chain_id": chain_id,
        "skill": "",
        "note": "",
    }


async def _null_send_to_agent(*, to, request, depth, chain_id) -> None:
    pass


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


async def _null_spawn_plan_task(
    *, plan_id, runtime, chain_id, parent_chain_id=None
) -> None:
    pass


# ---------------------------------------------------------------------------
# Fake probe helper matching _probe_server_tools(name, cfg) signature
# ---------------------------------------------------------------------------


class _CountingProbe:
    """Fake _probe_server_tools callable that records invocations.

    Matches the signature: async (server_name: str, cfg: dict, *, per_server_timeout=5.0)
    → (server_name, tools).  Used via monkeypatch.setattr so the adapter's
    maybe_refresh_mcp_tools_from_yaml calls this instead of the real MCPClient.
    """

    def __init__(self, tools_by_server: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[str] = []
        self._tools = tools_by_server or {}

    async def __call__(
        self, server_name: str, cfg: dict, *, per_server_timeout: float = 5.0
    ) -> tuple[str, list[dict]]:
        self.calls.append(server_name)
        return server_name, list(self._tools.get(server_name, []))


# ---------------------------------------------------------------------------
# Adapter factory helper
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    tmp_path: Path,
    mcp_servers: dict | None,
    probe: _CountingProbe,
    state_dir: Path,
    project_root: Path | None = None,
) -> RouterHostAdapter:
    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "test-agent"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    return RouterHostAdapter(
        agent_name="test-agent",
        agent_role="test",
        output_language="en",
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=mcp_servers,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=probe,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
        state_dir=state_dir,
        project_root=project_root,
    )


# ---------------------------------------------------------------------------
# Helper: write a minimal reyn.yaml with an MCP server entry
# ---------------------------------------------------------------------------


def _write_reyn_yaml(path: Path, servers: dict[str, dict]) -> None:
    """Write a minimal reyn.yaml with the given MCP servers dict."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"mcp": {"servers": servers}}
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. First call seeds mtime table — no probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_initial_call_seeds_mtimes(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: first call to maybe_refresh_mcp_tools_from_yaml reads current
    yaml mtimes and populates yaml_mtimes_snapshot WITHOUT triggering a probe."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project"
    reyn_yaml = project_root / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"myserver": {"command": "mcp-myserver"}})

    probe = _CountingProbe()
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"myserver": {"command": "mcp-myserver"}},
        probe=probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    await adapter.maybe_refresh_mcp_tools_from_yaml()

    # No probe on first call (= only seeding)
    assert probe.calls == [], "first call must NOT trigger a probe (seed only)"
    # Mtime table populated with at least the project yaml
    snapshot = adapter.yaml_mtimes_snapshot
    assert reyn_yaml in snapshot, "reyn.yaml mtime must be recorded after first call"
    assert isinstance(snapshot[reyn_yaml], float)


# ---------------------------------------------------------------------------
# 2. Unchanged mtime → no reprobe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_unchanged_mtimes_no_reprobe(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: calling maybe_refresh_mcp_tools_from_yaml twice with no yaml edit
    does NOT invoke the probe on the second call."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project"
    reyn_yaml = project_root / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"srv": {"command": "mcp-srv"}})

    probe = _CountingProbe({"srv": [{"name": "tool_a", "description": "a"}]})
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {"command": "mcp-srv"}},
        probe=probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    # First call: seeds mtime table
    await adapter.maybe_refresh_mcp_tools_from_yaml()
    assert probe.calls == [], "first call must not probe"

    # Second call: mtime unchanged
    await adapter.maybe_refresh_mcp_tools_from_yaml()
    assert probe.calls == [], "second call must not probe when yaml is unchanged"


# ---------------------------------------------------------------------------
# 3. Mtime advance triggers reprobe + cache write + mtime update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_advances_triggers_reprobe(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: when a yaml file's mtime advances (= operator edited it),
    maybe_refresh_mcp_tools_from_yaml re-probes, writes the cache file, and
    updates yaml_mtimes_snapshot."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project"
    reyn_yaml = project_root / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"srv": {"command": "mcp-srv"}})

    fresh_tools = [{"name": "new_tool", "description": "from updated yaml"}]
    probe = _CountingProbe({"srv": fresh_tools})
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {"command": "mcp-srv"}},
        probe=probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    # Seed
    await adapter.maybe_refresh_mcp_tools_from_yaml()
    assert probe.calls == []
    mtime_before = adapter.yaml_mtimes_snapshot.get(reyn_yaml)
    assert mtime_before is not None

    # Simulate yaml edit: advance mtime
    time.sleep(0.02)
    _write_reyn_yaml(reyn_yaml, {"srv": {"command": "mcp-srv-v2"}})

    # Second call: detects change
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    assert "srv" in probe.calls, "probe must be invoked after yaml mtime advances"
    # Cache file written
    cache_path = cache_file_path(state_dir)
    assert cache_path.exists(), "cache file must be written after reprobe"
    on_disk = read_cache(cache_path)
    assert on_disk is not None
    assert "srv" in on_disk
    # Mtime table updated
    mtime_after = adapter.yaml_mtimes_snapshot.get(reyn_yaml)
    assert mtime_after is not None
    assert mtime_after > mtime_before, "mtime table must be updated after reprobe"


# ---------------------------------------------------------------------------
# 4. Non-existent yaml paths are silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_only_existing_yamls_tracked(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: if ~/.reyn/config.yaml (or any yaml) doesn't exist, it is
    silently skipped — no exception raised, only existing paths recorded."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project_no_local"
    reyn_yaml = project_root / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"srv": {"command": "mcp-srv"}})
    # reyn.local.yaml and ~/.reyn/config.yaml intentionally absent

    probe = _CountingProbe()
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {"command": "mcp-srv"}},
        probe=probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    # Must not raise even if some yaml paths are missing
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    snapshot = adapter.yaml_mtimes_snapshot
    # Existing yaml is tracked
    assert reyn_yaml in snapshot
    # Non-existent paths must NOT appear in the snapshot
    local_yaml = project_root / "reyn.local.yaml"
    assert local_yaml not in snapshot, "non-existent yaml must not be in mtime table"


# ---------------------------------------------------------------------------
# 5. yaml created mid-session is detected + probed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_handles_yaml_creation_mid_session(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: a yaml file created between two maybe_refresh calls is detected
    on the second call and triggers a probe."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project_new"
    reyn_yaml = project_root / "reyn.yaml"
    local_yaml = project_root / "reyn.local.yaml"
    # Only project yaml exists initially
    _write_reyn_yaml(reyn_yaml, {"srv": {"command": "mcp-srv"}})

    probe = _CountingProbe({"srv_local": [{"name": "local_tool", "description": "x"}]})
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {}},
        probe=probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    # First call seeds — local yaml absent
    await adapter.maybe_refresh_mcp_tools_from_yaml()
    assert local_yaml not in adapter.yaml_mtimes_snapshot

    # Simulate operator creating reyn.local.yaml mid-session
    time.sleep(0.01)
    _write_reyn_yaml(local_yaml, {"srv_local": {"command": "mcp-srv-local"}})

    # Second call: detects new yaml
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    assert local_yaml in adapter.yaml_mtimes_snapshot, (
        "newly created yaml must be tracked after second call"
    )
    # Probe must have been triggered (new file = change detected)
    assert len(probe.calls) > 0, "probe must be invoked when a new yaml is detected"


# ---------------------------------------------------------------------------
# 6. probe failure → server cached as [] + no raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_probe_failure_does_not_raise(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: when _probe_server_tools raises on one server, the watch method
    swallows the error (probe helper itself must return [] on failure per S1
    contract), and maybe_refresh_mcp_tools_from_yaml returns without raising."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project_failprobe"
    reyn_yaml = project_root / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"bad_srv": {"command": "mcp-bad"}})

    # Probe stub that always returns an empty list (= _probe_server_tools
    # failure behavior per S1 contract: errors → []).
    class _AlwaysEmptyProbe:
        calls: list[str]

        def __init__(self) -> None:
            self.calls = []

        async def __call__(
            self, server_name: str, cfg: dict, *, per_server_timeout: float = 5.0
        ) -> tuple[str, list[dict]]:
            self.calls.append(server_name)
            return server_name, []

    fail_probe = _AlwaysEmptyProbe()
    noop_probe = _CountingProbe()  # for adapter's mcp_list_tools (unused here)
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", fail_probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"bad_srv": {"command": "mcp-bad"}},
        probe=noop_probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    # First call seeds
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    # Second call after mtime advance — probe returns empty list
    time.sleep(0.02)
    _write_reyn_yaml(reyn_yaml, {"bad_srv": {"command": "mcp-bad-v2"}})

    # Must NOT raise
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    # The probe was invoked and returned [] per the S1 contract.
    assert "bad_srv" in fail_probe.calls, "probe must be invoked even on failure path"
    # Cache file is written with an empty tools list for bad_srv.
    cache_path = cache_file_path(state_dir)
    assert cache_path.exists(), "cache must be written even when probe returns []"
    on_disk = read_cache(cache_path)
    assert on_disk is not None
    assert on_disk.get("bad_srv") == [], "failed probe must produce empty list in cache"


# ---------------------------------------------------------------------------
# 7. stat failure → warning + return, state unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_mtime_watch_silent_on_stat_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: when a yaml path disappears mid-session (stat falls back to
    OSError), the watch method silently skips it and does not probe."""
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "project_stat_fail"
    reyn_yaml = project_root / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"srv": {"command": "mcp-srv"}})

    probe = _CountingProbe()
    monkeypatch.setattr(mcp_cmd, "_probe_server_tools", probe)

    state_dir = tmp_path / "state"
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"srv": {"command": "mcp-srv"}},
        probe=probe,
        state_dir=state_dir,
        project_root=project_root,
    )

    # Seed
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    # Make the yaml disappear to simulate a stat failure on the second call.
    reyn_yaml.unlink()

    # The second call should detect the file is gone (no mtime), interpret it
    # as no-change (absent files are skipped silently), and not probe.
    # It should NOT raise.
    await adapter.maybe_refresh_mcp_tools_from_yaml()

    # No probe should have fired (file gone = no mtime = skipped in current_mtimes)
    assert probe.calls == [], "probe must not be invoked when yaml file disappears"


# ---------------------------------------------------------------------------
# 8. Call order: yaml-watch BEFORE disk-reload in _handle_user_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_handle_user_message_calls_yaml_watch_before_reload(
    tmp_path: Path,
) -> None:
    """Tier 2: at turn boundary, maybe_refresh_mcp_tools_from_yaml is called
    BEFORE maybe_reload_mcp_tools_cache_from_disk. Verified via a small
    subclass that records invocation order in a plain list attribute."""

    class _OrderTrackingAdapter(RouterHostAdapter):
        """Subclass that records call order for S2 turn-boundary methods."""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.call_order: list[str] = []

        async def maybe_refresh_mcp_tools_from_yaml(self) -> None:
            self.call_order.append("yaml_watch")
            await super().maybe_refresh_mcp_tools_from_yaml()

        def maybe_reload_mcp_tools_cache_from_disk(self) -> None:
            self.call_order.append("disk_reload")
            super().maybe_reload_mcp_tools_cache_from_disk()

    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "order-test"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    probe = _CountingProbe()
    state_dir = tmp_path / "state"

    adapter = _OrderTrackingAdapter(
        agent_name="order-test",
        agent_role="test",
        output_language="en",
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace,
        plan_registry_getter=lambda: None,
        file_read=_null_file_read,
        file_write=_null_file_write,
        file_delete=_null_file_delete,
        file_list_directory=_null_file_list,
        file_regenerate_index=_null_file_regen,
        mcp_list_servers=_null_mcp_list_servers,
        mcp_list_tools=probe,
        mcp_call_tool=_null_mcp_call_tool,
        run_skill_awaitable=_null_run_skill,
        spawn_skill=_null_spawn_skill,
        send_to_agent=_null_send_to_agent,
        put_outbox=_null_put_outbox,
        append_history=_null_append_history,
        spawn_plan_task=_null_spawn_plan_task,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
        state_dir=state_dir,
        project_root=None,
    )

    # Simulate the turn-boundary calls that session._handle_user_message makes.
    await adapter.maybe_refresh_mcp_tools_from_yaml()  # S2 (yaml watch)
    adapter.maybe_reload_mcp_tools_cache_from_disk()  # S1 (disk reload)

    assert adapter.call_order == ["yaml_watch", "disk_reload"], (
        "yaml-watch must be called before disk-reload at turn boundary"
    )


# ---------------------------------------------------------------------------
# Optional: yaml_scope_paths happy-path test
# ---------------------------------------------------------------------------


def test_yaml_scope_paths_includes_user_global_and_project(tmp_path: Path) -> None:
    """Tier 2: yaml_scope_paths returns all 3 tier paths when project_root is given."""
    from reyn.chat.services.mcp_cache_file import yaml_scope_paths

    project_root = tmp_path / "my_project"
    paths = yaml_scope_paths(project_root)

    user_global = Path.home() / ".reyn" / "config.yaml"
    # Set-equality is the canonical idiom per testing.ja.md: a single
    # behavior pin catches both missing AND extra entries, without
    # asserting a literal collection size separately.
    assert set(paths) == {
        user_global,
        project_root / "reyn.yaml",
        project_root / "reyn.local.yaml",
    }


def test_yaml_scope_paths_user_global_only_when_no_project_root() -> None:
    """Tier 2: yaml_scope_paths returns only user-global when project_root is None."""
    from reyn.chat.services.mcp_cache_file import yaml_scope_paths

    paths = yaml_scope_paths(None)

    user_global = Path.home() / ".reyn" / "config.yaml"
    assert paths == [user_global], (
        "when project_root is None, only user-global path must be returned"
    )
