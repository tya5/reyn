"""Tier 2: #3520 — a non-answering MCP tools probe must not be stored as an answer.

The defect: the tools cache was typed ``dict[str, list[dict]]``, so "this
server exposes zero tools" and "this server could not be measured" were the
same ``[]``. Because the cache is permanent by design (probing is expensive,
catalogs are near-static within a session), that non-answer then outlived the
condition that produced it — one slow probe removed a server's tools from the
model's capability surface for the rest of the session, and once the value
reached ``.reyn/state/mcp_tools_cache.json``, for every session after a
restart too.

**What these tests witness is the `tools=` payload the LLM receives**, not that
the mechanism ran. The mechanism-level facts (a `mcp_tool_probe_degraded`
audit-event fires; the cache dict has/hasn't a key) are all downstream
proxies; the thing the issue is about is whether the model is told about
capabilities it actually has, and that is the ``mcp_tool_name`` enum inside
``build_tools(...)`` output. `build_tools(mcp_servers=host.get_mcp_servers())`
is the exact expression `RouterLoop` uses to assemble `tools=`, so these tests
call it with the same input, not a paraphrase of it. (#5291: available_agents
removed from build_tools's own signature — 0 real consumers.)

Three surfaces, one defect — a fix that misses any of them leaves it alive:
  1. the in-memory cache          → `test_payload_*`
  2. the on-disk cache            → `test_disk_*` (an unknown written here
                                     survives a process restart)
  3. the CLI writer               → `test_cli_*` (`reyn mcp refresh` shares
                                     the same file, so fixing only the runtime
                                     lets a CLI run re-introduce it)

Policy: real instances only — a real `RouterHostAdapter`, the real
`build_tools`, the real `_probe_server_tools` against a real dying
subprocess. No `unittest.mock` / `MagicMock` / `AsyncMock` / `patch`.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.router_tools import build_tools
from reyn.runtime.services import (
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
)
from reyn.runtime.services.mcp_cache_file import cache_file_path, read_cache

_SERVER = "reyn_markitdown"
_TOOL = "convert_to_markdown"
_TOOLS = [{"name": _TOOL, "description": "convert a uri to markdown"}]

from tests._support.router_host_adapter import make_op_context_source  # noqa: E402

_EMPTY_OP_CTX = make_op_context_source()
_EMPTY_MCP_GATEWAY = McpGatewayInputs(
    mcp_connection_service=None, mcp_agent_id=None, ephemeral_fn=None,
)


# ---------------------------------------------------------------------------
# Null callbacks + adapter factory (same shape as test_mcp_cache_warm_start.py)
# ---------------------------------------------------------------------------


async def _null_file_read(path: str) -> dict:
    return {"content": ""}


async def _null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def _null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def _null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def _null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def _null_put_outbox(msg) -> None:
    pass


def _null_append_history(msg) -> None:
    pass


class _FlakyProbe:
    """A real async probe that hangs while ``healthy`` is False.

    Not a mock: a plain callable whose behaviour is a function of its own
    public attribute, standing in for the real-world condition the issue
    describes (a server that is too slow under co-located CPU load on one
    turn and fine on the next).
    """

    def __init__(self) -> None:
        self.healthy = False
        self.calls: list[str] = []

    async def __call__(self, server_name: str) -> list[dict]:
        self.calls.append(server_name)
        if not self.healthy:
            await asyncio.sleep(5.0)  # far beyond any timeout these tests pass
        return [dict(t) for t in _TOOLS]


def _make_adapter(*, tmp_path: Path, state_dir: Path, probe) -> RouterHostAdapter:
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
    adapter = RouterHostAdapter(
        agent_name="test-agent",
        agent_role="test",
        output_language="en",
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=None,
        mcp_servers={_SERVER: {"description": "markitdown"}},
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        state_dir=state_dir,
        universal_wrappers_enabled=False,  # #4159: preserves prior implicit default
    )
    # #3447 / existing convention in this suite: mcp_list_tools is a real
    # RouterHostAdapter method, so the probe is wired by shadowing that one
    # bound method on a real, cheaply-constructed instance (a real callable,
    # not a patch).
    adapter.mcp_list_tools = probe
    return adapter


def _mcp_tool_enum(adapter: RouterHostAdapter) -> "list[str] | None":
    """The `mcp_tool_name` enum as the LLM would receive it in `tools=`.

    Reproduces `RouterLoop`'s own assembly expression —
    ``build_tools(mcp_servers=self.host.get_mcp_servers())`` (#5291:
    available_agents removed from the signature) — so what is asserted is
    the payload content, not an intermediate the payload happens to be
    derived from. Returns None when the field carries no enum at all
    (= the schema degrades to a free-form string and the model is told
    nothing about which tools exist).
    """
    tools = build_tools(mcp_servers=adapter.get_mcp_servers())
    for entry in tools:
        if entry.get("type") == "function" and entry["function"]["name"] == "call_mcp_tool":
            props = entry["function"]["parameters"]["properties"]
            return props["mcp_tool_name"].get("enum")
    raise AssertionError(f"call_mcp_tool absent from the tools= payload: {tools!r}")


# ---------------------------------------------------------------------------
# 1. In-memory surface — the payload the model receives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_regains_the_mcp_tool_enum_after_a_probe_that_timed_out(
    tmp_path: Path,
) -> None:
    """Tier 2: a timed-out probe costs the enum for that turn ONLY — the next
    turn re-probes and the enum returns to the `tools=` payload.

    This is #3520's whole point stated in terms of what the LLM reads. Before
    the fix the second assertion was unreachable: the timed-out probe was
    stored as ``[]``, the populated-guard saw a non-None cache and returned,
    and the enum stayed missing for the life of the session no matter how
    healthy the server became.
    """
    state_dir = tmp_path / "state"
    probe = _FlakyProbe()
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=probe)

    # Turn 1 — the server is too slow for the deadline.
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert _mcp_tool_enum(adapter) is None, (
        "a probe that did not answer cannot contribute tools to the enum"
    )

    # Turn 2 — the same server, now healthy. Simulate the retry window
    # elapsing without sleeping; the next call must re-probe.
    probe.healthy = True
    adapter._mcp_probe_cooldown_until[_SERVER] = 0.0
    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    enum = _mcp_tool_enum(adapter)
    assert enum is not None and f"{_SERVER}.{_TOOL}" in enum, (
        "after the server answered, its tool must appear in the mcp_tool_name "
        f"enum the LLM receives; got {enum!r}"
    )


@pytest.mark.asyncio
async def test_an_answered_catalog_is_not_reprobed_on_later_turns(
    tmp_path: Path,
) -> None:
    """Tier 2: permanence is preserved for ANSWERS — a server that replied is
    probed once and reused.

    The fix must not be "re-probe every turn"; that would trade this defect for
    a per-turn latency defect. The distinction it actually draws is
    answer-vs-unknown, and this pins the answer half: no second probe, and the
    enum still in the payload.
    """
    state_dir = tmp_path / "state"
    probe = _FlakyProbe()
    probe.healthy = True
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=probe)

    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    calls_after_first = list(probe.calls)
    assert calls_after_first, "the first turn must probe"

    await adapter.ensure_mcp_tools_cached(per_server_timeout=5.0)
    assert probe.calls == calls_after_first, (
        "a server that already answered must not be re-probed on a later turn"
    )
    enum = _mcp_tool_enum(adapter)
    assert enum is not None and f"{_SERVER}.{_TOOL}" in enum


# ---------------------------------------------------------------------------
# 2. On-disk surface — an unknown must not survive a restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_probe_failure_is_backed_off_until_retry_window(
    tmp_path: Path,
) -> None:
    """Tier 2: a persistent probe failure is not retried on the next turn."""
    state_dir = tmp_path / "state"
    probe = _FlakyProbe()
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=probe)

    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert probe.calls == [_SERVER]
    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert probe.calls == [_SERVER]


@pytest.mark.asyncio
async def test_disk_cache_never_records_a_probe_that_did_not_answer(
    tmp_path: Path,
) -> None:
    """Tier 2: a non-answering probe leaves no entry in the on-disk cache.

    The disk file is the surface that makes the defect outlive the process.
    Asserting on the file directly (not on the in-memory dict) is what
    distinguishes "we stopped storing it in RAM" from "we stopped storing it".
    """
    state_dir = tmp_path / "state"
    probe = _FlakyProbe()
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=probe)

    await adapter.ensure_mcp_tools_cached(per_server_timeout=0.05)

    cache_path = cache_file_path(state_dir)
    on_disk = read_cache(cache_path) if cache_path.exists() else None
    assert on_disk is None or _SERVER not in on_disk, (
        "a probe that did not answer must not reach the cache file — written "
        f"there it would outlive the process that produced it; got {on_disk!r}"
    )


@pytest.mark.asyncio
async def test_a_restarted_session_regains_the_enum_after_an_earlier_timeout(
    tmp_path: Path,
) -> None:
    """Tier 2: a fresh adapter over the SAME state_dir gets the enum back.

    The restart face of #3520: the first adapter's probe timed out, the second
    adapter warm-starts from whatever the first left on disk. If a `[]` had
    been persisted, this second adapter would read it as "measured: no tools",
    skip the probe, and hand the model a payload with no enum — permanently,
    across every future restart, with no operator action that would ever
    dislodge it short of deleting the file.
    """
    state_dir = tmp_path / "state"

    first_probe = _FlakyProbe()
    first = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=first_probe)
    await first.ensure_mcp_tools_cached(per_server_timeout=0.05)
    assert _mcp_tool_enum(first) is None

    # "Restart": a brand-new adapter, same state_dir, healthy server.
    second_probe = _FlakyProbe()
    second_probe.healthy = True
    second = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=second_probe)
    await second.ensure_mcp_tools_cached(per_server_timeout=5.0)

    enum = _mcp_tool_enum(second)
    assert enum is not None and f"{_SERVER}.{_TOOL}" in enum, (
        "a restarted session must live-probe a server the previous session "
        f"could not measure, and the enum must come back; got {enum!r}"
    )


# ---------------------------------------------------------------------------
# 3. CLI surface — `reyn mcp refresh` writes the same file
# ---------------------------------------------------------------------------


def test_cli_refresh_omits_a_dead_server_from_the_shared_cache_file(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: `reyn mcp refresh` against a server that dies on startup writes
    no entry for it, and the runtime then re-probes it.

    Surface 3. The CLI and the runtime write the SAME
    ``.reyn/state/mcp_tools_cache.json``, so a fix confined to the runtime
    would be undone by one operator running `reyn mcp refresh` while a server
    happened to be down: the `[]` it wrote would be read back by every session
    as an answer. The probe here is the REAL ``_probe_server_tools`` against a
    real subprocess that exits non-zero — no stub stands between the assertion
    and the code path an operator actually runs.
    """
    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("", encoding="utf-8")

    dead_cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(1)"],
    }
    # Config plumbing only — the probe itself is the real one.
    monkeypatch.setattr(
        mcp_cmd,
        "_all_servers_with_scope",
        lambda root: [(_SERVER, "project", dead_cfg)],
    )
    monkeypatch.setattr(mcp_cmd, "_get_project_root", lambda: project_root)

    import argparse
    mcp_cmd.run_refresh(argparse.Namespace(project=None, func=mcp_cmd.run_refresh))

    err = capsys.readouterr().err.lower()
    assert "warning" in err, (
        "the operator must be told the server did not answer, not left to "
        f"infer it from a missing tool; stderr was {err!r}"
    )

    state_dir = project_root / ".reyn" / "state"
    on_disk = read_cache(cache_file_path(state_dir))
    assert on_disk is not None, "refresh must still write the cache file"
    assert _SERVER not in on_disk, (
        "a dead server must be OMITTED from the file the CLI writes, never "
        f"written as an empty list; got {on_disk!r}"
    )


