"""Tier 2: #1414 — the default file read/write ZONE anchors on file_zone_root
(container repo root under a container backend), distinct from the host-side
approvals/config base (project_root).

Permission-layer parallel of the #1410 base_dir(container)-vs-state_dir(host)
split: a non-grant container run can write into the container repo's own
`.reyn`/`reyn` default zone while approvals.yaml stays host-side. Host /
interactive behaviour is byte-identical (file_zone_root defaults to project_root).
"""
from __future__ import annotations

from pathlib import Path

from reyn.permissions import PermissionDecl
from reyn.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
    EffectivePermission,
)
from reyn.permissions.permissions import PermissionResolver

_HOST = Path("/host/proj")
_CONTAINER = Path("/testbed")


# ─── PermissionResolver: zone anchors on file_zone_root, approvals on project_root ──


def test_container_file_zone_allows_container_reyn_write() -> None:
    """Tier 2: #1414 — file_zone_root=/testbed lets a non-grant run write into
    /testbed/.reyn (the container default zone). (The approvals/config base stays
    host-side by construction — anchored on project_root, untouched by the fix.)"""
    r = PermissionResolver({}, project_root=_HOST, file_zone_root=_CONTAINER)
    assert r.is_write_allowed("/testbed/.reyn/scratch.txt") is True
    assert r.is_read_allowed("/testbed/src/mod.py") is True  # read zone = whole repo


def test_host_default_zone_unchanged() -> None:
    """Tier 2: #1414 — no file_zone_root → the zone behaves exactly as before
    (anchored on project_root): host/.reyn writeable, a /testbed path is NOT in
    the host zone (public-behaviour parity, no private-state assert)."""
    r = PermissionResolver({}, project_root=_HOST)  # no file_zone_root
    assert r.is_write_allowed("/host/proj/.reyn/scratch.txt") is True
    assert r.is_write_allowed("/testbed/.reyn/scratch.txt") is False  # outside host zone


def test_container_does_not_widen_host_zone() -> None:
    """Tier 2: #1414 — with a container file_zone_root, a HOST-side .reyn write is
    NOT in the (container) zone (the zone moved, it didn't union)."""
    r = PermissionResolver({}, project_root=_HOST, file_zone_root=_CONTAINER)
    assert r.is_write_allowed("/host/proj/.reyn/scratch.txt") is False


# ─── AgentLayer / EffectivePermission.of consume file_zone_root ───────────────


def test_agent_layer_file_zone_root() -> None:
    """Tier 2: #1414 — AgentLayer's zone uses file_zone_root (renamed from
    project_root; it was only ever the zone base)."""
    layer = AgentLayer(PermissionDecl(), file_zone_root=_CONTAINER)
    assert layer.allows(CapabilityAxis.FILE_WRITE, "/testbed/.reyn/x") is True
    assert layer.allows(CapabilityAxis.FILE_READ, "/testbed/src/x.py") is True
    assert layer.allows(CapabilityAxis.FILE_WRITE, "/host/proj/.reyn/x") is False


def test_effective_of_file_zone_root() -> None:
    """Tier 2: #1414 — EffectivePermission.of threads file_zone_root to AgentLayer."""
    eff = EffectivePermission.of(decl=PermissionDecl(), file_zone_root=_CONTAINER)
    assert eff.allows(CapabilityAxis.FILE_WRITE, "/testbed/.reyn/x") is True
