"""Tier 1: #5851 stage (a) — the Ctx pane's "memory" row (`chrome.py`'s
``_memory_line``, called from ``ctx_pane_lines``).

Mirrors ``test_5588_compaction_progress_render.py``'s own ``_folded`` row
tests exactly (same three-state discipline: absent-key / measurable-false /
real-value must render as three visually DISTINCT phrases, never collapse a
"could not measure" into a "did not report" or vice-versa)."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines


def _memory(snap: dict) -> str:
    (line,) = [ln for ln in ctx_pane_lines(snap) if ln.startswith("memory")]
    return line


def test_memory_row_says_not_reported_when_the_keys_are_absent():
    """Tier 1: a snapshot from before this field existed (or a remote
    server that predates it) carries none of the 4 keys — must read as
    "not reported", not "not measurable" (those are different facts: one
    is "this connection never told me", the other is "this platform
    cannot measure it")."""
    line = _memory({"ctx_window": 1000, "ctx_used": 100})
    assert "not reported on this connection" in line, line
    assert "not measurable" not in line, line


def test_memory_row_says_not_measurable_when_metric_is_none():
    """Tier 1: keys ARE present (a real snapshot reached this pane) but
    ``process_footprint_metric`` is ``None`` — this platform has no
    reader. Never renders a fabricated number."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "process_footprint_bytes": None, "process_footprint_metric": None,
        "process_memory_cap_bytes": None, "process_memory_enforce": False,
    }
    line = _memory(snap)
    assert "not measurable on this platform" in line, line
    assert "not reported" not in line, line


def test_memory_row_says_not_measurable_on_a_single_failed_read():
    """Tier 1: ``metric`` present (the platform IS supported) but THIS
    read's ``bytes`` came back ``None`` (a transient failure) — same
    wording as the platform-unsupported case; a single failed read is
    not a fourth phrase's worth of distinction."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "process_footprint_bytes": None, "process_footprint_metric": "phys_footprint",
        "process_memory_cap_bytes": None, "process_memory_enforce": False,
    }
    line = _memory(snap)
    assert "not measurable on this platform" in line, line


def test_memory_row_renders_real_value_with_no_cap():
    """Tier 1: accept side — no cap set -> "observe only" phrasing, GiB
    formatted, no fabricated cap figure."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "process_footprint_bytes": 1_288_490_188, "process_footprint_metric": "phys_footprint",
        "process_memory_cap_bytes": None, "process_memory_enforce": False,
    }
    line = _memory(snap)
    assert "1.2 GiB phys_footprint" in line, line
    assert "no cap" in line, line
    assert "observe only" in line, line


def test_memory_row_renders_cap_and_enforced_posture():
    """Tier 1: a real cap + enforce=True -> "enforced" posture, both
    figures shown (the measured value AND the cap it is compared
    against — an operator must never have to cross-reference config to
    read this one line)."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "process_footprint_bytes": 1_288_490_188, "process_footprint_metric": "phys_footprint",
        "process_memory_cap_bytes": 2_147_483_648, "process_memory_enforce": True,
    }
    line = _memory(snap)
    assert "1.2 GiB phys_footprint" in line, line
    assert "cap 2.0 GiB" in line, line
    assert "(enforced)" in line, line


def test_memory_row_renders_cap_with_observe_only_posture():
    """Tier 1: a real cap but enforce=False -> "observe only", not
    "enforced" — the cap is visible even when not (yet) acted on."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "process_footprint_bytes": 1_288_490_188, "process_footprint_metric": "phys_footprint",
        "process_memory_cap_bytes": 2_147_483_648, "process_memory_enforce": False,
    }
    line = _memory(snap)
    assert "(observe only)" in line, line
    assert "(enforced)" not in line, line
