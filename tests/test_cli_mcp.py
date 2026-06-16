"""Tier 2: OS invariant — ``reyn mcp`` CLI subcommands (new in Wave 2).

Covers:
  - search / install / list / remove / set-secret / clear-secret subcommand
    argparse registration (happy-path parse).
  - list cheap-default vs --probe flag distinction.
  - list STATUS derivation from os.environ (cheap mode).
  - remove scope-tier file write (local / project / user).
  - set-secret / clear-secret storage invariants.
  - --env flag parsing for install.
  - Invalid args / scope error messages.
  - Namespace coexistence: 'mcp serve' still parses.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
import yaml

from reyn.cli.commands.mcp import (
    _all_servers_with_scope,
    _get_servers_from_scope,
    _infer_credentials,
    _infer_status,
    _infer_transport,
    _load_yaml_file,
    _scope_path,
    _server_env_keys,
    _write_yaml_file,
    register,
    run_clear_secret,
    run_list,
    run_remove,
    run_set_secret,
)
from reyn.security.secrets.store import load_secrets, save_secret

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def _write_server_yaml(path: Path, servers: dict) -> None:
    """Write a minimal reyn-style yaml with mcp.servers content."""
    data: dict = {"mcp": {"servers": servers}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Argparse registration — namespace coexistence
# ---------------------------------------------------------------------------


def test_serve_still_parses():
    """Tier 2: 'mcp serve' parses correctly after new subcommands are registered."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "serve"])
    assert args.mcp_command == "serve"


def test_search_parses():
    """Tier 2: 'mcp search QUERY' is a valid CLI invocation."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "search", "github"])
    assert args.mcp_command == "search"
    assert args.query == "github"


def test_install_parses_minimal():
    """Tier 2: 'mcp install SERVER_ID' uses default scope=local."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "install", "io.github.foo/bar"])
    assert args.mcp_command == "install"
    assert args.server_id == "io.github.foo/bar"
    assert args.scope == "local"
    assert args.env == []
    assert args.non_interactive is False


def test_install_parses_with_scope():
    """Tier 2: 'mcp install --scope project' sets scope correctly."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "install", "my-server", "--scope", "project"])
    assert args.scope == "project"


def test_install_parses_with_env_flags():
    """Tier 2: 'mcp install --env K=V --env K2=V2' accumulates env pairs."""
    parser = _make_parser()
    args = parser.parse_args([
        "mcp", "install", "my-server",
        "--env", "TOKEN=abc",
        "--env", "KEY2=val2",
    ])
    assert args.env == ["TOKEN=abc", "KEY2=val2"]


def test_install_non_interactive_flag():
    """Tier 2: '--non-interactive' suppresses prompts (flag parsed correctly)."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "install", "my-server", "--non-interactive"])
    assert args.non_interactive is True


def test_install_invalid_scope_rejected():
    """Tier 2: 'mcp install --scope badscope' is rejected by argparse."""
    parser = _make_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["mcp", "install", "my-server", "--scope", "badscope"])


def test_list_parses():
    """Tier 2: 'mcp list' parses correctly; --probe defaults to False."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "list"])
    assert args.mcp_command == "list"
    assert args.probe is False


def test_list_probe_flag():
    """Tier 2: 'mcp list --probe' sets probe=True."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "list", "--probe"])
    assert args.probe is True


def test_remove_parses():
    """Tier 2: 'mcp remove NAME' parses correctly."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "remove", "github"])
    assert args.mcp_command == "remove"
    assert args.name == "github"
    assert args.scope is None  # auto-detect by default


def test_remove_parses_with_scope():
    """Tier 2: 'mcp remove NAME --scope user' sets scope."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "remove", "github", "--scope", "user"])
    assert args.scope == "user"


