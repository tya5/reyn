"""Tier 2: OS invariant — ``reyn pipe`` CLI subcommands (list/install/run).

Covers:
  - argparse registration (happy-path parse for list/install/run).
  - ``reyn pipe list``: LOAD STATUS distinguishes a working entry from a
    deliberately-broken one (real ``build_pipeline_registry``, real tmp_path
    files, no mocks — #2641's per-entry-isolation posture surfaced visibly).
  - ``reyn pipe install --path``: real local DSL file, driven through the
    CLI's own ``run_install`` with a real ``argparse.Namespace`` (matching
    ``test_mcp_source_install.py``'s own drive-through-run(args) shape) —
    asserts ``.reyn/config/pipelines.yaml`` gets the correct entry.
  - ``reyn pipe run NAME``: a real registered transform-only pipeline
    executed end-to-end via ``run_run``, asserting the printed JSON result.
  - ``reyn pipe run`` on a pipeline reaching a ``tool:`` step: real dispatch
    through a real, standalone ``ToolContext`` (a real side-effect-free test
    tool registered into the real ``ToolRegistry``, mirroring the IS-4
    inline-pipeline tests' ``_install_write_tool`` idiom) — no more blanket
    refusal.
  - ``reyn pipe run`` on a pipeline reaching an ``agent:`` step: a real
    ``AgentRegistry``/``Session``/``MessageBus`` ephemeral spawn under the
    ``default`` identity, with ONLY the LLM completion call faked (the
    documented ``litellm.acompletion`` replay seam — see
    ``test_llm_request_event_1669.py``), asserting the pipeline's final
    output reflects the (faked) LLM reply.
  - fail-closed-by-default permissions: a ``tool:`` step writing outside the
    default write zone via the real, shipped ``write_file`` tool is DENIED
    without ``--grant-file-write``, and succeeds with it — byte-identical to
    ``reyn chat``'s own no-flag/``--grant-file-write`` posture.
  - regression (bug fix): a ``tool:`` step calling a first-class
    ``mcp__<server>__<tool>`` action now genuinely resolves and dispatches —
    the ``router_state=None`` gap ("caveat-1" in ``runtime/router_loop.py``)
    silently dropped every resource-backed catalog category (mcp/agents/
    available_skills/rag_corpus/sandbox_backend). Only the MCP CLIENT's
    transport is faked (``reyn.mcp.pool.MCPClient``, mirroring
    ``test_2421_gateway_acceptance.py``'s existing fixture convention) — the
    resolution/dispatch/permission-gate machinery all runs for real.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import litellm
import pytest
import yaml

from reyn.interfaces.cli.commands.pipe import register, run_install, run_list, run_run

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def _write_reyn_yaml(project_root: Path, pipelines_entries: dict | None = None) -> None:
    # A real litellm-recognizable model string for 'standard' (mirrors a real
    # project's reyn.yaml) — avoids litellm's own unrecognized-provider banner
    # printing to stdout, which would otherwise corrupt 'reyn pipe run's JSON
    # output the moment a 'tool:' step lazily constructs a real Session (the
    # router_state fix's source of a RouterHostAdapter — see run_run).
    data: dict = {"model": "standard", "models": {"standard": "openai/gpt-4o-mini"}}
    if pipelines_entries is not None:
        data["pipelines"] = {"entries": pipelines_entries}
    (project_root / "reyn.yaml").write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8",
    )


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# Argparse registration
# ---------------------------------------------------------------------------


def test_pipe_list_parses():
    """Tier 2: 'pipe list' parses with no extra args."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "list"])
    assert args.pipe_command == "list"


def test_pipe_install_parses_path():
    """Tier 2: 'pipe install --path FILE' parses; --source/--name default None."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "install", "--path", "p.yaml", "--non-interactive"])
    assert args.pipe_command == "install"
    assert args.path == "p.yaml"
    assert args.source is None
    assert args.name is None
    assert args.non_interactive is True


def test_pipe_install_parses_source():
    """Tier 2: 'pipe install --source URL' parses."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "install", "--source", "https://github.com/x/y"])
    assert args.source == "https://github.com/x/y"


