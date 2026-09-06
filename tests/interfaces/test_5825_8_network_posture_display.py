"""Tier 1: #5825 item 8 (architect design) — the Ctx pane's "network" row
(``chrome.py``'s ``_network_posture_line``, called from ``ctx_pane_lines``)
and its projection onto the AG-UI wire (``agui.state.project_status``).

Mirrors ``test_5851a_ctx_pane_memory_row.py``'s / ``test_5851a_agui_process_
memory_wire.py``'s own three-state discipline exactly — see FP-0069 §8's own
acceptance line, verbatim: "a boundary that is not enforced is not silently
called one." The session-level fact this row renders
(``Session.network_enforcement_gap``) is covered separately in
``tests/runtime/test_5825_8_network_enforcement_gap.py``; this file is
scoped to the RENDERING (pure functions over an already-built ``snap`` dict,
never a live Session)."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines
from reyn.interfaces.transport.agui.state import project_status


def _network_line(snap: dict) -> str:
    (line,) = [ln for ln in ctx_pane_lines(snap) if ln.startswith("network")]
    return line


def test_network_row_says_not_reported_when_the_key_is_absent():
    """Tier 1: a snapshot from before this field existed (or a remote
    server that predates it) carries none of the key — must read as "not
    reported", not "enforced" (a missing observation is not evidence of a
    real boundary, the exact "display and reality diverge" shape this
    whole arc exists to close)."""
    line = _network_line({"ctx_window": 1000, "ctx_used": 100})
    assert "not reported on this connection" in line, line
    assert "enforced" not in line, line


def test_network_row_says_enforced_when_the_gap_is_none():
    """Tier 1: the key IS present (a real snapshot reached this pane) and
    the session reported nothing degraded — real reading, not a fabricated
    default."""
    snap = {"ctx_window": 1000, "ctx_used": 100, "network_posture_gap": None}
    line = _network_line(snap)
    assert line.strip() == "network      enforced", line


def test_network_row_names_the_reason_when_the_gap_is_a_string():
    """Tier 1: the accept-side degraded case — the reason string the
    session reported must reach the rendered line verbatim, not be
    collapsed to a generic "not enforced"."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "network_posture_gap": (
            "this backend provides no isolation — it runs the command with "
            "no policy enforcement, recording the policy for audit only"
        ),
    }
    line = _network_line(snap)
    assert "NOT enforced" in line, line
    assert "no isolation" in line, line


def test_network_posture_gap_is_projected_onto_the_wire():
    """Tier 1: rides the SAME snapshot/delta channel ``halted_reason``/
    ``process_footprint_*`` already do, so a remote Ctx pane renders the
    identical row a local one does."""
    out = project_status({"network_posture_gap": "Noop cannot enforce network"})
    assert out["network_posture_gap"] == "Noop cannot enforce network"


def test_network_posture_gap_defaults_to_none_on_the_wire_when_absent():
    """Tier 1: ``snapshot=None`` (no session attached) -> the field
    projects to ``None``, never a crash — mirrors every other field in
    this same dict literal's own ``snap.get(...)`` discipline."""
    out = project_status(None)
    assert out["network_posture_gap"] is None