def test_set_secret_parses():
    """Tier 2: 'mcp set-secret SERVER KEY=VALUE' parses correctly."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "set-secret", "github", "TOKEN=abc"])
    assert args.mcp_command == "set-secret"
    assert args.server == "github"
    assert args.key_value == "TOKEN=abc"


def test_clear_secret_parses_with_key():
    """Tier 2: 'mcp clear-secret SERVER KEY' parses correctly."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "clear-secret", "github", "TOKEN"])
    assert args.mcp_command == "clear-secret"
    assert args.server == "github"
    assert args.key == "TOKEN"


def test_clear_secret_parses_without_key():
    """Tier 2: 'mcp clear-secret SERVER' (no KEY) defaults key to None."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "clear-secret", "github"])
    assert args.key is None


# ---------------------------------------------------------------------------
# _scope_path
# ---------------------------------------------------------------------------


def test_scope_path_user():
    """Tier 2: scope=user maps to ~/.reyn/config.yaml."""
    p = _scope_path("user", None)
    assert p == Path.home() / ".reyn" / "config.yaml"


def test_scope_path_project(tmp_path):
    """Tier 2: scope=project maps to <project>/reyn.yaml."""
    p = _scope_path("project", tmp_path)
    assert p == tmp_path / "reyn.yaml"


def test_scope_path_local(tmp_path):
    """Tier 2: scope=local maps to <project>/reyn.local.yaml."""
    p = _scope_path("local", tmp_path)
    assert p == tmp_path / "reyn.local.yaml"


def test_scope_path_project_without_root_exits():
    """Tier 2: scope=project without project_root calls sys.exit."""
    with pytest.raises(SystemExit):
        _scope_path("project", None)


def test_scope_path_local_without_root_exits():
    """Tier 2: scope=local without project_root calls sys.exit."""
    with pytest.raises(SystemExit):
        _scope_path("local", None)


# ---------------------------------------------------------------------------
# list — cheap default STATUS derivation
# ---------------------------------------------------------------------------


def test_infer_status_no_env_is_ready():
    """Tier 2: server with no env declarations is always 'ready'."""
    cfg = {"type": "stdio", "command": "npx"}
    assert _infer_status(cfg) == "ready"


def test_infer_status_all_env_set(monkeypatch):
    """Tier 2: all declared env vars present in os.environ → 'ready'."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    cfg = {"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}
    assert _infer_status(cfg) == "ready"


