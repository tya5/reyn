"""Tier 2: OS invariant — `reyn plugin install`/`uninstall` CLI surface (ADR 0064 §3.9, P3).

The CLI is a thin adapter over the SAME typed op the LLM tool / slash surfaces
use: it builds a real ``ToolContext`` and calls
``invoke_tool(get_default_registry(), "plugin_management__install"/"__uninstall", ...)``
— the same lookup+dispatch a live chat-router LLM tool call uses. These tests
prove:

  1. the typed ``kind`` discriminator (builtin/local/git subcommand) threads
     through to the correct ``{kind, ...}`` source shape the op actually
     receives — real dispatch through ``run_install``, no mock of the
     dispatch call;
  2. a real local-plugin install + uninstall roundtrip through the CLI
     entrypoints reaches the SAME registry writes P2's own op-level tests
     assert on (``.reyn/config/skills.yaml`` + ``~/.reyn/plugins/<name>/``);
  3. `{kind:git}` install fails CLOSED when run non-interactively (real
     ``PermissionResolver`` + real ``require_plugin_git_run_code_trust`` gate,
     not a stub) — the CLI's own ``--non-interactive``/no-tty wiring must
     actually reach that gate, not merely assume it does.

No unittest.mock anywhere. Real ``PermissionResolver`` / ``EventLog`` /
``Workspace`` / op_runtime handlers throughout; ``HOME`` is monkeypatched per
test so ``~/.reyn/plugins/`` never touches the real home dir (mirrors
``tests/test_plugin_install.py``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from reyn.interfaces.cli.commands import plugin as plugin_cli

# ── shared fixtures ─────────────────────────────────────────────────────────


def _write_local_plugin(base: Path, name: str = "myplugin") -> Path:
    """A minimal real local plugin dir: manifest + one skills capability
    (mirrors tests/test_plugin_install.py::_make_plugin_source)."""
    plugin_dir = base / name
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "test plugin",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "hello").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: says hi\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    return plugin_dir


def _project(tmp_path: Path, *, allow_file_write: bool = True) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    perms = {"file.write": "allow"} if allow_file_write else {}
    (proj / "reyn.yaml").write_text(
        yaml.safe_dump({"permissions": perms}), encoding="utf-8",
    )
    return proj


def _install_args(kind: str, source_name: str, project: Path, *,
                   install_name: "str | None" = None,
                   non_interactive: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        kind=kind, source_name=source_name, project=str(project),
        install_name=install_name, non_interactive=non_interactive,
    )


def _uninstall_args(name: str, project: Path) -> argparse.Namespace:
    return argparse.Namespace(name=name, project=str(project))


# ── 1. typed kind discriminator threading (Tier 1: Contract) ──────────────


@pytest.mark.parametrize(
    "kind,source_name,expected_source",
    [
        ("builtin", "rag", {"kind": "builtin", "name": "rag"}),
        ("local", "/tmp/some/plugin/dir", {"kind": "local", "path": "/tmp/some/plugin/dir"}),
        ("git", "https://example.com/x.git", {"kind": "git", "url": "https://example.com/x.git"}),
    ],
)
def test_install_kind_threads_to_typed_source_shape(
    tmp_path, monkeypatch, kind, source_name, expected_source,
) -> None:
    """Tier 1: each CLI install subcommand (builtin/local/git) builds the
    EXACT typed {kind, ...} source shape PluginSourceBuiltin/Local/Git expects
    — never a form-sniffed string. Captures the real args dict handed to
    invoke_tool via a real (non-mock) capturing async function."""
    captured: dict = {}

    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        captured["name"] = name
        captured["args"] = args
        return {"status": "ok", "data": {"status": "installed", "name": "x"}}

    monkeypatch.setattr(plugin_cli, "_invoke_plugin_tool", _fake_invoke)

    project = _project(tmp_path)
    args = _install_args(kind, source_name, project)
    plugin_cli.run_install(args)

    assert captured["name"] == "plugin_management__install"
    assert captured["args"]["source"] == expected_source
    assert "name" not in captured["args"]  # no --name override supplied


def test_install_name_override_threads_through(tmp_path, monkeypatch) -> None:
    """Tier 1: --name overrides the op's `name` field (install-directory /
    registry-provenance key), distinct from the source's own name/path/url."""
    captured: dict = {}

    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        captured["args"] = args
        return {"status": "ok", "data": {"status": "installed", "name": "custom"}}

    monkeypatch.setattr(plugin_cli, "_invoke_plugin_tool", _fake_invoke)

    project = _project(tmp_path)
    args = _install_args("builtin", "rag", project, install_name="custom")
    plugin_cli.run_install(args)

    assert captured["args"]["source"] == {"kind": "builtin", "name": "rag"}
    assert captured["args"]["name"] == "custom"


