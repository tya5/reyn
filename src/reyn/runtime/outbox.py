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
# The settled disposition of every producer kind (P6a): standard 4 / profiled 11
# / control 2. This module DECLARES the vocabulary independently; the
# non-circular gate (tests/test_outbox_vocabulary.py +
# tests/test_agui_profile_completeness.py) binds it against the real codec map
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

# profiled (11): the codec emits a reyn.display.<kind> CUSTOM event that has an
# extension-profile entry (profile.CUSTOM_PROFILE). Renderer chrome with no
# standard AG-UI analog. INCLUDES the three client-consumed control sentinels
# that are FORWARDED on the wire (not filtered) — the CLIENT consumes them over
# the transport stream, so filtering them would make remote /copy · /rewind
# silent no-ops (they ride as reyn.display.* CUSTOM, round-trip losslessly).
_PROFILED_DISPLAY_KINDS: "frozenset[str]" = frozenset({
    "intervention",         # native prompt UI (answer round-trip via reyn.intervention.*)
    "presentation",         # a present op's text; render-node model on _reyn meta.nodes
    "user",                 # a user-authored line echoed live to the scrollback
    "system",               # persisted lifecycle/status chrome (compaction / budget / cost-warn)
    "trace",                # a nested detail / trace line (dim, transient)
    "tool_call_started",    # tool-call start trace line
    "tool_call_completed",  # tool-call completion trace line
    "tool_call_failed",     # tool-call failure trace line
    "__copy_last_reply__",  # /copy sentinel — client-side clipboard copy (stream_client._handle_copy_sentinel)
    "__rewind_list__",      # /rewind sentinel — client renders the rewind list / region picker
    "__attach_request__",   # /attach sentinel — upstream-consumed at registry._forwarder (agent swap); profile entry is a fail-safe
})

# Every kind FORWARDED to the AG-UI wire as a display frame (standard or CUSTOM).
DISPLAY_KINDS: "frozenset[str]" = _STANDARD_DISPLAY_KINDS | _PROFILED_DISPLAY_KINDS

# control-filtered (2): emitter-FILTERED control sentinels
# (== protocol.CONTROL_FILTER_KINDS) — consumed as signals, NEVER forwarded on
# the wire. Each documented with its consumption locus:
CONTROL_KINDS: "frozenset[str]" = frozenset({
    # Stream terminator: OutboxHub._drain / registry._forwarder /
    # _SessionFrameSource loops all return on it; the AG-UI emitter returns
    # (ends the SSE stream). Never rendered.
    "__end__",
    # `/session switch <sid>`: consumed at registry._forwarder (swallowed with
    # `continue` → attach_session); the AG-UI emitter also fail-safe-filters it.
    "__session_switch_request__",
})

# The complete closed vocabulary of valid OutboxMessage.kind values.
VOCABULARY: "frozenset[str]" = DISPLAY_KINDS | CONTROL_KINDS


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
        ``object.__setattr__`` (the dataclass is frozen)."""
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
