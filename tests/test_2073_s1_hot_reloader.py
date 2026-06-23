"""Tier 2: #2073 S1 — HotReloader core (safe-point orchestration + IN-set boundary).

S1 establishes the config hot-reload skeleton: schedule (request_reload) → apply at
the turn boundary (apply_pending) → re-read ONLY the IN-set (.reyn/*.yaml) → reapply
each seam (none in S1) → emit config_reloaded (P6). The load-bearing safety invariant
is the file-split: the loader NEVER reads the OUT-set (reyn.yaml), so a reload — and
the LLM-op that triggers one — cannot touch security/budget/valve.

No mocks: real EventLog / HotReloader / the real load_hot_reload_config loader.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.loader import load_hot_reload_config
from reyn.core.events.events import EventLog
from reyn.runtime.hot_reload import HotReloader

# ── SAFETY: the IN-set boundary (the OUT-set is never read) ──────────────────


def test_load_hot_reload_config_reads_only_in_set(tmp_path: Path) -> None:
    """Tier 2: load_hot_reload_config reads ONLY the IN-set (.reyn/*.yaml). The
    OUT-set (reyn.yaml security/budget/valve) is NEVER opened — the structural
    write-gate boundary (review-focus d)."""
    # OUT-set: reyn.yaml with security + budget keys
    (tmp_path / "reyn.yaml").write_text(
        "permissions:\n  shell: deny\nbudget:\n  daily_usd: 5\n", encoding="utf-8",
    )
    # IN-set: .reyn/mcp.yaml
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    (reyn_dir / "mcp.yaml").write_text(
        "mcp:\n  servers:\n    fs:\n      type: stdio\n", encoding="utf-8",
    )
    in_set = load_hot_reload_config(tmp_path)
    assert "mcp" in in_set                 # IN-set IS read
    assert "permissions" not in in_set     # OUT-set NEVER read
    assert "budget" not in in_set


def test_load_hot_reload_config_absent_is_empty_noop(tmp_path: Path) -> None:
    """Tier 2: no .reyn/ dir → {} (a no-op reload, never an error)."""
    assert load_hot_reload_config(tmp_path) == {}


# ── orchestration: schedule → apply → event → clear ─────────────────────────


@pytest.mark.asyncio
async def test_nothing_pending_is_noop(tmp_path: Path) -> None:
    """Tier 2: apply_pending with no scheduled reload returns None + emits nothing
    (zero-overhead happy path)."""
    events = EventLog()
    hr = HotReloader(project_root=tmp_path, events=events)
    assert hr.pending is False
    assert await hr.apply_pending() is None
    assert [e.type for e in events.all()] == []


@pytest.mark.asyncio
async def test_request_then_apply_emits_event_and_clears(tmp_path: Path) -> None:
    """Tier 2: request_reload schedules; apply_pending re-reads the IN-set, emits the
    config_reloaded P6 event (review-focus b), and clears pending."""
    events = EventLog()
    hr = HotReloader(project_root=tmp_path, events=events)
    hr.request_reload(source="operator")
    assert hr.pending is True

    summary = await hr.apply_pending()
    assert summary is not None and summary["source"] == "operator"
    assert hr.pending is False
    reloaded_sources = [
        e.data["source"] for e in events.all() if e.type == "config_reloaded"
    ]
    assert reloaded_sources == ["operator"]  # exactly one reload, by the operator


@pytest.mark.asyncio
async def test_idempotent_within_turn(tmp_path: Path) -> None:
    """Tier 2: multiple requests before a boundary collapse into ONE apply (1 turn =
    1 config snapshot); the last source wins."""
    events = EventLog()
    hr = HotReloader(project_root=tmp_path, events=events)
    hr.request_reload(source="a")
    hr.request_reload(source="b")
    summary = await hr.apply_pending()
    assert summary["source"] == "b"
    assert await hr.apply_pending() is None  # only one apply
    # collapsed into a single apply (one event), last source wins
    assert [
        e.data["source"] for e in events.all() if e.type == "config_reloaded"
    ] == ["b"]


# ── seams: per-component reapply hook (S2 wires real seams) ──────────────────


@pytest.mark.asyncio
async def test_seams_called_with_in_set_and_isolated(tmp_path: Path) -> None:
    """Tier 2: each registered seam is called with the re-read IN-set; a raising seam
    is isolated (recorded under `failed`, never breaks the apply)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    (reyn_dir / "mcp.yaml").write_text("mcp:\n  servers: {}\n", encoding="utf-8")
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    seen: dict = {}

    async def good(in_set: dict) -> bool:
        seen["in_set"] = in_set
        return True

    async def bad(in_set: dict) -> bool:
        raise RuntimeError("boom")

    hr.register_seam("good", good)
    hr.register_seam("bad", bad)
    hr.request_reload(source="llm_op")
    summary = await hr.apply_pending()

    assert summary["applied"] == ["good"]
    assert summary["failed"] == ["bad"]
    assert "mcp" in seen["in_set"]  # the seam received the re-read IN-set


# ── wiring: the config_reloaded notification + the /reload command ──────────


def test_config_reloaded_event_in_notification_map() -> None:
    """Tier 2: config_reloaded is wired into the state-change notification map (so the
    reload surfaces to the LLM), not just emitted to the log."""
    from reyn.runtime.session import _STATE_CHANGE_EVENT_MAPPINGS
    assert "config_reloaded" in _STATE_CHANGE_EVENT_MAPPINGS


def test_reload_slash_command_registered() -> None:
    """Tier 2: the operator /reload command is registered."""
    from reyn.interfaces.slash import REGISTRY
    assert REGISTRY.get("reload") is not None
