"""Tier 2: #5771 stage② — the 8 real cost keys reach a remote client's wire.

Owner report (relayed via architect): the web/connect cost tab stayed '-'.
Stage① (#5773) found and closed the structural drift (a project_remote_
snapshot output key not backed by a same-named project_status key); this
stage adds the missing project_status keys for real, so project_remote_
snapshot's own reads of them stop degrading.

Real project_status/project_remote_snapshot throughout — no mocks; a
snapshot dict is the only fixture, matching every other read-model test in
this package.
"""
from __future__ import annotations

from reyn.interfaces.repl.read_model import project_remote_snapshot
from reyn.interfaces.transport.agui.state import project_status
from reyn.llm.pricing import CostBreakdown


def _local_snapshot() -> dict:
    """A LOCAL _snapshot()-shaped dict carrying the 8 cost-tab fields the
    real producer (status.py) already builds — see that module's own
    dict literal for the field names this mirrors."""
    return {
        "cost_usd": 1.2345,
        "cost_breakdown_session": CostBreakdown(
            prompt_cost=0.5, completion_cost=0.6, prompt_tokens=100, cached_tokens=20,
        ),
        "cost_breakdown_agent": CostBreakdown(
            prompt_cost=0.3, completion_cost=0.2, prompt_tokens=50, cached_tokens=5,
        ),
        "cost_breakdown_project": CostBreakdown(
            prompt_cost=0.9, completion_cost=0.8, prompt_tokens=200, cached_tokens=40,
        ),
        "usage": (1000, 400, 1400),
        "cost_agent": 0.5,
        "cost_total": 1.5,
        "agent_tokens": 1400,
        "session_cached_tokens": 65,
        "turn_cost_usd": 0.02,
        "turn_tokens": 150,
        "turn_usage_fn": lambda chain_id: {"tokens": 1, "cost_usd": 0.0},
    }


def test_project_status_carries_the_8_cost_keys_for_real() -> None:
    """Tier 2: the wire-side producer — agui/state.py's project_status —
    emits real values for all 8 keys, not a placeholder."""
    out = project_status(_local_snapshot())
    assert out["cost_usd"] == 1.2345
    assert out["cost_breakdown_session"] == {
        "prompt_cost": 0.5, "cache_read_cost": 0.0, "cache_creation_cost": 0.0,
        "completion_cost": 0.6, "total_cost": 1.1, "cache_savings": 0.0,
        "cache_hit_rate": 0.2, "prompt_tokens": 100, "cached_tokens": 20,
    }
    assert out["usage"] == (1000, 400, 1400)
    assert out["session_cached_tokens"] == 65
    assert out["turn_cost_usd"] == 0.02
    assert out["turn_tokens"] == 150


def test_turn_usage_fn_never_reaches_project_status_output() -> None:
    """Tier 2: turn_usage_fn is a callable (Session.turn_usage, keyed
    per-chain_id) — architect's explicit instruction: it must never ride
    the wire. project_status's own dict literal simply never reads this
    key off snap.

    lead-coder BLOCKING (PR #5773, head 01bb92cbc): the negative
    membership assert alone would stay green even if project_status
    returned ``{}`` — indistinguishable from "the callable stopped
    leaking" and "this test is looking at nothing". ``assert "cost_usd"
    in out`` first is this test's OWN witness that the population is
    non-empty (not borrowed from a sibling test, which could be skipped
    or deleted later without this one noticing)."""
    out = project_status(_local_snapshot())
    assert "cost_usd" in out
    assert "turn_usage_fn" not in out


