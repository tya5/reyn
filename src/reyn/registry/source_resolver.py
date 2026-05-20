"""source_resolver.py — resolve a ``--source`` specifier to ServerJson-equivalent metadata.

Supported specifier forms
--------------------------
``npm:<package>[@<version>]``
    e.g. ``npm:@modelcontextprotocol/server-filesystem``
    e.g. ``npm:@modelcontextprotocol/server-filesystem@0.6.2``

``pypi:<package>[==<version>]``
    e.g. ``pypi:my-mcp-server``

``docker:<image>[:<tag>]``
    e.g. ``docker:my-org/my-mcp-server``

``https://github.com/<owner>/<repo>[/tree/<ref>/src/<subdir>]``
    e.g. ``https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem``

    Heuristic: GitHub URLs with a known npm scope convention
    (``@modelcontextprotocol/server-<subdir>``) are resolved to an npm entry.
    All other GitHub URLs are left as an unknown runtime — the caller receives
    ``runtime_hint=""`` and must handle the graceful-degradation path.

Resolution output
-----------------
``resolve(source: str) -> SourceResolution``

``SourceResolution`` carries the same fields the handler in
``op_runtime/mcp_install.py`` needs from ``ServerJson`` so the two install
paths (registry vs source) share the same downstream logic without re-importing
``ServerJson``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public data class
# ---------------------------------------------------------------------------

@dataclass
class SourceResolution:
    """Resolved metadata from a --source specifier.

    Mirrors the fields consumed by ``mcp_install.handle`` from ``ServerJson``:
      - ``server_name``    short config key (e.g. "server-filesystem")
      - ``runtime_hint``   "npx" | "uvx" | "docker" | ""
      - ``packages_raw``   list[dict] — same shape as server.json packages[]
      - ``remotes_raw``    list[dict] — same shape as server.json remotes[]
      - ``raw``            raw server.json-equivalent dict for forward compat
      - ``source``         the original specifier string
      - ``error``          non-empty if resolution failed; caller should check
    """

    server_name: str = ""
    runtime_hint: str = ""
    packages_raw: list[dict[str, Any]] = field(default_factory=list)
    remotes_raw: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# issue #319: common MCP-server-name prefixes that are stripped when
# deriving the short config key. The MCP ecosystem widely uses
# ``server-<name>`` (= Anthropic official) and ``mcp-server-<name>``
# (= third-party convention). Stripping these gives a short key that
# matches stdlib skill expectations (e.g. ``read_local_files`` looks
# for ``mcp.servers.filesystem``, not ``mcp.servers.server-filesystem``).
_MCP_NAME_PREFIXES = ("mcp-server-", "server-")


def _strip_mcp_prefix(name: str) -> str:
    """Strip common ``server-`` / ``mcp-server-`` prefixes (issue #319).

    Returns the input unchanged when no prefix matches.
    """
    for prefix in _MCP_NAME_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def _npm_package_name(identifier: str) -> str:
    """Derive a short config key from an npm package name.

    ``@modelcontextprotocol/server-filesystem`` → ``filesystem``
    ``mcp-server-foo``                          → ``foo``
    ``my-mcp-server``                           → ``my-mcp-server``
    ``@scope/plain-name``                       → ``plain-name``

    issue #319: pre-fix the function returned ``server-filesystem``
    verbatim (= the npm package's last path component). The stdlib
    ``read_local_files`` skill expects ``mcp.servers.filesystem`` —
    every install through this path required a manual rename. Now
    the ecosystem-standard prefixes are stripped automatically.
    """
    if "/" in identifier:
        name = identifier.split("/")[-1]
    else:
        name = identifier
    return _strip_mcp_prefix(name)


def _pypi_package_name(identifier: str) -> str:
    """Derive a short name from a PyPI package name.

    ``mcp-server-time``  → ``time``
    ``mcp-server-fetch`` → ``fetch``
    ``my-mcp-tool``      → ``my-mcp-tool``

    issue #319: same prefix-strip as ``_npm_package_name`` so the
    PyPI install path produces the same canonical short names as
    the npm path. Pre-fix ``pypi:mcp-server-time`` installed as
    ``mcp-server-time``; now installs as ``time``.
    """
    # Strip any version specifier
    base = re.split(r"[=><!]", identifier)[0].strip()
    return _strip_mcp_prefix(base.replace("_", "-"))


def _docker_image_name(image: str) -> str:
    """Derive a short name from a Docker image reference."""
    # Strip tag, take last path component
    name = image.split(":")[0].split("/")[-1]
    return name


# ---------------------------------------------------------------------------
# Scheme-specific resolvers
# ---------------------------------------------------------------------------

def _resolve_npm(specifier: str) -> SourceResolution:
    """Resolve ``npm:<package>[@<version>]``."""
    # Strip "npm:" prefix
    raw_pkg = specifier[4:].strip()
    if not raw_pkg:
        return SourceResolution(
            source=specifier,
            error="npm: specifier must include a package name (e.g. npm:@scope/package)",
        )

    # Split version: support @scope/package@version and plain package@version
    # Scoped packages start with @, so we look for @ after the first char.
    version = ""
    pkg_without_scope = raw_pkg.lstrip("@")
    if "@" in pkg_without_scope:
        # Find the position of the version @ in the original string
        idx = raw_pkg.index("@", 1) if raw_pkg.startswith("@") else raw_pkg.index("@")
        identifier = raw_pkg[:idx]
        version = raw_pkg[idx + 1:]
    else:
        identifier = raw_pkg

    args = ["-y", identifier]
    if version:
        args = ["-y", f"{identifier}@{version}"]

    packages_raw = [
        {
            "registryType": "npm",
            "identifier": identifier,
            "version": version,
            "transport": {"type": "stdio"},
            "environmentVariables": [],
        }
    ]

    return SourceResolution(
        server_name=_npm_package_name(identifier),
        runtime_hint="npx",
        packages_raw=packages_raw,
        raw={"packages": packages_raw},
        source=specifier,
    )


def _resolve_pypi(specifier: str) -> SourceResolution:
    """Resolve ``pypi:<package>[==<version>]``."""
    raw_pkg = specifier[5:].strip()
    if not raw_pkg:
        return SourceResolution(
            source=specifier,
            error="pypi: specifier must include a package name (e.g. pypi:my-mcp-server)",
        )

    # Parse version constraint
    version = ""
    m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*==\s*(.+)$", raw_pkg)
    if m:
        identifier = m.group(1).strip()
        version = m.group(2).strip()
    else:
        identifier = raw_pkg

    pkg_arg = identifier if not version else f"{identifier}=={version}"

    packages_raw = [
        {
            "registryType": "pypi",
            "identifier": identifier,
            "version": version,
            "transport": {"type": "stdio"},
            "environmentVariables": [],
        }
    ]

    return SourceResolution(
        server_name=_pypi_package_name(identifier),
        runtime_hint="uvx",
        packages_raw=packages_raw,
        raw={"packages": packages_raw},
        source=specifier,
    )


def _resolve_docker(specifier: str) -> SourceResolution:
    """Resolve ``docker:<image>[:<tag>]``."""
    image = specifier[7:].strip()
    if not image:
        return SourceResolution(
            source=specifier,
            error="docker: specifier must include an image name (e.g. docker:my-org/my-server)",
        )

    packages_raw = [
        {
            "registryType": "docker",
            "identifier": image,
            "version": "",
            "transport": {"type": "stdio"},
            "environmentVariables": [],
        }
    ]

    return SourceResolution(
        server_name=_docker_image_name(image),
        runtime_hint="docker",
        packages_raw=packages_raw,
        raw={"packages": packages_raw},
        source=specifier,
    )


# ---------------------------------------------------------------------------
# GitHub URL heuristics
# ---------------------------------------------------------------------------

# Mapping: known GitHub repo paths → npm scope prefix
# Key: "<owner>/<repo>" (lowercased)
# Value: npm scope (e.g. "@modelcontextprotocol")
_KNOWN_GITHUB_NPM_SCOPES: dict[str, str] = {
    "modelcontextprotocol/servers": "@modelcontextprotocol",
}


def _resolve_github_url(url: str) -> SourceResolution:
    """Resolve a GitHub URL to server metadata using heuristics.

    Supported URL shapes:
      https://github.com/<owner>/<repo>
      https://github.com/<owner>/<repo>/tree/<ref>/src/<subdir>

    Heuristic logic:
      1. If the owner/repo pair has a known npm scope mapping and the URL
         contains a ``src/<subdir>`` path component, resolve as npm with
         package ``@<scope>/server-<subdir>``.
      2. Otherwise, derive a server_name from the last path component and
         return ``runtime_hint=""`` — the handler will not check for a
         runtime binary and will store the raw URL as a note.
    """
    # Normalise: strip query/fragment, trailing slash
    clean = url.split("?")[0].split("#")[0].rstrip("/")

    # Parse path components after "github.com"
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+)(?:/(.*))?)?$",
        clean,
        re.IGNORECASE,
    )
    if not m:
        return SourceResolution(
            source=url,
            error=f"Unrecognised GitHub URL format: {url!r}. "
                  "Expected: https://github.com/<owner>/<repo>[/tree/<ref>/...]",
        )

    owner = m.group(1)
    repo = m.group(2)
    ref = m.group(3) or "main"
    subpath = m.group(4) or ""  # e.g. "src/filesystem"

    repo_key = f"{owner}/{repo}".lower()

    # Extract the last meaningful path segment as the subdir name
    subdir = subpath.rstrip("/").split("/")[-1] if subpath else ""

    # Check known npm scope mapping
    npm_scope = _KNOWN_GITHUB_NPM_SCOPES.get(repo_key)
    if npm_scope and subdir:
        # Convention: @scope/server-<subdir>
        package_name = f"{npm_scope}/server-{subdir}"
        packages_raw = [
            {
                "registryType": "npm",
                "identifier": package_name,
                "version": "",
                "transport": {"type": "stdio"},
                "environmentVariables": [],
                "_source_url": url,
            }
        ]
        return SourceResolution(
            # issue #319: don't reintroduce the ``server-`` prefix here
            # either — the github resolver builds the same config key
            # the npm path produces, so the stripped form is canonical.
            server_name=subdir,
            runtime_hint="npx",
            packages_raw=packages_raw,
            raw={"packages": packages_raw, "_source_github_url": url},
            source=url,
        )

    # Unknown repo — derive a best-effort server name
    server_name = subdir or repo.lower()
    # Remove common suffixes so the config key is clean
    for suffix in ("-mcp", "-server", "_mcp", "_server"):
        if server_name.endswith(suffix):
            server_name = server_name[: -len(suffix)]
            break

    return SourceResolution(
        server_name=server_name or repo.lower(),
        runtime_hint="",  # unknown; handler must gracefully degrade
        packages_raw=[],
        raw={"_source_github_url": url, "_owner": owner, "_repo": repo, "_ref": ref, "_subpath": subpath},
        source=url,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve(source: str) -> SourceResolution:
    """Resolve a ``--source`` specifier string to ``SourceResolution``.

    Dispatch table:
      ``npm:``   → ``_resolve_npm``
      ``pypi:``  → ``_resolve_pypi``
      ``docker:``→ ``_resolve_docker``
      ``https://github.com/`` → ``_resolve_github_url``
      ``http://github.com/``  → ``_resolve_github_url`` (normalised)

    Any unrecognised prefix returns a ``SourceResolution`` with a non-empty
    ``error`` field.
    """
    source = source.strip()
    if not source:
        return SourceResolution(source=source, error="--source value must not be empty")

    lower = source.lower()

    if lower.startswith("npm:"):
        return _resolve_npm(source)

    if lower.startswith("pypi:"):
        return _resolve_pypi(source)

    if lower.startswith("docker:"):
        return _resolve_docker(source)

    if lower.startswith("https://github.com/") or lower.startswith("http://github.com/"):
        return _resolve_github_url(source)

    return SourceResolution(
        source=source,
        error=(
            f"Unrecognised --source format: {source!r}. "
            "Supported formats: npm:<package>, pypi:<package>, docker:<image>, "
            "https://github.com/<owner>/<repo>[/tree/<ref>/...]"
        ),
    )
