"""Tier 2: FP-0008 #1115 Stage 0 — Workspace state_dir decouple + artifact handle.

#1115 Stage 0 makes artifact access OS-managed:
  - ``Workspace`` gains a host-side ``state_dir`` param, decoupled from
    ``base_dir`` (the repo working tree). Default = ``base_dir/.reyn``
    (backward-compat).
  - ``store_artifact`` returns a ``state_dir``-relative handle (no longer a
    ``base_dir``-relative FS path), so the handle survives independently of
    where the repo filesystem lives (= container-death recoverable, and the
    precondition for routing the repo FS through a backend in later stages).
  - ``resolve_artifact_handle`` is the OS's authoritative resolver for those
    handles, with a path-escape guard.

This file pins the producer-side decouple contract. No mocks; public surface
only (``state_dir`` / ``store_artifact`` / ``resolve_artifact_handle``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace


def test_default_state_dir_is_base_dir_reyn(tmp_path: Path) -> None:
    """Tier 2: default state_dir = base_dir/.reyn (backward-compat)."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    assert ws.state_dir == (tmp_path / ".reyn").resolve()


def test_state_dir_can_be_decoupled_from_base_dir(tmp_path: Path) -> None:
    """Tier 2: an explicit state_dir is honored independently of base_dir.

    This is the capability that lets a later stage put the repo working tree
    (base_dir) inside a container while artifacts + events stay on the host.
    """
    base = tmp_path / "repo"
    base.mkdir()
    state = tmp_path / "host_state"
    ws = Workspace(events=EventLog(), base_dir=base, state_dir=state)
    assert ws.state_dir == state.resolve()
    assert not ws.state_dir.is_relative_to(ws.base_dir)
    # state_dir is created eagerly with its artifacts subdir.
    assert (state / "artifacts").is_dir()


def test_store_artifact_returns_state_dir_relative_handle(tmp_path: Path) -> None:
    """Tier 2: store_artifact returns a state_dir-relative handle (not base-coupled).

    The handle must be relative (not absolute) and rooted at ``artifacts/`` —
    independent of base_dir. The OS resolves it against state_dir to read it
    back, round-tripping the stored content.
    """
    base = tmp_path / "repo"
    base.mkdir()
    state = tmp_path / "host_state"
    ws = Workspace(events=EventLog(), base_dir=base, state_dir=state)

    artifact = {"type": "demo", "data": {"x": 1, "y": "two"}}
    handle = ws.store_artifact("phase_a", artifact, skill_name="demo_skill")

    assert not Path(handle).is_absolute(), f"handle must be relative: {handle!r}"
    assert handle.startswith("artifacts/"), (
        f"handle must be state_dir-relative under artifacts/: {handle!r}"
    )
    assert not handle.startswith(".reyn"), (
        "handle must NOT carry the base_dir-coupled .reyn prefix after #1115"
    )

    resolved = ws.resolve_artifact_handle(handle)
    assert resolved.is_file()
    assert resolved.is_relative_to(state.resolve())
    assert json.loads(resolved.read_text(encoding="utf-8")) == artifact


def test_resolve_artifact_handle_rejects_escape(tmp_path: Path) -> None:
    """Tier 2: resolve_artifact_handle raises on a handle escaping state_dir."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    with pytest.raises(PermissionError):
        ws.resolve_artifact_handle("../../../etc/passwd")
