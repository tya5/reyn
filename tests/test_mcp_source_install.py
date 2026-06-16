"""Tier 2: --source install path invariants.

Covers:
  - source_resolver.resolve() returns correct metadata for all supported schemes
    (npm:, pypi:, docker:, GitHub URL with known scope, GitHub URL unknown repo)
  - mcp_install op handler: source path skips registry fetch and installs correctly
  - mcp_install op handler: source resolution failure → error result (no install)
  - CLI argparse: --source flag parsed; SERVER_ID made optional
  - CLI argparse: SERVER_ID and --source are mutually exclusive (error on both)
  - CLI argparse: neither SERVER_ID nor --source → error
  - Existing registry-based install test regression: SERVER_ID alone still works
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from reyn.registry.source_resolver import SourceResolution, resolve

# ===========================================================================
# Part 1: source_resolver unit tests
# ===========================================================================


class TestSourceResolverNpm:
    def test_npm_bare_package(self):
        """Tier 2: npm: scheme resolves a bare package name to npx runtime.

        issue #319: ``server-`` prefix is stripped from the derived
        config key so the stdlib skill expectations align without a
        manual rename. The ``identifier`` keeps the npm-canonical name.
        """
        r = resolve("npm:@modelcontextprotocol/server-filesystem")
        assert r.error == ""
        assert r.runtime_hint == "npx"
        assert r.server_name == "filesystem"
        assert r.packages_raw[0]["registryType"] == "npm"
        assert r.packages_raw[0]["identifier"] == "@modelcontextprotocol/server-filesystem"
        assert r.packages_raw[0]["version"] == ""

    def test_npm_scoped_with_version(self):
        """Tier 2: npm: scheme parses version from scoped package."""
        r = resolve("npm:@modelcontextprotocol/server-filesystem@0.6.2")
        assert r.error == ""
        assert r.runtime_hint == "npx"
        assert r.packages_raw[0]["identifier"] == "@modelcontextprotocol/server-filesystem"
        assert r.packages_raw[0]["version"] == "0.6.2"

    def test_npm_unscoped_package(self):
        """Tier 2: npm: scheme resolves an unscoped package.

        issue #319: the ``mcp-server-`` prefix is also stripped (= the
        third-party convention).
        """
        r = resolve("npm:mcp-server-tool")
        assert r.error == ""
        assert r.runtime_hint == "npx"
        assert r.server_name == "tool"

    def test_npm_empty_package_returns_error(self):
        """Tier 2: npm: with no package name returns error."""
        r = resolve("npm:")
        assert r.error != ""
        assert r.runtime_hint == ""

    def test_npm_source_preserved(self):
        """Tier 2: source field is set to the original specifier."""
        spec = "npm:@example/my-mcp"
        r = resolve(spec)
        assert r.source == spec


class TestSourceResolverPypi:
    def test_pypi_bare_package(self):
        """Tier 2: pypi: scheme resolves a bare package name to uvx runtime."""
        r = resolve("pypi:my-mcp-server")
        assert r.error == ""
        assert r.runtime_hint == "uvx"
        assert r.server_name == "my-mcp-server"
        assert r.packages_raw[0]["registryType"] == "pypi"
        assert r.packages_raw[0]["identifier"] == "my-mcp-server"

    def test_pypi_with_version(self):
        """Tier 2: pypi: scheme parses == version constraint."""
        r = resolve("pypi:my-mcp-server==2.0.0")
        assert r.error == ""
        assert r.packages_raw[0]["identifier"] == "my-mcp-server"
        assert r.packages_raw[0]["version"] == "2.0.0"

    def test_pypi_empty_package_returns_error(self):
        """Tier 2: pypi: with no package name returns error."""
        r = resolve("pypi:")
        assert r.error != ""


class TestSourceResolverDocker:
    def test_docker_image(self):
        """Tier 2: docker: scheme resolves to docker runtime."""
        r = resolve("docker:my-org/my-mcp-server")
        assert r.error == ""
        assert r.runtime_hint == "docker"
        assert r.server_name == "my-mcp-server"
        assert r.packages_raw[0]["registryType"] == "docker"
        assert r.packages_raw[0]["identifier"] == "my-org/my-mcp-server"

    def test_docker_with_tag(self):
        """Tier 2: docker: scheme with image:tag is accepted."""
        r = resolve("docker:my-org/my-mcp-server:latest")
        assert r.error == ""
        assert r.runtime_hint == "docker"

    def test_docker_empty_image_returns_error(self):
        """Tier 2: docker: with no image returns error."""
        r = resolve("docker:")
        assert r.error != ""


class TestSourceResolverGitHub:
    def test_github_known_repo_with_subdir(self):
        """Tier 2: GitHub URL for known repo with src/<subdir> resolves to npm.

        issue #319: same prefix-strip applies to the github→npm path.
        """
        url = "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem"
        r = resolve(url)
        assert r.error == ""
        assert r.runtime_hint == "npx"
        assert r.server_name == "filesystem"
        assert r.packages_raw[0]["identifier"] == "@modelcontextprotocol/server-filesystem"

    def test_github_known_repo_with_github_subdir(self):
        """Tier 2: GitHub URL for github subdir resolves to npm package."""
        url = "https://github.com/modelcontextprotocol/servers/tree/main/src/github"
        r = resolve(url)
        assert r.error == ""
        assert r.runtime_hint == "npx"
        assert r.server_name == "github"
        assert r.packages_raw[0]["identifier"] == "@modelcontextprotocol/server-github"

    def test_github_unknown_repo_no_runtime(self):
        """Tier 2: GitHub URL for unknown repo returns empty runtime_hint."""
        url = "https://github.com/some-org/some-private-mcp-server"
        r = resolve(url)
        assert r.error == ""
        assert r.runtime_hint == ""  # unknown — caller must handle graceful degrade
        assert r.server_name != ""   # some server_name is still derived

    def test_github_malformed_url_returns_error(self):
        """Tier 2: malformed GitHub URL returns error."""
        r = resolve("https://github.com/")  # no owner/repo
        assert r.error != ""

    def test_github_source_preserved(self):
        """Tier 2: source field matches original URL."""
        url = "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem"
        r = resolve(url)
        assert r.source == url

    def test_github_http_lowercase(self):
        """Tier 2: http:// variant of GitHub URL is also accepted."""
        url = "http://github.com/modelcontextprotocol/servers/tree/main/src/postgres"
        r = resolve(url)
        assert r.error == ""