def test_infer_status_missing_env(monkeypatch):
    """Tier 2: at least one declared env var absent → 'missing-cred'."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg = {"env": {"SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}"}}
    assert _infer_status(cfg) == "missing-cred"


def test_infer_credentials_none():
    """Tier 2: server with no env → CREDENTIALS column shows '(none)'."""
    assert _infer_credentials({}) == "(none)"


def test_infer_credentials_marks(monkeypatch):
    """Tier 2: credential column shows ✓ for set and ✗ for unset."""
    monkeypatch.setenv("SET_KEY", "value")
    monkeypatch.delenv("UNSET_KEY", raising=False)
    cfg = {"env": {"SET_KEY": "${SET_KEY}", "UNSET_KEY": "${UNSET_KEY}"}}
    creds = _infer_credentials(cfg)
    assert "SET_KEY ✓" in creds
    assert "UNSET_KEY ✗" in creds


def test_infer_transport_stdio():
    """Tier 2: server with 'command' field inferred as 'stdio' transport."""
    assert _infer_transport({"command": "npx"}) == "stdio"


def test_infer_transport_http():
    """Tier 2: server with 'url' field inferred as 'http' transport."""
    assert _infer_transport({"url": "http://localhost:3000/mcp"}) == "http"


def test_infer_transport_explicit():
    """Tier 2: explicit 'type' field wins over inference."""
    assert _infer_transport({"type": "stdio", "command": "npx"}) == "stdio"


# ---------------------------------------------------------------------------
# list — cheap vs --probe distinction (output test)
# ---------------------------------------------------------------------------


def test_run_list_empty(tmp_path, capsys, monkeypatch):
    """Tier 2: run_list with no servers configured prints a helpful message."""
    # Point project root to a temp dir with no yaml.
    monkeypatch.chdir(tmp_path)
    ns = argparse.Namespace(probe=False)
    run_list(ns)
    out = capsys.readouterr().out
    assert "No MCP servers configured" in out


def test_run_list_shows_servers(tmp_path, capsys, monkeypatch):
    """Tier 2: run_list enumerates servers from yaml files."""
    # Write a local scope file under tmp_path/reyn.local.yaml.
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "filesystem": {"type": "stdio", "command": "npx"},
    })
    # Also write a reyn.yaml so _find_project_root returns tmp_path.
    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(probe=False)
    run_list(ns)
    out = capsys.readouterr().out
    assert "filesystem" in out


def test_run_list_probe_false_no_handshake(tmp_path, capsys, monkeypatch):
    """Tier 2: probe=False uses cheap status (no subprocess/handshake initiated)."""
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "myserver": {"type": "stdio", "command": "npx"},
    })
    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # No network/subprocess access needed — if it tries to probe it would
    # raise an exception in this isolated env; passing probe=False must not.
    ns = argparse.Namespace(probe=False)
    run_list(ns)  # must not raise
    out = capsys.readouterr().out
    assert "myserver" in out


# ---------------------------------------------------------------------------
# remove — scope tier file write
# ---------------------------------------------------------------------------


def _setup_project(tmp_path: Path) -> None:
    """Create a minimal project root."""
    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")


def test_remove_from_local_scope(tmp_path, capsys, monkeypatch):
    """Tier 2: run_remove deletes the named server from the local scope yaml."""
    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "github": {"type": "stdio", "command": "npx"},
        "other": {"type": "stdio", "command": "uvx"},
    })
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(name="github", scope=None)
    run_remove(ns)

    remaining = _load_yaml_file(local_cfg)
    servers = remaining.get("mcp", {}).get("servers", {})
    assert "github" not in servers
    assert "other" in servers

    out = capsys.readouterr().out
    assert "github" in out
    assert "removed" in out


def test_remove_from_project_scope(tmp_path, capsys, monkeypatch):
    """Tier 2: run_remove with --scope project writes to reyn.yaml."""
    _setup_project(tmp_path)
    project_yaml = tmp_path / "reyn.yaml"
    _write_server_yaml(project_yaml, {
        "slack": {"type": "stdio", "command": "npx"},
    })
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(name="slack", scope="project")
    run_remove(ns)

    remaining = _load_yaml_file(project_yaml)
    servers = remaining.get("mcp", {}).get("servers", {})
    assert "slack" not in servers


def test_remove_from_user_scope(tmp_path, capsys, monkeypatch, tmp_path_factory):
    """Tier 2: run_remove with --scope user writes to ~/.reyn/config.yaml."""
    # We cannot safely write to actual ~/.reyn/ in tests, so we monkeypatch
    # _scope_path via monkeypatching Path.home().
    fake_home = tmp_path_factory.mktemp("fake_home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    user_cfg = fake_home / ".reyn" / "config.yaml"
    _write_server_yaml(user_cfg, {
        "filesystem": {"type": "stdio", "command": "npx"},
    })

    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(name="filesystem", scope="user")
    run_remove(ns)

    remaining = _load_yaml_file(user_cfg)
    servers = remaining.get("mcp", {}).get("servers", {})
    assert "filesystem" not in servers


def test_remove_missing_server_exits(tmp_path, monkeypatch):
    """Tier 2: removing a server not found in any scope exits with error."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(name="does-not-exist", scope=None)
    with pytest.raises(SystemExit):
        run_remove(ns)


