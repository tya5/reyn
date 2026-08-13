"""Tier 2: #4631 — ``reyn config validate``'s MCP server-placement check.

``unknown_config_keys`` walks the TOP-LEVEL ``ReynConfig`` schema only —
``mcp:`` is a recognized key, so nothing under it is ever inspected. A
server entry written directly at ``mcp.<name>`` (instead of
``mcp.servers.<name>``) loads WITHOUT error and WITHOUT warning:
``cfg.mcp.servers`` silently stays empty and the server is never
registered — the same "written, loaded, but the runtime value never
matches" class #4501 (hooks[] entries) closed for a different config
surface.

Checked PER SOURCE FILE (not the already-merged policy_merged/
in_set_merged dicts ``_validate`` also reads) — this check answers "which
FILE is wrong," a different question from #4174 T0's "which key is
unknown" (merged is enough for that one). The 3 static paths + the 1
dynamic path mirror ``_migrate_mcp``'s own already-established scan list
exactly (this module).

Real CLI invocation against real config files (mirrors
``test_config_validate_migrate_command_4174.py``'s own established
project fixture) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.setattr(
        "reyn.config.loader._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_well_placed_mcp_server_produces_no_finding(project, capsys):
    """Tier 2: accept-side — a real ``mcp.servers.<name>`` entry (the
    correct shape) must NOT be flagged as misplaced."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n    myserver:\n"
            "      command: python\n      args: [\"x\"]\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


def test_a_scalar_mcp_config_key_is_not_flagged(project, capsys):
    """Tier 2: accept-side — the shape discriminator is 'is the value
    shaped like a server entry (command/url/type)', not 'is it a
    non-servers key under mcp:' — a real scalar mcp.* config key must
    not false-positive."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + "mcp:\n  timeout_seconds: 30\n",
    )
    _validate()
    out = capsys.readouterr().out
    assert "MCP server placement" not in out


def test_a_server_entry_written_directly_under_mcp_in_reyn_yaml_is_flagged(
    project, capsys,
):
    """Tier 2: the core witness (#4631's own repro) — ``mcp.<name>``
    written where ``mcp.servers.<name>`` belongs is caught and the file
    is named."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  myserver:\n    command: python\n    args: [\"x\"]\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out

    assert "MCP server placement" in out
    assert "[reyn.yaml] mcp.myserver" in out
    assert "never registered" in out


def test_a_server_entry_in_dot_reyn_config_mcp_yaml_is_flagged_with_its_own_file_name(
    project, capsys,
):
    """Tier 2: the SAME misplacement in the dynamic file (architect's
    own measured extension) — the shape is identical
    ({"mcp": {"servers": ...}}, same as reyn.yaml's own section per
    loader.py:678-682's own comment), so the same detector catches it,
    naming THIS file, not reyn.yaml."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "mcp.yaml",
        "mcp:\n  myserver:\n    command: python\n    args: [\"x\"]\n",
    )
    _validate()
    out = capsys.readouterr().out

    assert "MCP server placement" in out
    assert "[.reyn/config/mcp.yaml] mcp.myserver" in out
    assert "[reyn.yaml]" not in out.split("MCP server placement")[1].split("\n\n")[0]


def test_a_server_entry_in_reyn_local_yaml_is_flagged_with_its_own_file_name(
    project, capsys,
):
    """Tier 2: reyn.local.yaml — the second of the 2 project-level static
    sources — is checked independently of reyn.yaml."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / "reyn.local.yaml",
        "mcp:\n  myserver:\n    command: python\n    args: [\"x\"]\n",
    )
    _validate()
    out = capsys.readouterr().out

    assert "[reyn.local.yaml] mcp.myserver" in out


def test_a_server_entry_in_user_global_config_is_flagged_with_its_own_file_name(
    project, capsys,
):
    """Tier 2: ~/.reyn/config.yaml (user_global) — the third static
    source ``_migrate_mcp`` already scans (this module's own
    ``legacy_paths``) — is checked too, not silently narrowed to just
    the 2 project-level files."""
    from reyn.interfaces.cli.commands.config import _validate

    fake_home = Path.home()
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        fake_home / ".reyn" / "config.yaml",
        "mcp:\n  myserver:\n    command: python\n    args: [\"x\"]\n",
    )
    _validate()
    out = capsys.readouterr().out

    assert "[~/.reyn/config.yaml] mcp.myserver" in out


def test_two_misplaced_servers_in_the_same_file_are_both_named(project, capsys):
    """Tier 2: multiple misplaced entries in one file are each named
    individually, not collapsed into a count."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n"
            "  serverA:\n    command: python\n    args: [\"a\"]\n"
            "  serverB:\n    url: \"http://localhost:9000\"\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out

    assert "[reyn.yaml] mcp.serverA" in out
    assert "[reyn.yaml] mcp.serverB" in out
