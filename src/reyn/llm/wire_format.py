"""LLM wire-format repair: the assistant.tool_calls ↔ role=tool pairing + adjacency invariant.

Every provider (OpenAI / Anthropic / …) requires BOTH: (1) membership — each assistant
``tool_calls`` id has a ``role="tool"`` result and each result has a declaring call; (2) adjacency
— the results IMMEDIATELY FOLLOW the assistant ``tool_calls`` message, no intervening turn. An
unpaired OR a matched-but-non-adjacent pair is a 400. Compaction / history-decompose can split a
pair across a discarded (or bridge-summary-separated) middle, leaving a DANGLING call, an ORPHAN
result, or a matched-but-separated pair. ``repair_tool_call_pairing`` is a pure, full-list repair
applied at the final provider-call boundary (so every path is covered by one guard) that restores
both membership AND adjacency.
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
    """Repair the tool_call ↔ tool_result pairing AND ADJACENCY on the FINAL assembled wire list.

    Providers require not just that every assistant ``tool_calls`` id has a matching ``role="tool"``
    result (membership), but that the results IMMEDIATELY FOLLOW the assistant message with no
    intervening message (adjacency) — OpenAI: tool messages must follow the tool_calls message;
    Anthropic: the tool_result turn must be the one right after the tool_use turn. So a matched but
    non-adjacent pair (e.g. `assistant(tc) | bridge-summary | role=tool(tc)`, the compaction-elide
    split) still 400s ("role=tool with no matching preceding tool_calls"). Set-membership alone is
    NOT sufficient — the repair must RE-ADJACENCY.

    Pure + full-list. For each assistant with ``tool_calls``, its results are GATHERED and emitted
    IMMEDIATELY after it — the real result from wherever it sits in the list, or a synthesized
    interrupted result for a missing id. Every ``role="tool"`` is skipped at its original position
    (it is either gathered by its declaring assistant, or an ORPHAN whose declaring call is gone →
    dropped, since it cannot be synthesized). Net effect:

    - matched-but-separated pair → results re-adjacented right after the assistant (the primary
      compaction-elide bug);
    - dangling call (no result anywhere) → interrupted synth, adjacent;
    - orphan result (no declaring call) → dropped;
    - already-adjacent pairs → unchanged (bonus: duplicate results for one id de-duped).

    Returns a new list; the input is not mutated (message dicts are reused by reference).
    """
    # Pass 1 over the FULL list: declared ids, the first real result per id, and — per assistant —
    # which of its results were ALREADY adjacent (the immediately-following role=tool run), so we can
    # log only genuine re-adjacency moves.
    declared: set[str] = set()
    result_for: dict[str, dict] = {}          # tool_call_id -> its (first) real role=tool message
    adjacent_after: dict[int, set[str]] = {}  # id(assistant msg) -> result ids already adjacent to it
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if tc.get("id"):
                    declared.add(tc["id"])
            adj: set[str] = set()
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid:
                    adj.add(tid)
                    result_for.setdefault(tid, messages[j])
                j += 1
            adjacent_after[id(m)] = adj
            i = j
        else:
            if m.get("role") == "tool":
                tid = m.get("tool_call_id")
                if tid:
                    result_for.setdefault(tid, m)
            i += 1

    out: list[dict] = []
    synthesized: list[str] = []    # dangling ids answered with a synthetic interrupted result
    dropped: list[str] = []        # orphan result ids removed (declaring call gone)
    readjacented: list[str] = []   # matched results MOVED to be adjacent to their call
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            out.append(m)
            adj = adjacent_after.get(id(m), set())
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if not tid:
                    continue
                if tid in result_for:
                    out.append(result_for[tid])  # gather the real result adjacent to its call
                    if tid not in adj:
                        readjacented.append(tid)
                        logger.warning(
                            "wire-repair re-adjacented tool_result %s (its real result was "
                            "separated from its tool_call by an intervening message)", tid,
                        )
                else:
                    synthesized.append(tid)
                    logger.warning(
                        "wire-repair synthesized an interrupted result for dangling tool_call %s "
                        "(its real result was compacted away)", tid,
                    )
                    out.append({
                        "role": "tool", "tool_call_id": tid,
                        "content": _INTERRUPTED_TOOL_RESULT,
                    })
        elif m.get("role") == "tool":
            # Every role=tool is emitted at its declaring assistant (gathered above) — so skip it
            # here. If its id is declared by NO assistant, it is an ORPHAN → dropped.
            if m.get("tool_call_id") not in declared:
                dropped.append(m.get("tool_call_id"))
                logger.warning(
                    "wire-repair dropped orphan tool_result %s (no matching tool_call in the "
                    "assembled history)", m.get("tool_call_id"),
                )
            continue
        else:
            out.append(m)
    if synthesized or dropped or readjacented:
        # #2287 follow-up: the owner wants the count surfaced — a repair firing means a split pair
        # reached the wire (an EDGE once the group-aware-trim prevention lands; before that it also
        # signals the elide split frequency).
        logger.warning(
            "wire-repair fired: %d re-adjacented, %d synthesized (dangling), %d dropped (orphan)",
            len(readjacented), len(synthesized), len(dropped),
        )
    return out
