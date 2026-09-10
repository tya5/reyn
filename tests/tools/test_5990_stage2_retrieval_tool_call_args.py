"""Tier 2: #5990 (part of, stage 2) — `RetrievalScheme.interpret`'s own
`except (json.JSONDecodeError, KeyError, TypeError): args = {}` swallowed
a malformed `search_actions` tool-call `arguments` string with no visible
report. Now warns before falling back to an empty query, same convention
#6038 established.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from reyn.tools.schemes.retrieval import RetrievalScheme

_LOGGER_NAME = "reyn.tools.schemes.retrieval"


@dataclass
class _FakeLLMResponse:
    tool_calls: "list[dict] | None"


def _search_call(arguments: str) -> dict:
    return {"function": {"name": "search_actions", "arguments": arguments}}


class _UnusedOps:
    """A `SchemeOps`-shaped stand-in whose methods are never actually
    called on this code path (the malformed-arguments branch returns a
    `RePresent` before ever reaching `ops.resolve`) — satisfies the
    static Protocol type only, not exercised at runtime."""

    def present(self, available, layer_ctx): ...
    def resolve(self, llm_response, tool_catalog): ...

    async def dispatch(self, actions, *, call_id=None): ...
    def feedback(self, result): ...
    def base_tools(self, available, layer_ctx): ...

    async def catalog_entries(self): ...
    async def search_actions(self, query, *, top_k=10): ...


def test_malformed_search_arguments_warns_and_falls_back_to_empty_query(caplog) -> None:
    """Tier 2: malformed JSON in `search_actions`'s own tool-call
    arguments warns AND `interpret` still returns a `RePresent` with an
    empty query rather than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call removed
    from `RetrievalScheme.interpret`): this test goes red — no record."""
    scheme = RetrievalScheme()
    response = _FakeLLMResponse(tool_calls=[_search_call("{not json")])
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        interp = scheme.interpret(response, tool_catalog={}, ops=_UnusedOps())
    assert interp.refinement == {"query": ""}
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_well_formed_search_arguments_do_not_warn(caplog) -> None:
    """Tier 2: negative control -- well-formed JSON arguments parse
    through with no warning at all."""
    scheme = RetrievalScheme()
    response = _FakeLLMResponse(tool_calls=[_search_call('{"query": "hello"}')])
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        interp = scheme.interpret(response, tool_catalog={}, ops=_UnusedOps())
    assert interp.refinement == {"query": "hello"}
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
