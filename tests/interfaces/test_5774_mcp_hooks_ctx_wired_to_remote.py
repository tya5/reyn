"""Tier 2: #5774 — the 3 remaining #5773-exposed keys reach a remote client.

lead-coder's own follow-up dispatch after #5771 landed: mcp_probe_states
(#4401 ②③'s own per-server probe display — owner's stated purpose, "tui mcp
tab でユーザは気付けて対処できる", was silently unmet for a remote attach)
and hooks_config_warnings were both real, per-connection server state that
had been mis-triaged as permanently session-local. ctx_recent_usage was a
THIRD case found mid-review: #5771 stage②'s own axis split
(cache_usage_reported -> session_cache_usage_reported) fixed the Cost
pane's cache-hit figure but left the Ctx pane's sibling figure on the same
old, now-stale "not reported" axis value.

Real project_status/project_remote_snapshot throughout — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import config_warning_text, ctx_pane_lines
from reyn.interfaces.repl.read_model import project_remote_snapshot
from reyn.interfaces.transport.agui.state import project_status


def _local_snapshot() -> dict:
    """A LOCAL _snapshot()-shaped dict carrying the 3 fields status.py's
    real producer already builds."""
    return {
        "mcp_probe_states": [
            {"name": "server-a", "state": "answered", "tool_count": 5},
            {"name": "server-b", "state": "failed", "reason": "timeout"},
            {"name": "server-c", "state": "not_probed"},
        ],
        "hooks_config_warnings": ["hooks.yaml could not be read: bad.yaml (~/.reyn/agents/x)"],
        "ctx_window": 200000,
        "ctx_used": 48120,
        "ctx_recent_usage": (48120, 14900),
    }


def test_project_status_carries_the_3_keys_for_real() -> None:
    """Tier 2: agui/state.py's project_status emits real values, not a
    placeholder, for all 3."""
    out = project_status(_local_snapshot())
    assert "mcp_probe_states" in out
    assert out["mcp_probe_states"] == [
        {"name": "server-a", "state": "answered", "tool_count": 5},
        {"name": "server-b", "state": "failed", "reason": "timeout"},
        {"name": "server-c", "state": "not_probed"},
    ]
    assert out["hooks_config_warnings"] == [
        "hooks.yaml could not be read: bad.yaml (~/.reyn/agents/x)",
    ]
    assert out["ctx_recent_usage"] == (48120, 14900)


def test_project_remote_snapshot_carries_all_3_through() -> None:
    """Tier 2: the wire round-trip — no more fixed [] / [] / (0, 0)
    placeholders (#5773's own root cause, extended to these 3 by #5774)."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)

    assert remote_snap["mcp_probe_states"] == [
        {"name": "server-a", "state": "answered", "tool_count": 5},
        {"name": "server-b", "state": "failed", "reason": "timeout"},
        {"name": "server-c", "state": "not_probed"},
    ]
    assert remote_snap["hooks_config_warnings"] == [
        "hooks.yaml could not be read: bad.yaml (~/.reyn/agents/x)",
    ]
    assert remote_snap["ctx_recent_usage"] == (48120, 14900)


def test_the_3_reported_axes_flip_true_for_remote() -> None:
    """Tier 2: the *_reported declarations gating these keys' own
    consumers (chrome.py) are genuinely True for remote now."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)
    assert remote_snap["mcp_probe_states_reported"] is True
    assert remote_snap["hooks_config_warnings_reported"] is True
    assert remote_snap["cache_usage_reported"] is True


def test_an_old_server_that_never_sent_these_3_keys_degrades_gracefully() -> None:
    """Tier 2: backward compat — an empty ``values`` dict simulates a
    pre-#5774 server's own STATE_SNAPSHOT (never populated these keys).
    project_remote_snapshot must fall back to the SAME graceful-degrade
    values the pre-#5774 placeholders already used."""
    remote_snap = project_remote_snapshot({})
    assert remote_snap["mcp_probe_states"] == []
    assert remote_snap["hooks_config_warnings"] == []
    assert remote_snap["ctx_recent_usage"] == (0, 0)


def test_ctx_pane_renders_the_real_recent_cache_figure_once_wired() -> None:
    """Tier 2: end-to-end witness (chrome.py's own consumer, not just the
    read-model layer) — the Ctx pane's recent-call cache line renders a
    real percentage now that cache_usage_reported is True for remote,
    mirroring test_5011_ctx_cache_line_note.py's own established
    assertions but driven off the real project_remote_snapshot output.

    Scoped to the CACHE line specifically (#5588's own established
    caution, test_5009_cache_reported_declaration.py's own docstring): a
    blanket "not reported" not in blob would also trip on this pane's
    OTHER, independently-gated rows (compaction/folded) that are
    genuinely unreported here and out of this test's own scope."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)
    blob = "\n".join(ctx_pane_lines(remote_snap))
    assert "31% hit" in blob, blob
    (cache_line,) = [ln for ln in ctx_pane_lines(remote_snap) if ln.strip().startswith("cache")]
    assert "not reported" not in cache_line, cache_line


def test_config_warning_text_renders_the_real_hooks_warning_once_wired() -> None:
    """Tier 2: end-to-end witness — config_warning_text (chrome.py) shows
    the real hooks.yaml warning text now that hooks_config_warnings_
    reported is True for remote, instead of degrading."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)
    text = config_warning_text(
        0, keys=None,
        hooks_warnings=remote_snap["hooks_config_warnings"],
        hooks_warnings_reported=remote_snap["hooks_config_warnings_reported"],
    )
    assert text is not None
    assert "bad.yaml" in text
