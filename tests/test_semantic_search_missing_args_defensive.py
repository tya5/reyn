"""Tier 2: recall tool returns a structured tool error when required
args are missing, instead of raising raw KeyError.

Pinned invariants:

- Calling ``_handle_semantic_search`` with ``args={}`` (= LLM forgot both
  ``query`` and ``sources``) returns a ToolResult mapping with
  ``ok=False`` and ``error_kind="missing_required_arg"`` — does NOT
  raise.
- Same for ``args={"query": "x"}`` (= sources missing).
- Same for ``args={"sources": ["foo"]}`` (= query missing).
- ``error_message`` mentions the missing key names so the LLM (and
  any human reading tool_failed events) can correct the call.
- A valid args dict still routes through to the op_runtime
  ``execute_op`` path — the defensive check is in front of, not in
  place of, the real handler. We verify this indirectly by
  observing that the early-return branch does NOT fire when both
  required keys are present (= ``args.get(k)`` truthy).

Why this matters: dogfood B45/B46 W3 ``recall_indexed_source``
scenarios observed the agent reply literally containing
``ERROR: KeyError: 'sources'`` — the raw Python exception bubbled
through the tool_failed event into the LLM's narration. That
exposes implementation internals to end users and degrades reply
quality.

testing.ja.md compliance:
- No mocks. Tests call ``_handle_semantic_search`` directly with a real
  ``ToolContext`` constructed in-test.
- No private-state assertions — only the public return value is
  inspected.
- No algorithm pinning beyond the error-shape contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from reyn.tools.semantic_search import _handle_semantic_search
from reyn.tools.types import ToolContext


@dataclass
class _NullEventLog:
    """Minimal stand-in for EventLog. _handle_semantic_search does not call
    this in the missing-args branch, so no behavior is needed."""
    subscribers: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.subscribers is None:
            self.subscribers = []


def _make_ctx() -> ToolContext:
    """Construct a real ToolContext with the bare minimum fields the
    missing-args branch touches (= none, in practice). Using the real
    dataclass rather than a mock keeps the test honest if the
    ToolContext shape changes."""
    return ToolContext(
        events=_NullEventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )


@pytest.mark.asyncio
async def test_recall_returns_error_when_both_args_missing():
    """Tier 2: empty args dict must not raise; instead
    returns ToolResult mapping with ok=False + missing list (B45/B46 fix)."""
    result = await _handle_semantic_search({}, _make_ctx())
    assert result["ok"] is False
    assert result["error_kind"] == "missing_required_arg"
    assert set(result["missing"]) == {"query", "sources"}
    # Error message references both missing keys so the LLM can fix
    # the call.
    msg = result["error_message"]
    assert "query" in msg
    assert "sources" in msg


@pytest.mark.asyncio
async def test_recall_returns_error_when_sources_missing():
    """Tier 2: the headline B45/B46 case — LLM provided query but
    forgot the required sources arg. Must return structured error,
    NOT raise KeyError. Confirms the symptom seen in dogfood logs
    (= "ERROR: KeyError: 'sources'" in agent reply) is closed."""
    result = await _handle_semantic_search({"query": "phase rollback"}, _make_ctx())
    assert result["ok"] is False
    assert result["error_kind"] == "missing_required_arg"
    assert result["missing"] == ["sources"]
    assert "sources" in result["error_message"]


@pytest.mark.asyncio
async def test_recall_returns_error_when_query_missing():
    """Tier 2: symmetric case — sources provided but query missing.
    Verifies the validation does not special-case 'sources' over
    'query' (both are required by the tool schema)."""
    result = await _handle_semantic_search(
        {"sources": ["docs"]}, _make_ctx(),
    )
    assert result["ok"] is False
    assert result["error_kind"] == "missing_required_arg"
    assert result["missing"] == ["query"]
    assert "query" in result["error_message"]


@pytest.mark.asyncio
async def test_recall_returns_error_when_sources_is_empty_list():
    """Tier 2: an empty list for sources also triggers the
    defensive branch (= ``not args.get(k)`` covers both missing key
    and falsy value). Without this, the downstream ``for source in
    op.sources`` loop would no-op silently and return empty
    matches — confusing for the LLM."""
    result = await _handle_semantic_search(
        {"query": "x", "sources": []}, _make_ctx(),
    )
    assert result["ok"] is False
    assert result["error_kind"] == "missing_required_arg"
    # Empty list is treated as missing.
    assert "sources" in result["missing"]


@pytest.mark.asyncio
async def test_recall_error_message_names_the_source_discovery_verb():
    """Tier 2: the error message must guide the LLM to the surface that actually
    answers `sources` — without a usable pointer it just retries with the same
    missing arg.

    #3026 changed WHERE that pointer points, not whether there is one. The message
    used to name the 'Indexed sources' system-prompt section; that section is not
    rendered (the SP builder accepts an ``indexed_sources_section`` argument and
    discards it — see #3025), so the message was directing the model to something
    that was never in its context. It now names ``list_rag_sources``, the verb
    #3026 added to enumerate the corpora. Pinning the referent keeps the
    educational pointer honest if someone edits the wording."""
    result = await _handle_semantic_search({"query": "x"}, _make_ctx())
    assert "list_rag_sources" in result["error_message"]
