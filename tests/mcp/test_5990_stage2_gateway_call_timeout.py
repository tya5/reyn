"""Tier 1: #5990 (part of, stage 2) — `mcp.gateway.resolve_call_timeout`'s
own `except (TypeError, ValueError): timeout = default` swallowed a
malformed `call_timeout_seconds` server config value with no visible
report. Now warns before falling back, same convention #6038 established
for `config/`'s own parse-error sites.
"""
from __future__ import annotations

import logging

from reyn.mcp.gateway import _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, resolve_call_timeout

_LOGGER_NAME = "reyn.mcp.gateway"


def test_malformed_call_timeout_warns_and_falls_back(caplog) -> None:
    """Tier 1: a non-numeric `call_timeout_seconds` warns AND the default
    timeout is still what is returned.

    Strip-falsify (verified by hand: the `logging.getLogger(__name__).
    warning(...)` call removed): this test goes red — no record at all."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        timeout = resolve_call_timeout({"call_timeout_seconds": "oops"})
    assert timeout == _DEFAULT_MCP_CALL_TIMEOUT_SECONDS
    assert any("call_timeout_seconds" in r.message for r in caplog.records)


def test_well_formed_call_timeout_does_not_warn(caplog) -> None:
    """Tier 1: negative control — a well-formed numeric value parses
    through with no warning at all."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        timeout = resolve_call_timeout({"call_timeout_seconds": 30})
    assert timeout == 30.0
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