def test_remove_shows_runtime_note(tmp_path, capsys, monkeypatch):
    """Tier 2: remove always prints the 'subprocess continues until next session' note."""
    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {"myserver": {"type": "stdio", "command": "npx"}})
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(name="myserver", scope=None)
    run_remove(ns)
    out = capsys.readouterr().out
    assert "subprocess" in out.lower() or "session" in out.lower()


# ---------------------------------------------------------------------------
# set-secret
# ---------------------------------------------------------------------------


def test_run_set_secret_saves_to_store(tmp_path, capsys, monkeypatch):
    """Tier 2: run_set_secret writes the value to secrets.store."""
    from reyn.security.secrets.store import clear_secret as _clear

    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Plant a minimal server declaration so KEY is "known".
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "github": {"type": "stdio", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
    })

    ns = argparse.Namespace(server="github", key_value="GITHUB_TOKEN=ghp_test_value")
    try:
        run_set_secret(ns)
    finally:
        _clear("GITHUB_TOKEN")

    out = capsys.readouterr().out
    assert "saved" in out or "Secret" in out


def test_run_set_secret_unknown_key_warns(tmp_path, capsys, monkeypatch):
    """Tier 2: setting an undeclared KEY emits a warning but proceeds."""
    from reyn.security.secrets.store import clear_secret as _clear

    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "github": {"type": "stdio", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
    })
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(server="github", key_value="UNKNOWN_KEY=some_value")
    try:
        run_set_secret(ns)
    finally:
        _clear("UNKNOWN_KEY")

    out = capsys.readouterr().out
    assert "warning" in out.lower()


def test_run_set_secret_adds_env_ref(tmp_path, capsys, monkeypatch):
    """Tier 2: run_set_secret ensures a ${KEY} ref exists in the local yaml."""
    from reyn.security.secrets.store import clear_secret as _clear

    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(server="newserver", key_value="MY_TOKEN=secret_val")
    try:
        run_set_secret(ns)
    finally:
        _clear("MY_TOKEN")

    local_cfg = tmp_path / "reyn.local.yaml"
    data = _load_yaml_file(local_cfg)
    servers = data.get("mcp", {}).get("servers", {})
    assert "newserver" in servers
    assert "${MY_TOKEN}" in str(servers["newserver"].get("env", {}).get("MY_TOKEN", ""))


# ---------------------------------------------------------------------------
# clear-secret
# ---------------------------------------------------------------------------


def test_run_clear_secret_specific_key(tmp_path, capsys, monkeypatch):
    """Tier 2: run_clear_secret with KEY removes that single secret."""
    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "github": {"type": "stdio", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
    })
    monkeypatch.chdir(tmp_path)

    save_secret("GITHUB_TOKEN", "ghp_to_be_cleared")

    ns = argparse.Namespace(server="github", key="GITHUB_TOKEN")
    run_clear_secret(ns)

    secrets = load_secrets()
    assert "GITHUB_TOKEN" not in secrets or secrets.get("GITHUB_TOKEN") != "ghp_to_be_cleared"

    out = capsys.readouterr().out
    assert "GITHUB_TOKEN" in out


def test_run_clear_secret_all_keys(tmp_path, capsys, monkeypatch):
    """Tier 2: run_clear_secret without KEY clears all declared secrets."""
    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "slack": {
            "type": "stdio",
            "env": {
                "SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}",
                "SLACK_TEAM_ID": "${SLACK_TEAM_ID}",
            },
        },
    })
    monkeypatch.chdir(tmp_path)

    save_secret("SLACK_BOT_TOKEN", "xoxb-test")
    save_secret("SLACK_TEAM_ID", "T12345")

    ns = argparse.Namespace(server="slack", key=None)
    run_clear_secret(ns)

    secrets = load_secrets()
    assert secrets.get("SLACK_BOT_TOKEN") != "xoxb-test"
    assert secrets.get("SLACK_TEAM_ID") != "T12345"

    out = capsys.readouterr().out
    # yaml ${} references should NOT have been touched (only store was changed)
    data = _load_yaml_file(local_cfg)
    servers = data.get("mcp", {}).get("servers", {})
    assert "${SLACK_BOT_TOKEN}" in str(servers.get("slack", {}).get("env", {}).get("SLACK_BOT_TOKEN", ""))


