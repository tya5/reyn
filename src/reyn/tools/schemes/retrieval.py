"""RetrievalScheme — RAG-over-tools, the scheme that exercises ``RePresent`` (#1593 PR-4).

Instead of presenting the whole catalog, retrieval presents a **search tool** (+ the
prior-shape base); the LLM searches, the OS re-presents the matched actions as
callable tools, the LLM calls one. This is the namespace/retrieval paradigm for huge
tool sets (no full-catalog token cost; the search narrows before the call), and it is
the **only** scheme that uses the ``interpret → RePresent`` loop-back — proving the
last unreached path of the PR-1 abstraction.

Split (lead-approved design): ``interpret`` is a **pure classifier** (a ``search_actions``
call → ``RePresent({query})`` with NO search I/O; any other call → ``Execute``).
``build_presentation`` (async) owns the search I/O — given a refinement query it runs
``ops.search_actions`` (embeds the dynamic query → async) and presents the matched
catalog subset, exposing the matches as ``Presentation.candidates`` so the OS detects
convergence (`new = candidates - seen`; empty ⇒ terminal). The OS RePresent loop is
**bounded by construction** (monotonic ``seen`` on a finite action space + a terminal
present that drops the search tool → guaranteed ``Execute`` exit). ``execute`` /
``format_feedback`` reuse the universal dispatch substrate (``ops.dispatch`` /
``ops.feedback``) — retrieval differs only in presentation + the RePresent round.
"""
from __future__ import annotations

import json

from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    Presentation,
    RePresent,
    SchemeOps,
)

_SEARCH_TOOL_NAME = "search_actions"


def _search_sp(*, terminal: bool) -> str:
    """The retrieval scheme's own tool-use instructions, supplied through the
    ``Presentation.sp_fragment`` channel (#1601). Retrieval runs with
    ``universal_wrappers_enabled=False`` — the OS's named-gate "## Action
    categories" block is off — so without this fragment the LLM would see the
    ``search_actions`` tool with no usage guidance. P7: the search paradigm is
    the scheme's concept, so its SP text lives here, not in the OS.

    ``terminal`` (= convergence reached, the search tool was dropped) flips the
    instruction from "search first" to "call one of the presented matches"."""
    if terminal:
        return (
            "## Finding tools\n"
            "The tools matching your search are now available above. Call the "
            "one that fits the request directly."
        )
    return (
        "## Finding tools\n"
        "You are not shown the full tool catalog up front. To act, first call "
        "`search_actions(query=...)` with a natural-language description of what "
        "you need; the matching tools are then presented for you to call "
        "directly. Search before you act, and refine the query if the first "
        "matches do not fit."
    )


def _search_tool_schema() -> dict:
    """The presentable ``search_actions`` tool (name + query). The *call* is
    intercepted by ``interpret`` → ``RePresent`` (never dispatched), so this only
    needs to advertise the search affordance to the LLM."""
    return {
        "type": "function",
        "function": {
            "name": _SEARCH_TOOL_NAME,
            "description": (
                "Search for the tools you need by a natural-language query; the "
                "matching tools are then presented for you to call directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to do, in natural language.",
                    },
                },
                "required": ["query"],
            },
        },
    }


class RetrievalScheme:
    """RAG-over-tools tool-use scheme (#1593 PR-4) — the ``RePresent`` exemplar."""

    name = "retrieval"

    async def build_presentation(self, available, layer_ctx, ops: SchemeOps) -> Presentation:
        base = list(ops.base_tools(available, layer_ctx))
        sp_params = {
            "universal_wrappers_enabled": False,
            "search_actions_enabled": bool(layer_ctx.get("search_visible", False)),
        }
        refinement = layer_ctx.get("refinement")
        if not refinement:
            # Initial presentation: the base + the search tool (no catalog flood).
            return Presentation(
                llm_tools_payload=base + [_search_tool_schema()], sp_params=sp_params,
                sp_fragment=_search_sp(terminal=False),
            )
        # Refined presentation: run the search (the async, dynamic-query I/O) and
        # present the matched catalog subset (∪ everything already presented).
        query = refinement.get("query", "")
        matched = await ops.search_actions(query) if query else []
        seen = set(layer_ctx.get("presented") or ())
        keep = set(matched) | seen
        catalog = await ops.catalog_entries()
        matched_tools = [
            t for t in catalog if t.get("function", {}).get("name") in keep
        ]
        tools = base + matched_tools
        # Terminal present drops the search tool → the LLM must Execute (no re-search)
        # → guarantees a non-RePresent exit (the OS convergence step).
        terminal = bool(layer_ctx.get("terminal", False))
        if not terminal:
            tools = tools + [_search_tool_schema()]
        return Presentation(
            llm_tools_payload=tools, sp_params=sp_params, candidates=tuple(matched),
            sp_fragment=_search_sp(terminal=terminal),
        )

    def interpret(self, llm_response, *, tool_catalog: dict, ops: SchemeOps) -> Interpretation:
        # Pure classifier (no I/O): a search call → RePresent(query); the search I/O
        # itself runs in build_presentation. Any other call → Execute (reuse the
        # shared resolution so the OS exclude-gates pre-dispatch).
        calls = getattr(llm_response, "tool_calls", None) or []
        for tc in calls:
            if tc.get("function", {}).get("name") == _SEARCH_TOOL_NAME:
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except (json.JSONDecodeError, KeyError, TypeError):
                    args = {}
                return RePresent(refinement={"query": args.get("query", "")})
        return Execute(actions=ops.resolve(llm_response, tool_catalog))

    async def execute(self, interp: Interpretation, exec_ctx: ExecContext, ops: SchemeOps) -> ExecutionResult:
        assert isinstance(interp, Execute), "retrieval routes RePresent via the OS loop"
        results = await ops.dispatch(interp.actions)
        return ExecutionResult(tool_results=results)

    def format_feedback(self, result: ExecutionResult, ops: SchemeOps) -> list[dict]:
        return ops.feedback(result.tool_results)


__all__ = ["RetrievalScheme"]
