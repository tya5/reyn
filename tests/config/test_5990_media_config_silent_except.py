"""Tier 1: #5990 (part of) — 3 `src/reyn/config/media.py` parse-error sites
that used to swallow a malformed value with a bare
``except (TypeError, ValueError): x = default`` report NOTHING an
operator would ever see. Each site now calls
``logging.getLogger(__name__).warning(...)`` on the SAME except branch
before falling back — each test below pins, per site, BOTH halves: (a)
the malformed value produces a visible warning, and (b) the default is
still what is returned.
"""
from __future__ import annotations

import logging

from reyn.config.media import (
    DEFAULT_WS_MAX_SIZE,
    GatewayConfig,
    MultimodalConfig,
    WebFetchConfig,
    _build_gateway_config,
    _build_multimodal_config,
    _build_web_fetch_config,
)

_LOGGER_NAME = "reyn.config.media"


def test_web_fetch_max_download_bytes_warns_and_falls_back(caplog) -> None:
    """Tier 1: `web_fetch.max_download_bytes` — invalid value warns AND
    the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_web_fetch_config({"max_download_bytes": "oops"})
    assert cfg.max_download_bytes == WebFetchConfig().max_download_bytes
    assert any(
        "web_fetch.max_download_bytes" in r.message for r in caplog.records
    )


def test_gateway_ws_max_size_warns_and_falls_back(caplog) -> None:
    """Tier 1: `gateway.ws_max_size` — invalid value warns AND the
    default (`DEFAULT_WS_MAX_SIZE`) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_gateway_config({"ws_max_size": "oops"})
    assert cfg.ws_max_size == DEFAULT_WS_MAX_SIZE == GatewayConfig().ws_max_size
    assert any("gateway.ws_max_size" in r.message for r in caplog.records)


def test_multimodal_max_bytes_warns_and_falls_back(caplog) -> None:
    """Tier 1: `multimodal.max_bytes` — invalid value warns AND the
    default (5_000_000) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_multimodal_config({"max_bytes": "oops"})
    assert cfg.max_bytes == MultimodalConfig().max_bytes
    assert any("multimodal.max_bytes" in r.message for r in caplog.records)


# ── negative controls: well-formed values never warn ─────────────────────────


def test_well_formed_media_values_do_not_warn(caplog) -> None:
    """Tier 1: negative control — well-formed `web_fetch:`/`gateway:`/
    `multimodal:` blocks parse through with no warning at all."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        web_cfg = _build_web_fetch_config({"max_download_bytes": 123})
        gw_cfg = _build_gateway_config({"ws_max_size": 456})
        mm_cfg = _build_multimodal_config({"max_bytes": 789})
    assert web_cfg.max_download_bytes == 123
    assert gw_cfg.ws_max_size == 456
    assert mm_cfg.max_bytes == 789
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
