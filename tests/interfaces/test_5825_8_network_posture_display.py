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
from reyn.interfaces.repl.read_model import project_remote_snapshot
from reyn.interfaces.transport.agui.state import RemoteStatusView, project_status


def _network_line(snap: dict) -> str:
    (line,) = [ln for ln in ctx_pane_lines(snap) if ln.startswith("network")]
    return line


def test_network_row_says_not_reported_from_a_real_unpopulated_read_model():
    """Tier 1: the "not reported" state must be reachable from a REAL read
    model, not only from a dict a test built (#5892 co-vet 🔴-2, question
    ③: the earlier version of this test constructed its own bare dict, so
    it stayed green while the production projection was fabricating the key
    on every real connection).

    A fresh ``RemoteStatusView`` — no frame applied yet, exactly the window
    between connect and the first STATE_SNAPSHOT — projected through the
    production ``project_remote_snapshot`` must render "not reported", never
    "enforced": a missing observation is not evidence of a real boundary."""
    line = _network_line(project_remote_snapshot(RemoteStatusView().values))
    assert "not reported on this connection" in line, line
    assert "enforced" not in line, line


def test_network_row_says_enforced_once_a_real_snapshot_reports_no_gap():
    """Tier 1: the sibling that makes the test above discriminating — the
    SAME real read model, after a server snapshot that explicitly reports
    no gap, must read "enforced". Without it, "not reported" would also
    pass for a build that never rendered anything at all (a deny-only
    assert is green in an empty world)."""
    view = RemoteStatusView()
    view.apply_snapshot({"agent": "default", "network_posture_gap": None})
    line = _network_line(project_remote_snapshot(view.values))
    assert line.strip() == "network      enforced", line


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


def test_the_wire_omits_the_gap_key_rather_than_synthesizing_none():
    """Tier 1: #5892 co-vet 🔴-2 — the projection must OMIT this key when
    the server did not send it, never synthesize ``None``.

    ``None`` reads as "enforced" downstream, so synthesizing it fabricates
    a boundary claim in the two windows where nothing was reported at all:
    before the first STATE_SNAPSHOT, and against a server that predates the
    field. (The earlier version of this test asserted the synthesized
    ``None`` — it pinned the defect's own shape.)"""
    assert "network_posture_gap" not in project_status(None)
    assert "network_posture_gap" not in project_status({"ctx_window": 1000})
