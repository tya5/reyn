"""Tier 2: ``reyn config migrate-mcp`` CLI command (#470 follow-up).

Tests the explicit operator migration of legacy ``mcp.servers`` entries
from ``reyn.yaml`` / ``reyn.local.yaml`` into the canonical
``.reyn/mcp.yaml`` location landed in PR #473.

Pins:

  1. No-op when no legacy entries exist (= clean state).
  2. Migration moves entries from reyn.yaml + reyn.local.yaml + user
     global into .reyn/mcp.yaml.
  3. Legacy files have ``mcp.servers`` removed; other config keys
     stay intact.
  4. Dry-run prints the plan without writing.
  5. Existing .reyn/mcp.yaml entries take precedence on conflict
     (= partial prior migration isn't clobbered).
  6. Empty ``mcp:`` section dropped entirely after removal (= no
     dangling ``mcp: {}`` left in legacy yaml).

Tier 2 because the command is the operator-facing migration path; a
regression that silently dropped servers or clobbered configs would
be a real data-loss hazard.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_migrate(*, dry_run: bool = False):
    from reyn.interfaces.cli.commands.config import _migrate_mcp
    _migrate_mcp(dry_run=dry_run)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Standard project root with a reyn.yaml seed.

    Monkeypatch ``Path.home`` to a tmp subdir so the user-global
    ``~/.reyn/config.yaml`` lookup doesn't touch the real home.
    Monkeypatch ``_find_project_root`` so the command finds the
    tmp project regardless of pytest cwd.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── no-op cases ────────────────────────────────────────────────────────


def test_migrate_no_legacy_entries_is_noop(project, capsys):
    """Tier 2: no ``mcp.servers`` anywhere → command prints "nothing to
    migrate" and doesn't write any files.
    """
    _write_yaml(project / "reyn.yaml", "model: standard\n")

    _run_migrate()

    out = capsys.readouterr().out
    assert "nothing to migrate" in out.lower()
    # No .reyn/mcp.yaml created.
    assert not (project / ".reyn" / "mcp.yaml").exists()


# ── full migration ────────────────────────────────────────────────────


def test_migrate_moves_reyn_yaml_servers_to_dynamic(project, capsys):
    """Tier 2: ``mcp.servers`` from reyn.yaml moves to .reyn/mcp.yaml;
    the source file has the section stripped.
    """
    _write_yaml(
        project / "reyn.yaml",
        "model: standard\n"
        "mcp:\n  servers:\n    sqlite:\n      type: stdio\n      command: npx\n",
    )

    _run_migrate()

    # Target file written with the entry.
    import yaml
    dyn = yaml.safe_load((project / ".reyn" / "mcp.yaml").read_text())
    assert "sqlite" in dyn["mcp"]["servers"]
    assert dyn["mcp"]["servers"]["sqlite"]["command"] == "npx"

    # Source file no longer has mcp.servers — model stays.
    src = yaml.safe_load((project / "reyn.yaml").read_text())
    assert src.get("model") == "standard"
    # Empty mcp section dropped entirely.
    assert "mcp" not in src

    # Summary printed.
    out = capsys.readouterr().out
    assert "sqlite" in out
    assert "reyn.yaml" in out


def test_migrate_moves_from_both_reyn_yaml_and_local(project, capsys):
    """Tier 2: entries from BOTH reyn.yaml and reyn.local.yaml are
    moved; the target file has the union of all sources.
    """
    _write_yaml(
        project / "reyn.yaml",
        "model: standard\n"
        "mcp:\n  servers:\n    sqlite:\n      type: stdio\n      command: npx\n",
    )
    _write_yaml(
        project / "reyn.local.yaml",
        "api_base: http://local\n"
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: uvx\n",
    )

    _run_migrate()

    import yaml
    dyn = yaml.safe_load((project / ".reyn" / "mcp.yaml").read_text())
    servers = dyn["mcp"]["servers"]
    assert "sqlite" in servers
    assert "git" in servers
    assert servers["sqlite"]["command"] == "npx"
    assert servers["git"]["command"] == "uvx"

    # Local file's api_base preserved; mcp section stripped.
    local = yaml.safe_load((project / "reyn.local.yaml").read_text())
    assert local.get("api_base") == "http://local"
    assert "mcp" not in local


def test_migrate_preserves_existing_dynamic_entries(project, capsys):
    """Tier 2: ``.reyn/mcp.yaml`` entries that already exist win on
    conflict — protects against double-migration corrupting a
    server entry that the operator may have edited post-install.
    """
    _write_yaml(
        project / "reyn.yaml",
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: old-cmd\n",
    )
    _write_yaml(
        project / ".reyn" / "mcp.yaml",
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: new-cmd\n",
    )

    _run_migrate()

    import yaml
    dyn = yaml.safe_load((project / ".reyn" / "mcp.yaml").read_text())
    # Existing dynamic entry wins.
    assert dyn["mcp"]["servers"]["git"]["command"] == "new-cmd"
    # Legacy source stripped regardless.
    src = yaml.safe_load((project / "reyn.yaml").read_text())
    assert "mcp" not in src


# ── dry-run ────────────────────────────────────────────────────────────


def test_migrate_dry_run_does_not_write_files(project, capsys):
    """Tier 2: ``--dry-run`` prints the plan but doesn't mutate any
    file — operators can preview before committing.
    """
    _write_yaml(
        project / "reyn.yaml",
        "model: standard\n"
        "mcp:\n  servers:\n    sqlite:\n      type: stdio\n      command: npx\n",
    )

    _run_migrate(dry_run=True)

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "sqlite" in out

    # Files unchanged.
    import yaml
    src = yaml.safe_load((project / "reyn.yaml").read_text())
    assert "mcp" in src
    assert "sqlite" in src["mcp"]["servers"]
    assert not (project / ".reyn" / "mcp.yaml").exists()


# ── side-key preservation ──────────────────────────────────────────────


def test_migrate_preserves_other_mcp_subkeys_if_any(project):
    """Tier 2: only the ``servers`` sub-key is removed from the legacy
    ``mcp:`` section. If sibling keys exist (= hypothetical future
    schema extension), they stay intact.
    """
    _write_yaml(
        project / "reyn.yaml",
        "mcp:\n"
        "  servers:\n    sqlite:\n      type: stdio\n      command: npx\n"
        "  hypothetical_future_key: keep-me\n",
    )

    _run_migrate()

    import yaml
    src = yaml.safe_load((project / "reyn.yaml").read_text())
    # Server moved out, but the sibling key stays.
    assert src.get("mcp") == {"hypothetical_future_key": "keep-me"}


# ── command surface ───────────────────────────────────────────────────


def test_migrate_subcommand_is_registered():
    """Tier 2: ``reyn config migrate-mcp`` is registered as a
    subcommand. Without this, the CLI surface doesn't expose the
    migration path even though the function works.
    """
    import argparse

    from reyn.interfaces.cli.commands.config import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)

    # Parse just enough to verify the subcommand exists.
    args = parser.parse_args(["config", "migrate-mcp", "--dry-run"])
    assert args.config_cmd == "migrate-mcp"
    assert args.dry_run is True