class TestSourceResolverUnknownScheme:
    def test_unknown_scheme_returns_error(self):
        """Tier 2: unrecognised prefix returns error with guidance."""
        r = resolve("ftp://example.com/my-server")
        assert r.error != ""
        assert "Unrecognised" in r.error

    def test_empty_string_returns_error(self):
        """Tier 2: empty specifier returns error."""
        r = resolve("")
        assert r.error != ""


# ===========================================================================
# Part 2: mcp_install op handler — source path
# ===========================================================================

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.mcp_install import handle as mcp_install_handle
from reyn.schemas.models import MCPInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _AutoApproveInterventionBus:
    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        if iv.choices:
            return InterventionAnswer(choice_id="always")
        return InterventionAnswer(text="test-secret-value")


def _make_resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=True,
    )


def _phase5_source_install_decl(resolver: PermissionResolver) -> PermissionDecl:
    """Phase 5 + Phase 6 successor to ``PermissionDecl(mcp_install=True)``.

    Source installs skip the registry fetch but still write to
    ``.reyn/mcp.yaml`` — declare the file.write entry. Wildcard
    secret.write (= #571 Phase 6) covers any isSecret env vars the
    source resolver surfaces.
    """
    canonical_config = str(resolver._project_root / ".reyn" / "mcp.yaml")
    resolver.session_approve_path(canonical_config, "mcp_install_source_test", "file.write")
    return PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        secret_write=["*"],
    )