def test_run_clear_secret_yaml_ref_untouched(tmp_path, monkeypatch):
    """Tier 2: clear-secret does NOT remove ${KEY} references from yaml."""
    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "github": {"type": "stdio", "env": {"MY_KEY": "${MY_KEY}"}},
    })
    monkeypatch.chdir(tmp_path)

    save_secret("MY_KEY", "value_to_clear")

    ns = argparse.Namespace(server="github", key="MY_KEY")
    run_clear_secret(ns)

    data = _load_yaml_file(local_cfg)
    env_decl = data.get("mcp", {}).get("servers", {}).get("github", {}).get("env", {})
    assert "MY_KEY" in env_decl, "yaml ref must survive clear-secret"


def test_run_clear_secret_no_known_keys(tmp_path, capsys, monkeypatch):
    """Tier 2: clear-secret for unknown server (no env decl) prints info and exits."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    ns = argparse.Namespace(server="nonexistent", key=None)
    run_clear_secret(ns)

    out = capsys.readouterr().out
    assert "Nothing to clear" in out or "No env" in out or "nonexistent" in out


# ---------------------------------------------------------------------------
# _all_servers_with_scope — deduplication / priority
# ---------------------------------------------------------------------------


def test_all_servers_higher_scope_wins(tmp_path, monkeypatch, tmp_path_factory):
    """Tier 2: local scope overrides project for the same server name."""
    _setup_project(tmp_path)

    # project scope: reyn.yaml
    project_yaml = tmp_path / "reyn.yaml"
    _write_server_yaml(project_yaml, {
        "github": {"type": "stdio", "command": "project-cmd"},
    })

    # local scope: reyn.local.yaml
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "github": {"type": "stdio", "command": "local-cmd"},
    })

    monkeypatch.chdir(tmp_path)

    entries = _all_servers_with_scope(tmp_path)
    github_entries = [(name, scope, cfg) for name, scope, cfg in entries if name == "github"]
    assert github_entries, "expected at least one github entry after dedup"
    _name, scope, cfg = github_entries[0]
    assert scope == "local"
    assert cfg.get("command") == "local-cmd"


def test_all_servers_merges_distinct_names(tmp_path, monkeypatch, tmp_path_factory):
    """Tier 2: distinct names from different scopes all appear in the output."""
    _setup_project(tmp_path)

    project_yaml = tmp_path / "reyn.yaml"
    _write_server_yaml(project_yaml, {"fs": {"type": "stdio", "command": "npx"}})

    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {"gh": {"type": "stdio", "command": "npx"}})

    monkeypatch.chdir(tmp_path)
    entries = _all_servers_with_scope(tmp_path)
    names = {name for name, _, _ in entries}
    assert "fs" in names
    assert "gh" in names


# ---------------------------------------------------------------------------
# _server_env_keys
# ---------------------------------------------------------------------------


def test_server_env_keys_found(tmp_path, monkeypatch):
    """Tier 2: _server_env_keys returns the declared env key set for a known server."""
    _setup_project(tmp_path)
    local_cfg = tmp_path / "reyn.local.yaml"
    _write_server_yaml(local_cfg, {
        "slack": {"env": {"SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}", "SLACK_TEAM_ID": "T1"}},
    })
    monkeypatch.chdir(tmp_path)

    keys = _server_env_keys("slack", tmp_path)
    assert keys == {"SLACK_BOT_TOKEN", "SLACK_TEAM_ID"}


def test_server_env_keys_not_found(tmp_path, monkeypatch):
    """Tier 2: _server_env_keys returns None for a server not in any yaml."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _server_env_keys("no-such-server", tmp_path)
    assert result is None
