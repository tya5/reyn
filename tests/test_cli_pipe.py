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
  - the clear-error path for a pipeline reaching a ``tool:`` step (scoped out
    of v1 standalone execution) — asserts a clean ``SystemExit(1)`` with an
    actionable message, never a crash or silent no-op.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    data: dict = {"model": "standard"}
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
    """Tier 2: 'pipe run NAME --input JSON' parses; --async is present but suppressed."""
    parser = _make_parser()
    args = parser.parse_args(["pipe", "run", "my_pipeline", "--input", '{"a": 1}'])
    assert args.pipe_command == "run"
    assert args.name == "my_pipeline"
    assert args.input == '{"a": 1}'
    assert args.async_ is False


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


def test_run_tool_step_is_refused_with_clear_error_not_a_crash(tmp_path, monkeypatch, capsys):
    """Tier 2: a pipeline reaching a 'tool:' step is refused BEFORE anything
    runs, with a clear, actionable message pointing at a live agent session —
    never a silent no-op, never a confusing mid-run crash."""
    monkeypatch.chdir(tmp_path)

    dsl_path = tmp_path / "uses_tool.yaml"
    dsl_path.write_text(
        "pipeline: uses_tool\n"
        "steps:\n"
        "  - tool: {name: search, args: {query: \"reyn\"}, output: hits}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"uses_tool": {"path": "uses_tool.yaml"}})

    args = _ns(name="uses_tool", input="{}", project=str(tmp_path), async_=False)
    with pytest.raises(SystemExit) as exc_info:
        run_run(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "tool:" in err
    assert "does not yet support" in err.lower() or "live agent session" in err.lower()