def test_pipe_run_parses():
    """Tier 2: 'pipe run NAME --input JSON' parses; --async is present but
    suppressed; --grant-file-write defaults to False (fail-closed)."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "run", "my_pipeline", "--input", '{"a": 1}'])
    assert args.pipe_command == "run"
    assert args.name == "my_pipeline"
    assert args.input == '{"a": 1}'
    assert args.async_ is False
    assert args.grant_file_write is False


def test_pipe_run_grant_file_write_flag_parses():
    """Tier 2: '--grant-file-write' parses to True (opt-in, same flag name/
    semantics as `reyn chat --grant-file-write`)."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "run", "my_pipeline", "--grant-file-write"])
    assert args.grant_file_write is True


def test_pipe_run_async_flag_parses_but_is_rejected_at_runtime(capsys):
    """Tier 2: '--async' parses (so argparse doesn't hard-reject it) but
    run_run() refuses it with a clear message rather than silently accepting."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "run", "my_pipeline", "--async"])
    assert args.async_ is True
    with pytest.raises(SystemExit) as exc_info:
        run_run(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--async" in err
    assert "not supported" in err.lower()


# ---------------------------------------------------------------------------
# reyn pipe list
# ---------------------------------------------------------------------------


def test_list_shows_loaded_and_failed_entries(tmp_path, monkeypatch, capsys):
    """Tier 2: a working entry shows 'loaded'; a broken one (missing file)
    shows 'FAILED' — #2641's per-entry isolation surfaced visibly by the CLI."""
    monkeypatch.chdir(tmp_path)

    working_dsl = (
        "pipeline: good_one\n"
        "description: a working pipeline\n"
        "steps:\n"
        "  - transform: {value: \"ctx.x\", output: y}\n"
    )
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "good.yaml").write_text(working_dsl, encoding="utf-8")

    _write_reyn_yaml(
        tmp_path,
        {
            "good_one": {"path": "pipelines/good.yaml", "description": "a working pipeline"},
            "missing_one": {"path": "pipelines/does_not_exist.yaml"},
        },
    )

    run_list(_ns())

    out = capsys.readouterr().out
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln and not ln.startswith("─")}
    assert "loaded" in lines["good_one"]
    assert "FAILED" in lines["missing_one"]


def test_list_no_project_root_prints_message(tmp_path, monkeypatch, capsys):
    """Tier 2: outside any project (no reyn.yaml reachable), list prints a
    clear message instead of crashing."""
    monkeypatch.chdir(tmp_path)
    run_list(_ns())
    out = capsys.readouterr().out
    assert "no reyn.yaml" in out.lower()


def test_list_no_pipelines_configured(tmp_path, monkeypatch, capsys):
    """Tier 2: a project with reyn.yaml but no pipelines.entries reports zero
    configured pipelines rather than an empty/confusing table."""
    monkeypatch.chdir(tmp_path)
    _write_reyn_yaml(tmp_path)
    run_list(_ns())
    out = capsys.readouterr().out
    assert "no pipelines configured" in out.lower()


# ---------------------------------------------------------------------------
# reyn pipe install --path
# ---------------------------------------------------------------------------


def test_install_local_path_writes_config(tmp_path, capsys):
    """Tier 2: 'pipe install --path FILE' registers the DSL's declared name
    into .reyn/config/pipelines.yaml via the real pipeline_install op handler."""
    _write_reyn_yaml(tmp_path)

    dsl_path = tmp_path / "my_pipeline.yaml"
    dsl_path.write_text(
        "pipeline: my_pipeline\n"
        "description: installed via CLI\n"
        "steps:\n"
        "  - transform: {value: \"ctx.x\", output: y}\n",
        encoding="utf-8",
    )

    args = _ns(
        path=str(dsl_path), source=None, name=None,
        project=str(tmp_path), non_interactive=True,
    )
    run_install(args)

    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    assert config_path.exists()
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = written["pipelines"]["entries"]["my_pipeline"]
    assert entry["path"] == str(dsl_path.resolve())
    assert entry["description"] == "installed via CLI"
    assert entry["enabled"] is True

    out = capsys.readouterr().out
    assert "installed successfully" in out.lower()


def test_install_name_mismatch_is_a_clean_error(tmp_path, capsys):
    """Tier 2: a --name that disagrees with the DSL's declared 'pipeline:'
    name is refused with a clear message (pipeline_install.py's own
    validation), not a raw exception leaking to the CLI user."""
    _write_reyn_yaml(tmp_path)

    dsl_path = tmp_path / "p.yaml"
    dsl_path.write_text(
        "pipeline: real_name\nsteps:\n  - transform: {value: \"ctx.x\", output: y}\n",
        encoding="utf-8",
    )

    args = _ns(
        path=str(dsl_path), source=None, name="different_name",
        project=str(tmp_path), non_interactive=True,
    )
    with pytest.raises(SystemExit) as exc_info:
        run_install(args)
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "name mismatch" in err.lower() or "does not match" in err.lower()


