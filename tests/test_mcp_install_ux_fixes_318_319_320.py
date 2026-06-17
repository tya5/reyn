"""Tier 2: ``reyn mcp install`` UX fixes (issues #318 + #319 + #320).

Pre-fix every install via ``reyn mcp install --source ...`` produced a
config that needed THREE manual edits before any skill could use it:

  1. **#318**: add ``type: stdio`` (loader rejected without it)
  2. **#319**: rename ``server-foo`` → ``foo`` (stdlib skills expect
     the short form)
  3. **#320**: on macOS, replace ``/tmp`` sandbox path with a non-
     symlink path (server's literal-path check denies otherwise)

The trio was discovered + filed in the 2026-05-20 smoke-test round
(see ``docs/guide/for-users/popular-mcp-servers.md``'s Known Issues
table for the workaround pattern users had to apply on every install).

This file pins the post-fix behaviour so users never see those three
gotchas again, and so a regression of any one of the three fails CI
before reaching the docs / users.

Pins (= one section per issue):

  - **#318** ``_build_server_entry`` always writes ``type: <transport_type>``
    even for the default ``stdio`` case. Pre-fix the field was omitted
    for stdio and written under the wrong key (``transport``) for
    non-stdio.
  - **#319** ``_npm_package_name`` / ``_pypi_package_name`` /
    ``_resolve_github_url`` all strip ``server-`` and ``mcp-server-``
    prefixes from the derived short name.
  - **#320** ``reyn mcp install`` warns on Darwin when ``--args``
    contains ``/tmp`` paths.
"""
from __future__ import annotations

import platform
import subprocess
import sys

from reyn.core.op_runtime.mcp_install import _build_server_entry
from reyn.core.registry.source_resolver import (
    _npm_package_name,
    _pypi_package_name,
    _strip_mcp_prefix,
    resolve,
)

# ── #318: type: stdio in every install ────────────────────────────────


def test_build_server_entry_writes_type_stdio_for_npm() -> None:
    """Tier 2: npm package entry includes ``type: stdio``.

    Pre-#318 the field was omitted because the default-case branch
    didn't write it; the loader then rejected with 'Unsupported MCP
    server type: None'.
    """
    pkg_raw = {
        "registryType": "npm",
        "identifier": "@modelcontextprotocol/server-filesystem",
        "version": "",
        "transport": {"type": "stdio"},
        "environmentVariables": [],
    }
    entry = _build_server_entry(pkg_raw, env_keys=[])
    assert entry["type"] == "stdio"
    assert entry["command"] == "npx"


def test_build_server_entry_writes_type_stdio_for_pypi() -> None:
    """Tier 2: pypi package entry also gets ``type: stdio``."""
    pkg_raw = {
        "registryType": "pypi",
        "identifier": "mcp-server-time",
        "version": "",
        "transport": {"type": "stdio"},
        "environmentVariables": [],
    }
    entry = _build_server_entry(pkg_raw, env_keys=[])
    assert entry["type"] == "stdio"
    assert entry["command"] == "uvx"


def test_build_server_entry_writes_type_stdio_for_docker() -> None:
    """Tier 2: docker registry-type also gets ``type: stdio``."""
    pkg_raw = {
        "registryType": "docker",
        "identifier": "my-org/my-mcp:latest",
        "version": "",
        "transport": {"type": "stdio"},
        "environmentVariables": [],
    }
    entry = _build_server_entry(pkg_raw, env_keys=[])
    assert entry["type"] == "stdio"
    assert entry["command"] == "docker"


def test_build_server_entry_writes_type_http_when_specified() -> None:
    """Tier 2: explicit non-stdio transport is written under the
    canonical ``type:`` key, NOT the pre-fix ``transport:`` key.
    The loader reads ``type:`` and rejected ``transport:``.
    """
    pkg_raw = {
        "registryType": "npm",
        "identifier": "some/http-server",
        "version": "",
        "transport": {"type": "http"},
        "environmentVariables": [],
    }
    entry = _build_server_entry(pkg_raw, env_keys=[])
    assert entry["type"] == "http"
    # Pre-fix bug: would set entry["transport"] = "http" instead.
    assert "transport" not in entry


# ── #319: ``server-`` and ``mcp-server-`` prefix strip ───────────────


