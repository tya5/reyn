"""Tier 2: RetrievalScheme — the RePresent-exemplar scheme (#1593 PR-4, scheme side).

These pin the scheme's 4 methods in isolation (a recording Fake SchemeOps — real
callables, no mocks): the search-tool-first initial presentation, the refined
presentation (run search → matched subset + candidates), the terminal presentation
(search tool dropped → forces Execute), and the **pure** interpret classifier
(search call → RePresent(query) with NO search I/O; other call → Execute). The OS
RePresent convergence loop (which consumes Presentation.candidates) is exercised
end-to-end when it lands in the dispatch RePresent arm (PR-3-sequenced).
"""
from __future__ import annotations

import pytest

from reyn.tools.scheme import ExecContext, Execute, ExecutionResult, RePresent
from reyn.tools.schemes.retrieval import _SEARCH_TOOL_NAME, RetrievalScheme


class _Resp:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


def _call(name, **args):
    import json
    return {"function": {"name": name, "arguments": json.dumps(args)}}


class _FakeOps:
    """Recording Fake SchemeOps — base/catalog/search return fixtures; the dispatch
    path delegates are identity-ish."""

    def __init__(self, matches=None, catalog=None):
        self._matches = matches or []
        self._catalog = catalog or []
        self.calls = []

    def base_tools(self, available, layer_ctx):
        return [{"type": "function", "function": {"name": "respond"}}]

    async def search_actions(self, query, *, top_k=10):
        self.calls.append(("search", query))
        return list(self._matches)

    async def catalog_entries(self):
        return list(self._catalog)

    def resolve(self, llm_response, tool_catalog):
        return [{"tc": tc, "name": tc["function"]["name"], "args": {}} for tc in llm_response.tool_calls]

    async def dispatch(self, actions):
        return [{"status": "ok", "for": a["name"]} for a in actions]

    def feedback(self, result):
        # #1608: ops.feedback now receives the enriched ExecutionResult; the Fake
        # echoes its tool_results so the delegation assertion stays meaningful.
        return result.tool_results


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


@pytest.mark.asyncio
async def test_initial_presentation_shows_search_tool(tmp_path) -> None:
    """Tier 2: with no refinement, retrieval presents base + the search tool (NOT
    the whole catalog) — the narrowing-before-call posture."""
    ops = _FakeOps()
    pres = await RetrievalScheme().build_presentation({}, {}, ops)
    names = [t["function"]["name"] for t in pres.llm_tools_payload]
    assert _SEARCH_TOOL_NAME in names and "respond" in names
    assert ops.calls == []                                # no search yet (no refinement)


@pytest.mark.asyncio
async def test_refined_presentation_runs_search_and_exposes_candidates(tmp_path) -> None:
    """Tier 2: given a refinement query, build_presentation runs the search and
    presents the matched catalog subset + the search tool, exposing the matches as
    Presentation.candidates (the OS convergence signal)."""
    ops = _FakeOps(matches=["file__write", "file__read"], catalog=[_tool("file__write"), _tool("file__read"), _tool("web__fetch")])
    pres = await RetrievalScheme().build_presentation({}, {"refinement": {"query": "edit a file"}}, ops)
    names = {t["function"]["name"] for t in pres.llm_tools_payload}
    assert {"file__write", "file__read"} <= names         # matched subset presented
    assert "web__fetch" not in names                      # unmatched NOT presented
    assert _SEARCH_TOOL_NAME in names                     # search stays (non-terminal)
    assert pres.candidates == ("file__write", "file__read")  # candidates for OS convergence
    assert ops.calls == [("search", "edit a file")]       # the dynamic query ran


@pytest.mark.asyncio
async def test_convergence_drops_search_tool(tmp_path) -> None:
    """Tier 2: the SCHEME self-determines terminal (#1593 ratified seam — the OS arm
    holds no convergence logic). When the search yields nothing NEW beyond what the
    OS already threaded in via ``presented`` (matched ⊆ presented ⇒ new == ∅), the
    presentation is terminal: it drops the search tool → the LLM can only Execute
    (no re-search) → the OS RePresent loop exits."""
    ops = _FakeOps(matches=["file__write"], catalog=[_tool("file__write")])
    pres = await RetrievalScheme().build_presentation(
        {}, {"refinement": {"query": "edit"}, "presented": ("file__write",)}, ops,
    )
    names = {t["function"]["name"] for t in pres.llm_tools_payload}
    assert "file__write" in names
    assert _SEARCH_TOOL_NAME not in names                 # converged → search dropped → must Execute


