"""EventBackend — the audit-event WRITE-side abstraction (#4496 PR-2).

Lets an operator choose where `.reyn/events` audit-events are written:
local disk (default, current behavior), or discarded entirely. A
`network` backend is a deliberate, flagged scope cut — see below.

Deliberately THIN, per architect's #4496 design (issue comment, 2026-08-13):
a backend has exactly 2 responsibilities.

    1. receive an event and dispatch it (write / send / discard)
    2. name what it does NOT retain, so a consumer (`reyn events replay`,
       support-bundle, dogfood_trace) can tell "this backend doesn't keep
       that" apart from "nothing happened" (contract 2 — see `declare_gaps`)

The THIRD contract architect names — a monotonic `audit_seq` per emitter
so a subscriber can detect a gap — is NOT a backend responsibility. It is
already implemented in `EventLog.emit()` itself (#4496 PR-1): `audit_seq`
is stamped on every event regardless of backend, including `discard`
(measured: `emit()` does agent_id/run_id stamp -> emitter+audit_seq stamp
-> hand off for subscriber dispatch -> return; no file I/O happens inside
`emit()` at all — see `EventLog.emit`'s own docstring). Stamping always
precedes hand-off, in either dispatch branch #4966 introduced (queued to
a background consumer when a running loop exists, inline when it doesn't)
— #4966 changed WHEN dispatch happens relative to `emit()` returning, not
this stamp-before-dispatch ordering. A backend that skipped seq under its
own logic would be reimplementing (and could diverge from) a guarantee
`emit()` already provides for free — so backends never touch it.

## Not a subscriber (the structural guarantee this module exists for)

`EventLog.emit()` calls `self._backend.write(event)` directly — wrapped in
try/except — BEFORE handing the event off for subscriber dispatch (#4496
PR-2). This is deliberate, not incidental:

    - the subscriber dispatch loop (`for sub in self._subscribers:
      sub(event)`) HAS per-subscriber try/except (#4961 A — this used to
      be a real gap: a raising subscriber aborted the loop and every
      LATER subscriber in the list was silently skipped, `events.py`,
      measured). #4966 split this single loop into two call sites that
      must stay in sync — `_dispatch_inline` (no running loop, runs
      synchronously inside `emit()`) and `_dispatch_consumer` (running
      loop present, runs later on the background consumer task) — both
      preserve the same per-subscriber isolation, just at different call
      sites and different times relative to `emit()` returning. That
      isolation isolates one subscriber's failure from the NEXT ones, but
      it does NOT make position in `self._subscribers` a safe place for
      the backend: inserting a backend as JUST ANOTHER subscriber would
      still make it position-dependent (registered early enough to run
      before whatever fills the list — including a future backend that
      changes registration order) instead of unconditional — the exact
      "discard silences the UI" failure mode the owner's #4496 ruling
      forbids ("emit は抽象に対して必ず行う、Backend 側で破棄するだけ")
      demands the backend write happen NO MATTER what's registered or in
      what order, not merely "isolated from subscribers that happen to
      raise".
    - calling the backend FIRST, outside the subscriber loop, with its own
      try/except, gets both halves of prohibition ③ (backend failure must
      not reach subscribers, and vice versa) from ORDERING alone: the
      backend has already run by the time any subscriber could raise, and
      a backend exception is caught right where it's raised, before the
      subscriber loop even starts.

## `network` backend + `on_failure` (#4496, owner ruling 2026-09-09)

The owner's #4496 write-up left one point open: what a `network` backend
does when the network send fails — discard-and-let-the-seq-gap-show-it /
spool-locally / halt-the-run (three options the issue itself names). The
owner's own recorded words settle it (issue body, 2026-08-13): "呼び戻し
がないと reyn 動けないわけじゃない" ("reyn doesn't need a callback to
function") — so **discard-and-continue is the default** (`on_failure:
discard`), never "halt the run" (explicitly rejected — stopping a run over
an audit-delivery failure is excessive; a capacity-limited spool already
covers the rare case that genuinely needs to not lose the record). `spool`
is an explicit opt-in: an operator who chose `network` specifically to NOT
keep events locally must not have that silently reversed on a transient
failure — see `NetworkEventBackend`'s own docstring for the exact mechanics
and `AuditEventsConfig.on_failure`'s docstring for the config-side framing.

**Wire protocol — NOT specified by the issue thread.** No existing
general-purpose "send an arbitrary audit-event over the network" primitive
exists in this repo (`observability/otel_exporter.py` maps a FIXED set of
event kinds to OTel's own GenAI span/metric/log vocabulary — a different,
narrower contract, not a transport this backend can reuse for an arbitrary
event). Chose the minimal, conservative shape: one HTTP POST per event, to
an operator-configured `endpoint`, with `event.model_dump(mode="json")` as
the JSON body — documented here plainly as a choice, not a derivation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Callable, Literal, Protocol, runtime_checkable

from reyn.schemas.models import Event

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

#: #4960 — architect ruling C: ``agent_delta`` (one audit-event per
#: streamed content chunk) is NOT durably written per-fragment. Live
#: subscriber dispatch (TUI / AG-UI) is completely unaffected — see
#: ``EventLog.emit()``'s own ordering (backend write, then subscriber
#: loop) — this constant only throttles what ``LocalEventBackend``
#: persists to disk.
#:
#: Measured (#4960, 2000-delta / 60KB streamed reply, the SAME real
#: transport/router/TUI path the #3570 repaint-budget precedent uses):
#: unthrottled, ``agent_delta`` writes are 99.4% of the audit file's
#: total bytes for that run (550,917 / 554,112 bytes), at a fixed
#: ~275 bytes/fragment and ~28-40us of backend-write time per fragment.
#: 100 caps that to ~1% of the unthrottled footprint (~20 durable
#: records instead of 2000 for that run) while keeping the loss-of-
#: recency bound small (at most 99 fragments, ~27KB of text, between
#: any two durable checkpoints for a chain still actively streaming).
_DEFAULT_AGENT_DELTA_COALESCE_FRAGMENTS = 100

#: Measured burst rate through a proxy (#3570's own comment): up to
#: ~1000 deltas/s, so at the fragment default above a burst is governed
#: by the FRAGMENT count (~100ms between durable writes), never by this
#: timer. This interval exists for the cases the fragment count cannot
#: reach on its own:
#:
#:   ① (primary) a process-level death (SIGKILL / OOM-kill / host
#:      crash) that the terminal flush below CANNOT catch — a Python
#:      ``finally`` block never runs when the process itself is killed,
#:      so this interval is the ONLY durable-record guarantee left for a
#:      chain that dies mid-stream with fewer than the fragment count
#:      accumulated (architect's #4960 ruling: this is the scenario a
#:      short interruption is MOST likely to hit, and losing it defeats
#:      the whole reason C was chosen over B — cost accountability for a
#:      call whose usage record never lands).
#:   ② (secondary) an idle-but-long-lived stream (few deltas over a long
#:      wall-clock span) still leaves periodic evidence of progress.
#:
#: 2 seconds is far above the measured per-fragment cost (tens of
#: microseconds) so it never fires under normal bursty streaming, and
#: short enough that an operator inspecting mid-crash state is not
#: staring at a multi-minute-old durable record.
_DEFAULT_AGENT_DELTA_COALESCE_INTERVAL_MS = 2_000

#: #4666 item ③ (owner ruling: user input is opt-in, its OWN knob —
#: separate from ① ``agent_delta_include_text`` and the still-in-design
#: ② "completed conversation" knob). The content-bearing field on every
#: emit site where a user's OWN typed/chosen text reaches an audit-event,
#: per an AST census across the whole tree (lead-coder, #4666, confirmed
#: 6 — an earlier pass found 3 and undercounted):
#:
#:   user_submitted                 -> text            (session.py)
#:   user_message_received          -> text            (session.py)
#:   intervention_answer_submitted  -> text             (intervention_handler.py)
#:   user_answered_intervention     -> answer_text      (intervention_handler.py)
#:   user_intervention_received     -> answer           (ask_user.py)
#:   router_retry_exhausted         -> user_message      (budget_gateway.py, truncated to 200 chars at the emit site already)
#:
#: ⚠️ Known gap, deliberately NOT closed by this knob (architect + lead-
#: coder, #4666): ``ask_user``'s question/answer ALSO reach the audit log
#: unconditionally via ``tool_called.args["question"]`` /
#: ``tool_returned.result["answer"]`` (``dispatch_tool``, a different emit
#: path this knob does not touch — those carry a tool's own payload, not
#: one of the 6 kinds above). Closing that needs a per-tool "this field is
#: conversation content" declaration the dispatcher can consult (architect
#: ruling in progress) — turning this knob ON/OFF does not affect that
#: path either way. Do not read the 6-kind list above as exhaustive
#: coverage of "user input reaches the audit log".
_USER_INPUT_CONTENT_FIELDS: dict[str, str] = {
    "user_submitted": "text",
    "user_message_received": "text",
    "intervention_answer_submitted": "text",
    "user_answered_intervention": "answer_text",
    "user_intervention_received": "answer",
    "router_retry_exhausted": "user_message",
}


@runtime_checkable
class EventBackend(Protocol):
    """The write-side surface `EventLog.emit()` calls into (#4496 PR-2)."""

    def write(self, event: Event) -> None:
        """Persist / send / discard *event*.

        May raise — the caller (`EventLog.emit`) catches and logs; a
        backend must never assume its own exception reaches anything
        downstream (subscribers included)."""
        ...

    def declare_gaps(self) -> list[str]:
        """Human-readable statements of what this backend does NOT retain.

        Empty list = no gaps (this backend keeps everything a consumer
        might expect). A consumer reading `[]` sees "nothing missing",
        never confuses it with an empty EVENT list (contract 2: "empty"
        and "unsupported" must be told apart)."""
        ...


class LocalEventBackend:
    """Writes to local disk via an `EventStore` (the default, current
    behavior — #4496 PR-2 wraps the EXISTING EventStore.write, no I/O
    change), EXCEPT ``agent_delta`` (#4960, architect ruling C): coalesced
    to one durable record per ``agent_delta_coalesce_fragments`` fragments
    OR ``agent_delta_coalesce_interval_ms`` milliseconds, whichever comes
    first, per streaming chain (``event.data["chain_id"]``) — see the two
    module-level constants above for the measured rationale, and
    :meth:`flush_pending_deltas` for the terminal-flush half of the
    guarantee (the 3 mechanisms — fragment count, interval, terminal
    flush — cover each other's gap; see each one's own docstring for
    which failure mode it alone covers).

    Live subscriber dispatch is completely unaffected: this coalescing
    lives entirely inside ``write()``, called by ``EventLog.emit()``
    BEFORE the (unthrottled) subscriber loop — every raw fragment still
    reaches the TUI/AG-UI exactly as before #4960.

    #4666①: the coalesced durable record's ``text`` field (the streamed
    reply content itself) is ALSO opt-in — off by default
    (``agent_delta_include_text``), its OWN knob, deliberately not tied
    to the coalescing above (owner ruling: each opt-in gets its own
    config, never a single toggle covering both).

    #4666②: same drop-the-free-text-field-only shape, applied to TWO more
    kinds that are NOT coalesced (one record per occurrence, unlike
    ``agent_delta``'s per-fragment volume — no throttling need):
    ``agent_response_committed`` (``Session._put_outbox``'s own emit,
    filtered on ``msg.kind == "agent"`` — the completed model→user text)
    drops ``text``; ``user_intervention_requested`` (``ask_user.py``, the
    model's own question) drops ``question``/``suggestions``/``options``.
    Both gated by the SAME ``completed_response_include_text`` knob
    because both carry the SAME content type — text the MODEL directed
    at the user (owner ruling: one knob per content type). The
    `ask_user` ANSWER (``user_intervention_received``, item ③'s own
    kind) is a DIFFERENT content type — text the USER directed at the
    model — so it is necessarily gated by item ③'s own, separate knob
    instead. (An earlier framing here read this as "② and ③ must share
    one knob, since question+answer are two ends of one exchange" —
    corrected: the owner's rule is one knob per content TYPE, and
    question/answer are two different types by construction, so they
    were always going to land on two different knobs.)
    ``chain_id``/``intervention_id`` and every other field are kept
    either way, so "a response was committed" / "a question was asked"
    remains provable from the durable record alone with the flag off —
    same reasoning as ``agent_delta``'s own drop above.

    #4666 item ③: a SEPARATE opt-in (``user_input_include_text``, also
    off by default) covers 6 kinds carrying a user's own typed/chosen
    text (``user_submitted``, ``user_message_received``,
    ``intervention_answer_submitted``, ``user_answered_intervention``,
    ``user_intervention_received``, ``router_retry_exhausted`` — see
    ``_USER_INPUT_CONTENT_FIELDS`` for the exact field dropped per kind).
    No coalescing here — every event of these kinds is still written
    individually, just with its one content field redacted when off. No
    overlap with ②'s two kinds above — a kind belongs to at most one of
    these opt-ins.

    All OTHER event kinds: unchanged, no gaps — replay / support-bundle /
    dogfood_trace work normally against this backend's output for them."""

    def __init__(
        self,
        store: "EventStoreLike",
        *,
        agent_delta_coalesce_fragments: int = _DEFAULT_AGENT_DELTA_COALESCE_FRAGMENTS,
        agent_delta_coalesce_interval_ms: int = _DEFAULT_AGENT_DELTA_COALESCE_INTERVAL_MS,
        agent_delta_include_text: bool = False,
        completed_response_include_text: bool = False,
        user_input_include_text: bool = False,
        provider_body_include_text: bool = False,
        provider_body_max_chars: int = 4000,
        tool_result_max_chars: int = 4000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._agent_delta_coalesce_fragments = agent_delta_coalesce_fragments
        self._agent_delta_coalesce_interval_ms = agent_delta_coalesce_interval_ms
        # #4666 (owner ruling, opt-in, its OWN knob — separate from any
        # future "completed conversation" opt-in, deliberately not unified
        # under one toggle): whether the coalesced durable record keeps
        # `text` (the streamed reply content itself). Default False — the
        # OTel GenAI convention #4666 follows ("every attribute that can
        # hold prompt/output content is opt-in, default metadata-only").
        # Live TUI/AG-UI delivery is UNAFFECTED either way — this backend
        # only decides what reaches DISK; every raw fragment (`text`
        # included) still dispatches to subscribers regardless of this
        # flag (see `write()`'s own module-level ordering: backend.write
        # runs, then EventLog's subscriber loop, both from the SAME raw
        # `event` this flag never touches).
        self._agent_delta_include_text = agent_delta_include_text
        # #4666②: same shape as `_agent_delta_include_text` immediately
        # above, gating `agent_response_committed`'s `text` and
        # `user_intervention_requested`'s `question`/`suggestions`/
        # `options` — see this class's own docstring for the full
        # rationale and the config field's own docstring
        # (`AuditEventsConfig.completed_response_include_text`) for the
        # owner ruling this mirrors.
        self._completed_response_include_text = completed_response_include_text
        # #4666 item ③ (owner ruling, its OWN knob — separate from ① AND
        # ② above): whether the durable record for any of the 6 user-input
        # kinds in `_USER_INPUT_CONTENT_FIELDS` keeps that kind's content
        # field. Default False, same OTel-convention rationale as ①. Live
        # subscriber delivery is UNAFFECTED — this flag is consulted only
        # inside `write()`, after `EventLog.emit()` has already handed the
        # raw event off for subscriber dispatch.
        self._user_input_include_text = user_input_include_text
        # #4975 (architect ruling, issuecomment-5384508845): a provider's
        # own 4xx/5xx error body can quote back ANY of the 3 content
        # classes above — reyn does not choose that body's shape, so it
        # cannot tell which of the 3 a given quote is. Showing
        # `provider_body`/`provider_response` (`llm_request_error`)
        # therefore requires ALL 3 above (a lattice-meet, the narrowest
        # participant wins) AND this flag itself — its own opt-in on top
        # of the meet, since "opted into all 3" is not the same claim as
        # "opted into an externally-shaped blob that might contain any of
        # them" (see `_persist_llm_request_error`'s own docstring).
        self._provider_body_include_text = provider_body_include_text
        # #4975: the operator-adjustable cap on the SHOWN provider_body/
        # provider_response text (CLAUDE.md: never a baseless embedded
        # constant) — reyn cannot bound a provider's own error-body size.
        self._provider_body_max_chars = provider_body_max_chars
        # #5891 (architect ruling, this issue): a tool's own return value
        # is arbitrary — reyn does not bound its size (measured on
        # reyn-self: two `exec` `tool_returned.data["result"]` values were
        # ~2.79MB and ~5.94MB, written straight into one audit-event
        # line). Its own dedicated cap, NOT `provider_body_max_chars`
        # above — that field caps a provider's own error-body text under
        # a lattice-meet content-visibility gate (#4975); this one is an
        # unconditional SIZE bound on every `tool_returned.result`,
        # applied regardless of any #4666 content opt-in (a tool result
        # is not one of the 3 content classes those knobs gate — it is
        # the tool's own structured output, already passed through
        # `_redact_content_fields` upstream in `dispatch_tool` before it
        # ever reaches this backend). See `_persist_tool_returned`'s own
        # docstring for the full shape (CLAUDE.md: never a baseless
        # embedded constant — this is an operator-adjustable knob,
        # `AuditEventsConfig.tool_result_max_chars`, same 4000-char
        # default as `provider_body_max_chars` for the same reason: a
        # single-record excerpt long enough to be useful for triage
        # without being the multi-MB body itself).
        self._tool_result_max_chars = tool_result_max_chars
        # Test seam (mirrors this repo's existing ``clock: Callable[[],
        # float]`` idiom, e.g. TextualChatApp) — production always passes
        # the default ``time.monotonic``; a test can inject a fake to
        # exercise the interval branch without a real sleep (CLAUDE.md:
        # "a test writes no duration ... the clock is an INPUT you
        # supply, never a sleep you wait out").
        self._clock = clock
        # Per-chain_id coalescing state (#4960). "" buckets any
        # agent_delta that, unexpectedly, carries no chain_id — never
        # silently dropped, just coalesced under one shared key instead
        # of per-chain isolation.
        self._delta_pending_count: dict[str, int] = {}
        self._delta_last_persisted_at: dict[str, float] = {}
        self._delta_last_event: dict[str, Event] = {}

    #4666②: the free-text field name(s) to drop per kind while
    # `completed_response_include_text` is off — no coalescing (each kind
    # here is one record per occurrence, not per-fragment volume like
    # `agent_delta`), so `write()` drops and writes straight through
    # rather than routing through the coalescing state below.
    _COMPLETED_RESPONSE_TEXT_FIELDS: "dict[str, tuple[str, ...]]" = {
        "agent_response_committed": ("text",),
        "user_intervention_requested": ("question", "suggestions", "options"),
    }

    #: #4975: the provider-controlled fields on ``llm_request_error`` this
    #: gate applies to — see :meth:`_persist_llm_request_error`'s own
    #: docstring for why this event kind needs a MEET, not a single flag
    #: like every other kind in :attr:`_COMPLETED_RESPONSE_TEXT_FIELDS`.
    _PROVIDER_BODY_FIELDS: "tuple[str, ...]" = ("provider_body", "provider_response")

    def write(self, event: Event) -> None:
        if event.type == "llm_request_error":
            self._persist_llm_request_error(event)
            return
        if event.type == "tool_returned":
            self._persist_tool_returned(event)
            return
        if event.type in self._COMPLETED_RESPONSE_TEXT_FIELDS:
            if self._completed_response_include_text:
                self._store.write(event)
            else:
                fields = self._COMPLETED_RESPONSE_TEXT_FIELDS[event.type]
                data = {k: v for k, v in event.data.items() if k not in fields}
                self._store.write(event.model_copy(update={"data": data}))
            return
        if event.type != "agent_delta":
            content_field = _USER_INPUT_CONTENT_FIELDS.get(event.type)
            if content_field is not None and not self._user_input_include_text:
                self._persist_redacted_user_input(event, content_field)
                return
            self._store.write(event)
            return
        chain_id = event.data.get("chain_id")
        key = chain_id if isinstance(chain_id, str) else ""
        now = self._clock()
        if key not in self._delta_last_persisted_at:
            self._delta_last_persisted_at[key] = now
        self._delta_last_event[key] = event
        # #5261: an incoming ``agent_delta`` may already stand in for MORE
        # than one raw provider chunk — the source-side merge that issue
        # introduced tags a merged event with its own ``raw_chunk_count``.
        # Sum that (not a bare +1 per EVENT) so ``coalesced_fragment_count``
        # below keeps meaning "how many raw provider chunks", not "how many
        # agent_delta events arrived" — those stopped being the same number
        # the moment source-side merging could produce an event standing in
        # for more than one chunk. An event with no ``raw_chunk_count``
        # (pre-#5261 callers, or #5261's own unmerged single-chunk case)
        # contributes exactly 1 — an unmerged event IS one raw chunk, by
        # definition, so this is not a guessed default.
        _raw_count = event.data.get("raw_chunk_count")
        pending = self._delta_pending_count.get(key, 0) + (
            _raw_count if isinstance(_raw_count, int) and _raw_count > 0 else 1
        )
        elapsed_ms = (now - self._delta_last_persisted_at[key]) * 1000
        if (
            pending >= self._agent_delta_coalesce_fragments
            or elapsed_ms >= self._agent_delta_coalesce_interval_ms
        ):
            self._persist_coalesced_delta(event, coalesced_fragment_count=pending)
            self._delta_pending_count[key] = 0
            self._delta_last_persisted_at[key] = now
        else:
            self._delta_pending_count[key] = pending

    def flush_pending_deltas(self, chain_id: str) -> None:
        """#4960 — the terminal-flush mechanism: called once a streaming
        chain ends (success, exception, or cancellation — see
        ``EventLog.flush_agent_delta``'s own call site in
        ``RouterLoop.run()``'s ``finally``). Persists one final coalesced
        record for any fragments accumulated since the last durable write,
        so a SHORT interruption (fewer fragments than the coalesce count,
        less wall-clock than the coalesce interval — the interruption
        shape most likely to occur, per architect's #4960 ruling) still
        leaves durable evidence that partial output existed.

        Does NOT cover a process-level death (SIGKILL / OOM-kill / host
        crash) — a Python ``finally`` never runs in that case; the
        coalesce-interval mechanism in :meth:`write` is the ONLY durable-
        record guarantee for THAT failure mode. The two mechanisms
        deliberately cover different, non-overlapping failure classes."""
        key = chain_id if isinstance(chain_id, str) else ""
        pending = self._delta_pending_count.get(key, 0)
        last_event = self._delta_last_event.get(key)
        if pending > 0 and last_event is not None:
            self._persist_coalesced_delta(last_event, coalesced_fragment_count=pending)
        # Drop this chain's state — a chain_id is not reused after its
        # stream ends, so keeping it would grow these dicts unbounded
        # across a long-lived process handling many turns.
        self._delta_pending_count.pop(key, None)
        self._delta_last_persisted_at.pop(key, None)
        self._delta_last_event.pop(key, None)

    def _persist_coalesced_delta(self, event: Event, *, coalesced_fragment_count: int) -> None:
        """Write ONE durable record standing in for *coalesced_fragment_count*
        raw ``agent_delta`` fragments — the most recently arrived fragment's
        own event, with the coalesced count added to ``data`` (a new field,
        not a mutation of *event* itself — *event* is the SAME object the
        (already-completed) subscriber dispatch loop may still be holding a
        reference to, so a new object is written, never the original
        mutated in place).

        *coalesced_fragment_count* (#5261: this caller's own ``pending``,
        summed from each incoming event's ``raw_chunk_count`` — see
        :meth:`write`'s own comment) is provider-raw-chunk-accurate even
        when the source itself already merges: it always means "how many
        chunks the provider actually sent", never "how many
        ``agent_delta`` events arrived here" — those stopped being
        interchangeable the moment source-side merging could make one
        event stand in for more than one chunk.

        #4666: when ``agent_delta_include_text`` is off (the default),
        ``text`` (the streamed reply content itself) is dropped from the
        durable record — everything else (``chain_id``/``round_index``/
        ``coalesced_fragment_count``/``audit_seq``) is kept. #4960's own
        reason for existing survives this drop unchanged: "a partial
        reply of N fragments existed" is provable from those fields alone,
        with no dependency on the reply's own content — dropping the
        WHOLE event here (rather than just this one field) would reopen
        the exact gap #4960 closed (cost accountability for a call whose
        usage record never lands)."""
        data = {**event.data, "coalesced_fragment_count": coalesced_fragment_count}
        if not self._agent_delta_include_text:
            data.pop("text", None)
        self._store.write(event.model_copy(update={"data": data}))

    def _persist_redacted_user_input(self, event: Event, content_field: str) -> None:
        """#4666 item ③: write *event* with *content_field* dropped from
        ``data`` — a NEW object (``model_copy``), never a mutation of
        *event* itself, for the same reason :meth:`_persist_coalesced_delta`
        never mutates its own argument: *event* is the same object the
        (already-completed) subscriber dispatch loop may still hold a
        reference to. Every other field on the kind (``chain_id``/
        ``intervention_id``/``msg_id``/``seq``/etc.) survives untouched —
        only the one content-bearing field named in
        ``_USER_INPUT_CONTENT_FIELDS`` for this kind is dropped."""
        data = {**event.data}
        data.pop(content_field, None)
        self._store.write(event.model_copy(update={"data": data}))

    def _persist_llm_request_error(self, event: Event) -> None:
        """#4975 (architect ruling, issuecomment-5384508845): the
        lattice-meet gate for ``provider_body``/``provider_response`` —
        a provider's own 4xx/5xx error body can echo back content from
        the request it rejected, but reyn does not choose that body's
        shape, so it cannot tell WHICH content class (streamed reply /
        completed response / user input) a given quote would be. Showing
        either field therefore requires ALL 3 #4666 knobs AND this
        kind's own opt-in (:attr:`_provider_body_include_text`) — the
        narrowest participant wins, same "compose_resolved is a
        lattice-meet (∩ allow, ∪ deny)" idiom this repo's permission
        resolution already uses.

        NEVER silent either way (constitution's 2nd lens): ``error_type``/
        ``status_code`` are untouched (unconditional, unchanged);
        ``<field>_length`` is added for every field this method touches,
        gate-independent, so "a body existed but was not shown" stays
        distinguishable from "there was none" even when the meet fails.
        When the meet holds, the field is kept but capped at
        :attr:`_provider_body_max_chars` (reyn cannot bound a provider's
        own body size) — ``<field>_truncated`` is added only when the cap
        actually cut something, so a caller can tell a capped body from a
        genuinely short one."""
        meet = (
            self._agent_delta_include_text
            and self._completed_response_include_text
            and self._user_input_include_text
            and self._provider_body_include_text
        )
        data = dict(event.data)
        for field in self._PROVIDER_BODY_FIELDS:
            value = data.get(field)
            if value is None:
                continue
            text = value if isinstance(value, str) else str(value)
            data[f"{field}_length"] = len(text)
            if meet:
                if len(text) > self._provider_body_max_chars:
                    data[field] = text[: self._provider_body_max_chars]
                    data[f"{field}_truncated"] = True
                else:
                    data[field] = text
            else:
                data.pop(field, None)
        self._store.write(event.model_copy(update={"data": data}))

    def _persist_tool_returned(self, event: Event) -> None:
        """#5891 — ``tool_returned.data["result"]`` is an arbitrary tool's
        own return value, whose size reyn does not choose or bound:
        measured on reyn-self, two `exec` results were ~2.79MB and
        ~5.94MB, written straight into ONE audit-event line, breaking
        anything that reads `.reyn/events` a line at a time (`dogfood_
        trace`, `reyn events replay`, loop-side assembly).

        Same skeleton as :meth:`_persist_llm_request_error` (#4975 —
        "``<field>_length`` always, body capped, ``<field>_truncated``
        only when it actually cut something"), applied to ONE field
        (`result`) instead of a lattice-gated pair, and UNCONDITIONAL —
        there is no #4666 opt-in to consult here: a tool's own structured
        return value is not one of the 3 #4666 content classes (streamed
        reply / completed response / user input); this is a plain SIZE
        bound, always applied.

        `result_bytes` (the true UTF-8 byte length of *value*'s
        deterministic serialization — ``value`` unchanged if already a
        ``str``, else ``json.dumps(value, default=str, sort_keys=True)``:
        ``sort_keys`` makes the hash independent of dict insertion order,
        ``default=str`` covers a non-JSON-native value rather than
        raising) and `result_sha256` (the sha256 of that SAME
        serialization) are set UNCONDITIONALLY, computed over the FULL,
        untruncated body — this is what lets a later reader correlate an
        excerpt here with the full body once a `content_ref` mechanism
        (#5896, not this PR) can hand it back. `result` itself is
        replaced with the first `tool_result_max_chars` characters of
        that serialization, and `result_truncated` added, ONLY when the
        serialization is longer than the cap — a genuinely short/small
        result is written completely unchanged, with no `_truncated`
        marker, so the two cases stay distinguishable.

        Ordering (architect ruling, #5891, load-bearing): this method
        only ever sees `event.data` as handed to `write()` — which is
        ALREADY the output of `dispatch_tool`'s own
        ``_redact_content_fields(name, result, ctx)`` call
        (`core/dispatch/dispatcher.py`), run BEFORE `ctx.events.emit(
        "tool_returned", ...)`. A per-tool content declaration
        (`content_declarations.get_content_fields`, e.g. ``ask_user``'s
        ``answer``) has therefore already dropped whatever it named
        BEFORE this method serializes/hashes/excerpts `result` — this
        method can only re-expose what redaction LEFT, never what
        redaction removed. Computing the excerpt/hash from a PRE-redact
        payload would defeat that upstream declaration; this method never
        does its own redaction, it only bounds the size of what already
        arrived redacted.

        `content_ref_unavailable` is set unconditionally, on every event
        this method touches, regardless of length — #5891 adds only the
        size cap; the reference this excerpt could point back to for the
        full body (`content_ref`) is a SEPARATE, not-yet-wired mechanism
        (#5896, stage before its own stage ① ref-linking). Naming that
        explicitly here keeps "no ref exists yet" distinguishable from a
        reader silently concluding the full body never existed."""
        data = dict(event.data)
        if "result" in data:
            value = data["result"]
            serialized = value if isinstance(value, str) else json.dumps(
                value, default=str, sort_keys=True,
            )
            data["result_bytes"] = len(serialized.encode("utf-8", errors="replace"))
            data["result_sha256"] = hashlib.sha256(
                serialized.encode("utf-8", errors="replace")
            ).hexdigest()
            if len(serialized) > self._tool_result_max_chars:
                data["result"] = serialized[: self._tool_result_max_chars]
                data["result_truncated"] = True
        data["content_ref_unavailable"] = (
            "not yet wired (#5891, stage before #5896 stage ① ref-linking)"
        )
        self._store.write(event.model_copy(update={"data": data}))

    def declare_gaps(self) -> list[str]:
        gaps = [
            "agent_delta (streamed reply content, one per chunk) is not "
            "retained per-fragment — coalesced to one durable record per "
            f"{self._agent_delta_coalesce_fragments} fragments or "
            f"{self._agent_delta_coalesce_interval_ms}ms, whichever comes "
            "first, plus one final record when a stream ends (#4960). "
            "Live TUI/AG-UI delivery is unaffected — this is a durable-"
            "write-only gap. `reyn events replay` sees fewer agent_delta "
            "records than fragments actually streamed.",
        ]
        # #4666 (architect ruling on #4960: "declared" vs "never existed"
        # must stay distinguishable) — this gap is CONDITIONAL, not
        # static: it only applies while `agent_delta_include_text` is
        # off. A reader of a durable log written while this flag was ON
        # must not be told "the text was never retained" when it was —
        # and a reader of a log written while OFF must not read the
        # #4960-only gap above and conclude the reply's content was kept.
        if not self._agent_delta_include_text:
            gaps.append(
                "agent_delta's `text` field (the streamed reply content "
                "itself) is not retained in the durable coalesced record "
                "— dropped by config (audit_events.agent_delta_include_"
                "text=False, the default, #4666), not a #4960 side "
                "effect. `chain_id`/`round_index`/`coalesced_fragment_"
                "count`/`audit_seq` are still recorded, so 'a partial "
                "reply of N fragments existed' remains provable without "
                "the reply's own content. Live TUI/AG-UI delivery is "
                "unaffected — every subscriber still receives the full "
                "text for every fragment; this gap is durable-write-only.",
            )
        # #4666②: same "declared vs never-existed" discipline as ①'s gap
        # above, for the kinds `_COMPLETED_RESPONSE_TEXT_FIELDS` names.
        # DERIVED from that mapping (lead-coder review, same requirement
        # as ③'s own gap below, #4970) — a hand-listed string here would
        # silently under-declare if a 3rd kind/field were ever added to
        # the mapping without a matching edit to this string: drop the
        # field, but not name it. Deriving makes that skew structurally
        # impossible instead of merely detectable.
        if not self._completed_response_include_text:
            kind_field_pairs = ", ".join(
                f"{kind}.{'/'.join(fields)}"
                for kind, fields in sorted(self._COMPLETED_RESPONSE_TEXT_FIELDS.items())
            )
            gaps.append(
                "The free-text field(s) on each of these kinds "
                f"({kind_field_pairs}) — the completed model-to-user "
                "reply, and the model's own ask_user question — are not "
                "retained in the durable record — dropped by config "
                "(audit_events.completed_response_include_text=False, "
                "the default, #4666②), not a #4960 side effect and not "
                "the same knob as ①'s own streamed-fragment text opt-in "
                "above. Every other field (chain_id / intervention_id / "
                "round metadata) is still recorded, so 'a response was "
                "committed' / 'a question was asked' remains provable "
                "without either one's own content. Live TUI/AG-UI "
                "delivery, and any opt-in OTEL subscriber, are "
                "unaffected — this gap is durable-write-only.",
            )
        # #4666 item ③ — same conditional-not-static discipline as ①/②
        # above, for the user-input kinds in `_USER_INPUT_CONTENT_FIELDS`.
        # DERIVED from that mapping (lead-coder review, PR #4970), not
        # hand-listed: a 7th kind added to the mapping without a matching
        # edit here would otherwise silently under-declare (drop the
        # field, but not name it) — deriving makes that skew structurally
        # impossible instead of merely detectable.
        if not self._user_input_include_text:
            kind_field_pairs = ", ".join(
                f"{kind}.{field}"
                for kind, field in sorted(_USER_INPUT_CONTENT_FIELDS.items())
            )
            gaps.append(
                "The content-bearing field on each of these kinds "
                f"({kind_field_pairs}) is not retained in the durable "
                "record — dropped by config "
                "(audit_events.user_input_include_text=False, the default, "
                "#4666 item 3). Every other field on these kinds "
                "(chain_id/intervention_id/msg_id/seq/audit_seq/etc.) is "
                "still recorded. Live subscriber delivery (TUI/AG-UI/peer "
                "broadcast) is unaffected — this gap is durable-write-only. "
                "Known gap NOT closed by this flag either way: ask_user's "
                "question/answer also reach the audit log via "
                "tool_called.args/tool_returned.result (a different emit "
                "path, see this module's _USER_INPUT_CONTENT_FIELDS "
                "docstring).",
            )
        # #4975: a lattice-meet, not a single flag — the gap applies
        # whenever ANY participant is off, named per-participant so a
        # reader knows exactly which knob(s) would need to flip.
        missing = [
            name for name, on in (
                ("agent_delta_include_text", self._agent_delta_include_text),
                ("completed_response_include_text", self._completed_response_include_text),
                ("user_input_include_text", self._user_input_include_text),
                ("provider_body_include_text", self._provider_body_include_text),
            ) if not on
        ]
        if missing:
            gaps.append(
                "llm_request_error's provider_body/provider_response "
                "(a provider's own 4xx/5xx error body, which can quote "
                "back request content reyn does not control the shape "
                "of) are not retained in the durable record — a "
                "lattice-meet gate (#4975): showing them requires ALL of "
                f"audit_events.{{{', '.join(missing)}}} to be true, and "
                f"{'these are' if len(missing) > 1 else 'this is'} "
                "currently False (the default). error_type/status_code "
                "are always recorded; provider_body_length/"
                "provider_response_length are also always recorded when "
                "a body existed, so 'a body existed but was not shown' "
                "stays distinguishable from 'there was none'. This gap "
                "is durable-write-only.",
            )
        return gaps


class DiscardEventBackend:
    """Writes nothing (sink-null). `emit()` and subscriber dispatch are
    UNCHANGED when this backend is active (#4496's structural guarantee,
    see module docstring) — only the write-to-disk step becomes a no-op.

    `reyn events replay` / support-bundle / dogfood_trace must consult
    `declare_gaps()` and report it explicitly rather than reading an
    empty local events tree as "nothing happened" (contract 2)."""

    def write(self, event: Event) -> None:
        return None

    def declare_gaps(self) -> list[str]:
        return [
            "this backend does not retain events locally (audit_events."
            "backend: discard) — `reyn events replay`, support-bundle, "
            "and dogfood_trace have nothing to read for this run",
        ]


_NETWORK_QUEUE_SENTINEL = object()


class NetworkEventBackend:
    """Sends each event over the network (one HTTP POST per event), per
    #4496's `network` backend + `on_failure` knob (owner ruling,
    2026-09-09 — see this module's own docstring for the full design and
    why discard-and-continue is the default).

    ## Off-loop by construction (never blocks `EventLog.emit()`)

    `write()` only enqueues *event* onto a bounded in-process queue and
    returns — the actual HTTP POST runs on a single dedicated background
    thread (`_run`), never on the caller's thread. This mirrors every
    other off-loop write primitive in this package (`EventStore` routes
    its own I/O through `DurabilityWorker`; `OtelExporter`'s OTLP export
    runs on a background thread via `BatchSpanProcessor`) for the same
    reason: `emit()` is called from synchronous AND async call sites,
    many of them hot paths, and a blocking network call inline would
    stall every one of them on a slow or unreachable endpoint — exactly
    the kind of cost this module's contract ③ (a backend's failure must
    never reach a subscriber) is adjacent to but does not, by itself,
    prevent (a SLOW backend is not a RAISING backend).

    A full ``queue`` (``queue_maxsize``, bounded so a persistently
    unreachable endpoint cannot grow memory without limit) is treated
    identically to a failed send — the event goes through the SAME
    `on_failure` policy below, never silently dropped with no trace
    (the dropped count is WARN-logged once per process, latched, same
    "do not spam on a broken endpoint" discipline as `OtelExporter`'s own
    `_latch_error`).

    ## `on_failure` (owner ruling: discard is the default, spool is opt-in)

    - ``"discard"`` (default): a failed send does nothing further — the
      event disappears from durable storage. `EventLog.emit()` already
      stamped `audit_seq` on it BEFORE handing it to this backend (#4496
      PR-1 — this backend never touches `audit_seq`), so a receiver that
      DOES get delivery for neighbouring events sees a skipped number —
      "not silent" per contract 3, even though nothing is written here.
    - ``"spool"``: a failed send is written to *spool_store* instead (an
      `EventStoreLike` — typically a plain `EventStore` pointed at a
      dedicated local directory) — the owner's own framing (issue
      #4496): choosing `network` to NOT keep events locally, and then
      having a failure silently keep them locally anyway, would reverse
      the operator's own choice without telling them. `declare_gaps()`
      below says plainly, when `spool` is active, that events ARE being
      held on local disk despite choosing `network`.

    Never raises from `write()` — a send failure (queue full, connection
    error, non-2xx response, timeout) is always caught and routed through
    `on_failure` above; `EventLog.emit()`'s own try/except around
    `self._backend.write(event)` is a second, redundant line of defense
    for a bug in this class, not the primary mechanism."""

    def __init__(
        self,
        *,
        endpoint: str,
        on_failure: Literal["discard", "spool"] = "discard",
        spool_store: "EventStoreLike | None" = None,
        timeout_s: float = 5.0,
        client: "httpx.Client | None" = None,
        queue_maxsize: int = 1000,
    ) -> None:
        self._endpoint = endpoint
        self._on_failure: Literal["discard", "spool"] = on_failure
        self._spool_store = spool_store
        self._timeout_s = timeout_s
        # Test seam (mirrors this repo's existing idiom, e.g.
        # `LocalEventBackend`'s injectable `clock`): production passes
        # `client=None` and this constructs the standard DRY client
        # (`reyn._network.build_sync_http_client` — the ONE constructor
        # every reyn-owned `httpx.Client` must go through, per #3075's
        # completeness gate); a test injects a real `httpx.Client` wired
        # to `httpx.MockTransport` (a real httpx collaborator, not a
        # fake of reyn's own code) or a real client pointed at a
        # connection-refused address for a genuine failure.
        self._client = client
        self._owns_client = client is None
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=max(1, queue_maxsize))
        self._dropped_for_full_queue = 0
        self._error_latched = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="reyn-network-event-backend", daemon=True,
        )
        self._thread.start()

    def write(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped_for_full_queue += 1
            self._latch_warning(
                "network event backend queue is full (endpoint=%s, "
                "maxsize=%d) — treating as a send failure under "
                "on_failure=%s", self._endpoint, self._queue.maxsize,
                self._on_failure,
            )
            self._handle_failure(event)

    def _run(self) -> None:
        client = self._client
        if client is None:
            from reyn._network import build_sync_http_client

            client = build_sync_http_client(egress="audit_events_network_backend")
            self._client = client
        while True:
            item = self._queue.get()
            if item is _NETWORK_QUEUE_SENTINEL:
                self._queue.task_done()
                return
            event = item
            assert isinstance(event, Event)
            try:
                self._send(client, event)
            except Exception as exc:  # noqa: BLE001 — never let a send
                # failure kill this worker thread; route through policy.
                self._latch_warning(
                    "network event backend send failed (endpoint=%s): %s",
                    self._endpoint, exc,
                )
                self._handle_failure(event)
            finally:
                self._queue.task_done()

    def _send(self, client: "httpx.Client", event: Event) -> None:
        response = client.post(
            self._endpoint,
            json=event.model_dump(mode="json"),
            timeout=self._timeout_s,
        )
        response.raise_for_status()

    def _handle_failure(self, event: Event) -> None:
        if self._on_failure != "spool" or self._spool_store is None:
            return  # "discard" (default): the event disappears, on purpose
        try:
            self._spool_store.write(event)
        except Exception:
            logger.exception(
                "network event backend on_failure=spool: writing the "
                "failed event to the local spool ALSO failed (endpoint=%s) "
                "— this event is lost", self._endpoint,
            )

    def _latch_warning(self, msg: str, *args: object) -> None:
        if self._error_latched:
            return
        self._error_latched = True
        logger.warning(
            msg + " — suppressing further network-backend warnings for "
            "this process (subscriber delivery and audit_seq continuity "
            "are unaffected)", *args,
        )

    def wait_idle(self) -> None:
        """Block until every currently-queued event has been sent (or
        handled via `on_failure`) — a real wait on queue state
        (`queue.Queue.join`'s task-count, not a sleep), for a caller (a
        test, or a deliberate shutdown barrier) that must observe this
        backend's effect before proceeding. Never returns early just
        because the queue was momentarily empty — `join()` only returns
        once every `put` has a matching `task_done`."""
        self._queue.join()

    def close(self) -> None:
        """Stop the background thread after draining the queue. Idempotent.
        The thread is a daemon, so this is not required for process exit —
        it exists for deterministic test teardown (no thread leaked across
        tests) and for a graceful session shutdown path."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_NETWORK_QUEUE_SENTINEL)
        self._thread.join(timeout=self._timeout_s + 5.0)
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — best-effort on shutdown
                pass

    def declare_gaps(self) -> list[str]:
        gaps = [
            "this backend sends events over the network (audit_events."
            f"backend: network, endpoint={self._endpoint!r}) instead of "
            "writing them to `.reyn/events` — `reyn events replay`, "
            "support-bundle, and dogfood_trace have nothing to read "
            "LOCALLY for any event this backend handles, delivered or "
            "not (those tools only ever read this process's local disk; "
            "whatever the network endpoint does with a delivered event "
            "is outside this process's own audit trail).",
        ]
        if self._on_failure == "spool":
            gaps.append(
                "on_failure=spool: a FAILED network send is buffered "
                "locally on disk for retry — 'not saving locally' "
                "becomes inexact once this is chosen: events that fail "
                "delivery ARE being held on local disk (a capacity-"
                "limited spool, not a re-enabled local backend).",
            )
        else:
            gaps.append(
                "on_failure=discard (the default): a FAILED network send "
                "is dropped — the event disappears entirely, with no "
                "local copy. `audit_seq` still increments for it "
                "(stamped before this backend ever runs), so a receiver "
                "that DOES get delivery for neighbouring events sees a "
                "skipped number — not silent, even though nothing is "
                "written anywhere.",
            )
        return gaps


class EventStoreLike(Protocol):
    """The one method `LocalEventBackend` needs from `EventStore` — kept
    separate from importing `EventStore` directly so this module has no
    dependency on `event_store.py`'s file-rotation machinery (P7:
    OS-level generic infrastructure stays decoupled from any one backend's
    implementation details)."""

    def write(self, event: Event) -> None: ...
