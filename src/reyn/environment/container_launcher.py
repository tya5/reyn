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

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reyn.environment.container_backend import SyncRunner, _sync_runner
from reyn.sandbox.backend import SandboxResult

# The bundled reyn base image (#1324): a minimal generic agent runtime built ON
# DEMAND from reyn_base.Dockerfile (local-build, no registry). The tag is local
# (not registry-qualified); a registry-published variant is a #1324 follow-up.
REYN_BASE_IMAGE = "reyn-base:local"

# Default image for a launched container = the bundled reyn base, extended at
# runtime via setup_command (#1324 owner decision (A)). Override per run via
# --image / operator config.
DEFAULT_IMAGE = REYN_BASE_IMAGE


def _reyn_base_dockerfile() -> Path:
    """Path to the bundled reyn base Dockerfile (shipped as package data)."""
    return Path(__file__).parent / "reyn_base.Dockerfile"


def _devcontainer_image_tag(spec: "BuildSpec") -> str:
    """#1341: a content-addressed local tag for a build-based devcontainer image.

    The hash covers the Dockerfile *contents* + sorted build args + target — the
    inputs that change the built image (F2: content-addressed + inspect-then-build).
    A changed Dockerfile/args/target yields a new tag → an inspect-miss → rebuild;
    an unchanged spec reuses the existing tag (no rebuild). NOTE: a context-only
    change (e.g. a COPY'd file edited without touching the Dockerfile) keeps the
    same tag — a documented minor staleness limit, matching the VS Code model
    (which also keys rebuilds on the Dockerfile/args, not arbitrary context edits).
    Falls back to hashing the Dockerfile *path* if the file is unreadable.
    """
    h = hashlib.sha256()
    try:
        h.update(Path(spec.dockerfile).read_bytes())
    except OSError:
        h.update(spec.dockerfile.encode("utf-8"))
    for k in sorted(spec.build_args):
        h.update(f"\0{k}={spec.build_args[k]}".encode())
    if spec.target:
        h.update(f"\0target={spec.target}".encode())
    return f"reyn-dc-{h.hexdigest()[:12]}:local"

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
class BuildSpec:
    """A build-on-demand spec for a build-based devcontainer (#1341).

    dockerfile:  absolute path to the devcontainer Dockerfile.
    context:     absolute path to the build context dir.
    build_args:  ``build.args`` → ``--build-arg k=v`` (str values only).
    target:      optional ``build.target`` multi-stage target.

    When a ``LaunchConfig`` carries a ``build``, the launcher builds the image
    on demand (content-addressed tag, inspect-then-build) instead of pulling.
    """

    dockerfile: str
    context: str
    build_args: dict[str, str] = field(default_factory=dict)
    target: str | None = None


