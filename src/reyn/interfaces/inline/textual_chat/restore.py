"""Restore-on-restart projection: persisted ``ChatMessage`` log → display frames.

Phase 5 of the TUI-rebuild arc (#3273). On ``reyn chat`` restart the Textual
conversation pane should show the PREVIOUS conversation (Claude-Code ``--resume``
parity) rather than starting blank. :func:`project_restored_frames` is the pure
projection that turns the persisted conversation log — a ``list[ChatMessage]``
loaded from ``history.jsonl`` (``Session.load_history``) — into the
:class:`~reyn.runtime.outbox.OutboxMessage` display frames the app's retained
``FlowModel`` is fed from, so a restored turn renders through the EXACT SAME
presenter/gutter path a live frame does (only the timing differs: these are
appended once at ``on_mount`` BEFORE the live frame pump starts).

**Source of truth (D1, architect-ratified).** The restore source is the
persisted ``ChatMessage`` log (``history.jsonl``), NOT the P6 audit-event log:
audit-events do not carry assistant text / tool results and rotate. This
projection reads ONLY ``ChatMessage`` instances (see the accessor
:meth:`reyn.interfaces.repl.read_model.ChatReadModel.conversation_history`).

**Non-authoritative projection (recovery truncate-falsify gate is N/A).** The
``FlowModel`` this feeds is a *projection*; the authority is
``history.jsonl`` / the ``ChatMessage`` log itself. This reads that authoritative
store DIRECTLY — it is derived-at-read, NOT WAL-event-reconstructed state — so
the CLAUDE.md recovery-feature truncate-falsify gate (which guards
WAL-event-derived reconstruction state against WAL truncation below its source
events) does not apply here: there is no WAL-derived state to lose. If a future
change makes any restored surface WAL-reconstructed rather than read straight
from the ChatMessage log, that gate WOULD apply and this note must be revisited.

**Coalesced, resolved, never RUNNING.** A restored tool result is a SETTLED
outcome and is projected into the SAME coalesced shape the LIVE path settles a
completed tool into (``app.py``'s ``_coalesce_tool_result``): a single
``kind="tool_call_started"`` frame whose ``meta`` carries both the ORIGINAL
tool call (``tool`` / ``args``, correlated from the preceding assistant
message's ``tool_calls`` by ``tool_call_id``) and the result, stashed under
``RESULT_KIND_KEY`` / ``RESULT_META_KEY`` (:mod:`._meta_keys`). ``RESULT_KIND_KEY``
is ``"tool_call_completed"`` for a genuine success, or ``"tool_call_failed"``
when the persisted ``ChatMessage.meta`` carries the TYPED
``TOOL_STATUS_META_KEY == TOOL_STATUS_ERROR`` flag (:mod:`reyn.runtime.chat_message`;
#73) — a raised-exception failure never reaches here, since that already
produces a distinct ``ChatMessage``. That flag is stamped at PERSIST time by
``router_loop.py``'s tool-result assembly, the ONE place that already knows
the success/failure classification (a dispatch-envelope
``{"status":"error",...}`` or an MCP ``isError`` result) — this projection
reads it directly rather than re-deriving it from the rendered result STRING
(a display string is a renderer/formatting concern, not a stable data
contract: a legitimate success payload can itself start with the word
"Error"). A pre-#73 persisted history has no such flag at all — ABSENCE is
read as success/completed (never inferred as failure), so old history reads
exactly as it always has. So a restored failed tool tints coral exactly like
a live one. The presenter's existing ``tool_call_started`` branch renders
this as ONE block — ``⏺ tool(args)`` + ``⎿ result`` — with no presenter
change needed, so a restored tool turn is visually identical to a live
settled one (no orphan ``⎿``-only row). An uncorrelated result (no matching
``tool_call_id`` — e.g. truncated history) still projects to the same
coalesced shape with just the tool name as header (empty args) — it never
regresses to an orphan row. The caller (``_hydrate_from_history``) gives the
entry the SUCCESS/ERROR lifecycle state the live path's completion handler
would have: ``ERROR`` directly when ``RESULT_KIND_KEY=="tool_call_failed"``,
else derived from the SAME ``summarize_tool_result`` the presenter reads —
so gutter colour and body agree either way. The ``system`` / ``summary``
roles are Reyn-internal chrome (filtered at the LLM wire boundary), so both
are skipped.

**Answered intervention → ONE self-contained "Q→A" entry (#3299 P4).**
``InterventionHandler.announce`` (``runtime/services/intervention_handler.py``)
publishes an intervention's PROMPT only to the outbox — it never appends to
history — so before this, only the ANSWER half existed in ``history.jsonl``
(a ``role="user"`` entry stamped ``meta["intervention_id"]`` /
``meta["intervention_kind"]`` by ``deliver_answer_to``). There was and is no
separate "question" record to correlate the answer with, so — unlike the
tool call/result coalesce above, which really does have two records — this
does NOT correlate two entries: ``deliver_answer_to`` now folds the prompt
(+ optional detail, + the resolved answer-display text — needed because a
CLOSED-SET answer's own ``ChatMessage.content`` is an empty string; see
``INTERVENTION_ANSWER_META_KEY``'s docstring) onto that SAME answer record
(``INTERVENTION_PROMPT_META_KEY`` / ``INTERVENTION_DETAIL_META_KEY`` /
``INTERVENTION_ANSWER_META_KEY``, :mod:`reyn.runtime.chat_message`). One
history entry is already fully self-contained — this projection just reads
it, with no join/correlation step and therefore no GUESSED-key risk (the
#3287 / #3299 P2 defect class this arc hit twice before). It projects into
``kind="intervention_resolved"`` (#5057 axis B — the sibling kind a LIVE
entry is folded to once answered, :meth:`TextualChatApp._resolve_intervention` /
:meth:`TextualChatApp._handle_intervention_answer_event`), carrying
``meta["prompt"]`` / ``meta["detail"]`` for the head and
``meta["_answer_label"]`` for the "✓ answered: ..." line —
``ReynPresenter._present_intervention_pending`` renders both kinds through
the ONE function (:meth:`ReynPresenter.present` dispatches ``intervention``
and ``intervention_resolved`` to the same call), so a restored Q→A reads
through the EXACT SAME render path — and hits the EXACT SAME neutralization
boundary (the prompt / detail / a matched choice's label are all
model-derived / untrusted; that presenter call site is where they get
neutralized, never here) — a live resolved entry does. P5's out-of-order
answering is safe by construction:
each answer record carries its OWN question, so answering interventions in
any order restores each with its correct pairing. An intervention that was
NEVER answered leaves no history trace at all (``announce`` never appends)
and so projects to nothing — the specified behavior (pinned by a dedicated
test), not an accidental gap.

**Recovery implication (truncate-falsify gate N/A for the typed flag too).**
``TOOL_STATUS_META_KEY`` is written straight into the persisted ``ChatMessage``
at the SAME time ``content`` is (``router_loop.py``'s ``append_history_entry``
call) — it is authoritative-at-write, exactly like the rest of the message,
NOT WAL-event-reconstructed derived state. The CLAUDE.md recovery-feature
truncate-falsify gate therefore does not apply to it any more than it applies
to ``content`` or ``tool_call_id`` (see the "Non-authoritative projection"
note above) — there is no WAL-derived reconstruction step in between that
could silently drop the flag.

This module imports only :class:`~reyn.runtime.outbox.OutboxMessage`, the
plain ``str`` constants in :mod:`._meta_keys`, and the plain ``str`` /
``dataclass`` constants in :mod:`reyn.runtime.chat_message` (no ``textual`` /
``textual_flowview``): the projection is pure and unit-testable without
mounting the app.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from reyn.runtime.chat_message import (
    INTERVENTION_ANSWER_META_KEY,
    INTERVENTION_DETAIL_META_KEY,
    INTERVENTION_PROMPT_META_KEY,
    TOOL_ERROR_KIND_META_KEY,
    TOOL_ERROR_MESSAGE_META_KEY,
    TOOL_STATUS_ERROR,
    TOOL_STATUS_META_KEY,
)

from ._meta_keys import RESULT_KIND_KEY, RESULT_META_KEY

if TYPE_CHECKING:
    from reyn.runtime.chat_message import ChatMessage
    from reyn.runtime.outbox import OutboxMessage

# Roles with no operator-facing conversation surface: ``system`` is persisted
# lifecycle chrome and ``summary`` is the chat-compactor's internal carry —
# both filtered at the LLM wire boundary, so neither belongs in the restored
# scrollback.
_SKIP_ROLES = frozenset({"system", "summary"})

#: Marker stamped on every restored frame's ``meta`` so a consumer (or a test)
#: can tell a hydrated turn from a live one. Purely informational — the frame
#: renders identically either way.
RESTORED_META_KEY = "_restored"

#: Leading divider row announcing that what follows is the resumed prior
#: conversation (operator legibility — the Product-Think lens). Emitted once,
#: only when there is at least one restored turn. Public (#4387 Phase B ②):
#: ``app.py``'s ``_extend_older_frames_from_disk`` needs to recognise and
#: strip a SECOND divider a later re-projection call would otherwise add —
#: this module only ever inserts ONE per call, but a caller re-projecting
#: the growing log MULTIPLE times (disk-extension) must dedupe across calls
#: itself, which needs the exact text this module uses.
RESUME_DIVIDER = "⤺ resumed previous conversation"


def _failure_meta(m: "ChatMessage") -> "dict[str, object] | None":
    """Read the TYPED failure classification off a ``role="tool"`` message's
    persisted ``meta`` (#73, Option C) — never re-derive it by sniffing
    ``m.text``. ``router_loop.py``'s tool-result assembly stamps
    ``TOOL_STATUS_META_KEY=TOOL_STATUS_ERROR`` (+ ``error_message`` /
    ``error_kind``) at PERSIST time, from the SAME classification the LIVE
    path already has (a dispatch-envelope ``{"status":"error",...}`` or an
    MCP ``isError`` result) — this is a typed, discriminated field, not a
    string shape a display renderer happens to produce.

    Returns ``None`` when the flag is absent (a SUCCESS, or a pre-#73
    persisted history that never had this field — absence is read as
    success/completed, never inferred as failure)."""
    meta = m.meta or {}
    if meta.get(TOOL_STATUS_META_KEY) != TOOL_STATUS_ERROR:
        return None
    result_meta: "dict[str, object]" = {}
    error_kind = meta.get(TOOL_ERROR_KIND_META_KEY)
    if error_kind is not None:
        result_meta["error_kind"] = error_kind
    result_meta["error_message"] = meta.get(TOOL_ERROR_MESSAGE_META_KEY, "")
    return result_meta


def _correlated_calls(messages: "list[ChatMessage]") -> "dict[str, dict]":
    """Map ``tool_call_id -> {"name": ..., "args": ...}`` from assistant turns.

    Every ``role=="assistant"`` message's ``.tool_calls`` (a
    ``list[dict]`` of ``{"id": <tool_call_id>, "function": {"name": ...,
    "arguments": <json str>}}``) contributes one entry per call. ``arguments``
    is parsed defensively — a malformed / non-JSON string falls back to the
    raw string, never raises — so a corrupt history entry degrades to a
    plain-text arg summary rather than crashing the projection.
    """
    calls: "dict[str, dict]" = {}
    for m in messages:
        if m.role != "assistant" or not m.tool_calls:
            continue
        for call in m.tool_calls:
            call_id = call.get("id")
            if not call_id:
                continue
            fn = call.get("function") or {}
            name = fn.get("name")
            raw_args = fn.get("arguments")
            args: object = {}
            if isinstance(raw_args, str) and raw_args:
                try:
                    args = json.loads(raw_args)
                except (ValueError, TypeError):
                    args = raw_args
            elif raw_args is not None:
                args = raw_args
            calls[call_id] = {"name": name, "args": args}
    return calls


def project_restored_frames(
    messages: "list[ChatMessage]",
) -> "list[OutboxMessage]":
    """Project a persisted ``ChatMessage`` log into restore display frames.

    Oldest→newest, preserving conversation order. Mapping:

    - ``user`` (non-empty text)      → ``kind="user"``
    - ``user`` carrying
      ``meta[INTERVENTION_PROMPT_META_KEY]`` (#3299 P4) → a SINGLE
      ``kind="intervention"`` frame (the RESOLVED Q→A shape — see the
      dedicated docstring paragraph below), never a plain ``kind="user"``
      row. An intervention that was never answered has no history entry at
      all (``InterventionHandler.announce`` never appends to history) and so
      projects to nothing — the specified behavior, not an omission.
    - ``assistant`` (non-empty text) → ``kind="agent"``
    - ``tool`` (a tool RESULT)       → a SINGLE ``kind="tool_call_started"``
      frame carrying the coalesced call+result shape (see the module
      docstring): ``meta["tool"]`` / ``meta["args"]`` (correlated from the
      preceding assistant message's ``tool_calls`` by ``tool_call_id``,
      falling back to ``m.name`` / empty args when uncorrelated) plus
      ``RESULT_KIND_KEY="tool_call_completed"`` and
      ``RESULT_META_KEY={"result": m.text}`` for a SUCCESS (including a
      pre-#73 persisted history with no typed failure flag at all), or
      ``RESULT_KIND_KEY="tool_call_failed"`` and
      ``RESULT_META_KEY={"error_kind": ..., "error_message": ...}`` (or just
      ``{"error_message": ...}``) for a FAILURE — read directly off the
      persisted ``meta[TOOL_STATUS_META_KEY]`` typed flag (:func:`_failure_meta`;
      #73), never re-derived from ``m.text``.
    - ``system`` / ``summary``       → skipped (internal chrome), EXCEPT a
      ``system`` entry carrying ``meta["kind"] == "turn_cancelled"``
      (#3694) — a genuinely-cancelled turn's durable outcome, rescued by
      ``meta.kind`` rather than by role so every OTHER ``system`` entry
      (``state_change``, real SP chrome) stays skipped → ``kind="system"``,
      matching the live path's own outbox rendering.

    An assistant message carrying ONLY ``tool_calls`` (empty text) produces no
    ``agent`` frame of its own — the call header is delivered entirely through
    the coalesced ``tool`` projection above, matching how the live coalesce
    path folds a call into its result rather than emitting two rows.

    Every frame is stamped ``meta[RESTORED_META_KEY]=True``. Returns ``[]``
    for an empty log (no divider), so a first-ever run stays blank.
    """
    from reyn.runtime.outbox import (
        OutboxMessage,  # noqa: PLC0415 — keep module textual-free at import
    )

    calls_by_id = _correlated_calls(messages)
    frames: "list[OutboxMessage]" = []
    for m in messages:
        role = m.role
        meta = m.meta or {}
        if role in _SKIP_ROLES:
            # #3694: a cancelled-turn outcome reuses role="system" (the
            # same no-new-role precedent as notify_state_change), so the
            # blanket _SKIP_ROLES exclusion above would silently drop it
            # too — restoring the exact "why was there no reply" gap this
            # entry exists to close. Rescued by meta.kind, never by role:
            # every OTHER system entry (state_change, genuine SP chrome)
            # stays skipped.
            if role == "system" and meta.get("kind") == "turn_cancelled":
                frames.append(
                    OutboxMessage(
                        kind="system",
                        text=m.text,
                        meta={RESTORED_META_KEY: True, "chain_id": meta.get("chain_id")},
                    )
                )
            continue
        if role == "user":
            meta = m.meta or {}
            prompt = meta.get(INTERVENTION_PROMPT_META_KEY)
            if prompt:
                # #3299 P4: this history entry IS an answered intervention —
                # ``InterventionHandler.deliver_answer_to`` folded the prompt
                # (+ optional detail, + the resolved answer-display text) onto
                # this SAME record (see chat_message.py's
                # ``INTERVENTION_*_META_KEY`` docstrings — there is no
                # separate "question" record to correlate with). Project it
                # straight into the live ``kind="intervention"`` shape
                # (``ReynPresenter._present_intervention_pending``'s RESOLVED
                # branch — ``meta["prompt"]``/``meta["detail"]`` for the head,
                # ``meta["_answer_label"]`` for the "✓ answered: ..." line) so
                # a restored Q→A reads through the EXACT SAME render path —
                # and the EXACT SAME neutralization boundary — a live resolved
                # entry does. ALL THREE values are RAW here; neutralization
                # happens only at that shared render call site, never here (a
                # persisted record must stay the original, un-display-shaped
                # value).  An intervention that was NEVER answered has no
                # history trace at all (``announce`` never appends to
                # history) — nothing to project, which is the specified
                # behavior, not an omission.
                #
                # #5057 (axis B — architect's confirmed design): this
                # projection is ALWAYS an already-answered entry (an
                # unanswered intervention has no history trace at all, per
                # the docstring above), so it builds ``kind=
                # "intervention_resolved"`` — the sibling kind axis B adds
                # specifically so a resolved frame IS structurally distinct
                # from a genuinely pending one, rather than relying on
                # ``_ingest_frame`` (or anything downstream) to inspect
                # ``meta`` to tell them apart. ``intervention_resolved`` is
                # NOT in ``outbox._INTERVENTION_FAMILY_KINDS`` — axis A's
                # identity requirement does not apply here (a resolved
                # frame is never answered again, so it needs no
                # correlation anchor), which is what lets this branch stay
                # ONE shape instead of the id-present/id-absent split axis
                # A alone forced (#5047's own history: a record from
                # BEFORE ``deliver_answer_to`` started stamping
                # ``intervention_id`` carries none, and that must render
                # exactly the same as one that does).
                frames.append(
                    OutboxMessage(
                        kind="intervention_resolved",
                        text=str(prompt),
                        meta={
                            RESTORED_META_KEY: True,
                            "prompt": prompt,
                            "detail": meta.get(INTERVENTION_DETAIL_META_KEY),
                            "_answer_label": meta.get(INTERVENTION_ANSWER_META_KEY, ""),
                            "intervention_id": meta.get("intervention_id"),
                        },
                    )
                )
            else:
                text = m.text
                if text.strip():
                    frames.append(
                        OutboxMessage(kind="user", text=text, meta={RESTORED_META_KEY: True})
                    )
        elif role == "assistant":
            text = m.text
            if text.strip():
                frames.append(
                    OutboxMessage(kind="agent", text=text, meta={RESTORED_META_KEY: True})
                )
        elif role == "tool":
            call = calls_by_id.get(m.tool_call_id or "", {})
            tool_name = call.get("name") or m.name
            failure = _failure_meta(m)
            if failure is not None:
                result_kind = "tool_call_failed"
                result_meta: "dict[str, object]" = failure
            else:
                result_kind = "tool_call_completed"
                result_meta = {"result": m.text}
            frames.append(
                OutboxMessage(
                    kind="tool_call_started",
                    text=tool_name or "",
                    meta={
                        RESTORED_META_KEY: True,
                        "tool": tool_name,
                        "args": call.get("args") or {},
                        RESULT_KIND_KEY: result_kind,
                        RESULT_META_KEY: result_meta,
                    },
                )
            )
    if not frames:
        return frames
    divider = OutboxMessage(
        kind="system", text=RESUME_DIVIDER, meta={RESTORED_META_KEY: True}
    )
    return [divider, *frames]


# A sentinel that never equals a real ``chain_id`` string (nor ``None``) —
# forces a message with no ``chain_id`` at all to always start its OWN
# singleton group, on both sides of it, rather than accidentally merging
# with a neighbor that also happens to lack one.
_NO_CHAIN = object()


def _group_by_chain_id(history: "list[ChatMessage]") -> "list[list[ChatMessage]]":
    """Split *history* into contiguous runs sharing one ``meta["chain_id"]``
    (#5139 C) — router_loop.py stamps the SAME ``chain_id`` on every entry
    one turn produces (user input, an assistant reply's own tool calls, and
    each tool result), so a run is exactly one turn's worth of correlated
    entries. This is the ONLY unit :func:`page_restored_history` ever cuts
    a page boundary between — never inside one (see that function's own
    docstring for why)."""
    groups: "list[list[ChatMessage]]" = []
    prev_key: object = _NO_CHAIN
    for msg in history:
        key = msg.meta.get("chain_id") if isinstance(msg.meta, dict) else None
        if key is not None and key == prev_key:
            groups[-1].append(msg)
        else:
            groups.append([msg])
        prev_key = key if key is not None else _NO_CHAIN
    return groups


def page_restored_history(
    history: "list[ChatMessage]",
    *,
    before_root_id: "str | None" = None,
    limit: int,
) -> "tuple[list[OutboxMessage], bool, str | None]":
    """#5139 C: one server-side backlog PAGE — at most *limit* frames'
    worth of the newest remaining turns, cut only at a ``chain_id``
    (turn) boundary, never inside one.

    ``before_root_id`` (``None`` = the newest page, i.e. the initial
    connect/switch backlog): restricts the source to turns strictly OLDER
    than the turn whose ``chain_id`` equals this value — the caller's own
    previous page's oldest turn, so paging never re-sends what a prior
    page already covered and never skips a turn between two pages either.
    A ``before_root_id`` that names no known turn (a stale cursor — the
    turn it pointed at rotated out of ``history`` between calls) degrades
    to "nothing more" (``([], False, None)``) rather than silently
    re-serving the newest page, which would look like new history to a
    caller expecting strictly older content.

    Returns ``(frames, has_more, next_cursor)``: ``frames`` is this page,
    newest-last (the same order :func:`project_restored_frames` always
    produces); ``has_more`` is whether an OLDER turn still exists beyond
    this page (the caller's own "reached the true start" signal — ``False``
    here is a hard "no more", never "didn't check"); ``next_cursor`` is
    ``None`` exactly when ``has_more`` is ``False``, else the ``chain_id``
    to pass as THIS call's ``before_root_id`` on the next page.

    Bounded to walk only the groups this page and ``before_root_id``'s own
    slice actually need — never the full history when only a page's worth
    is being paged into a long-running conversation's tail."""
    groups = _group_by_chain_id(history)
    if before_root_id is not None:
        cut = None
        for i, g in enumerate(groups):
            key = g[0].meta.get("chain_id") if isinstance(g[0].meta, dict) else None
            if key == before_root_id:
                cut = i
                break
        if cut is None:
            return [], False, None
        groups = groups[:cut]
    selected: "list[ChatMessage]" = []
    boundary = len(groups)
    for i in range(len(groups) - 1, -1, -1):
        selected = groups[i] + selected
        boundary = i
        if len(project_restored_frames(selected)) >= limit:
            break
    has_more = boundary > 0
    next_cursor = None
    if has_more:
        root = groups[boundary][0]
        next_cursor = root.meta.get("chain_id") if isinstance(root.meta, dict) else None
    frames = project_restored_frames(selected)
    # ``project_restored_frames`` unconditionally prepends ONE resume-divider
    # row to any non-empty projection (its own docstring) — correct for the
    # newest page (``before_root_id is None``, the one a client shows first),
    # a would-be DUPLICATE on every OLDER page this same function projects
    # independently per call. Mirrors ``_extend_older_frames_from_disk``'s
    # identical dedup for local's own backward-extend.
    if before_root_id is not None and frames and frames[0].kind == "system" and frames[0].text == RESUME_DIVIDER:
        frames = frames[1:]
    return frames, has_more, next_cursor


__all__ = [
    "RESTORED_META_KEY",
    "RESUME_DIVIDER",
    "page_restored_history",
    "project_restored_frames",
]
