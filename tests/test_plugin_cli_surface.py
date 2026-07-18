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
import subprocess
from pathlib import Path

import pytest
import yaml

from reyn.interfaces.cli.commands import plugin as plugin_cli

# ── shared fixtures ─────────────────────────────────────────────────────────


def _make_git_plugin_repo(base: Path, name: str = "gitplugin") -> Path:
    """A real local git repo holding a minimal plugin (skills capability only —
    no requirements.txt ⇒ no dep-materialisation http.get), usable as a
    ``file://`` git source (mirrors tests/test_plugin_install.py::
    _make_git_plugin_repo). Using a REAL, reachable repo is what makes the
    run-code-gate strip-falsify meaningful: with the gate stripped the clone
    SUCCEEDS and the install proceeds, so the test flips RED — an unreachable
    URL would instead keep failing (clone error → same SystemExit) and mask
    the strip."""
    repo = base / "repo"
    (repo / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (repo / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "git plugin",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (repo / "skills" / "hi").mkdir(parents=True, exist_ok=True)
    (repo / "skills" / "hi" / "SKILL.md").write_text(
        "---\nname: hi\ndescription: from git\n---\n\nBody.\n", encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


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


def _project(
    tmp_path: Path, *, allow_file_write: bool = True, allow_web_fetch: bool = False,
) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    perms: dict = {}
    if allow_file_write:
        perms["file.write"] = "allow"
    # ``web.fetch: allow`` config-approves the http.get FETCH axis (git clone /
    # pypi reachability). Set it to ISOLATE the run-code trust gate: with the
    # fetch axis granted, the ONLY thing that can deny a {kind:git} install is
    # require_plugin_git_run_code_trust — so a green "fails closed" result
    # actually witnesses THAT gate, not a redundant fetch-axis deny (mirrors
    # the P2 op-level test's approve_all_http=True isolation).
    if allow_web_fetch:
        perms["web.fetch"] = "allow"
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
    # Deliberately NO chdir: `--project` must load the target project's
    # permissions itself (load_config(cwd=project_root)); a cwd-dependent
    # install would be a latent `--project` bug.

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
#
# CONFOUND ISOLATION (why these use a REAL local git repo + web.fetch:allow):
# the {kind:git} handler runs the run-code trust gate FIRST, then (for an https
# host) require_http_get, then clones. If the test used an UNREACHABLE
# `https://example.invalid/...` URL with web.fetch NOT granted, THREE independent
# causes each produce the SAME SystemExit(2): the run-code deny, the http.get
# non-interactive deny, AND the clone failure. Stripping the run-code gate would
# leave the test GREEN (masked by the other two) — "green ≠ the gate ran". Using
# a REAL, reachable `file://` repo removes ALL of that: `_source_host` returns
# None for file:// so require_http_get is skipped (web.fetch:allow is belt-and-
# suspenders isolation of the fetch axis regardless), and the clone SUCCEEDS —
# so the run-code trust gate is the ONE remaining thing that can deny. Strip it
# and the install proceeds (no SystemExit) → the test flips RED. Verified by
# strip-falsify (temporarily removing the gate call) — see PR notes.


def test_cli_git_install_noninteractive_fails_closed_real_gate(tmp_path, monkeypatch) -> None:
    """Tier 2: a {kind:git} install run via `--non-interactive`, against a REAL
    reachable file:// repo with the fetch axis fully granted (web.fetch:allow),
    is denied ONLY by the REAL require_plugin_git_run_code_trust gate (no stub)
    — SystemExit(2), nothing installed. Isolated so stripping the gate flips it
    RED (the clone would otherwise succeed)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    repo = _make_git_plugin_repo(tmp_path)
    project = _project(tmp_path, allow_file_write=True, allow_web_fetch=True)
    args = _install_args("git", repo.as_uri(), project, non_interactive=True)

    with pytest.raises(SystemExit) as exc_info:
        plugin_cli.run_install(args)
    assert exc_info.value.code == 2
    # Gate fired BEFORE any clone/copy: nothing landed under ~/.reyn/plugins/.
    installed = [
        p for p in (home / ".reyn" / "plugins").glob("*")
        if not p.name.startswith(".")
    ] if (home / ".reyn" / "plugins").exists() else []
    assert installed == [], f"git plugin installed despite no run-code trust: {installed}"


def test_cli_git_install_notty_fails_closed_even_without_flag(tmp_path, monkeypatch) -> None:
    """Tier 2: even WITHOUT --non-interactive, a non-tty CLI invocation (sys.stdin.isatty()
    False, the real subprocess/CI condition) must fail closed for {kind:git} — the CLI's
    `interactive = not non_interactive and sys.stdin.isatty()` wiring, not just the flag.
    Same real-repo + web.fetch:allow isolation as above so the run-code gate is the sole
    denier."""
    import sys

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    repo = _make_git_plugin_repo(tmp_path)
    project = _project(tmp_path, allow_file_write=True, allow_web_fetch=True)
    args = _install_args("git", repo.as_uri(), project, non_interactive=False)

    with pytest.raises(SystemExit) as exc_info:
        plugin_cli.run_install(args)
    assert exc_info.value.code == 2
    installed = [
        p for p in (home / ".reyn" / "plugins").glob("*")
        if not p.name.startswith(".")
    ] if (home / ".reyn" / "plugins").exists() else []
    assert installed == [], f"git plugin installed despite no run-code trust: {installed}"