@dataclass
class LaunchConfig:
    """Operator-level config for launching a mount-mode container.

    workspace_root: host path mounted as the default workspace (= the .reyn
        parent / project root; resolved by the caller, bounded discovery — the
        global cwd-vs-project_root fix is #1316, out of scope here).
    workspace_dest: in-container mount target for workspace_root.
    image / setup_command: the default generic image + a runtime extension hook.
    build: a #1341 build-on-demand spec — when set, the launcher builds
        ``image`` from this Dockerfile (build-based devcontainer) rather than
        relying on ``docker run`` to pull it. ``image`` then holds the
        content-addressed build tag (see ``_devcontainer_image_tag``).
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
    build: BuildSpec | None = None
    mounts: list[MountSpec] = field(default_factory=list)
    network: bool = False
    user: str | None = None
    read_only_rootfs: bool = True
    persistent: bool = False
    name: str | None = None


# ── devcontainer.json awareness (#1324 follow-up b) ──────────────────────────
#
# When a workspace ships a devcontainer.json, reyn reads a minimal-useful subset
# to seed the LaunchConfig (industry-standard spec; coding-task oriented). An
# explicit CLI flag (--image / --mount) overrides the devcontainer default.
# build/dockerFile-based devcontainers are NOT yet supported — the caller warns
# and uses the default image (build-based support via the #1329 ensure_image
# build-on-demand is a tracked follow-up).


@dataclass
class DevcontainerConfig:
    """A devcontainer.json mapped to the LaunchConfig-relevant subset.

    image:          devcontainer ``image`` (None if absent / build-based).
    setup_command:  devcontainer ``postCreateCommand`` (once-after-create —
                    semantically matches reyn's once-on-create setup_command;
                    postStartCommand is every-start and intentionally NOT mapped).
    mounts:         devcontainer ``mounts`` (bind mounts only).
    user:           devcontainer ``remoteUser`` / ``containerUser``.
    build_based:    True if the devcontainer is dockerFile/build/compose-based.
    dockerfile:     #1341 — resolved absolute path to the devcontainer Dockerfile
                    (``dockerFile`` legacy or ``build.dockerfile``), or None for a
                    non-buildable (``dockerComposeFile``) devcontainer.
    build_context:  #1341 — resolved absolute build context dir.
    build_args:     #1341 — ``build.args`` (str values).
    build_target:   #1341 — ``build.target`` multi-stage target.

    A dockerFile/build devcontainer is **buildable** (``buildable`` True →
    build-on-demand, #1341); ``dockerComposeFile`` is build_based but NOT
    buildable (multi-service is out of scope → caller warns + falls back to the
    default image).
    """

    image: str | None = None
    setup_command: str | None = None
    mounts: list[MountSpec] = field(default_factory=list)
    user: str | None = None
    build_based: bool = False
    dockerfile: str | None = None
    build_context: str | None = None
    build_args: dict[str, str] = field(default_factory=dict)
    build_target: str | None = None

    @property
    def buildable(self) -> bool:
        """True if this devcontainer can be built on demand (#1341): build_based
        AND a Dockerfile was resolved (i.e. dockerFile/build, not compose-only)."""
        return self.build_based and self.dockerfile is not None


def _strip_jsonc(text: str) -> str:
    """Best-effort strip of JSONC comments + trailing commas (devcontainer.json
    permits ``//`` / ``/* */`` comments and trailing commas)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)  # block comments
    text = re.sub(r"(?m)//.*$", "", text)  # line comments
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # trailing commas
    return text


def _parse_devcontainer_mount(m: Any) -> MountSpec | None:
    """Parse a devcontainer ``mounts`` entry → MountSpec.

    Accepts the string form ``source=/h,target=/c[,type=bind][,readonly]`` and
    the object form ``{"source": "/h", "target": "/c", ...}``. Non-bind mounts
    (e.g. ``type=volume`` without a host source) are skipped (return None).
    """
    src = tgt = None
    mode = "rw"
    if isinstance(m, str):
        for part in m.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                if k == "source":
                    src = v.strip()
                elif k == "target":
                    tgt = v.strip()
            elif part.strip() in ("readonly", "ro"):
                mode = "ro"
    elif isinstance(m, Mapping):
        src = m.get("source")
        tgt = m.get("target")
        if m.get("readonly") or m.get("readOnly"):
            mode = "ro"
    if not src or not tgt:
        return None
    return MountSpec(host=str(src), container=str(tgt), mode=mode)


def _map_devcontainer_build(
    data: Mapping[str, Any], devcontainer_dir: Path, cfg: DevcontainerConfig
) -> None:
    """#1341: map a build-based devcontainer's build spec onto ``cfg``.

    Sets ``build_based`` for any of dockerFile / build / dockerComposeFile.
    For the buildable forms (legacy top-level ``dockerFile`` [+ ``context``] or
    the ``build`` object ``{dockerfile, context, args, target}``) resolves the
    Dockerfile + context relative to the devcontainer.json location (per the
    devcontainer spec) and captures args/target. ``dockerComposeFile`` is
    build_based but left non-buildable (dockerfile stays None) — multi-service
    is out of scope for the single-container launcher.
    """
    build = data.get("build")
    dockerfile_rel = context_rel = target = None
    args: dict[str, str] = {}
    if isinstance(build, Mapping):
        df = build.get("dockerfile")
        if isinstance(df, str):
            dockerfile_rel = df
        ctx = build.get("context")
        if isinstance(ctx, str):
            context_rel = ctx
        tgt = build.get("target")
        if isinstance(tgt, str):
            target = tgt
        ba = build.get("args")
        if isinstance(ba, Mapping):
            args = {str(k): str(v) for k, v in ba.items()}
    elif isinstance(data.get("dockerFile"), str):  # legacy top-level form
        dockerfile_rel = data["dockerFile"]
        ctx = data.get("context")
        if isinstance(ctx, str):
            context_rel = ctx

    if data.get("dockerFile") or build or data.get("dockerComposeFile"):
        cfg.build_based = True
    if dockerfile_rel:  # buildable (dockerFile/build) — resolve relative to the json dir
        cfg.dockerfile = str((devcontainer_dir / dockerfile_rel).resolve())
        cfg.build_context = str((devcontainer_dir / (context_rel or ".")).resolve())
        cfg.build_args = args
        cfg.build_target = target
    # dockerComposeFile (or build without a dockerfile) → build_based, non-buildable.


def _map_devcontainer(
    data: Mapping[str, Any], devcontainer_dir: Path
) -> DevcontainerConfig:
    cfg = DevcontainerConfig()
    _map_devcontainer_build(data, devcontainer_dir, cfg)
    img = data.get("image")
    if isinstance(img, str):
        cfg.image = img
    pcc = data.get("postCreateCommand")
    if isinstance(pcc, str):
        cfg.setup_command = pcc
    elif isinstance(pcc, list):  # devcontainer allows an argv array
        cfg.setup_command = " ".join(str(x) for x in pcc)
    for m in data.get("mounts") or []:
        ms = _parse_devcontainer_mount(m)
        if ms is not None:
            cfg.mounts.append(ms)
    user = data.get("remoteUser") or data.get("containerUser")
    if isinstance(user, str):
        cfg.user = user
    return cfg


def load_devcontainer_config(workspace_root: str) -> DevcontainerConfig | None:
    """Return the mapped devcontainer config for ``workspace_root``, or None.

    Checks the two standard locations (``.devcontainer/devcontainer.json`` then
    ``.devcontainer.json``). Parse failures return None (a malformed
    devcontainer.json must not crash a launch — the caller falls back to defaults).
    """
    root = Path(workspace_root)
    for rel in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        p = root / rel
        if p.exists():
            try:
                data = json.loads(_strip_jsonc(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, ValueError):
                return None
            if isinstance(data, Mapping):
                # #1341: pass the devcontainer.json's directory so build-based
                # dockerfile/context paths resolve relative to it (spec).
                return _map_devcontainer(data, p.parent)
            return None
    return None


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

        # #1341: a build-based devcontainer builds its image on demand (the tag
        # is content-addressed); everything else uses ensure_image (reyn-base
        # on-demand / docker-run pull). A generous build timeout (apt layers),
        # independent of the shorter launch/run timeout.
        if config.build is not None:
            self._build_devcontainer_image(config)
        else:
            self.ensure_image(config.image)
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

    def ensure_image(self, image: str, *, timeout: int = 600) -> None:
        """Build the bundled reyn base image on demand if it is missing.

        Only the bundled local tag (:data:`REYN_BASE_IMAGE`) is built here — any
        other ``image`` is left to ``docker run`` to pull. A present image is a
        no-op (``docker image inspect`` succeeds). Raises ``RuntimeError`` if the
        build fails. The build timeout is generous (apt layers).
        """
        if image != REYN_BASE_IMAGE:
            return
        if self._image_present(image, timeout=timeout):
            return  # already built
        df = _reyn_base_dockerfile()
        self._docker_build(
            image, str(df), str(df.parent),
            timeout=timeout, err_label="reyn base image build",
        )

    def _build_devcontainer_image(self, config: LaunchConfig, *, timeout: int = 600) -> None:
        """#1341: build a build-based devcontainer image on demand (inspect-then-build).

        No-op if the content-addressed tag (``config.image``) is already present.
        SECURITY (documented): a `docker build` runs the **workspace-authored**
        Dockerfile's RUN steps on the host docker daemon — NOT inside reyn's
        runtime sandbox (the network-off / non-root / read-only-rootfs flags
        apply to ``docker run``, not ``docker build``; builds typically need
        network). The trust model is identical to VS Code "Reopen in Container"
        building the workspace Dockerfile: the operator already opted in via
        ``--env-backend=docker`` on a workspace they chose. The *runtime*
        container still gets all reyn security flags. We log the build for
        transparency (no extra confirmation prompt — the opt-in is the gate).
        """
        spec = config.build
        if spec is None:
            return
        if self._image_present(config.image, timeout=timeout):
            return  # content-addressed tag already built → reuse (F2)
        print(
            f"reyn: building devcontainer image {config.image} from "
            f"{spec.dockerfile} (workspace Dockerfile runs on the host docker "
            f"daemon at build time)",
            file=sys.stderr,
        )
        self._docker_build(
            config.image, spec.dockerfile, spec.context,
            build_args=spec.build_args, target=spec.target,
            timeout=timeout, err_label="devcontainer image build",
        )

    def _image_present(self, image: str, *, timeout: int) -> bool:
        """True if ``docker image inspect`` succeeds (image locally present)."""
        res = self._runner(
            [self.docker_bin, "image", "inspect", image], timeout=timeout
        )
        return res.returncode == 0

    def _docker_build(
        self, tag: str, dockerfile: str, context: str, *,
        build_args: dict[str, str] | None = None, target: str | None = None,
        timeout: int = 600, err_label: str = "image build",
    ) -> None:
        """Shared ``docker build`` primitive (reyn-base + #1341 devcontainer).

        ``docker build -t <tag> -f <dockerfile> [--build-arg k=v ...]
        [--target t] <context>``. Raises ``RuntimeError`` on non-zero exit.
        """
        argv = [self.docker_bin, "build", "-t", tag, "-f", str(dockerfile)]
        for k, v in (build_args or {}).items():
            argv += ["--build-arg", f"{k}={v}"]
        if target:
            argv += ["--target", target]
        argv.append(str(context))
        res = self._runner(argv, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(
                f"{err_label} failed (rc={res.returncode}): "
                f"{res.stderr.decode(errors='replace')[:400]}"
            )

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