def _make_op_ctx(
    tmp_path: Path,
    resolver: PermissionResolver,
    bus: object,
    decl: PermissionDecl,
) -> OpContext:
    events = EventLog()
    workspace = type("Workspace", (), {"root": str(tmp_path)})()
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=resolver,
        skill_name="mcp_install_source_test",
        intervention_bus=bus,
    )


def _run(coro):
    return asyncio.run(coro)


def test_source_npm_skips_registry_and_installs(tmp_path, monkeypatch):
    """Tier 2: --source npm: skips registry fetch and writes config via npx."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_source_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="npm:@modelcontextprotocol/server-filesystem",
        scope="local",
        source="npm:@modelcontextprotocol/server-filesystem",
    )

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    # RegistryClient._get must NOT be called; if it is, it would fail
    # because we're not patching it.  The test passes only if it's skipped.
    result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["runtime"] == "npx"
    assert result["source"] == "npm:@modelcontextprotocol/server-filesystem"

    # Config file written. issue #319: server key is the prefix-stripped
    # short form. issue #318: ``type: stdio`` is present so the loader
    # accepts the entry without manual edit.
    config_path = tmp_path / ".reyn" / "mcp.yaml"
    assert config_path.exists()
    import yaml
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    servers = written["mcp"]["servers"]
    assert "filesystem" in servers
    entry = servers["filesystem"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "npx"
    assert "@modelcontextprotocol/server-filesystem" in " ".join(str(a) for a in entry["args"])


def test_source_pypi_skips_registry_and_installs(tmp_path, monkeypatch):
    """Tier 2: --source pypi: skips registry fetch and writes config via uvx."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_source_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="pypi:my-mcp-server",
        scope="local",
        source="pypi:my-mcp-server",
    )

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uvx")
    result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["runtime"] == "uvx"

    import yaml
    config_path = tmp_path / ".reyn" / "mcp.yaml"
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    servers = written["mcp"]["servers"]
    assert "my-mcp-server" in servers
    assert servers["my-mcp-server"]["command"] == "uvx"


def test_source_invalid_specifier_returns_error(tmp_path):
    """Tier 2: unresolvable --source returns error result without raising."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_source_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="bad-source",
        scope="local",
        source="ftp://not-a-valid-source",
    )

    result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "error"
    assert "Source resolution failed" in result["error"]
    # Config file must NOT have been written
    config_path = tmp_path / ".reyn" / "mcp.yaml"
    assert not config_path.exists()


def test_source_event_includes_source_field(tmp_path, monkeypatch):
    """Tier 2: mcp_server_installed event carries source field when source install used (P6)."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_source_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    source_spec = "npm:@modelcontextprotocol/server-filesystem"
    op = MCPInstallIROp(
        kind="mcp_install",
        server_id=source_spec,
        scope="local",
        source=source_spec,
    )

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    _run(mcp_install_handle(op, ctx, "control_ir"))

    events = ctx.events.all()
    install_events = [e for e in events if e.type == "mcp_server_installed"]
    assert install_events, "Expected mcp_server_installed event"
    evt = install_events[0]
    assert "source" in evt.data
    assert evt.data["source"] == source_spec


def test_source_github_known_installs_npm(tmp_path, monkeypatch):
    """Tier 2: GitHub URL for known modelcontextprotocol repo resolves to npm and installs."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_source_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    github_url = "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem"
    op = MCPInstallIROp(
        kind="mcp_install",
        server_id=github_url,
        scope="local",
        source=github_url,
    )

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["runtime"] == "npx"

    import yaml
    config_path = tmp_path / ".reyn" / "mcp.yaml"
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    servers = written["mcp"]["servers"]
    # issue #319: prefix-stripped short key.
    assert "filesystem" in servers


def test_source_missing_runtime_returns_error(tmp_path, monkeypatch):
    """Tier 2: source install returns error when runtime binary is absent."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_source_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="npm:@example/server",
        scope="local",
        source="npm:@example/server",
    )

    monkeypatch.setattr("shutil.which", lambda name: None)
    result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "error"
    assert "npx" in result["error"].lower() or "node" in result["error"].lower()