def test_strip_mcp_prefix_anthropic_server_dash() -> None:
    """Tier 2: ``server-<name>`` (= Anthropic-official convention) is
    stripped.
    """
    assert _strip_mcp_prefix("server-filesystem") == "filesystem"
    assert _strip_mcp_prefix("server-memory") == "memory"
    assert _strip_mcp_prefix("server-everything") == "everything"


def test_strip_mcp_prefix_thirdparty_mcp_server_dash() -> None:
    """Tier 2: ``mcp-server-<name>`` (= third-party convention) is
    stripped (= PyPI ecosystem standard).
    """
    assert _strip_mcp_prefix("mcp-server-time") == "time"
    assert _strip_mcp_prefix("mcp-server-fetch") == "fetch"
    assert _strip_mcp_prefix("mcp-server-git") == "git"


def test_strip_mcp_prefix_no_prefix_passthrough() -> None:
    """Tier 2: a name without a known prefix is returned unchanged."""
    assert _strip_mcp_prefix("custom-tool") == "custom-tool"
    assert _strip_mcp_prefix("foo") == "foo"


def test_strip_mcp_prefix_does_not_strip_to_empty() -> None:
    """Tier 2: defensive — a name that IS exactly the prefix doesn't
    get stripped to empty (= would produce an unusable config key).
    """
    assert _strip_mcp_prefix("server-") == "server-"
    assert _strip_mcp_prefix("mcp-server-") == "mcp-server-"


def test_npm_package_name_strips_prefix() -> None:
    """Tier 2: ``_npm_package_name`` returns the stripped form."""
    assert _npm_package_name("@modelcontextprotocol/server-filesystem") == "filesystem"
    assert _npm_package_name("server-everything") == "everything"


def test_pypi_package_name_strips_prefix() -> None:
    """Tier 2: ``_pypi_package_name`` returns the stripped form."""
    assert _pypi_package_name("mcp-server-time") == "time"
    assert _pypi_package_name("mcp-server-fetch") == "fetch"


def test_resolve_npm_canonical_server_name_post_319() -> None:
    """Tier 2: resolver's ``server_name`` is the prefix-stripped short form (integration).

    Pins the install→loader→stdlib-skill contract so stdlib expectations
    (= ``mcp.servers.filesystem``, not ``mcp.servers.server-filesystem``)
    are met without rename.
    """
    r = resolve("npm:@modelcontextprotocol/server-filesystem")
    assert r.server_name == "filesystem"


def test_resolve_pypi_canonical_server_name_post_319() -> None:
    """Tier 2: same for pypi path."""
    r = resolve("pypi:mcp-server-time")
    assert r.server_name == "time"


def test_resolve_github_canonical_server_name_post_319() -> None:
    """Tier 2: same for github → npm path."""
    r = resolve(
        "https://github.com/modelcontextprotocol/servers"
        "/tree/main/src/filesystem",
    )
    assert r.server_name == "filesystem"


# ── #320: macOS /tmp install-time warning ────────────────────────────


def test_install_command_warns_on_tmp_args_on_darwin(tmp_path) -> None:
    """Tier 2: on macOS, ``reyn mcp install --args /tmp/...`` emits a
    warning explaining the symlink gotcha. Skipped on non-Darwin
    because the warning is platform-gated.

    Drives the actual CLI subprocess (= ``reyn mcp install``) with a
    bogus source so the command errors quickly without network — we
    only need to capture the pre-install warning, which is emitted
    BEFORE the resolver runs.
    """
    if platform.system() != "Darwin":
        import pytest
        pytest.skip("warning is Darwin-gated by design")

    # Use a known-unresolvable source so install fails fast.
    # The warning we're checking fires BEFORE the resolver, so the
    # subsequent install failure is irrelevant to the assertion.
    import shutil
    reyn_bin = shutil.which("reyn")
    if reyn_bin is None:
        import pytest
        pytest.skip("reyn executable not on PATH for subprocess invocation")
    # #1442: install now resolves a project root and fails loud outside one
    # (error-not-silent-cwd), so give tmp_path a reyn.yaml — the #320 /tmp-args
    # warning fires inside the source install, past project resolution.
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    result = subprocess.run(
        [
            reyn_bin, "mcp", "install",
            "--source", "npm:nonexistent-bogus-pkg",
            "--args", "/tmp/some-sandbox",
            "--non-interactive",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert "/tmp" in combined
    assert "symlink" in combined or "literal" in combined, (
        f"expected #320 warning text; got combined output: {combined!r}"
    )
