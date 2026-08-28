"""``TurnBehaviorTally`` — a per-session, closed-vocabulary counter over a
fixed watch-list of "sensitive" audit-event kinds (#5221).

This is the deterministic data source the behavioral-anomaly-detector
pipeline's ``transform`` step reads (via ``turn_end``'s hook payload) — never
raw message text, never a free-form string. Every value this class can ever
produce is drawn from :data:`SENSITIVE_OP_KINDS`, itself a fixed subset of
``reyn.core.events.event_schema.AUDIT_EVENT_KINDS`` (the closed audit-event
vocabulary, #3410). A supervised agent may cause DIFFERENT members of that
set to fire (it chooses WHICH sensitive ops it performs), but it cannot make
this class report a kind outside the set — the vocabulary is authored here,
by the supervisor, not by anything the agent's turn produces.

**What "sensitive" means here**: kinds whose firing at all is a signal worth
counting for anomaly purposes — secret access/rotation, a threat-detector
match on a to-be-executed/installed artifact, a denied permission, or a
weakened security setting. This list is deliberately small and reviewable;
growing it is a Tier-1-visible code change (CLAUDE.md: "a rule that binds one
directory belongs in that directory's own CLAUDE.md" — this one lives here,
next to the code it binds).

**Scope discipline**: this counts kinds that ALREADY fired during the current
turn — it is observational, not preventive (mirrors the pipeline's own
detect-not-prevent posture, since ``turn_end`` fires after the turn). A
count of zero means "none of the watched kinds fired", never "the turn was
verified safe" — the asymmetric-trust rule the judge AgentStep itself
carries applies one level up too: an empty tally is not evidence of a clean
turn, only the absence of this narrow signal.

**Lifecycle**: one instance per ``Session``, subscribed once to that
session's own ``EventLog`` for the life of the session (a session's audit
events are already scoped to that agent — no cross-session leakage). Counts
accumulate between calls to :meth:`snapshot_and_reset`; the caller
(``Session._run_router_loop``, at ``turn_end``) is responsible for calling it
exactly once per turn so the window it reports is "since the last turn
ended" (approximately "during this turn" — see the method's own docstring
for the one imprecision this leaves).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from reyn.core.events.event_schema import AUDIT_EVENT_KINDS

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.schemas.models import Event

# The closed watch-list. Every member MUST already be a member of
# AUDIT_EVENT_KINDS (asserted at import time below) — this class can never
# invent a new token, only count fewer or more occurrences of these.
SENSITIVE_OP_KINDS: frozenset[str] = frozenset({
    "secret_set",
    "secret_rotated",
    "secret_cleared",
    "permission_denied",
    "exec_threat_blocked",
    "exec_threat_match",
    "sandboxed_exec_started",
    "network_ssl_verify_disabled",
    "file_read_media_denied",
    "mcp_install_threat_blocked",
    "mcp_install_threat_match",
    "skill_install_threat_blocked",
    "skill_install_threat_match",
    "pipeline_install_threat_blocked",
    "pipeline_install_threat_match",
})

_unknown = SENSITIVE_OP_KINDS - AUDIT_EVENT_KINDS
if _unknown:  # pragma: no cover — defensive; a typo here would silently under-count forever
    raise AssertionError(
        f"SENSITIVE_OP_KINDS names kinds absent from AUDIT_EVENT_KINDS: {sorted(_unknown)}"
    )


@dataclass
class TurnBehaviorTally:
    """Subscribes to *events* for :data:`SENSITIVE_OP_KINDS` only (#5260's
    fixed-interest filter — the dispatcher skips this subscriber entirely for
    every other kind, so the steady-state cost of a session that never emits
    a watched kind is a dict lookup per relevant emit, not per emit)."""

    events: "EventLog"
    _counts: "dict[str, int]" = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.events.add_subscriber(self._on_event, kinds=SENSITIVE_OP_KINDS)

    def _on_event(self, event: "Event") -> None:
        kind = event.type
        self._counts[kind] = self._counts.get(kind, 0) + 1

    def snapshot_and_reset(self) -> "tuple[int, str]":
        """Return ``(total_count, kinds_csv)`` for everything counted since
        the last call, then clear the counters.

        ``kinds_csv`` is the SORTED, comma-joined set of distinct watched
        kinds that fired at least once (``""`` if none) — every token is a
        member of :data:`SENSITIVE_OP_KINDS`, so this string stays within
        the closed vocabulary no matter what the turn did.

        Called exactly once per turn, at ``turn_end`` — the window this
        reports is "since the previous call", which is "during this turn"
        as long as nothing else emits a watched kind between one turn's
        ``turn_end`` and the next turn's start (true for the session's own
        turn-serialized work; a concurrent background task on the SAME
        session's EventLog is the one acknowledged source of imprecision —
        see the module docstring's asymmetric-trust note: this is a signal,
        not a certified boundary)."""
        total = sum(self._counts.values())
        kinds_csv = ",".join(sorted(self._counts))
        self._counts = {}
        return total, kinds_csv