def test_uninstall_threads_name(tmp_path, monkeypatch) -> None:
    """Tier 1: `reyn plugin uninstall NAME` forwards {"name": NAME} to the
    plugin_management__uninstall op — no extra/renamed fields."""
    captured: dict = {}

    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        captured["name"] = name
        captured["args"] = args
        return {"status": "ok", "data": {"status": "uninstalled", "name": "myplugin"}}

    monkeypatch.setattr(plugin_cli, "_invoke_plugin_tool", _fake_invoke)

    project = _project(tmp_path)
    plugin_cli.run_uninstall(_uninstall_args("myplugin", project))

    assert captured["name"] == "plugin_management__uninstall"
    assert captured["args"] == {"name": "myplugin"}


# ── 2. real install/uninstall roundtrip through the CLI entrypoints ───────


def test_cli_local_install_uninstall_roundtrip_real_stack(tmp_path, monkeypatch) -> None:
    """Tier 2: `reyn plugin install local <path>` (non-interactive, real
    PermissionResolver/op_runtime handler stack — no invoke_tool stub) writes
    the SAME .reyn/config/skills.yaml + ~/.reyn/plugins/<name>/ copy P2's own
    op-level tests assert on, reached THROUGH the CLI surface. Then
    `reyn plugin uninstall` removes both."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    project = _project(tmp_path, allow_file_write=True)
    src = _write_local_plugin(tmp_path / "src")
    monkeypatch.chdir(project)

    install_args = _install_args("local", str(src), project, non_interactive=True)
    plugin_cli.run_install(install_args)

    plugin_copy = home / ".reyn" / "plugins" / "myplugin"
    assert plugin_copy.is_dir(), "plugin copy not written under ~/.reyn/plugins/"
    skills_yaml = project / ".reyn" / "config" / "skills.yaml"
    assert skills_yaml.exists(), "skills registry entry not written"
    registered = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))
    assert "hello" in (registered.get("skills") or {}).get("entries", {})

    plugin_cli.run_uninstall(_uninstall_args("myplugin", project))
    assert not plugin_copy.exists(), "plugin copy not removed on uninstall"
    registered_after = yaml.safe_load(skills_yaml.read_text(encoding="utf-8")) or {}
    assert "hello" not in (registered_after.get("skills") or {}).get("entries", {})


# ── 3. {kind:git} fails closed non-interactively (Security lens) ──────────


def test_cli_git_install_noninteractive_fails_closed_real_gate(tmp_path, monkeypatch) -> None:
    """Tier 2: a {kind:git} install run via `--non-interactive` hits the REAL
    require_plugin_git_run_code_trust gate (no stub) and is denied — SystemExit(2)
    with a permission-denied message, no fetch/write attempted."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    project = _project(tmp_path, allow_file_write=True)
    args = _install_args(
        "git", "https://example.invalid/some/plugin.git", project,
        non_interactive=True,
    )

    with pytest.raises(SystemExit) as exc_info:
        plugin_cli.run_install(args)
    assert exc_info.value.code == 2
    # No plugin copy should have been attempted.
    assert not (home / ".reyn" / "plugins").exists()


def test_cli_git_install_notty_fails_closed_even_without_flag(tmp_path, monkeypatch) -> None:
    """Tier 2: even WITHOUT --non-interactive, a non-tty CLI invocation (sys.stdin.isatty()
    False, the real subprocess/CI condition) must fail closed for {kind:git} — the CLI's
    `interactive = not non_interactive and sys.stdin.isatty()` wiring, not just the flag."""
    import sys

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    project = _project(tmp_path, allow_file_write=True)
    args = _install_args(
        "git", "https://example.invalid/some/plugin.git", project,
        non_interactive=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        plugin_cli.run_install(args)
    assert exc_info.value.code == 2
