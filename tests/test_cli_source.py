"""Tier 2 invariants for the `reyn source` CLI subcommands (ADR-0033 Phase 1).

Tests cover:
  - Argparse registration: list / describe / rm subcommand parsing
  - cmd_list: empty manifest → hint message
  - cmd_list: populated manifest → tabular output with all entries
  - cmd_list --json: valid JSON output
  - cmd_describe: populated → full detail output
  - cmd_describe: missing source → exit 1 + stderr
  - cmd_rm --yes: manifest entry removed + chunks_dropped count printed
  - cmd_rm: missing source → exit 1
  - cmd_rm: no --yes + stdin "n" → aborted, no removal

Uses real SourceManifest instances (tmp_path fixture).
No mocks — op dispatch for rm is monkeypatched at execute_op level.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

from reyn.cli.commands.source import (
    _cmd_describe_async,
    _cmd_list_async,
    _cmd_rm_async,
    register,
)
from reyn.index.source_manifest import SourceEntry, SourceManifest, get_source_manifest


# ── helpers ───────────────────────────────────────────────────────────────────


class _MinimalWorkspace:
    """Minimal workspace duck-type for CLI rm tests."""
    def __init__(self, root: Path) -> None:
        self.base_dir = root


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def _list_args(*, as_json: bool = False) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.as_json = as_json
    return ns


def _describe_args(name: str) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.name = name
    return ns


def _rm_args(name: str, *, yes: bool = False) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.name = name
    ns.yes = yes
    return ns


async def _seed_manifest(workspace: Path, entries: list[SourceEntry]) -> SourceManifest:
    """Write entries into a fresh SourceManifest under ``workspace``."""
    manifest = SourceManifest(workspace)
    for entry in entries:
        await manifest.upsert(entry)
    return manifest


def _make_entry(
    name: str = "reyn_code",
    description: str = "Reyn Python source",
    path: str = "src/**/*.py",
    chunk_count: int = 1247,
    embedding_model: str | None = "text-embedding-3-small",
    last_indexed: str | None = "2026-05-10T14:32:00Z",
) -> SourceEntry:
    return SourceEntry(
        name=name,
        description=description,
        path=path,
        backend="sqlite",
        chunk_count=chunk_count,
        embedding_model=embedding_model,
        last_indexed=last_indexed,
    )


# ── argparse registration ─────────────────────────────────────────────────────


def test_list_parses():
    """Tier 2: 'source list' is a valid CLI invocation."""
    parser = _make_parser()
    args = parser.parse_args(["source", "list"])
    assert args.source_action == "list"
    assert args.as_json is False


def test_list_json_flag():
    """Tier 2: 'source list --json' sets as_json=True."""
    parser = _make_parser()
    args = parser.parse_args(["source", "list", "--json"])
    assert args.as_json is True


def test_describe_parses():
    """Tier 2: 'source describe NAME' parses correctly."""
    parser = _make_parser()
    args = parser.parse_args(["source", "describe", "reyn_code"])
    assert args.source_action == "describe"
    assert args.name == "reyn_code"


def test_rm_parses():
    """Tier 2: 'source rm NAME' parses correctly; --yes defaults to False."""
    parser = _make_parser()
    args = parser.parse_args(["source", "rm", "my_source"])
    assert args.source_action == "rm"
    assert args.name == "my_source"
    assert args.yes is False


def test_rm_yes_flag():
    """Tier 2: 'source rm NAME --yes' sets yes=True."""
    parser = _make_parser()
    args = parser.parse_args(["source", "rm", "my_source", "--yes"])
    assert args.yes is True


def test_rm_short_yes_flag():
    """Tier 2: 'source rm NAME -y' sets yes=True."""
    parser = _make_parser()
    args = parser.parse_args(["source", "rm", "my_source", "-y"])
    assert args.yes is True


# ── cmd_list — empty manifest ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_list_empty_manifest(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_list with empty manifest prints a 'No indexed sources' hint."""
    import reyn.cli.commands.source as _src_mod

    manifest = SourceManifest(tmp_path)
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_list_async(_list_args())
    captured = capsys.readouterr()

    assert rc == 0
    assert "No indexed sources" in captured.out
    assert "index_docs" in captured.out  # hint mentions the command


# ── cmd_list — populated manifest ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_list_populated_tabular(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_list with sources prints tabular output with name + chunks."""
    import reyn.cli.commands.source as _src_mod

    manifest = await _seed_manifest(tmp_path, [
        _make_entry("reyn_code", chunk_count=1247),
        _make_entry("reyn_docs", description="mkdocs", chunk_count=89),
    ])
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_list_async(_list_args())
    captured = capsys.readouterr()

    assert rc == 0
    assert "reyn_code" in captured.out
    assert "reyn_docs" in captured.out
    assert "1247" in captured.out
    assert "89" in captured.out