def test_install_neither_path_nor_source_rejects(tmp_path, capsys):
    """Tier 2: no --path and no --source → sys.exit(1) with an actionable message."""
    args = _ns(path=None, source=None, name=None, project=str(tmp_path), non_interactive=True)
    with pytest.raises(SystemExit) as exc_info:
        run_install(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--path" in err and "--source" in err


# ---------------------------------------------------------------------------
# reyn pipe run
# ---------------------------------------------------------------------------


def test_run_transform_pipeline_end_to_end(tmp_path, monkeypatch, capsys):
    """Tier 2: a real registered transform-only pipeline runs end-to-end via
    run_run(), printing the correct final result as JSON."""
    monkeypatch.chdir(tmp_path)

    dsl_path = tmp_path / "hello.yaml"
    dsl_path.write_text(
        "pipeline: hello_cli\n"
        "description: greets ctx.name\n"
        "steps:\n"
        "  - transform: {value: \"'hello ' + ctx.name\", output: greeting}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"hello_cli": {"path": "hello.yaml"}})

    args = _ns(
        name="hello_cli", input=json.dumps({"name": "world"}),
        project=str(tmp_path), async_=False,
    )
    run_run(args)

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["pipe_data"] == "hello world"
    assert result["named_stores"]["greeting"] == "hello world"


def test_run_unregistered_pipeline_is_clean_error(tmp_path, monkeypatch, capsys):
    """Tier 2: running a NAME with no matching registered pipeline exits
    cleanly with an actionable message, not a raw KeyError."""
    monkeypatch.chdir(tmp_path)
    _write_reyn_yaml(tmp_path)

    args = _ns(name="nope", input="{}", project=str(tmp_path), async_=False)
    with pytest.raises(SystemExit) as exc_info:
        run_run(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "not registered" in err.lower()


def _install_echo_tool(monkeypatch) -> None:
    """Register a REAL, side-effect-free test tool into the real
    ``ToolRegistry`` — mirrors ``test_pipeline_is4_inline.py``'s
    ``_install_write_tool`` idiom (a genuinely-dispatched real tool, not a
    caller-supplied fake ``tool_dispatch``)."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        return {"content": str(args.get("text", "")).upper()}

    tool = ToolDefinition(
        name="cli_pipe_echo",
        description="Test tool: uppercases 'text' (no side effect).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow", phase="allow"),
        handler=_handler,
        category="io",
        purity="pure",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def test_run_tool_step_dispatches_for_real(tmp_path, monkeypatch, capsys):
    """Tier 2: a pipeline reaching a 'tool:' step now dispatches for REAL
    through a standalone ToolContext — the corrected scope decision (was:
    a blanket refusal)."""
    monkeypatch.chdir(tmp_path)
    _install_echo_tool(monkeypatch)

    dsl_path = tmp_path / "uses_tool.yaml"
    dsl_path.write_text(
        "pipeline: uses_tool\n"
        "steps:\n"
        "  - tool: {name: cli_pipe_echo, args: {text: !expr ctx.msg}, output: shout}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"uses_tool": {"path": "uses_tool.yaml"}})

    args = _ns(
        name="uses_tool", input=json.dumps({"msg": "hi reyn"}),
        project=str(tmp_path), async_=False,
    )
    run_run(args)

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["named_stores"]["shout"] == {"content": "HI REYN"}
    assert result["pipe_data"] == {"content": "HI REYN"}


def test_run_tool_step_file_write_is_denied_without_grant_flag(
    tmp_path, monkeypatch, capsys,
):
    """Tier 2: fail-closed-by-default permission posture (security fix). A
    'tool:' step writing OUTSIDE the default write zone (.reyn/) via the
    real, shipped 'write_file' tool is DENIED without --grant-file-write —
    byte-identical to 'reyn chat's own no-flag posture. This matters
    specifically because a pipeline may be installed from an untrusted
    source (`reyn pipe install --source`); it must not silently gain
    file-write access merely by being run."""
    monkeypatch.chdir(tmp_path)

    dsl_path = tmp_path / "writer.yaml"
    dsl_path.write_text(
        "pipeline: writer\n"
        "steps:\n"
        "  - tool: {name: write_file, args: {path: \"out.txt\", content: \"hello\"}, "
        "output: r}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"writer": {"path": "writer.yaml"}})

    args = _ns(
        name="writer", input="{}", project=str(tmp_path), async_=False,
        grant_file_write=False,
    )
    run_run(args)

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["named_stores"]["r"]["status"] == "denied"
    assert not (tmp_path / "out.txt").exists()


def test_run_tool_step_file_write_allowed_with_grant_flag(
    tmp_path, monkeypatch, capsys,
):
    """Tier 2: the SAME write_file pipeline as above, but with
    --grant-file-write — the opt-in flag (same name/semantics as `reyn chat
    --grant-file-write`) grants file.write for THIS invocation, and the
    write actually lands."""
    monkeypatch.chdir(tmp_path)

    dsl_path = tmp_path / "writer.yaml"
    dsl_path.write_text(
        "pipeline: writer\n"
        "steps:\n"
        "  - tool: {name: write_file, args: {path: \"out.txt\", content: \"hello\"}, "
        "output: r}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"writer": {"path": "writer.yaml"}})

    args = _ns(
        name="writer", input="{}", project=str(tmp_path), async_=False,
        grant_file_write=True,
    )
    run_run(args)

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["named_stores"]["r"]["status"] == "ok"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


def _fake_scripted_acompletion(content: str):
    """Real async fake for litellm.acompletion (the documented replay seam —
    see test_llm_request_event_1669.py) — a fixed plain-text reply, no tool
    calls, regardless of the actual prompt/messages sent."""

    async def _acompletion(*_args, **_kwargs):
        return litellm.ModelResponse(
            choices=[{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model="openai/gemini-2.5-flash-lite",
        )

    return _acompletion


def test_run_agent_step_spawns_real_ephemeral_session(tmp_path, monkeypatch, capsys):
    """Tier 2: a pipeline reaching an 'agent:' step genuinely spawns an
    ephemeral session (real AgentRegistry/Session/MessageBus) under the
    'default' identity and runs it to completion — ONLY the LLM completion
    call is faked (litellm.acompletion, the documented replay seam), so the
    pipeline's final output is the (faked) LLM reply threaded through R3."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        litellm, "acompletion", _fake_scripted_acompletion("the agent's answer"),
    )

    dsl_path = tmp_path / "uses_agent.yaml"
    dsl_path.write_text(
        "pipeline: uses_agent\n"
        "steps:\n"
        "  - agent: {prompt: 'please answer', output: reply}\n",
        encoding="utf-8",
    )
    # A real litellm-recognizable model string for 'standard' (mirrors a real
    # project's reyn.yaml) — avoids litellm's own unrecognized-provider
    # banner, which is orthogonal to what this test exercises.
    (tmp_path / "reyn.yaml").write_text(
        yaml.dump(
            {
                "model": "standard",
                "models": {"standard": "openai/gpt-4o-mini"},
                "pipelines": {"entries": {"uses_agent": {"path": "uses_agent.yaml"}}},
            },
            allow_unicode=True, default_flow_style=False,
        ),
        encoding="utf-8",
    )

    args = _ns(name="uses_agent", input="{}", project=str(tmp_path), async_=False)
    run_run(args)

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["named_stores"]["reply"] == "the agent's answer"


# ---------------------------------------------------------------------------
# reyn pipe run — MCP (and other resource-backed) tool-category dispatch fix
# ---------------------------------------------------------------------------


class _FakeMCPClient:
    """Fakes the transport ONLY — every layer above (resolve_invoke_action,
    mcp_call_tool, MCPGateway/MCPConnectionService, the OpContext / permission
    gate) runs for real. Mirrors ``MCPClient``'s real construction signature
    (``config`` positional, ``agent_id``/``server_name``/``message_handler``/
    ``elicitation_handler`` kwargs — the ``default`` identity's main Session
    is non-ephemeral, so it holds its MCP connection open via
    ``MCPConnectionService`` rather than the ephemeral-session
    ``MCPClientPool`` path — both construct a bare ``MCPClient`` the same way,
    mirroring ``test_2421_gateway_acceptance.py``'s existing fixture
    convention of patching the constructor at its import site) and the
    async-CM + ``call_tool`` surface + the negotiated-version/capabilities
    read ``MCPConnectionService`` does once per (re)connect."""

    def __init__(self, config: dict, *, agent_id: "str | None" = None,
                 server_name: "str | None" = None, **_kwargs: Any) -> None:
        self._config = config
        self.server_name = server_name
        self.negotiated_version = "2024-11-05"

    async def __aenter__(self) -> "_FakeMCPClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    def advertised_capabilities(self) -> dict:
        return {}

    def is_initialized(self) -> bool:
        return True

    async def call_tool(
        self, name: str, args: dict, *, progress_callback=None, timeout_seconds=None,
    ) -> dict:
        return {
            "content": [{"type": "text", "text": f"{name}:{args.get('msg', '')}"}],
            "isError": False,
        }


def test_run_tool_step_dispatches_mcp_action_for_real(tmp_path, monkeypatch, capsys):
    """Tier 2: regression pin for the router_state=None bug — a 'tool:' step
    calling the first-class 'mcp__<server>__<tool>' action now genuinely
    resolves and dispatches through the real MCP call path (permission gate,
    MCPGateway/MCPConnectionService, the op_runtime mcp handler all real; only
    the MCP CLIENT's transport is faked, mirroring
    test_2421_gateway_acceptance.py's own fixture convention of patching
    MCPClient at its import site)."""
    monkeypatch.chdir(tmp_path)
    import reyn.mcp.connection_service as connection_service_mod
    import reyn.mcp.pool as pool_mod
    monkeypatch.setattr(pool_mod, "MCPClient", _FakeMCPClient)
    monkeypatch.setattr(connection_service_mod, "MCPClient", _FakeMCPClient)

    dsl_path = tmp_path / "uses_mcp.yaml"
    dsl_path.write_text(
        "pipeline: uses_mcp\n"
        "steps:\n"
        "  - tool: {name: mcp__echo__ping, args: {msg: !expr ctx.msg}, output: r}\n",
        encoding="utf-8",
    )
    (tmp_path / "reyn.yaml").write_text(
        yaml.dump(
            {
                "model": "standard",
                "models": {"standard": "openai/gpt-4o-mini"},
                "mcp": {"servers": {"echo": {"type": "stdio", "command": "x"}}},
                # Non-interactive caller (no one to answer the JIT approval
                # prompt) needs the MCP runtime-approval gate pre-granted in
                # config — mirrors what an operator running 'reyn pipe run'
                # non-interactively against a trusted server would configure.
                "permissions": {"mcp": {"echo": "allow"}},
                "pipelines": {"entries": {"uses_mcp": {"path": "uses_mcp.yaml"}}},
            },
            allow_unicode=True, default_flow_style=False,
        ),
        encoding="utf-8",
    )

    args = _ns(
        name="uses_mcp", input=json.dumps({"msg": "hi reyn"}),
        project=str(tmp_path), async_=False,
    )
    run_run(args)

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["named_stores"]["r"]["status"] == "ok"
    assert result["named_stores"]["r"]["content"] == "ping:hi reyn"


@pytest.mark.asyncio
async def test_router_state_resource_categories_populated_from_real_session(
    tmp_path, monkeypatch,
):
    """Tier 2: a more direct regression-pin than the end-to-end MCP case above
    — the SAME real Session (default identity) run_run sources router_state
    from has its RouterHostAdapter report the project's configured MCP
    servers, confirming build_resource_caller_state saw a REAL host (not the
    router_state=None gap this bug fix closes)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text(
        yaml.dump(
            {
                "model": "standard",
                "mcp": {"servers": {"echo": {"type": "stdio", "command": "x"}}},
            },
            allow_unicode=True, default_flow_style=False,
        ),
        encoding="utf-8",
    )

    from reyn.config import load_config
    from reyn.runtime.registry import DEFAULT_AGENT_NAME
    from reyn.runtime.registry_bootstrap import build_agent_registry_from_project
    from reyn.tools.types import build_resource_caller_state

    config = load_config()
    agent_registry = build_agent_registry_from_project(
        tmp_path, config, non_interactive=True,
    )
    try:
        session = agent_registry.get_or_load(DEFAULT_AGENT_NAME)
        router_state = await build_resource_caller_state(session.router_host)
    finally:
        await agent_registry.shutdown()

    servers = {s["name"] for s in (router_state.mcp_servers or [])}
    assert "echo" in servers
