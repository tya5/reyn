"""Tier 2: web.ws_max_size explicit WebSocket frame ceiling (#1934 part B).

The `reyn web` gateway previously relied on uvicorn's IMPLICIT ~16 MiB ws_max_size
default to bound inbound WebSocket frames. This pins it EXPLICITLY + makes it
operator-tunable via `web.ws_max_size`, so a uvicorn version bump or an operator
server override cannot silently drop the bound. Hardening (fragility removal),
not an unbounded-vuln fix — frames were already server-bounded at 16 MiB.

Policy: real `_build_web_config` + real `web.run` (uvicorn.run / load_config are
the external/boundary seams, patched). Tier line first.
"""
from __future__ import annotations

import argparse
import dataclasses

import pytest

from reyn.config.media import (
    DEFAULT_WS_MAX_SIZE,
    WebConfig,
    _build_web_config,
)

# ── config round-trip ────────────────────────────────────────────────────────

def test_ws_max_size_round_trip_non_default():
    """Tier 2: a NON-DEFAULT ws_max_size round-trips — a non-default value proves
    real parsing (a default value would pass even if the field were unwired)."""
    cfg = _build_web_config({"ws_max_size": 5_000_000})
    assert cfg.ws_max_size == 5_000_000
    assert cfg.ws_max_size != DEFAULT_WS_MAX_SIZE


def test_ws_max_size_default_when_absent():
    """Tier 2: a missing ws_max_size → the named default constant."""
    assert _build_web_config({}).ws_max_size == DEFAULT_WS_MAX_SIZE
    assert WebConfig().ws_max_size == DEFAULT_WS_MAX_SIZE


@pytest.mark.parametrize("bad", [0, -5, "x", None])
def test_ws_max_size_invalid_falls_back_to_default(bad):
    """Tier 2: <=0 / non-integer falls back to the default (no crash, no zero cap)."""
    assert _build_web_config({"ws_max_size": bad}).ws_max_size == DEFAULT_WS_MAX_SIZE


# ── wiring: the value reaches uvicorn.run ─────────────────────────────────────

def test_web_run_passes_ws_max_size_to_uvicorn(monkeypatch):
    """Tier 2: `reyn web` threads the configured ws_max_size through to uvicorn.run
    (the bound is actually applied at the server, not merely stored in config)."""
    import uvicorn

    import reyn.config as config_mod
    import reyn.interfaces.cli.commands.web as web_cmd
    from reyn.config.root import ReynConfig

    cfg = dataclasses.replace(ReynConfig(), web=WebConfig(ws_max_size=7_000_000))
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: captured.update(k))

    web_cmd.run(
        argparse.Namespace(host="127.0.0.1", port=8080, reload=False, log_level="info")
    )
    assert captured.get("ws_max_size") == 7_000_000
