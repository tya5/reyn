"""Shared CLI helper: --env-backend arg registration + EnvironmentBackend build.

#1289: per-frontend container-chat activation. `reyn run` (the Agent/OSRuntime
entry) originally owned the `--env-backend` args + builder; chat / dogfood now
reuse the SAME helper so any CLI frontend can launch/attach a container and pass
the resulting backend uniformly. The single-shared-sandbox invariant (#1200) is
the caller's: pass the ONE built backend instance to BOTH `environment_backend`
(FS seam) and `sandbox_backend` (exec seam) so chat / planner / phase share it.

Generic — no skill-specific knowledge (P7). Image / mount / security are
agent-level operator config (#1326), independent of phase sandbox-policy.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register_env_backend_args(p: argparse.ArgumentParser) -> None:
    """Register the shared --env-backend / container args on a CLI subparser.

    Frontends (`run`, `chat`, `dogfood`) call this so the flag surface + help is
    identical everywhere; :func:`build_environment_backend` consumes them.
    """
    p.add_argument("--env-backend", dest="env_backend",
                   choices=["host", "docker"], default="host",
                   help=(
                       "Where the repo filesystem and commands execute: 'host' "
                       "(default, no isolation change) or 'docker'. For docker, "
                       "either attach to a running container (--container/--repo-dir) "
                       "or, if --container is omitted, reyn LAUNCHES a mount-mode "
                       "container (workspace bind-mounted, security-hardened)."
                   ))
    p.add_argument("--container", dest="container", default=None, metavar="NAME",
                   help=(
                       "Running container name/id to ATTACH to (--env-backend=docker). "
                       "Omit to have reyn launch a new mount-mode container instead."
                   ))
    p.add_argument("--repo-dir", dest="repo_dir", default=None, metavar="PATH",
                   help=(
                       "In-container absolute path of the repo working tree "
                       "(e.g. /repo) for --env-backend=docker ATTACH mode. Relative "
                       "paths resolve against this; commands run with it as cwd."
                   ))
    p.add_argument("--image", dest="image", default=None, metavar="IMAGE",
                   help=(
                       "Container image for --env-backend=docker LAUNCH mode "
                       "(when --container is omitted). Defaults to a sensible "
                       "generic base; override per task."
                   ))
    p.add_argument("--mount", dest="mounts", action="append", default=None,
                   metavar="host:container[:rw|ro]",
                   help=(
                       "Additional bind mount for docker LAUNCH mode (repeatable). "
                       "The workspace root is always mounted at /workspace."
                   ))
    p.add_argument("--keep-container", dest="keep_container", action="store_true",
                   help=(
                       "Do not tear down a reyn-launched container after the run "
                       "(persistent reuse). Default: launched containers are removed."
                   ))
    p.add_argument("--state-dir", dest="state_dir", default=None, metavar="PATH",
                   help=(
                       "Host-side directory for OS state/artifacts (events, "
                       "offload, artifact handles), kept off the routed repo FS. "
                       "Recommended with --env-backend=docker so state stays on the host."
                   ))


def build_environment_backend(args: argparse.Namespace, *, launcher=None):
    """Build the EnvironmentBackend + workspace dirs + optional cleanup.

    Returns ``(backend, workspace_base_dir, workspace_state_dir, cleanup)``:

    - host (default): ``(None, None, None, None)`` — identity HostBackend.
    - docker ATTACH (``--container`` given): a ``DockerEnvironmentBackend`` over
      the existing container; base_dir = the in-container ``--repo-dir``; no
      cleanup (the operator owns the container).
    - docker LAUNCH (``--container`` omitted): reyn launches a mount-mode
      container (workspace bind-mounted at ``/workspace``, security-hardened);
      base_dir = ``/workspace``; ``cleanup`` tears it down after the run unless
      ``--keep-container``.

    Generic — no skill-specific knowledge (P7). Image / mount / security are
    agent-level operator config (#1326), independent of phase sandbox-policy.
    """
    backend_kind = getattr(args, "env_backend", "host")
    if backend_kind == "host":
        return None, None, None, None

    if backend_kind == "docker":
        from reyn.environment import DockerEnvironmentBackend
        container = getattr(args, "container", None)
        state_dir = getattr(args, "state_dir", None)
        state_path = Path(state_dir) if state_dir else None

        if container:
            # ATTACH mode: existing operator-owned container (no teardown).
            repo_dir = getattr(args, "repo_dir", None)
            if not repo_dir:
                print(
                    "Error: --env-backend=docker with --container requires --repo-dir.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if state_path is None:
                # baked-repo model: no bind mount, so OS state cannot be made
                # coherent with the in-container repo FS. Warn that state will
                # land on the repo FS (lost on container death) unless host-side.
                print(
                    "Warning: --env-backend=docker --container without --state-dir: "
                    "OS state (events/artifacts) will live on the in-container repo "
                    "FS and is lost on container death. Pass --state-dir for a "
                    "host-side state directory.",
                    file=sys.stderr,
                )
            backend = DockerEnvironmentBackend(container=container, repo_dir=repo_dir)
            return backend, Path(repo_dir), state_path, None

        # LAUNCH mode: reyn starts a mount-mode container (#1324).
        from reyn.config import _find_project_root
        from reyn.environment.container_launcher import (
            DEFAULT_IMAGE,
            WORKSPACE_DEST_DEFAULT,
            ContainerLauncher,
            LaunchConfig,
            load_devcontainer_config,
            parse_mount_spec,
        )
        # _find_project_root returns None when no reyn.yaml is found up the tree;
        # fall back to cwd so the mount source + state_dir are a real host path
        # (NOT the bogus "None/.reyn" that str(None) would produce).
        project_root = _find_project_root(Path.cwd())
        workspace_root = str(project_root if project_root is not None else Path.cwd())
        try:
            cli_mounts = [parse_mount_spec(m) for m in (getattr(args, "mounts", None) or [])]
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        keep = bool(getattr(args, "keep_container", False))
        # #1324 (b) devcontainer awareness: a workspace devcontainer.json seeds
        # the launch defaults; explicit CLI flags override it. build-based
        # devcontainers are not yet supported → warn + use the default image.
        dc = load_devcontainer_config(workspace_root)
        if dc is not None and dc.build_based:
            print(
                "Warning: build-based devcontainer (dockerFile/build) is not yet "
                "supported; using the default image.",
                file=sys.stderr,
            )
        cli_image = getattr(args, "image", None)
        dc_image = dc.image if (dc is not None and not dc.build_based) else None
        config = LaunchConfig(
            workspace_root=workspace_root,
            image=cli_image or dc_image or DEFAULT_IMAGE,
            mounts=cli_mounts or (dc.mounts if dc is not None else []),
            persistent=keep,
            setup_command=(dc.setup_command if dc is not None else None),
            user=(dc.user if dc is not None else None),
        )
        launcher = launcher or ContainerLauncher()
        try:
            container_id = launcher.launch(config)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"launched container : {container_id} (image={config.image})")
        backend = DockerEnvironmentBackend(
            container=container_id, repo_dir=WORKSPACE_DEST_DEFAULT
        )
        cleanup = None if keep else (lambda: launcher.teardown(container_id))
        # part2 (slice-2): in the workspace-mount model the host workspace_root is
        # bind-mounted at /workspace, so the OS state dir defaults to the HOST
        # workspace_root/.reyn — that same dir appears in-container at
        # /workspace/.reyn, keeping OS index/approvals and the agent FS coherent.
        # An explicit --state-dir still wins.
        launch_state_path = state_path or (Path(workspace_root) / ".reyn")
        return backend, Path(WORKSPACE_DEST_DEFAULT), launch_state_path, cleanup

    # argparse `choices` prevents this, but stay defensive.
    print(f"Error: unknown --env-backend '{backend_kind}'.", file=sys.stderr)
    sys.exit(1)
