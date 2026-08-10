"""Tier 2: FP-0008 #1115 Stage 0 — Workspace state_dir decouple.

#1115 Stage 0 gives ``Workspace`` a host-side ``state_dir`` param, decoupled
from ``base_dir`` (the repo working tree). Default = ``base_dir/.reyn``
(backward-compat); an explicit ``state_dir`` lets a later stage put the repo
working tree inside a container while OS-managed state stays on the host.

This file pins the state_dir-decouple contract itself. No mocks; public
surface only (``state_dir``).

(The artifact-handle half of Stage 0 — ``store_artifact`` /
``resolve_artifact_handle`` — was removed along with the ``judge_output`` op,
its only production reader; see the op's removal PR. The state_dir decouple
capability pinned here is independent of that and survives.)
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace


def test_default_state_dir_is_base_dir_reyn(tmp_path: Path) -> None:
    """Tier 2: default state_dir = base_dir/.reyn (backward-compat)."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    assert ws.state_dir == (tmp_path / ".reyn").resolve()


def test_state_dir_can_be_decoupled_from_base_dir(tmp_path: Path) -> None:
    """Tier 2: an explicit state_dir is honored independently of base_dir.

    This is the capability that lets a later stage put the repo working tree
    (base_dir) inside a container while OS-managed state stays on the host.
    """
    base = tmp_path / "repo"
    base.mkdir()
    state = tmp_path / "host_state"
    ws = Workspace(events=EventLog(), base_dir=base, state_dir=state)
    assert ws.state_dir == state.resolve()
    assert not ws.state_dir.is_relative_to(ws.base_dir)
    # state_dir itself is created eagerly.
    assert state.is_dir()
