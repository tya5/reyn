"""LLM wire-format repair: the assistant.tool_calls ↔ role=tool pairing invariant.

Every provider (OpenAI / Anthropic / …) requires that each assistant ``tool_calls`` id has a
corresponding ``role="tool"`` result, and each ``role="tool"`` result has a declaring assistant
``tool_call`` — an unpaired one is a 400 BadRequest. Compaction / history-decompose can split a
tool_call/result pair across a discarded middle segment, leaving a DANGLING tool_call (its result
elided) or an ORPHAN result (its call elided). ``repair_tool_call_pairing`` is a pure, full-list
repair applied at the final provider-call boundary so every path is covered by one guard.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The synthetic ``role="tool"`` content injected for a dangling tool_call (its real result was
# lost to a crash / compaction elision). An error result, so the model sees the tool did not
# complete rather than a fabricated success.
_INTERRUPTED_TOOL_RESULT = json.dumps(
    {"status": "error", "error": {"kind": "interrupted",
                                  "message": "Tool execution was interrupted."}},
)


def repair_tool_call_pairing(messages: list[dict]) -> list[dict]:
    """Repair the tool_call ↔ tool_result pairing on the FINAL assembled wire message list.

    Pure + full-list (NOT per-segment): the pairing is computed over the COMPLETE list, so an
    intact pair split only across a segment boundary (both halves present, e.g. the call in
    ``head`` and its result in ``tail``) is left untouched — this is what avoids the per-segment /
    adjacency-walk failure of wrongly "repairing" (duplicate-synthesizing) an intact pair.

    Two directions:

    - **Orphan result** — a ``role="tool"`` whose ``tool_call_id`` is declared by NO assistant
      ``tool_calls`` anywhere in the list (its declaring call was elided). DROP it: you cannot
      retroactively synthesize the assistant call, so dropping is the only valid repair (lossy,
      but the alternative is a 400).
    - **Dangling tool_call** — an assistant ``tool_calls`` id answered by NO ``role="tool"``
      anywhere in the list (its result was elided). Synthesize an interrupted error result
      immediately after that assistant message.

    Returns a new list; the input is not mutated (message dicts are reused by reference).
    """
    # First pass over the FULL list: what is declared (assistant calls) vs answered (tool results).
    declared: set[str] = set()
    answered: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tc_id = tc.get("id")
                if tc_id:
                    declared.add(tc_id)
        elif m.get("role") == "tool":
            tc_id = m.get("tool_call_id")
            if tc_id:
                answered.add(tc_id)

    out: list[dict] = []
    dropped: list[str] = []       # orphan tool_result ids removed
    synthesized: list[str] = []   # dangling tool_call ids answered with a synthetic result
    for m in messages:
        if m.get("role") == "tool":
            # Orphan drop: a result whose declaring assistant call is not in the list.
            if m.get("tool_call_id") not in declared:
                dropped.append(m.get("tool_call_id"))
                logger.warning(
                    "wire-repair dropped orphan tool_result %s (no matching tool_call in the "
                    "assembled history)", m.get("tool_call_id"),
                )
                continue
            out.append(m)
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            out.append(m)
            # Dangling synth: this assistant's ids with no result anywhere in the list, injected
            # immediately after (real results, if any, follow later in the list — all after the call).
            for tc in m["tool_calls"]:
                tc_id = tc.get("id")
                if tc_id and tc_id not in answered:
                    synthesized.append(tc_id)
                    logger.warning(
                        "wire-repair synthesized an interrupted result for dangling tool_call %s "
                        "(its real result was compacted away)", tc_id,
                    )
                    out.append({
                        "role": "tool", "tool_call_id": tc_id,
                        "content": _INTERRUPTED_TOOL_RESULT,
                    })
        else:
            out.append(m)
    if dropped or synthesized:
        # #2287 follow-up: the owner wants the count surfaced — a repair firing means a split pair
        # reached the wire (an EDGE once the group-aware-trim prevention lands; before that, it also
        # signals the elide split frequency).
        logger.warning(
            "wire-repair fired: synthesized %d dangling result(s), dropped %d orphan result(s)",
            len(synthesized), len(dropped),
        )
    return out
