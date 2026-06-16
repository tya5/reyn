"""Tier 2: #1481 — DockerEnvironmentBackend.get_environment_info() in-container probe.

The probe runs a single ``python3 -c`` INSIDE the container (via the sync
``fs_runner``) and returns platform/os_version/shell/is_git_repo from the
CONTAINER OS — feeding the host adapter's non-host branch. Degrade contract
(#1477 host-value-leak prevention): a sub-field the probe doesn't supply is
OMITTED; a full exec failure (or malformed output) returns ``{}`` so the adapter
omits every host-derived field rather than guessing host values.

No mocks — a real-construct fake ``fs_runner`` (callable returning SandboxResult).
The fs_runner injection is the documented seam for testing docker-exec paths
without a live daemon.
"""
from __future__ import annotations

import json

# isort: off
# Intentional order (NOT alphabetical): pre-load the leaf the host_backend↔
# workspace pair needs, so importing container_backend (which transitively
# touches host_backend) doesn't hit a partial-init circular import when this
# module is collected first. The existing container-backend suite relies on
# collection order for the same reason.
from reyn.workspace.text_codec import decode_text_or_none  # noqa: F401
from reyn.environment.container_backend import DockerEnvironmentBackend, _ENV_INFO
from reyn.security.sandbox.backend import SandboxResult
# isort: on


def _backend(fs_runner) -> DockerEnvironmentBackend:
    return DockerEnvironmentBackend(
        container="c", repo_dir="/testbed", fs_runner=fs_runner,
    )


def test_env_info_happy_path_returns_all_fields() -> None:
    """Tier 2: a successful in-container probe surfaces all four fields verbatim."""
    payload = {
        "platform": "linux", "os_version": "5.15.0-generic",
        "shell": "/bin/bash", "is_git_repo": True,
    }

    def runner(argv, stdin=None):
        return SandboxResult(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    assert _backend(runner).get_environment_info() == payload


def test_env_info_exec_failure_returns_empty_dict() -> None:
    """Tier 2: a docker-exec failure → {} → adapter omits all host-derived fields."""
    def runner(argv, stdin=None):
        return SandboxResult(returncode=1, stdout=b"", stderr=b"docker exec failed")

    assert _backend(runner).get_environment_info() == {}


def test_env_info_partial_probe_omits_missing_keys() -> None:
    """Tier 2: a sub-field the probe omits (e.g. no $SHELL) is absent — never host-filled."""
    payload = {"platform": "linux", "os_version": "5.15.0", "is_git_repo": False}  # no shell

    def runner(argv, stdin=None):
        return SandboxResult(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    info = _backend(runner).get_environment_info()
    assert "shell" not in info
    assert info["platform"] == "linux"
    assert info["is_git_repo"] is False


def test_env_info_malformed_output_degrades_to_empty() -> None:
    """Tier 2: non-JSON probe output → {} (defensive degrade, no crash)."""
    def runner(argv, stdin=None):
        return SandboxResult(returncode=0, stdout=b"not json at all", stderr=b"")

    assert _backend(runner).get_environment_info() == {}


def test_env_info_probe_passes_repo_dir_and_script_as_argv() -> None:
    """Tier 2: the probe passes the script + repo_dir as argv (not interpolated)
    — the in-container .git check targets the CONTAINER repo path, inject-safe."""
    seen: dict = {}

    def runner(argv, stdin=None):
        seen["argv"] = argv
        return SandboxResult(returncode=0, stdout=b"{}", stderr=b"")

    _backend(runner).get_environment_info()
    assert "/testbed" in seen["argv"]          # repo_dir passed as argv
    assert _ENV_INFO in seen["argv"]           # script passed verbatim, not spliced