def test_registry_path_unaffected_by_source_field_being_none(tmp_path, monkeypatch):
    """Tier 2: registry path still works when source=None (regression guard).

    The source=None path hits the registry fetch, so the decl must
    include the http.get host as well as the file.write entry.
    """
    resolver = _make_resolver(tmp_path)
    decl = _phase5_source_install_decl(resolver)
    decl.http_get = [{"host": "registry.modelcontextprotocol.io"}]
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.modelcontextprotocol/server-filesystem",
        scope="local",
        source=None,  # registry path
    )

    _FILESYSTEM_RESPONSE = {
        "name": "io.github.modelcontextprotocol/server-filesystem",
        "description": "Filesystem MCP server",
        "version": "0.6.2",
        "repository": {"url": "https://github.com/modelcontextprotocol/servers"},
        "$schema": "https://static.modelcontextprotocol.io/schemas/server.schema.json",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@modelcontextprotocol/server-filesystem",
                "version": "0.6.2",
                "transport": {"type": "stdio"},
                "environmentVariables": [],
            }
        ],
        "remotes": [],
    }

    async def _fake_get(self, path: str, params=None, base_url=None):
        return {"server": _FILESYSTEM_RESPONSE}

    from reyn.registry.client import RegistryClient
    monkeypatch.setattr(RegistryClient, "_get", _fake_get)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx")
    result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["source"] == ""  # no source in registry path


# ===========================================================================
# Part 3: CLI argparse — --source flag and mutual exclusivity
# ===========================================================================

from reyn.cli.commands.mcp import register


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def test_install_source_flag_parsed():
    """Tier 2: 'mcp install --source npm:...' is parsed; source attribute is set."""
    parser = _make_parser()
    args = parser.parse_args([
        "mcp", "install",
        "--source", "npm:@modelcontextprotocol/server-filesystem",
        "--non-interactive",
    ])
    assert args.mcp_command == "install"
    assert args.source == "npm:@modelcontextprotocol/server-filesystem"
    assert args.server_id is None
    assert args.non_interactive is True


def test_install_server_id_without_source():
    """Tier 2: 'mcp install SERVER_ID' still works (no --source)."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "install", "io.github.foo/bar"])
    assert args.server_id == "io.github.foo/bar"
    assert args.source is None


def test_install_source_with_scope():
    """Tier 2: --source works with --scope flag."""
    parser = _make_parser()
    args = parser.parse_args([
        "mcp", "install",
        "--source", "pypi:my-mcp",
        "--scope", "user",
    ])
    assert args.source == "pypi:my-mcp"
    assert args.scope == "user"
    assert args.server_id is None


def test_install_source_with_env_flags():
    """Tier 2: --source works with --env flags."""
    parser = _make_parser()
    args = parser.parse_args([
        "mcp", "install",
        "--source", "npm:@example/server",
        "--env", "TOKEN=abc",
    ])
    assert args.source == "npm:@example/server"
    assert args.env == ["TOKEN=abc"]


def test_install_both_server_id_and_source_rejects(tmp_path, capsys):
    """Tier 2: SERVER_ID + --source together → sys.exit(1) with error message."""
    parser = _make_parser()
    args = parser.parse_args([
        "mcp", "install", "io.github.foo/bar",
        "--source", "npm:@example/server",
        "--non-interactive",
    ])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err.lower()


def test_install_neither_server_id_nor_source_rejects(tmp_path, capsys):
    """Tier 2: no SERVER_ID and no --source → sys.exit(1) with error message."""
    parser = _make_parser()
    args = parser.parse_args(["mcp", "install", "--non-interactive"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "source" in err.lower() or "server_id" in err.lower() or "provide" in err.lower()


def test_mcp_install_irop_source_field_defaults_none():
    """Tier 2: MCPInstallIROp.source defaults to None (backward compat)."""
    op = MCPInstallIROp(kind="mcp_install", server_id="io.example/server")
    assert op.source is None