@pytest.mark.asyncio
async def test_new_matches_keep_search_tool(tmp_path) -> None:
    """Tier 2: the contrast — a search that yields a match NOT yet presented
    (new != ∅) is non-terminal: the search tool stays so the LLM may refine again.
    Pins that the terminal decision is the scheme's, driven by new-vs-presented."""
    ops = _FakeOps(matches=["file__read"], catalog=[_tool("file__read"), _tool("file__write")])
    pres = await RetrievalScheme().build_presentation(
        {}, {"refinement": {"query": "read"}, "presented": ("file__write",)}, ops,
    )
    names = {t["function"]["name"] for t in pres.llm_tools_payload}
    assert _SEARCH_TOOL_NAME in names                     # file__read is new → not converged → search stays


@pytest.mark.asyncio
async def test_initial_presentation_supplies_search_sp_fragment(tmp_path) -> None:
    """Tier 2: #1601 channel — retrieval runs with universal_wrappers_enabled=False
    (named-gate SP off), so it MUST supply its own search-tool instructions through
    Presentation.sp_fragment, else the LLM sees search_actions with no guidance."""
    ops = _FakeOps()
    pres = await RetrievalScheme().build_presentation({}, {}, ops)
    assert pres.sp_params["universal_wrappers_enabled"] is False   # named-gate SP off
    assert pres.sp_fragment                                        # scheme supplies its own
    assert _SEARCH_TOOL_NAME in pres.sp_fragment                   # tells the LLM to search first


@pytest.mark.asyncio
async def test_terminal_presentation_sp_fragment_reflects_dropped_search(tmp_path) -> None:
    """Tier 2: when the presentation converges to terminal (search tool dropped), the
    sp_fragment flips from "search first" to "call one of the presented matches"
    — the SP and the tools= stay consistent (no dangling search instruction)."""
    ops = _FakeOps(matches=["file__write"], catalog=[_tool("file__write")])
    pres = await RetrievalScheme().build_presentation(
        {}, {"refinement": {"query": "edit"}, "presented": ("file__write",)}, ops,
    )
    assert pres.sp_fragment                                        # still guided
    assert _SEARCH_TOOL_NAME not in pres.sp_fragment               # no "call search_actions" — it's gone
    assert "available" in pres.sp_fragment.lower()                 # "matching tools are now available"


def test_interpret_search_call_is_represent_pure() -> None:
    """Tier 2: a search call → RePresent(query), with NO search I/O in interpret
    (pure classifier — the search runs in build_presentation)."""
    ops = _FakeOps()
    interp = RetrievalScheme().interpret(
        _Resp([_call(_SEARCH_TOOL_NAME, query="edit a file")]), tool_catalog={}, ops=ops,
    )
    assert isinstance(interp, RePresent)
    assert interp.refinement == {"query": "edit a file"}
    assert ops.calls == []                                # interpret did NOT search (pure)


def test_interpret_tool_call_is_execute() -> None:
    """Tier 2: a non-search tool call → Execute (reuses the shared resolution so the
    OS exclude-gates pre-dispatch)."""
    interp = RetrievalScheme().interpret(
        _Resp([_call("file__write", path="x")]), tool_catalog={}, ops=_FakeOps(),
    )
    assert isinstance(interp, Execute)
    assert [a["name"] for a in interp.actions] == ["file__write"]


def test_interpret_no_tool_call_is_plaintext() -> None:
    """Tier 2: NO tool calls → PlainText — the model answered without searching
    (#1593 loop-unify binds PlainText as the terminal text-reply for every scheme).
    Without this, the loop-unified OS would route an empty-actions Execute through
    the tool path instead of the text reply."""
    from reyn.tools.scheme import PlainText
    interp = RetrievalScheme().interpret(_Resp([]), tool_catalog={}, ops=_FakeOps())
    assert isinstance(interp, PlainText)


@pytest.mark.asyncio
async def test_execute_and_feedback_delegate() -> None:
    """Tier 2: execute/format_feedback reuse the universal dispatch substrate."""
    ops = _FakeOps()
    scheme = RetrievalScheme()
    res = await scheme.execute(Execute(actions=[{"name": "file__write"}]), ExecContext(), ops)
    assert res.tool_results == [{"status": "ok", "for": "file__write"}]
    assert scheme.format_feedback(ExecutionResult(tool_results=res.tool_results), ops) == res.tool_results