def test_project_remote_snapshot_decodes_the_cost_breakdown_round_trip() -> None:
    """Tier 2: the CostBreakdown wire round-trip — encode
    (agui/state.py's _cost_breakdown_wire, inside project_status) then
    decode (read_model.py's _cost_breakdown_from_wire, inside
    project_remote_snapshot) reconstructs a real CostBreakdown a
    consumer can read via ATTRIBUTE access (chrome.py's own
    _cost_breakdown_table does `snap.get("cost_breakdown_session") or
    CostBreakdown()`, never dict-style .get on the breakdown itself)."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)

    session_bd = remote_snap["cost_breakdown_session"]
    assert isinstance(session_bd, CostBreakdown)
    assert session_bd.prompt_cost == 0.5
    assert session_bd.completion_cost == 0.6
    assert session_bd.prompt_tokens == 100
    assert session_bd.cached_tokens == 20

    agent_bd = remote_snap["cost_breakdown_agent"]
    project_bd = remote_snap["cost_breakdown_project"]
    assert agent_bd.prompt_tokens == 50
    assert project_bd.prompt_tokens == 200


def test_project_remote_snapshot_carries_the_rest_of_the_8_keys_through() -> None:
    """Tier 2: the remaining 5 real values (not the CostBreakdown trio)
    survive the SAME wire round-trip un-degraded — no more (0, 0,
    agent_tokens)/0/alias placeholders (#5773's own root cause)."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)

    assert remote_snap["cost_usd"] == 1.2345
    assert remote_snap["usage"] == (1000, 400, 1400)
    assert remote_snap["session_cached_tokens"] == 65
    assert remote_snap["turn_cost_usd"] == 0.02
    assert remote_snap["turn_tokens"] == 150


def test_turn_usage_fn_never_reaches_project_remote_snapshot_output_either() -> None:
    """Tier 2: the SAME callable-never-on-wire property, at the remote
    read model's own output — project_remote_snapshot's own pre-existing
    ``"turn_usage_fn": None`` entry is a deliberate, PERMANENT LOCAL
    placeholder (#3283 ④: "a callable slot, always None remotely"), never
    read from ``v``. Confirms it stays exactly that: never the caller's
    OWN real callable smuggled through.

    lead-coder BLOCKING (PR #5773, head 01bb92cbc): same fix as this
    file's ``test_turn_usage_fn_never_reaches_project_status_output`` —
    a non-empty-population witness this test owns itself, not borrowed
    from a sibling."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)
    assert "cost_usd" in remote_snap
    assert remote_snap["turn_usage_fn"] is None


def test_the_8_reported_axes_flip_true_for_remote_once_wired() -> None:
    """Tier 2: the *_reported declarations that gate these keys' own
    consumers (chrome.py) are genuinely True for remote now — see
    test_5009_closing_pass_declarations.py /
    test_5011_ctx_cache_line_note.py for the per-axis assertions this
    would otherwise duplicate; this test only pins that project_remote_
    snapshot's own output carries them through."""
    wire_values = project_status(_local_snapshot())
    remote_snap = project_remote_snapshot(wire_values)
    assert remote_snap["usage_breakdown_reported"] is True
    assert remote_snap["session_cache_usage_reported"] is True
    # ctx_recent_usage's own axis is UNCHANGED by this stage — still False.
    assert remote_snap["cache_usage_reported"] is False


def test_an_old_server_that_never_sent_the_new_keys_degrades_gracefully() -> None:
    """Tier 2: backward compat (#5771's own acceptance criterion) — a
    server predating this stage never populated these 8 keys in its own
    wire payload at all (an empty ``values`` dict simulates the pre-#5771
    RemoteStatusView.values an old server's STATE_SNAPSHOT would produce).
    project_remote_snapshot must not crash and must fall back to the SAME
    graceful-degrade values the pre-#5771 placeholders already used —
    never a fabricated-looking real figure."""
    remote_snap = project_remote_snapshot({})
    assert remote_snap["cost_usd"] == 0.0
    assert remote_snap["cost_breakdown_session"] is None
    assert remote_snap["cost_breakdown_agent"] is None
    assert remote_snap["cost_breakdown_project"] is None
    assert remote_snap["usage"] == (0, 0, 0)
    assert remote_snap["session_cached_tokens"] == 0
    assert remote_snap["turn_cost_usd"] is None
    assert remote_snap["turn_tokens"] is None
