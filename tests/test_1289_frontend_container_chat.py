"""Tier 2: per-frontend container-chat activation (#1289).

#1200 wired Session's two seams (FS Workspace + exec OpContext) to a single
injected backend (verified in test_1200_*). #1289 ACTIVATES that at the CLI
frontends: a shared `reyn.interfaces.cli.env_backend` helper registers the `--env-backend`
args + builds the EnvironmentBackend; `reyn chat` / `reyn dogfood` (like `reyn
run`) build it and pass the SAME instance to BOTH Session seams.

These pin the shared-helper surface + the frontend-activation contract (the same
instance reaches both seams = the #1200 single-shared-sandbox review-gate that any
frontend must uphold). No mocks: a real argparse parser + real Session +
a real backend instance.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.environment.host_backend import HostBackend
from reyn.interfaces.cli.env_backend import build_environment_backend, register_env_backend_args
from reyn.runtime.session import Session


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


def test_build_environment_backend_host_is_identity(tmp_path, monkeypatch) -> None:
    """Tier 2: env_backend=host → HostBackend identity for the backend/cleanup slots (None,
    no container, no cleanup). The workspace base_dir slot anchors on the PROJECT ROOT (#2415
    root 3) and workspace_state_dir anchors on project_root/.reyn (#2427) — so FS writes,
    state (events/WAL), AND the permission zone all share the same project root, even for
    subdir invocations (cwd != project_root)."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("model: stub/model\n", encoding="utf-8")
    subdir = project_root / "docs"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    backend, ws_base_dir, ws_state_dir, cleanup = build_environment_backend(
        argparse.Namespace(env_backend="host")
    )
    assert (backend, cleanup) == (None, None), "HostBackend identity (no container, no cleanup)"
    assert ws_base_dir == project_root, "host workspace base_dir anchors on project_root, not the cwd subdir"
    assert ws_state_dir == project_root / ".reyn", "host workspace_state_dir anchors on project_root/.reyn, not cwd"


def test_frontend_contract_same_instance_reaches_both_seams(tmp_path: Path) -> None:
    """Tier 2: ★#1289 activation gate — the frontend contract (pass the ONE built
    backend as BOTH environment_backend + sandbox_backend, as chat.py/dogfood.py
    do) reaches the FS seam (Workspace) AND the exec seam (OpContext) as the SAME
    object. This is the #1200 single-shared-sandbox invariant the activation must
    uphold (a frontend wiring different instances = reject)."""
    one = HostBackend()  # stands in for a built DockerEnvironmentBackend
    session = Session(
        agent_name="b",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        environment_backend=one,   # FS seam (what the frontend passes)
        sandbox_backend=one,       # exec seam — SAME instance
    )
    ctx = session._make_router_op_context()
    assert ctx.workspace.backend is one    # FS seam
    assert ctx.sandbox_backend is one      # exec seam — same instance
