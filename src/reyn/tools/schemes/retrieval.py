"""RetrievalScheme — the (retrieval, tool_calls) cell, and the only user of
``RePresent`` (#1593 PR-4).

Instead of presenting the whole catalog, retrieval presents a **search tool** (+ the
prior-shape base); the LLM searches, the OS re-presents the matched actions as
callable tools, the LLM calls one. This is the namespace/retrieval paradigm for huge
tool sets (no full-catalog token cost; the search narrows before the call), and this
cell is the only place the ``interpret → RePresent`` loop-back is used — proving the
last unreached path of the PR-1 abstraction.

#3376 P3: ``retrieval`` now also has a ``content_fence`` cell
(``retrieval_content_fence.py``). It reaches the same paradigm WITHOUT
``RePresent`` — a system prompt cannot be swapped mid-turn the way a ``tools=``
payload can, so it dispatches the real ``search_actions`` wrapper from inside the
snippet instead. The two cells therefore share retrieval's ``sp_facts``
(``_retrieval_exposure``) but not an exposure builder; see that module for why
merging them would smuggle in a per-cell composer.

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

from reyn.prompt.retrieval import SEARCH_SP_NON_TERMINAL, SEARCH_SP_TERMINAL
from reyn.tools.encoders import encoder_for_transport
from reyn.tools.exposure import (
    Exposure,
    descriptors_from_entries,
    without_duplicate_names,
)
from reyn.tools.scheme import (
    ExecContext,
    Execute,
    ExecutionResult,
    Interpretation,
    PlainText,
    Presentation,
    RePresent,
    SchemeOps,
    register_scheme,
)
from reyn.tools.schemes._retrieval_exposure import retrieval_sp_facts
from reyn.tools.transport import Transport

_SEARCH_TOOL_NAME = "search_actions"


def _search_sp(*, terminal: bool) -> str:
    """The retrieval scheme's own tool-use instructions, carried to the encoder
    as an ``Exposure.sp_slot_overrides`` entry for the post-catalog slot.
    Retrieval runs with ``universal_wrappers_enabled=False`` — the OS's
    named-gate "## Action categories" block is off — so without this text the
    LLM would see the ``search_actions`` tool with no usage guidance. P7: the
    search paradigm is the scheme's concept, so its SP text lives here, not in
    the OS.

    ``terminal`` (= convergence reached, the search tool was dropped) flips the
    instruction from "search first" to "call one of the presented matches"."""
    return SEARCH_SP_TERMINAL if terminal else SEARCH_SP_NON_TERMINAL


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

    def _sp_facts(self, layer_ctx) -> "dict[str, object]":
        """This cell's ``sp_facts``, from the presentation's shared source.

        #3376 P3: the formula moved to ``_retrieval_exposure.retrieval_sp_facts``
        when the ``content_fence`` cell arrived — the facts are a property of the
        *presentation*, so two cells computing them separately could drift on a
        value neither test would notice (the ``content_fence`` encoder does not
        read ``sp_facts`` at all, so its copy would rot silently)."""
        return retrieval_sp_facts(layer_ctx)

    def _encode(self, exposure: Exposure, **presentation_fields) -> Presentation:
        """Hand one retrieval exposure to the ``tool_calls`` encoder.

        Every branch below goes through here, so no branch can grow its own
        answer to "how is this written down" — the seam's whole point."""
        encoder = encoder_for_transport(Transport.TOOL_CALLS)
        return Presentation(
            tools_channel=encoder.encode_tools(exposure),
            tool_use_sp=encoder.encode_tool_use_sp(exposure),
            **presentation_fields,
        )

    async def build_presentation(self, available, layer_ctx, ops: SchemeOps) -> Presentation:
        base = list(ops.base_tools(available, layer_ctx))
        refinement = layer_ctx.get("refinement")
        if not refinement:
            if not layer_ctx.get("search_visible", False):
                # #2895 fix (b): runtime auto-fallback. ``search_visible`` is the
                # same D14 gate (index + provider + model class + is_ready) that
                # decides whether search_actions is usable — when it's False the
                # embedding is unavailable (never configured, extras missing, or
                # index not ready yet). Presenting the search tool anyway would
                # let ``ops.search_actions`` return ``[]`` on the very first call
                # (index/provider is None — degrades silently), and this scheme's
                # own terminal rule below (empty match ⇒ terminal) would then drop
                # the search tool and strand the LLM on ``base`` only forever —
                # the exact silent dead-session #2895 reports. Config load
                # (``reyn.config.loader._validate_retrieval_scheme_embedding``)
                # rejects the common never-configured case up front; this is the
                # defense-in-depth leg for whatever slips past that (index build
                # failure, extras missing at Session-build time — env facts
                # config load can't see). Degrade like ``enumerate-all``: present
                # the full flat catalog directly (no search indirection needed,
                # so nothing is ever unreachable) and surface the SAME
                # enable-hint the graceful schemes inject via ``list_actions`` —
                # no duplicated hint text.
                from reyn.tools.universal_catalog import _HIDDEN_STATE_HINT

                catalog = await ops.catalog_entries()
                return self._encode(Exposure(
                    descriptors=descriptors_from_entries(
                        without_duplicate_names(base + catalog)
                    ),
                    sp_facts=self._sp_facts(layer_ctx),
                    sp_slot_overrides={"slot_post_catalog": _HIDDEN_STATE_HINT},
                ))
            # Initial presentation: the base + the search tool (no catalog flood).
            return self._encode(Exposure(
                # No dedup pass here, and that is a decision rather than an
                # omission: this branch adds ONE entry, ``search_actions``,
                # which the base tools do not carry, so no duplicate can arise.
                # It becomes wrong the day this branch composes a catalog
                # subset (#3428).
                descriptors=descriptors_from_entries(base + [_search_tool_schema()]),
                sp_facts=self._sp_facts(layer_ctx),
                sp_slot_overrides={"slot_post_catalog": _search_sp(terminal=False)},
            ))
        # Refined presentation: run the search (the async, dynamic-query I/O) and
        # present the matched catalog subset (∪ everything already presented).
        query = refinement.get("query", "")
        matched = await ops.search_actions(query) if query else []
        # ``presented`` = the OS loop-local accumulator of every candidate shown so
        # far this turn, threaded in by the OS RePresent arm (#1593 ratified seam:
        # the accumulator is an OS loop-local, NOT scheme self-state — schemes are
        # registered singletons, so per-turn self-state would collide across
        # concurrent turns). The SCHEME self-determines convergence from it.
        seen = set(layer_ctx.get("presented") or ())
        keep = set(matched) | seen
        catalog = await ops.catalog_entries()
        matched_tools = [
            t for t in catalog if t.get("function", {}).get("name") in keep
        ]
        tools = base + matched_tools
        # Convergence is the scheme's decision (the OS arm holds no convergence
        # logic): the search yielded nothing NEW beyond what is already presented
        # ⇒ terminal. A terminal present drops the search tool → the LLM can only
        # Execute (no re-search) → guarantees a non-RePresent exit. Bounded by
        # construction: ``seen`` grows monotonically over a finite action space, so
        # ``new`` empties in finite rounds.
        new = set(matched) - seen
        terminal = not new
        if not terminal:
            tools = tools + [_search_tool_schema()]
        return self._encode(
            Exposure(
                # The searched subset can name an action ``base`` already
                # carries (a catalog hit on ``read_file`` next to the base
                # ``read_file`` row). Deduplicating here does not touch
                # ``matched`` / ``candidates``, so the OS's convergence
                # accumulator sees the same candidate set it would have seen;
                # only the repeated row is withheld, and the capability stays
                # visible under the base entry.
                descriptors=descriptors_from_entries(
                    without_duplicate_names(tools)
                ),
                sp_facts=self._sp_facts(layer_ctx),
                sp_slot_overrides={"slot_post_catalog": _search_sp(terminal=terminal)},
            ),
            candidates=tuple(matched),
        )

    def interpret(self, llm_response, *, tool_catalog: dict, ops: SchemeOps) -> Interpretation:
        # Pure classifier (no I/O): NO tool calls → PlainText (the model answered
        # without searching/calling = done; the OS routes it to the terminal
        # text-reply path — #1593 loop-unify binds this for every scheme). A search
        # call → RePresent(query); the search I/O itself runs in build_presentation.
        # Any other call → Execute (reuse the shared resolution so the OS
        # exclude-gates pre-dispatch).
        calls = getattr(llm_response, "tool_calls", None) or []
        if not calls:
            return PlainText()
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
        # #4691 Phase B ①(remainder): forward the round's call_id — see
        # universal_category.py's own execute() for the full reasoning.
        results = await ops.dispatch(
            interp.actions, call_id=(getattr(exec_ctx, "extra", None) or {}).get("call_id"),
        )
        return ExecutionResult(tool_results=results)

    def format_feedback(self, result: ExecutionResult, ops: SchemeOps) -> list[dict]:
        # #1608: delegate to the OS substrate (now returns appendable messages);
        # retrieval's Execute feedback is identical to universal's.
        return ops.feedback(result)


__all__ = ["RetrievalScheme"]

# #1608: self-register on import (P7 — the OS resolve no longer names this class).
register_scheme(RetrievalScheme())
