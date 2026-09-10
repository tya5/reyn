"""Tier 2: #5990 (part of, stage 2) — the `/web/data` `web_data` endpoint's
own `except Exception: agents = []` swallowed a registry-build failure
with no visible report. Now warns before falling back to an empty agent
list, same convention #6038 established.
"""
from __future__ import annotations

import logging

import pytest

import reyn.interfaces.web.routers.web_data as web_data_module

_LOGGER_NAME = "reyn.interfaces.web.routers.web_data"


@pytest.mark.asyncio
async def test_registry_build_failure_warns_and_falls_back_to_empty_agents(
    tmp_path, monkeypatch, caplog,
) -> None:
    """Tier 2: `_build_agents` raising warns AND `web_data` still returns
    `AGENTS: []` rather than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from `web_data`'s `except Exception`): this test goes red —
    no record at all."""
    def _raise(registry):
        raise RuntimeError("simulated registry build failure")

    monkeypatch.setattr(web_data_module, "_build_agents", _raise)
    monkeypatch.setattr(web_data_module, "get_registry", lambda: object())

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = await web_data_module.web_data(project_root=tmp_path)

    assert result["AGENTS"] == []
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("AGENTS" in r.message for r in records)


@pytest.mark.asyncio
async def test_normal_registry_does_not_warn(tmp_path, monkeypatch, caplog) -> None:
    """Tier 2: negative control -- a normal (non-raising) registry build
    never reaches the except branch, so no warning fires."""
    monkeypatch.setattr(web_data_module, "get_registry", lambda: object())
    monkeypatch.setattr(web_data_module, "_build_agents", lambda registry: [])

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await web_data_module.web_data(project_root=tmp_path)

    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
