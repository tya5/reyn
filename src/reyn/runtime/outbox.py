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
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    """
    kind: str
    text: str
    meta: dict = field(default_factory=dict)
    reply_to: "TransportRef | None" = field(default=None)

    def __post_init__(self) -> None:
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

    @classmethod
    def from_wire(
        cls,
        kind: str,
        text: str,
        meta: "dict | None" = None,
        reply_to: "TransportRef | None" = None,
    ) -> "OutboxMessage":
        """Reconstruct from UNTRUSTED wire values, BYPASSING vocabulary validation.

        The AG-UI decode path (``protocol.decode_event``) rebuilds an
        OutboxMessage from a remote peer's frame; an unknown wire kind MUST
        degrade gracefully (ignore-unknown), never fail-close — so decode routes
        around :meth:`__post_init__` here. All PRODUCTION construction uses the
        validating ``__init__``. Bypasses ``__init__`` via ``object.__new__`` +
        ``object.__setattr__`` (the dataclass is frozen).

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
        catches a KNOWN kind with a missing REQUIRED field."""
        if kind in _INTERVENTION_FAMILY_KINDS and not (meta or {}).get("intervention_id"):
            kind = "system"
        obj = object.__new__(cls)
        object.__setattr__(obj, "kind", kind)
        object.__setattr__(obj, "text", text)
        object.__setattr__(obj, "meta", dict(meta) if meta is not None else {})
        object.__setattr__(obj, "reply_to", reply_to)
        return obj


__all__ = [
    "OutboxMessage",
    "DISPLAY_KINDS",
    "CONTROL_KINDS",
    "VOCABULARY",
]
