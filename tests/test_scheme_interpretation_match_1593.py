"""Tier 2: #1593 loop-unify (Issue-1) — interpret-driven routing classification.

The loop-unify makes routing **interpret-driven**: the OS classifies every LLM
result via the active scheme's ``interpret()`` and routes on the returned
``Interpretation`` (instead of sniffing ``result.tool_calls``). universal-category
returns:
  - ``Execute``   when the response has tool calls → the tool-round path, and
  - ``PlainText`` when it has none → the terminal text-reply path,
which is **byte-identical** to the former ``if result.tool_calls:`` gate (tool path
vs text-reply). This test pins that classification.

The loop-level dispatch itself (Execute→tool / PlainText→text, plus the defensive
``CodeBlock``/``RePresent`` guards that no on-main scheme emits) is exercised
byte-identically by the existing router suite; ``CodeBlock``/``RePresent``
reachability lands with PR-3 (CodeAct) / PR-4 (retrieval). Real scheme + a real Fake
SchemeOps — no mocks.
"""
from __future__ import annotations

from types import SimpleNamespace

from reyn.tools.scheme import Execute, PlainText
from reyn.tools.schemes.universal_category import UniversalCategoryScheme


class _FakeOps:
    """A real Fake ``SchemeOps`` — ``resolve`` returns canned actions (universal's
    ``interpret`` delegates resolution to it for the tool-call case)."""

    def resolve(self, llm_response, tool_catalog):
        return [
            {"tc": tc, "name": tc["function"]["name"], "args": {}}
            for tc in (llm_response.tool_calls or [])
        ]


def test_interpret_tool_calls_to_execute() -> None:
    """Tier 2: a response WITH tool calls → Execute (the tool-round path)."""
    resp = SimpleNamespace(
        content="",
        tool_calls=[{"id": "c1", "function": {"name": "file__read", "arguments": "{}"}}],
    )
    interp = UniversalCategoryScheme().interpret(resp, tool_catalog={}, ops=_FakeOps())
    assert isinstance(interp, Execute)
    # Behavior: the tool call resolved into an Execute action carrying its effective
    # name (not a count/shape pin) — the OS exclude-gates this pre-dispatch.
    assert [a["name"] for a in interp.actions] == ["file__read"]


def test_interpret_empty_tool_calls_to_plaintext() -> None:
    """Tier 2: NO tool calls → PlainText (terminal text-reply) — byte-identical to
    the former empty-``tool_calls`` → text-reply gate."""
    resp = SimpleNamespace(content="here is your answer", tool_calls=[])
    interp = UniversalCategoryScheme().interpret(resp, tool_catalog={}, ops=_FakeOps())
    assert isinstance(interp, PlainText)


def test_interpret_none_tool_calls_to_plaintext() -> None:
    """Tier 2: ``tool_calls=None`` (providers that omit the field) → PlainText too."""
    resp = SimpleNamespace(content="answer", tool_calls=None)
    interp = UniversalCategoryScheme().interpret(resp, tool_catalog={}, ops=_FakeOps())
    assert isinstance(interp, PlainText)
