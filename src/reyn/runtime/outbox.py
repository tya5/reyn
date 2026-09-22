"""OutboxMessage — structured payload for Session's display stream.

Replaces the previous (kind, text) tuple. Provenance fields (run_id,
actor, intervention_id, …) live in `meta: dict` rather than as fixed
attributes, so future additions (e.g. `agent_id` for multi-agent sessions)
don't require dataclass schema changes. This mirrors the `ChatMessage.meta`
convention already used for history entries.

Outbox is the **presentation stream**, distinct from history (durable log).
- agent → also persisted to history.jsonl by Session
- status / error / trace / intervention → display-only, never in history
- __end__ → control signal for _output_loop shutdown

**Closed kind vocabulary (ADR-0039 P6b).** ``kind`` is drawn from a CLOSED set
(:data:`DISPLAY_KINDS` ∪ :data:`CONTROL_KINDS`), validated at construction in
:meth:`OutboxMessage.__post_init__`. A kind outside the vocabulary would leak an
unprofiled ``CUSTOM`` name on the AG-UI wire (the P6a disposition-gate concern);
fail-visible at construction catches the helper/dynamic constructions a static
scan misses. The validation is **production-side ONLY** — the AG-UI decode path
rebuilds an OutboxMessage from an UNTRUSTED remote frame and must degrade
gracefully on an unknown wire kind (ignore-unknown, never fail-close), so it uses
:meth:`OutboxMessage.from_wire`, which bypasses the vocabulary check.

**Sentinel-family text invariant (#6230 stage 3).** Every ``__``-prefixed
kind in the vocabulary except ``__end__`` (see :data:`_SENTINEL_FAMILY_KINDS`)
additionally REQUIRES a non-empty ``text`` at construction — the same
"identity at construction time, never recovered later" shape #5047 already
established for the intervention family, applied to stage 1/2's own subject
(a legible representation) instead of an intervention id.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.transport import TransportRef

# ── the closed kind vocabulary ───────────────────────────────────────────────
# ★ This is the DISPLAY vocabulary — how to render a frame. It is not the inbox
# vocabulary, which answers who authored a turn's text and lives in
# ``runtime.turn_origin.TurnOrigin``. Both are called ``kind``, and until #3595
# they shared exactly one word, ``"user"`` — the display kind below and the inbox
# claim that a human typed the line, unrelated to each other and indistinguishable
# by grep. The inbox member is now spelled ``CLIENT_INPUT``, so the two symbol sets
# no longer intersect; the value here is unchanged and means what it always did.
#
# The settled disposition of every producer kind (P6a): standard 4 / profiled 11
# / control 2. This module DECLARES the vocabulary independently; the
# non-circular gate (tests/interfaces/test_outbox_vocabulary.py +
# tests/interfaces/test_agui_profile_completeness.py) binds it against the real codec map
# (protocol._DISPLAY_KIND_EVENT), the extension profile (profile.CUSTOM_PROFILE),
# and the wire-filter allowlist (protocol.CONTROL_FILTER_KINDS).

# standard-mapped (4): the codec emits a STANDARD AG-UI event (a generic client
# renders it) — no reyn.* CUSTOM name. (protocol._DISPLAY_KIND_EVENT non-CUSTOM.)
_STANDARD_DISPLAY_KINDS: "frozenset[str]" = frozenset({
    "agent",      # → TEXT_MESSAGE_CONTENT (role assistant)
    "status",     # → TEXT_MESSAGE_CONTENT (role status)
    "reasoning",  # → REASONING_MESSAGE_CONTENT
    "error",      # → RUN_ERROR
})

# profiled (10): the codec emits a reyn.display.<kind> CUSTOM event that has an
# extension-profile entry (profile.CUSTOM_PROFILE). Renderer chrome with no
# standard AG-UI analog. INCLUDES the two client-consumed control sentinels
# that are FORWARDED on the wire (not filtered) — the CLIENT consumes them over
# the transport stream, so filtering them would make remote /copy · /rewind
# silent no-ops (they ride as reyn.display.* CUSTOM, round-trip losslessly).
_PROFILED_DISPLAY_KINDS: "frozenset[str]" = frozenset({
    "intervention",         # native prompt UI (answer round-trip via reyn.intervention.*)
    "intervention_resolved",  # #5057 axis B: an ALREADY-ANSWERED intervention —
                              # replayed from history or folded in place after a
                              # live answer. Never routes to the panel (never a
                              # frontend-tool round-trip) — the sibling kind IS
                              # the resolved/pending discriminator, so nothing
                              # downstream needs to read meta to tell the two
                              # apart.
    "presentation",         # a present op's text; render-node model on _reyn meta.nodes
    "user",                 # a user-authored line echoed live to the scrollback
    "system",               # persisted lifecycle/status chrome (compaction / budget / cost-warn)
    "trace",                # a nested detail / trace line (dim, transient)
    "tool_call_started",    # tool-call start trace line
    "tool_call_completed",  # tool-call completion trace line
    "tool_call_failed",     # tool-call failure trace line
    "__copy_last_reply__",  # /copy sentinel — client-side clipboard copy (repl._copy_sentinel.handle_copy_sentinel)
    "__rewind_list__",      # /rewind sentinel — client renders the rewind list / region picker
})

# Every kind FORWARDED to the AG-UI wire as a display frame (standard or CUSTOM).
DISPLAY_KINDS: "frozenset[str]" = _STANDARD_DISPLAY_KINDS | _PROFILED_DISPLAY_KINDS

# control-filtered (1): emitter-FILTERED control sentinels
# (== protocol.CONTROL_FILTER_KINDS) — consumed as signals, NEVER forwarded on
# the wire. Each documented with its consumption locus:
CONTROL_KINDS: "frozenset[str]" = frozenset({
    # Stream terminator: OutboxHub._drain / registry._forwarder /
    # _SessionFrameSource loops all return on it; the AG-UI emitter returns
    # (ends the SSE stream). Never rendered.
    "__end__",
    # #4482 PR-3: `/open <ref>` sentinel — client-side ref-resolve + OS-launch
    # (interfaces.inline.textual_chat.app._handle_open_artifact_request).
    # Control-filtered (unlike /copy · /rewind's PROFILED forwarding) because
    # "launch a local application" is local-only by construction — remote
    # handling is explicitly deferred (owner ruling, #4482: "ローカル前提で
    # 進めてください", remote tracked separately as #4494) — forwarding this
    # on the wire today would just be a sentinel no remote client has a
    # handler for, the exact silent-no-op shape the /copy·/rewind comment
    # above warns about, not a real remote capability.
    "__open_artifact__",
})

# The complete closed vocabulary of valid OutboxMessage.kind values.
VOCABULARY: "frozenset[str]" = DISPLAY_KINDS | CONTROL_KINDS


def is_unknown_kind(kind: str) -> bool:
    """Whether *kind* falls outside the closed display vocabulary
    (:data:`DISPLAY_KINDS`) — the population #6230 stage 2's degrade
    (:func:`legible_degrade_text`) and witness (``reyn.interfaces.repl.
    renderer.record_unknown_kind_frame``) exist for.

    The three CONTROL kinds (``__end__`` / ``__copy_last_reply__`` /
    ``__open_artifact__``) are intercepted BY NAME in each caller's own
    frame loop before reaching a generic fallback — this function is
    never evaluated for them in practice. It answers the general
    question ("is this kind in the closed vocabulary at all"), not "is
    this specifically one of the sentinels callers already special-case
    by name."

    Lives here, not in ``interfaces/repl/renderer.py`` where it was first
    drafted (PR #6238 review, architect + lead-coder): :func:`_encode_display`
    (``interfaces/transport/agui/protocol.py``) needs it too, and that
    module must not import ``renderer.py`` — a wire CODEC importing a
    ``prompt_toolkit``-carrying presentation module is a layering
    inversion this module (the shared runtime vocabulary both already
    depend on) does not have. ``renderer.py`` re-exports this name so
    every existing caller's import path is unchanged.
    """
    return kind not in DISPLAY_KINDS


def legible_degrade_text(kind: str, text: str) -> str:
    """#6230 stage 2 (issue thread ruling): the line a surface shows for a
    frame it has no specific presentation for — including a ``kind`` it
    does not recognize at all. Three tiers, tried in order, so the result
    is STRUCTURALLY impossible to be empty (never "we call a function
    that outputs text" — the invariant is enforced by this function's own
    shape, not by hoping every caller remembers to check):

    1. ``text`` is non-empty → shown verbatim (the common case: a real
       reply, a real status line, or — since stage 1, #6235 — a sentinel's
       own prepared human-readable fallback).
    2. ``text`` is empty but ``kind`` is not → a line naming the kind, so
       a reader at least knows WHAT arrived even with nothing to say
       about it.
    3. Both empty → a fixed line. This is the tier that makes "empty"
       unreachable: there is no fourth case left to fall through.

    ``OutboxMessage.from_wire``'s own docstring already states the
    decision this makes true: an unknown wire kind "MUST degrade
    gracefully (ignore-unknown), never fail-close" — this function is
    what "gracefully" cashes out to at the point text actually reaches a
    human, not a new decision.

    ``__end__`` (the one CONTROL kind with ``text=""`` BY CONSTRUCTION,
    ``transport/agui/endpoint.py``) never reaches this function: every
    caller's own frame loop returns/breaks on ``kind == "__end__"``
    before rendering anything (``textual_chat/app.py``'s pump,
    ``repl/stream_client.py``'s output loop), and it is CONTROL-FILTERED
    before ever reaching ``_encode_display`` on the wire path
    (``transport/agui/emitter.py``'s ``CONTROL_FILTER_KINDS`` check) — so
    tier 3 firing for ``__end__`` specifically is structurally
    unreachable on every call site, not excluded by a literal check here.

    Applied at TWO points, deliberately, not one (PR #6238 review,
    architect + lead-coder): ⑴ ``_encode_display`` (the wire codec) so a
    REMOTE consumer — a generic AG-UI client, or the openui browser —
    never needs its own copy of this logic (a JS twin of this exact
    function used to live in ``index.html`` and was removed once this
    became the single source); ⑵ each IN-PROCESS renderer (the inline
    TUI's presenter, the plain ``--cui`` renderers) still calls this
    directly, because an in-process consumer never goes through
    ``_encode_display`` at all — there is no single funnel upstream of
    both.
    """
    if text:
        return text
    if kind:
        return f"(unrecognized frame: kind={kind!r}, no text)"
    return "(an unreadable frame arrived)"


# #5047/#5057 (structural fix, architect's confirmed design — axis A): the
# intervention-family kinds — every one of these REQUIRES
# ``meta["intervention_id"]`` to be a genuine identity, checked by
# :meth:`OutboxMessage.__post_init__` (in-process construction) and
# demoted-around by :meth:`OutboxMessage.from_wire` (untrusted wire
# construction, which cannot fail-close).
#
# #5057 axis B deliberately does NOT add ``"intervention_resolved"`` here.
# Identity is required because a frame in this family can still be ANSWERED
# — the id is the correlation anchor ``answer_intervention_by_id`` needs.
# An already-resolved frame is never answered again, so it carries no such
# requirement; giving it its OWN kind (rather than growing this set) is
# what lets ``_ingest_frame``'s registration guard become a bare
# ``kind == "intervention"`` check with no meta inspection at all — the
# sibling kind IS the resolved/pending discriminator now, so this set stays
# exactly ``{"intervention"}`` rather than "growing" the way an earlier
# draft of this comment expected.
_INTERVENTION_FAMILY_KINDS: "frozenset[str]" = frozenset({"intervention"})

# #6230 stage 3 (issue thread ruling, architect + lead-coder): the SAME
# structural-fix shape #5047 established two paragraphs up — "must carry its
# own identity at construction time, never recovered later by position or
# absence" — applied to stage 1/2's own subject (a legible ``text``) instead
# of an intervention's ``intervention_id``.
#
# ★ Population, NOT a hand-maintained literal list: every ``__``-prefixed
# member of :data:`VOCABULARY`, MINUS the one name below. It is DERIVED from
# the vocabulary the ``__post_init__`` gate two members down already
# enforces — a kind cannot reach this class's own ``__init__`` at all
# unless it is already in VOCABULARY (ADR-0039 P6b, pre-existing), so this
# set can never miss a future sentinel added to DISPLAY_KINDS or
# CONTROL_KINDS the way a fresh ``git grep`` of this file alone would (that
# grep is exactly the needle architect's own review named as too narrow —
# it sees a literal in ``outbox.py``, never a kind defined or built
# elsewhere, and never a dynamically-assembled one). Adding a new
# ``__``-prefixed kind to either frozenset above enrolls it here
# AUTOMATICALLY, with no second edit required — that is what "closed by
# construction" means for this population specifically.
#
# The one exclusion, ``"__end__"``, is irreducible — not a growing list,
# a single named carve-out with its own reason: it is not a slash command's
# response (the observable discriminator this arc settled on) but the
# stream TERMINATOR, and its ``text`` is ``""`` BY CONSTRUCTION at its one
# call site (``transport/agui/endpoint.py:1002``, unconditionally). Every
# caller's own frame loop returns/breaks on ``kind == "__end__"`` before
# any rendering is attempted (the same fact :func:`legible_degrade_text`'s
# own docstring already documents) — forcing a legible ``text`` onto it
# would fabricate a human sentence for a frame that, by design, carries
# none (this arc's own repeated "never a minted placeholder" ruling). A
# FUTURE purely-transport-control ``__``-prefixed kind would need this same
# reasoning restated for its own name before joining the exclusion — this
# module does not pre-guess what that kind might be.
_SENTINEL_FAMILY_KINDS: "frozenset[str]" = frozenset(
    kind for kind in VOCABULARY if kind.startswith("__") and kind != "__end__"
)

# #6184: the SINGLE enumeration of OutboxMessage's wire-safe fields is
# "every dataclass field except the ones named here" — derived by
# :meth:`OutboxMessage.to_wire_dict`/:meth:`OutboxMessage.from_wire` from
# ``dataclasses.fields()``, never hand-typed at either the encode
# (agui/protocol.py) or decode (this module's own ``from_wire``) side. A
# field gained later is wire-safe automatically; a field that must NOT
# cross the wire is excluded by adding its name HERE, with the reason
# recorded in this comment — never by writing a third, independent list
# somewhere else.
#
# ``reply_to`` (FP-0013): a ``TransportRef`` is a process-local runtime
# routing object — ADR-B (``transport.py``'s own module docstring):
# "refs are purely runtime objects in this implementation -- they do NOT
# survive crash recovery". It names a surface INSIDE this process (the
# local TUI, one in-flight MCP request, one in-flight A2A request) that a
# remote AG-UI client cannot consume or usefully echo back, and the
# ``TransportRef`` union has no wire discriminator tag to reconstruct the
# right variant from a dict (``TuiRef``/``McpRef``/``A2aRef``/... share no
# common field to switch on) — sending it would need that reconstruction
# machinery built first, for no reader that exists today. This was
# already true before #6184 (the pre-existing hand-written encode side
# never included it); #6184 only makes the omission a DECLARED exclusion
# instead of a fact recoverable only by reading which of two independent
# lists happened to be shorter.
_NON_WIRE_FIELDS: "frozenset[str]" = frozenset({"reply_to"})


def _dataclass_field_default(f: "dataclasses.Field") -> object:
    """The value :meth:`OutboxMessage.from_wire` gives a field whose wire
    key is ABSENT — an excluded field (currently only ``reply_to``) always
    takes this path; any other field takes it only when the wire dict
    genuinely omits that key (not merely an unrecognised extra key, which
    is a different, already-tolerated case). Reads the field's OWN
    declared default/default_factory rather than a value hand-typed here
    a second time, so a future field with a declared default needs no
    change to this function.

    #6184 BLOCKING round 2 (lead-coder, measured): a field with NEITHER a
    declared default NOR a default_factory (today: ``kind``/``text``,
    ``from_wire``'s two REQUIRED core content fields) does NOT fall back
    to ``None`` here. ``None`` is not a graceful degrade for a
    ``str``-typed field — it only RELOCATES a fail-close into a
    fail-FAR-AWAY ``TypeError`` at whichever consumer calls a ``str``
    method on the result (``agui/protocol.py``'s own ``str(wire["text"])``
    conversion did not crash only because ``str(None) == "None"`` happens
    to be a legal call — a different consumer would not be so lucky).
    Falls back to ``""`` instead — the SAME choice ``from_wire``'s own
    pre-existing ``kind = str(wire.get("kind") or "")`` line already
    makes for the OTHER no-default field: one rule for "no-default
    field", not a different value invented for ``text`` alone. A FUTURE
    no-default field of a non-``str`` type would be a new REQUIRED
    argument on a frozen, wire-facing dataclass — adding one is already a
    breaking change to every existing construction call site and
    warrants its own review of what ITS missing-value fallback should
    be; this function does not pre-guess that case."""
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()
    return ""


# #6184 段2a-1 (architect design, issuecomment-5681485023; lead-coder
# ruling, issuecomment-5681510010): the producer-declared half of the
# structured-tool-entry vocabulary — deliberately 2 members, NOT the 4
# census landing shapes (NEST/FLAT-bypass/UPDATE-in-place/DROP). A
# producer cannot know whether its entry will render nested (that is
# the RENDERER's decision, made from whether a parent_id was given, not
# something the producer is positioned to predict — the same "producer
# doesn't know the shape of its own audience" failure this arc's own
# census caught 4 times already). DROP is never a producer declaration
# either — D1 (app.py) is a CONSUMER's dedup judgment on an already-
# emitted entry, not something a producer would ever assert about
# itself. See OutboxMessage.operation's own field comment for how NEW/
# UPDATE map back onto the full 4-shape landing.
class Operation(StrEnum):
    """#6184 段2a-1: which of the two things a producer can assert about
    one structured entry — create a fresh one, or change an existing
    one. ``StrEnum`` for the same reason ``Spillability``/
    ``HistoryEntryKind`` (chat_message.py) are: a value that reaches the
    wire (via :meth:`OutboxMessage.to_wire_dict`) must serialise to its
    own string, and a value read back off it must still compare equal
    to the member.
    """

    #: A fresh entry. Nests under `parent_id` when one is given, lands
    #: flat (top-level) when it is not — the SAME declaration either
    #: way; nesting is the renderer's decision, not a second value here.
    NEW = "new"
    #: An existing entry (named by `id`) changes. Carries the entry's
    #: NEW value, not a diff — #6184's own census found a landing shape
    #: (C7, app.py `_handle_intervention_answer_event`) that rewrites
    #: `kind` itself on an already-emitted entry; a diff-only UPDATE
    #: could not express that without a special case carved out just
    #: for C7. This field never records that a landing was DROPPED
    #: (D1, app.py) either — D1 is a CONSUMER's dedup judgment on an
    #: already-emitted entry, not a producer declaration.
    UPDATE = "update"

    @classmethod
    def default(cls) -> "Operation":
        """#6184 段2a-1: the safe-side default is `NEW` — every
        `OutboxMessage` construction that predates this field (the
        overwhelming majority, since nothing produces `operation` yet
        in this stage) IS, retroactively and accurately, a fresh entry,
        never a change to one that already existed. Unlike
        `HistoryEntryKind.UNSPECIFIED` (chat_message.py), which must
        NOT retroactively claim either of ITS two real members are
        true, `NEW` genuinely is what an un-declared entry always was
        — so there is no third, neutral member here to fall back to
        instead."""
        return cls.NEW


def _normalize_operation(value: object) -> Operation:
    """#6184 段2a-1: the ONE normalization point for `operation` — the
    SAME degrade-never-raise shape `_normalize_spillability` (this
    module's own sibling pattern in chat_message.py) and
    `_normalize_history_entry_kind` already use, not an invented one
    (lead-coder, dispatch: "既存 pattern に倣ってください。独自の扱いを
    発明しないこと").

    - ``None`` (omitted at a call site — every current one, since
      nothing produces this field yet) → :meth:`Operation.default`.
    - Already an ``Operation`` member → passed through unchanged (an
      ``Operation`` member IS ALSO a ``str``, via ``StrEnum`` — the
      ``isinstance`` order below matters for the same reason
      ``_normalize_spillability``'s own docstring names).
    - A plain ``str`` naming a real member (the wire-decoded case,
      ``from_wire`` bypasses ``__post_init__`` and so must normalize
      here explicitly too) → converted to that member.
    - Anything else — an unrecognised string (a future value this
      version's enum doesn't have yet, or a malformed wire/history
      value) — degrades to :meth:`Operation.default` rather than
      raising. `from_wire`'s own founding rule (never fail-close on
      untrusted wire data) applies identically here."""
    if value is None:
        return Operation.default()
    if isinstance(value, Operation):
        return value
    if isinstance(value, str):
        try:
            return Operation(value)
        except ValueError:
            return Operation.default()
    return Operation.default()


def _derive_id_and_parent_id(*, kind: str, meta: dict) -> "tuple[str | None, str | None]":
    """#6184 段2a-2: the ONE derivation point for `id`/`parent_id` on the
    in-process construction path (:meth:`OutboxMessage.__post_init__`) —
    zero producer diff, every existing construction call site keeps
    working unchanged. `from_wire` does NOT call this — a wire-decoded
    value's `id`/`parent_id` is whatever the ORIGIN process already
    derived, passed through as-is (the same direction #6184's own
    `id` field already established in 段2a-1).

    The original 4-branch rule below faithfully REPRODUCES today's real
    consumer (``app.py``'s :meth:`_resolve_append_parent`/:meth:`_register_call_
    parent`) — it invents nothing new. #6213 added 2 branches AHEAD of
    it (checked first, both gated on `dispatch_id` presence — see that
    branch's own comment at the call site for why this is a NEW
    identity source in the SAME `kind:value` vocabulary, not a 6th
    identifier kind #6186 already ruled against). #6186's own compaction
    UPDATE axis adds 2 MORE, checked FIRST of all (ahead of #6213's own
    2 — accept④'s own witness that they cannot catch a non-compaction
    frame, since a message either carries `compaction_episode_seq` or it
    falls straight through to the table below, unchanged):

    | condition                                | id                | parent_id         |
    |-------------------------------------------|-------------------|--------------------|
    | `compaction_episode_seq` + `compaction_episode_marker` | — | `episode:{compaction_episode_seq}` |
    | `compaction_episode_seq`, no `compaction_episode_marker` | `episode:{compaction_episode_seq}` | — |
    | `dispatch_id` + `kind=="tool_call_started"`| `tool:{dispatch_id}` | `call:{call_id}` (unchanged) |
    | `dispatch_id` + `kind in (completed, failed)` | — | `tool:{dispatch_id}` |
    | `call_id` + `kind=="agent"`         | `call:{call_id}`| `turn:{chain_id}` |
    | `call_id` + other `kind`            | —               | `call:{call_id}`  |
    | no `call_id`, `kind != "user"`      | —               | `turn:{chain_id}` |
    | `kind == "user"`                    | `turn:{chain_id}`| —                |

    ① ``call_id`` is the litellm RESPONSE's own ``id`` — a THIRD PARTY's
    identifier, not something reyn mints (``llm.py``'s own
    ``_response_call_id``, verbatim: "The litellm response's own
    ``id``"). That SAME docstring names the exact risk this function's
    own key-namespacing guards against, verbatim: a consumer keying on
    ``call_id`` "would otherwise BUNDLE UNRELATED CALLS AS ONE" if two
    genuinely different calls ever shared a reported id — the `call:`
    prefix does not fix that risk (a third party's own uniqueness is
    still a third party's property, not reyn's to assert), it only
    keeps THAT risk in its own namespace, separate from `turn:`'s.

    ② The two remaining risks are NOT the same severity, and must not
    be read as if they were: ``args_hash`` (``dispatcher.py``'s own memo
    key) collides BY DESIGN — collision IS its purpose (resume
    memoization: same args, same key, on purpose). A litellm response
    id repeating across two genuinely different calls would be a
    provider-side ACCIDENT outside reyn's own design, not a designed
    property of anything reyn built. `id`'s own "unique per issuance"
    contract (段2a-1) is inherited FROM the provider for `call:`-prefixed
    values, not asserted independently by this function.

    ③ A ``kind="agent"`` row with NO ``call_id`` declares no `id` at
    all — never `""`, never a fabricated placeholder. This is the SAME
    judgment :meth:`_register_call_parent`'s own ``if kind != "agent"
    or not call_id: return`` already makes: such a row does not own a
    call-level group. An empty-string `id` would be a SHARED key every
    such row collides on — the exact hazard ``_response_call_id``'s own
    docstring warns against, reproduced by this function instead of
    avoided by it, if a bare falsy check were skipped here.

    ``chain_id`` gets the identical no-key-when-absent treatment for
    the same reason — a turn row promoted before #4691 arc item ④
    started stamping ``chain_id`` (or any other caller a future change
    might add) must not collide with every other keyless row on a
    fabricated shared ``"turn:"`` value either.
    """
    # #6186 (compaction UPDATE axis): `episode:{compaction_episode_seq}` —
    # reyn's own fact (`Session._compaction_episode_seq`, session.py),
    # never a THIRD PARTY's, so this is the SAME `kind:value` vocabulary
    # extended with a new namespace, not a new identifier kind (matches
    # #6213's own `tool:` addition, same reasoning). Checked BEFORE
    # `dispatch_id`/`call_id` below (accept④'s own witness: a message
    # with no `compaction_episode_seq` falls straight through, so this
    # cannot change any non-compaction message's `id`/`parent_id`).
    #
    # Both producers are `kind="system"` — `kind` alone cannot tell them
    # apart (unlike `tool_call_started` vs `completed`/`failed` above),
    # so the branch is gated on `compaction_episode_marker`'s presence
    # instead: `lifecycle_forwarder.py`'s 5 marker-emitting handlers
    # (`on_compaction_started`/`failed`/`on_summary_resummarize_failed`/
    # `on_recovery_summary_persisted`/`on_compaction_completed`, all via
    # `_compaction_marker_meta()`) stamp the marker flag; `app.py`'s own
    # `_ensure_compaction_progress_entry` (the progress-entry row itself)
    # does not. `_COMPACTION_PROGRESS_KEY` (the OTHER thing that names
    # this row, `_meta_keys.py`) is deliberately NOT read here — that
    # module lives in `reyn.interfaces`, and importing it from this
    # runtime-layer function would invert the layer boundary (#6106
    # architect ruling, same reasoning).
    #
    # ⚠️ DISCLOSED, UNVERIFIED (#6186 issue thread — same disclosure
    # shape as #6198's own overflow-retry premise): `compaction_episode_
    # seq` is per-``Session``, in-process, resets to 0 on a fresh Session
    # (a reconnect/new process) — never persisted, never cross-session
    # (session.py's own single-increment-point discipline, #6085
    # ruling). A stale `episode:0` from a PRIOR process could in
    # principle collide with a fresh `episode:0` after reconnect; this
    # has NOT been run-verified. The reasoning it is likely harmless
    # (the progress-entry row itself is per-app-instance/volatile state
    # too, so a reconnect that resets the counter also clears the row
    # that could have collided with it) is INFERENCE, not a measurement.
    if meta.get("compaction_episode_seq") is not None:
        episode_seq = meta.get("compaction_episode_seq")
        if meta.get("compaction_episode_marker"):
            return None, f"episode:{episode_seq}"
        return f"episode:{episode_seq}", None
    call_id = meta.get("call_id")
    chain_id = meta.get("chain_id")
    dispatch_id = meta.get("dispatch_id")
    # #6213: a `tool:{dispatch_id}` branch, ADDED to the 4-branch table
    # above, not a replacement of it — `dispatch_id` (dispatcher.py's own
    # `new_dispatch_id()`, minted fresh per `dispatch_tool` call, unlike
    # `args_hash`, which collides BY DESIGN when the same tool is called
    # with the same args twice) is the SAME identity vocabulary this
    # function already speaks (`kind:value` strings, #6186's own "answer
    # in one system" ruling), not a 6th identifier kind read directly by
    # a consumer — a `tool_call_started` row's OWN `id` becomes
    # `tool:{dispatch_id}` (its `parent_id` is UNCHANGED — still derived
    # from `call_id`/`chain_id` below, a started row belongs to its
    # LITELLM call same as before); a `tool_call_completed`/`failed`
    # row's `parent_id` becomes `tool:{dispatch_id}` INSTEAD OF
    # `call:{call_id}` — it now names the ONE started row it settles,
    # not the whole call (#6213 accept ⑦'s own note: correct AS nesting,
    # Claude Code's own "the tool call and its result expand together"
    # shape; an orphaned completion whose started row never absorbed it
    # still renders identically — its OWN presentation never reads
    # `parent_id`, see the app.py side of this fix). A message with no
    # `dispatch_id` (any producer that predates this fix, or one that
    # never threads it through) falls through to the pre-#6213 rule
    # below, unchanged.
    if dispatch_id and kind == "tool_call_started":
        return f"tool:{dispatch_id}", (f"call:{call_id}" if call_id else None)
    if dispatch_id and kind in ("tool_call_completed", "tool_call_failed"):
        return None, f"tool:{dispatch_id}"
    if call_id and kind == "agent":
        return f"call:{call_id}", (f"turn:{chain_id}" if chain_id else None)
    if call_id:
        return None, f"call:{call_id}"
    if kind != "user":
        return None, (f"turn:{chain_id}" if chain_id else None)
    return (f"turn:{chain_id}" if chain_id else None), None


@dataclass(frozen=True)
class OutboxMessage:
    """One item published by Session to its outbox queue.

    `kind` selects the renderer's formatting branch and MUST be in the closed
    :data:`VOCABULARY` (validated in :meth:`__post_init__`). `meta` carries
    optional provenance:

    Common keys:
      run_id           full chat-side run id (e.g. "20260501T...Z_run_abcd")
      run_id_short     trailing 4 chars of run_id, used in display prefix
      actor       human-friendly actor name for [actor#abcd] prefix
      intervention_id  for kind="intervention", which UI is being announced

    Future keys (multi-agent):
      agent_id         which agent emitted this message

    FP-0013:
      reply_to         TransportRef identifying the logical destination for
                       routing.  ``None`` during migration; the routing layer
                       falls back to the registered default surface (TUI) when
                       absent.

    #6184 段2a-1 (structural fields — every one below defaults so no
    existing construction call site changes, and NOTHING in ``src/``
    reads any of them yet; landing this is itself the accept criterion,
    not any new rendering behaviour). 段2a-2 added the DERIVATION of
    `id`/`parent_id` (below) — still zero producer diff, since every
    existing construction call site passes neither field and so is
    unaffected; `subject`/`details` remain unwired.

    id
      Opaque, UNIQUE PER ISSUANCE. Must NOT be ``op_id``/``args_hash``
      reused — those are ``dispatcher.py``'s own MEMO KEY
      (``_compute_args_hash``, verbatim: "collision risk is acceptable
      for resume memoization"), deliberately DETERMINISTIC so the SAME
      tool called with the SAME args collides on purpose. `id` needs the
      opposite property: two calls with identical args must still get
      two different `id`s. #6184 段2a-2: DERIVED in :meth:`__post_init__`
      via :func:`_derive_id_and_parent_id` from `kind`/`meta` (a
      ``call:``/``turn:``-prefixed key built from ``meta["call_id"]``/
      ``meta["chain_id"]``) — see that function's own docstring for the
      full rule and, critically, WHY `call_id` is a THIRD PARTY's
      identifier (litellm's own response id), not something whose
      uniqueness this field can claim independently. A row with no
      qualifying key (see that function's own table) still gets `None`,
      never a fabricated placeholder. NO PRODUCER OVERRIDE this stage —
      the field is ``init=False`` (BLOCKING round 2 fix, PR #6191
      review: silently overwriting an explicit value was worse than
      raising — a round-trip test built against one stayed green while
      comparing the discarded-then-re-derived value to itself; a
      hand-written raise in turn broke real ``dataclasses.replace()``
      call sites in app.py, which legitimately carry an existing
      instance's OWN already-derived value forward as a constructor
      kwarg — indistinguishable from a producer's hand-typed one at
      that point. ``init=False`` resolves both: a caller cannot pass
      this at all (``TypeError``, at the language level), while
      ``replace()`` — which never attempts to pass an ``init=False``
      field — always re-derives fresh instead of either conflicting or
      carrying a stale value forward).
    parent_id
      Opaque, optional. Declares that this entry belongs to the SAME
      something another entry does — NOT that it is that entry's
      tree-structural child; whether (and how) a consumer renders that
      as nesting is the CONSUMER's decision, not asserted here.
      (Architect ruling, #6184 issuecomment-5681485023: writing this as
      "parent in a tree" invites the next producer to add a SECOND field
      for "belongs to the same episode" instead of reusing this one.)
      #6184 段2a-2: DERIVED alongside `id` — see that field's own note
      and :func:`_derive_id_and_parent_id`.
    operation
      See :class:`Operation` — the producer's own 2-value declaration
      (``NEW``/``UPDATE``), normalized via :func:`_normalize_operation`.
    subject
      Deliberately left EMPTY this stage. A producer must NOT hand-pick
      its own subject — deriving it from each tool's own declaration
      (``descriptions/``) is a LATER stage (#6184 段3); a producer
      choosing by hand here recreates the "registration can be
      forgotten" failure mode on the PRODUCER side that this arc's own
      dispatch-table census already found and rejected on the RENDERER
      side.
    details
      Free-form, producer-assigned. Supplementary structured content
      beyond ``subject`` — what a future structured renderer would draw
      the rest of an entry's display from.
    """
    kind: str
    text: str
    meta: dict = field(default_factory=dict)
    reply_to: "TransportRef | None" = field(default=None)
    # #6184 段2a-1: all five below are new, structural, and PURELY
    # ADDITIVE — every field defaults so no existing construction call
    # site needs a change, and (this stage's own accept criterion) no
    # code under src/ reads any of them yet.
    # #6184 段2a-2 BLOCKING round 2 (lead-coder review, PR #6191; a real
    # production regression this session's own strip-falsify found, not
    # hypothetical): ``init=False`` — NOT a hand-typed ``if self.id is
    # not None: raise`` in __post_init__. That first attempt broke real
    # production call sites: several ``dataclasses.replace(entry.item,
    # ...)`` sites in app.py carry an EXISTING OutboxMessage's own
    # already-derived `id`/`parent_id` forward as constructor kwargs
    # (``replace()`` re-invokes ``__init__`` with every current field
    # value) — indistinguishable, at ``__post_init__`` time, from a
    # producer hand-typing an unrelated value. ``init=False`` removes
    # `id`/`parent_id` from the generated ``__init__``'s OWN parameter
    # list entirely — ``dataclasses.replace()`` then never attempts to
    # pass them at all (its own contract: an ``init=False`` field is
    # never copied forward, only ever recomputed by the NEW instance's
    # own ``__post_init__``), so a `kind`/`meta`-changing ``replace()``
    # call correctly re-derives against the NEW values instead of
    # carrying a now-stale one. A caller still cannot set either field
    # by hand — attempting ``OutboxMessage(..., id="x")`` now raises
    # ``TypeError`` at the language level (an unrecognised keyword
    # argument), before construction even reaches this class's own
    # code — MORE fail-visible than a hand-written raise, not less.
    id: "str | None" = field(default=None, init=False)
    parent_id: "str | None" = field(default=None, init=False)
    operation: "Operation" = field(default_factory=Operation.default)
    subject: "str | None" = field(default=None)
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # #6184 段2a-1: the ONE normalization point for `operation` on
        # the in-process construction path — mirrors ChatMessage.
        # __init__'s own `self.kind = _normalize_history_entry_kind(kind)`
        # call (chat_message.py). `from_wire` bypasses this method
        # entirely (frozen-dataclass __init__ workaround, see its own
        # docstring) so it normalizes `operation` itself, separately.
        object.__setattr__(self, "operation", _normalize_operation(self.operation))
        # Production-side vocabulary gate (fail-visible at construction, catching
        # the dynamic/helper constructions a static scan misses). Untrusted wire
        # values MUST route around this via :meth:`from_wire`.
        if self.kind not in VOCABULARY:
            raise ValueError(
                f"OutboxMessage.kind {self.kind!r} is not in the closed vocabulary. "
                "Add it to DISPLAY_KINDS or CONTROL_KINDS (with its codec mapping / "
                "profile entry) — an un-dispositioned kind would leak an unprofiled "
                "CUSTOM name on the AG-UI wire. Untrusted wire values must use "
                "OutboxMessage.from_wire (lenient)."
            )
        # #5047 (axis A — architect's confirmed design): identity is not
        # optional for the intervention family. Before this, `kind` was a
        # closed, validated vocabulary while `intervention_id` lived
        # unchecked inside the free-form `meta` dict — a well-formed
        # `kind="intervention"` frame could be built with no identity at
        # all, and #5047's own real-environment bug (a restored/replayed
        # frame silently registering as a fresh pending intervention) is
        # exactly what that gap let through. Checked HERE, in the SAME
        # constructor that already validates `kind` — one mechanism, not
        # two. Untrusted WIRE values still cannot fail-close this way —
        # see :meth:`from_wire`'s own demotion instead.
        if self.kind in _INTERVENTION_FAMILY_KINDS and not self.meta.get("intervention_id"):
            raise ValueError(
                f"OutboxMessage.kind {self.kind!r} requires a genuine "
                "meta['intervention_id'] — every intervention-family frame "
                "must carry its own identity at construction time, never "
                "recovered later by position or absence (#5047)."
            )
        # #6230 stage 3 (architect's confirmed design, same shape as #5047
        # two paragraphs up): a legible representation is not optional for
        # the sentinel family. Stage 1 made `text` the human-readable
        # channel and stage 2 made an UNRECOGNIZED kind degrade legibly at
        # render time — this closes the gap between the two: a RECOGNIZED
        # sentinel built with no `text` at all would still slip a blank
        # line past stage 2 (`legible_degrade_text` never runs — the kind
        # IS known) and land back in the pre-#6230 hole. Checked HERE, the
        # SAME constructor that already validates `kind` and the
        # intervention family, not a THIRD mechanism a future sentinel
        # could forget to call. Untrusted WIRE values still cannot
        # fail-close this way — `from_wire` bypasses this method entirely
        # (its own docstring: an unknown/incomplete wire frame "MUST
        # degrade gracefully ... never fail-close"); a wire-decoded
        # sentinel with an empty `text` still degrades through stage 2's
        # `legible_degrade_text` at render time instead of raising here.
        if self.kind in _SENTINEL_FAMILY_KINDS and not self.text:
            raise ValueError(
                f"OutboxMessage.kind {self.kind!r} requires a genuine, "
                "non-empty `text` — every sentinel-family frame must carry "
                "its own legible representation at construction time, "
                "never recovered later by position or absence (#5047 "
                "shape, #6230 stage 3)."
            )
        # #6184 段2a-2: the ONE derivation point for `id`/`parent_id` —
        # see :func:`_derive_id_and_parent_id`'s own docstring for the
        # 4-branch rule and why it reproduces today's real consumer
        # rather than inventing a new one. Both fields are declared
        # ``init=False`` (see their own field comment above) — a caller
        # CANNOT reach this method with an explicit value to conflict
        # with in the first place, so there is nothing to check here;
        # this always derives fresh from `kind`/`meta`, including on
        # every ``dataclasses.replace(...)`` reconstruction (which never
        # attempts to pass an ``init=False`` field, so a replace() that
        # changes `kind`/`meta` correctly re-derives against the NEW
        # values instead of carrying a stale one forward).
        derived_id, derived_parent_id = _derive_id_and_parent_id(kind=self.kind, meta=self.meta)
        object.__setattr__(self, "id", derived_id)
        object.__setattr__(self, "parent_id", derived_parent_id)

    def to_wire_dict(self) -> "dict[str, object]":
        """Wire-safe fields, DERIVED from this dataclass's own field list
        (#6184) — a field this class gains later is included here
        automatically; only a name in :data:`_NON_WIRE_FIELDS` (with its
        own reason recorded there) is ever excluded. A dict-valued field
        is shallow-copied (matches the pre-#6184 hand-written
        ``dict(msg.meta or {})`` at the encode call site) so a caller
        mutating the returned dict cannot mutate this frozen message's
        own state.

        The ONE encode-side source of truth: ``agui/protocol.py``'s own
        wire-dict construction sites call this instead of hand-listing
        field names — see #6184's own finding that two independently
        hand-typed lists (encode's 3 fields vs. this class's then-4-field
        ``from_wire`` body) had already drifted apart before anyone
        noticed, because ``reply_to`` never crossing the wire looked
        identical to "someone forgot it" either way."""
        out: "dict[str, object]" = {}
        for f in dataclasses.fields(self):
            if f.name in _NON_WIRE_FIELDS:
                continue
            value = getattr(self, f.name)
            out[f.name] = dict(value) if isinstance(value, dict) else value
        return out

    @classmethod
    def from_wire(cls, **wire: object) -> "OutboxMessage":
        """Reconstruct from UNTRUSTED wire values, BYPASSING vocabulary validation.

        The AG-UI decode path (``protocol.decode_event``) rebuilds an
        OutboxMessage from a remote peer's frame; an unknown wire kind MUST
        degrade gracefully (ignore-unknown), never fail-close — so decode routes
        around :meth:`__post_init__` here. All PRODUCTION construction uses the
        validating ``__init__``. Bypasses ``__init__`` via ``object.__new__`` +
        ``object.__setattr__`` (the dataclass is frozen).

        #6184: accepts arbitrary wire keys (``**wire``, not a hand-typed
        ``kind, text, meta, reply_to=None`` parameter list) and sets EVERY
        dataclass field by iterating :func:`dataclasses.fields` — the same
        derivation :meth:`to_wire_dict` uses, so the two can never drift
        the way the pre-#6184 hand-typed 3-field encode dict and 4-field
        ``object.__setattr__`` body here did. An excluded field (currently
        only ``reply_to``) is never read from ``wire`` at all — it gets its
        own dataclass default via :func:`_dataclass_field_default`,
        regardless of whether the caller happened to pass that key. An
        unrecognised extra key in ``wire`` (e.g. the ``"frame"`` tag every
        decode call site's own dict still carries) is silently ignored,
        the same tolerance ``dict.get`` already gave every field before.

        #5047 (axis A, wire side — architect's confirmed design): a wire
        frame carrying a KNOWN intervention-family ``kind`` but no
        ``meta["intervention_id"]`` is DEMOTED to ``kind="system"`` rather
        than built as-is. Requiring identity here the way ``__post_init__``
        does for in-process construction would mean fail-closing on
        untrusted wire data — "never fail-close" is this method's own
        founding rule, not negotiable. Demotion satisfies BOTH constraints
        at once: the frame is never silently dropped (it renders, as a
        plain persistent info row — the same "lifecycle chrome" kind
        already used for compaction/budget/cost-warn), and it can never
        claim an identity it does not have, so it can never register as
        pending or become an answer's destination. This is UNRELATED to
        ignore-unknown (an UNKNOWN kind is untouched by this — that is a
        different failure mode, ignored exactly as before); this only
        catches a KNOWN kind with a missing REQUIRED field. ``kind``/
        ``meta`` are resolved BEFORE the generic field loop below because
        the demotion decision needs both together — every OTHER field
        (including any this class gains later) is read from ``wire``
        inside the loop with no such special case.

        #6184 BLOCKING (lead-coder, measured): a field ABSENT from
        ``wire`` — a genuinely missing key, not merely an unrecognised
        surplus one — used to default to a literal ``""`` here regardless
        of that field's own type. That defeats #6184's own promise ("a
        field gained later works automatically"): the very next field
        added that is NOT a ``str`` (an ``int``, a ``list``, ...) would
        silently receive the WRONG-TYPED zero value the moment an older
        peer's wire payload omits it, not its own declared default. Uses
        :func:`_dataclass_field_default` here too — the SAME fallback
        the excluded-field branch above already used correctly — so a
        missing key's value is always that field's own default, never a
        one-size-fits-all string."""
        kind = str(wire.get("kind") or "")
        meta_raw = wire.get("meta")
        meta: dict = dict(meta_raw) if isinstance(meta_raw, dict) else {}
        if kind in _INTERVENTION_FAMILY_KINDS and not meta.get("intervention_id"):
            kind = "system"
        resolved: "dict[str, object]" = {**wire, "kind": kind, "meta": meta}
        obj = object.__new__(cls)
        for f in dataclasses.fields(cls):
            if f.name in _NON_WIRE_FIELDS:
                value = _dataclass_field_default(f)
            else:
                value = resolved.get(f.name, _dataclass_field_default(f))
            object.__setattr__(obj, f.name, value)
        # #6184 段2a-1: this method bypasses __post_init__ entirely (the
        # frozen-dataclass __init__ workaround this method's own
        # docstring names), so `operation` — normally normalized THERE
        # — is normalized here instead, same as `kind`'s own demotion
        # a few lines up needed its own pre-loop handling.
        object.__setattr__(obj, "operation", _normalize_operation(obj.operation))
        return obj


__all__ = [
    "OutboxMessage",
    "Operation",
    "DISPLAY_KINDS",
    "CONTROL_KINDS",
    "VOCABULARY",
    "is_unknown_kind",
    "legible_degrade_text",
]