# ── cmd_list --json ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_list_json_output(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_list --json prints valid JSON with all source entries."""
    import reyn.cli.commands.source as _src_mod

    entry = _make_entry("my_source", chunk_count=55)
    manifest = await _seed_manifest(tmp_path, [entry])
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_list_async(_list_args(as_json=True))
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    assert "my_source" in payload
    assert payload["my_source"]["chunk_count"] == 55


@pytest.mark.asyncio
async def test_cmd_list_json_empty(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_list --json with empty manifest prints empty JSON object."""
    import reyn.cli.commands.source as _src_mod

    manifest = SourceManifest(tmp_path)
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_list_async(_list_args(as_json=True))
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    assert payload == {}


# ── cmd_describe ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_describe_full_details(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_describe prints all SourceEntry fields."""
    import reyn.cli.commands.source as _src_mod

    entry = _make_entry(
        "reyn_code",
        description="Reyn Python framework source",
        path="src/**/*.py",
        chunk_count=1247,
        embedding_model="text-embedding-3-small",
        last_indexed="2026-05-10T14:32:00Z",
    )
    manifest = await _seed_manifest(tmp_path, [entry])
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_describe_async(_describe_args("reyn_code"))
    captured = capsys.readouterr()

    assert rc == 0
    assert "reyn_code" in captured.out
    assert "Reyn Python framework source" in captured.out
    assert "src/**/*.py" in captured.out
    assert "1247" in captured.out
    assert "text-embedding-3-small" in captured.out
    assert "2026-05-10T14:32:00Z" in captured.out


@pytest.mark.asyncio
async def test_cmd_describe_missing_source(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_describe missing source returns exit code 1 and writes to stderr."""
    import reyn.cli.commands.source as _src_mod

    manifest = SourceManifest(tmp_path)
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_describe_async(_describe_args("nonexistent"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "nonexistent" in captured.err


# ── cmd_rm ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_rm_missing_source(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_rm with unknown source name returns exit 1 + stderr message."""
    import reyn.cli.commands.source as _src_mod

    manifest = SourceManifest(tmp_path)
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    rc = await _cmd_rm_async(_rm_args("does_not_exist", yes=True))
    captured = capsys.readouterr()

    assert rc == 1
    assert "does_not_exist" in captured.err


@pytest.mark.asyncio
async def test_cmd_rm_yes_removes_source(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_rm --yes dispatches index_drop op and prints chunks_dropped.

    execute_op is monkeypatched to return a success result, bypassing the
    real SQLite backend and permission resolver (which require a full project
    environment). The invariant under test is that the CLI correctly wires
    the op and prints the result.
    """
    import reyn.cli.commands.source as _src_mod
    import reyn.op_runtime as _orm

    entry = _make_entry("trial_source", chunk_count=77)
    manifest = await _seed_manifest(tmp_path, [entry])
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(_src_mod, "_find_project_root_safe", lambda _: None)
    monkeypatch.setattr(_src_mod, "_make_cli_workspace", lambda _: _MinimalWorkspace(tmp_path))

    async def fake_execute_op(op, ctx, *, caller):
        return {"removed": True, "chunks_dropped": 77}

    # Patch execute_op at the op_runtime level so the CLI handler's lazy
    # import picks it up (the handler does `from reyn.op_runtime import execute_op`
    # at call time, so we patch the module attribute).
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    rc = await _cmd_rm_async(_rm_args("trial_source", yes=True))
    captured = capsys.readouterr()

    assert rc == 0
    assert "77" in captured.out
    assert "trial_source" in captured.out


@pytest.mark.asyncio
async def test_cmd_rm_no_confirmation_aborts(tmp_path, capsys, monkeypatch):
    """Tier 2: cmd_rm without --yes aborts when user inputs 'n'."""
    import reyn.cli.commands.source as _src_mod

    entry = _make_entry("keep_me", chunk_count=10)
    manifest = await _seed_manifest(tmp_path, [entry])
    monkeypatch.setattr(_src_mod, "get_source_manifest", lambda _root: manifest)
    monkeypatch.setattr(_src_mod, "_get_workspace_root", lambda: tmp_path)

    # Simulate user entering "n" at the confirmation prompt.
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    rc = await _cmd_rm_async(_rm_args("keep_me", yes=False))
    captured = capsys.readouterr()

    assert rc == 1
    assert "Aborted" in captured.out

    # Source still present in manifest
    remaining = await manifest.get("keep_me")
    assert remaining is not None


# ── top-level parser integration ─────────────────────────────────────────────


def test_source_subcommand_wired_in_cli():
    """Tier 2: `reyn source` is a valid top-level subcommand."""
    from reyn.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["source", "list"])
    assert args.command == "source"
    assert args.source_action == "list"
