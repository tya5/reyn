"""Tier 2: #2372 — installing an MCP server surfaces its tools within the same session (no restart).

Two gaps made MCP install require a restart: (1) mcp_install triggered no refresh, and (2) the
server ROSTER (`RouterHostAdapter._mcp_servers`, gating the LLM-facing enumeration) was frozen at
ctor. Fix: `refresh_mcp_servers` re-reads the roster from the config cascade (which merges the
IN-set `.reyn/config/mcp.yaml` that mcp_install writes) BEFORE the tool-probe chain, swapping both
the Session field and the adapter's roster; and mcp_install schedules a hot-reload so the mcp seam
(`_reapply_mcp` → `refresh_mcp_servers`) runs at the next turn boundary. Refreshing the tools cache
alone is insufficient — a server absent from the roster has no entry to attach its tools to.

The falsify exercises the roster re-read directly (the load-bearing gap-2 fix): a server written to
the IN-set mid-session appears in the router enumeration after `refresh_mcp_servers`, no restart.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _session(tmp_path: Path) -> Session:
    # load_config resolves the project root by walking up for reyn.yaml (the marker gating the
    # dynamic .reyn/config/mcp.yaml read); a Reyn project always has one.
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    s = make_session(
        agent_name="alice", state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    s.register_intervention_listener("test")
    return s


def _install_server_in_config(tmp_path: Path, name: str) -> None:
    """Write a new MCP server to the IN-set `.reyn/config/mcp.yaml` (where mcp_install writes)."""
    cfg = tmp_path / ".reyn" / "config" / "mcp.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        yaml.safe_dump({"mcp": {"servers": {name: {"command": "/nonexistent", "description": "d"}}}}),
        encoding="utf-8",
    )


def _server_names(session: Session) -> list[str]:
    return [s["name"] for s in session._router_host.get_mcp_servers()]


@pytest.mark.asyncio
async def test_installed_server_appears_after_refresh_without_restart(tmp_path, monkeypatch):
    """Tier 2: a server installed mid-session (written to the IN-set) is enumerated by the router
    after refresh_mcp_servers — no restart. RED today (roster frozen at ctor → not enumerated),
    GREEN after (roster re-read from the cascade)."""
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path)
    assert "gh" not in _server_names(session)  # absent before install

    _install_server_in_config(tmp_path, "gh")
    await session.refresh_mcp_servers()

    assert "gh" in _server_names(session), "the installed server must be enumerated without a restart"


@pytest.mark.asyncio
async def test_install_is_additive_existing_servers_survive(tmp_path, monkeypatch):
    """Tier 2: installing a second server does NOT drop the first — the roster re-read is the full
    cascade, so a mid-session install is additive (no regression to already-configured servers)."""
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path)

    _install_server_in_config(tmp_path, "srv1")
    await session.refresh_mcp_servers()
    assert _server_names(session) == ["srv1"] or "srv1" in _server_names(session)

    # a second install adds to the file (mirrors mcp_install's merge) → both must enumerate
    cfg = tmp_path / ".reyn" / "config" / "mcp.yaml"
    cfg.write_text(
        yaml.safe_dump({"mcp": {"servers": {
            "srv1": {"command": "/nonexistent", "description": "d1"},
            "srv2": {"command": "/nonexistent", "description": "d2"},
        }}}),
        encoding="utf-8",
    )
    await session.refresh_mcp_servers()

    names = _server_names(session)
    assert "srv1" in names and "srv2" in names, "install is additive — existing servers survive"
