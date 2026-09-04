"""Tier 1: #4401 ② — the mcp pane's 3-state probe display
(``_apply_mcp_probe_note``/``_mcp_pane_entries``), plus ③'s retry row.

The defect this closes: the mcp tab conflated "not yet probed" with "0
tools" — the owner's reported symptom was 8 configured servers reading as
unusable when the real cause was the model never having been TOLD their
tool names (no probe result yet), not the servers being broken. The 3
states (``"answered"`` / ``"failed"`` / ``"not_probed"``) — plus a
transient 4th, ③'s own ``"retrying"`` — must never render identically.

Pure-function tests against ``_mcp_pane_entries``/``_apply_mcp_probe_note``
directly — no Session, no RouterHostAdapter, matching this repo's existing
``DrawerRow`` pane tests (``test_4686_mcp_pane_subscriptions.py``,
``test_drawer_row_3691.py``)."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import (
    DrawerRow,
    _apply_mcp_probe_note,
    _mcp_pane_entries,
)


def _snap(probe_states: "list[dict] | None", *, reported: bool = True) -> dict:
    items = [
        {"kind": "mcp", "name": "broker", "on": True, "denied": False, "denied_reason": None},
        {"kind": "mcp", "name": "filesystem", "on": True, "denied": False, "denied_reason": None},
    ]
    return {
        "visibility_items": items,
        "mcp_probe_states": probe_states or [],
        "mcp_probe_states_reported": reported,
    }


def test_answered_zero_tools_is_never_rendered_the_same_as_not_probed():
    """Tier 1: THE core #4401 conflation this issue exists to close — a
    server that answered with genuinely zero tools reads differently from
    one nothing has probed at all."""
    answered_zero = _apply_mcp_probe_note(
        DrawerRow(label="broker", state="on"), {"state": "answered", "tool_count": 0},
    )
    not_probed = _apply_mcp_probe_note(
        DrawerRow(label="broker", state="on"), {"state": "not_probed"},
    )
    assert answered_zero.note == "0 tools"
    assert not_probed.note == "not yet probed"
    assert answered_zero.note != not_probed.note


def test_answered_note_pluralizes_by_count():
    """Tier 1: the tool-count note reads "1 tool" (singular) vs "N tools"
    (plural) — a wording detail, but one that answers a real question ("is
    that ONE tool or an empty list rendered oddly")."""
    row = _apply_mcp_probe_note(DrawerRow(label="x"), {"state": "answered", "tool_count": 1})
    assert row.note == "1 tool"
    row = _apply_mcp_probe_note(DrawerRow(label="x"), {"state": "answered", "tool_count": 3})
    assert row.note == "3 tools"


def test_failed_note_names_the_reason():
    """Tier 1: a failed probe's note states WHY (timeout/exception) — #4401
    ②'s own requirement that the owner can read the reason on screen
    instead of digging in .reyn/events."""
    row = _apply_mcp_probe_note(
        DrawerRow(label="x"), {"state": "failed", "reason": "timeout"},
    )
    assert row.note == "probe failed: timeout"


def test_retrying_note_is_distinct_from_failed_and_not_probed():
    """Tier 1: #4401 ③'s in-flight state gets its own wording, not
    conflated with "failed" (the retry hasn't resolved yet) or
    "not_probed" (a probe genuinely IS running)."""
    row = _apply_mcp_probe_note(DrawerRow(label="x"), {"state": "retrying"})
    assert row.note == "retrying…"


def test_unreported_snapshot_ignores_probe_states_even_when_present():
    """Tier 1: the ``mcp_probe_states_reported`` gate is checked, not just
    "is the list empty" — a connection that carries a (theoretically
    stale/untrustworthy) ``mcp_probe_states`` payload but says
    ``reported=False`` must still add NO probe note, never trusting data
    the producer itself disclaimed. A non-empty payload here is
    deliberate: an empty-list test would pass whether or not the gate
    check exists at all, silently."""
    rows = _mcp_pane_entries(_snap(
        [{"name": "broker", "state": "answered", "tool_count": 5}], reported=False,
    ))
    assert not any("probed" in (r[0]) or "tool" in r[0] for r in rows), (
        f"an un-reported connection must add no probe note; got {rows!r}"
    )


def test_reported_not_probed_state_renders_plainly_not_as_zero_tools():
    """Tier 1: end-to-end through ``_mcp_pane_entries`` — a "not_probed"
    server's row text says so plainly, distinct from a genuinely-answered
    zero-tool server's row text."""
    rows = _mcp_pane_entries(_snap([
        {"name": "broker", "state": "not_probed"},
        {"name": "filesystem", "state": "answered", "tool_count": 0},
    ]))
    assert rows[0] == ("[on] broker  · not yet probed", "/visibility off mcp broker")
    assert rows[1] == ("[on] filesystem  · 0 tools", "/visibility off mcp filesystem")


def test_failed_row_gets_a_retry_subrow_the_not_probed_row_does_not():
    """Tier 1: #4401 ③ — retry is offered ONLY on a row that actually
    failed a probe attempt, never on "not_probed" (nothing has failed
    yet)."""
    rows = _mcp_pane_entries(_snap([
        {"name": "broker", "state": "failed", "reason": "timeout"},
        {"name": "filesystem", "state": "not_probed"},
    ]))
    assert ("    ↻ retry probe", "/mcp retry broker") in rows
    assert ("    ↻ retry probe", "/mcp retry filesystem") not in rows


def test_retrying_row_gets_no_retry_subrow_a_second_retry_would_be_redundant():
    """Tier 1: a server already mid-retry offers no SECOND retry action —
    one already in flight covers it."""
    rows = _mcp_pane_entries(_snap([{"name": "broker", "state": "retrying"}]))
    assert ("    ↻ retry probe", "/mcp retry broker") not in rows
