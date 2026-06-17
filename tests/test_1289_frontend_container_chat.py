"""Tier 2: per-frontend container-chat activation (#1289).

#1200 wired ChatSession's two seams (FS Workspace + exec OpContext) to a single
injected backend (verified in test_1200_*). #1289 ACTIVATES that at the CLI
frontends: a shared `reyn.interfaces.cli.env_backend` helper registers the `--env-backend`
args + builds the EnvironmentBackend; `reyn chat` / `reyn dogfood` (like `reyn
run`) build it and pass the SAME instance to BOTH ChatSession seams.

These pin the shared-helper surface + the frontend-activation contract (the same
instance reaches both seams = the #1200 single-shared-sandbox review-gate that any
frontend must uphold). No mocks: a real argparse parser + real ChatSession +
a real backend instance.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reyn.chat.session import ChatSession
from reyn.core.events.state_log import StateLog
from reyn.environment.host_backend import HostBackend
from reyn.interfaces.cli.env_backend import build_environment_backend, register_env_backend_args


def test_register_env_backend_args_surface() -> None:
    """Tier 2: the shared helper registers the full --env-backend arg surface
    (so every frontend exposes the identical flags)."""
    p = argparse.ArgumentParser()
    register_env_backend_args(p)
    ns = p.parse_args([])
    # defaults = host identity (no container)
    assert ns.env_backend == "host"
    assert ns.container is None
    assert ns.repo_dir is None
    # the docker knobs parse
    ns2 = p.parse_args([
        "--env-backend", "docker", "--container", "c1", "--repo-dir", "/repo",
        "--image", "img", "--mount", "a:b", "--keep-container", "--state-dir", "/s",
    ])
    assert ns2.env_backend == "docker" and ns2.container == "c1"
    assert ns2.repo_dir == "/repo" and ns2.keep_container is True


def test_build_environment_backend_host_is_identity() -> None:
    """Tier 2: env_backend=host → (None, None, None, None) = the HostBackend
    identity path (no container, no cleanup) — frontends stay byte-identical
    unless --env-backend=docker is passed."""
    ns = argparse.Namespace(env_backend="host")
    assert build_environment_backend(ns) == (None, None, None, None)


def test_frontend_contract_same_instance_reaches_both_seams(tmp_path: Path) -> None:
    """Tier 2: ★#1289 activation gate — the frontend contract (pass the ONE built
    backend as BOTH environment_backend + sandbox_backend, as chat.py/dogfood.py
    do) reaches the FS seam (Workspace) AND the exec seam (OpContext) as the SAME
    object. This is the #1200 single-shared-sandbox invariant the activation must
    uphold (a frontend wiring different instances = reject)."""
    one = HostBackend()  # stands in for a built DockerEnvironmentBackend
    session = ChatSession(
        agent_name="b",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        environment_backend=one,   # FS seam (what the frontend passes)
        sandbox_backend=one,       # exec seam — SAME instance
    )
    ctx = session._make_router_op_context()
    assert ctx.workspace.backend is one    # FS seam
    assert ctx.sandbox_backend is one      # exec seam — same instance