def test_a_session_reading_the_cli_written_cache_still_gets_the_enum(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: end-to-end across surfaces 3 → 2 → 1.

    `reyn mcp refresh` fails against a dead server, then a session pointed at
    the same state directory (with the server now healthy) hands the model a
    payload that HAS the enum. This is the composition the two surfaces exist
    to protect: the CLI writes, the runtime reads, and the LLM-visible result
    is what is asserted.

    Synchronous on purpose: ``run_refresh`` owns its own event loop
    (``asyncio.run``), so this test may not already be inside one.
    """
    import argparse

    import reyn.interfaces.cli.commands.mcp as mcp_cmd

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("", encoding="utf-8")

    dead_cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(1)"],
    }
    monkeypatch.setattr(
        mcp_cmd,
        "_all_servers_with_scope",
        lambda root: [(_SERVER, "project", dead_cfg)],
    )
    monkeypatch.setattr(mcp_cmd, "_get_project_root", lambda: project_root)
    mcp_cmd.run_refresh(argparse.Namespace(project=None, func=mcp_cmd.run_refresh))

    state_dir = project_root / ".reyn" / "state"
    probe = _FlakyProbe()
    probe.healthy = True
    adapter = _make_adapter(tmp_path=tmp_path, state_dir=state_dir, probe=probe)
    asyncio.run(adapter.ensure_mcp_tools_cached(per_server_timeout=5.0))

    enum = _mcp_tool_enum(adapter)
    assert enum is not None and f"{_SERVER}.{_TOOL}" in enum, (
        "a session must live-probe a server the CLI could not measure; the "
        f"enum must reach the LLM payload; got {enum!r}"
    )
