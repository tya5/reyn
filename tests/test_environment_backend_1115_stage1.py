"""Tier 2: FP-0008 #1115 Stage 1 — EnvironmentBackend seam.

Stage 1 introduces an EnvironmentBackend so the repo working tree can later
route through a pluggable backend (host=identity now / container at Stage 2)
while the OS + permission layer stay host-side. This file pins:

  (a) HostBackend satisfies the EnvironmentBackend Protocol (runtime_checkable);
  (b) Workspace ROUTES every repo-FS op through its backend (not the raw
      filesystem directly) — proven with a real recording backend (a Fake, not
      a mock), so a non-host backend at Stage 2 will actually be exercised;
  (c) with the default HostBackend the FS ops round-trip identically (= the
      identity / behavior-preservation property, beyond the broad existing
      file-op suites);
  (d) grep is a backend primitive (Workspace.grep delegates the scan);
  (e) the permission gate + state_dir artifact storage stay host-side (NOT
      routed through the repo-FS backend).

No mocks: collaborators are real instances or a hand-written recording Fake
(per testing policy). Assertions use public surfaces only.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.environment.backend import EnvironmentBackend, GrepResult
from reyn.environment.host_backend import HostBackend


class _RecordingBackend:
    """A real EnvironmentBackend that records which ops it served, delegating
    the actual IO to a wrapped HostBackend. A Fake (test double), not a mock.
    """

    name = "recording"

    def __init__(self) -> None:
        self._inner = HostBackend()
        self.calls: list[str] = []

    def read_bytes(self, path: Path) -> bytes | None:
        self.calls.append("read_bytes")
        return self._inner.read_bytes(path)

    def write_bytes(self, path: Path, data: bytes) -> None:
        self.calls.append("write_bytes")
        self._inner.write_bytes(path, data)

    def delete(self, path: Path) -> bool:
        self.calls.append("delete")
        return self._inner.delete(path)

    def mkdir(self, path: Path, *, parents: bool = True) -> bool:
        self.calls.append("mkdir")
        return self._inner.mkdir(path, parents=parents)

    def move(self, src: Path, dst: Path) -> bool:
        self.calls.append("move")
        return self._inner.move(src, dst)

    def stat(self, path: Path) -> dict | None:
        self.calls.append("stat")
        return self._inner.stat(path)

    def glob(self, pattern: str, *, root: Path | None = None) -> list[Path]:
        self.calls.append("glob")
        return self._inner.glob(pattern, root=root)

    def grep(self, root, regex, **kw) -> GrepResult:
        self.calls.append("grep")
        return self._inner.grep(root, regex, **kw)


def test_host_backend_satisfies_protocol() -> None:
    """Tier 2: (a) HostBackend is a structural EnvironmentBackend."""
    assert isinstance(HostBackend(), EnvironmentBackend)
    assert HostBackend().name == "host"


def test_workspace_routes_every_fs_op_through_backend(tmp_path: Path) -> None:
    """Tier 2: (b) Workspace delegates each repo-FS op to its backend.

    A recording backend proves the seam is real — if any op bypassed the
    backend (touched the FS directly), its name would be absent from `calls`.
    """
    backend = _RecordingBackend()
    ws = Workspace(events=EventLog(), base_dir=tmp_path, environment_backend=backend)

    ws.write_file("a.txt", "hello")
    content, found = ws.read_file("a.txt")
    assert (content, found) == ("hello", True)
    ws.make_directory("sub")
    ws.move_path("a.txt", "sub/b.txt")
    ws.stat_path("sub/b.txt")
    ws.glob_files("**/*.txt")
    ws.delete_file("sub/b.txt")
    ws.grep("sub", re.compile("x"))

    for op in ("write_bytes", "read_bytes", "mkdir", "move", "stat", "glob", "delete", "grep"):
        assert op in backend.calls, f"Workspace did not route {op!r} through the backend"


def test_default_backend_is_host_identity(tmp_path: Path) -> None:
    """Tier 2: (c) with the default backend, FS ops round-trip identically.

    The write→read→stat→glob→delete round-trip below runs against the REAL
    local filesystem under tmp_path — pinning the host-identity property
    behaviorally (no private-state inspection of the backend).
    """
    ws = Workspace(events=EventLog(), base_dir=tmp_path)

    ws.write_file("dir/x.txt", "data-本文")
    # Host identity: the default backend wrote through to the REAL local FS.
    assert (tmp_path / "dir" / "x.txt").read_text(encoding="utf-8") == "data-本文"

    content, found = ws.read_file("dir/x.txt")
    assert (content, found) == ("data-本文", True)

    info = ws.stat_path("dir/x.txt")
    assert info is not None and info["is_file"] is True and info["size"] > 0

    assert ws.glob_files("dir/*.txt") == ["dir/x.txt"]
    assert ws.delete_file("dir/x.txt") is True
    assert ws.read_file("dir/x.txt") == ("", False)


def test_grep_primitive_via_backend(tmp_path: Path) -> None:
    """Tier 2: (d) Workspace.grep delegates the scan to the backend and returns
    a GrepResult with absolute Paths."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    ws.write_file("code.py", "alpha\nNEEDLE here\nbeta\n")
    ws.write_file("other.py", "no match\n")

    res = ws.grep(".", re.compile("NEEDLE"), output_mode="files_with_matches")
    assert isinstance(res, GrepResult)
    assert [p.name for p in res.files] == ["code.py"]
    assert all(p.is_absolute() for p in res.files)

    content_res = ws.grep(".", re.compile("NEEDLE"))
    assert content_res.output_mode == "content"
    # Unpack-enforcement: exactly the one matching line, with correct content
    # (= the grep-returns-the-right-match invariant, not a count format-pin).
    [hit] = content_res.matches
    assert hit["line_number"] == 2
    assert "NEEDLE" in hit["content"]

    count_res = ws.grep(".", re.compile("a"), output_mode="count")
    assert count_res.count >= 1


def test_state_dir_artifacts_not_routed_through_repo_backend(tmp_path: Path) -> None:
    """Tier 2: (e) artifact storage is host-side (state_dir) — NOT routed through
    the repo-FS backend (Stage 0 decouple intent: artifacts survive on host even
    when the repo FS lives in a container)."""
    backend = _RecordingBackend()
    ws = Workspace(events=EventLog(), base_dir=tmp_path, environment_backend=backend)

    handle = ws.store_artifact("p", {"type": "demo", "data": {"v": 1}}, skill_name="s")
    # store_artifact wrote the file without going through the repo-FS backend.
    assert "write_bytes" not in backend.calls
    assert ws.resolve_artifact_handle(handle).is_file()
