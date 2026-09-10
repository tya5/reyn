"""Tier 2: #5990 (part of, stage 2) — `RouterLoop._resolve_tool_call`'s
own `except (json.JSONDecodeError, KeyError): args = {}` swallowed a
malformed tool-call `arguments` string with no visible report. Now warns
before falling back to no-args, same convention #6038 established.
"""
from __future__ import annotations

import logging
from typing import Any

from reyn.runtime.router_loop import RouterLoop

_LOGGER_NAME = "reyn.runtime.router_loop"


class _RecordingEvents:
    def __init__(self) -> None:
        self.events: "list[tuple[str, dict]]" = []

    def emit(self, type: str, **data: Any) -> None:
        self.events.append((type, dict(data)))


class _ResolveShim:
    """Minimal RouterLoop surface to exercise the real `_resolve_tool_call`
    -- same shape as test_provider_tool_namespace_strip_1989.py's own
    `_ResolveShim`, duplicated per this repo's own test-isolation
    convention rather than imported across test files."""

    chain_id = "test-chain"
    _resolve_tool_call = RouterLoop._resolve_tool_call
    _maybe_salvage_action_direct_call = RouterLoop._maybe_salvage_action_direct_call

    def __init__(self, catalog) -> None:
        self._catalog = catalog
        self._dispatch_catalog = None

        class _Host:
            events = _RecordingEvents()
        self.host = _Host()


def _tc(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def test_malformed_tool_call_arguments_warns_and_falls_back_to_no_args(caplog) -> None:
    """Tier 2: malformed JSON in a tool call's own `arguments` string warns
    AND resolution still falls back to `{}` rather than raising.

    Strip-falsify (verified by hand: the `logger.warning(...)` call
    removed from `_resolve_tool_call`): this test goes red — no record at
    all, though `args` still comes back `{}` either way (the SAME false-
    green risk #6038's own review caught once already -- the message
    assertion is the real witness here, not just the return value)."""
    shim = _ResolveShim(catalog={"invoke_action": object()})
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        name, args, raw_name = shim._resolve_tool_call(_tc("invoke_action", "{not json"))
    assert name == "invoke_action"
    assert args == {}
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_well_formed_tool_call_arguments_do_not_warn(caplog) -> None:
    """Tier 2: negative control -- well-formed JSON arguments parse through
    with no warning at all."""
    shim = _ResolveShim(catalog={"invoke_action": object()})
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        name, args, raw_name = shim._resolve_tool_call(_tc("invoke_action", '{"x": 1}'))
    assert args == {"x": 1}
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
