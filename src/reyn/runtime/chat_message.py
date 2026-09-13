"""ChatMessage — the chat-history entry value object.

One ``ChatMessage`` is a single entry in the LLM-facing conversation history,
shaped to mirror the OpenAI/Anthropic message-list wire format so the history
serialises straight to the LLM (``user`` / ``assistant`` / ``tool`` / ``system``
/ ``summary`` roles; ``str`` or list-of-parts ``content``;
OpenAI tool-turn fields) — plus one Reyn-internal, never-wire role
(``spill_record``, #5612) that never becomes an LLM-facing turn at all.
Also provides the read-time migration that rewrites
pre-#383 on-disk history entries into this shape (``_migrate_legacy_chat_message``)
and the ``_now_iso`` timestamp helper. Pure value object — no dependency on
``Session``.

``Disclosure`` (#5678) is a SECOND declared axis, same shape as
``Spillability`` (#5514) — one declared field, normalized through one
choke point in ``__init__``, required (no default) whenever
``role="system"`` — answering "who may see this: the model's own
next-turn projection, an operator's restored TUI, both, or neither"
(``role`` alone conflates Reyn-internal chrome with producer-authored
content meant for the model — see that enum's own docstring).

``HistoryEntryKind`` (#6093 §1) is a THIRD declared axis, same
one-field/one-choke-point shape — defaulted (not required) to
``UNSPECIFIED`` — answering "is this entry Reyn's own OS chrome
(FRAME), or content someone/something outside Reyn's own OS layer
produced (MATERIAL)?" (see that enum's own docstring for the census
of the 4 previously-unshared vocabularies this replaces).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, NewType

#: #5973 裁定 4 (architect, precursor to that issue's ①②③): two byte
#: counts this codebase measures are DIFFERENT resources that #5896's
#: migration (inline body -> content_ref) let drift apart while both
#: stayed a bare `int` -- nothing distinguished "how many bytes THIS ROW
#: occupies while resident" from "how many bytes the BODY that row's ref
#: points at is". `ChatMessage.resident_bytes()` (this file) is the
#: former; the file a `content_ref` names is the latter
#: (`CONTENT_BYTES_META_KEY` below). Before #5896 a resident row WAS its
#: body, so the two counts were the SAME number and nothing separated
#: them; after #5896 a resident ref row can be ~400 bytes while the body
#: it points at is hundreds of MB -- #5973's own root cause is exactly
#: this: a bound written when the two currencies coincided kept counting
#: the wrong one once they split.
#:
#: `NewType` is a STATIC-ONLY distinction (zero runtime cost, zero
#: runtime enforcement -- both are still plain `int` at execution) that
#: makes the TWO KINDS OF INT mypy-incompatible with each other: passing
#: a `BodyBytes` value where a `ResidentBytes` is expected (or the
#: reverse) is a real `[arg-type]` mypy finding, not merely a naming
#: convention a reader has to remember to honor. #5975 did that
#: separation alone — no bound moved yet, see
#: `tests/runtime/test_5973_resident_body_bytes_types.py` for the live
#: mypy witness proving the distinction is enforced. #5973 ①/②/③ (this
#: PR) is what actually moves `history_resident.max_bytes`: it now sums
#: BOTH currencies for a content_ref row (`Session._evict_oldest_
#: resident_entries`'s own `_pull_weight`, ①) — a bound that only ever
#: counted `ResidentBytes` bounded nothing once the two currencies split
#: — and the SAME field doubles as the wire-materialization budget
#: (`RouterHistoryBuffer._wire_materialization_budget`, ②) and the
#: recovery-candidate hydration budget (`Session._hydrate_candidates_
#: under_budget`, ③): one resource, one field, three places that pull a
#: body in, never three independently-tuned numbers.
ResidentBytes = NewType("ResidentBytes", int)
BodyBytes = NewType("BodyBytes", int)


class Spillability(StrEnum):
    """#5514 §1 — answers ONE question: "when shrinking, may this content
    be spilled out (to a file, read back via tool), and in what order?"
    The ONLY consumer is `mid`'s own spill ordering inside the compaction
    overflow ladder (`retry_loop`/`_spill_candidates`, #5531 PR-3) — `mid`
    is not wire, it is `compact()`'s own input, so what gets spilled
    first is what the SUMMARY is built from; `head`/`tail` never look at
    this (they stay largest-first, unconditionally — #5514 §3).

    Deliberately NOT `role`-derived: `role` cannot express this axis.
    Cross-agent injection arrives with no literal role at all (#4477);
    reyn's own FRAME (`notify_turn_cancelled`) and MATERIAL
    (`template_push`) share `role=="system"`; a hand-typed and a pasted
    user message share `role=="user"`. See #5514 §2 for the full
    argument and §4 for the call-site assignment table.

    ``StrEnum``, not the ``(str, Enum)`` spelling this repo uses
    elsewhere (see ``turn_origin.TurnOrigin``'s own module docstring for
    the full argument) — a member persisted to ``history.jsonl`` via
    ``asdict(msg)`` + ``json.dumps`` must serialise to its own wire
    string, not ``Enum.__repr__``'s ``"Spillability.FIRST_CHOICE"``; a
    value read back as a plain ``str`` must still compare equal to the
    member. ``StrEnum`` gives both for free.
    """

    FIRST_CHOICE = "first_choice"  #: spill this before anything else
    LAST_RESORT = "last_resort"    #: spill only once FIRST_CHOICE is exhausted
    NEVER = "never"                #: never spilled — losing it falsifies the model's own world-state (#5514 §1.1), NOT merely "less detail". Still fully eligible for COMPACT (folded into prose like any other entry, #5531 invariant 5) — this only forbids the spill mechanism (content leaves the conversation to an external file).

    @classmethod
    def default(cls) -> "Spillability":
        """#5514 §1: the safe-side default is `LAST_RESORT`, not `NEVER`
        — an omitted declaration must degrade availability (spill later
        than it should), never silently fail a turn. Making `NEVER` the
        default would turn a missed call-site declaration into a turn
        failure instead."""
        return cls.LAST_RESORT


def _normalize_spillability(value: object) -> Spillability:
    """#5580: ``ChatMessage.__init__``'s ONE normalization point for
    ``spillability`` — every construction path funnels through here,
    including ``ChatMessage(**raw)`` from a read-back ``history.jsonl``
    line (``Session._parse_history_line``).

    #5514 closed the WRITE side (persisting ``.value``, a plain ``str``,
    via ``asdict`` + ``json.dumps``) but not the READ side: a value that
    survived a restart round-trip arrived here as that same plain ``str``,
    which every consumer of ``msg.spillability`` (starting with
    ``router_history_buffer.py``'s own ``.value`` access) assumed was
    already a ``Spillability`` member — AttributeError the first time an
    overflow ladder ran against a restarted session's history (#5580).

    - ``None`` (omitted at a call site) → ``Spillability.default()``,
      same as before this fix.
    - Already a ``Spillability`` member → passed through unchanged (the
      overwhelmingly common in-process construction path; StrEnum's own
      ``isinstance`` check here is why the ORDER of the checks below
      matters — a ``Spillability`` member is ALSO a ``str``).
    - A plain ``str`` that names a real member (the read-back case) →
      converted to that member.
    - Anything else — an unrecognized string (a future value this
      version's enum doesn't have yet, or a corrupted history line) —
      degrades to ``Spillability.default()`` rather than raising. #5514
      §1's own safe-side argument applies here identically: an unreadable
      declaration must degrade availability, never fail the turn.
    """
    if value is None:
        return Spillability.default()
    if isinstance(value, Spillability):
        return value
    if isinstance(value, str):
        try:
            return Spillability(value)
        except ValueError:
            return Spillability.default()
    return Spillability.default()


#: #5678 §3 rung order for ``Disclosure`` below — the widest-audience-first
#: reading a member's own ordinal expresses structurally (see that class's
#: docstring for why this is a LADDER, not 3 independent flags).
_DISCLOSURE_RANK: "dict[str, int]" = {
    "internal": 0,
    "operator": 1,
    "model": 2,
}


class Disclosure(StrEnum):
    """#5678 — answers ONE question: "how widely may this ``role="system"``
    entry be shown — nowhere, to the operator reconstructing their own
    session, or to the model's own next-turn projection?" ``role`` alone
    cannot express this: it already carries TWO unrelated meanings for
    ``"system"`` — Reyn-internal chrome (never shown anywhere: SP notes,
    ``notify_state_change``) and producer-authored content meant to reach
    the model (a hook push, a ride-along, a mid-turn peer request) — and
    both filters that read ``role`` (``RouterHistoryBuffer``'s allowlist,
    ``restore.py``'s ``_SKIP_ROLES``) end up excluding the SECOND meaning
    as collateral damage from excluding the first. Same shape as
    ``Spillability`` (#5514) — one declared field, normalized through ONE
    choke point — deliberately NOT that same field: spillability answers
    "may this be OFFLOADED"; this answers a completely different question
    ("who gets to SEE this"), and #5514 itself is the argument that a
    single role cannot safely carry two independent axes.

    ``StrEnum`` for the same reason ``Spillability`` is: a value
    persisted to ``history.jsonl`` via ``asdict`` + ``json.dumps`` must
    serialise to its own wire string, and a value read back as a plain
    ``str`` must still compare equal to the member.

    **A LADDER, not three independent flags** (architect ruling, #5678
    §3 — the axis-count question the issue explicitly reserved for
    them): #5678's own investigation found no CURRENTLY KNOWN
    ``role="system"`` producer needing "model=yes, operator-restore=no"
    — anything worth the model seeing is worth an operator
    reconstructing their own session seeing too. Rather than leave "MODEL
    implies operator-visible" as an OBSERVATION a future reader could
    contradict by adding a 4th, asymmetric member, the three members are
    given a REAL total order (``<``/``<=``/``>``/``>=`` below, backed by
    ``_DISCLOSURE_RANK``) — each rung's own value IS "the widest audience
    this may reach", so a consumer checking operator-visibility asks
    ``disclosure >= Disclosure.OPERATOR`` (true for OPERATOR AND MODEL,
    by construction) rather than maintaining a separate membership set
    that could silently drift from the model-visibility check. If a
    future producer genuinely needs "model=yes, operator-restore=no",
    that value cannot be expressed by inserting a member ABOVE MODEL on
    this ladder (there is nothing above "everyone") — it is the signal
    this axis needs to become two independent ones, not something to
    guess ahead of by leaving room in the ordering.

    Deliberately NO default (architect ruling, verbatim): "既定値を置く
    と同じ沈黙が戻ります（既定 INTERNAL＝新しい producer が黙って消える
    ／既定 MODEL＝chrome が漏れる）". A missing declaration must be a
    LOUD, structural failure (``ChatMessage.__init__`` raises for
    ``role="system"`` with no ``disclosure``) at every FRESH call site —
    never silently resolved to either extreme. The one place this field
    IS genuinely absent — a ``history.jsonl`` line persisted before
    #5678 shipped — is handled by ``_migrate_legacy_chat_message``
    computing the value the OLD ``meta.kind``-based logic would have
    produced, BEFORE calling ``ChatMessage(...)`` — not by this
    ``__init__`` silently defaulting, which would blur "an old record
    predates the axis" and "a new call site forgot to declare" into the
    same code path.
    """

    #: Rung 0 — reaches no one. The pre-#5678 behavior for every
    #: ``role="system"`` entry (``notify_state_change``, SP chrome). Its
    #: OWN docstring claims LLM-visibility (pre-existing, false — #5678's
    #: own census) — kept INTERNAL, unchanged, for #5678's migration
    #: (architect ruling: behavior-preserving here; whether it SHOULD
    #: become ``MODEL`` is a separate, deferred question, #5686).
    INTERNAL = "internal"
    #: Rung 1 — reaches an operator reconstructing their own session in
    #: TUI restore, never the model's own turn projection. Exactly one
    #: producer: ``Session.notify_turn_cancelled`` / ``RouterLoop``'s own
    #: cooperative-cancel terminal (#3694) — a UI acknowledgement of an
    #: operator-initiated cancel, not conversational content the model
    #: should carry forward as if the turn had happened normally.
    OPERATOR = "operator"
    #: Rung 2 (widest) — reaches the model's next-turn projection (once
    #: the allowlist widens, #5686/architect co-vet — NOT yet wired by
    #: this field alone) AND, by this ladder's own structural rule (see
    #: the class docstring), TUI restore too. A hook push (E), a
    #: wake=false ride-along (C), a mid-turn AGENT_REQUEST injection
    #: (#5677/#5684).
    MODEL = "model"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Disclosure):
            return NotImplemented
        return _DISCLOSURE_RANK[self.value] < _DISCLOSURE_RANK[other.value]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Disclosure):
            return NotImplemented
        return _DISCLOSURE_RANK[self.value] <= _DISCLOSURE_RANK[other.value]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Disclosure):
            return NotImplemented
        return _DISCLOSURE_RANK[self.value] > _DISCLOSURE_RANK[other.value]

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Disclosure):
            return NotImplemented
        return _DISCLOSURE_RANK[self.value] >= _DISCLOSURE_RANK[other.value]


def is_model_visible(m: Any) -> bool:
    """#5678: true for a ``role="system"`` entry declared ``Disclosure.
    MODEL`` — the ONE case every filter below (and every history-window/
    compaction-candidate filter in ``session.py``, ``compaction_
    controller.py`` and ``router_history_buffer.py``) admits on TOP of
    each filter's own always-included roles.

    THE single shared predicate for this question (#5678, relocated here
    by #5699 — see :func:`is_compaction_eligible`'s own docstring for
    why): never duplicated inline at any call site.

    ``m.disclosure`` is ``None`` for every non-``"system"`` role (see
    ``Disclosure``'s own docstring / ``_normalize_disclosure``) and for a
    ``role="system"`` entry it is NEVER ``None`` (``ChatMessage.__init__``
    raises otherwise, and a pre-#5678 persisted line is migrated to a real
    value before construction) — so the ``is not None`` guard here is a
    defensive belt, not a live branch for any reachable production value.
    """
    return (
        m.role == "system"
        and m.disclosure is not None
        and m.disclosure >= Disclosure.MODEL
    )


#: #5699 (architect ruling): the base roles a history-window/compaction
#: filter always includes, REGARDLESS of ``is_model_visible`` — E-full
#: #383's own user/assistant/tool/agent. Named here (not a literal tuple
#: hand-typed at each of the 4 call sites this used to be #5699's own
#: root cause) so ``scripts/check_no_hardcoded_compaction_role_tuple.py``
#: has exactly one legitimate definition site to allow.
COMPACTION_ELIGIBLE_BASE_ROLES: "tuple[str, ...]" = ("user", "assistant", "tool", "agent")


def is_compaction_eligible(m: Any) -> bool:
    """#5699: true for an entry a history-window projection or a
    compaction candidate filter should admit — the base roles (E-full
    #383) OR a MODEL-visible ``system`` entry (#5678). Does NOT admit
    ``role="summary"`` — see :func:`is_compaction_eligible_including_
    summary` for the one filter (``decompose_history_for_retry``) that
    deliberately does, and #5678's own acceptance-item-3 test for why
    the two must stay genuinely different predicates, not one collapsed
    into the other.

    THE single shared predicate for "can this entry enter the window /
    be offered to ``/compact``" — used by ``router_history_buffer.py``'s
    own ``_elide_candidate_turns`` (feeds ``build_history``, the wire
    projection), ``compaction_controller.py``'s ``force_compact_now``
    candidate filter, and ``session.py``'s own ``_compact_now_for_op``
    reporting filter. Before #5699 these were 2 independent hand-typed
    copies of the base-role tuple (``router_history_buffer.py``'s own
    filter DID gain the ``is_model_visible`` OR-term when #5678/#5688
    widened the window; ``compaction_controller.py``'s and
    ``session.py``'s never did) — an entry could enter the live window
    forever while staying permanently un-foldable by ``/compact``, the
    owner's own real-machine incident (2026-09-03): a history dominated
    by such entries reported "Nothing was compacted this pass" on every
    ``/compact``, while the next turn's overflow eventually exhausted the
    reactive ladder's own, correctly-widened raw_middle and surfaced a
    raw, unrecovered provider error.
    """
    return m.role in COMPACTION_ELIGIBLE_BASE_ROLES or is_model_visible(m)


def is_compaction_eligible_including_summary(m: Any) -> bool:
    """#5699: :func:`is_compaction_eligible`'s own sibling for the ONE
    filter that also admits ``role="summary"`` —
    ``decompose_history_for_retry`` (the reactive overflow ladder's own
    candidate builder). #5531's own invariant: a summary represents ONE
    continuous span and sits at its own chronological position, so the
    windowing that places head/mid/tail must be able to see it — never
    admitted by :func:`is_compaction_eligible` itself (``build_history``'s
    own projection attaches summary content via a synthetic bridge
    instead; a raw ``role="summary"`` turn must never leak into it — see
    ``test_5678_disclosure_axis.py``'s own
    ``test_summary_role_reaches_retry_decompose_but_not_build_history``,
    the strip-falsifier for accidentally collapsing these two predicates
    into one)."""
    return (
        m.role in (*COMPACTION_ELIGIBLE_BASE_ROLES, "summary")
        or is_model_visible(m)
    )


def is_seq_still_active(
    seq: int, *, covers_from: "int | None", covers_through: int,
) -> bool:
    """#5765 — THE single predicate for "is this turn still active" (kept
    on the wire / in an untrusted-taint scan) vs "compacted out" (folded
    into the latest summary, permanently hidden). Replaces 2 literal-copy
    predicates (``router_history_buffer.py``'s own ``_apply_watermark_
    filter`` and ``session.py``'s ``_update_untrusted_taint_on_append``)
    that both hand-typed ``seq == 0 or seq > watermark`` independently
    (lead-coder finding, PR review on #5765) — a THIRD, INDEPENDENT copy
    is exactly the shape #4954(2)/#5612 already closed twice for this
    same predicate, so this is the one place it may be written.

    #5765's own root cause (architect, issue #5765): the pre-fix scalar
    watermark answered "is seq <= covers_through" — TRUE for a turn that
    was head-PROTECTED (never folded, `trim_head`'s own #5719 guard) but
    numerically below the LATEST fold's `covers_through_seq` — silently
    hiding it from the wire though it was never summarised either. The
    fix is a RANGE, not a scalar: a turn is compacted out only if it
    falls inside ``[covers_from, covers_through]`` — the range the latest
    summary ACTUALLY folded, never "everything below the ceiling".

    ``seq == 0`` is always active (#3704's own "no coordinate assigned"
    sentinel, pre-#3704 legacy history — never a compaction candidate
    either, so never foldable and never hidden by this predicate).
    ``covers_through <= 0`` (no summary yet) — always active, nothing to
    hide.

    ``covers_from is None`` (a summary persisted BEFORE #5765 — no
    recorded fold-start boundary) — SAFE SIDE: never hide anything based
    on this summary. Driven reasoning (not merely risk-averse): the raw
    turns this would otherwise hide are NOT actually gone — history.jsonl
    is append-only, so they are still genuinely on disk; a pre-#5765
    summary's own `covers_through_seq` scalar cannot distinguish "folded"
    from "head-protected", so hiding by it would silently repeat #5765's
    own defect for every summary written before this fix landed. The
    alternative (preserve the old scalar behavior for legacy summaries)
    would leave the exact damage this fix exists to close in place for
    every session with pre-existing history — the field is retroactively
    inferrable as "unknown", never as 0 (which would hide everything) or
    as `covers_through` (which reproduces the bug). A resulting larger
    wire payload is not a new failure mode — the existing overflow-
    recovery ladder (spill/shrink/retry, `engine.py`) already handles an
    oversized turn payload from many other causes; this is bounded by the
    SAME mechanism, and self-heals the next time compaction runs (which
    now always records a real `covers_from`)."""
    if seq == 0 or covers_through <= 0:
        return True
    if covers_from is None:
        return True
    return not (covers_from <= seq <= covers_through)


def compaction_coverage_from_summary(
    summary: "ChatMessage | None",
) -> "tuple[int | None, int]":
    """#5765: the ONE place that parses a summary message's ``meta`` into
    the ``(covers_from_seq, covers_through_seq)`` pair
    :func:`is_seq_still_active` needs — ``(None, 0)`` if *summary* is
    ``None`` (no compaction has ever run).

    A pure function, not a method on either ``Session`` or
    ``RouterHistoryBuffer``, deliberately: both need this exact parsing,
    but each already has its OWN way to find "the latest summary"
    (``Session._latest_summary`` scans ``self.history``;
    ``RouterHistoryBuffer._latest_summary`` scans whatever ``history``
    its caller already fetched, #2939). A method on either side would
    force the OTHER side to either duplicate the parsing (the #5765
    drift this whole consolidation exists to close) or reach across the
    object boundary for it — which for ``Session`` specifically would
    mean calling into ``self._history_buffer``, an attribute NOT YET SET
    on a ``Session`` built via ``Session.__new__`` + manual field
    assignment (the exact shape ``test_load_history_migrates_legacy_
    lines`` uses to exercise ``load_history`` without booting a full
    session) or, during real construction, before ``_build_history_
    compaction_bundle`` returns (see that builder's own ★★ docstring).
    Neither caller needs the other's object at all for this — only the
    already-resolved summary message."""
    if summary is None:
        return None, 0
    meta = summary.meta or {}
    covers_through = int(meta.get("covers_through_seq", 0))
    covers_from_raw = meta.get("covers_from_seq")
    covers_from = int(covers_from_raw) if covers_from_raw is not None else None
    return covers_from, covers_through


def parse_history_line(line: str) -> "ChatMessage | None":
    """Parse one raw ``history.jsonl`` line into a :class:`ChatMessage`, or
    ``None`` if malformed (skipped, never raised).

    Extracted from ``Session._parse_history_line`` (#5759 stage 2): that
    method's own body never touched ``self`` — a pure JSON-line parse
    (read-time legacy migration + construction), so a SECOND caller with
    no live ``Session`` (the history.jsonl GC below, which runs from
    ``AgentRegistry`` and may need to find the latest summary for an agent
    that isn't currently loaded) would otherwise have had to duplicate
    this exact 3-line body — the same "same guard, second copy" shape
    ``AgentRegistry._oldest_kept_seq`` was extracted to avoid, one module
    over. ``Session._parse_history_line`` now delegates here unchanged."""
    try:
        raw = json.loads(line)
        raw = _migrate_legacy_chat_message(raw)
        return ChatMessage(**raw)
    except Exception:
        return None


def history_record(msg: "ChatMessage") -> dict:
    """The durable ``history.jsonl`` form of *msg* — the ONE place the
    resident ``ChatMessage`` and its on-disk line are allowed to differ
    (#5896, #5364 §1.1 "A": "history.jsonl は参照だけを持つ").

    An un-spilled tool row that carries ``CONTENT_REF_META_KEY`` (its body
    was written to ``history-content/`` by ``MediaStore.save_tool_result``
    BEFORE this row is appended — write-ahead, see ``RouterLoop.
    persist_feedback``) is persisted WITHOUT its body: the file is the
    durable copy, the resident message keeps the body as the live cache,
    and ``Session._parse_history_line`` hydrates it back through
    ``history_content_resolve.resolve`` on the next process start. Every
    other row — a spilled row (its content IS the small ref-preview the
    model sees), a #5364 §1.5 write-refused row (no ref, body inline by
    decision), a user/assistant/system/summary row — is ``asdict`` as
    before, byte-identical.

    Keyed on the TYPED meta fields, never on the content's own shape:
    a body that happens to look like a preview is still a body."""
    record = asdict(msg)
    meta = msg.meta or {}
    if (
        msg.role == "tool"
        and meta.get(CONTENT_REF_META_KEY)
        and not meta.get(SPILLED_META_KEY)
    ):
        record["content"] = ""
    return record


def _normalize_disclosure(value: object, *, role: str, meta: dict) -> "Disclosure | None":
    """#5678: the ONE normalization point for ``disclosure`` — every
    construction path funnels through here, including
    ``ChatMessage(**raw)`` from a read-back ``history.jsonl`` line
    (``Session._parse_history_line``, via ``_migrate_legacy_chat_message``
    computing a value for pre-#5678 lines BEFORE this runs — see that
    function's own docstring).

    - ``role != "system"``: this axis does not apply (every other role
      is dispatched on ``role`` itself, by the SAME filters that read
      ``disclosure`` for ``"system"``) — always ``None``, whatever was
      passed, so a caller can never accidentally believe a non-system
      entry's visibility was declared here.
    - ``role == "system"`` and *value* already a ``Disclosure`` member —
      passed through unchanged.
    - ``role == "system"`` and *value* a plain ``str`` naming a real
      member (the read-back case, once ``_migrate_legacy_chat_message``
      has supplied one) — converted to that member.
    - ``role == "system"`` and *value* is ``None`` or an unrecognized
      string — raises ``ValueError``. Deliberately NOT a safe-side
      default (contrast ``_normalize_spillability``): architect's own
      ruling for this axis is that an omitted declaration must be a
      LOUD failure, not a silent INTERNAL (hides real content) or MODEL
      (leaks chrome) guess. A legacy on-disk line without a value never
      reaches here missing one — ``_migrate_legacy_chat_message``
      supplies it first.
    """
    if role != "system":
        return None
    if isinstance(value, Disclosure):
        return value
    if isinstance(value, str):
        try:
            return Disclosure(value)
        except ValueError:
            pass
    raise ValueError(
        f"ChatMessage(role='system', ...) requires an explicit "
        f"disclosure= (INTERNAL / OPERATOR / MODEL) — got {value!r}. "
        f"#5678: role='system' carries two unrelated meanings (Reyn "
        f"chrome vs producer content meant for the model), so 'system' "
        f"alone cannot say which this entry is. meta={meta!r}"
    )


class HistoryEntryKind(StrEnum):
    """#6093 §1 — answers ONE question: "is this entry Reyn's own OS
    chrome, or is it MATERIAL someone/something outside Reyn's own OS
    layer produced?" Vocabulary taken verbatim from this file's own
    prose (:class:`Spillability`'s own docstring, #5514): "reyn's own
    ★FRAME (``notify_turn_cancelled``) and ★MATERIAL (``template_push``)
    share ``role=="system"``" — the census that led to this field found
    that same FRAME/MATERIAL split already informally present but never
    given a field, spread across FOUR different keys with no shared
    vocabulary (a bare ``meta["kind"]`` string, ``meta["origin"]``,
    ``meta["source"]``, and a second, unrelated ``meta["kind"]``
    spelling holding a :class:`~reyn.runtime.turn_origin.TurnOrigin`
    member) across the 17 ``_append_history(`` call sites (``git grep
    -n '_append_history(' -- src/``).

    Deliberately NOT :class:`~reyn.runtime.turn_origin.TurnOrigin`
    (architect ruling, #6093 §1): ``TurnOrigin`` answers "which
    PRODUCER authored this MATERIAL" — a question that does not even
    apply to a FRAME entry (there is no producer to name), and it has
    no member for what a FRAME entry actually IS (``"turn_cancelled"``
    is not among its 11 members, all of which name a MATERIAL
    provenance). This field does not replace ``origin``/``TurnOrigin``
    or either existing ``meta["kind"]`` spelling — it answers a
    logically PRIOR question (frame vs material), leaving "which
    producer" exactly where it already lived.

    ``StrEnum`` for the same reason ``Spillability``/``Disclosure`` are:
    a value persisted to ``history.jsonl`` via ``asdict`` + ``json.dumps``
    must serialise to its own wire string, and a value read back as a
    plain ``str`` must still compare equal to the member.
    """

    #: The safe-side default (see :meth:`default`) — asserts NOTHING
    #: about which layer this entry belongs to. Every entry constructed
    #: before this field existed reads back as this value (no backfill,
    #: #6093 §1 ruling — same forward-only shape as ``ChatMessage.
    #: origin``: "existing entries written before this field existed
    #: have no key, which is treated as not-yet-classified").
    UNSPECIFIED = "unspecified"
    #: Reyn's own OS chrome — never producer-authored, never something
    #: the model or an operator should read as conversational content
    #: (``notify_turn_cancelled``'s ack, a canned retry-exhausted reply).
    FRAME = "frame"
    #: Content someone or something OUTSIDE Reyn's own OS layer produced
    #: — a user prompt, an LLM reply, a tool result, a hook push, a
    #: ride-along, an inter-agent message (``template_push`` and every
    #: other #5514 "deliverable", in that docstring's own vocabulary).
    MATERIAL = "material"

    @classmethod
    def default(cls) -> "HistoryEntryKind":
        """#6093 §1 (architect ruling): the safe-side default is
        ``UNSPECIFIED``, never ``FRAME`` or ``MATERIAL`` — an omitted
        declaration must not silently claim EITHER layer. The same
        asymmetry ``Spillability.default()`` names for its own axis
        (verbatim): a missing declaration must degrade to "not yet
        classified", never assert the answer to a question nobody
        actually answered. Concretely: a discriminator reading this
        field must treat ``UNSPECIFIED`` as *not applicable* — never as
        a 3rd member of either side of the FRAME/MATERIAL split."""
        return cls.UNSPECIFIED


def _normalize_history_entry_kind(value: object) -> HistoryEntryKind:
    """#6093 §1: ``ChatMessage.__init__``'s ONE normalization point for
    ``kind`` — every one of the 17 ``_append_history(`` call sites funnels
    through here, including ``ChatMessage(**raw)`` from a read-back
    ``history.jsonl`` line.

    - ``None`` (omitted at a call site, or a pre-#6093 persisted line
      with no ``kind`` key at all) → :meth:`HistoryEntryKind.default`
      (``UNSPECIFIED``) — see that method's own docstring for why this
      must not fall to either real member. This is what lets all 16 of
      the 17 call sites this PR does not touch keep working unchanged:
      an entry that never declares ``kind`` is simply not-yet-classified,
      never silently misclassified.
    - Already a ``HistoryEntryKind`` member → passed through unchanged.
    - A plain ``str`` that names a real member (the read-back case) →
      converted to that member.
    - Anything else — an unrecognized string (a future value this
      version's enum doesn't have yet, or a corrupted history line) —
      degrades to :meth:`HistoryEntryKind.default` (``UNSPECIFIED``)
      rather than raising.

      SAME safe-side judgment as ``_normalize_spillability`` (lead-coder
      BLOCKING correction, #6093 review — an earlier version of this
      function raised here instead, reasoning by a DIFFERENT precedent,
      ``Disclosure``'s own required-value raise; that precedent does not
      apply: ``Disclosure`` is required with no default, so a bad value
      IS a caller bug to surface loudly. ``kind`` has a real, safe-side
      default — the exact shape ``_normalize_spillability``'s own
      docstring already argues for: "an unrecognized string … degrades
      to ``Spillability.default()`` rather than raising", "an unreadable
      declaration must degrade availability, never fail the turn").
      Concretely: this function is reached from ``ChatMessage(**raw)``
      on every ``history.jsonl`` read-back (``Session._parse_history_
      line``) — raising here would mean (1) a NEWER build's ``kind``
      value could never be read back by an OLDER build, and (2) one
      corrupted/foreign-written history line could fail loading the
      WHOLE session on restart, for a field whose own default is
      already "not yet classified". Degrading to ``UNSPECIFIED`` also
      satisfies #6093 §1's own default ruling directly (the default
      "must not fall to either real member... never assert the answer
      to a question nobody actually answered") — the same value an
      omitted declaration produces, which is correct: an unrecognized
      string is not more informative than no declaration at all.
    """
    if value is None:
        return HistoryEntryKind.default()
    if isinstance(value, HistoryEntryKind):
        return value
    if isinstance(value, str):
        try:
            return HistoryEntryKind(value)
        except ValueError:
            return HistoryEntryKind.default()
    return HistoryEntryKind.default()


# #73: typed (not form-sniffed) tool-outcome classification, stamped on a
# ``role="tool"`` message's ``meta`` at PERSIST time by the ONE place that
# already knows the classification (``router_loop.py``'s tool-result
# assembly — a dispatch-envelope ``{"status":"error",...}`` or an MCP
# ``isError`` result). A consumer (e.g. the TUI restore projection,
# ``interfaces/inline/textual_chat/restore.py``) reads these keys directly —
# it must NEVER re-derive the classification by sniffing the rendered
# ``content`` string (that string's shape is a renderer/display concern, not
# a stable data contract, and a success payload can legitimately start with
# the same words an error message would). ABSENCE of ``TOOL_STATUS_META_KEY``
# (e.g. a pre-#73 persisted history) means "unknown" — a reader must treat
# that as success/completed (today's existing behavior), never infer failure
# from its absence or from the content string.
TOOL_STATUS_META_KEY = "tool_status"
TOOL_STATUS_ERROR = "error"
TOOL_ERROR_KIND_META_KEY = "error_kind"
TOOL_ERROR_MESSAGE_META_KEY = "error_message"

# #5364 §1.2: the tool-result history-content resolver's own persisted
# signals — typed via named meta keys, NOT a new top-level ChatMessage
# field (lead-coder ruling: a new field creates a "missing" state for
# every ALREADY-persisted record, the same defect class ``seq: int = 0``'s
# own "0 = no coordinate assigned (pre-fix history only)" already carries;
# `meta` + a named key is this repo's typed convention for exactly this
# shape — see `TOOL_STATUS_META_KEY` above, "restore.py reads this typed
# field directly, matching reyn's typed-over-form-sniffed convention").
#
# ABSENCE of ``SPILLED_META_KEY`` (pre-#5364 history) means "never
# spilled" (today's only possible history — nothing offloaded a tool
# result into this store before #5364 existed), never "unknown".
# #5896 writes an explicit ``False`` on every un-spilled tool row; every
# reader is a ``meta.get(SPILLED_META_KEY)`` truth read, so absence and
# False are ONE observation. Never add a membership read (``KEY in
# meta``) — it would split them into two facts, and that class fails open.
SPILLED_META_KEY = "spilled"
# The backing file's project-relative path. #5364 §1.1 "A" (#5896: now
# delivered at return time, not only on spill): EVERY tool result whose
# write landed is file-backed — a SPILLED entry's persisted content is the
# ref-preview the model sees, an UN-SPILLED entry's durable history.jsonl
# line carries NO body at all (``history_record`` drops it; the file is
# the one durable copy) while its resident ChatMessage keeps the body.
# `reyn.core.offload.history_content_resolve.resolve` is the ONE table
# that turns (spilled, ref, content) back into a body.
# #5364 §1.5: "A" is not "always" without exception — a write that is
# known, in advance, not to land (MediaStoreWriteUnavailable) never
# reaches this store at all; that turn's content stays inline and this
# key is never set (see LostReason.NEVER_PERSISTED below).
CONTENT_REF_META_KEY = "content_ref"
# #5896: stamped alongside CONTENT_REF_META_KEY on an un-spilled tool row
# — the body's byte length and mime type, so `reyn storage`-class readers
# can size a session's tool results from history.jsonl alone (the row no
# longer carries the bytes they used to measure) without opening files.
CONTENT_BYTES_META_KEY = "bytes"
CONTENT_MIME_META_KEY = "mime"
# #5949 (owner-hit P0): a BOUNDED preview of an un-spilled tool row's body
# — set ONLY by Session._fill_content_previews, for a row loaded via
# backward-paging (extend_history_backward/_async, attach's own scrollback
# read) whose `content` stays EMPTY (durable shape, untouched — this is a
# SEPARATE field, never a substitute for `.content`, precisely so the LLM
# wire path's own lazy resolve at _serialise_turn — which reads `.content`,
# not this key — always sees the REAL full body, never the preview). A
# reader that wants a bounded, cheap-to-show text for a row it has NOT
# hydrated reads this key when present; every other reader (build_history,
# compaction, a live in-process row) is completely unaffected — this key is
# additive-only, never read by the wire-serialise path.
CONTENT_PREVIEW_META_KEY = "content_preview"
# Set once `resolve()` (reyn.core.offload.history_content_resolve) has
# actually observed the backing file missing — never guessed ahead of
# that check. ABSENCE means "not (yet) known to be lost", never "present".
LOST_META_KEY = "lost"
LOST_REASON_META_KEY = "lost_reason"


class LostReason(StrEnum):
    """#5364 §1.5 / #5438 (architect ruling): why a spilled entry's own
    backing file is missing — carried as a TYPED value (``StrEnum``, same
    discipline ``Spillability`` above documents — never a bare ``str``,
    the exact "a callback that can't carry a reason" defect #5438 named:
    "引数を運べない callback は『None が2つの事実を表す』の署名版").

    ``NEVER_PERSISTED`` is WRITTEN, at persist time, by the write-time cap
    (``router_loop.py``) when an offload was attempted and refused —
    content stayed inline, but the entry still records why.

    ``GC`` is NEVER written — #5438's own design ("compute, don't store"):
    a spilled entry whose backing file is missing and whose own meta does
    NOT carry ``NEVER_PERSISTED`` is derived as ``GC`` at READ time
    (``router_history_buffer.py``'s own resolver), because eviction
    (``media_store._evict_history_content_over_cap``) is reyn's ONLY
    deleter of an already-persisted ref — no other src/ code path removes
    an offload file. Disclosed, not claimed exhaustive: a file removed
    OUTSIDE reyn (manual deletion, an external process) also reads as
    ``GC`` — this repo keeps no separate record that would tell the two
    apart, and #5438 explicitly rules out adding one (a ledger just
    duplicates history.jsonl's own append-only truth).

    ``EXTERNAL`` (#5896 stage ①) is likewise NEVER written, derived the
    same way for an UN-SPILLED entry (``SPILLED_META_KEY`` False, a
    ``CONTENT_REF_META_KEY`` present): no eviction pass selects an
    un-spilled file — the per-session pass by this store's own record,
    the project-wide pass by re-reading every session's manifest lines
    at pass time (``MediaStore._manifest_paths_project_wide``, architect
    co-vet 🔴 #2: a pass that used only the calling store's own view
    evicted a neighbour's un-spilled body, and this reason then LIED
    about it) — so a missing one was removed by something outside reyn's
    own GC. Disclosed exception, not claimed closed: a neighbouring
    store's body whose content landed while its manifest line was still
    queued (the next job in the same FIFO worker) is invisible to a
    project-wide pass run by a DIFFERENT store in that instant; a loss
    in that window also reads as ``EXTERNAL``. Stage ③ (spilled-first
    ordering with un-spilled eviction, owner-confirmation-pending — NOT
    part of this change) is where this stops being derivable at all and
    needs its own record.

    ``OUTSIDE_BOUNDARY`` (#5982): a DIFFERENT failure class from the three
    above — those all mean "the file used to be there and now genuinely
    is not" (normal, an expected design property: an offloaded body may
    be GC'd). This one means the entry's own ``ref`` resolves to a path
    ``MediaStore`` refuses to read at all (outside ``history_content_
    root``/``tool_results_dir`` — ``read_tool_result``'s own boundary
    check, ``PermissionError``) — an ABNORMAL state: something constructed
    a ``ref`` that never should have existed. #5982's own explicit
    condition: this reason must never be folded into ``EXTERNAL`` or any
    other "file missing" reason — mixing "normal" (file legitimately gone)
    with "abnormal" (a malformed/malicious ref reached an internal reader)
    would let the normal case bury the abnormal one from any observer
    reading ``LostReason`` alone (the same "two zeros" shape #5991 ② named
    for a different field). Computed fresh at read time, never persisted —
    same "compute, don't store" discipline #5438 already established for
    ``GC``/``EXTERNAL``, not a new ledger."""

    GC = "gc"
    NEVER_PERSISTED = "never_persisted"
    EXTERNAL = "external"
    OUTSIDE_BOUNDARY = "outside_boundary"

# #5612 (owner ruling — "永続化というのは llm に見える履歴が元に戻らない
# ということ、history.jsonl に追記するということ"): the reactive
# overflow-recovery spill's own durable record — a ``role="spill_record"``
# ChatMessage (see that role's own vocabulary entry below), appended
# ONCE per successfully-spilled turn, that SUPERSEDES the ORIGINAL turn's
# projection (build_history / decompose_history_for_retry both read the
# latest spill_record whose ``SPILL_TARGET_CONTENT_HASH_META_KEY`` matches
# a candidate turn's own content hash — no separate "overlay" object;
# history.jsonl is the ONLY state, matching #5578's own compact()-side
# design: a summary supersedes an earlier span the same way this record
# supersedes one earlier turn). Reuses ``SPILLED_META_KEY``/
# ``CONTENT_REF_META_KEY`` (this is the SAME "spilled, here's the ref"
# vocabulary the write-time cap already stamps on a brand-new entry,
# #5364 §1.2 "D" above — #5612 reuses that shape for a REACTIVE spill of
# an EXISTING turn instead of inventing a second one) — this key is the
# ONLY new one: which turn does this record supersede.
SPILL_TARGET_CONTENT_HASH_META_KEY = "spill_target_content_hash"
# Diagnostic-only (audit/debugging — "which seq did this replace"); never
# read by the projection substitution itself, which matches by content
# hash alone (the same key `is_already_spilled`/the pre-#5612 in-memory
# overlay already keyed by, #5296 PR-2's own architect ruling: "既存spill
# の _offload_content_hash と語彙を揃える").
SPILL_TARGET_SEQ_META_KEY = "spill_target_seq"

# #3299 P4: the intervention PROMPT + resolved ANSWER, stamped on the
# ``role="user"`` history entry ``InterventionHandler.deliver_answer_to``
# already appends (mirroring ``intervention_id`` / ``intervention_kind``
# alongside it). ``InterventionHandler.announce`` never writes to history —
# it only publishes to the outbox — so before this, the QUESTION half of an
# answered intervention did not exist anywhere in ``history.jsonl``; the TUI
# restore projection (``interfaces/inline/textual_chat/restore.py``) could
# not show it after a restart. Rather than inventing a correlation key to
# join a separate prompt record (there is no such record, and P5's
# out-of-order answering makes any GUESSED key a repeat of the #3287/#3299 P2
# "guessed correlation key" defect class), the prompt is folded onto the
# SAME answer record — one history entry is now fully self-contained, no
# correlation needed at all.
#
# ★Untrusted / RAW (#2770 discipline: "the single truth is RAW, neutralize at
# each display boundary"): ``ask_user`` prompts/suggestions come straight
# from a model tool-call, and a selected CHOICE's label is one of those
# model-supplied options too. These three values are stored EXACTLY as
# ``UserIntervention`` carried them (no neutralization at write time) — a
# consumer (restore projection, any future surface) MUST neutralize before
# rendering, never persist a display-shaped (already-neutralized) copy, or
# the audit/restore record stops being the original. The live TUI path's
# equivalent leaf (``intervention_handler._neutralize_terminal`` /
# ``presenter._neutralized_label``) neutralizes at ITS OWN render call site,
# not at persist time — this mirrors that discipline for the restore path.
#
# These NEVER reach the LLM: ``RouterHistoryBuffer._serialise_turn`` builds
# the wire dict from ``role`` / ``content`` / ``tool_calls`` / ``tool_call_id``
# / ``name`` (+ the ``reasoning`` meta sub-key) only — arbitrary ``meta`` keys
# (these three included) are never copied into the payload. So this addition
# costs zero LLM context / tokens; it only grows the PERSISTED
# ``history.jsonl`` (something that was already visible via the outbox
# ``announce`` — this makes it durable, not newly exposed).
INTERVENTION_PROMPT_META_KEY = "intervention_prompt"
INTERVENTION_DETAIL_META_KEY = "intervention_detail"
#: The resolved answer's DISPLAY text — a matched CHOICE's ``label`` (model-
#: supplied, RAW/untrusted) or the raw free-text answer. Needed because a
#: choice-selected answer's own ``ChatMessage.content`` is an EMPTY string
#: (``InterventionHandler.deliver_answer_to`` passes ``text=""`` through the
#: choice-id-override path — the choice id, not a label, is what the wire
#: transport carries) — so ``m.text`` alone cannot reconstruct "what was
#: answered" for a closed-set intervention; this key always carries it.
INTERVENTION_ANSWER_META_KEY = "intervention_answer"

# #3629: stamped on a ``role="tool"`` ``load_skill`` result's persisted entry
# ONLY (``router_loop.py``'s tool-result assembly, mirroring the
# ``TOOL_STATUS_META_KEY`` pattern above — the ONE place that already knows
# a mapper set ``history_text``/``history_meta``, canonical.py's
# ``load_skill_to_canonical``). ``content`` for such an entry keeps
# ``${REYN_SKILL_DIR}``/``${REYN_PLUGIN_ROOT}`` (+ ``CLAUDE_*`` aliases)
# LITERAL rather than baked to an absolute value that a later rename/move
# would freeze forever (history is immutable) — these two keys are what a
# wire-serialise pass (``router_history_buffer.py``'s ``_serialise_turn`` →
# ``reyn.plugins.skill_load.refresh_location_tokens``) needs to re-resolve
# the tokens FRESH, against the CURRENT filesystem, every time the entry is
# replayed.
#
# ``TOKEN_MAP_META_KEY`` is audit-completeness ONLY (#3629 architect
# ruling: LLM-payload trace dumping is opt-in via ``REYN_LLM_TRACE_DUMP``,
# so history is the only ALWAYS-ON record of what a turn's tokens actually
# resolved to at the time) — a wire-serialise pass MUST NOT read substitution
# VALUES from it; it re-derives fresh values from ``SKILL_SOURCE_PATH_META_KEY``
# every time (a frozen value can only repeat what was already stale; only a
# re-resolvable identity can self-heal — see ``refresh_location_tokens``'s
# docstring). Like every other ``meta`` key, this NEVER reaches the LLM
# (``RouterHistoryBuffer._serialise_turn`` builds the wire dict from
# ``role``/``content``/``tool_calls``/``tool_call_id``/``name`` only).
TOKEN_MAP_META_KEY = "token_map"
SKILL_SOURCE_PATH_META_KEY = "skill_source_path"


@dataclass(init=False)
class ChatMessage:
    """Chat-history entry, shaped to mirror the OpenAI/Anthropic message
    list wire format (issue #383 E-full).

    Each ``ChatMessage`` is one entry in the LLM-facing conversation, so
    ``self.history`` can be serialised straight to the LLM without
    synthesis. Tool turns are represented as their own ``role="tool"``
    entries; assistant turns that emitted tool calls carry the
    ``tool_calls`` field; multi-modal user / tool turns use the
    list-of-parts ``content`` shape.

    Role vocabulary:
      - ``user`` — user input
      - ``assistant`` — LLM reply (= previously ``agent``)
      - ``tool`` — tool response (= new)
      - ``system`` — system prompt (rare; usually built at wire time)
      - ``summary`` — chat-compactor output (Reyn-internal; ``build_history``'s
        own projection still filters it out and attaches its content via a
        synthetic bridge turn instead — but ``RouterHistoryBuffer.
        decompose_history_for_retry``'s projection (#5531) includes it
        directly, positioned by ordinary chronological order like any
        other turn, not filtered)
      - ``spill_record`` — reactive overflow-recovery spill's own durable
        supersede record (Reyn-internal, #5612; NEVER reaches the wire —
        excluded from every turns filter in ``router_history_buffer.py``
        the same way ``summary`` is excluded from ``build_history``'s own
        projection, never included anywhere the way ``summary`` is in
        ``decompose_history_for_retry``: this role has no wire shape of
        its own at all, unlike ``summary``'s synthetic bridge turn). See
        ``SPILL_TARGET_CONTENT_HASH_META_KEY``'s own comment above for
        the full contract.
    """
    role: Literal[
        "user", "assistant", "tool", "system", "summary", "spill_record",
    ]
    # ``content`` is either:
    #   - a ``str`` (= text-only turn), or
    #   - a ``list[dict]`` of litellm-style content parts (= multimodal user
    #     turn / tool response with an image / etc.). Each part is e.g.
    #       {"type": "text", "text": "..."}
    #       {"type": "image_url", "image_url": {"url": "<data url OR file ref>"}}
    #       {"type": "image",     "path": "<abs or cwd-rel>",
    #                             "mime_type": "...", "content_hash": "sha256:..."}
    # The last shape (= ``"image"`` with ``path``) is the **path-ref**
    # introduced by #383: storage points at a file on disk, the
    # wire-shape builder reads and embeds the binary at LLM-call time.
    content: str | list[dict] = ""
    ts: str = ""
    seq: int = 0  # monotonic per-session sequence id; #3704: 0 = no coordinate assigned (pre-fix history only — every entry now gets one at persist time, any role)
    meta: dict = field(default_factory=dict)
    # OpenAI/Anthropic tool-turn fields ─────────────────────────────────
    # ``tool_calls`` is set ONLY on ``role="assistant"`` entries where the
    # LLM emitted one or more tool calls. Each block follows the OpenAI
    # function-tool shape:
    #   {"id": "<tool_call_id>", "type": "function",
    #    "function": {"name": "<tool>", "arguments": "<json str>"}}
    tool_calls: list[dict] | None = None
    # ``tool_call_id`` is set ONLY on ``role="tool"`` entries. Links the
    # response back to the originating ``tool_call`` block on the
    # preceding assistant message.
    tool_call_id: str | None = None
    # ``name`` is set ONLY on ``role="tool"`` entries (= function name).
    # Mirrors the OpenAI tool-message ``name`` field; some providers
    # require it for tool-result attribution.
    name: str | None = None
    # #5514 §2: the ONLY declaration point — each `_append_history` call
    # site states what it just produced (a fact vs a deliverable, a
    # frame vs material, hand-typed vs pasted — see #5514's own call-site
    # table), never inferred later from `role` (which cannot express
    # this axis at all — see `Spillability`'s own docstring). Defaults
    # to `Spillability.default()` (`LAST_RESORT`) — see that method's
    # own docstring for why the safe-side default is NOT `NEVER`.
    spillability: Spillability = field(default_factory=Spillability.default)
    # #5678: the ONLY declaration point — required (raises otherwise) on
    # every FRESH ``role="system"`` construction; ``None`` for every other
    # role (the axis does not apply there — see ``Disclosure``'s own
    # docstring). See ``_normalize_disclosure`` for the full contract,
    # including how a pre-#5678 persisted line supplies one via
    # ``_migrate_legacy_chat_message`` rather than this raising.
    disclosure: "Disclosure | None" = None
    # #6093 §1: the ONE declaration point — see ``HistoryEntryKind``'s
    # own docstring and ``_normalize_history_entry_kind`` for the full
    # contract. Defaults to ``HistoryEntryKind.default()``
    # (``UNSPECIFIED``) — omitted at 16 of the 17 existing
    # ``_append_history(`` call sites, all unaffected by this field's
    # addition.
    kind: HistoryEntryKind = field(default_factory=HistoryEntryKind.default)

    def __init__(
        self,
        role: str,
        content: "str | list[dict]" = "",
        ts: str = "",
        seq: int = 0,
        meta: "dict | None" = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
        # #5580: widened to accept a raw ``str`` too — ``ChatMessage(**raw)``
        # from a read-back history.jsonl line passes exactly that shape
        # (spillability was persisted as its ``.value``, #5514). See
        # ``_normalize_spillability``'s own docstring for the full
        # normalization this parameter goes through below.
        spillability: "Spillability | str | None" = None,
        # #5678: see ``_normalize_disclosure`` — required for
        # ``role="system"``, irrelevant (stays ``None``) for every other
        # role.
        disclosure: "Disclosure | str | None" = None,
        # #6093 §1: see ``_normalize_history_entry_kind`` — omitted
        # (``None``) at every call site this PR does not touch, which
        # normalizes to ``HistoryEntryKind.UNSPECIFIED``, never either
        # real member.
        kind: "HistoryEntryKind | str | None" = None,
    ) -> None:
        # Reject the pre-#383 ``"agent"`` spelling. Migration of on-disk
        # ``history.jsonl`` entries happens at load time via
        # ``_migrate_legacy_chat_message``; nothing else should be
        # constructing with ``role="agent"`` anymore.
        if role == "agent":
            raise ValueError(
                "ChatMessage role='agent' was renamed to 'assistant' in "
                "issue #383. Pass role='assistant' instead. "
                "(Legacy on-disk entries are migrated read-time by "
                "_migrate_legacy_chat_message.)"
            )
        self.role = role
        self.content = content
        self.ts = ts
        self.seq = seq
        self.meta = meta if meta is not None else {}
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.name = name
        self.spillability = _normalize_spillability(spillability)
        self.disclosure = _normalize_disclosure(disclosure, role=role, meta=self.meta)
        self.kind = _normalize_history_entry_kind(kind)
        # #5896 P0 (owner-hit, 2026-09-07): a plain instance attribute, NOT
        # a dataclass field — deliberately NO class-level annotation, so
        # `dataclasses.fields()`/`asdict()`/`__eq__`/`repr()` never see it.
        # If it WERE a declared field, `resident_bytes()`'s own
        # `asdict(self)` call below would include this cache slot in what
        # it measures, drifting the computed size from what the pre-fix
        # `json.dumps(asdict(m))` call (still used verbatim as the
        # equivalence baseline in tests) would have produced.
        self._resident_bytes_cache: "ResidentBytes | None" = None
        # #5973 BLOCKING (lead-coder review, issuecomment-5578156411):
        # same "plain instance attribute, no class-level annotation" shape
        # as `_resident_bytes_cache` above, and the same reason (kept out
        # of `asdict(self)`/`__eq__`/`repr()`). A SEPARATE bool from the
        # cache value itself — `None` is a legitimate DERIVED result for
        # `body_bytes()` (ref missing / no store / file gone), unlike
        # `_resident_bytes_cache` where `None` unambiguously means
        # "not yet computed" — so this cache cannot reuse that sentinel.
        self._body_bytes_cache: "BodyBytes | None" = None
        self._body_bytes_cached: bool = False

    def resident_bytes(self) -> ResidentBytes:
        """This message's own serialized size in bytes — computed the
        FIRST time this is called, cached for the rest of this object's
        lifetime. Owner-hit incident (2026-09-07): ``Session.
        _evict_oldest_resident_entries`` used to run
        ``len(json.dumps(asdict(m), ensure_ascii=False).encode("utf-8"))``
        for EVERY resident message on EVERY eviction pass (every append) —
        a single 369 MB row on the owner's real history made that ONE
        re-serialize cost another 369 MB copy, on the hot append path.

        Caching here is safe (never goes stale) because nothing in this
        codebase in-place-mutates ANY field ``asdict(self)`` can see
        (``role`` / ``content`` / ``ts`` / ``seq`` / ``meta`` /
        ``tool_calls`` / ``tool_call_id`` / ``name`` / ``spillability`` /
        ``disclosure`` / ``kind`` — all 11 dataclass fields, not just
        ``content``/``meta``) AFTER a message becomes resident.
        Confirmed by reading every in-place write to any of them across
        the whole of ``src/`` (lead-coder review, PR #5945 BLOCKING —
        widened from an earlier draft that only checked ``.content =``/
        ``.meta[...] =``, missing the one below), not assumed. All THREE
        that exist run strictly BEFORE the write that first makes a
        message resident (``self.history.append(msg)``,
        ``Session._append_history``):

        - ``msg.seq = self._next_seq`` (``session.py:4267``)
        - ``msg.meta["wal_seq"] = ...`` (``session.py:4278``, the
          ``wal_seq`` stamp)
        - ``msg.content = resolve_history_content(...)``
          (``session.py:5039``, ``_parse_history_line``'s ref-resolve, on
          a freshly-parsed message before its own later append)

        A 4th candidate, ``router_loop.py:4496``'s
        ``result.tool_calls = tcs[:cap]``, mutates a completion RESULT
        object before ``ChatMessage.__init__`` ever runs on it — not a
        resident ``ChatMessage`` field write at all. A message built via
        ``dataclasses.replace()`` goes back through ``__init__`` (this
        cache is reset to ``None`` there), so a copy never inherits a
        stale value from the original. If a future change adds an
        in-place write to any of the 11 fields AFTER a message is
        resident, this cache goes silently stale — re-run this same
        search (``.content =`` / ``.meta[...] =`` / ``.seq =`` / etc.,
        across ``src/``, not just ``runtime/``) before trusting it
        again."""
        if self._resident_bytes_cache is None:
            self._resident_bytes_cache = ResidentBytes(len(
                json.dumps(asdict(self), ensure_ascii=False).encode("utf-8"),
            ))
        return self._resident_bytes_cache

    def body_bytes(self, media_store: Any) -> "BodyBytes | None":
        """#5973 BLOCKING (lead-coder review, issuecomment-5578156411,
        follow-up to issuecomment-5578035547): this content_ref row's
        real body size — from ``CONTENT_BYTES_META_KEY`` when present
        (every row a PRODUCTION write mints since #5896 carries one), or
        DERIVED via a stat (``media_store.read_tool_result_preview``'s
        own ``os.path.getsize``, ``max_bytes=0``) when it is not — a
        ``reyn storage migrate-bodies`` (#5947) migrated row stamps
        ``CONTENT_REF_META_KEY`` but never ``CONTENT_BYTES_META_KEY``,
        and owner's own real history is entirely this population.

        Computed the FIRST time this is called, cached for the rest of
        this object's lifetime — the SAME shape :meth:`resident_bytes`
        uses, for the same reason and then some: ``Session.
        _evict_oldest_resident_entries`` runs on EVERY append (not just
        once), scanning every resident row, so an un-memoized stat here
        would ``open()`` a migrated row's backing file once per resident
        migrated row PER APPEND — O(resident ref count) file opens,
        repeated on every single turn (lead-coder's own measurement:
        the exact class this PR's own fix for BLOCKING-1 introduced by
        deriving via stat without caching it).

        Caching here is safe (never goes stale) for the SAME reason
        :meth:`resident_bytes` documents (nothing in this codebase
        in-place-mutates ``meta`` after a message becomes resident) PLUS
        one more: a ``content_ref``'s target file is content-addressed
        and write-once (``MediaStore.save_tool_result`` never overwrites
        an existing ref's file) — so the SIZE a ref names is an immutable
        fact from the moment this row is parsed, not merely
        un-mutated-so-far. ``media_store=None`` on the first call caches
        ``None`` permanently — safe in production (a session's
        ``_media_store`` is set once at construction and never swapped
        mid-lifetime); a test double that first calls this with no store
        and later wants a real derivation must construct a fresh message
        instead of expecting a second call to see a different store.

        Returns ``None`` (never raises) for every "unknown" case: the ref
        is absent, ``media_store`` is unset, the backing file is missing
        (``found=False``), OR the ref names a path OUTSIDE ``media_
        store``'s own boundary — folded to ``found=False`` by ``media_
        store``'s own internal-reader entrypoint (#5982:
        :meth:`~reyn.data.workspace.media_store.MediaStore.read_tool_
        result_preview_for_internal_reader`, which LOGS the fold
        distinctly rather than silently dropping the "why" the way a
        bare ``try/except PermissionError`` here used to). This runs on
        the hot append path (:meth:`Session._evict_oldest_resident_
        entries`, called from every :meth:`Session._append_history`) —
        an uncaught raise for one out-of-boundary row would make every
        future append fail, turning a degrade into a hard stop."""
        if not self._body_bytes_cached:
            self._body_bytes_cache = self._derive_body_bytes(media_store)
            self._body_bytes_cached = True
        return self._body_bytes_cache

    def _derive_body_bytes(self, media_store: Any) -> "BodyBytes | None":
        meta = self.meta or {}
        ref = meta.get(CONTENT_REF_META_KEY)
        if not ref:
            return None
        stamped = meta.get(CONTENT_BYTES_META_KEY)
        if isinstance(stamped, int):
            return BodyBytes(stamped)
        if media_store is None:
            return None
        # #5973 BLOCKING (lead-coder review, issuecomment-5578223027): a
        # ref outside media_store's own boundary makes read_tool_result_
        # preview raise PermissionError, not return found=False — and
        # this is now on the hot append path (body_bytes() is called from
        # _pull_weight, Session._evict_oldest_resident_entries, which
        # Session._append_history runs on EVERY append). Uncaught, ONE
        # out-of-boundary ref would make every future append raise,
        # turning a degrade (this PR's own "unknown -> treated safely by
        # ①②③") into a hard stop.
        #
        # #5982: switched from a bare try/except around the raising form
        # to MediaStore's own internal-reader entrypoint — the earlier
        # local fold (still shown in this comment's own history) silently
        # dropped the "why" the moment PermissionError was caught here;
        # the new entrypoint still folds to the SAME "unknown" None every
        # other branch here returns, but now LOGS the fold distinctly
        # from an ordinary not-found (see that method's own docstring)
        # instead of the defect (a ref outside the boundary reached an
        # internal reader) going permanently unobserved.
        _head, found, total_bytes = media_store.read_tool_result_preview_for_internal_reader(
            ref, max_bytes=0,
        )
        return BodyBytes(total_bytes) if found else None

    @property
    def text(self) -> str:
        """Derived view returning a str representation of ``content``.

        - str content → returned as-is.
        - list-of-parts content → the first ``{"type":"text"}`` part's text.
        - neither → empty string.

        This is a convenience accessor, NOT a legacy compatibility shim:
        readers that want a textual rendering of any ChatMessage (text or
        multimodal) call ``m.text`` instead of branching on isinstance.
        Writers update ``content`` directly.
        """
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            for part in self.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
        return ""


# ── Legacy ChatMessage migration ───────────────────────────────────────
#
# history.jsonl files written before issue #383 used the pre-Design-B
# shape: ``role`` ∈ {"user","agent","summary"}; ``text:
# str``; ``media: list[dict]`` (= inline base64 image_url parts from
# #366). On load, ``_migrate_legacy_chat_message`` rewrites such
# entries into the new wire shape so the runtime only ever sees
# Design-B ChatMessage instances.


def _migrate_legacy_disclosure(raw: dict) -> dict:
    """#5678: read-time migration for a ``role="system"`` line persisted
    BEFORE ``disclosure`` existed (every ``history.jsonl`` line written
    before this issue shipped, new-shape #383 lines included — this is
    orthogonal to the #383 legacy-text-shape migration above).

    Computes the value the OLD role/meta-based logic would have used,
    so ``ChatMessage.__init__``'s hard requirement (no default, raises
    for ``role="system"`` with no ``disclosure``, see ``Disclosure``'s
    own docstring) never fires for a record that predates the axis —
    only for a FRESH call site that forgot to declare:

      - ``meta.kind == "turn_cancelled"`` → ``OPERATOR`` (exactly
        what ``restore.py``'s pre-#5678 ``meta.get("kind") ==
        "turn_cancelled"`` rescue already singled out — #3694).
      - anything else → ``INTERNAL`` (exactly what the pre-#5678
        ``_SKIP_ROLES`` blanket skip, and the pre-#5678
        ``build_history``/``decompose_history_for_retry`` role
        allowlist, already did for every OTHER ``system`` entry —
        ``state_change``, hook pushes, ride-alongs, SP chrome alike).

    Does nothing if ``disclosure`` is already present (a line written
    BY #5678-aware code, or already migrated) or the role is not
    ``"system"`` (the axis does not apply — ``ChatMessage.__init__``
    would ignore any value here anyway)."""
    if raw.get("role") != "system" or "disclosure" in raw:
        return raw
    raw = dict(raw)
    meta = raw.get("meta") or {}
    raw["disclosure"] = (
        "operator"
        if isinstance(meta, dict) and meta.get("kind") == "turn_cancelled"
        else "internal"
    )
    return raw


def _migrate_legacy_chat_message(raw: dict) -> dict:
    """Read-time migration for pre-#383 history.jsonl entries.

    Detects the legacy shape (= ``text`` key + optional ``media`` list,
    ``role="agent"`` for assistant replies) and emits the Design-B
    shape (= ``content`` field, ``role="assistant"``). Mutates a copy;
    the caller hands the result to ``ChatMessage(**kwargs)``.

    Legacy → new:
      role: "agent"            → "assistant"
      text: "hi"               → content: "hi"
      text + media: [...]      → content: [{"type": "text", "text": "hi"}, ...media]
      (no text, media: [...])  → content: [...media]

    Inline base64 in media blocks is left alone — those entries
    pre-date the path-ref design and rewriting them to files would
    be a one-shot tool, out of scope for read-time migration.

    Also runs the #5678 ``disclosure`` migration (:func:`_migrate_legacy_disclosure`)
    — a SEPARATE axis from the #383 shape migration this function is
    named for, folded into the same read-time call site rather than a
    second pass over every line, since ``Session._parse_history_line``
    already calls this exactly once per line before constructing.
    """
    raw = dict(raw)  # don't mutate the caller's dict
    if "content" in raw:
        # Already new shape (= written post-#383 or already migrated).
        # Still normalise role just in case "agent" snuck in.
        if raw.get("role") == "agent":
            raw["role"] = "assistant"
        return _migrate_legacy_disclosure(raw)

    # Legacy shape: text + optional media.
    text_val = raw.pop("text", "")
    media_val = raw.pop("media", None) or []

    if media_val:
        parts: list[dict] = []
        if text_val:
            parts.append({"type": "text", "text": text_val})
        parts.extend(media_val)
        raw["content"] = parts
    else:
        raw["content"] = text_val

    if raw.get("role") == "agent":
        raw["role"] = "assistant"
    return _migrate_legacy_disclosure(raw)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
