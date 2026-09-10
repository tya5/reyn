"""Tier 2: #5990 (part of, stage 2) — `op_runtime.embed._resolve_provider`'s
own `except Exception: cfg = None` swallowed a `reyn.yaml` load failure
with no visible report. Now warns before falling back to the litellm
provider's own defaults, same convention #6038 established.
"""
from __future__ import annotations

import logging

import reyn.core.op_runtime.embed as embed_module

_LOGGER_NAME = "reyn.core.op_runtime.embed"


def test_config_load_failure_warns_and_falls_back_to_provider_defaults(
    monkeypatch, caplog,
) -> None:
    """Tier 2: `load_config()` raising while resolving the embedding
    config warns AND `_resolve_provider` still returns a provider (no
    exception propagates).

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from `_resolve_provider`'s `except Exception`): this test
    goes red — no record at all."""
    import reyn.config as config_module

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated reyn.yaml load failure")

    monkeypatch.setattr(config_module, "load_config", _raise)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        provider = embed_module._resolve_provider()

    assert provider is not None
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("reyn.yaml" in r.message for r in records)


def test_normal_config_load_does_not_warn(caplog) -> None:
    """Tier 2: negative control -- a normal (non-raising) config load
    never reaches the except branch, so no warning fires."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        embed_module._resolve_provider()
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
