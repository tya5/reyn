"""Tier 2: #1390 L3 — state_dir reads route host-side, not through the repo backend.

state_dir storage is host-side (``store_artifact`` writes directly host-side,
bypassing the repo backend). The READ side must mirror that split: a read whose
resolved path is under ``state_dir`` (e.g. an OS-offloaded artifact the agent is
told to ``file.read``) stays host-side, while repo reads go to the repo backend.
Under the docker env-backend the repo backend is the *container*, so without this
split a host state_dir path is read in-container (where it does not exist) →
not_found → the offloaded-input read fails (the #183 13236 L3 abort).

This file pins the routing split on the public surface with a real recording
backend (a Fake, not a mock — per testing policy):

  (a) a read whose resolved path is under state_dir is served host-side — the
      injected repo backend records NO read for it, yet the host content comes
      back (the offloaded-artifact case);
  (b) a base_dir (repo) read DOES route to the injected repo backend;
  (c) resolution-robustness: an UNRESOLVED state_dir path (a symlink form, the
      /tmp ↔ /private/tmp case) still routes host-side, because the seam runs on
      the resolved path and state_dir is resolved at construction.

No mocks: real HostBackend / Workspace + a hand-written recording Fake.
"""
from __future__ import annotations

from pathlib import Path

from reyn.environment.host_backend import HostBackend
from reyn.events.events import EventLog
from reyn.security.permissions.permissions import PermissionResolver
from reyn.workspace.workspace import Workspace


class _RecordingRepoBackend:
    """A real EnvironmentBackend standing in for the repo (e.g. container)
    backend: it records which reads it served and delegates IO to a wrapped
    HostBackend. A Fake (test double), not a mock."""

    name = "recording-repo"

    def __init__(self) -> None:
        self._inner = HostBackend()
        self.reads: list[str] = []

    def read_bytes(self, path: Path) -> bytes | None:
        self.reads.append(str(path))
        return self._inner.read_bytes(path)

    def write_bytes(self, path: Path, data: bytes) -> None:
        self._inner.write_bytes(path, data)

    def delete(self, path: Path) -> bool:
        return self._inner.delete(path)

    def mkdir(self, path: Path, *, parents: bool = True) -> bool:
        return self._inner.mkdir(path, parents=parents)

    def move(self, src: Path, dst: Path) -> bool:
        return self._inner.move(src, dst)

    def stat(self, path: Path):
        self.reads.append(str(path))
        return self._inner.stat(path)

    def glob(self, pattern: str, *, root: Path | None = None):
        self.reads.append(pattern)
        return self._inner.glob(pattern, root=root)

    def grep(self, root, regex, **kwargs):
        self.reads.append(str(root))
        return self._inner.grep(root, regex, **kwargs)


def _ws(tmp_path: Path, backend, state_dir: Path, *, grant: Path | None = None) -> Workspace:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir(exist_ok=True)
    # Real PermissionResolver — in production the D12 offload grant (#1383/#1389)
    # permits the out-of-zone state_dir read BEFORE the L3 routing seam runs. The
    # grant mirrors that so the read reaches the routing under test.
    perm = PermissionResolver(config_permissions={}, project_root=repo, interactive=False)
    if grant is not None:
        perm.grant_offload_read(str(grant))
    return Workspace(
        events=EventLog(),
        base_dir=repo,
        state_dir=state_dir,
        permission_resolver=perm,
        skill_name="swe_bench",
        environment_backend=backend,
    )


def test_state_dir_read_served_host_side_not_repo_backend(tmp_path: Path) -> None:
    """Tier 2: a state_dir read bypasses the repo backend (host-side)."""
    backend = _RecordingRepoBackend()
    state_dir = tmp_path / "state"
    # an offloaded artifact the OS wrote host-side under state_dir
    art = state_dir / "artifacts" / "offload.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text('{"big": "input"}')
    ws = _ws(tmp_path, backend, state_dir, grant=art)

    content, found = ws.read_file(str(art))
    assert found and content == '{"big": "input"}'
    # the repo backend must NOT have been asked to read it (it would read the
    # path in its own environment — the container, where it does not exist)
    assert backend.reads == [], backend.reads


def test_repo_read_routes_to_repo_backend(tmp_path: Path) -> None:
    """Tier 2: a base_dir (repo) read DOES route to the repo backend."""
    backend = _RecordingRepoBackend()
    ws = _ws(tmp_path, backend, tmp_path / "state")

    src = ws.base_dir / "mod.py"
    src.write_text("x = 1\n")

    content, found = ws.read_file("mod.py")
    assert found and content == "x = 1\n"
    assert str(src.resolve()) in backend.reads  # repo backend served it


def test_unresolved_state_dir_path_still_routes_host_side(tmp_path: Path) -> None:
    """Tier 2: an unresolved (symlinked) state_dir path routes host-side.

    The /tmp ↔ /private/tmp case (lead-coder's resolution-robustness condition):
    state_dir is resolved at construction and the seam runs on the resolved read
    path, so a state_dir path given in unresolved (symlink) form still lands
    host-side rather than mis-routing to the repo backend.
    """
    real_state = tmp_path / "real_state"
    real_state.mkdir()
    link_state = tmp_path / "link_state"
    link_state.symlink_to(real_state)

    backend = _RecordingRepoBackend()
    art = real_state / "offload.json"
    art.write_text("payload")
    # state_dir passed via the symlink — Workspace .resolve()s it to real_state
    ws = _ws(tmp_path, backend, link_state, grant=art)

    # read via the UNRESOLVED symlink form of the path
    content, found = ws.read_file(str(link_state / "offload.json"))
    assert found and content == "payload"
    assert backend.reads == [], backend.reads  # routed host-side despite symlink form
