"""Pure, module-level chat-session helpers: chain-id minting, chat-summary
rendering, and shared+agent memory-index merging.

Each function here takes no ``self`` and has a light dependency footprint
(stdlib only) — safe for any caller (including ``reyn.runtime.session``
itself, and `reyn.runtime.services` modules that `session.py` in turn
imports) to import at its own module top level with no risk of a circular
import back into `session.py`.
"""
from __future__ import annotations

import uuid
from pathlib import Path


def new_chain_id() -> str:
    """Mint a fresh chain_id for a top-level user request. Each user submission
    starts a new chain; agent_request / agent_response payloads forward the
    chain_id they received without minting new ones."""
    # #3700: every chain_id minted anywhere in the runtime goes through this
    # function — if a call site mints its own (a bare uuid4, or a copy of this
    # rule), the same conversation can end up identified under two different
    # generation rules, and anything keyed on chain_id (chain lookup, audit
    # correlation) breaks the moment the two rules stop agreeing (e.g. hex vs
    # dashed uuid4 string form, as the now-deleted duplicate in
    # inter_agent_messaging.py did).
    return uuid.uuid4().hex


def _read_memory_index(path: Path) -> str:
    """Return MEMORY.md contents at `path` or empty string if absent."""
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _strip_index_header(content: str) -> str:
    """Drop a leading `# Memory Index` heading (with optional trailing blank
    lines) from a stored MEMORY.md so we don't render two headings when
    merging. Anything else is returned verbatim."""
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("# Memory Index"):
        i = 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        lines = lines[i:]
    return "\n".join(lines).strip()


def merge_memory_indexes(
    *, shared_path: Path, agent_path: Path, agent_name: str,
) -> dict:
    """Combine the shared and agent-scoped MEMORY.md files into a single
    `data.memory_index` payload (PR15).

    The router phase used to read `.reyn/memory/MEMORY.md` via a preprocessor
    `file/read` step; that step is removed because the agent-scoped path
    `.reyn/agents/<name>/memory/MEMORY.md` is dynamic and a static phase
    YAML cannot interpolate it. Session synthesizes the merged view
    here and stuffs it directly into the artifact.

    The two layers are kept separate in the output markdown — `(shared)` and
    `(agent: <name>)` — so the LLM can decide which slug path to use when
    writing new memory entries.
    """
    shared = _read_memory_index(shared_path).strip()
    agent = _read_memory_index(agent_path).strip()

    if not shared and not agent:
        return {"status": "not_found", "content": ""}

    parts: list[str] = []
    if shared:
        parts.append(f"# Memory Index (shared)\n\n{_strip_index_header(shared)}")
    else:
        parts.append("# Memory Index (shared)\n\n(empty)")
    parts.append(
        f"# Memory Index (agent: {agent_name})\n\n"
        f"{_strip_index_header(agent) if agent else '(empty)'}"
    )
    return {"status": "ok", "content": "\n\n".join(parts).strip() + "\n"}


def render_summary_for_storage(
    structured: dict,
    *,
    spill_reachability: "tuple[int, str] | None" = None,
) -> str:
    """Render a chat_summary structured dict to a quick-display text blob.

    Stored in ChatMessage.text so REPL traces and audit dumps don't need
    to re-render the structured form.

    #5720 (architect correction, issuecomment-5525827176): the prior
    docstring here said "the slicer prefers the structured form for LLM
    consumption — this is for human consumption only," read by two
    people in the SAME hour as opposite claims about which LLM. Both
    readings were partly right: for the COMPACTION LLM (``compact()``'s
    own call, which JSON-dumps the whole message dict as its input),
    the structured keys genuinely ride along — "for LLM consumption"
    is true there. For the MAIN LLM (``main_call`` — the one the owner
    meant by "何を覚えてるか聞いてみた"), this rendered TEXT is the ONE
    form that reaches the wire at all (``engine.py``'s own
    ``wrap_summary_as_message``, verbatim: "this rendered form is the
    ONE the wire actually sends") — never the structured dict, never
    ``meta``. "For human consumption only" was accurate for that
    reader, not for this one.

    ``spill_reachability``, when given, is a PRE-COMPUTED ``(count,
    directory)`` pair — this function stays pure (no store access) and
    never reads a decision out of *structured* for it: baking a
    deterministic count into ``structured`` would make it ride through
    the summarizing LLM on the NEXT fold and become that LLM's own
    judgment whether to keep it, echo it, or drop it — the exact
    failure mode ``artifacts_referenced`` (an LLM-output field) already
    has, measured directly in #5720 (a real fold discarding it). The
    caller is expected to derive a FRESH value from the store every
    time this function is called (a summary message is re-rendered on
    every turn, not once at fold time), never persist it into
    ``structured``, and pass ``None`` when there is nothing to report
    (zero spilled bodies is a normal answer, not an omitted section).
    """
    parts: list[str] = []
    # #1092 PR-F2a: a force-close handoff consolidation carries its (free-text)
    # body in the dedicated ``consolidation`` field — render it verbatim and
    # first (it IS the conversation's carried-forward essence). Absent on normal
    # compaction summaries → no output change for them (byte-identical).
    consolidation = (structured.get("consolidation") or "").strip()
    if consolidation:
        parts.append(consolidation)
    topic = (structured.get("topic_arc") or "").strip()
    if topic:
        parts.append(f"[topic] {topic}")
    for key in ("decisions", "pending", "session_user_facts", "artifacts_referenced"):
        items = structured.get(key) or []
        if not items:
            continue
        parts.append(f"[{key}]")
        parts.extend(f"  - {item}" for item in items)
    if spill_reachability is not None:
        # #5720: fixed-length section — 3 lines regardless of how many
        # bodies were spilled (charter Q1's own bound: wire cost must
        # not scale with N). Names the COUNT and a LOCATE instruction,
        # never every path — answers the owner's own two questions
        # ("is there a spill file at all" / "I didn't know how to get
        # it") without re-introducing the per-path enumeration this
        # section deliberately avoids.
        count, directory = spill_reachability
        parts.append("[spilled_content]")
        parts.append(
            f"  - {count} tool result(s) from this conversation were "
            f"offloaded to disk and are not shown above"
        )
        parts.append(
            f'  - list them: glob(path="{directory}/*"); '
            f'then read one: read_file(path="<listed path>")'
        )
    return "\n".join(parts)
