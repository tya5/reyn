"""Tier 2: #2259 — the PR-A2 config-recovery TRUNCATION bug (RED on main, GREEN under fix).

PR-A2 made config recovery read ONLY `config_changed` WAL events; the WAL is truncated below
floor = min(agent applied_seq); `config_changed` is NOT exempt from truncation and config is
in NO snapshot. ⇒ a registry whose latest `config_changed` falls below the truncation floor is
silently LOST on reconstruct: events alone cannot reconstruct config because events get
truncated. The fix is config-as-snapshot (full-state, survives truncation, like the agent
snapshot).

This test asserts the CORRECT as-of-cut config. It is RED on current main (the bug) and the
GREEN target once config is a truncation-surviving snapshot.
"""
from __future__ import annotations

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called")


@pytest.mark.asyncio
async def test_config_survives_wal_truncation_below_its_seq(tmp_path):
    """Tier 2: a config set at an early seq, then truncated below the agents' floor, must
    still be reconstructable on a rewind to a cut ≥ its seq. RED on main: config_changed@early
    is truncated → reconstruct loses it (the registry is dropped). GREEN under config-as-
    snapshot: the full config state survives truncation as a base, like the agent snapshot."""
    sl = StateLog(tmp_path / ".reyn" / "config" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=sl,
    )
    mcp_path = tmp_path / ".reyn" / "config" / "mcp.yaml"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    # seq 1: install MCP server A.  This is the as-of state we'll rewind to.
    await reg.record_config_change("config/mcp.yaml", {"mcp": {"servers": {"A": {}}}})
    cut = sl.current_seq  # == 1

    # the agents advance far past it (filler events → the truncation floor climbs).
    for i in range(120):
        await sl.append("inbox_put", n=i)

    # a LATER config change (post-floor) — add server B.
    await reg.record_config_change("config/mcp.yaml", {"mcp": {"servers": {"A": {}, "B": {}}}})
    mcp_path.write_text(  # the live on-disk state mirrors the latest change
        yaml.dump({"mcp": {"servers": {"A": {}, "B": {}}}}), encoding="utf-8",
    )

    # GC truncates the WAL below floor 100 (= min agent applied_seq) → config_changed@1 GONE.
    stats = await sl.truncate_below(100)
    assert stats["dropped"] >= 1, "the early config_changed should have been truncated"

    # rewind to cut=1: config should reconstruct to {A} (its state as-of seq 1).
    reg._reconcile_config_as_of_cut(cut)

    assert mcp_path.is_file(), "config must survive the rewind (RED on main: truncated → dropped)"
    restored = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))
    assert set(restored["mcp"]["servers"]) == {"A"}, (
        "rewind to seq 1 must restore config {A} — but config_changed@1 was truncated below "
        "the floor and config is in no snapshot, so events-alone reconstruct LOSES it (the bug)"
    )
