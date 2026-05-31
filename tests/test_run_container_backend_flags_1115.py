"""Tier 2: FP-0008 #1115 Stage 2 — `reyn run` container-backend flags.

`reyn run --env-backend=docker --container=<id> --repo-dir=/testbed
--state-dir=<host>` builds a single DockerEnvironmentBackend that run.py injects
at BOTH the FS seam (Agent.environment_backend) and the exec seam
(Agent.sandbox_backend) — agent-level uniform, 案C-pure. host (default) keeps the
identity behavior. The flags are generic (no skill-specific knowledge, P7).

These pin the flag→backend construction + validation (the new logic). The
threading of the injected instance through to Workspace / OpContext is already
pinned by test_backend_injection_threading_1115_stage2.

No mocks; real argparse.Namespace + real DockerEnvironmentBackend (no docker
daemon touched — construction only). Docstrings open "Tier 2:".
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.cli.commands.run import _build_environment_backend


def _args(**kw) -> argparse.Namespace:
    base = {
        "env_backend": "host",
        "container": None,
        "repo_dir": None,
        "state_dir": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_host_backend_returns_none_triple() -> None:
    """Tier 2: --env-backend=host yields no backend + default dirs (unchanged behavior)."""
    backend, base_dir, state_dir = _build_environment_backend(_args(env_backend="host"))
    assert backend is None
    assert base_dir is None
    assert state_dir is None


def test_default_is_host() -> None:
    """Tier 2: a Namespace without env_backend defaults to host (getattr fallback)."""
    backend, base_dir, state_dir = _build_environment_backend(argparse.Namespace())
    assert (backend, base_dir, state_dir) == (None, None, None)


def test_docker_builds_single_backend_with_dirs() -> None:
    """Tier 2: --env-backend=docker builds a DockerEnvironmentBackend + maps the dirs."""
    backend, base_dir, state_dir = _build_environment_backend(
        _args(
            env_backend="docker",
            container="reyn_inst_1",
            repo_dir="/testbed",
            state_dir="/host/state",
        )
    )
    assert backend is not None
    assert backend.container == "reyn_inst_1"
    assert backend.repo_dir == "/testbed"
    # base_dir = in-container repo path; state_dir = host-side path.
    assert base_dir == Path("/testbed")
    assert state_dir == Path("/host/state")


def test_docker_backend_satisfies_both_protocols() -> None:
    """Tier 2: the single docker instance satisfies BOTH the FS and exec Protocols.

    This is what makes run.py's dual-seam injection (environment_backend +
    sandbox_backend = the same instance) agent-level uniform (案C-pure).
    """
    from reyn.environment import EnvironmentBackend
    from reyn.sandbox import SandboxBackend

    backend, _b, _s = _build_environment_backend(
        _args(env_backend="docker", container="c", repo_dir="/testbed", state_dir="/h")
    )
    assert isinstance(backend, EnvironmentBackend), "must satisfy the FS seam Protocol"
    assert isinstance(backend, SandboxBackend), "must satisfy the exec seam Protocol"


def test_docker_without_state_dir_keeps_base_dir_and_backend() -> None:
    """Tier 2: --state-dir omitted → state_dir None (host default), backend + base_dir still set."""
    backend, base_dir, state_dir = _build_environment_backend(
        _args(env_backend="docker", container="c", repo_dir="/testbed")
    )
    assert backend is not None
    assert base_dir == Path("/testbed")
    assert state_dir is None


def test_docker_missing_container_exits() -> None:
    """Tier 2: --env-backend=docker without --container is a clean exit."""
    with pytest.raises(SystemExit):
        _build_environment_backend(_args(env_backend="docker", repo_dir="/testbed"))


def test_docker_missing_repo_dir_exits() -> None:
    """Tier 2: --env-backend=docker without --repo-dir is a clean exit."""
    with pytest.raises(SystemExit):
        _build_environment_backend(_args(env_backend="docker", container="c"))
