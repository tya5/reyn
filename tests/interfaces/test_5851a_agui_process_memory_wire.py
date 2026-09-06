"""Tier 1: #5851 stage (a) — the 4 process-memory snapshot fields ride the
SAME wire projection ``halted_reason`` already does (``agui.state.
project_status``), so a remote Ctx pane can render the identical "memory"
row a local one does.

Direct call against the real, pure ``project_status`` function — no
transport/codec needed to pin this projection's own dict shape."""
from __future__ import annotations

from reyn.interfaces.transport.agui.state import project_status


def test_process_memory_fields_are_projected_onto_the_wire():
    """Tier 1: a real reading present in the local snapshot reaches the
    wire dict unchanged, under the SAME 4 key names."""
    snap = {
        "process_footprint_bytes": 1_288_490_188,
        "process_footprint_metric": "phys_footprint",
        "process_memory_cap_bytes": 2_147_483_648,
        "process_memory_enforce": True,
    }
    out = project_status(snap)
    assert out["process_footprint_bytes"] == 1_288_490_188
    assert out["process_footprint_metric"] == "phys_footprint"
    assert out["process_memory_cap_bytes"] == 2_147_483_648
    assert out["process_memory_enforce"] is True


def test_process_memory_fields_default_to_none_when_snapshot_is_absent():
    """Tier 1: ``snapshot=None`` (no session attached) -> every field
    projects to ``None``, never a crash — mirrors every OTHER field in
    this same dict literal's own ``snap.get(...)`` discipline."""
    out = project_status(None)
    assert out["process_footprint_bytes"] is None
    assert out["process_footprint_metric"] is None
    assert out["process_memory_cap_bytes"] is None
    assert out["process_memory_enforce"] is None
