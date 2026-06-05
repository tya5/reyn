"""ContainerLauncher — launch + lifecycle for a mount-mode Docker container (#1324).

reyn core is otherwise **attach-only**: :class:`~reyn.environment.container_backend.DockerEnvironmentBackend`
attaches (``docker exec``) to a user-provided ``--container``. This component
adds the owner-decided **(a) launch path** (#1199 / #1324):

    docker run -d <image> -v <workspace-root>:/workspace <security-flags> \
        sleep infinity

returning a container id the caller wires into
``DockerEnvironmentBackend(container=<id>, repo_dir=/workspace)`` — the existing
attach/exec model is reused unchanged.

**Agent-level operator config, NOT phase-level sandbox-policy** (#1326): the
container lifecycle (image / mount / network / security) is a once-per-run
operator decision, supplied via CLI / operator config. It does NOT derive from
a phase's ``default_sandbox_policy`` (the retired #3 remnant); the per-phase
:class:`~reyn.sandbox.policy.SandboxPolicy` continues to govern ``sandboxed_exec``
*inside* the container, a separate layer.

**Security defaults** follow the owner decision (#1324), shared across the
industry (OpenHands / Hermes / OpenClaw): ``--cap-drop ALL`` / non-root user /
network off / read-only root filesystem except the writable mounts (+ a tmpfs
``/tmp`` so the read-only rootfs can still host scratch).

**P7-clean**: no skill / phase / artifact strings — bound only to launch config.
The Docker invocation is built by the pure :func:`build_docker_run_argv` and run
through an injectable runner, so launch / teardown are testable without a live
Docker daemon.

Note: the default image is a configurable reference (:data:`DEFAULT_IMAGE`)
extended at runtime via ``setup_command``. Shipping a purpose-built reyn base
image (Dockerfile + publish) is a follow-up tracked in #1324 — the launch
mechanism here is independent of whether the default is published or bundled.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from reyn.environment.container_backend import SyncRunner, _sync_runner
from reyn.sandbox.backend import SandboxResult

# Configurable default generic image (#1324 owner decision (A): a sensible
# minimal default, extended at runtime via setup_command). Override per run via
# --image / operator config. A purpose-built reyn base image is a #1324 follow-up.
DEFAULT_IMAGE = "python:3.12-slim"

# Fixed in-container mount destination for the workspace (override-able via
# LaunchConfig.workspace_dest). The agent's repo_dir resolves here.
WORKSPACE_DEST_DEFAULT = "/workspace"


@dataclass
class MountSpec:
    """A single bind mount, OpenHands-compatible ``host:container:rw|ro``."""

    host: str
    container: str
    mode: str = "rw"  # "rw" | "ro"

    def to_arg(self) -> str:
        """Return the ``-v`` value ``<host>:<container>:<mode>`` (host resolved)."""
        host_abs = os.path.abspath(os.path.expanduser(self.host))
        return f"{host_abs}:{self.container}:{self.mode}"


def parse_mount_spec(raw: str) -> MountSpec:
    """Parse ``host:container[:rw|ro]`` into a :class:`MountSpec`.

    The mode is optional and defaults to ``rw``. Raises ``ValueError`` on a
    malformed spec (missing host/container, or an unknown mode). Absolute
    Windows-style paths are not supported (POSIX hosts only).
    """
    parts = raw.split(":")
    if len(parts) == 2:
        host, container = parts
        mode = "rw"
    elif len(parts) == 3:
        host, container, mode = parts
    else:
        raise ValueError(
            f"invalid mount spec {raw!r}: expected 'host:container' or "
            f"'host:container:rw|ro'"
        )
    if not host or not container:
        raise ValueError(f"invalid mount spec {raw!r}: host and container required")
    if mode not in ("rw", "ro"):
        raise ValueError(f"invalid mount mode {mode!r} in {raw!r}: expected 'rw' or 'ro'")
    return MountSpec(host=host, container=container, mode=mode)


@dataclass
class LaunchConfig:
    """Operator-level config for launching a mount-mode container.

    workspace_root: host path mounted as the default workspace (= the .reyn
        parent / project root; resolved by the caller, bounded discovery — the
        global cwd-vs-project_root fix is #1316, out of scope here).
    workspace_dest: in-container mount target for workspace_root.
    image / setup_command: the default generic image + a runtime extension hook.
    mounts: additional user bind mounts.
    network: outbound network (default off — the exfiltration gate).
    user: non-root ``uid[:gid]``; defaults to the host operator's ids so mounted
        files keep correct ownership and the process is non-root.
    read_only_rootfs: read-only root filesystem (writable mounts + tmpfs /tmp).
    persistent / name: reuse a named container across runs (Hermes-style).
    """

    workspace_root: str
    workspace_dest: str = WORKSPACE_DEST_DEFAULT
    image: str = DEFAULT_IMAGE
    setup_command: str | None = None
    mounts: list[MountSpec] = field(default_factory=list)
    network: bool = False
    user: str | None = None
    read_only_rootfs: bool = True
    persistent: bool = False
    name: str | None = None


def _default_user() -> str | None:
    """Return ``uid:gid`` for the host operator (non-root mount ownership).

    Returns ``None`` on platforms without ``os.getuid`` (e.g. Windows), letting
    the image's own ``USER`` apply.
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return None
    return f"{getuid()}:{getgid()}"


def build_docker_run_argv(config: LaunchConfig, *, docker_bin: str = "docker") -> list[str]:
    """Build the ``docker run`` argv for a mount-mode container (pure function).

    Detached (``-d``) long-lived (``sleep infinity``) so the caller can then
    ``docker exec`` into it via :class:`DockerEnvironmentBackend`. Security
    defaults (#1324): drop all caps, non-root, network off unless requested,
    read-only rootfs with a tmpfs ``/tmp`` and writable bind mounts.
    """
    argv: list[str] = [docker_bin, "run", "-d"]

    if config.name:
        argv += ["--name", config.name]

    # Security baseline.
    argv += ["--cap-drop", "ALL"]
    user = config.user if config.user is not None else _default_user()
    if user:
        argv += ["--user", user]
    if not config.network:
        argv += ["--network", "none"]
    if config.read_only_rootfs:
        argv += ["--read-only", "--tmpfs", "/tmp"]

    # Default workspace mount (always rw — the agent edits it).
    workspace_host = os.path.abspath(os.path.expanduser(config.workspace_root))
    argv += ["-v", f"{workspace_host}:{config.workspace_dest}:rw"]

    # Additional user mounts.
    for m in config.mounts:
        argv += ["-v", m.to_arg()]

    argv += [config.image, "sleep", "infinity"]
    return argv


class ContainerLauncher:
    """Launch + tear down a mount-mode container (injectable runner for tests)."""

    def __init__(self, *, docker_bin: str = "docker", runner: SyncRunner | None = None) -> None:
        self.docker_bin = docker_bin
        self._runner: SyncRunner = runner or _sync_runner

    def launch(self, config: LaunchConfig, *, timeout: int = 120) -> str:
        """Launch the container and return its id.

        For a persistent config with a ``name`` that already exists, the existing
        container is reused (Hermes-style) instead of launching a new one. Runs
        ``setup_command`` (if any) inside the container after launch. Raises
        ``RuntimeError`` if Docker reports a non-zero exit.
        """
        if config.persistent and config.name:
            existing = self._existing_container(config.name, timeout=timeout)
            if existing:
                return existing

        argv = build_docker_run_argv(config, docker_bin=self.docker_bin)
        res = self._runner(argv, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(
                f"container launch failed (rc={res.returncode}): "
                f"{res.stderr.decode(errors='replace')[:400]}"
            )
        container_id = res.stdout.decode(errors="replace").strip()
        if not container_id:
            raise RuntimeError("container launch returned an empty container id")

        if config.setup_command:
            self._run_setup(container_id, config.setup_command, timeout=timeout)
        return container_id

    def teardown(self, container_id: str, *, timeout: int = 60) -> bool:
        """Remove the container (``docker rm -f``). Returns True on success."""
        res = self._runner(
            [self.docker_bin, "rm", "-f", container_id], timeout=timeout
        )
        return res.returncode == 0

    def _existing_container(self, name: str, *, timeout: int) -> str | None:
        """Return the id of a running container with ``name``, or None."""
        res = self._runner(
            [self.docker_bin, "ps", "-q", "-f", f"name=^{name}$"], timeout=timeout
        )
        if res.returncode != 0:
            return None
        out = res.stdout.decode(errors="replace").strip()
        return out or None

    def _run_setup(self, container_id: str, setup_command: str, *, timeout: int) -> None:
        """Run the runtime setup_command inside the container via a login shell."""
        res = self._runner(
            [self.docker_bin, "exec", container_id, "sh", "-lc", setup_command],
            timeout=timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"container setup_command failed (rc={res.returncode}): "
                f"{res.stderr.decode(errors='replace')[:400]}"
            )
