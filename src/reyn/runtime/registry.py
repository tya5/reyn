"""AgentRegistry — owner of all Session instances in a `reyn chat` process.

PR10 introduces multiple agents (= multiple Session instances) sharing
one process. The registry handles persistence (`.reyn/agents/<name>/`),
lifecycle (lazy load, background `session.run()` task, attach/detach), and
attached-agent routing for the REPL.

Lifecycle invariants (PR10):
- A `default` agent always exists; created on registry init if absent.
- Agents are loaded lazily — `start_attached()` is the first time we
  spin up `session.run()` for the named agent.
- After `attach(B)`, agent A's `session.run()` keeps running in the
  background (its turns can keep progressing); only the REPL's display
  pointer moves to B.

The registry deliberately knows nothing about prompt_toolkit, renderers,
or the inbox/outbox queue mechanics — those live in `repl.py`. Registry's
contract is:
- `attached` returns the currently-attached Session (or None)
- `attach(name)` makes that session the attached one and returns it
- `running_tasks_for_agents()` lets the REPL `await` shutdown drain
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import threading
import time
from collections.abc import Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote
from uuid import uuid4

logger = logging.getLogger(__name__)

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.anchor_store import AnchorStore
from reyn.core.events.events import Event
from reyn.core.events.retention import RetentionPolicy, compute_retention_floor
from reyn.core.events.snapshot_generations import (
    GLOBAL_SCOPE,
    REWIND_KIND,
    Branch,
    RewindBeyondRetentionError,
    RewindIntoAbandonedError,
    RewindQuiesceTimeoutError,
    SnapshotGenerationStore,
    active_rewind_target_with_scope,
    branch_ids_for,
    build_active_predicate,
    is_active_seq,
    lineage_predecessor,
    list_branches,
    reconstruct,
)
from reyn.core.events.snapshot_generations import checkout as _append_reset_record
from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.frames import EventFrame

from .profile import PROFILE_FILENAME, AgentProfile
from .spawn_routing import ReviewedNA
from .topology import TOPOLOGY_DIRNAME, Topology, _validate_topology_name

DEFAULT_AGENT_NAME = "default"

# shutdown() grace window: how long to let session.run loops drain cooperatively
# (notice the shutdown sentinel at a turn boundary) before hard-cancelling any
# that are still stuck — e.g. blocked mid-LLM-call on a slow/hung provider, which
# never reaches the boundary to see the sentinel. Keeps /quit from hanging.
_SHUTDOWN_GRACE_S = 3.0

# #4771: per-connection worst-case for ONE MCP client's own close() teardown —
# NOT reused from _SHUTDOWN_GRACE_S above (that name names shutdown, and its
# meaning is different: shutdown can safely abandon a straggler because the
# PROCESS exits right after; a rewound session keeps running, so proceeding
# past an unquiesced session risks a straggler WAL append landing past the
# reset-record — see RewindQuiesceTimeoutError's own docstring). Measured by
# reading the installed SDK's own teardown source directly (#4771 — NOT a
# guessed number, per the owner's standing "no baseless constant" rule),
# `mcp/client/stdio.py` (installed mcp==2.0.0):
#   PROCESS_TERMINATION_TIMEOUT (2.0s, wait after closing stdin)
#   + FORCE_KILL_TIMEOUT        (2.0s, SIGTERM -> SIGKILL grace, POSIX)
#   + _KILL_REAP_TIMEOUT        (2.0s, wait for the kill to land)
#   + _WRITER_FLUSH_TIMEOUT     (0.5s, writer-side flush cap)
#   = 6.5s, and critically the SDK's own teardown ALWAYS returns even in the
#   worst case (a logged "abandoning it" rather than hanging further) — so
#   reyn adds NO timeout of its own at the MCP-close layer (#4771's own
#   conclusion: stacking a second, reyn-side timeout on an already-bounded
#   third party would create two independent truths about how long
#   teardown may take, with no way to tell which one actually fired).
# ONLY the stdio transport was measured this way — HTTP/SSE transports'
# close-path bound was not verified with the same rigor (#4771).
_MCP_CLIENT_CLOSE_WORST_CASE_S = 6.5


def _quiesce_bound_s(held_mcp_connections: int) -> float:
    """The rewind quiesce timeout for a session holding
    ``held_mcp_connections`` open MCP connections (#4771) — a pure
    function, deliberately separate from :meth:`AgentRegistry.
    _await_quiescent_bounded`'s own ``asyncio.wait_for`` call so a test
    can assert on the VALUE this returns instead of racing a real clock
    against it (lead-coder review, #4799).

    ``max(1, held_mcp_connections)``: a floor of one whole
    :data:`_MCP_CLIENT_CLOSE_WORST_CASE_S` unit even for a connection-less
    session (the common case) — the OTHER quiesce steps (``_turn_idle``,
    chain-timeout watchdog cancellation) still need a real, non-zero
    window, not a timeout racing effectively zero."""
    return max(1, held_mcp_connections) * _MCP_CLIENT_CLOSE_WORST_CASE_S


# FP-0043 Stage 3: the implicit per-agent session id. Single-session paths
# resolve to this id, keeping N=1 behaviour byte-identical. Spawned sessions get
# generated ids (Stage 4 routes inbound messages to non-default sessions).
_DEFAULT_SID = "main"

# #1954: tombstone marker for an archived (soft-deleted) agent. Lives in the
# agent dir; its content is the WAL seq at archival time (slice-2 GC hinge —
# hard-purge once the retention floor passes it, §24-faithful). Archived agents
# stay on disk (generations kept → rewind-to-before-delete works) but are hidden
# from active surfaces (list_active_names) while remaining visible to the
# rewind/GC substrate (list_names stays the literal all-on-disk set).
ARCHIVED_MARKER = ".archived"

# #2103: the lifecycle WAL create-kinds recognised by the as-of-cut DROP /
# re-materialise primitive by default. One registration point (no per-construction-
# site arg → no #2093 propagation drift): S2 added agent_created; S1bc adds
# session_spawned. A registry built with an explicit ``create_event_kinds`` overrides
# this (the foundation tests do). Inert until the events are emitted.
_LIFECYCLE_CREATE_KINDS = frozenset({"agent_created", "session_spawned"})

# #5729: the closed set of a session's own audit-event kinds that can flip
# turn_active or iv_waiting — see AgentRegistry._subscribe_session_status.
# turn_started/turn_settled (NOT chat_turn_completed_inline, which only
# fires on one router branch — see that method's own docstring) cover
# turn_active. intervention_announced covers iv_waiting's ENQUEUE side for
# ALL 6 intervention paths (a NEW kind, declared in AUDIT_EVENT_KINDS +
# events.md, emitted from InterventionHandler.announce — the one choke
# point every caller shares; user_intervention_requested alone only
# covers ask_user.py, per renderer.py's own verified comment).
# user_answered_intervention covers the RESOLVE side (also common to all
# 6, InterventionHandler.deliver_answer_to).
_STATUS_AUDIT_EVENT_KINDS = frozenset({
    "turn_started",
    "turn_settled",
    "intervention_announced",
    "user_intervention_requested",
    "user_intervention_received",
    "intervention_denied",
    "intervention_answer_submitted",
    "user_answered_intervention",
})


def _count_inflight_disposition(tasks: "list") -> "tuple[int, int]":
    """#2115: classify settled in-flight tasks → (cancelled, finished). A task
    cancelled at an await reports ``cancelled()``; one that RETURNED before the
    cancel landed is ``done()`` and not cancelled = finished (it won the cancel
    race). Powers the TRUTHFUL /rewind summary (vs the old hardcoded "in-flight
    cancelled" literal that lied about finished runs, #2115)."""
    cancelled = sum(1 for t in tasks if t.cancelled())
    finished = sum(1 for t in tasks if t.done() and not t.cancelled())
    return cancelled, finished

# ADR-0038 1f: WAL-entry-kind → rewind-point boundary label. All inputs are
# OS-level ``WAL_EVENT_KINDS`` (P7-safe — no skill/domain strings). The three
# output labels are the D6 Phase-1 granularity (turn / plan-step / phase).
_REWIND_PLAN_STEP_KINDS = frozenset({
    "step_completed", "step_failed",
})


def _rewind_point_kind(wal_kind: str) -> str:
    """Map a WAL entry kind to a rewind-point boundary label (turn / plan-step)."""
    if wal_kind in _REWIND_PLAN_STEP_KINDS:
        return "plan-step"
    return "turn"

# PR13: synthesized auto-network topology. Members = every known agent
# that does NOT belong to any user-declared topology. Computed on demand
# (no caching — registry state mutates and stale caches are a footgun).
# Underscore prefix marks it as system-managed; the topology name regex
# rejects user attempts to create one starting with `_`.
_DEFAULT_TOPOLOGY_NAME = "_default"

# Lowercase ASCII + digit + underscore + hyphen, 1-32 chars. Mirrors usual
# directory-name-safety rules and keeps the on-disk layout uncluttered.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _validate_agent_name(name: str) -> None:
    if not _AGENT_NAME_RE.match(name):
        raise ValueError(
            f"invalid agent name {name!r}: must be 1-32 chars of "
            "[a-z0-9_-] starting with [a-z0-9]"
        )


class AttachedConnection:
    """#3793 stage 1 (ADR-0039 D4 conformance) — the vocabulary architect's
    design settled on for a "connection" (the tmux-multiplexer analogy: a
    connection is a *client*, not a session):

    - ``attached`` — the set of sessions a connection receives OUTPUT from.
      N-capable (multiple sessions could be attached at once — stage 2/3).
    - ``active`` — the ONE destination for un-addressed INPUT (composer free
      text). Exactly one per connection, or None.
    - ``addressed`` input (an id-carrying answer — ``cancel_queued(msg_id)``,
      ``answer_intervention_by_id``) is NOT this class's concern: it already
      reaches whichever session owns that id, independent of ``active`` —
      unaffected by this class and must stay that way.

    ★Stage 1 invariant (deliberately, not yet the end state): ``switch``
    always fully REPLACES the previous entry — ``attached`` never holds more
    than one key. This is what keeps stage 1 a "zero behaviour change" PR:
    TUI and AG-UI share ONE instance of this class today (byte-identical to
    sharing one ``_attached`` tuple), and switching still means exactly what
    it always meant — detach the old, attach the new. Stage 2 is what
    actually lets ``attached`` grow past one entry (giving AG-UI its own
    instance instead of sharing the registry's).

    Lives inside ``registry.py`` for stage 1 (the registry still constructs
    and owns the one shared instance) — the design's end state moves
    ownership to "the connection" itself, a transport-side concept core
    should not construct; that relocation is stage 2/3, not this class's
    definition changing.
    """

    def __init__(self) -> None:
        self._attached: "dict[tuple[str, str], None]" = {}  # insertion-ordered set
        self._active: "tuple[str, str] | None" = None
        self._background_attach_error: "str | None" = None

    def attached_set(self) -> "frozenset[tuple[str, str]]":
        """The full N-capable attached set (stage 1: always 0 or 1 entries)."""
        return frozenset(self._attached)

    @property
    def active(self) -> "tuple[str, str] | None":
        return self._active

    def is_attached(self, key: "tuple[str, str]") -> bool:
        return key in self._attached

    def switch(self, key: "tuple[str, str] | None") -> "tuple[str, str] | None":
        """Replace the (stage 1: at-most-one) attached/active entry with
        ``key`` (``None`` to clear/detach). Returns the PREVIOUS active key
        (or ``None``) so the caller can detach/unwire it — mirrors what
        ``AgentRegistry.attach``'s own ``old = self._attached`` capture did
        before this class existed."""
        old = self._active
        self._attached.clear()
        if key is not None:
            self._attached[key] = None
        self._active = key
        return old

    def record_background_attach_error(self, error: "str | None") -> None:
        self._background_attach_error = error

    def attach_failed(self) -> bool:
        return self._background_attach_error is not None


class AgentRegistry:
    """In-process map of agent_name -> Session with persistence wired in.

    Owns the **REPL-facing outbox**: a single queue that consumers (e.g.
    `repl._output_loop`) read regardless of which agent is attached. A
    per-agent forwarder task pumps the agent's own `outbox` into this queue
    only while that agent is the attached one — detached agents drop
    transient outbox items, durable kinds (agent) still persist to history
    via the agent's `_append_history` (handled at the Session layer, not here).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        session_factory: Callable[[AgentProfile], "object"],
        state_log: StateLog | None = None,
        retention_policy: RetentionPolicy | None = None,
        delegation_capability_default: str = "inherit",
        max_spawn_depth: int = 0,
        max_spawn_children: int = 0,
        max_pipeline_fan_out_depth: int = 0,
        max_pipeline_spawns: int = 0,
        factory_config: "object | None" = None,
        create_event_kinds: "frozenset[str] | None" = None,
    ) -> None:
        """
        session_factory: returns a configured Session given an AgentProfile.
            The factory captures CLI-derived defaults (model, resolver, permissions,
            limits, mcp config, …) — registry doesn't need to know them.
        state_log: PR21 WAL for crash recovery. When None, persistence is
            disabled (tests / non-chat invocation). Owned by the caller; the
            registry just hands it to each constructed session and uses it
            during `restore_all()`.
        retention_policy: ADR-0038 Stage 1e (D5) retention window. ``None`` →
            live (current behaviour, no deeper retention). When deeper, clamps the
            truncation floor + GCs generations/blobs to the configured window.
        """
        # #2093: when the shared SessionFactoryConfig bundle is provided (the 5
        # frontend factory sites pass it), it SUPPLIES the uniform config-derived args
        # (delegation_capability_default) — so a new one is added in one place (the
        # bundle) and can't be missed at a site (delegation_capability_default was the
        # drift). The individual params remain for the utility / test callers (which use
        # defaults), keeping them unchanged.
        if factory_config is not None:
            delegation_capability_default = factory_config.delegation_capability_default
            max_spawn_depth = factory_config.max_spawn_depth
            max_spawn_children = factory_config.max_spawn_children
            max_pipeline_fan_out_depth = factory_config.max_pipeline_fan_out_depth
            max_pipeline_spawns = factory_config.max_pipeline_spawns
        # #2103 C3: operator spawn-tree bounds (safety.spawn.*), enforced at the LLM
        # spawn seams (host adapter). 0 = unlimited. Util/test callers default to
        # unlimited (byte-identical); the 5 frontend factory sites supply them via the
        # factory_config bundle.
        self._max_spawn_depth = max_spawn_depth
        self._max_spawn_children = max_spawn_children
        # #2187 for_each S5: pipeline fan-out spawn bounds (safety.spawn.*),
        # enforced by the pipeline executor (guards b/c). The driver-session reads
        # them off this registry and threads them into run()/resume(). 0 = unlimited.
        self._max_pipeline_fan_out_depth = max_pipeline_fan_out_depth
        self._max_pipeline_spawns = max_pipeline_spawns
        self._dir = project_root / ".reyn" / "agents"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._topology_dir = project_root / ".reyn" / TOPOLOGY_DIRNAME
        # #1827 S3: capability_profile bindings (.reyn/capability_profiles/<name>.yaml)
        # resolved per-agent from its topology role bindings (Topology.profiles).
        self._capability_profile_dir = project_root / ".reyn" / "capability_profiles"
        self._factory = session_factory
        self._state_log = state_log
        self._project_root = project_root
        # #2103 B (agent-spawn, Decision A): the spawn lineage child→parent. OS-set at
        # an agent-SPAWN (not plain create), set-once + immutable (a forged/repeat parent
        # is rejected), acyclic-by-construction (the parent pre-exists; a new child can't
        # be an ancestor). resolved_profile_for composes the parent's LIVE resolved
        # effective as a restrict-only conjunct → spawned ⊆ parent by construction,
        # recursively (no-escalation-via-spawn). WAL-carried on agent_created for
        # rewind-reconstruction.
        #
        # #2103 C2b (#2166): the edge value is keyed on the parent's stable IDENTITY,
        # not its reusable name — child → (parent_name, parent_identity). A purged +
        # name-REUSED parent gets a NEW identity, so the orphan's stored edge identity
        # mismatches → the edge is STALE → resolved_profile_for #2161-fail-closes and
        # is_spawn_descendant rejects (fixes both consumers from one identity check;
        # composes with #2161's absent-parent existence-check).
        #
        # #5084 (architect ruling, issuecomment-5380583991): the identity is now the
        # parent AGENT DIRECTORY's own ``(ino, st_birthtime)`` (``agent_directory_identity``), stat'd
        # fresh at spawn AND at every comparison — NOT the #2259 PR-2b in-memory
        # monotonic counter this used to be. That counter is populated ONLY by
        # create_agent, so a DECLARED (never-created) parent always read None — "no
        # staleness signal, honour the link" (correct for a parent's first-ever
        # appearance) — but a LATER purge + same-name re-declare of that parent kept
        # producing the SAME missing signal, so the reuse was never caught: measured
        # live (tui-coder), no rewind needed, both this cap-walk AND
        # is_spawn_descendant's forge-guard stayed on the OLD parent's answer. A
        # filesystem stat is available for ANY existing declared agent regardless of
        # how it was created, closing that gap without a new entry point.
        self._spawn_lineage: "dict[str, tuple[str, tuple[int, float | None] | None]]" = {}
        # #2259 PR-2b: name → the in-memory monotonic id assigned when this name was
        # last create_agent'd (role ①, "who was minted and in what WAL order" — used
        # for the truncation-surviving identity generation's own create_seq field).
        # #5084: this is NO LONGER what staleness comparison reads (role ②, "is this
        # the same parent as before" moved to agent_directory_identity's filesystem stat (the
        # directory, not profile.yaml -- see that method's own docstring) —
        # see _spawn_lineage's own comment above for why) — a declared-only agent has
        # no entry here at all, and that is fine now.
        self._agent_create_seq: "dict[str, int]" = {}
        # #2103 C2b + #2259 PR-2b: the monotonic in-memory identity source — now the identity
        # for EVERY create_agent (the WAL seq is worker-assigned async, so the in-memory id is
        # what a child reads synchronously at spawn for the ⊆-parent cap; the worker links
        # id↔seq in the durable agent_created record + the truncation-surviving identity gen).
        self._spawn_create_counter: int = 0
        # #2081: delegation policy. ``deny`` narrows an UNBOUND delegate with the
        # restrictive _delegate floor; ``inherit`` (default) = byte-identical to
        # pre-#2081. ``_constructing_as_delegate`` is the transient is_delegate
        # context set by _construct_session around the (synchronous) factory call,
        # read by resolved_profile_for — keeps the session_factory contract
        # unchanged (it is a caller-provided closure, 60+ construction sites).
        self._delegation_capability_default = delegation_capability_default
        self._constructing_as_delegate = False
        # #2103: WAL kinds the as-of-cut DROP primitive treats as entity-creates.
        # Each such event carries {entity_kind: "agent"|"session", name, sid?}; on
        # rewind, an entity whose create-event seq > the cut is torn down (it did
        # not exist as-of-cut) instead of lingering as an empty-snapshot orphan.
        # Empty by default → the primitive is a byte-identical no-op until
        # session_spawned (S1bc) / agent_created (S2) register their kinds. The
        # create-side inverse of the #1954 archive (delete-side).
        self._create_event_kinds = (
            create_event_kinds if create_event_kinds is not None
            else _LIFECYCLE_CREATE_KINDS
        )
        # FP-0043 Stage 3: the Registry holds N conversation Sessions per Agent.
        # Identity (the Agent value object, S2) is shared per name; the
        # conversation instances (= today's Session, inbox+run-loop+history)
        # are keyed by an opaque session-id, default ``_DEFAULT_SID`` ("main") so
        # single-session behaviour is byte-identical. Inbound routing to non-main
        # sessions is Stage 4 — S3 just lets the structure hold N.
        self._identities: dict[str, "object"] = {}            # name -> Agent (shared identity)
        self._sessions: dict[str, dict[str, "object"]] = {}   # name -> {sid -> Session}
        # Run-loop + outbox-forwarder task handles, keyed by (name, sid) — they are
        # per-conversation, so they scale with sessions, not identities.
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}         # (name,sid) -> session.run() task
        self._forward_tasks: dict[tuple[str, str], asyncio.Task] = {} # (name,sid) -> outbox forwarder
        # #3793 stage 1 (ADR-0039 D4): TUI and AG-UI both point at this ONE
        # shared instance today (zero behaviour change — see AttachedConnection's
        # own docstring). Stage 2 gives AG-UI its own instance instead of
        # sharing this one, which is what actually lets `attach()` on one
        # connection stop affecting another.
        self._connection = AttachedConnection()
        # Focus-following front-end listeners (REPL/CUI): an audit-event callback
        # (working indicator) and an intervention listener channel (ask_user) that
        # must follow the attached session across agent switches. None until a
        # front-end binds them; wired to the attached session on bind and re-wired
        # on every attach so a `/attach <other>` doesn't strand them on the old
        # session. Generic session-level listeners (not domain-specific).
        self._focus_chat_listener: "Callable[..., None] | None" = None
        # #5041 ①: the ACTUAL closure subscribed to a session's audit_events
        # — never the raw ``_focus_chat_listener`` itself once a listener is
        # set (see ``_wire_focus_listeners``'s own docstring for why this
        # exists: the raw listener is called with a name BOUND AT SUBSCRIBE
        # TIME, and unsubscribe must target the exact wrapped object, not a
        # freshly-built one, or the unsubscribe silently no-ops).
        self._wired_chat_listener: "Callable[[object], None] | None" = None
        self._focus_intervention_channel: str | None = None
        # WAL truncation throttle (WAL-floor design). monotonic ts of last
        # successful truncation attempt; ``None`` means no throttle is active.
        self._last_truncation_ts: float | None = None
        # ADR-0038 Stage 1c-2: set for the duration of a global rewind. While
        # set, ``maybe_truncate_for_size`` no-ops so a compaction can't advance
        # the WAL keep-floor over the reset-record / reconstruct reads mid-cut.
        self._rewind_in_progress: bool = False
        # ADR-0038 Stage 1e (D5): retention window. None → live (current).
        self._retention_policy = retention_policy or RetentionPolicy()
        # #1547: per-checkpoint anchor text (rewind-timeline preview). One global
        # store keyed by WAL seq; lazily built. None when no WAL.
        self._anchor_store: AnchorStore | None = None
        # Single queue the REPL drains; registry routes each attached agent's
        # outbox into here.
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        # #4534 PR-2b: per-agent-name switch-follow subscribers — the registry
        # analogue of OutboxHub.subscribe for a REMOTE connection watching one
        # agent (repl_outbox is a single process-wide queue a remote surface
        # cannot also drain without stealing frames from the local REPL, #3825's
        # own wall). ``_announce_session_attached`` fires every listener
        # registered for the switched agent, synchronously, from the SAME
        # no-await critical section as the ``repl_outbox`` barrier put.
        self._attach_listeners: "dict[str, list[Callable[[str], None]]]" = {}
        # #5146: subscribers wanting to know "an agent name was just purged"
        # — NOT agent-name-keyed like _attach_listeners above (a subscriber
        # cannot pre-register per name; it wants every purge, whichever name).
        # Fired from remove(purge=True) only (archive keeps the name taken,
        # so no name-reuse risk there — see remove()'s own docstring on the
        # archive/purge split). Exists so a transport-layer registry keyed by
        # agent name (AG-UI's SurfaceRegistry, #5146) can drop ITS OWN
        # bookkeeping for a purged name without this module reaching INTO
        # transport (#5139's own "AgentRegistry does not call into interfaces"
        # ruling) — the listener lets transport clean up after itself instead.
        self._remove_listeners: "list[Callable[[str], None]]" = []
        # #5729: per-session status (turn_active / iv_waiting) delta fan-out —
        # the registry-level sibling of ``_attach_listeners`` above, NOT a new
        # kind of mechanism. Fired synchronously with ``(agent_name, sid,
        # turn_active, iv_waiting, seq)`` whenever one of the 4 audit-events
        # that can flip either bool fires for a live session (see
        # :meth:`_subscribe_session_status`). No subscriber list is keyed by
        # agent name (unlike ``_attach_listeners``) — a status consumer (the
        # TUI agent tab) wants every session in this process, not one it has
        # pre-selected, and owner ruling B (#5729) says a remote client sees
        # them too.
        self._status_listeners: "list[Callable[[str, str, bool, bool, int], None]]" = []
        # ``seq`` is a fan-out-owned monotonic counter per (name, sid) — NOT
        # a copy of turn_active/iv_waiting (architect's "no stored copy of
        # STATUS" ruling is about those two values, which this dict never
        # holds; a sequence token is metadata about ordering, exactly like
        # ``Session._queue_seq`` is already metadata about the queue, not the
        # queue's content). Deliberately its OWN counter, not a reuse of
        # ``Session.queue_seq`` — that field is scoped to the sent-queue race
        # specifically (its own docstring), and folding an unrelated bool pair
        # through it would be the same "one value, two facts" shape the
        # architect has ruled against elsewhere in this same design.
        self._status_seq_by_key: "dict[tuple[str, str], int]" = {}
        # #3671 P3 (now #3793 stage 1: moved onto self._connection): the ONE
        # place a caller doing a BACKGROUND attach (P2's
        # `chat.py._background_attach`, running off the render path) can
        # record that its attach ultimately failed, so a client reading
        # `has_session() is False` can distinguish "still connecting" from
        # "gave up" (see `ClientTransport.attach_failed`). `None` = no
        # recorded failure. Cleared at the top of `attach()` so a fresh
        # attach attempt is never shadowed by a stale prior failure.
        # #3671 P4 item C-1: post-WAL-replay snapshots `restore_all(only_names
        # =...)` deferred instead of building+running immediately — applied
        # once, lazily, the first time `get_or_load` actually constructs that
        # (name, DEFAULT_SID) session (an `attach()` or a delegation target's
        # `ensure_running()`, both of which call `get_or_load`). Entries are
        # POPPED on use, so a name that's never reached this run just never
        # gets its Session built at all — the deferred cost, not just a
        # delayed one.
        self._pending_restore: "dict[tuple[str, str], AgentSnapshot]" = {}
        # Ensure default exists so `reyn chat` (no name) works out of the box.
        if not (self._dir / DEFAULT_AGENT_NAME / PROFILE_FILENAME).is_file():
            AgentProfile.new(DEFAULT_AGENT_NAME, role="").save(
                self._dir / DEFAULT_AGENT_NAME
            )
        # PR12: topology declarations under `.reyn/topologies/<name>.yaml`.
        # Bad files become warnings rather than startup errors so a hand-edited
        # yaml doesn't lock the user out of `reyn chat`.
        # #2946 Item 4: lazy — ``None`` means "not yet loaded from disk". The
        # glob + per-file YAML parse only runs on first access (the
        # ``_topologies`` property below), not at construction, so a
        # `reyn chat` cold-start that never touches topologies pays nothing.
        # ★ behavior change: a malformed topology yaml's warning now surfaces
        # at first topology access instead of at Registry construction time.
        self._topologies_raw: dict[str, Topology] | None = None
        # #4995 slice 1 (architect ruling, issuecomment-5384869791, CORRECTED
        # by issuecomment-5384963741 after CI caught the first version): the
        # thread that CONSTRUCTS this registry owns its MUTATIONS, from
        # birth — mirroring ``ThreadedTransportProxy``'s own "worker owns
        # from the moment it exists" shape (that class's own module
        # docstring), never a second owner assigned later.
        #
        # "Owns" means "only thread allowed to MUTATE", NOT "only thread
        # allowed to touch" — the first version of this invariant asserted
        # on 11 methods including 7 READS, and CI found a real, pre-existing,
        # LEGITIMATE second thread within one run: ``app.py``'s #4983 design
        # deliberately reads conversation history off the event loop via
        # ``asyncio.to_thread`` (app.py's own ``_read_conversation_history``
        # call sites), which genuinely executes on a second OS thread and
        # reaches ``get_session``/``agent_workspace_dir``/etc. through
        # ``RegistryReadModel``. That is not the race #4995 exists to
        # prevent — 27 CI failures were this PR breaking a real feature, not
        # catching a real bug. Scoped down to exactly the 4 methods that
        # MUTATE registry-owned state: ``attach``, ``restore_all``,
        # ``resume_deferred_agents``, ``record_background_attach_error``.
        # The 7 read methods (``exists``, ``loaded_names``,
        # ``agent_cost_usd``, ``agent_total_usage``, ``attached_session``,
        # ``get_session``, ``agent_workspace_dir``) are NOT asserted — see
        # each one's own docstring.
        #
        # #5203 tried to restore the guard onto the 7 reads too (architect
        # issuecomment-5385152402, reasoning ``app.py``'s hoist below closed
        # the ONLY legitimate off-thread reader) — WITHDRAWN by the same
        # architect (issuecomment-5385481839, then reaffirmed against a
        # narrower "just `exists()`" alternative, issuecomment-5385499177)
        # for 2 INDEPENDENT reasons, not one:
        #   (a) CI caught 2 MORE legitimate off-thread readers this PR
        #       never enumerated: the web server's A2A (``resolve_a2a_
        #       session`` → ``resolve_session`` → ``get_session``) and
        #       artifact-by-ref (``registry.exists``) routes.
        #   (b) even with every current caller enumerated, "one owner
        #       thread" is topology-dependent, not a real invariant — the
        #       web server structurally runs a SYNC endpoint via FastAPI's
        #       own threadpool, and Starlette's own TestClient dispatches
        #       through its own background portal thread. Narrowing to
        #       "only `exists()` needs excluding" (the 6 OTHER reads
        #       currently having no known off-thread caller) is SHALLOW
        #       reasoning — "safe because nothing off-thread touches it
        #       TODAY" is exactly the assumption that broke twice already
        #       here (#5202's first version, then #5203's own restoration
        #       attempt); it is a fact about today's callers, not a
        #       structural guarantee, and a 3rd unenumerated caller would
        #       be the same mistake a 3rd time. This is the SAME judgment
        #       that withdrew #4995 slice 2's own lock proposal.
        # #5203's real fix (a SEPARATE PR, not this one) is at the actual
        # accidental-safety window instead: ``get_or_load`` (below) calls
        # ``_store_session`` — publishing the session to this registry's
        # map — BEFORE its own later ``load_persisted_toggles``/``restore_
        # state`` finish constructing it. Moving ``_store_session`` to run
        # LAST (publish only once complete) means any thread that finds a
        # session in the map at all finds a COMPLETE one — no guard needed
        # on the reads (there is nothing left for a read-side guard to
        # strip-to-red once publication order is fixed), and A2A/web need
        # no changes either. ``app.py``'s own hoist (below) stays in THIS
        # PR regardless — independently correct hygiene (the registry
        # touch happening on the loop, not because a guard depends on it).
        #
        # #4995 slice 2's own open question (architect, issuecomment-
        # 5384963741): whether #4983's off-thread READS observe a live view
        # of registry-owned state or an immutable snapshot determines
        # whether an `asyncio.Lock` around the 4 mutating methods (slice 2)
        # is sufficient — a lock serializes MUTATION against MUTATION
        # (same-loop coroutines), never against a genuinely concurrent
        # OS-thread READER. See #4995's own issue comments for that
        # measurement once it exists.
        self._owner_thread_ident = threading.get_ident()

    def _assert_owner_thread(self) -> None:
        """#4995 slice 1 (scope corrected — see ``_owner_thread_ident``'s
        own comment): raise if called from a thread OTHER than the one that
        constructed this registry. Call ONLY at the top of a method that
        MUTATES registry-owned state — a read must NOT call this (#4983's
        own off-thread reads are a legitimate second thread; asserting on a
        read breaks them, as CI found — and, per #5215's own withdrawn
        attempt, so does the web server's A2A/artifact-by-ref reads, plus
        the web server's structural use of a threadpool for sync endpoints;
        see ``_owner_thread_ident``'s own comment for the full history).

        Disclosed gap (architect, issuecomment-5385029098), not a
        completeness claim: nothing here or in ``tests/runtime/test_4995_
        registry_owner_thread.py`` catches a FUTURE mutating method added
        with no call to this guard — "which method mutates" is semantics,
        not syntax, so no zero-false-positive gate can enforce it (a naive
        census of ``self._x =`` assignments over-fires on cached/memoized
        reads). Whoever adds a new mutating method must add both the call
        AND a test entry; this is a manually-maintained list, deliberately
        NOT derived from source (see that test file's own comment on why
        deriving it would weaken, not strengthen, the witness)."""
        current = threading.get_ident()
        if current != self._owner_thread_ident:
            raise RuntimeError(
                f"AgentRegistry mutated from thread {current} but is owned "
                f"by thread {self._owner_thread_ident} — see #4995 (a single "
                f"owner thread is the invariant this registry's cross-thread "
                f"MUTATION safety depends on; a second thread reaching this "
                f"method is the race #4995 exists to prevent, not a "
                f"permitted access pattern — reads are a separate, allowed "
                f"case, see this registry's own ``_owner_thread_ident`` "
                f"comment)"
            )

    @property
    def owner_thread_ident(self) -> int:
        """#4995 slice 1: the thread identity this registry is owned by —
        the public read a test (or a future caller deciding whether it is
        safe to touch this registry) can check without reaching into
        ``_owner_thread_ident`` directly."""
        return self._owner_thread_ident

    @property
    def state_log(self) -> StateLog | None:
        return self._state_log

    @property
    def last_truncation_ts(self) -> "float | None":
        """Return the monotonic timestamp of the last truncation attempt, or None."""
        return self._last_truncation_ts

    # ── persistence ──────────────────────────────────────────────────────────

    def list_names(self) -> list[str]:
        """All agent names found on disk (sorted) — incl. archived (#1954).

        Stays the literal all-on-disk set so the rewind/GC substrate
        (_materialize_rewind / _prune_generations_below / checkpoint-seq unions)
        reaches archived agents' generations. Active surfaces use
        ``list_active_names()``."""
        out = []
        for entry in self._dir.iterdir():
            if entry.is_dir() and (entry / PROFILE_FILENAME).is_file():
                out.append(entry.name)
        return sorted(out)

    def is_archived(self, name: str) -> bool:
        """True when ``name`` is an archived (soft-deleted) agent (#1954)."""
        return (self._dir / name / ARCHIVED_MARKER).is_file()

    def list_active_names(self) -> list[str]:
        """Active (non-archived) agent names — the user-facing listing (#1954).

        The fail-safe complement to ``list_names()``: active surfaces
        (CLI/web/TUI/MCP/A2A/slash + the startup load) hide archived agents; the
        rewind/GC substrate keeps using ``list_names()`` so a missed surface is
        merely cosmetic (an archived agent shown), never broken rewind."""
        return [n for n in self.list_names() if not self.is_archived(n)]

    def _archived_seq(self, name: str) -> "int | None":
        """The WAL seq at which ``name`` was archived (#1954 slice-2 GC hinge).

        ``None`` when not archived or the marker is unreadable."""
        marker = self._dir / name / ARCHIVED_MARKER
        if not marker.is_file():
            return None
        try:
            return int(marker.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    # ── FP-0043 Stage 3: session-store accessors (centralize sid-defaulting) ──
    # Every former ``self._agents[name]`` access routes through these so the
    # ~25 internal call-sites + the public API stay correct by construction
    # (default sid = "main" → byte-identical at N=1). The conversation Session
    # lives in self._sessions[name][sid]; the shared Agent in self._identities.
    def _peek_session(self, name: str, sid: str = _DEFAULT_SID) -> "object | None":
        """Non-loading lookup of a Session (= the former ``self._agents.get(name)``)."""
        return self._sessions.get(name, {}).get(sid)

    def _store_session(self, name: str, session: "object", sid: str = _DEFAULT_SID) -> None:
        """Insert a Session under (name, sid), capturing its shared Agent identity."""
        self._sessions.setdefault(name, {})[sid] = session
        ident = getattr(session, "_agent", None)
        if ident is not None:
            self._identities.setdefault(name, ident)
        # #5729: every real (name, sid) insertion goes through this one
        # method (the ``spawn_session`` new-sid path below routes through
        # it too, rather than assigning ``self._sessions`` directly, so this
        # stays the single hook) — wire the status fan-out here so a status
        # listener never has to know how many session-creation call sites
        # exist.
        self._subscribe_session_status(name, sid, session)

    def _has_session(self, name: str, sid: str = _DEFAULT_SID) -> bool:
        return sid in self._sessions.get(name, {})

    def _iter_sessions(self) -> "list[object]":
        """All conversation Sessions across every (name, sid)."""
        return [s for sd in self._sessions.values() for s in sd.values()]

    def _iter_named_sessions(self) -> "list[tuple[str, object]]":
        """(name, Session) for every (name, sid) — for per-agent-name fan-out."""
        return [(name, s) for name, sd in self._sessions.items() for s in sd.values()]

    def get_session(self, name: str, sid: str = _DEFAULT_SID) -> "object | None":
        """Public non-loading accessor for a Session (FP-0043 Stage 3) — the
        supported replacement for external ``registry._agents.get(name)`` reach-in.
        Defaults to the implicit "main" session (byte-identical to the prior
        single-session lookup).

        #4995 slice 1 (architect correction): NOT owner-thread-asserted —
        a read, not a mutation. See ``AgentRegistry``'s own
        ``_owner_thread_ident`` docstring for which methods DO assert and
        why (#5215's attempt to restore this for #5203 was withdrawn —
        the web server's A2A/artifact-by-ref routes reach this method the
        same way ``app.py``'s off-thread reads do)."""
        return self._peek_session(name, sid)

    def session_ids(self, name: str) -> list[str]:
        """FP-0043 Stage 4a: the loaded session-ids for an agent (for `/session
        list`). Empty until the agent's default session loads; "main" + any
        spawned ids thereafter."""
        return list(self._sessions.get(name, {}).keys())

    def _shared_budget_tracker(self) -> "object | None":
        """The process-shared BudgetTracker, reached via any loaded session's gateway (all sessions
        of all agents share ONE tracker in production). ``None`` when no session is loaded / no
        tracker is wired. Because it is process-shared, reading it via any one session yields the
        durable per-agent totals for EVERY agent — including agents with no currently-live session
        (the ledger-hydrated counters persist regardless)."""
        for a_name in self.loaded_names():
            for sid in self.session_ids(a_name):
                sess = self.get_session(a_name, sid)
                tracker = getattr(getattr(sess, "_budget", None), "tracker", None)
                if tracker is not None:
                    return tracker
        return None

    def agent_cost_usd(self, name: str) -> float:
        """All-time cumulative USD cost for agent ``name``, read from the DURABLE process-shared
        BudgetTracker (ledger-hydrated).

        Single source of truth for per-agent cost aggregation — used by the inline status bar and the
        run_repl exit summary. Reading the durable tracker (one per-agent counter) makes this survive
        restart and byte-align with ``/cost``. (Was: a SUM over per-session gateways — this-process
        only, so it reset to 0 on restart AND N×-counted an agent's cost across ``/session new``
        sessions, since each gateway held the full per-agent seed. #cost-restart.)

        #4995 slice 1 (architect correction): NOT owner-thread-asserted —
        a read, not a mutation. See :meth:`get_session`'s own docstring."""
        tracker = self._shared_budget_tracker()
        return tracker.agent_cost_usd(name) if tracker is not None else 0.0

    def agent_unpriced_calls(self, name: str) -> int:
        """How many of agent ``name``'s recorded LLM calls had no known price
        (#3695) — the companion to :meth:`agent_cost_usd`, read off the SAME
        process-shared tracker so the figure and its caveat can never come
        from different places. Non-zero means that cost is a LOWER BOUND."""
        tracker = self._shared_budget_tracker()
        return tracker.agent_unpriced_calls(name) if tracker is not None else 0

    def agent_tokens(self, name: str) -> int:
        """All-time cumulative TOTAL tokens for agent ``name`` from the durable tracker (restart-
        surviving companion to ``agent_cost_usd``). Total only — the prompt/completion breakdown is
        not persisted per ledger record. #cost-restart."""
        tracker = self._shared_budget_tracker()
        return tracker.agent_tokens(name) if tracker is not None else 0

    def agent_total_usage(self, name: str) -> "object":
        """Aggregate TokenUsage across ALL sessions of agent ``name``.

        #4995 slice 1 (architect correction): NOT owner-thread-asserted —
        a read, not a mutation. See :meth:`get_session`'s own docstring."""
        from reyn.llm.pricing import TokenUsage
        total: "TokenUsage" = TokenUsage()
        for sid in self.session_ids(name):
            sess = self.get_session(name, sid)
            if sess is not None:
                total += sess.total_usage
        return total

    def agent_cost_breakdown(self, name: str) -> "object":
        """Agent-scope cache-aware ``CostBreakdown`` — the durable process-shared
        BudgetTracker's per-agent accumulation (all sessions of ``name``, this
        process only; see ``BudgetTracker._agent_cost_breakdown`` for why it is
        NOT ledger-durable unlike ``agent_cost_usd``). Cost-panel Agent column
        source. Empty ``CostBreakdown`` when no tracker is wired."""
        from reyn.llm.pricing import CostBreakdown
        tracker = self._shared_budget_tracker()
        return tracker.agent_cost_breakdown(name) if tracker is not None else CostBreakdown()

    def project_cost_breakdown(self) -> "object":
        """Project-scope cache-aware ``CostBreakdown`` — summed across every
        currently-loaded agent's ``agent_cost_breakdown`` (mirrors the existing
        ad hoc ``sum(agent_cost_usd(name) for name in loaded_names())`` Project
        total the inline status bar already computes). Cost-panel Project
        column source."""
        from reyn.llm.pricing import CostBreakdown
        total = CostBreakdown()
        for name in self.loaded_names():
            total += self.agent_cost_breakdown(name)
        return total

    def agent_embedding_cost(self, name: str) -> "object":
        """FP-0063 PC: agent-scope INDEPENDENT ``EmbeddingCost`` aggregate —
        the durable process-shared BudgetTracker's per-agent embedding
        accumulation (all sessions of ``name``, this process only; same
        non-durability posture as ``agent_cost_breakdown`` — see
        ``BudgetTracker._agent_embedding_cost``). Deliberately NOT part of
        ``agent_cost_breakdown`` / the chat ``CostBreakdown`` — see
        ``EmbeddingCost``'s docstring for why. Empty ``EmbeddingCost`` when no
        tracker is wired."""
        from reyn.llm.pricing import EmbeddingCost
        tracker = self._shared_budget_tracker()
        return tracker.agent_embedding_cost(name) if tracker is not None else EmbeddingCost()

    def project_embedding_cost(self) -> "object":
        """FP-0063 PC: project-scope INDEPENDENT ``EmbeddingCost`` aggregate —
        summed across every currently-loaded agent's ``agent_embedding_cost``
        (mirrors ``project_cost_breakdown`` above, applied to the separate
        embedding aggregate rather than the chat one)."""
        from reyn.llm.pricing import EmbeddingCost
        total = EmbeddingCost()
        for name in self.loaded_names():
            total += self.agent_embedding_cost(name)
        return total

    def resolve_session(
        self,
        agent_name: str,
        transport: str,
        native_id: str,
        explicit_sid: "str | None" = None,
    ) -> "object":
        """FP-0043 Stage 4b-1: the routing-core primitive — map an inbound message
        to the right Session of ``agent_name`` by routing-key (settled design, the
        0043 §Routing-key). Scope is WITHIN one Agent (shared identity/permissions).

        - **Default — deterministic mapping**: ``session_id = "<transport>:<native_id>"``
          (namespaced; e.g. ``slack:T123`` / ``cron:morning_news`` / ``web:<tab>``).
          get-or-spawn: the first message for a key auto-spawns the Session; the
          same key resumes it (stateful per-conversation + isolation, zero-config).
        - **Explicit — join an EXISTING Session** (``explicit_sid``; cross-transport
          bridging): looked up only. A non-existent explicit id is an ERROR — a
          Session is created via the mapping default or an explicit spawn op, never
          silently by a typo'd id.

        This is a pure S3 reuse (``_has_session`` / ``spawn_session`` /
        ``get_session``); transport wiring of the inbound sites is staged separately
        (S4b-2+). Returns the resolved Session."""
        if explicit_sid is not None:
            session = self.get_session(agent_name, explicit_sid)
            if session is None:
                raise KeyError(
                    f"explicit-join target session {explicit_sid!r} does not exist "
                    f"for agent {agent_name!r}. An explicit session id must already "
                    f"exist (created via the routing-key mapping default or an "
                    f"explicit spawn) — it is never auto-created, so a typo'd id is "
                    f"rejected rather than silently opening a new conversation."
                )
            return session
        sid = f"{transport}:{native_id}"
        if not self._has_session(agent_name, sid):
            # #2708 P3-item3: a transport-native inbound session — the transport drains its OWN
            # outbox, no parent to bridge to; self-binding to the factory default is reviewed-correct.
            _routing = ReviewedNA("runtime/registry.py::resolve_session")
            self.spawn_session(
                agent_name, sid=sid,
                presentation_consumer=_routing.presentation_consumer,
                intervention_bridge=_routing.intervention_bridge,
            )
        return self.get_session(agent_name, sid)

    def load_profile(self, name: str) -> AgentProfile:
        return AgentProfile.load(self._dir / name)

    def agent_workspace_dir(self, name: str) -> Path:
        """The agent's home directory, computed WITHOUT constructing or
        attaching a :class:`Session` (#4824) — the same ``<state-root>/
        agents/<name>`` this registry already uses internally for a
        profile's own path (:meth:`load_profile`/:meth:`exists`/
        :meth:`create`, all ``self._dir / name``), exposed publicly so a
        caller that only knows the TARGET agent name — not yet an attached
        Session, because attach hasn't run — can still resolve the same
        path an attached ``Session.workspace_dir`` would report (verified:
        :class:`~reyn.runtime.agent.Agent`'s own ``workspace_dir`` derives
        from ``workspace_state_dir``, which every registry-constructed
        agent gets set to this SAME ``project_root / ".reyn"`` at
        bootstrap — not a re-derivation, the identical value by a
        different, session-free route).

        #4995 slice 1 (architect correction): NOT owner-thread-asserted —
        a read, not a mutation. See :meth:`get_session`'s own docstring."""
        return self._dir / name

    def exists(self, name: str) -> bool:
        """#4995 slice 1 (architect correction): NOT owner-thread-asserted
        — a read, not a mutation. See :meth:`get_session`'s own docstring."""
        return (self._dir / name / PROFILE_FILENAME).is_file()

    def create(
        self, name: str, *, role: str = "", base_dir: "str | Path | None" = None,
    ) -> AgentProfile:
        """#5080: ``base_dir`` (optional) is #4206's axis ① (capability,
        restrict-only) applied to a "file zone" the agent layer had none
        of before — NOT a new kind of override. Written into THIS agent's
        own ``profile.yaml`` (``.reyn/agents/<name>/`` — keyed by AGENT
        identity), never ``.reyn/capability_profiles/<X>.yaml`` (architect
        BLOCK, #5081 review): that directory's ``<X>`` is keyed by PROFILE
        name, a free string a topology's ``profiles: {member: profile_
        name}`` binding writes with no uniqueness constraint against agent
        names — ``profiles: {alice: alice}`` (an agent bound to a
        same-named narrowing template) is a real, unconstrained possibility (lead-coder's own measurement: no same-name binding appears in this repo's current examples, but the frequency claim isn't what makes this a real collision -- the absent uniqueness constraint is), so
        writing base_dir there would silently collide with an unrelated
        narrowing template. Validated ⊆ the project workspace root here,
        the ONE seam every creation surface (CLI / web / slash / the
        ``spawn_agent`` LLM tool) routes through, so the check applies
        uniformly rather than being replicated per surface — the SAME
        "restrict-only, reject rather than clamp, name the boundary" shape
        ``spawn_session``'s own ``base_dir`` argument already uses
        (``router_host_adapter.py``'s ``spawn_session``), but bounded by
        the WORKSPACE ROOT here, not a spawner's own effective
        ``base_dir`` — owner's own resolution rule (issue #5080): an
        agent-spawn with nothing given defaults to the PROJECT base_dir,
        not the spawner's.

        #5742 PR2 (architect ruling, issue #5742): the ``project_context_
        path`` write-side parameter this method used to accept (#5111,
        #5084's own creation-seam declaration path) is retired along with
        the read side (``AgentProfile.project_context_path`` itself,
        ``registry_bootstrap.resolve_agent_project_context``) — a live
        creation seam that kept writing a key ``AgentProfile.load`` now
        hard-rejects would silently make the created agent unstartable.
        An agent's own instructed text is now declared via ``profile.
        yaml``'s ``context_path`` (edited directly, or a future creation-
        seam parameter for it, not yet built — no caller has asked for
        one)."""
        _validate_agent_name(name)
        if self.exists(name):
            raise FileExistsError(f"agent {name!r} already exists")
        resolved_base_dir: "str | None" = None
        if base_dir is not None:
            # This is a STRUCTURED API parameter (CLI/web/slash/the
            # spawn_agent LLM tool), not hand-typed YAML text — a relative
            # value here has always meant "relative to the project root"
            # unambiguously (the caller passed a Python str, never a
            # ``${...}`` token string), so this resolution is unchanged.
            # #5084: only the BOUND check now comes from the shared
            # ``reyn.runtime.workspace_paths.within_workspace`` — that
            # module's own docstring explains why this and the READ side
            # (``Session._read_base_dir_override``, which DOES read
            # hand-typed YAML text and therefore DOES need the
            # ``${REYN_PROJECT_DIR}`` token vocabulary, a cwd-anchor fix
            # of a different shape) share the bound-check function without
            # sharing a relative-resolution rule that doesn't apply here.
            from reyn.runtime.workspace_paths import within_workspace

            candidate = Path(base_dir)
            if not candidate.is_absolute():
                candidate = self._project_root / candidate
            candidate = candidate.resolve()
            workspace_resolved = self._project_root.resolve()
            if not within_workspace(candidate, workspace_resolved):
                raise ValueError(
                    f"requested base_dir {str(candidate)!r} resolves outside "
                    f"the project workspace {str(workspace_resolved)!r} — "
                    "restrict-only: an agent's base_dir must fall under the "
                    "project workspace."
                )
            resolved_base_dir = str(candidate)
        profile = AgentProfile.new(name, role=role, base_dir=resolved_base_dir)
        profile.save(self._dir / name)
        return profile

    #: #5084: an impossible (ino, birthtime) pair — never returned by a real
    #: ``stat()`` (inode numbers are non-negative) — the sentinel
    #: :meth:`remove` stamps onto an edge it actively invalidates at purge
    #: time, guaranteed to never equal a later re-stat regardless of ino
    #: reuse (see :meth:`agent_directory_identity`'s own docstring for why
    #: active invalidation is needed on top of the comparison-time stat).
    #: Public: a test/observer comparing against :meth:`frozen_spawn_
    #: parent_identity`'s own return needs a real value to compare to,
    #: not private state.
    INVALIDATED_SPAWN_PARENT_IDENTITY: "tuple[int, float | None]" = (-1, None)

    def agent_directory_identity(self, name: str) -> "tuple[int, float | None] | None":
        """#5084: ``name``'s DURABLE identity — ``(st_ino, st_birthtime)`` of
        its AGENT DIRECTORY (``self._dir / name``, NOT ``profile.yaml``
        itself) — ``None`` when the directory is absent. ``st_birthtime`` is
        ``None`` on a platform that does not expose it (e.g. most Linux
        filesystems via plain ``stat()`` — an older Python/no ``statx``
        support); the comparison still works with ``ino`` alone there, just
        with the weaker guarantee :meth:`is_spawn_descendant`'s own ⑦
        witness names.

        Replaces ``_agent_create_seq.get(name)`` as the value the ⊆-parent
        cap (``resolved_profile_for``) and the C1 forge-guard
        (``is_spawn_descendant``) freeze at spawn and compare at query time.

        **Why the DIRECTORY, not ``profile.yaml``'s own content or stat**:
        an agent can write its own ``profile.yaml``
        (``_DEFAULT_WRITE_ZONES=(".reyn",)``), so anything read FROM its
        content (e.g. ``created_at``) is forgeable — a forger could
        re-declare with an old timestamp and defeat detection entirely.
        The FILE's own stat is not safe either: any content edit (e.g.
        ``/agent edit role``, ``slash/agent.py``, re-``save()``ing an
        existing agent's ``profile.yaml``) bumps its ``ctime``, which
        would make a routine, legitimate edit read as a NEW identity —
        breaking the ⊆-parent cap's own LIVE-re-resolution guarantee
        (#2103 B: the parent's topology binding can be narrowed live, and
        the child must re-cap to it, not go stale because an unrelated
        field changed). The DIRECTORY's own identity does not change on a
        profile-content edit — only on ``rmtree`` + recreate (a genuinely
        NEW directory, new inode) — so it distinguishes "the same agent
        got edited" from "a different agent now has this name".

        **Why the filesystem's own metadata at all, not counted-in-
        memory**: ``_agent_create_seq`` is populated ONLY by
        ``create_agent`` — a DECLARED agent (a hand-written/config-applied
        ``profile.yaml``, never routed through that method) never gets an
        entry, so the frozen ``spawn_parent_seq`` for a child spawned ⊆ a
        declared parent is always ``None`` — "no signal, honour the link"
        (the correct behaviour for a parent's first-ever appearance, but
        also the answer after a purge + same-name re-declare produces a
        genuinely DIFFERENT identity, since neither event changes what is
        tracked in memory). Reading the directory's own durable stat
        instead means EVERY existing declared agent has a real, comparable
        identity regardless of how it was created.

        A directory's own ``ino`` CAN be reused by the OS after ``rmtree``
        — which is why :meth:`remove`'s own ``purge=True`` path ACTIVELY
        invalidates edges pointing at the removed name (stamping
        :data:`INVALIDATED_SPAWN_PARENT_IDENTITY`) rather than relying
        purely on a later ino collision never happening; this comparison-
        time stat is the primary mechanism (covers offline edits + a
        manual out-of-band ``rmtree``, no entry point needed) and the
        active invalidation is the backstop for the one thing a stat alone
        cannot rule out.

        See :meth:`frozen_spawn_parent_identity` for the PUBLIC read of
        what a specific child's edge currently holds (vs. this method,
        which reads a name's CURRENT identity)."""
        try:
            st = (self._dir / name).stat()
        except OSError:
            return None
        return (st.st_ino, getattr(st, "st_birthtime", None))

    def frozen_spawn_parent_identity(self, child: str) -> "tuple[int, float | None] | None":
        """#5084: the PUBLIC read of what ``child``'s spawn-lineage edge
        currently holds as its frozen parent identity — ``None`` if
        ``child`` has no spawn edge at all. Exposed so a test/observer can
        confirm identity-tracking behaviour (a value frozen at spawn, or
        actively invalidated by :meth:`remove`'s ``purge=True`` path —
        compare against :data:`INVALIDATED_SPAWN_PARENT_IDENTITY`) without
        reading ``_spawn_lineage`` directly.

        This answers "what value is currently frozen", not "is the child
        capped" — the canonical fail-closed interpretation of that value
        is :meth:`resolved_profile_for`/:meth:`is_spawn_descendant`, not
        this raw read."""
        edge = self._spawn_lineage.get(child)
        return edge[1] if edge is not None else None

    def _record_spawn_lineage(self, child: str, parent: str) -> None:
        """#2103 B: OS-set the spawn lineage ``child → parent``, set-once + immutable.
        The lineage is the no-escalation linchpin (resolved_profile_for caps the child
        at ⊆ parent via it), so it must NOT be forgeable or mutable post-spawn: a
        re-set to a DIFFERENT parent is refused, and a self-link is rejected. Acyclic
        by construction — the parent pre-exists and the child is freshly created, so a
        child can never be an ancestor of its parent. Idempotent on the same parent
        (rewind-reconstruction may replay the same edge).

        #2103 C2b: the edge stores ``(parent_name, parent_identity)`` — the parent's
        identity FROZEN at spawn time (#5084: ``agent_directory_identity(parent)`` — the
        parent AGENT DIRECTORY's own ``(ino, st_birthtime)``, never ``None`` for an
        EXISTING parent regardless of whether it was ``create_agent``'d or merely
        declared — see that method's own docstring for the full "why"). None only
        when the parent's profile is itself absent at spawn time (should not
        happen — the caller resolved ``parent`` as existing — kept as a defensive
        fallback, not a documented case). Immutability/cycle compare by NAME (the
        identity is metadata for staleness)."""
        if child == parent:
            raise ValueError(f"spawn-lineage self-link rejected: {child!r}")
        existing = self._spawn_lineage.get(child)
        if existing is not None and existing[0] != parent:
            raise ValueError(
                f"spawn-lineage for {child!r} is immutable "
                f"(set to {existing[0]!r}; refused re-set to {parent!r})")
        # cycle-guard (B-core close-review note): the parent must not already be a
        # DESCENDANT of the child — walking the parent's lineage to the root must not
        # reach the child. Acyclic-by-construction holds for a fresh spawn (the parent
        # pre-exists, the child is new), but this makes it explicit + safe under
        # rewind-reconstruction replay (edges re-recorded in arbitrary order).
        cursor: "str | None" = parent
        seen: "set[str]" = set()
        while cursor is not None and cursor not in seen:
            if cursor == child:
                raise ValueError(
                    f"spawn-lineage cycle rejected: {parent!r} is a descendant of {child!r}")
            seen.add(cursor)
            _edge = self._spawn_lineage.get(cursor)
            cursor = _edge[0] if _edge is not None else None
        self._spawn_lineage[child] = (parent, self.agent_directory_identity(parent))

    def is_spawn_descendant(self, agent: str, ancestor: str) -> bool:
        """#2103 C1: True iff ``agent`` is ``ancestor`` itself OR a transitive spawn-
        descendant of it (walk ``agent``'s lineage chain upward; acyclic → terminates).

        The subtree-membership predicate the ``create_topology`` spawn-seam uses to
        forge-guard which agents an LLM may wire into a topology: members must be ⊆ the
        creator's spawn subtree. That restriction is what makes C's profile bindings
        safe BY CONSTRUCTION — every LLM-bindable member is a lineage descendant of the
        creator, so ``resolved_profile_for``'s live parent-conjunct (B-core) backstops
        the binding (it can only narrow within the member's ⊆-creator envelope, never
        re-grant past it). An agent with no lineage edge (operator-top) is in no one's
        subtree but its own → an LLM cannot wire a non-descendant peer.

        #2103 C2b (#2166): a STALE edge — its parent name was purged + REUSED, so the
        frozen parent identity no longer matches the CURRENT parent's identity (#5084:
        ``agent_directory_identity(pname)`` — that agent DIRECTORY's own ``(ino, st_birthtime)``, re-
        stat'd fresh here, not an in-memory counter) — is a dangling link to a GONE
        identity, NOT a real ancestry. The walk stops at it (returns False), so a name-
        reused agent is rejected as a forged ancestor (the C1 forge-guard bypass tui
        found — measured live, no rewind needed: a DECLARED parent's edge used to carry
        no staleness signal at all, #5084, so this bypass was reachable without any WAL
        truncation). ``pseq is None`` only when the parent's profile was itself absent
        at spawn time (a defensive fallback, not a documented case since #5084 — see
        ``agent_directory_identity``'s own docstring)."""
        if agent == ancestor:
            return True
        cursor: str = agent
        seen: "set[str]" = set()
        while True:
            edge = self._spawn_lineage.get(cursor)
            if edge is None:
                return False
            pname, pseq = edge
            if pseq is not None and self.agent_directory_identity(pname) != pseq:
                return False  # stale (name-reused parent) → dangling, not a real link
            if pname == ancestor:
                return True
            if pname in seen:
                return False
            seen.add(pname)
            cursor = pname

    # ── #2103 C3: operator spawn-tree bounds (safety.spawn.*) ───────────────────────
    # Computed over the SAME identity-keyed lineage as the cap-walk, so a stale
    # (name-reused) edge does not inflate the counts. Enforced at the LLM spawn SEAMS
    # (host adapter) only — the operator CLI create path is unbounded (authority).

    def spawn_depth(self, agent: str) -> int:
        """#2103 C3: the spawn-lineage chain depth of ``agent`` (an operator-top agent =
        0; each spawn edge +1). Walks ``_spawn_lineage`` to the root; a STALE edge
        (name-reused parent → frozen identity ≠ current) terminates the walk — the chain
        is broken there, so a purged+reused ancestor does not inflate the depth."""
        depth = 0
        cursor = agent
        seen: "set[str]" = set()
        while True:
            edge = self._spawn_lineage.get(cursor)
            if edge is None:
                return depth
            pname, pseq = edge
            if pseq is not None and self.agent_directory_identity(pname) != pseq:
                return depth  # stale edge → chain broken
            if pname in seen:
                return depth
            seen.add(pname)
            depth += 1
            cursor = pname

    def spawn_child_count(self, parent: str) -> int:
        """#2103 C3: the number of LIVE direct spawn-children of ``parent`` — edges whose
        parent NAME matches AND whose frozen identity matches ``parent``'s current
        identity (a stale name-reuse edge from an orphan of a PRIOR same-named parent is
        excluded; an untracked-parent edge is counted by name)."""
        pid = self.agent_directory_identity(parent)
        n = 0
        for _child, (pname, pseq) in self._spawn_lineage.items():
            if pname == parent and (pseq is None or pseq == pid):
                n += 1
        return n

    def session_nesting_depth(self, name: str, sid: "str | None" = None) -> int:
        """#2737: the LLM ``spawn_session`` NESTING depth of session ``(name, sid)`` — a
        root/main session = 0, each ``spawn_session`` edge +1. Walks the
        ``SpawnBridgeInterventionListener`` parent-linkage chain — the SAME parent linkage
        ``SpawnBridgeInterventionListener.bus()`` recurses over to resolve an ``ask_user``
        toward the root operator (#2735) — so the depth this returns is exactly that
        ``bus()`` recursion depth. Capping it at the ``spawn_session`` seam therefore bounds
        BOTH unbounded session nesting (resource) AND the compositional ``bus()`` recursion
        (a deep-chain ``RecursionError``) by construction — the #2708 P3-item3 co-vet edge.

        This is a LIVE-runtime property (the in-memory bridge chain), matching the live risk
        it bounds: a crash tears the chain down (restore re-creates a spawned session
        self-bound via ``ReviewedNA`` — no ``SpawnBridge*`` bridge), so there is no persisted
        depth to survive WAL truncation and no recovery-gate surface. A non-spawned session
        (no ``SpawnBridgeInterventionListener`` bridge) terminates the walk at depth 0."""
        from reyn.runtime.session_buses import SpawnBridgeInterventionListener

        session = self._peek_session(
            name, sid if sid is not None else _DEFAULT_SID,
        )
        depth = 0
        seen: "set[int]" = set()
        while session is not None:
            bridge = getattr(session, "intervention_bridge", None)
            if not isinstance(bridge, SpawnBridgeInterventionListener):
                break
            parent = bridge.parent_session
            if parent is None or id(parent) in seen:
                break
            seen.add(id(parent))
            depth += 1
            session = parent
        return depth

    # #2175: the BASE operator spawn bounds (safety.spawn.*, config-set restart-only).
    # Exposed so the LLM spawn SEAM (host adapter) can compute the EFFECTIVE limit
    # (base + the on_limit per-operation extension) and route an exceed through the
    # safety.on_limit checkpoint — exactly as the inter_agent_messaging does over max_hop_depth +
    # _safety_extensions (retiring C3's parallel hard-reject helpers). ``0`` = unlimited.
    # The raw counts (spawn_depth / spawn_child_count above) stay the registry's source
    # of truth; the effective-limit + checkpoint logic lives at the seam.

    @property
    def max_spawn_depth(self) -> int:
        """#2175: the operator base max spawn-lineage depth (0 = unlimited)."""
        return self._max_spawn_depth

    @property
    def max_spawn_children(self) -> int:
        """#2175: the operator base max fan-out — direct children + topology size
        (0 = unlimited)."""
        return self._max_spawn_children

    @property
    def max_pipeline_fan_out_depth(self) -> int:
        """#2187 for_each S5: the operator max ``for_each`` fan-out NESTING depth
        the pipeline executor enforces (guard b; 0 = unlimited)."""
        return self._max_pipeline_fan_out_depth

    @property
    def max_pipeline_spawns(self) -> int:
        """#2187 for_each S5: the operator max ephemeral-session spawn COUNT per
        pipeline run the executor enforces (guard c; 0 = unlimited)."""
        return self._max_pipeline_spawns

    async def create_agent(
        self, name: str, *, role: str = "", parent: "str | None" = None,
        base_dir: "str | None" = None,
    ) -> AgentProfile:
        """#2103 S2b: the action-layer CREATE seam — create the profile (sync) +
        emit ``agent_created`` so rewind can track / reconstruct / drop the agent
        (the create-side of the as-of-cut lifecycle, #2114/#2117). Every creation
        SURFACE (CLI / web / slash + the spawn op) routes through this ONE seam, so
        no surface can miss the emit (rewind-completeness). Emit no-ops without a
        WAL. The mechanism (sync ``create``) stays separate — the event marks the
        user/LLM action, not the file write.

        #2103 B (agent-SPAWN): when ``parent`` is given, record the OS-set immutable
        spawn lineage (the ⊆-parent cap) AND carry ``parent`` on the agent_created
        event so a rewind RECONSTRUCTS the lineage. If the lineage were lost on rewind,
        the reconstructed child would resolve WITHOUT the parent-conjunct = UN-capped =
        escalation-on-rewind — so the carry+restore is a security linchpin (the emit
        AND the reconstruction-restore are both verified, the registered-but-unemitted
        → resurrection hazard class)."""
        profile = self.create(name, role=role, base_dir=base_dir)
        if parent is not None:
            self._record_spawn_lineage(name, parent)
        # #2103 C2b: the parent's identity FROZEN at this spawn — read back from the
        # edge _record_spawn_lineage just stored (#5084: agent_directory_identity(parent),
        # the parent AGENT DIRECTORY's own (ino, st_birthtime) — NOT re-stat'd here, same
        # value the edge holds) — carried on agent_created so a rewind reconstructs
        # the edge with the parent-identity-AT-SPAWN (not the latest), so a rewind
        # across a purge+name-reuse does not resurrect this child under the reused
        # parent.
        parent_seq = self._spawn_lineage[name][1] if parent is not None else None
        # #2259 PR-2b + #2103 C2b(b): the agent's stable identity is an IN-MEMORY ID assigned
        # SYNCHRONOUSLY at spawn — NOT the WAL seq (now worker-assigned async, so unavailable
        # synchronously; a child spawn must read the parent's identity NOW for the ⊆-parent cap).
        # The worker links id↔seq in the durable `agent_created` record, and the identity
        # generation (keyed by the durable worker seq, truncation-surviving) stores this id as
        # ``create_seq`` — so rewind reconstructs identity/lineage from the gen (the owner-
        # corrected model: no consumer reads a live/non-durable seq).
        self._spawn_create_counter += 1
        agent_id = self._spawn_create_counter
        self._agent_create_seq[name] = agent_id
        if self._state_log is not None:
            # Non-blocking (the blocking-invariant): append_nowait + the identity-gen job are a
            # synchronous pair (no await between → atomic enqueue; the gen job is FIFO-after the
            # agent_created WAL job, so it stamps the gen at that durable seq, invariant #2).
            self._state_log.append_nowait(
                "agent_created", entity_kind="agent", name=name, sid="",
                parent=parent,  # #2103 B: lineage for rewind-reconstruction
                parent_seq=parent_seq,  # #2103 C2b: parent identity-at-spawn (rewind)
                agent_id=agent_id,  # #2259 PR-2b: the in-memory identity (links to the seq)
                profile={
                    "name": profile.name,
                    "role": profile.role,
                    "created_at": profile.created_at,
                    "allowed_mcp": profile.allowed_mcp,
                },
            )
            self._record_agent_identity_generation(name)
        return profile

    def remove(
        self, name: str, *, purge: bool = False,
    ) -> "tuple[list[tuple[str, Topology | None]], list[str]]":
        """Delete an agent. Default (#1954 Option A) = ARCHIVE (soft-delete): the
        runtime PITR generations are kept in place so rewind-to-before-delete
        works within the retention window, plus a tombstone recording the archival
        WAL seq (the slice-2 WAL-window GC hinge). ``purge=True`` is the guarded
        escape hatch — a real hard-delete (rmtree) that destroys the rewind
        history (time-travel-to-before-purge is intentionally unsupported).

        Returns ``(topology_changes, vanished_sids)`` — #2103 MUST-1's topology
        cascade, plus (#2159) the agent's non-main spawned session ids subsumed
        by a purge's rmtree, so the async caller (this method is sync — no WAL
        access here) can emit ``session_vanished`` for each through the logged
        seam. Archive cascades neither list — topology membership and sessions
        are both preserved on disk."""
        if name == DEFAULT_AGENT_NAME:
            raise ValueError("cannot remove the default agent")
        if self._connection.active is not None and self._connection.active[0] == name:
            raise ValueError(f"cannot remove attached agent {name!r}")
        target = self._dir / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        # Cancel any cached tasks / drop in-memory sessions (both paths).
        # FP-0043 Stage 3: removing an agent drops ALL its sessions (every sid).
        sids = list(self._sessions.get(name, {}).keys())
        # #2159: enumerate the agent's spawned sessions BEFORE the in-memory map is
        # popped below — ``_discover_session_ids`` unions the in-memory keys with
        # on-disk discovery, and a session that was only ever spawned in-memory
        # (never yet flushed to ``state/sessions/<sid>/``) would otherwise be missed
        # once ``self._sessions.pop(name, None)`` clears its only record of existing.
        vanished_sids = [
            sid for sid in self._discover_session_ids(name) if sid != _DEFAULT_SID
        ]
        for task_dict in (self._tasks, self._forward_tasks):
            for sid in sids:
                task = task_dict.pop((name, sid), None)
                if task and not task.done():
                    task.cancel()
        self._sessions.pop(name, None)
        self._identities.pop(name, None)
        if purge:
            # #2159: the purge rmtree below subsumes every nested spawned session
            # (they live under agents/<name>/state/sessions/<sid>/) with no
            # per-session destroy record — "main" (_DEFAULT_SID) is the agent's own
            # primary session, not a spawned one — the agent_purged record already
            # covers it. Return the pre-pop enumeration (above) so the async caller
            # can emit the session_vanished destroy-side mirror for each.
            # Explicit hard-delete — agents/<name>/ is reyn-managed. Destroys the
            # runtime PITR generations (rewind-to-before-purge is intentionally
            # unsupported); the real escape hatch for a genuine delete.
            import shutil
            shutil.rmtree(target)
            # #5084: ACTIVELY invalidate every spawn-lineage edge naming ``name``
            # as parent — the backstop the comparison-time directory stat alone
            # cannot provide: an inode number CAN be reused by the OS for a LATER
            # same-named re-declare, and a stat-only comparison would then read
            # as "same identity" by coincidence. Stamping the impossible sentinel
            # here means a purge always closes the gap regardless of what ino a
            # future re-declare happens to get — the same edges a comparison-time
            # stat would ALSO catch in the common (non-colliding) case, closed
            # unconditionally at the one moment reyn's own API genuinely knows
            # the identity is gone.
            for _child, (_pname, _pseq) in list(self._spawn_lineage.items()):
                if _pname == name:
                    self._spawn_lineage[_child] = (_pname, self.INVALIDATED_SPAWN_PARENT_IDENTITY)
            # #5146: tell every remove-listener this name is gone, BEFORE the
            # cascade below (a subscriber's own cleanup should not race a
            # later re-declare of the same name — fire while "name" is
            # unambiguously the just-purged identity, not after any other
            # side effect that could reintroduce it). Synchronous, no await
            # crosses this loop — same barrier property add_attach_listener's
            # own docstring states.
            #
            # architect co-vet (issuecomment-5383416113): each callback is
            # isolated — one listener raising must not stop the REST from
            # running, and must not turn into remove() itself raising (this
            # loop runs strictly AFTER `shutil.rmtree(target)` above already
            # succeeded, so the agent is genuinely gone from disk regardless
            # of what any listener does here — a listener's own failure
            # destroys no evidence, CLAUDE.md's third gating question; the
            # `rmtree` failing IS possible, but that raises further up, well
            # before this loop is ever reached, a different and unrelated
            # failure this method already lets propagate). A listener that
            # raised simply did not get to run its own cleanup this time —
            # for AG-UI's listener (SurfaceRegistry.remove, a single dict
            # pop) that reopens exactly the bug this issue closes for that
            # one purge, never a crash or a corrupted registry state.
            for _cb in list(self._remove_listeners):
                try:
                    _cb(name)
                except Exception:
                    logger.exception(
                        "AgentRegistry: a remove-listener raised for purged agent %r "
                        "— continuing with the remaining listeners", name,
                    )
            # PR12: a hard-deleted agent would leave dangling topology references,
            # so drop it from every topology (a team losing its leader / an
            # emptied topology is removed entirely). #2103 MUST-1: return the
            # cascade's topology changes so the async caller emits them logged.
            return self._cascade_agent_removal(name), vanished_sids
        else:
            # #1954 Option A: archive-default. Keep generations in place (rewind
            # works) + tombstone with the archival WAL seq. Hidden from active
            # surfaces (list_active_names); still visible to the rewind/GC
            # substrate (list_names). PRESERVE topology membership — the agent dir
            # survives (no dangling refs), so rewind-to-before-archive restores it
            # to its ORG, not just its state; active topology ops (can_send /
            # _default_topology) skip archived members so it stays dormant. The
            # WAL-window GC hard-purges + cascades once the archival seq leaves the
            # window (slice 2).
            seq = self._state_log.last_durable_seq if self._state_log is not None else 0
            (target / ARCHIVED_MARKER).write_text(str(seq), encoding="utf-8")
        return [], []  # archive does not cascade — topology + sessions preserved (#1954)

    async def archive_agent(self, name: str, *, purge: bool = False) -> None:
        """#2103 S2b: the action-layer DELETE seam — archive (or purge) the agent
        (sync ``remove``) + emit the lifecycle event (``agent_archived`` |
        ``agent_purged``) so rewind reconstructs the as-of-cut archived-state and
        honors the permanent purge (fork A). The ONE delete seam the action-layer
        callers (CLI / web + the spawn op) route through. Emit no-ops without a WAL."""
        # #2597 S2a: close held MCP connections (Option C) for EVERY loaded session of
        # this agent (main + any spawned sids still in memory) before ``self.remove``
        # (sync) drops them from the in-memory map — ``remove`` has no async seam of
        # its own, so this async wrapper is the teardown seam for the main session
        # (mirrors ``remove_session``'s teardown for spawned sessions).
        for sid in list(self._sessions.get(name, {}).keys()):
            session = self._peek_session(name, sid)
            aclose_mcp = getattr(session, "aclose_mcp_connections", None)
            if callable(aclose_mcp):
                await aclose_mcp()
            # #2783: same teardown-completeness gap as remove_session — close
            # FsWatcher/EventStore here too, not just MCP, before ``self.remove``
            # drops the session from the in-memory map.
            aclose_fs_watcher = getattr(session, "aclose_fs_watcher", None)
            if callable(aclose_fs_watcher):
                await aclose_fs_watcher()
            aclose_event_store = getattr(session, "aclose_event_store", None)
            if callable(aclose_event_store):
                await aclose_event_store()
            # #4961 C: same teardown-completeness gap as EventStore above —
            # a 4th instance (see Session.aclose_audit_events's own docstring).
            aclose_audit_events = getattr(session, "aclose_audit_events", None)
            if callable(aclose_audit_events):
                await aclose_audit_events()
            # #5364 §1.4: same teardown-completeness gap — a 5th instance
            # (see Session.aclose_media_store's own docstring).
            aclose_media_store = getattr(session, "aclose_media_store", None)
            if callable(aclose_media_store):
                await aclose_media_store()
        cascade_changes, vanished_sids = self.remove(name, purge=purge)
        if self._state_log is not None:
            await self._state_log.append(
                "agent_purged" if purge else "agent_archived",
                entity_kind="agent", name=name,
            )
            # #2159: a purge's rmtree subsumes the agent's spawned sessions — emit
            # the destroy-side session_vanished mirror for each (the same genuine-
            # destroy record remove_session's record=True path appends, #2154) so
            # the WAL keeps create↔destroy symmetry instead of the sessions just
            # vanishing from the WAL's perspective with no destroy record.
            for sid in vanished_sids:
                await self._state_log.append(
                    "session_vanished", entity_kind="session", name=name, sid=sid,
                )
            # #2103 MUST-1: emit the purge cascade's topology changes through the
            # logged seam so rewind reconstructs the topology config-set consistently.
            for tname, topo in cascade_changes:
                await self._emit_topology(
                    "topology_removed" if topo is None else "topology_updated",
                    tname, topo,
                )

    def last_activity_at(self, name: str) -> datetime | None:
        """Last mtime across history.jsonl and any audit events file.

        history.jsonl lives in `agents/<name>/`; chat audit log lives under
        `events/agents/<name>/chat/<YYYY-MM>/*.jsonl` (PR20). Take the max
        mtime across all those files.
        """
        agent_dir = self._dir / name
        candidates: list[float] = []
        history = agent_dir / "history.jsonl"
        if history.is_file():
            candidates.append(history.stat().st_mtime)
        # PR20: events live outside agents/<name>/. Path is computed relative
        # to .reyn/ root which is the parent of self._dir (= .reyn/agents).
        events_root = self._dir.parent / "events" / "agents" / name / "chat"
        if events_root.is_dir():
            for f in events_root.rglob("*.jsonl"):
                try:
                    candidates.append(f.stat().st_mtime)
                except OSError:
                    continue
        if not candidates:
            return None
        return datetime.fromtimestamp(max(candidates), tz=timezone.utc)

    # ── PR21: crash recovery ─────────────────────────────────────────────────

    @staticmethod
    def _encode_sid_for_dir(sid: str) -> str:
        """FP-0043 S4b-1: bijective-encode a logical sid into a SAFE single-path-
        segment directory name.

        A routing-key sid is ``<transport>:<native_id>`` where native_id is
        arbitrary (webhook source / MCP conn id can carry ``:`` ``/`` whitespace).
        Used verbatim as a dir name that breaks (``/`` → nested/garbled dirs) or is
        non-portable. percent-encode with an EMPTY safe set so every reserved /
        unsafe char (``:`` ``/`` space …) is escaped into one flat segment;
        alphanumerics + ``_.-~`` pass through unchanged, so an existing safe sid
        (uuid hex, "main") encodes to ITSELF = byte-identical for pre-S4b sessions.
        The logical sid is unchanged everywhere else (dict key / WAL session_id /
        _matches_agent filter) — only the filesystem dir component is encoded."""
        return quote(sid, safe="")

    @staticmethod
    def _decode_sid_from_dir(dirname: str) -> str:
        """FP-0043 S4b-1: inverse of ``_encode_sid_for_dir`` — recover the logical
        sid from an on-disk session dir name (round-trip for discovery/restore)."""
        return unquote(dirname)

    def _session_state_dir(self, name: str, sid: str) -> Path:
        """FP-0043 Stage 5: on-disk state dir for ``(name, sid)``.

        The "main" session keeps the legacy agent-level dir (byte-identical
        pre-S5); spawned sessions nest under ``state/sessions/<enc(sid)>/`` — the
        same layout spawn_session's fixup writes to (base-aligned via self._dir).
        S4b-1: the dir component is bijective-encoded so an arbitrary routing-key
        sid (``slack:T123``, ``webhook:a/b``) is a single safe path segment."""
        state_dir = self._dir / name / "state"
        if sid == _DEFAULT_SID:
            return state_dir
        return state_dir / "sessions" / self._encode_sid_for_dir(sid)

    def _session_snapshot_path(self, name: str, sid: str) -> Path:
        """FP-0043 Stage 5: snapshot.json path for ``(name, sid)``."""
        return self._session_state_dir(name, sid) / "snapshot.json"

    def _session_generations_dir(self, name: str, sid: str) -> Path:
        """FP-0043 Stage 5: PITR generations dir for ``(name, sid)``."""
        return self._session_state_dir(name, sid) / "generations"

    def _discover_session_ids(self, name: str) -> list[str]:
        """FP-0043 Stage 5: every session id for ``name`` — "main" + loaded +
        on-disk spawned (``state/sessions/<sid>/``).

        Used by the rewind materialiser, which is shared with crash-recovery
        (sessions not yet loaded), so disk discovery — not just the loaded map —
        is required to bring EVERY session's substrate to the target cut."""
        sids = {_DEFAULT_SID}
        sids.update(self._sessions.get(name, {}).keys())
        sessions_root = self._dir / name / "state" / "sessions"
        if sessions_root.is_dir():
            for child in sessions_root.iterdir():
                if child.is_dir():
                    # S4b-1: dir names are encoded → decode back to logical sid.
                    sids.add(self._decode_sid_from_dir(child.name))
        return sorted(sids)

    async def restore_all(
        self, *, only_names: "set[str] | None" = None
    ) -> dict[str, AgentSnapshot]:
        """Reconstruct each known agent's runtime state from snapshot + WAL.

        Algorithm:
        1. Load every agent's snapshot (or empty)
        2. Find min(applied_seq); tail WAL from there
        3. Apply each WAL entry to the matching agent's snapshot
        4. Save the updated snapshot back (so next restart starts from the
           more advanced point)
        5. For agents with non-empty restored state, instantiate the session
           and call `session.restore_state(snapshot)` to populate inbox /
           pending_chains and re-arm chain timeout watchdogs

        Idempotent: calling twice on a clean state is a no-op.

        ADR-0038 Stage 1d: crash-mid-rewind recovery runs FIRST (before loading
        snapshots) so a reset-record fsync'd before its materialisation completed
        re-materialises both substrates as-of-N — every startup path that calls
        ``restore_all`` gets crash-recovery by construction. No-op without a
        rewind record.

        ``only_names`` (#3671 P4 item C-1): steps 1-4 (WAL replay + the
        durable snapshot re-save) are UNCONDITIONAL regardless of this param —
        every agent's on-disk snapshot is brought current either way, so
        the STATE itself is never lost. Only step 5's DEFAULT-session build
        (``get_or_load`` + ``restore_state`` + ``ensure_running`` — a full
        Session construction plus a live running task) is scoped: with
        ``only_names`` given, an in-flight agent NOT in the set has its
        post-replay snapshot stashed in ``self._pending_restore`` instead of
        being built now; ``get_or_load`` applies it (``restore_state``, once)
        the first time that agent is actually reached — an explicit
        ``attach()``, or a delegation target via ``ensure_running()`` (both
        call ``get_or_load`` internally). ``None`` (every caller except
        ``chat.py``, e.g. ``mcp.py`` — which must be able to serve ANY agent
        name on arbitrary MCP calls, not one known target) is the real,
        still-eager behavior — not a compat default kept only to avoid a
        signature break.

        ⚠️ DELIBERATE crash-recovery semantic change (lead-coder review,
        #3683 — flagged here explicitly so it is never mistaken for a
        performance-only side effect): before this param existed, EVERY
        in-flight agent auto-RESUMED its run-loop at startup (crash-recovery-
        by-construction). With ``only_names`` given, a non-requested in-flight
        agent's state is preserved but its run-loop does NOT auto-resume —
        only "first real use" (attach/delegation) resumes it, and if nothing
        ever reaches it during this process's lifetime, it simply never runs
        this session, even though it was mid-task when the process last
        stopped. The state is not lost (still restorable by a LATER
        ``restore_all`` — e.g. the next process start, or a future
        `only_names` that includes it) but auto-resume itself does not
        happen. See ``test_deferred_agent_does_not_auto_resume_if_never_touched``
        (tests/core/test_registry_restore_all_only_names_3671_p4c1.py) for the
        behavioral pin. Whether this trade-off is acceptable is pending
        explicit owner confirmation as of #3683 (asked by lead-coder) — this
        docstring states the CURRENT actual behavior either way, so a future
        reversal is a deliberate, documented decision, not a silent one.

        Deliberately UNSCOPED by ``only_names`` (so NOT subject to the same
        deferral): a SPAWNED (non-default-sid) session's build in step 5's
        other branch, and ``_rewake_pipeline_runs`` below. The pipeline case
        in particular creates a real ASYMMETRY worth naming: a crashed
        in-flight PIPELINE run still self-resumes unconditionally on every
        ``restore_all()`` call regardless of ``only_names``, while a crashed
        in-flight AGENT (this step) does not, unless requested or reached.
        Both are "genuinely separate mechanisms" in the narrow sense that
        `_rewake_pipeline_runs` doesn't share step 5's code path — but
        whether an OPERATOR should see pipelines auto-resume while ordinary
        agent turns do not is a real product-consistency question, not
        resolved here; narrowed out of this PR's scope rather than silently
        decided either way.
        """
        self._assert_owner_thread()
        if self._state_log is None:
            return {}

        # 0. crash-mid-rewind recovery (no-op without an active reset-record).
        await self.recover_rewind_if_needed()

        # 1. Load snapshots — main (legacy path) + per-session (spawned).
        # FP-0043 Stage 5: ``snapshots`` (name → MAIN AgentSnapshot) is the
        # returned back-compat view; ``all_snaps`` ((name, sid) → AgentSnapshot)
        # drives per-session replay-routing + restore. A legacy install has only
        # the main path → loads as session_id "main" (the migration fallback).
        snapshots: dict[str, AgentSnapshot] = {}
        all_snaps: dict[tuple[str, str], AgentSnapshot] = {}
        # #1954: load only ACTIVE agents at startup — an archived agent's state
        # stays on disk (rewind-reachable) but is not resurrected as a live
        # session (else archive wouldn't survive a restart).
        for name in self.list_active_names():
            state_dir = self._dir / name / "state"
            main_path = state_dir / "snapshot.json"
            if main_path.is_file():
                main_snap = AgentSnapshot.load(name, main_path)
            else:
                main_snap = AgentSnapshot.empty(name)
            snapshots[name] = main_snap
            all_snaps[(name, main_snap.session_id)] = main_snap
            # Spawned sessions persist under <state>/sessions/<sid>/snapshot.json.
            sessions_root = state_dir / "sessions"
            if sessions_root.is_dir():
                for sid_dir in sorted(sessions_root.iterdir()):
                    sp = sid_dir / "snapshot.json"
                    if sp.is_file():
                        # S4b-1: dir name is encoded → decode to the logical sid so
                        # the session restores under its routing-key, not the escaped
                        # form (else get_session(logical_sid) misses post-restore).
                        sid = self._decode_sid_from_dir(sid_dir.name)
                        all_snaps[(name, sid)] = AgentSnapshot.load(
                            name, sp, session_id=sid,
                        )

        if not all_snaps:
            return {}

        # 2-3. WAL replay from min(applied_seq) + 1.
        # #2946 item 2: read the shared tail ONCE, then bucket it by (agent,
        # session_id) — ``AgentSnapshot.event_route_key`` — BEFORE handing entries
        # to snapshots. Handing every snapshot the same full `wal_entries` list (the
        # prior shape) made EACH snapshot's `apply_events` walk the WHOLE tail and
        # call `_matches_agent` on every OTHER (agent, session)'s entries too — an
        # O(agents × tail) re-scan where one lagging idle agent's low applied_seq
        # widens the tail for every OTHER agent's walk as well. Bucketing makes the
        # tail-walk O(tail) once (one route_key lookup per entry) and each
        # snapshot's apply O(its own bucket) — O(tail) total, not O(agents × tail).
        # Structural, not a census fix: this holds regardless of how many (agent,
        # session) pairs restore_all discovers.
        min_seq = min(s.applied_seq for s in all_snaps.values())
        buckets: dict[tuple[str, str], list[dict]] = {}
        for event in self._state_log.iter_from(min_seq + 1):
            key = AgentSnapshot.event_route_key(event)
            if key is not None:
                buckets.setdefault(key, []).append(event)
        for key, snap in all_snaps.items():
            snap.apply_events(buckets.get(key, ()))

        # 4. Save the post-replay snapshots back to their per-session paths.
        for (name, sid), snap in all_snaps.items():
            snap.save(self._session_snapshot_path(name, sid))

        # 5. Hand each non-empty snapshot to its session.
        # PR-intervention-link L4: outstanding_interventions also triggers
        # restore — without it, an agent whose only stranded state is an
        # in-flight ask_user would be skipped here and the user could not
        # clear the queued intervention after restart.
        # FP-0043 S5: the main session is get_or_load'd + ensure_running (live,
        # unchanged); a spawned session is recreated via spawn_session(name, sid)
        # — which re-applies the S5 path fixup — then re-adopts its state. Its
        # run-loop starts lazily on attach_session (S4a), so no auto-run here.
        for (name, sid), snap in all_snaps.items():
            if (not snap.inbox
                    and not snap.pending_chains
                    and not snap.outstanding_interventions):
                continue
            if sid == _DEFAULT_SID:
                # #3671 P4 item C-1: an in-flight agent OUTSIDE only_names is
                # deferred — its snapshot is durably saved above already
                # (step 4), so nothing is lost by not building+running it now.
                if only_names is not None and name not in only_names:
                    self._pending_restore[(name, sid)] = snap
                    continue
                session = self.get_or_load(name)
                session.restore_state(snap)
                await self.ensure_running(name)
            else:
                if not self._has_session(name, sid):
                    # #2708 P3-item3: crash-recovery re-creates a spawned session to re-adopt its
                    # snapshot — a headless re-wake with no attached surface; self-bound reviewed-NA.
                    _routing = ReviewedNA("runtime/registry.py::restore_all")
                    self.spawn_session(
                        name, sid=sid,
                        presentation_consumer=_routing.presentation_consumer,
                        intervention_bridge=_routing.intervention_bridge,
                    )
                session = self._peek_session(name, sid)
                if session is not None:
                    session.restore_state(snap)

        # IS-2: pipeline driver-session re-wake — a SELF-SUFFICIENT scan of
        # `.reyn/pipeline/state/` (invocation.json + terminal marker), deliberately
        # independent of the snapshot-driven step-5 loop: a driver-session that
        # crashed before its first session snapshot has no snapshot.json (never
        # enters all_snaps), and its start-nudge was already consumed (empty
        # inbox) — a "RUNNING-but-empty-inbox" trap. The scan re-creates + re-wakes it from
        # the work-order FILE alone (truncation-surviving, like the R4 gens).
        await self._rewake_pipeline_runs()
        return snapshots

    async def _rewake_pipeline_runs(self) -> "list[str]":
        """IS-2: re-create + re-wake the driver-session of every NON-TERMINAL
        pipeline run found under ``.reyn/pipeline/state/`` (see
        ``reyn.core.pipeline.work_order`` for the run-dir lifecycle files and
        the exactly-once/at-least-once contract). Returns the re-woken run ids
        (for the log / tests).

        Per run dir, in order:
        - terminal marker present → one-stat skip (a delivered run never
          resurrects; scan cost over historical runs stays trivial).
        - work-order absent/corrupt → logged skip (nothing to resume from).
        - rewind guard (DEFAULT-OPEN polarity): skip ONLY when the recorded
          ``spawn_seq`` is provably on an abandoned WAL branch
          (``is_active_seq`` False — the run was rewound away). A missing /
          truncated-away spawn record keeps the run eligible: requiring the
          event to be present would make recovery depend on a truncatable WAL
          entry, exactly what the CLAUDE.md truncate-falsify gate forbids.
        - bump ``attempts.json`` durably BEFORE the wake (the A8 poison cap's
          monotonic counter — a resume that crashes the process still counted;
          the driver terminal-fails past the cap instead of crash-looping).
        - ensure the driver-session EXISTS (re-create via ``spawn_session(name,
          sid=...)`` when the crash predated any session record), swap in a
          fresh ``PipelineExecutorDriver`` built from the work-order, nudge
          (empty user turn), and boot the run-loop pump."""
        if self._state_log is None:
            return []
        from reyn.core.events.config_recovery import reyn_root
        from reyn.core.pipeline.work_order import (
            bump_resume_attempts,
            has_result,
            load_invocation,
        )
        from reyn.runtime.services.pipeline_executor_driver import (
            PipelineExecutorDriver,
        )

        root = reyn_root(self._state_log.path)
        if root is None:
            return []
        state_root = root / "pipeline" / "state"
        if not state_root.is_dir():
            return []
        rewoken: "list[str]" = []
        for run_dir in sorted(state_root.iterdir()):
            if not run_dir.is_dir() or has_result(run_dir):
                continue
            work_order = load_invocation(run_dir)
            if work_order is None:
                logger.warning(
                    "pipeline recovery: run dir %s has no readable invocation.json "
                    "— skipping (nothing to resume from)", run_dir,
                )
                continue
            # #5769 stage 2: work_order.spawn_seq is the WAL seq of the
            # driver session's own spawn call — its real, nameable owner is
            # (driver_agent, driver_sid), read from the SAME work_order
            # right here (architect's #5772-review-triggered finding: this
            # was earlier suspected NOT nameable at this call site; it is —
            # no extra lookup needed, just this reordering). Built per
            # run_dir rather than hoisted once: `build_active_predicate`'s
            # own record fetch is incremental/cached (#2939), so a fresh
            # build per run costs O(rewind records), not O(WAL) — the
            # #2941/#2944 quadratic-WAL-scan concern this loop's own
            # comment used to cite no longer applies to this call.
            is_active = build_active_predicate(
                self._state_log,
                scope=(work_order.driver_agent, work_order.driver_sid),
            )
            if work_order.spawn_seq is not None and not is_active(
                work_order.spawn_seq,
            ):
                # Provably rewound-away (abandoned WAL branch) — do not resurrect.
                continue
            name, sid = work_order.driver_agent, work_order.driver_sid
            if not self.exists(name):
                logger.warning(
                    "pipeline recovery: run %r's driver agent %r no longer exists "
                    "— skipping", work_order.run_id, name,
                )
                continue
            bump_resume_attempts(run_dir)
            if not self._has_session(name, sid):
                # #2708 P3-item3: pipeline driver crash-recovery re-wake — the originally-attached
                # caller is gone, the result routes via the inbox reply address; self-bound reviewed-NA.
                _routing = ReviewedNA("runtime/registry.py::_rewake_pipeline_runs")
                self.spawn_session(
                    name, sid=sid,
                    presentation_consumer=_routing.presentation_consumer,
                    intervention_bridge=_routing.intervention_bridge,
                )
            session = self._peek_session(name, sid)
            if session is None:
                continue
            session.set_loop_driver(PipelineExecutorDriver(
                work_order, registry=self, state_log=self._state_log,
                # IS-6: recovery always delivers via inbox — the originally-
                # attached sync caller (if any) is gone after the crash, so the
                # result must route back through the reply address, never
                # in-band. (A run launched async was already notify_reply=True;
                # a crashed sync run degrades to async inbox delivery here.)
                notify_reply=True,
            ))
            await session.submit_user_text("")  # the no-payload resume nudge (D案)
            self.ensure_session_running(name, sid)
            rewoken.append(work_order.run_id)
        if rewoken:
            logger.info(
                "pipeline recovery: re-woke %d non-terminal run(s): %s",
                len(rewoken), ", ".join(rewoken),
            )
        return rewoken

    # ── Global rewind (ADR-0038 Stage 1c-2, D2 consistent-cut) ──────────────

    def _store_for(self, name: str, sid: str = _DEFAULT_SID) -> SnapshotGenerationStore:
        """Return the snapshot-generation store for session ``(name, sid)``.

        Reuses the live session's store when that session is loaded (so an
        in-flight session and the rewind path share one view of the generations
        dir); otherwise constructs one over the per-session on-disk path. Default
        sid "main" = the legacy agent-level generations dir (byte-identical)."""
        session = self._peek_session(name, sid)
        store = getattr(session, "_generation_store", None)
        if isinstance(store, SnapshotGenerationStore):
            return store
        return SnapshotGenerationStore(
            name, self._session_generations_dir(name, sid),
        )

    async def _await_quiescent_bounded(self, session: "object") -> None:
        """``session.await_quiescent()``, bounded and fail-safe (#4771).

        ``await_quiescent()`` is otherwise unbounded by design — see its own
        docstring's "critical invariant": once it returns, no WAL append can
        still land, because a straggler past the reset-record seq would
        silently contaminate the active branch. This wraps that call for
        REWIND specifically (not shutdown, and not a change to
        ``await_quiescent()`` itself, which stays correct for any other
        caller that genuinely needs to wait out true quiescence).

        On timeout, raises :class:`RewindQuiesceTimeoutError` — the rewind
        ABORTS before the reset-record is ever appended (see that
        exception's own docstring for why this is the opposite tradeoff
        from ``shutdown()``'s bounded wait: a rewound session keeps
        running afterward, so "log and proceed" would risk landing the
        straggler in a session the operator believes was reset — silent
        corruption, not just a hang).

        Cancellation caveat (lead-coder review, #4799): ``asyncio.wait_for``
        cancels the ``await_quiescent()`` coroutine on timeout, but any
        durable WAL write that had ALREADY been handed to
        ``DurabilityWorker.submit`` before that moment is NOT itself
        cancelled — the worker drains its queue on its OWN task,
        independent of the caller that submitted the write (see
        ``durability_worker.py``'s own docstring: "a background task
        drained by ONE background task"). Such a write can still land
        durably after this method raises. This is SAFE specifically
        because ``checkout()`` never reaches step 4 (the reset-record
        append) on this path — there is no reset-record for that write to
        land "past"; it becomes an ordinary entry on the still-live,
        un-rewound branch, exactly as if this rewind attempt had never
        been made.

        The bound value itself is computed by :func:`_quiesce_bound_s` — a
        pure function, kept separate specifically so a test can assert on
        the VALUE (0 connections -> 1 unit, 3 connections -> 3 units)
        instead of racing a real clock against it (lead-coder review,
        #4799: a sleep-vs-bound margin test is flaky under a loaded
        runner, and a failure there can't distinguish "the formula broke"
        from "the runner was slow")."""
        held = len(session.mcp_held_servers())
        bound_s = _quiesce_bound_s(held)
        try:
            await asyncio.wait_for(session.await_quiescent(), timeout=bound_s)
        except TimeoutError as exc:
            raise RewindQuiesceTimeoutError(
                f"session {session.agent_name!r} did not quiesce within "
                f"{bound_s:.1f}s ({held} held MCP connection(s)) — rewind "
                "aborted before the reset-record was appended, to avoid a "
                "straggler WAL append landing past it"
            ) from exc

    def _oldest_kept_seq(self) -> "int | None":
        """The WAL's own PHYSICAL oldest currently-retained seq, or
        ``None`` if the WAL is empty/unwired.

        #5759 stage 2 (lead-coder ruling): extracted from ``checkout()``'s
        own retention guard and ``list_rewind_points()``'s identical
        inline copy (#2236) — a third caller (the history.jsonl GC below)
        would have made this a THIRD independently-typed copy of the same
        expression. All three now call this one accessor, so a future
        change to how "oldest kept" is determined cannot update two call
        sites and silently miss the third — the exact "same guard,
        second copy" shape this codebase already rejects elsewhere.

        Deliberately reads the PHYSICAL floor (what the WAL file itself
        still contains), not the last-computed retention-policy floor —
        under a live policy nothing is truncated between turns, so recent
        history stays reachable; only genuinely-truncated history is
        rejected. See ``checkout()``'s own docstring for why this matters
        (truncated-vs-not is the caller's real question, not the target
        seq's raw numeric value)."""
        if self._state_log is None:
            return None
        oldest = next(iter(self._state_log.iter_from(1)), None)
        return oldest.get("seq") if oldest else None

    def _history_path_for(self, name: str, sid: str = _DEFAULT_SID) -> Path:
        """On-disk ``history.jsonl`` path for ``(name, sid)`` (#5759 stage 2).

        Mirrors the 2 real construction sites this codebase already has —
        ``last_activity_at`` above (main: ``<agent>/history.jsonl``, the
        legacy byte-identical path) and ``spawn_session``'s own per-session
        fixup (spawned: ``<agent>/state/sessions/<enc(sid)>/history.jsonl``,
        i.e. ``_session_state_dir(name, sid) / "history.jsonl"``) — rather
        than re-deriving a third copy of this branch."""
        if sid == _DEFAULT_SID:
            return self._dir / name / "history.jsonl"
        return self._session_state_dir(name, sid) / "history.jsonl"

    def _history_margin_boundary_seq(
        self, name: str, sid: str = _DEFAULT_SID,
    ) -> "int | None":
        """The oldest ``seq`` that startup hydration would still read back
        for session ``(name, sid)`` — history.jsonl condition ④ (#5759
        stage 2, lead-coder ruling). Only entries STRICTLY OLDER than this
        are outside the startup-hydration margin and eligible for GC; this
        boundary itself and everything newer must never be removed.

        Reuses the SAME named constant and the SAME tail-reading function
        real startup hydration already uses (``Session._HISTORY_HYDRATE_
        MIN_LINES`` / ``read_history_tail``, session.py's own 4 call
        sites) rather than a second, independently-typed ``200`` literal
        or a second reader — the exact "same guard, second copy" shape
        ``_oldest_kept_seq`` above already exists to avoid, now for the
        margin instead of the WAL floor.

        Returns ``None`` when history.jsonl is missing/empty (nothing to
        bound — there is no content for GC to consider either)."""
        # Deferred import: mirrors this module's own existing lazy-import
        # pattern (e.g. ``workspace_paths``, ``process_registry`` above) —
        # avoids a module-level session.py <-> registry.py coupling for a
        # single shared constant.
        from reyn.runtime.history_tail_reader import read_history_tail
        from reyn.runtime.session import _HISTORY_HYDRATE_MIN_LINES

        history_path = self._history_path_for(name, sid)
        tail = read_history_tail(history_path, min_lines=_HISTORY_HYDRATE_MIN_LINES)
        if not tail:
            return None
        try:
            seq = json.loads(tail[0]).get("seq")
        except (json.JSONDecodeError, AttributeError):
            return None
        return seq if isinstance(seq, int) else None

    def _history_compacted_ranges(
        self, name: str, sid: str = _DEFAULT_SID,
    ) -> "list[tuple[int, int]]":
        """Every ``(covers_from_seq, covers_through_seq)`` range recorded
        by EVERY ``role="summary"`` line in session ``(name, sid)``'s
        ``history.jsonl`` — #5759 stage 2, architect correction.

        GC needs the UNION of every fold's coverage, not just the latest
        one: an older fold's range sits below the latest summary's own
        ``(covers_from, covers_through)`` pair once a later fold has run
        again (each fold only records its OWN span). Copying the 2
        existing consumers of ``compaction_coverage_from_summary``
        (``router_history_buffer.py``, ``session.py`` — both pass only
        the SINGLE latest summary) would make GC drop almost nothing —
        the exact "discard-only, 0 bytes" shape an earlier #5759 design
        was already rejected for. The parsing function itself is reused
        unchanged (called once per summary found, not redefined) so
        structuralization ② (one accessor) still holds — only the call
        COUNT differs from those 2 existing callers, not the function.

        Fail-closed per summary (matches ``is_seq_still_active``'s own
        per-summary safe-side default): a summary with no ``covers_from_
        seq`` (persisted before #5765) contributes NO range rather than a
        guessed one — never hides content based on it."""
        from reyn.runtime.chat_message import (
            compaction_coverage_from_summary,
            parse_history_line,
        )

        history_path = self._history_path_for(name, sid)
        ranges: list[tuple[int, int]] = []
        if not history_path.is_file():
            return ranges
        with history_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    quick = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(quick, dict) or quick.get("role") != "summary":
                    continue
                msg = parse_history_line(line)
                if msg is None:
                    continue
                covers_from, covers_through = compaction_coverage_from_summary(msg)
                if covers_from is not None and covers_through > 0:
                    ranges.append((covers_from, covers_through))
        return ranges

    def _gc_one_session_history(
        self, name: str, sid: str, oldest_seq: int,
    ) -> dict:
        """The synchronous GC rewrite body for ONE ``(name, sid)`` session's
        ``history.jsonl`` (#5759 stage 2) — run off the event loop by the
        caller (``asyncio.to_thread``; history.jsonl can reach hundreds of
        MB, #4387's own measurement).

        A turn is GC-eligible only when ALL of:
          ① below the WAL's own PHYSICAL oldest-kept seq (``oldest_seq``,
             passed in from ``_oldest_kept_seq()`` — the SAME accessor
             ``checkout()``/``list_rewind_points()`` use, never re-derived
             here as a second copy)
          ② outside the startup-hydration margin
             (``_history_margin_boundary_seq``)
          ③ inside SOME recorded fold's ``[covers_from, covers_through]``
             range (``_history_compacted_ranges``, unioned via the shared
             ``is_seq_still_active`` predicate — #5765's own single place
             for this check)
        A ``role="summary"`` line is NEVER dropped, regardless of its own
        seq: it is the durable EVIDENCE of what a fold covered — dropping
        one would corrupt every future GC pass's own range computation
        (``_history_compacted_ranges`` reads every surviving summary), the
        same reasoning ``truncate_below``'s own ``always_keep_kinds``
        gives ``REWIND_KIND`` reset-records.
        Missing history.jsonl, or nothing ever folded, is a no-op."""
        from reyn.runtime.chat_message import is_seq_still_active
        from reyn.runtime.history_tail_reader import rewrite_history_dropping

        margin_boundary = self._history_margin_boundary_seq(name, sid)
        if margin_boundary is None:
            return {"dropped": 0, "kept": 0}
        ranges = self._history_compacted_ranges(name, sid)
        if not ranges:
            return {"dropped": 0, "kept": 0}

        def should_drop(entry: dict) -> bool:
            if entry.get("role") == "summary":
                return False
            seq = entry["seq"]
            if seq >= oldest_seq or seq >= margin_boundary:
                return False
            return any(
                not is_seq_still_active(seq, covers_from=cf, covers_through=ct)
                for cf, ct in ranges
            )

        history_path = self._history_path_for(name, sid)
        return rewrite_history_dropping(history_path, should_drop=should_drop)

    async def _gc_history_jsonl_below(self, floor: int) -> None:
        """#5759 stage 2: GC every known session's ``history.jsonl`` on the
        SAME throttled pass as the WAL truncation + generation prune above
        (lead-coder ruling: no new trigger mechanism — piggyback on the
        existing pass, never a "compaction just finished" trigger).

        Deliberately re-derives the WAL floor via ``_oldest_kept_seq()``
        rather than trusting the ``floor`` parameter this method receives:
        ``truncate_below`` (called just above, in the caller) is FIRE-AND-
        FORGET — its rewrite drains in a worker, not necessarily done yet
        — so ``_oldest_kept_seq()`` may still report the PRE-truncation
        physical floor for a cycle. That is the fail-safe direction (more
        conservative, never drops a history.jsonl range whose WAL
        counterpart has not actually been truncated away yet), and it is
        the SAME accessor ``checkout()``/``list_rewind_points()`` use —
        never a second, independently-derived floor (structuralization ②).

        Best-effort per session, matching this method's own sibling prune
        steps: one session's failure never blocks another's."""
        oldest_seq = self._oldest_kept_seq()
        if oldest_seq is None:
            return  # nothing has ever been truncated from the WAL yet
        for name in self.list_names():
            for sid in self._discover_session_ids(name):
                try:
                    await asyncio.to_thread(
                        self._gc_one_session_history, name, sid, oldest_seq,
                    )
                except Exception as e:  # noqa: BLE001 — defensive, matches sibling prune steps
                    logger.warning(
                        "history.jsonl GC failed for %r/%r: %s", name, sid, e,
                    )

    async def checkout(
        self, seq: int, *, scope: "tuple[str, str] | None",
    ) -> dict:
        """Consistent-cut checkout to ANY WAL ``seq`` (ADR-0038 D8 Phase-2 /
        ADR-0047 decision 3, session-scoped rewind).

        The unified time-travel primitive: jump the active cut to ``seq`` —
        whether ``seq`` is on the live branch (= undo, the ``rewind_to``
        special case) or on an abandoned/dead branch (= branch-switch / fork
        revival). Unlike ``rewind_to`` there is **no active-target guard**: a
        target on a dead branch is allowed and revives that lineage.

        This needs no new persisted field and no lineage-walk: a single
        guard-lifted reset-record ``(R, seq)`` composes correctly through the
        latest-first ``_abandoned_intervals`` machinery (a newer record subsumes
        an intervening one when its R falls inside the new interval, and an older
        abandonment resurrects when the subsuming record is itself later
        abandoned). Because ``reconstruct`` / ``_materialize_rewind`` recompute
        ``is_active`` from the full chain, the runtime substrate follows the
        *target's* lineage automatically.

        ``scope=GLOBAL_SCOPE`` is the **architecture-enforced global cut**
        (D2), byte-identical to before this parameter existed: one global
        single-seq WAL ⇒ one reset-record moves *every* agent atomically:

          1. retention guard — reject a target truncated out of the WAL (1e).
          2. all-cancel  — ``cancel_inflight`` on every loaded session.
          3. all-quiesce — ``await_quiescent`` on every loaded session (1c-1):
             stop-world THEN settle, so no straggler appends past the record.
          4. append ONE global reset-record (fsync'd before any reconstruct —
             the crash-mid-rewind idempotence keystone, 1b).
          5. reconstruct every KNOWN agent as-of the target lineage (honoring the
             recomputed is_active) + persist a **self-contained** snapshot at
             ``applied_seq = R`` (``restore_all`` replays only > R); loaded
             sessions reset (``reset_for_rewind``) + re-adopt.

        ``scope=(name, sid)`` (ADR-0047 decision 3/4/5) cancels/quiesces
        **only** that session, appends a reset-record **scoped** to it, and
        materialises **only** that ``(name, sid)`` — never touching the
        workspace, config generations, or any other agent/session (decision
        4/6). **The retention guard stays GLOBAL regardless of scope**
        (architect's explicit ruling): the WAL floor is one global fact,
        not owned by any one session, so a scoped checkout is bounded by the
        exact same physical floor a global one is.

        ``_rewind_in_progress`` gates compaction for the whole window.

        No default (required keyword-only): a forgotten ``scope`` here does
        not merely widen a read, it WRITES a real, effectful reset-record
        that rewinds every session atomically — the exact function ADR-0047
        decision 3 names as the session-scoped-rewind boundary. Architect's
        ruling (following the parallel finding on the module-level
        ``checkout`` this method delegates the reset-record write to,
        via ``_append_reset_record``): a default here would be unsafe in
        the dangerous direction (an omitted ``scope`` silently becomes a
        full global rewind, not an error) and every real caller already
        knows its own answer (``rewind_to`` below, and the ``/rewind``
        slash command, are both intentionally-global callers that name
        ``GLOBAL_SCOPE`` explicitly).
        """
        if self._state_log is None:
            raise RuntimeError("checkout requires a state log")
        # 1e (D5): bounded by retention — reject targets truncated out of the WAL.
        # Guard on the PHYSICAL oldest kept seq (not the policy floor): under a
        # live policy nothing is truncated between turns, so recent history stays
        # reachable; only genuinely-truncated history is rejected. UNCHANGED by
        # scope — architect's ruling: the floor is a global fact either way.
        oldest_seq = self._oldest_kept_seq()
        if oldest_seq is not None and seq < oldest_seq:
            raise RewindBeyondRetentionError(
                f"checkpoint seq {seq} is outside the retained WAL (oldest "
                f"kept = {oldest_seq}) — it has been truncated. Configure a deeper "
                "retention window to reach this far back."
            )

        self._rewind_in_progress = True
        try:
            if scope is not None:
                name, sid = scope
                # Scoped cancel/quiesce: ONLY the target session, if loaded
                # (an unloaded session has nothing in-flight to stop-world).
                session = self._peek_session(name, sid)
                if session is not None:
                    await session.cancel_inflight()
                    await self._await_quiescent_bounded(session)
                prior_head = self._state_log.last_durable_seq
                reset_seq = await _append_reset_record(
                    self._state_log, target_seq=seq, scope=scope,
                    supersedes=prior_head,
                )
                agents = await self._materialize_rewind(
                    reconstruct_seq=reset_seq, workspace_at_or_below=seq,
                    scope=scope,
                )
                return {
                    "target_n": seq,
                    "reset_seq": reset_seq,
                    "agents": agents,
                    "scope": list(scope),
                    # #2115: no in-flight background-run machinery exists
                    # (stage1 decouple) — same "always 0" fact the unscoped
                    # path below states explicitly, not a new claim.
                    "in_flight_cancelled": 0,
                    "in_flight_finished": 0,
                }
            sessions = self._iter_sessions()
            # #2115: the in-flight-task snapshot would be taken BEFORE the cancel
            # here, but background run machinery was removed (stage1 decouple):
            # no in-flight tasks exist; the summary always reflects 0 cancelled /
            # 0 finished.
            inflight_tasks: list = []
            # 2. all-cancel (stop-world).
            for session in sessions:
                await session.cancel_inflight()
            # 3. all-quiesce (re-drain to a fixpoint — no append lands past the reset).
            # #4771: bounded + fail-safe — see _await_quiescent_bounded's own
            # docstring. A session that can't confirm quiescence within its
            # bound aborts the WHOLE checkout here, before step 4 ever
            # appends the reset-record — nothing has been written yet, so
            # this is a clean, no-op failure for the caller to retry or
            # investigate, not a partial/torn rewind.
            for session in sessions:
                await self._await_quiescent_bounded(session)
            # 4. single global reset-record; supersedes = prior active head (audit).
            prior_head = self._state_log.last_durable_seq
            reset_seq = await _append_reset_record(
                self._state_log, target_seq=seq, scope=GLOBAL_SCOPE, supersedes=prior_head,
            )
            # 5. materialise both substrates along the target lineage.
            agents = await self._materialize_rewind(
                reconstruct_seq=reset_seq, workspace_at_or_below=seq,
            )
            # #2115: the ACTUAL in-flight disposition (truthful rewind summary).
            in_flight_cancelled, in_flight_finished = _count_inflight_disposition(
                inflight_tasks
            )
            return {
                "target_n": seq,
                "reset_seq": reset_seq,
                "agents": agents,
                "in_flight_cancelled": in_flight_cancelled,
                "in_flight_finished": in_flight_finished,
            }
        finally:
            self._rewind_in_progress = False

    async def rewind_to(self, target_n: int) -> dict:
        """Phase-1 undo: the active-node special case of ``checkout`` (ADR-0038 1c-2).

        Thin wrapper — validates ``target_n`` is on the **active branch** up front
        (so a bad target never cancels live work), then delegates to ``checkout``.
        The active-target guard lives HERE, not in the shared core: Phase-1 undo
        only rewinds along the live timeline, while ``checkout`` lifts it for
        Phase-2 branch-switch.

        Raises ``RewindIntoAbandonedError`` if ``target_n`` is on an abandoned
        branch (switching branches is a Phase-2 fork, not Phase-1 undo — use
        ``checkout``).

        No ``scope`` parameter of its own: like the module-level ``rewind``,
        this is the pre-#5769, genuinely global Phase-1 undo primitive — it
        passes ``GLOBAL_SCOPE`` to ``checkout`` explicitly.
        """
        if self._state_log is None:
            raise RuntimeError("rewind_to requires a state log")
        if not is_active_seq(self._state_log, target_n, scope=GLOBAL_SCOPE):
            raise RewindIntoAbandonedError(
                f"rewind target seq {target_n} is on an abandoned branch — "
                "Phase-1 undo only rewinds to a seq on the active timeline "
                "(use checkout for a Phase-2 branch-switch)."
            )
        return await self.checkout(target_n, scope=GLOBAL_SCOPE)

    def list_rewind_points(self, *, include_abandoned: bool = False) -> list[dict]:
        """Enumerate rewind targets for the time-travel UI (1f / Phase-2 fork).

        Returns one row per snapshot-generation boundary, ascending by seq::

            [{"seq": int, "ts": str, "kind": str, "anchor": str, "branch_id": int,
              "name": str | None, "sid": str | None}, ...]

        Default (``include_abandoned=False``) keeps only **active-branch** boundaries
        (Phase-1 1f timeline). Phase-2 fork UX passes ``include_abandoned=True`` to
        get every branch's boundaries (the tree), each tagged with its
        ``branch_id`` (#1533 2a→2b). **`branch_id` is the lineage-correct membership
        source** — group rows by it (a branch's `[fork_point, head]` *range*
        physically contains its abandoned children's seqs, so range-intersection
        over-includes; the substrate segment-map resolves true ownership).

        ``seq`` is the WAL boundary the user can ``rewind_to``. ``ts`` and
        ``kind`` are read from the WAL entry at that seq (the EventStore /
        audit log is a *separate* log and is intentionally not consulted —
        WAL and audit stay decoupled). ``kind`` is an OS-level execution
        boundary derived from the WAL entry kind (P7-safe — all source kinds
        live in ``WAL_EVENT_KINDS``, none are skill/domain strings):

          - ``step_completed`` / ``step_failed``          → ``plan-step``
          - anything else (``inbox_consume``, …)           → ``turn``

        Generations are per-agent but keyed by the single global WAL seq, so
        the union across known agents is the global rewind-point set. Abandoned
        (rewound-past) boundaries are filtered out via ``is_active_seq``.

        #5769 stage 3 (③, architect scope): ``name``/``sid`` — every row's
        ORIGIN ``(agent_name, session_id)`` pair, not only the agent's
        default ("main") session. Before this stage the loop below
        enumerated only ``_store_for(name)`` (the implicit
        ``_DEFAULT_SID``), so a checkpoint cut by a spawned subagent
        session's own generation store was invisible here entirely, not
        merely unlabeled — ``_discover_session_ids`` (the same
        disk+in-memory discovery the rewind materialiser already depends
        on for crash recovery) now supplies every sid to fold in.

        A boundary seq is *architecturally* the origin of exactly one
        ``(name, sid)`` pair (a WAL entry belongs to one session's own
        turn/step by construction — the same "seq is globally unique per
        event" property today's per-agent union already relies on).
        **Architecturally guaranteed is not the same as checked** (#5782
        review, architect BLOCKING): this method used to look the pair up
        with ``.get(s, _DEFAULT_SID)``, which — had the invariant ever been
        violated by a future bug — would have silently handed back
        ``"main"``: not a placeholder, a REAL session's own name, wired
        straight into the row the operator clicks to choose which session
        to rewind. That is the exact "answer from a fallback instead of
        admitting the owner can't be named" shape decision 7 (ADR-0047)
        already forbids — it had simply reappeared inside this row instead
        of a scope predicate. So the lookup is now checked, not assumed:
        a seq claimed by two DIFFERING ``(name, sid)`` pairs is logged
        (this should never happen) and ``name``/``sid`` come back ``None``
        for that row rather than either owner's real value — an admitted
        "don't know", never a fabricated one. The pair also travels
        TOGETHER on one row (not ``sid`` alone) — a consumer that obtained
        ``name`` from a different source (e.g. "whichever agent tab is
        open") could otherwise present a ``(name, sid)`` combination that
        never actually owned this seq; carrying both from the same lookup
        makes that misattribution unwritable.

        ⚠️ Scope boundary (explicit, per dispatch): these fields only make
        the DATA available. Whether/how a caller surfaces them (grouping,
        a column, a filter) is a presentation decision left to whoever
        wires the timeline UI to it (owner-gated) — not decided here.

        Empty when there is no WAL or no generations.
        """
        if self._state_log is None:
            return []

        # #2236: compute the WAL retention floor using the SAME source as
        # checkout() so the list and the checkout guard agree by
        # construction. Points below this floor would always be
        # rejected by checkout — advertising them is misleading.
        oldest_seq = self._oldest_kept_seq()

        # Union of generation boundary seqs across every known agent AND every
        # sid of each (#5769 stage 3 ③ — was agent-default-sid-only through
        # stage 2). Default = active branch only (1f); include_abandoned =
        # all branches (Phase-2 tree).
        seqs: set[int] = set()
        # None once written = seen more than one DIFFERING (name, sid) claim
        # this seq — an admitted "don't know", never a fabricated owner
        # (#5782 review, architect BLOCKING; see the method's own docstring).
        seq_owner: dict[int, "tuple[str, str] | None"] = {}
        for name in self.list_names():
            # #5769 stage 3: every sid this agent has (main + loaded +
            # on-disk spawned — the SAME discovery the rewind materialiser
            # already depends on, `_discover_session_ids`'s own docstring),
            # not only `_DEFAULT_SID`. Built per (name, sid) rather than
            # hoisted once: `build_active_predicate`'s own record fetch is
            # incremental/cached (#2939), so this is O(rewind records) per
            # (agent, sid), not O(WAL) — the #2941 quadratic-WAL-scan
            # concern this loop's own comment used to cite no longer
            # applies (only the seq-independent DERIVATION was ever the
            # expensive part; that stays cached regardless of how many
            # times a scope filter runs over it).
            for sid in self._discover_session_ids(name):
                is_active = build_active_predicate(
                    self._state_log, scope=(name, sid),
                )
                for s in self._store_for(name, sid).seqs():
                    if oldest_seq is not None and s < oldest_seq:
                        continue  # #2236: truncated out of WAL — not reachable
                    if include_abandoned or is_active(s):
                        seqs.add(s)
                        # A boundary seq is the origin of exactly one (name,
                        # sid) pair by construction (see this method's own
                        # docstring) — this SHOULD never fire. #5769 stage 3
                        # ④ (architect's re-written acceptance item, #5782
                        # review): a seq without EXACTLY one owner must be
                        # represented AS SUCH — never a fabricated value, not
                        # even a first-seen-wins guess. So a second, DIFFERING
                        # claim flips the entry to ``None`` rather than
                        # keeping either owner.
                        claim = (name, sid)
                        if s not in seq_owner:
                            seq_owner[s] = claim
                        elif seq_owner[s] is not None and seq_owner[s] != claim:
                            logger.warning(
                                "list_rewind_points: seq %d claimed by both "
                                "%r and %r — this should be structurally "
                                "impossible; reporting no owner rather than "
                                "either guess (see #5769 stage 3 ④)",
                                s, seq_owner[s], claim,
                            )
                            seq_owner[s] = None
        if not seqs:
            return []

        # One pass over the WAL to map boundary seq → (ts, kind). The audit
        # EventStore is NOT consulted — keeping WAL and audit decoupled.
        wal_at: dict[int, dict] = {}
        for entry in self._state_log.iter_from(oldest_seq if oldest_seq is not None else 1):
            s = entry.get("seq")
            if isinstance(s, int) and s in seqs:
                wal_at[s] = entry

        anchors = self.anchor_store
        # #1533 2a→2b: lineage-correct branch membership per checkpoint seq.
        # #5789: `branch_ids_for` is now SCOPED (decision table, #5786
        # review) -- a seq's branch_id must be derived under its OWN
        # owner's abandoned-interval view (global + that owner's own
        # scoped records), never one global tree blindly applied to every
        # owner's seqs (the same class of bug #5786 fixed for
        # `reconstruct`). `seq_owner` already has each seq's real owner
        # (or `None` for the structurally-impossible ambiguous case, see
        # above) -- grouped here and called once per owner, merged into
        # one dict. An unresolved (`None`) owner gets `GLOBAL_SCOPE`: with
        # no nameable owner to ask for, the global-only view is the
        # honest "don't know" answer, not a guess (ADR-0047 decision 7).
        by_owner: "dict[tuple[str, str] | None, list[int]]" = {}
        for s in seqs:
            by_owner.setdefault(seq_owner.get(s), []).append(s)
        branch_of: dict[int, int] = {}
        for owner, owner_seqs in by_owner.items():
            branch_of.update(
                branch_ids_for(
                    self._state_log, sorted(owner_seqs),
                    scope=owner if owner is not None else GLOBAL_SCOPE,
                ),
            )
        rows: list[dict] = []
        for s in sorted(seqs):
            entry = wal_at.get(s, {})
            # #5769 stage 3 ③ (re-fixed per #5782 review, architect
            # BLOCKING): the (name, sid) pair that cut this generation —
            # carried TOGETHER, off the SAME lookup, never ``sid`` alone
            # (a consumer sourcing ``name`` separately could otherwise
            # present a pair that never actually owned this seq). ``None``
            # for BOTH when ``seq_owner`` never saw exactly one claim for
            # this seq (structurally shouldn't happen — see the method's
            # own docstring) — an admitted "don't know", never the old
            # ``.get(s, _DEFAULT_SID)`` fallback, which fabricated a REAL
            # session's name ("main") for a row the operator clicks to
            # choose which session to rewind.
            owner = seq_owner.get(s)
            owner_name, owner_sid = owner if owner is not None else (None, None)
            rows.append({
                "seq": s,
                "ts": entry.get("ts", ""),
                "kind": _rewind_point_kind(entry.get("kind", "")),
                # #1547: per-checkpoint preview anchor ("" when none). Additive —
                # existing consumers ignore it; the timeline widget renders it as
                # a 2nd dim line. Keyed by the same WAL seq → trivial lookup.
                "anchor": anchors.get(s) if anchors is not None else "",
                # #1533 2a→2b: the branch this checkpoint belongs to (group by this).
                "branch_id": branch_of.get(s, 0),
                "name": owner_name,
                "sid": owner_sid,
            })
        return rows

    def list_branches(self, *, scope: "tuple[str, str] | None") -> "list[Branch]":
        """The derived branch tree for the fork UX (#1533 Phase-2 2a / D8).

        ``[Branch(branch_id, fork_point_seq, head_seq, parent_branch_id,
        is_active)]`` derived from the reset-record chain (no stored registry).
        Tree topology (nesting/active); per-branch checkpoint *membership* comes
        from ``list_rewind_points(include_abandoned=True)`` rows' ``branch_id``.
        Empty when there is no WAL.

        #5789: ``scope`` is required, no default -- SCOPED (decision table,
        #5786 review): "the fork UX's branches are the OWNER's own
        branches." Callers wiring a cross-session picker alongside
        ``list_rewind_points``'s own multi-owner rows should pass
        ``GLOBAL_SCOPE`` to keep every row's branch_id resolvable in the
        returned tree (a session-scoped tree would omit branches other
        sessions' rows reference) -- see ``interfaces/slash/rewind.py``'s
        own call for the disclosed reasoning.
        """
        if self._state_log is None:
            return []
        return list_branches(self._state_log, scope=scope)

    def predecessor_turn_checkpoint(
        self, seq: int, *, scope: "tuple[str, str] | None",
    ) -> int | None:
        """The lineage-correct prior **turn** checkpoint of ``seq`` (#1533 2c edit).

        The 2c edit flow re-runs an edited turn from the state before it: checkout
        this predecessor, then submit the edit (a new fork). The result is the
        immediately-prior checkpoint that is a **turn** (phase cuts are
        skipped — they cut intra-turn checkpoints, but an
        edit must return to the prior *turn*) AND on ``seq``'s **lineage** (its branch
        + ancestors back to the fork-point — so a forked branch's first turn resolves
        to the parent's fork-point turn, not a same-branch-only miss).

        ``None`` when there is no prior turn (``seq`` is the first turn = genesis):
        the UX disables first-turn edit. Genesis-checkout is intentionally NOT
        offered — there is no captured pre-turn-1 workspace version, so it would be
        workspace-incoherent (coherent genesis = a future session-start capture).

        #5789: ``scope`` is required, no default -- SCOPED, same family as
        ``list_branches``/``branch_ids_for`` (decision table, #5786
        review): an edit's own lineage is its OWNER's lineage. No real
        caller exists yet (the 2c edit-flow wiring this serves has not
        landed) -- ``scope=GLOBAL_SCOPE`` preserves this method's own
        pre-#5789 behavior byte-for-byte (checkpoints across every known
        agent's default session); ``scope=(name, sid)`` narrows to that
        one session's own checkpoints only, for whenever the edit flow
        lands and needs it.
        """
        if self._state_log is None:
            return None
        # Checkpoint seqs = generation boundaries across every known agent (all
        # branches — the lineage walk may cross to a parent/ancestor branch).
        # #5789: `scope=GLOBAL_SCOPE` keeps this exact cross-agent collection
        # (byte-identical to pre-#5789); a real `(name, sid)` narrows to
        # just that owner's own store.
        cps: set[int] = set()
        if scope is None:
            for name in self.list_names():
                cps.update(self._store_for(name).seqs())
        else:
            cps.update(self._store_for(*scope).seqs())
        if not cps:
            return None
        # Turn-kind filter via the WAL entry kind at each boundary (one pass;
        # reuses _rewind_point_kind for consistency with list_rewind_points — the
        # audit EventStore is not consulted, WAL/audit stay decoupled).
        turn_cps: list[int] = []
        for entry in self._state_log.iter_from(1):
            s = entry.get("seq")
            if isinstance(s, int) and s in cps and _rewind_point_kind(entry.get("kind", "")) == "turn":
                turn_cps.append(s)
        return lineage_predecessor(self._state_log, turn_cps, seq, scope=scope)

    @property
    def anchor_store(self) -> AnchorStore | None:
        """The per-checkpoint anchor store (#1547), lazily built. None w/o WAL."""
        if self._state_log is None:
            return None
        if self._anchor_store is None:
            self._anchor_store = AnchorStore(
                self._project_root / ".reyn" / "generation-anchors.json",
            )
        return self._anchor_store

    def _created_at_map(self) -> "dict[tuple[str, str, str], int]":
        """#2103: (entity_kind, name, sid) → the WAL seq at which the entity was
        created, scanned from the registered create-event kinds. Empty when no
        kinds are registered (the no-op default) or there is no WAL. Drives the
        as-of-cut DROP primitive in ``_materialize_rewind``."""
        if not self._create_event_kinds or self._state_log is None:
            return {}
        created: dict[tuple[str, str, str], int] = {}
        for entry in self._state_log.iter_from(0):
            if entry.get("kind") not in self._create_event_kinds:
                continue
            seq = entry.get("seq")
            if not isinstance(seq, int):
                continue
            key = (
                str(entry.get("entity_kind", "")),
                str(entry.get("name", "")),
                str(entry.get("sid", "")),
            )
            created[key] = seq  # a create is unique; last-write-wins is moot
        return created

    def _session_vanished_map(self) -> "dict[tuple[str, str], int]":
        """#2154: per (agent, sid), the latest ``session_vanished`` seq from the WAL —
        the destroy-side mirror of the ``session_spawned`` create-cut in
        ``_created_at_map``. Reconstruction drops a session that vanished at-or-before
        the cut (it was gone as-of-cut). Empty without a WAL."""
        vanished: dict[tuple[str, str], int] = {}
        if self._state_log is None:
            return vanished
        for entry in self._state_log.iter_from(0):
            if entry.get("kind") != "session_vanished":
                continue
            name = entry.get("name")
            sid = entry.get("sid")
            seq = entry.get("seq")
            if isinstance(name, str) and isinstance(sid, str) and isinstance(seq, int):
                vanished[(name, sid)] = seq  # last wins = the latest vanish
        return vanished

    def _drop_agent(self, name: str) -> None:
        """#2103: tear down an agent created after the rewind cut. A post-cut agent
        has NO pre-cut generations → nothing to preserve → a clean drop (vs the
        #1954 archive HIDE on the delete side). rmtree subsumes the agent's sessions
        (they nest under the agent dir). Best-effort, but a failure is LOGGED (not
        silently swallowed) so a stuck teardown is visible (#2114 review note)."""
        import shutil
        try:
            shutil.rmtree(self._dir / name)
        except FileNotFoundError:
            pass  # already gone — fine
        except OSError as e:  # noqa: BLE001 — best-effort; never raise into rewind
            logger.warning("#2103: drop of agent %r failed (left on disk): %s", name, e)

    def _agent_lifecycle(
        self,
    ) -> "tuple[dict[str, tuple[int, dict, str | None, tuple[int, float | None] | None]], dict[str, int], set[str]]":
        """#2103 S2: one WAL scan → the agent-lifecycle state (created, archived,
        purged):
        - created: name → (create_seq, profile-payload, parent, parent_seq) from
          ``agent_created`` (the payload re-materialises the profile on a
          forward-checkout-past-drop; ``parent`` (#2103 B) rebuilds the spawn lineage
          as-of-cut so a re-materialised child regains its ⊆-parent cap — else
          escalation-on-rewind; ``parent_seq`` (#2103 C2b, #5084: now the parent
          AGENT DIRECTORY's own ``(ino, st_birthtime)``, not an in-memory
          counter) is the parent's identity
          AT-SPAWN, so the rebuilt edge reads STALE if the parent name was later
          purged+reused → no resurrection of the child under the reused parent).
        - archived: name → latest ``agent_archived`` seq (the as-of-cut hide hinge).
        - purged: names with an ``agent_purged`` event (fork A: permanent — never
          re-materialised at any cut).
        Empty without a WAL. Inert until S2b emits the events."""
        created: "dict[str, tuple[int, dict, str | None, tuple[int, float | None] | None]]" = {}
        archived: dict[str, int] = {}
        purged: set[str] = set()
        if self._state_log is None:
            return created, archived, purged
        for entry in self._state_log.iter_from(0):
            kind = entry.get("kind")
            name = entry.get("name")
            seq = entry.get("seq")
            if not isinstance(name, str) or not isinstance(seq, int):
                continue
            if kind == "agent_created":
                payload = entry.get("profile")
                _parent = entry.get("parent")
                _parent_seq = entry.get("parent_seq")  # #2103 C2b: parent identity-at-spawn
                # #5084: WAL/JSON round-trips a tuple as a 2-element list —
                # normalise back to a tuple so equality against a freshly
                # stat'd (ino, st_birthtime) pair compares correctly (a list
                # would never equal a tuple).
                _parent_seq_tuple = (
                    tuple(_parent_seq)
                    if isinstance(_parent_seq, (list, tuple)) and len(_parent_seq) == 2
                    else None
                )
                created[name] = (
                    seq,
                    payload if isinstance(payload, dict) else {},
                    _parent if isinstance(_parent, str) else None,
                    _parent_seq_tuple,
                )
            elif kind == "agent_archived":
                archived[name] = seq  # last wins = the latest archival
            elif kind == "agent_purged":
                purged.add(name)
        return created, archived, purged

    def _rematerialise_agent(self, name: str, profile_payload: dict) -> None:
        """#2103 S2: re-create a dropped agent's profile from its ``agent_created``
        record (the inverse of ``_drop_agent``), so a forward-checkout past the
        create brings the agent back. Its per-agent generations were rmtree'd on the
        drop, so the subsequent reconstruct replays the WAL from 0 for it — correct,
        just unoptimised for this rare forward-checkout-past-drop path."""
        prof = AgentProfile(
            name=name,
            role=str(profile_payload.get("role", "")),
            created_at=str(profile_payload.get("created_at", "")),
            allowed_mcp=profile_payload.get("allowed_mcp"),
        )
        prof.save(self._dir / name)

    def _reconcile_archived_as_of_cut(self, archived: "dict[str, int]") -> None:
        """#2103 S2: rewrite each present agent's ``.archived`` tombstone to the
        as-of-cut archived-state, using ``is_active_seq`` as the canonical predicate:

        • Pre-target (aseq ≤ N): ``is_active_seq=True`` → write marker (was archived).
        • Abandoned branch (N < aseq < R): ``is_active_seq=False`` → clear marker
          (agent was active as-of-target; the archival is on the discarded branch).
        • Post-rewind active (aseq > R): ``is_active_seq=True`` → write marker (the
          archival happened after the completed rewind and must be preserved).

        The single-cut ``aseq ≤ N`` had a symmetric gap: a post-rewind archive
        (aseq > R > N) gave aseq > N → marker unlinked → agent un-archived on crash
        recovery. Both production callers guarantee a rewind record exists (see
        comment at the ``drop_cut`` assignment above). Inert when no ``agent_archived``
        events exist (the #1954 file-only tombstone is left untouched)."""
        # Hoisted once for the whole archived.items() scan (fix-class sibling of
        # #2941/restore_all — the seq-independent derivation must not re-scan the
        # WAL once per archived agent).
        # #5769: archival is agent-wide, not owned by any one session
        # (ADR-0047 decision 6 — "agent-level lifecycle is global; only
        # session-level state is scoped" — archiving preserves every
        # session, #1954) ∴ GLOBAL_SCOPE here is the FINAL answer, not a
        # placeholder pending a later decision.
        is_active = (
            build_active_predicate(self._state_log, scope=GLOBAL_SCOPE)
            if self._state_log is not None
            else None
        )
        for name, aseq in archived.items():
            target = self._dir / name
            if not target.is_dir():
                continue
            marker = target / ARCHIVED_MARKER
            if is_active is not None and is_active(aseq):
                marker.write_text(str(aseq), encoding="utf-8")
            elif marker.is_file():
                marker.unlink()

    def _topology_lifecycle(
        self,
    ) -> "dict[str, list[tuple[int, str, dict | None]]]":
        """#2103 Piece-2: one WAL scan → per WAL-TRACKED topology name, its ordered
        lifecycle events ``(seq, kind, payload)`` from ``topology_created`` /
        ``topology_updated`` / ``topology_removed`` (payload = FULL config; None for a
        removal). Sourced from the WAL only — never the rotated #P6 audit log.
        MUST-2: only names that appear here are WAL-tracked, so only these are touched
        by reconstruction — pre-WAL/untracked topologies are invisible to this map and
        left alone. Empty without a WAL."""
        events: dict[str, list[tuple[int, str, dict | None]]] = {}
        if self._state_log is None:
            return events
        for entry in self._state_log.iter_from(0):
            kind = entry.get("kind")
            if kind not in ("topology_created", "topology_updated", "topology_removed"):
                continue
            name = entry.get("name")
            seq = entry.get("seq")
            if not isinstance(name, str) or not isinstance(seq, int):
                continue
            payload = entry.get("topology") if kind != "topology_removed" else None
            events.setdefault(name, []).append((seq, kind, payload))
        return events

    def _reconcile_topologies_as_of_cut(self, cut: int) -> None:
        """#2103 Piece-2: reconstruct the topology config-set as-of-cut from the
        lifecycle WAL (WAL-sourced only — never the rotated audit log). The LATEST
        ACTIVE event decides per WAL-tracked topology name: created/updated → exists
        with that FULL config; removed (or no active event, i.e. created-after-cut) →
        gone. Reconcile both the on-disk YAML and the in-memory ``_topologies`` map.
        MUST-2: ONLY WAL-tracked names are touched — untracked/pre-WAL topologies are
        never created, mutated, or deleted here. Inert without lifecycle events.

        #2405: ``is_active_seq`` replaces the former ``e[0] ≤ cut`` filter — same
        symmetric gap as vanish/archive: post-rewind active mutations (seq > R) were
        excluded, reverting topology state to as-of-N on crash recovery."""
        # Hoisted once for the whole (names x lifecycle-events) scan — same
        # fix-class sibling as above: one WAL scan for every topology name's
        # events, not one scan per event.
        is_active = (
            build_active_predicate(self._state_log, scope=GLOBAL_SCOPE)
            if self._state_log is not None
            else None
        )
        for name, evs in self._topology_lifecycle().items():
            if is_active is not None:
                latest = max(
                    (e for e in evs if is_active(e[0])),
                    key=lambda e: e[0], default=None,
                )
            else:
                latest = max(
                    (e for e in evs if e[0] <= cut), key=lambda e: e[0], default=None,
                )
            path = self._topology_dir / f"{name}.yaml"
            if latest is None or latest[1] == "topology_removed":
                # Didn't exist as-of-cut (created-after-cut OR removed-≤-cut) → drop.
                self._topologies.pop(name, None)
                if path.is_file():
                    path.unlink()
            else:
                payload = latest[2] or {}
                topo = Topology(
                    name=payload.get("name", name),
                    kind=payload.get("kind", "network"),
                    members=tuple(payload.get("members") or ()),
                    leader=payload.get("leader"),
                    created_at=payload.get("created_at", ""),
                    profiles=dict(payload.get("profiles") or {}),
                )
                topo.save(path)
                self._topologies[name] = topo

    def _config_generation_store(self):
        """The config-as-snapshot generation store (#2259 PR-1). Full-state config
        generations under ``.reyn/config/generations/`` — truncation-surviving bases (they
        replace the truncatable `config_changed` WAL event that lost config below the floor)."""
        from reyn.core.events.config_generations import ConfigGenerationStore  # noqa: PLC0415
        from reyn.core.events.config_recovery import config_generations_dir  # noqa: PLC0415
        return ConfigGenerationStore(config_generations_dir(self._project_root / ".reyn"))

    def _agent_identity_generation_store(self):
        """The agent-identity-as-snapshot generation store (#2259 PR-1b). Per-agent full-state
        identity + frozen lineage under ``.reyn/state/agent_identity/`` — truncation-surviving
        bases (they replace the truncatable `agent_created` WAL event that lost identity/lineage
        below the floor → escalation-on-rewind). Same pattern as the config store (PR-1)."""
        from reyn.core.events.agent_identity_generations import (  # noqa: PLC0415
            AgentIdentityGenerationStore,
        )
        return AgentIdentityGenerationStore(
            self._project_root / ".reyn" / "state" / "agent_identity",
        )

    def _record_agent_identity_generation(self, name: str) -> None:
        """#2259 PR-1b + PR-2b: persist ``name``'s identity (``create_seq`` = its in-memory id) +
        frozen spawn edge as a truncation-surviving generation, keyed by the DURABLE
        ``agent_created`` WAL seq — so a rewind reconstructs the ⊆-parent cap from the generation,
        NOT from the `agent_created` WAL event (truncation drops it → escalation-on-rewind).

        PR-2b: the keying seq is assigned in the worker (seq-in-worker), so the gen record runs
        in a worker job that reads ``last_assigned_seq`` (= the paired ``agent_created`` append's
        seq, FIFO-before this job). No await between the append_nowait + this call → atomic pair
        (invariant #2). No-op without a WAL."""
        if self._state_log is None:
            return
        edge = self._spawn_lineage.get(name)
        create_id = self._agent_create_seq.get(name, 0)
        spawn_parent = edge[0] if edge else None
        spawn_parent_seq = edge[1] if edge else None
        log = self._state_log
        store = self._agent_identity_generation_store()

        async def _record() -> None:
            store.record(
                name,
                create_seq=create_id,
                spawn_parent=spawn_parent,
                spawn_parent_seq=spawn_parent_seq,
                seq=log.last_assigned_seq,
            )

        log.submit_durable_nowait(_record)

    def _agent_identity_as_of_cut(
        self, cut: int,
    ) -> "dict[str, tuple[int, str | None, tuple[int, float | None] | None]]":
        """#2259 PR-1b: per-agent identity + frozen lineage as-of-cut from the truncation-
        surviving generations — the latest generation ≤ cut per agent. Returns
        ``{name: (create_seq, spawn_parent, spawn_parent_seq)}`` (#5084: ``spawn_parent_
        seq`` is now the agent DIRECTORY's own ``(ino, st_birthtime)``, not an in-memory
        counter). The rewind rebuild
        prefers this (survives truncation) over the `agent_created` WAL scan.

        #2405: ``≤ cut`` here is INTENTIONAL — unlike topology/config (which change
        post-rewind and must use ``is_active_seq``), agent identity is SET ONCE at spawn
        and never mutated. ``cut = drop_cut = N`` recovers the truncation-surviving
        pre-N lineage (the security linchpin for ⊆-parent caps after WAL truncation).
        Post-rewind active agents (C' > R) are NEW entities whose first generation is at
        seq > R > N — not "updated" versions of pre-N agents. Their identity is not
        established as-of-N and is set fresh when first accessed via ``spawn_recorded``
        (line 654). The WAL-scan fallback at the call site (``ag_created`` loop) uses
        the same ``≤ drop_cut`` for the same reason."""
        store = self._agent_identity_generation_store()
        out: "dict[str, tuple[int, str | None, tuple[int, float | None] | None]]" = {}
        for name in store.names():
            latest = store.latest_at_or_below(name, cut)
            if latest is None:
                continue  # first generation after the cut → didn't exist as-of-cut
            _seq, data = latest
            _raw_pseq = data.get("spawn_parent_seq")
            # #5084: JSON round-trips a tuple as a 2-element list — normalise
            # back to a tuple (see _agent_lifecycle's own identical note).
            _pseq = (
                tuple(_raw_pseq)
                if isinstance(_raw_pseq, (list, tuple)) and len(_raw_pseq) == 2
                else None
            )
            out[name] = (
                int(data.get("create_seq", _seq)),
                data.get("spawn_parent"),
                _pseq,
            )
        return out

    async def record_config_change(self, rel_path: str, content: dict) -> None:
        """#2259 PR-1: record the FULL post-mutation content of a recovery-core config
        registry as a truncation-surviving generation keyed by the current WAL head. A
        dedicated config op calls this AFTER persisting its `.yaml`; the yaml is a derived
        projection — the generation is the recovery base (it survives WAL truncation, unlike
        the former `config_changed` event). No-op without a WAL (the opt-in / test contract)."""
        if self._state_log is None:
            return
        self._config_generation_store().record(
            rel_path, content, self._state_log.last_durable_seq,
        )

    def _reconcile_config_as_of_cut(self, cut: int) -> None:
        """#2259 PR-1: reconstruct the recovery-core config registries as-of-cut from the
        config GENERATIONS (truncation-surviving, full-state). Per registry, restore the
        LATEST ACTIVE generation (each generation is complete — no forward-replay). A
        registry with no active generation didn't exist on the active branch → removed.
        Only generation-tracked registries are touched (operator-owned / pre-feature yaml with
        no generation is left alone). This survives WAL truncation — the bug the former
        event-replay reconstruct had (config_changed below the floor was lost).

        #2405: ``is_active_seq``-based ``latest_active`` replaces ``latest_at_or_below(cut=N)``
        — same symmetric gap as topology/vanish/archive: post-rewind active config generations
        (seq > R) were excluded, reverting config to as-of-N on crash recovery."""
        import yaml  # noqa: PLC0415 — local, matching the file convention

        store = self._config_generation_store()
        # #5769 stage 2: `ConfigGenerationStore.latest_active` now takes
        # (state_log, scope) directly and builds its own predicate — no
        # caller-side hoist needed (see that method's own docstring for
        # why a fresh build per rel_path is still cheap). GLOBAL_SCOPE:
        # config generations have no session notion at all (ADR-0047
        # decision 4) — not a placeholder pending a later real scope.
        for rel_path in store.paths():
            latest = (
                store.latest_active(rel_path, self._state_log, scope=GLOBAL_SCOPE)
                if self._state_log is not None
                else store.latest_at_or_below(rel_path, cut)
            )
            abs_path = (self._project_root / ".reyn" / rel_path).resolve()
            if latest is None:
                # First generation after the cut → didn't exist as-of-cut → drop.
                if abs_path.is_file():
                    abs_path.unlink()
            else:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(
                    yaml.dump(
                        latest[1], allow_unicode=True, default_flow_style=False,
                    ),
                    encoding="utf-8",
                )

    async def _drop_session(self, name: str, sid: str, *, purge_dir: bool = True) -> None:
        """#2103 S1bc: tear down a single spawned session created after the rewind cut
        (the primitive's seam, now wired). Delegates to ``remove_session`` — the single
        session-teardown used by BOTH this rewind-drop AND the ephemeral auto-vanish. A
        post-cut session has no pre-cut generations → a clean drop (vs the #1954 archive
        HIDE on the delete side). Reversible: a forward-checkout past the spawn would
        re-materialise it from the config-complete ``session_spawned`` WAL record (the
        session re-materialise seam is a follow-up; sessions are drop-only today).

        ``purge_dir=False`` (the rewind path) defers the destructive on-disk rmtree to
        the caller, so it runs only AFTER the substrate restores succeed (#2125 atomicity
        — a restore-failure must not leave the dir dropped). The session is quiesced here
        (in-flight writes settle before teardown)."""
        await self.remove_session(name, sid, purge_dir=purge_dir, record=False)

    async def remove_session(
        self, name: str, sid: str, *, purge_dir: bool = True, record: bool = True,
    ) -> bool:
        """#2103 S1bc: tear down a SPAWNED (non-main) session — the single teardown
        seam used by BOTH the rewind as-of-cut DROP (``_drop_session``) AND the
        ephemeral auto-vanish. Quiesces the session, cancels the ``(name, sid)`` run-loop +
        forwarder tasks, drops it from the in-memory map, and (``purge_dir``) removes its
        on-disk per-session state dir (``state/sessions/<enc(sid)>/``). Returns True iff
        anything was removed.

        Quiesces (``cancel_inflight`` + ``await_quiescent``, mirroring the global-rewind
        stop-world — idempotent when rewind_to already quiesced; REQUIRED for the
        ephemeral caller, which has no rewind orchestration) so any in-flight write
        settles before teardown.

        Full teardown (rmtree) is correct: the global WAL is the durable source — the
        ``session_spawned`` create-record + the session's session_id-routed entries
        survive (the per-session dir is the snapshot/generations CACHE), so a
        forward-checkout re-materialises from the WAL, not the dir. The MAIN session
        (``_DEFAULT_SID``) is the agent's primary and is NOT removable here (its
        lifecycle is ``registry.remove``). A no-op for an unknown ``(name, sid)``."""
        if sid == _DEFAULT_SID:
            raise ValueError("cannot remove the main session via remove_session")
        removed = False
        # #2125: quiesce the session BEFORE teardown so any in-flight write
        # completes ahead of teardown.
        session = self._peek_session(name, sid)
        if session is not None:
            cancel_inflight = getattr(session, "cancel_inflight", None)
            if callable(cancel_inflight):
                await cancel_inflight()
            quiesce = getattr(session, "await_quiescent", None)
            if callable(quiesce):
                await quiesce()
            # #2597 S2a: close any held MCP connections (Option C — persistent
            # per-server connections opened for this session's lifetime) BEFORE the
            # session drops out of the in-memory map below, so a dropped/rewound
            # session doesn't leak an open subprocess/HTTP connection. A no-op for an
            # ephemeral session (never populates the connection service) or a
            # session built without one (getattr guard).
            aclose_mcp = getattr(session, "aclose_mcp_connections", None)
            if callable(aclose_mcp):
                await aclose_mcp()
            # #2783: close FsWatcher/EventStore SYNCHRONOUSLY here too, mirroring the
            # MCP close above — do not rely on session.run()'s own `finally` (which
            # also closes FsWatcher) to get there before this function returns. That
            # `finally` only runs once the task.cancel() below is actually scheduled
            # and unwound, which this function never awaits (bare `task.cancel()`,
            # no `await task`) — a genuine race, not hypothetical. Both closes are
            # idempotent (FsWatcher.aclose / EventStore.aclose, verified — see their
            # docstrings), so `run()`'s later finally-close is a harmless no-op if it
            # still fires after this.
            aclose_fs_watcher = getattr(session, "aclose_fs_watcher", None)
            if callable(aclose_fs_watcher):
                await aclose_fs_watcher()
            aclose_event_store = getattr(session, "aclose_event_store", None)
            if callable(aclose_event_store):
                await aclose_event_store()
            # #4961 C: same teardown-completeness gap as EventStore above —
            # a 4th instance (see Session.aclose_audit_events's own
            # docstring). This is the site that actually matters for a
            # driver-session spawned to run a detached async pipeline and
            # reclaimed here after its terminal state lands — `run()`'s
            # own `finally` (which now also drains+stops _audit_events)
            # is subject to the EXACT race the comment above already
            # names for FsWatcher/EventStore: this function never awaits
            # the cancelled run() task, so closing here (not relying on
            # that finally to have already fired) is required, not
            # redundant.
            aclose_audit_events = getattr(session, "aclose_audit_events", None)
            if callable(aclose_audit_events):
                await aclose_audit_events()
            # #5364 §1.4: same teardown-completeness gap — a 5th instance
            # (see Session.aclose_media_store's own docstring).
            aclose_media_store = getattr(session, "aclose_media_store", None)
            if callable(aclose_media_store):
                await aclose_media_store()
            # #4215 ②: cancel this session's own hook-bus->parent bridge
            # task, if it is one (bridged children only —
            # session_api._spawn_pipeline_driver_session's attached path;
            # every other session's own attribute stays None and this is a
            # no-op). Bare cancel(), not awaited, mirroring the
            # self._tasks/_forward_tasks cancellation a few lines below —
            # the bridge's own loop has nothing to flush on cancellation
            # (it only forwards live events, nothing durable).
            bridge_task = getattr(session, "_hook_bus_bridge_task", None)
            if bridge_task is not None and not bridge_task.done():
                bridge_task.cancel()
        for task_dict in (self._tasks, self._forward_tasks):
            task = task_dict.pop((name, sid), None)
            if task is not None:
                removed = True
                if not task.done():
                    task.cancel()
        if self._sessions.get(name, {}).pop(sid, None) is not None:
            removed = True
        if purge_dir:
            if self._purge_session_dir(name, sid):
                removed = True
        elif self._session_state_dir(name, sid).is_dir():
            removed = True  # dir present; destructive purge deferred to the caller (#2125)
        # #2154: a GENUINE vanish (ephemeral auto-vanish / explicit removal) emits
        # session_vanished — the create↔destroy WAL symmetry (the destroy-side mirror
        # of session_spawned). The rewind-reconstruction caller (_drop_session) passes
        # record=False: a reconstruction-drop UNDOES history, so recording it would
        # pollute the append-only WAL and corrupt as-of-cut reconstruction.
        if removed and record and self._state_log is not None:
            await self._state_log.append(
                "session_vanished", entity_kind="session", name=name, sid=sid,
            )
        return removed

    def _purge_session_dir(self, name: str, sid: str) -> bool:
        """#2125: the destructive half of session teardown — rmtree the per-session
        state dir. Split out from ``remove_session`` so the rewind path can DEFER it
        until after the substrate restores succeed (atomicity). Best-effort; LOGs an
        ``OSError`` rather than swallowing. Returns True iff a dir was removed."""
        state_dir = self._session_state_dir(name, sid)  # sid != main → sessions/<enc>/
        removed = False
        if state_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(state_dir)
                removed = True
            except OSError as e:  # noqa: BLE001 — best-effort; LOG (don't silently swallow)
                logger.warning(
                    "#2103/#2125: teardown of session %r/%r left state on disk: %s",
                    name, sid, e,
                )
        # #5364 §1.6 "Q": a vanished session's own spilled tool-result
        # content is otherwise ORPHANED — nothing else ever purges
        # history-content/<agent>/<sid>/ (the GC cap (§1.6 "C") bounds one
        # session's OWN content, it does not know a session vanished).
        # Same (name, sid) key-space as state_dir above — safe to purge
        # together only because #5364's key-space fix made this dir's own
        # keying agent-scoped, matching _session_state_dir's shape exactly
        # (before that fix, this path was shared across every agent's
        # same-named session — purging it here would have been the exact
        # cross-agent data-loss #5364's key-space fix closed).
        from reyn.data.workspace.media_store import history_content_dir_for
        content_dir = history_content_dir_for(self._project_root, name, sid)
        if content_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(content_dir)
                removed = True
            except OSError as e:  # noqa: BLE001 — best-effort; LOG (don't silently swallow)
                logger.warning(
                    "#5364 §1.6: teardown of session %r/%r left spilled "
                    "tool-result content on disk: %s",
                    name, sid, e,
                )
        return removed

    async def _materialize_rewind(
        self, *, reconstruct_seq: int, workspace_at_or_below: int,
        scope: "tuple[str, str] | None" = None,
    ) -> list[str]:
        """Bring the runtime substrate to the active branch as-of ``reconstruct_seq``.

        Idempotent — shared by ``rewind_to``/``checkout`` (right after the
        reset-record) and crash ``recover_rewind_if_needed`` (at restart).
        Per agent: ``reconstruct`` as-of the active branch + persist a
        self-contained snapshot pinned to ``reconstruct_seq`` (so
        ``restore_all`` replays only beyond it); loaded sessions are reset +
        re-adopt it.

        ``reconstruct_seq`` is the WAL head at call time (= R in rewind_to, =
        current head in recovery); ``workspace_at_or_below`` is the as-of-cut DROP
        boundary = ``target_n`` in rewind_to or head in recovery. Returns the agents
        materialised.

        #5769 stage 3 (ADR-0047 decision 3/4/5): ``scope=None`` (default) is
        the full GLOBAL pass below, byte-identical to before this parameter
        existed. ``scope=(name, sid)`` brings ONLY that session's own
        substrate to as-of-cut — reconstruct + restore its conversation and
        per-session agent state — and returns immediately, touching NOTHING
        else: no agent create/drop/archive/purge, no topology reconcile, no
        config-generation reconcile, no spawn-lineage/identity rebuild. All
        of those are global, agent-wide facts (decision 6) a session-scoped
        rewind must never move; the unscoped path below already handles them
        exhaustively for `scope=None`, which is the ONLY case that needs to.
        """
        if scope is not None:
            name, sid = scope
            created_at = self._created_at_map()
            sess_vanished = self._session_vanished_map()
            session_is_active = (
                build_active_predicate(self._state_log, scope=scope)
                if self._state_log is not None
                else None
            )
            sess_seq = created_at.get(("session", name, sid))
            van_seq = sess_vanished.get((name, sid))
            spawned_after_cut = (
                sess_seq is not None
                and session_is_active is not None
                and not session_is_active(sess_seq)
            )
            vanished_by_cut = (
                van_seq is not None
                and session_is_active is not None
                and session_is_active(van_seq)
            )
            if spawned_after_cut or vanished_by_cut:
                # Mirrors the unscoped path's own drop shape (#2125): quiesce
                # via _drop_session, then purge the dir — no restore succeeds
                # here to defer the purge past, so it is safe to do inline.
                await self._drop_session(name, sid, purge_dir=False)
                self._purge_session_dir(name, sid)
                return []
            store = self._store_for(name, sid)
            snap = reconstruct(
                name, store, self._state_log,
                target_seq=reconstruct_seq, session_id=sid, scope=scope,
            )
            snap.applied_seq = reconstruct_seq
            snap.save(self._session_snapshot_path(name, sid))
            session = self._peek_session(name, sid)
            if session is not None:
                await session.reset_for_rewind()
                session.restore_state(snap)
            return [name if sid == _DEFAULT_SID else f"{name}/{sid}"]

        # FP-0043 Stage 5: the runtime snapshot is reconstructed PER SESSION (each
        # (name, sid) from its own generations + session_id-routed WAL delta), so a
        # global cut moves every session of every agent to the target — consistent
        # with the D2 whole-world invariant. Session discovery is from disk (this is
        # shared with crash-recovery, where sessions are not loaded).
        agents: list[str] = []
        # #2125 (b)-split atomicity: collect the post-cut sessions whose destructive
        # on-disk rmtree is DEFERRED until AFTER the substrate restores succeed (a
        # restore-failure must not leave the dirs dropped). The quiesce still happens
        # inline at drop time below.
        deferred_session_purges: list[tuple[str, str]] = []
        created_at = self._created_at_map()   # #2103: as-of-cut DROP input (empty → no-op)
        sess_vanished = self._session_vanished_map()  # #2154: as-of-cut session destroy-cut
        # #2103 S2: agent-lifecycle reconstruction (re-materialise / hide / purge) —
        # all inert until S2b emits the events.
        ag_created, ag_archived, ag_purged = self._agent_lifecycle()
        # #2103: the existence predicate is ``is_active_seq``: an entity whose
        # create-seq is on an ABANDONED branch didn't exist as-of-target → drop it.
        # ``is_active_seq`` correctly handles BOTH call sites:
        #   • ``rewind_to`` (live, no post-R agents) — agents in (N, R) are inactive.
        #   • ``recover_rewind_if_needed`` (crash recovery) — same inactive interval;
        #     post-rewind active agents at C' > R are IS active, so NOT dropped.
        # Using the single-cut ``agent_seq > drop_cut`` would drop post-rewind active
        # agents (C' > R > N = drop_cut) on a crash after a completed rewind.
        # ``vanished_by_cut`` and ``_reconcile_archived_as_of_cut`` use the SAME predicate:
        #   V ≤ N OR V > R (both ``is_active_seq=True``) → drop / write marker
        #   N < V < R (``is_active_seq=False``) → keep / clear marker
        # Both production callers guarantee a rewind record exists before calling here
        # (rewind_to appends it at line 1091 before line 1095; recover_rewind_if_needed
        # gates on active_rewind_target is not None at line 2011 before line 2014).
        drop_cut = workspace_at_or_below
        # Hoisted once for the whole materialise pass — the same fix-class as
        # restore_all/#2941: the below loops call the seq-independent predicate once
        # per (created-agent, agent, session) triple; without hoisting, each call
        # re-scans the entire WAL (`is_active_seq` → `_rewind_records` →
        # `iter_from(1)`), turning this cold-start/rewind path quadratic in WAL size.
        # #5769 stage 2: this one stays GLOBAL_SCOPE — an agent's own
        # existence (create/drop) is not owned by any ONE of its sessions;
        # every session lives or dies with the agent, so a single
        # session-scoped rewind must never selectively resurrect or
        # destroy the agent itself. Only the PER-SESSION checks below (the
        # session loop) get a real per-(name, sid) scope.
        agent_is_active = (
            build_active_predicate(self._state_log, scope=GLOBAL_SCOPE)
            if self._state_log is not None
            else None
        )
        # #2103 S2 re-materialise: an agent on the ACTIVE branch, NOT purged, currently
        # ABSENT (dropped at a prior cut) → re-create from its agent_created record
        # so a forward-checkout-past-drop brings it back (the inverse of the drop).
        for _rname, (_rcseq, _rpayload, _rparent, _rpseq) in ag_created.items():
            if _rname in ag_purged:
                continue  # fork A: purged = permanent, never re-materialised
            _is_active = agent_is_active is None or agent_is_active(_rcseq)
            if _is_active and not (self._dir / _rname).is_dir():
                self._rematerialise_agent(_rname, _rpayload)
        for name in self.list_names():
            # An agent on an ABANDONED branch — OR purged (fork A: permanent) — is
            # torn down (subsumes its nested sessions) instead of reconstructed.
            # Reversible (create case): a forward-checkout past the create
            # re-materialises it from the agent_created WAL record (the pass above).
            agent_seq = created_at.get(("agent", name, ""))
            _on_abandoned = (
                agent_seq is not None
                and agent_is_active is not None
                and not agent_is_active(agent_seq)
            )
            if name in ag_purged or _on_abandoned:
                self._drop_agent(name)
                continue
            for sid in self._discover_session_ids(name):
                # #5769 stage 2: a session's own spawn/vanish seq IS owned
                # by exactly this (name, sid) — its real, nameable scope,
                # built per session (cheap: build_active_predicate's own
                # record fetch is incremental/cached, #2939 — not a WAL
                # re-scan per session).
                session_is_active = (
                    build_active_predicate(self._state_log, scope=(name, sid))
                    if self._state_log is not None
                    else None
                )
                # A session on an abandoned branch → drop just that session.
                # #2154: OR a session that VANISHED at-or-before the cut (it was gone
                # as-of-cut) — the destroy-side mirror of the spawn-cut. A genuine
                # vanish normally already rmtree'd the dir (so discovery won't surface
                # it); this guard keeps reconstruction correct if a dir SURVIVES its
                # vanish (a crash mid-rmtree, or a future session re-materialise seam).
                sess_seq = created_at.get(("session", name, sid))
                van_seq = sess_vanished.get((name, sid))
                spawned_after_cut = (
                    sess_seq is not None
                    and session_is_active is not None
                    and not session_is_active(sess_seq)
                )
                vanished_by_cut = (
                    van_seq is not None
                    and session_is_active is not None
                    and session_is_active(van_seq)
                )
                if spawned_after_cut or vanished_by_cut:
                    # #2125: detach now (quiesce in-flight writes); defer the
                    # destructive rmtree until the restores succeed.
                    await self._drop_session(name, sid, purge_dir=False)
                    deferred_session_purges.append((name, sid))
                    continue
                store = self._store_for(name, sid)
                snap = reconstruct(
                    name, store, self._state_log,
                    target_seq=reconstruct_seq, session_id=sid, scope=(name, sid),
                )
                # Self-contained: the reset-record carries no agent target, so
                # reconstruct leaves applied_seq at the last active entry. Pin it to
                # the head so restore_all's replay floor skips the abandoned segment.
                snap.applied_seq = reconstruct_seq
                snap.save(self._session_snapshot_path(name, sid))
                session = self._peek_session(name, sid)
                if session is not None:
                    await session.reset_for_rewind()
                    session.restore_state(snap)
                # main → bare name (back-compat with single-session callers);
                # spawned → "name/sid".
                agents.append(name if sid == _DEFAULT_SID else f"{name}/{sid}")
        # #2103 S2: rewrite present agents' .archived tombstones to the as-of-cut
        # archived-state (rewind-before-archive → active; rewind-after → archived).
        self._reconcile_archived_as_of_cut(ag_archived)
        self._reconcile_topologies_as_of_cut(drop_cut)
        # #2259 PR-1: rebuild the recovery-core config registries (mcp/cron/hooks/…)
        # as-of-cut from the config GENERATIONS — same latest-≤-cut-wins model as topology.
        self._reconcile_config_as_of_cut(drop_cut)
        # #2103 B (the rewind LINCHPIN): rebuild the spawn lineage as-of-cut from the
        # agent_created records — a re-materialised child REGAINS its ⊆-parent cap and a
        # dropped/post-cut child's edge is gone. A FULL rebuild (not an incremental
        # patch) so the lineage deterministically matches the as-of-cut present-agent
        # set with no stale/missing edge: escalation-on-rewind is precisely a MISSING
        # edge for a present child (resolved_profile_for would then skip the
        # parent-conjunct → un-capped). Assigned directly (the WAL is the trusted source;
        # the forge/cycle guards already ran at spawn time).
        #
        # #2103 C2b: rebuild the identity map (name → create_seq) for present-as-of-cut
        # agents FIRST, so the staleness check has the current identity to compare each
        # edge's FROZEN parent identity against. The edge keeps the parent identity
        # AT-SPAWN (the recorded ``parent_seq``, ``_pseq``); if the parent name was later
        # purged + REUSED, the reused parent's create_seq differs → the edge reads STALE
        # → resolved_profile_for fail-closes + is_spawn_descendant rejects = no
        # resurrection of the orphan under the reused parent on a forward checkout.
        #
        # #2259 PR-1b: identity/lineage comes from the truncation-surviving per-agent
        # GENERATIONS (latest ≤ cut), with the `agent_created` WAL scan as a fallback for
        # any agent without a generation. The WAL event is truncated below the floor — so a
        # long-lived agent's edge would be LOST if rebuilt from the WAL alone, dropping its
        # ⊆-parent cap on rewind (escalation-on-rewind). The generation is the recovery base.
        identity = self._agent_identity_as_of_cut(drop_cut)
        for _n, (_s, _payload, _p, _pseq) in ag_created.items():
            if _s <= drop_cut:
                identity.setdefault(_n, (_s, _p, _pseq))
        self._agent_create_seq = {
            _n: _cs for _n, (_cs, _p, _pseq) in identity.items()
            if _n not in ag_purged and (self._dir / _n).is_dir()
        }
        self._spawn_lineage = {
            _n: (_p, _pseq) for _n, (_cs, _p, _pseq) in identity.items()
            if _p and _n not in ag_purged and (self._dir / _n).is_dir()
        }
        # #2125 (b)-split: the runtime reconstruction succeeded — NOW perform the
        # deferred destructive rmtree of the dropped post-cut session dirs. Reaching
        # here means no reconstruction raised (a failure propagates before this), so the
        # drop is committed only alongside a successful reconstruction (no half-applied
        # "dirs dropped despite checkout failed" state).
        for _purge_name, _purge_sid in deferred_session_purges:
            self._purge_session_dir(_purge_name, _purge_sid)
        return agents

    async def recover_rewind_if_needed(self) -> dict | None:
        """Re-materialise both substrates as-of-N after a crash mid-rewind (1d).

        The reset-record is fsync'd before any reconstruction (1b keystone), so
        on restart an active reset-record means "a rewind was decided"; recovery
        re-runs the idempotent materialisation BEFORE ``restore_all`` loads
        sessions, closing the window where the crash hit after the reset-record
        but before snapshots / workspace were brought to as-of-N. No-op when no
        rewind record exists. Returns a summary or ``None``.

        #5769 stage 3 (ADR-0047 decision 3's recovery half): reads
        ``(target_n, scope)`` off the LATEST reset-record, not just its
        target — a crash mid-SCOPED-rewind must re-materialise only that
        ``(name, sid)``, never the whole world. A legacy/global record
        (``scope is None``) takes the unchanged full-materialise path below,
        byte-identical to before this parameter existed.
        """
        if self._state_log is None:
            return None
        result = active_rewind_target_with_scope(self._state_log)
        if result is None:
            return None
        target, scope = result
        head = self._state_log.last_durable_seq
        agents = await self._materialize_rewind(
            reconstruct_seq=head, workspace_at_or_below=target, scope=scope,
        )
        return {
            "recovered_target_n": target,
            "head": head,
            "agents": agents,
            "scope": list(scope) if scope is not None else None,
        }

    # ── WAL truncation (WAL-floor design) ───────────────────────────────────
    #
    # Trigger policy: semantic boundary — call this at a turn/append boundary.
    # Throttled to avoid thrashing on bursty boundaries. Size-based safety net
    # (long-idle sessions) backs it up until we have real WAL size telemetry
    # from dogfood.
    #
    # Floor calculation: `min(全 agent applied_seq) + 1` — everything strictly
    # below this seq is universally absorbed and droppable. Replaying from
    # `floor - 1` would be a no-op for every snapshot, so dropping below it is
    # safe.
    #
    # Owner rationale: AgentRegistry is the only layer that has both
    # (a) the StateLog handle, and (b) visibility into all agents' snapshots.
    # Pushing this into entry points (`reyn chat`, `reyn web`)
    # would duplicate the orchestration; pushing it into StateLog itself
    # would force the WAL to know about agent layout.

    _TRUNCATION_THROTTLE_SECS: float = 5.0
    # R-D4: size safety net default. Session's chat-turn-boundary
    # call uses this threshold. Long-idle sessions with no semantic
    # boundary events would otherwise let the WAL grow unboundedly
    # between turns.
    _SIZE_SAFETY_NET_BYTES: int = 1_000_000

    async def truncate_wal_if_eligible(
        self, *, bypass_throttle: bool = False,
    ) -> dict | None:
        """Compute floor across all agents' snapshots, then
        truncate the WAL if eligible.

        Returns the truncate stats dict, or ``None`` if skipped (no state
        log, throttled, or floor not advanced).

        Skip conditions:
          - no StateLog wired (test / non-chat)
          - last truncation was within ``_TRUNCATION_THROTTLE_SECS`` —
            unless ``bypass_throttle=True`` (R-D4: size safety net)
          - computed floor is 0 (no snapshots, or any snapshot read failed
            — conservative: don't truncate when we can't trust the floor)

        ``bypass_throttle`` is for size-driven calls
        (``maybe_truncate_for_size``): if the WAL is bloated, the
        throttle's burst-protection rationale doesn't apply — we
        should truncate now even if a semantic-boundary truncate just
        happened.

        On computation or rewrite failure, the exception is caught and
        logged; the next trigger naturally retries. We never let truncation
        bubble up to disturb the caller's hot path (phase advance / run
        completion).
        """
        if self._state_log is None:
            return None
        now = time.monotonic()
        if (not bypass_throttle
                and self._last_truncation_ts is not None
                and now - self._last_truncation_ts < self._TRUNCATION_THROTTLE_SECS):
            return None
        try:
            floor = self.compute_truncate_floor()
        except Exception as e:  # noqa: BLE001 — defensive; never fail caller
            logger.warning("WAL truncation: floor computation failed: %s", e)
            return None
        if floor <= 0:
            return None
        try:
            # #2259 PR-2b: fire-and-forget (the GC does not await the worker); the rewrite +
            # any failure are handled in the worker (stats on last_truncate_stats, post-drain).
            # always_keep_kinds="rewind": reset-records must outlive the floor so
            # _active_branch_history can call is_active_seq on history.jsonl wal_seq anchors
            # that fall below the floor (abandoned conversation turns — the append-only file
            # is never truncated, so the wal_seq references must remain resolvable).
            await self._state_log.truncate_below(
                floor, always_keep_kinds=frozenset({REWIND_KIND}),
            )
        except Exception as e:  # noqa: BLE001 — defensive; never fail caller
            logger.warning("WAL truncation: rewrite failed (floor=%d): %s", floor, e)
            return None
        # Stamp success so throttle gates the next attempt. (We don't gate
        # on dropped==0 — even a no-op rewrite resets the throttle window.)
        self._last_truncation_ts = now
        # ADR-0038 Stage 1e (D5): GC generations on the SAME boundary (Q3 piggyback).
        # prune_below(floor) drops only what is below the (retention-clamped) WAL
        # floor — generations >= floor stay reconstructable, so this never drops
        # rewind history within the retention window.
        await self._prune_generations_below(floor)
        # #2259 PR-2b: truncate is fire-and-forget, so this returns the last-recorded stats
        # (a non-None dict = "truncation triggered"; the actual rewrite drains in the worker).
        # The caller only uses not-None as the trigger signal (we don't gate on dropped==0).
        return self._state_log.last_truncate_stats

    async def _prune_generations_below(self, floor: int) -> None:
        """Drop snapshot generations below ``floor`` (Stage 1e GC).

        ``floor`` is the truncation floor (already retention-clamped), so a
        generation at-or-above it stays reconstructable. Defensive — never raises
        into the truncation hot path.
        """
        try:
            for name in self.list_names():
                # FP-0043 S5: prune EVERY session's runtime-snapshot generations (main +
                # spawned) — these stay PER-SESSION (the runtime-snapshot substrate).
                for sid in self._discover_session_ids(name):
                    self._store_for(name, sid).prune_below(floor)  # SnapshotGenerationStore (sync)
            # #1547: anchors GC'd on the same boundary as generations.
            anchors = self.anchor_store
            if anchors is not None:
                anchors.prune_below(floor)                  # AnchorStore (sync)
            # #2259 PR-1: GC config generations on the SAME boundary — but the store keeps,
            # per registry, the nearest gen BELOW the floor (the truncation-surviving base),
            # so config-as-of-floor stays reconstructable (the bug the event-replay had).
            self._config_generation_store().prune_below(floor)
            # #2259 PR-1b: GC agent-identity generations on the same boundary, with the SAME
            # prune-KEEPS-BASE crux — keep the nearest identity gen BELOW the floor per agent,
            # so the ⊆-parent cap stays reconstructable on a rewind to the floor.
            self._agent_identity_generation_store().prune_below(floor)
        except Exception as e:  # noqa: BLE001 — defensive; never fail caller
            logger.warning("Stage 1e generation GC failed (floor=%d): %s", floor, e)
        # #5759 stage 2: history.jsonl GC on the SAME boundary/pass — no new
        # trigger, piggybacks on this already-throttled truncation cycle
        # (lead-coder ruling). Own try/except inside (best-effort per
        # session), so a failure here never blocks the archived-agent purge
        # below, matching the existing generation-GC try's own isolation.
        await self._gc_history_jsonl_below(floor)
        # #1954 slice 2: WAL-window-bounded auto-purge of archived agents — run
        # OUTSIDE the generation-GC try so a hiccup above never
        await self._purge_archived_below(floor)

    async def _purge_archived_below(self, floor: int) -> None:
        """#1954 slice 2: hard-delete archived agents whose archival seq fell
        below the retention ``floor`` — the soft-delete left the WAL window, so
        rewind-to-before-delete is no longer possible → hard-delete is safe
        (§24-faithful). Best-effort; never raises into the truncation path."""
        import shutil
        for name in self.list_names():
            try:
                seq = self._archived_seq(name)
                if seq is None or seq >= floor:
                    continue
                # #2159: same cascade-emit gap as the explicit remove(purge=True)
                # path — this rmtree ALSO subsumes the agent's nested spawned
                # sessions with no per-session destroy record. Enumerate from disk
                # before the rmtree (main excluded — it's the agent's own primary
                # session, covered by no vanish record of its own here either way).
                vanished_sids = [
                    sid for sid in self._discover_session_ids(name)
                    if sid != _DEFAULT_SID
                ]
                shutil.rmtree(self._dir / name, ignore_errors=True)
                # #2159: emit the destroy-side session_vanished mirror for each
                # subsumed session through the logged seam (async GC path already
                # awaits emits below for the topology cascade — same shape).
                for sid in vanished_sids:
                    await self._state_log.append(
                        "session_vanished", entity_kind="session", name=name, sid=sid,
                    )
                # Now a permanent hard-delete → drop the (previously preserved)
                # topology membership so no dangling reference is left behind.
                # #2103 MUST-1: emit the cascade's topology changes through the
                # logged seam (async GC path → await the emits).
                for tname, topo in self._cascade_agent_removal(name):
                    await self._emit_topology(
                        "topology_removed" if topo is None else "topology_updated",
                        tname, topo,
                    )
            except Exception as e:  # noqa: BLE001 — best-effort; never fail caller
                logger.warning("#1954 archived auto-purge failed for %r: %s", name, e)

    async def maybe_truncate_for_size(
        self, *, threshold_bytes: int | None = None,
    ) -> dict | None:
        """Size-driven WAL truncation safety net (R-D4).

        Called from places that don't naturally fire phase-completion
        events but still want to bound WAL growth — primarily the
        Session chat-turn boundary (each user message handled).

        Behavior:
          - If WAL file size <= threshold (default 1 MB): no-op, no
            throttle reset, no rewrite.
          - If WAL file size > threshold: call
            ``truncate_wal_if_eligible(bypass_throttle=True)``. The
            throttle is bypassed because a bloated WAL means waiting
            another 5 seconds doesn't help; the rewrite needs to
            happen now to reclaim disk + replay time.

        Returns the truncate stats dict on a successful rewrite, or
        ``None`` if skipped (state log absent, WAL small, floor not
        advanced, etc.).
        """
        if self._state_log is None:
            return None
        # ADR-0038 Stage 1c-2: no compaction during a global rewind — it would
        # risk advancing the keep-floor over the reset-record / reconstruct WAL
        # reads. Compaction resumes (against the new active state) once the
        # rewind clears the flag.
        if self._rewind_in_progress:
            return None
        threshold = (
            threshold_bytes if threshold_bytes is not None
            else self._SIZE_SAFETY_NET_BYTES
        )
        try:
            size = self._state_log.path.stat().st_size
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("WAL size check failed: %s", e)
            return None
        if size <= threshold:
            return None
        return await self.truncate_wal_if_eligible(bypass_throttle=True)

    # ── R-D14: cross-agent chain discard notification ──────────────────────

    async def notify_chain_discarded(
        self,
        *,
        chain_id: str,
        by_agent_name: str,
        reason: str = "peer_discarded",
    ) -> bool:
        """Find the upstream waiter agent and force-resolve their chain.

        When a run is discarded on agent B, and that
        run was processing a chain registered on agent A's side, A would
        otherwise stay stuck on ``waiting_on={B}`` until the watchdog
        fires (chain_timeout_seconds, often minutes-to-hours in real
        use). This method bridges the gap by scanning every other agent's
        ChainManager for ``chain_id`` and invoking the matching
        session's ``_on_chain_peer_discarded`` handler so the chain
        resolves immediately.

        Parameters:
          chain_id: the chain that was being processed by the discarded run
          by_agent_name: name of the agent doing the discard (= B in
            the example); excluded from the scan to prevent self-notify
          reason: short tag stored on the chain_resolve audit event

        Returns True if a waiter was found and notified, False otherwise
        (no other agent tracks this chain).

        Defensive: a session whose ``_chains`` attribute is missing or
        whose handler raises is logged and skipped — never blocks the
        discard path.
        """
        notified = False
        for name, session in self._iter_named_sessions():
            if name == by_agent_name:
                continue
            chain_mgr = getattr(session, "_chains", None)
            if chain_mgr is None:
                continue
            try:
                pending = chain_mgr.find_chain(chain_id)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning(
                    "notify_chain_discarded: find_chain raised on agent %s: %s",
                    name, e,
                )
                continue
            if pending is None:
                continue
            handler = getattr(session, "_on_chain_peer_discarded", None)
            if handler is None:
                continue
            try:
                await handler(
                    chain_id=chain_id, peer=by_agent_name, reason=reason,
                )
                notified = True
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning(
                    "notify_chain_discarded: handler raised on agent %s: %s",
                    name, e,
                )
        return notified

    # R-D16: a run awaiting an intervention longer than this many seconds
    # is excluded from the WAL truncation floor calc. Without this, a
    # single run stuck on ``ask_user`` (e.g. user away from terminal)
    # could pin the floor indefinitely and let the WAL grow unbounded. A
    # long-await run accepts memo loss for the awaited window — at resume
    # it falls through to re-execute the op whose memo was truncated,
    # which is the same behaviour as a memo cache miss.
    _LONG_AWAIT_THRESHOLD_SEC: float = 300.0

    def compute_truncate_floor(self) -> int:
        """Lowest seq that must remain in the WAL, clamped by the retention policy.

        ``= min(live_floor, retention_floor)``. **Live policy → ``live_floor``
        unchanged** (the in-memory fast path below; no disk reads — preserves
        PR-N7). Only the **opt-in deeper** policy reads generation seqs (bounded
        disk) to clamp the floor down so the retention window stays
        reconstructable (ADR-0038 Stage 1e, D5).
        """
        live_floor = self._compute_live_floor()
        if self._retention_policy.is_live or live_floor <= 0:
            return live_floor
        return compute_retention_floor(
            self._retention_policy,
            live_floor=live_floor,
            checkpoint_seqs=self._checkpoint_seqs(),
        )

    def _checkpoint_seqs(self) -> list[int]:
        """Global checkpoint (generation) seqs — union across agents' gen stores.

        Disk-backed (gen-dir glob); called ONLY on the non-live retention path so
        the default floor computation stays in-memory (PR-N7).
        """
        seqs: set[int] = set()
        for name in self.list_names():
            seqs.update(self._store_for(name).seqs())
        return sorted(seqs)

    def _compute_live_floor(self) -> int:
        """Return the lowest seq that MUST remain in the WAL (live floor).

        ``floor = min(全 active session applied_seq) + 1``

        PR-N7 (FP-0008): reads watermarks exclusively from in-memory
        state — session journal snapshots — by walking
        ``self._agents.values()`` and calling
        each session's ``iter_applied_seqs`` public method. The pre-N7
        implementation walked every snapshot file on disk inside the
        async ``truncate_wal_if_eligible`` caller, which blocked the
        event loop for O(N agents × disk read) and was the root cause
        of the 13-hour hang observed in PR-N5 13236 single-instance
        pilot. The in-memory path matches the existing reyn architecture
        choice (event loop friendly, event-sourced state from WAL apply,
        no thread offload).

        Dormant agents (no live Session registered in
        ``self._agents``) are excluded from the floor calculation — the
        same skip the pre-N7 disk-read path applied for
        ``applied_seq == 0`` snapshots. The invariant that justifies
        this:

          A dormant agent has no live ``Session``. WAL events are
          only appended through a session's ``SnapshotJournal``, which
          targets the session's own agent. Therefore no WAL event can
          target an agent whose session has never been instantiated
          this run, and dropping events older than the dormant agent's
          (zero) applied_seq cannot orphan messages.

          When the dormant agent later receives its first event,
          ``ensure`` instantiates a session that immediately registers
          here, and the next floor recompute picks up its watermark.

        R-D16: a run awaiting an intervention for longer than
        ``_LONG_AWAIT_THRESHOLD_SEC`` is excluded so the WAL can keep
        advancing — the elapsed check is performed in-memory.

        Returns 0 when no watermark is available (no live session).
        """
        seqs: list[int] = []
        now = time.monotonic()
        for session in self._iter_sessions():
            iter_method = getattr(session, "iter_applied_seqs", None)
            if iter_method is None:
                # Conservative: a session shim without the method (test
                # fixtures, future variants) is treated as a non-pinner
                # — never block truncation on a stale shim.
                continue
            seqs.extend(
                iter_method(
                    now_ts=now,
                    long_await_threshold=self._LONG_AWAIT_THRESHOLD_SEC,
                )
            )
        if not seqs:
            return 0
        # Drop entries strictly below the lowest absorbed seq. The +1 makes
        # the boundary exclusive: the seq itself remains as a watermark
        # (StateLog.truncate_below additionally guards the highest seq).
        return min(seqs) + 1

    # ── lifecycle ────────────────────────────────────────────────────────────

    def get_or_load(self, name: str, *, is_delegate: bool = False) -> "object":
        """Return the Session for `name`, instantiating from profile if new.

        ``is_delegate`` (#2081): True when this load is a DELEGATION target (the
        A2A request path). It is recorded on FIRST construction (a cache hit
        returns the existing session unchanged) and drives the unbound-delegate
        default-deny in ``resolved_profile_for``. Default False = a top-level /
        non-delegation load (byte-identical to pre-#2081).

        #5217: ``_store_session`` (PUBLISH) runs LAST, after toggle-restore and
        state-restore (COMPLETE), not before. Before this fix, a session was
        publicly visible in the registry's own map for the 3 lines between
        ``_store_session`` and ``restore_state`` — any OTHER thread reaching
        this registry in that window (``get_session``/``attached_session``)
        could observe a half-built ``Session`` (toggles/pending-state not yet
        applied). #5203 measured that this window happened to be harmless
        TODAY only because the one field its own off-thread readers consumed
        (``Session.history``) is populated earlier, inside ``_construct_
        session``'s own factory call — an accidental safety, not a designed
        one (architect issuecomment-5385481839). Publishing only once
        construction is COMPLETE makes it structural instead: any thread that
        finds a session in the map at all now finds a fully-built one, or
        finds nothing — never a partial one. See ``AgentRegistry``'s own
        ``_owner_thread_ident`` comment for the read-guard history this
        replaces (#5215 tried enforcing the OLD window with a guard on the
        READERS; withdrawn once found to be topology-dependent — this fix is
        the one architect named as the real one, closing the WRITER's own
        window instead).

        Reorder safety: a GREP for a direct registry reach (registry/
        get_session/attached_session/_peek/self._reg) inside ``load_
        persisted_toggles``/``restore_state`` finds none — but this PR's
        own TESTS-READ B (tui-coder) found a real INDIRECT one a grep
        cannot see: ``load_persisted_toggles`` → ``CapabilityVisibility.
        reapply_visibility_override`` → ``self.resolved_profile_for``
        (THIS class's own method, below). Independently confirmed
        harmless for the reorder: ``resolved_profile_for`` reads topology
        bindings and capability-profile YAML files only — it never
        touches ``self._sessions``/the session map, ``get_session``,
        ``attached_session``, ``_peek_session``, or ``_pending_restore``
        (same grep, same zero hits, re-run over its own body). Left here
        because grep alone missed it once already — the next person
        reordering something similar should trace the actual call graph
        one level deeper than the direct-hit search, not just repeat it."""
        existing = self._peek_session(name)
        if existing is not None:
            return existing
        if not self.exists(name):
            raise FileNotFoundError(
                f"agent {name!r} not found; run `reyn agent new {name}` to create it"
            )
        profile = self.load_profile(name)
        session = self._construct_session(profile, is_delegate=is_delegate)
        # #2285 step2: restore persisted visibility/hook toggles for the MAIN session (spawned
        # sessions load in spawn_session after the per-session path re-key). First-construction only
        # — cache-hits return above — so this runs once per session lifetime, incl. on restart.
        loader = getattr(session, "load_persisted_toggles", None)
        if callable(loader):
            loader()
        # #3671 P4 item C-1: apply a deferred restore_all(only_names=...)
        # snapshot on FIRST construction — this is the one place both
        # `attach()` and `ensure_running()` (delegation targets) converge, so
        # a single hook here covers both "first real use" triggers for the
        # default session. Pop (not peek): applies exactly once per Session
        # lifetime, same as the toggle-load above.
        pending = self._pending_restore.pop((name, _DEFAULT_SID), None)
        if pending is not None:
            session.restore_state(pending)
        # #5217: PUBLISH last — see this method's own docstring for why.
        self._store_session(name, session)
        return session

    def _construct_session(
        self,
        profile: AgentProfile,
        *,
        is_delegate: bool = False,
        presentation_consumer: "object | None" = None,
        intervention_bridge: "object | None" = None,
    ) -> "object":
        """Build a configured Session from a profile (factory + shared-store
        attach), WITHOUT inserting it into the session map. Shared by get_or_load
        (default session) and spawn_session (additional sessions) — FP-0043 S3.

        #2081: ``is_delegate`` is published on the transient
        ``_constructing_as_delegate`` for the duration of the (synchronous) factory
        call, so the factory's ``resolved_profile_for(profile.name)`` sees it
        without a factory-signature change. Save/restore (not set-False) so it is
        correct under nesting too — non-re-entrant today, but free future-proofing.

        #2708 P3.1/P3.2a: ``presentation_consumer`` (present-sink) and
        ``intervention_bridge`` (ask_user/permission routing) are the spawn-time
        capability OVERRIDES — the reusable capability-bundle inheritance seam. ``None``
        for both (every non-spawn / default-spawn caller) keeps the factory call
        byte-identical (``self._factory(profile)``); only a spawn site that opts in (the
        attached pipeline driver, ``session_api._spawn_pipeline_driver_session``) forwards
        an override, so the widened-factory contract only binds spawn sites that actually
        pass one — bare test factories stay callable."""
        _prev_delegate = self._constructing_as_delegate
        self._constructing_as_delegate = is_delegate
        try:
            factory_kwargs: dict = {}
            if presentation_consumer is not None:
                factory_kwargs["presentation_consumer"] = presentation_consumer
            if intervention_bridge is not None:
                factory_kwargs["intervention_bridge"] = intervention_bridge
            session = self._factory(profile, **factory_kwargs)
        finally:
            self._constructing_as_delegate = _prev_delegate
        # #1547: hand the session the shared anchor store so cut_generation
        # records the rewind-timeline preview text at each boundary.
        anchors = self.anchor_store
        attach_anchor = getattr(session, "attach_anchor_store", None)
        if anchors is not None and callable(attach_anchor):
            attach_anchor(anchors)
        return session

    def _persist_session_narrowing(
        self, name: str, sid: str, narrowing: dict, *,
        base_dir: "Path | None" = None,
        sandbox: "dict | None" = None,
    ) -> None:
        """Write ``narrowing`` (+ optionally #4200's ``base_dir`` override, + #5352's
        ``sandbox`` override) to ``(name, sid)``'s own ``config.yaml`` — the
        #2103-S1a per-session capability layer, workspace-backed (P5), now ALSO the
        #4200 per-session ``base_dir`` override layer AND the #5352 per-session
        ``sandbox`` override layer (both SIBLING keys in the SAME file —
        ``Session._workspace_base_dir`` reads ``base_dir`` directly;
        ``AgentRegistry.resolved_sandbox_for`` reads ``sandbox`` directly — neither
        goes through this narrowing-specific code path).

        The single WRITER of that file, so the spawn entry points cannot drift from
        each other or from its readers (``_load_per_session_capability_profile`` /
        ``per_session_narrowing`` / ``Session._read_base_dir_override`` /
        ``resolved_sandbox_for``, all keyed through ``_session_state_dir``). The
        synthetic ``name`` key is what ``per_session_narrowing`` strips back off, so
        the parent→child round-trip is exact.

        Writing the file is not by itself enforcement (for the narrowing half, and
        for the sandbox half — the ``base_dir`` half is read live on every
        ``Session._workspace_base_dir`` access, so no separate injection step applies
        to it): the live session's ``_contextual_permission`` (resp. its sandbox
        override) was resolved/injected by the factory with ``sid=None`` and has
        never seen this file. Each of the two callers re-resolves WITH the sid and
        injects — ``spawn_session`` immediately after calling this, and
        ``spawn_session_recorded`` after its own ``refresh_config_projections()`` (the
        ordering there is measured, not stylistic; see the note at its ``spawn_session``
        call). A caller that writes without injecting has persisted a narrowing (or a
        sandbox override) nothing enforces, which is the #2126 failure mode."""
        import yaml
        cfg_path = self._session_state_dir(name, sid) / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"name": f"_session_{sid}", **narrowing}
        if base_dir is not None:
            payload["base_dir"] = str(base_dir)
        if sandbox is not None:
            payload["sandbox"] = dict(sandbox)
        cfg_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    def spawn_session(
        self, name: str, sid: "str | None" = None,
        *, presentation_consumer: "object | None",
        intervention_bridge: "object | None",
        narrowing: "dict | None" = None,
        sandbox: "dict | None" = None,
    ) -> str:
        """FP-0043 Stage 3: open a NEW conversation Session under an existing
        Agent, SHARING the agent's identity object. Returns the new session-id.

        #3562: ``narrowing`` is the #2103-S1a per-session capability mapping the child
        is BORN under — persisted to its own ``config.yaml`` and injected into the live
        session before this returns, so the first turn already gates against it.
        ``None`` (every recovery caller, and any caller with nothing to impose) is
        byte-identical to the pre-#3562 behaviour: no file is written and the sid's own
        persisted layer, if it has one, is what the injection below applies.

        #5352: ``sandbox`` is the SAME shape, one axis over — the resolved sandbox-
        policy override the child is BORN under (the caller has already applied the
        same-agent / cross-agent-declared / cross-agent-undeclared priority table —
        this primitive has no notion of "the spawner" to resolve it itself, same
        division of labour as ``base_dir``). Persisted as a sibling ``config.yaml``
        key and injected into the live session before this returns, so the first
        turn already gates against it. ``None`` is byte-identical to pre-#5352: no
        key is written and the sid's own persisted layer, if it has one (recovery),
        is what the injection below applies.

        ★ Why the channel is HERE and not "route the remaining caller through
        ``spawn_session_recorded``" — a decision, not an omission. ``/session new``
        (``interfaces/slash/session.py``) was the one reachable caller with something to
        inherit and no way to pass it (#3561 recorded that as an UNMET REQUIREMENT
        rather than an exemption). Sending it through the recorded seam instead would
        have carried three unrelated changes with it: a ``session_spawned`` WAL record
        making an operator-opened session rewind-tracked and re-materialisable (which
        operator-created sessions are not today, so a rewind past the command would
        start DROPPING the operator's session), a spawn-time
        ``refresh_config_projections()``, and async-ness. #3562 changes exactly one
        observable thing — the child's envelope — so the channel belongs on the
        primitive every direct caller already shares. It is also where #3561 put the
        injection, for the same reason: this is where the sid becomes known.

        #2708 P3-item3: ``presentation_consumer`` + ``intervention_bridge`` are REQUIRED,
        no-default kwargs (the spawn-axis completeness gate) — every caller must declare an
        explicit spawn-time user-reaching routing decision (``runtime/spawn_routing``): a
        ``BridgeToParent`` / ``AuditOnlyNoSurface`` value's resolved pair, or ``None``/``None``
        only at a reviewed ``ReviewedNA`` self-bound site.
        A missing kwarg is a TypeError (pinned by ``inspect.signature``), so a new spawn site
        cannot silently self-bind into an orphan/hang.

        Structure-only (lead-confirmed): this lets the Registry hold N sessions
        per agent; INBOUND routing to a non-default session is Stage 4 — until
        then the default "main" session receives all inbound traffic. The new
        session shares ``self._identities[name]`` (the same Agent object, S2's
        ``agent=`` seam) so identity is genuinely shared, not duplicated."""
        self.get_or_load(name)  # ensure the default session + _identities[name] exist
        shared = self._identities.get(name)
        new_sid = sid or uuid4().hex[:8]
        if self._has_session(name, new_sid):
            raise ValueError(f"session {new_sid!r} already exists for agent {name!r}")
        # #2708 P3.1/P3.2a: forward the optional present-sink + intervention overrides to the
        # factory (None keeps today's default self-bound outbox consumer + listener-less
        # intervention registry; the attached driver spawn passes a
        # SpawnBridgePresentationConsumer so the driver's present reaches the parent, and a
        # SpawnBridgeInterventionListener so its ask_user reaches the parent's live operator).
        session = self._construct_session(
            self.load_profile(name),
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )
        if shared is not None:
            # Share the SAME identity object (not the fresh one the factory built),
            # so a future identity change propagates to all of the agent's sessions.
            session._agent = shared
        # FP-0043 Stage 5: stamp the new session's id so EVERY WAL append it makes
        # carries new_sid (per-session snapshot routing). Done here — before the
        # session's run-loop / forwarder go live (that is attach_session, S4a,
        # strictly later) — so there is NO "main"-tagged append window for the
        # spawned session. The journal is built eagerly in __init__ (set_session_id
        # propagates to the in-memory snapshot too).
        session.rekey_session_id(new_sid)  # #5287: was a bare field write; now bumps the capability-census generation too
        session._journal.set_session_id(new_sid)
        # FP-0043 Stage 5: re-key the spawned session's persistence to its OWN
        # per-session location so it does NOT collide with the agent's "main"
        # snapshot.json / generations. Derived from the session's own base (the
        # parent of its current main snapshot path) so a test tmp-base is respected
        # — the same base-alignment invariant restore_all's discovery relies on:
        #   <state>/snapshot.json          (main, byte-identical legacy path)
        #   <state>/sessions/<enc(sid)>/snapshot.json + .../generations  (spawned)
        # S4b-1: dir component bijective-encoded (same as _session_state_dir) so an
        # arbitrary routing-key sid is one safe segment; discovery reverse-decodes.
        state_dir = Path(session._snapshot_path).parent
        session_dir = state_dir / "sessions" / self._encode_sid_for_dir(new_sid)
        per_session_snapshot = session_dir / "snapshot.json"
        per_session_generations = SnapshotGenerationStore(
            name, session_dir / "generations",
        )
        session._snapshot_path = per_session_snapshot  # diagnostic mirror
        session._generation_store = per_session_generations  # rewind path reads this
        session._journal.set_snapshot_path(per_session_snapshot)
        session._journal.set_generation_store(per_session_generations)
        # #2348: re-key the conversation transcript + chat audit events per-session
        # too — they were keyed name-only (session.py: history_path / events_dir),
        # so sessions of the same agent shared one history.jsonl (conversations bled
        # across sessions) and one events/agents/<name>/chat tree. history.jsonl is an
        # independent durable transcript (not WAL-reconstructed, outside snapshot/rewind
        # scope); audit events are the P6 audit log. Isolating both aligns them with the
        # already-per-session WAL/snapshot above. "main" (_DEFAULT_SID) never reaches
        # this fixup (it comes through get_or_load), so single-session agents keep the
        # legacy name-only paths byte-identical — no migration.
        session.history_path = session_dir / "history.jsonl"
        # _append_history opens the file directly (no mkdir), mirroring __init__'s
        # workspace_dir.mkdir — the per-session dir must exist. (EventStore creates its
        # own dir lazily on first write, so events need no explicit mkdir.)
        session.history_path.parent.mkdir(parents=True, exist_ok=True)
        session.set_events_dir(
            session.events_dir.parent / "sessions" / self._encode_sid_for_dir(new_sid) / "chat"
        )
        # #3561: re-resolve + inject the sid-keyed #2103-S1a narrowing, for the same
        # reason #2126 does it in spawn_session_recorded and at the same point in the
        # sequence — right after the per-session state dir is finalized, before the
        # caller starts a run-loop. The factory resolved this session's envelope with
        # sid=None (no sid exists yet at construction), so the live session's
        # _contextual_permission — the SINGLE source the RouterLoop's advertisement
        # filter and its _excluded_result call-time gate both read — ignores this sid's
        # config.yaml until something re-resolves WITH the sid.
        #
        # spawn_session_recorded did that for itself and only for itself, which left
        # every caller that reaches this primitive DIRECTLY outside the narrowing:
        # restore_all and _rewake_pipeline_runs re-create a crashed session under its
        # ORIGINAL sid, whose config.yaml is still on disk, and the re-woken session ran
        # unnarrowed. Measured, not inferred: against a live positive control (an
        # un-narrowed session whose write_file DOES land), the re-woken session's denied
        # write_file landed too — while capability_visibility_state() still showed the
        # tool denied, because THAT surface re-resolves with the sid on every read and
        # the enforcement path does not. Two surfaces, one of them decorative; the gap is
        # invisible from the operator's status bar.
        # See tests/runtime/test_3561_spawn_session_seam_reachability.py.
        #
        # Injecting here rather than at each recovery site closes the class at the one
        # place every path shares: this is where the sid becomes known, so it is where
        # the sid-keyed layer becomes resolvable. Inert for a sid with no config.yaml
        # (resolved_profile_for then returns the name-keyed layers the factory already
        # applied).
        #
        # #3562: a caller-supplied ``narrowing`` is persisted FIRST, so the single
        # re-resolve below covers both the sid's already-persisted layer (recovery) and
        # the value this spawn imposes (inheritance) — one write, one injection, no
        # second seam where the two could disagree.
        #
        # ⚠️ This injection USED to be non-durable for a caller that then ran
        # ``refresh_config_projections()``: that refresh's
        # ``reapply_visibility_override`` re-resolves from base and SETs, and on a
        # session with no registry back-reference there was no base to re-resolve, so it
        # set ALLOW-ALL and discarded this. #3593 ① removed the discard at its source —
        # with no base obtained, that method now PRESERVES the live envelope (it has no
        # standing to overwrite it) instead of writing a fabricated one, and says so.
        # ``spawn_session_recorded`` still does NOT pass ``narrowing`` here and re-injects
        # after its own refresh; #3593 ① did not re-litigate that ordering (measured
        # there: with the preserve fix in place, routing the value down this channel no
        # longer REDs the two tests that pinned the ordering — deciding whether to move it
        # is a separate change with its own review). See the note at its
        # ``spawn_session`` call.
        if narrowing or sandbox is not None:
            self._persist_session_narrowing(name, new_sid, narrowing or {}, sandbox=sandbox)
        inject = getattr(session, "apply_per_session_narrowing", None)
        if callable(inject):
            contextual, excluded = self.resolved_profile_for(name, sid=new_sid)
            inject(contextual, excluded)
        # #5352: re-resolve + inject the sid-keyed sandbox override, same reason and
        # same point in the sequence as the narrowing injection immediately above —
        # the factory resolved this session's ``_sandbox_config`` with no sid-keyed
        # override in scope, so the live session ignores this sid's config.yaml
        # ``sandbox:`` key (or the value this spawn just persisted) until something
        # re-resolves + injects it. Inert for a sid with no override at either layer
        # (``resolved_sandbox_for`` returns None → ``apply_per_session_sandbox(None)``
        # is a no-op, byte-identical to pre-#5352).
        inject_sandbox = getattr(session, "apply_per_session_sandbox", None)
        if callable(inject_sandbox):
            inject_sandbox(self.resolved_sandbox_for(name, sid=new_sid))
        # #2285 step2: now that the per-session state dir is finalized (snapshot re-key above),
        # restore any persisted visibility/hook toggles for this (name, sid) — the loaded override
        # composes atop the authoritative envelope, so visible ⊆ authorized survives across restart.
        loader = getattr(session, "load_persisted_toggles", None)
        if callable(loader):
            loader()
        # #5729: route through _store_session (not a raw dict assignment) so
        # this new-sid path gets the status fan-out subscription for free —
        # ``name`` already exists in ``_identities`` here, so the identity-
        # capture half of ``_store_session`` is a harmless no-op setdefault.
        self._store_session(name, session, sid=new_sid)
        return new_sid

    async def spawn_session_recorded(
        self, name: str, *, sid: "str | None" = None, mode: str = "persistent",
        narrowing: "dict | None" = None,
        base_dir: "Path | None" = None,
        sandbox: "dict | None" = None,
        attended: bool = True,
        presentation_consumer: "object | None",
        intervention_bridge: "object | None",
    ) -> str:
        """#2103 S1bc: the action-layer SESSION-SPAWN seam — spawn a fresh-context
        session under ``name`` (sync ``spawn_session``) + persist the spawner's
        per-session capability narrowing (workspace-backed P5 config.yaml, the #2103
        S1a layer — written through the primitive's ``_persist_session_narrowing``, the
        single writer, but from HERE and not through the primitive's ``narrowing``
        channel: see the note at the ``spawn_session`` call for the measurement that
        pins the ordering) + emit ``session_spawned`` so rewind tracks/drops/re-materialises
        it. Mirrors ``create_agent`` (the agent CREATE seam): the mechanism stays sync;
        the event marks the LLM action. ``session_spawned`` is config-complete
        (mode + narrowing) for symmetric re-materialise. Returns the new sid.

        ``sid`` (#4556, optional): pass a caller-chosen session id instead of
        letting ``spawn_session`` auto-generate one via ``uuid4``. Threaded
        straight through — the sync primitive already raises ``ValueError``
        on a duplicate ``(name, sid)`` pair (its own existing guard), which
        this async wrapper does NOT catch; the caller (``RouterHostAdapter.
        spawn_session``) reshapes it into a typed error response.

        ``attended`` (#4193 ①, default ``True``): whether THIS CALLER is going to
        wait on the spawned session — a CALLER decision, never derived from
        ``mode``. The one caller that passes ``False`` is
        ``RouterHostAdapter.spawn_session`` (the ``session_spawn`` LLM tool's
        dispatch target): it returns a spawn-ack and submits the task without
        awaiting completion, regardless of ``mode``. Every other caller
        (the attached pipeline driver, the ``agent``-step ephemeral worker)
        leaves the default — see ``Session._attended``'s own docstring and
        ``OpContext.attended``'s for what this feeds.

        ``base_dir`` (#4200 2/2) is a SIBLING persisted value, not composed the way
        ``narrowing`` is: this method does NOT validate it — restrict-only enforcement
        (the child's resolved value must fall under the SPAWNER's own effective
        ``base_dir``) is the CALLER's job (``RouterHostAdapter.spawn_session``, the
        LLM-facing entry point), because this primitive has no notion of "the
        spawner's session" to validate against and is also called directly by
        non-LLM callers (crash-recovery re-wake, ``/session new``) that have no LLM
        input to restrict in the first place. Pass an already-validated (or
        operator-trusted) value.

        ``sandbox`` (#5352) is ANOTHER sibling persisted value, same posture as
        ``base_dir`` immediately above: this method does NOT resolve the
        same-agent/cross-agent priority table — that is the CALLER's job (it knows
        the spawner's session and the target agent's own declared value; this
        primitive knows neither). Pass an already-resolved value.

        Does NOT submit a task — that is the caller (the spawn op), separable from the
        record. Emit no-ops without a WAL.

        #2708 P3-item3: ``presentation_consumer`` + ``intervention_bridge`` are REQUIRED,
        no-default kwargs — the spawn-time user-reaching routing decision, forwarded to the
        constructed session. A caller declares one of ``runtime/spawn_routing``'s decisions:
        ``BridgeToParent`` (attached pipeline driver / delegated sub-agent — present reaches the
        parent surface, ask_user reaches the parent's live operator), ``AuditOnlyNoSurface``
        (detached/headless — present audit-only, ask_user a typed refusal), or ``ReviewedNA``
        (``None``/``None`` self-bound, reviewed sites only). No silent default (#2708 P3-item3)."""
        # #3562: this seam does NOT hand ``narrowing`` down the primitive's new channel.
        # The original reason was measured: its own write + re-inject (below) had to stay
        # AFTER ``refresh_config_projections()``, because that refresh fires
        # ``reapply_visibility_override``, which re-resolves the envelope from base and
        # SETs it — and when the session had no registry back-reference there was no base
        # to re-resolve, so it set an ALLOW-ALL envelope and silently discarded anything
        # injected before it. Passing the narrowing down was tried and falsified by
        # tests/runtime/test_2103_s1bc_session_spawn_tool.py::
        # test_spawn_session_recorded_enforces_narrowing_on_live_session and
        # tests/runtime/test_pipeline_a2_spawn_ephemeral_session.py::
        # test_spawn_ephemeral_session_narrowing_applied — both went RED with an empty
        # ``tool_deny`` on the live session.
        #
        # ⚠️ #3593 ① removed that discard: with no base obtained,
        # ``reapply_visibility_override`` now preserves the live envelope instead of
        # overwriting it with a fabricated one. Re-measured under the fix — routing the
        # value down the primitive's channel keeps both of those tests GREEN — so the
        # ordering below is no longer FORCED by the refresh. It is left exactly as it is
        # here: #3593 ① is scoped to the fail-open write, and moving a spawn seam's
        # narrowing injection is a behavioural change that deserves its own PR and its
        # own review. Read this as "no longer forced, deliberately not moved", not as
        # "still impossible". The primitive's channel is for callers that do not run that
        # refresh (``/session new``, #3562).
        sid = self.spawn_session(
            name, sid,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )
        # #3036/#3097: every programmatic spawn funnels through here (agent-step
        # ephemeral workers via spawn_ephemeral_session, pipeline driver-sessions via
        # _spawn_pipeline_driver_session — mode="persistent" — and delegate_to_agent's
        # spawn_session tool). None of these callers ever fire a "turn boundary" of
        # their own BEFORE their first programmatic step runs — that trigger is a
        # RouterLoopDriver-only chat-turn concept (Session._run_router_loop), and the
        # spawned session's config-derived projections are otherwise frozen at
        # whatever the (baked-once-at-registry-construction) session_factory closure
        # captured — stale even for config an install wrote (e.g. mcp_install's IN-set
        # .reyn/config/mcp.yaml, or a plugin_install's pipelines/skills/presentations
        # entry) moments before THIS spawn. #3036/#3061 closed this for the MCP roster
        # alone; #3094 point-fixed the pipeline registry alone after that seam
        # surfaced live (#3094); #3097 closes the WHOLE family uniformly —
        # refresh_config_projections() iterates EVERY registered hot-reload seam
        # (session.py's _register_hot_reload_seams(), the same registry a live
        # /reload uses) except cron (the one genuinely side-effecting seam — see
        # that method's own docstring). Reading it fresh here — the spawned
        # session's own action-boundary (invariant: "programmatic/ephemeral sessions
        # refresh at spawn + before each dispatch", #3036 architect verdict) — closes
        # the gap the RAG turnkey flow hit: install (chat session) always precedes
        # the pipeline-driver/agent-step spawn that consumes it, so a spawn-time
        # refresh alone is sufficient (no mid-run re-spawn case exists on this
        # path). ``refresh_config_projections`` never raises (each seam is isolated
        # by the applier) — no try/except needed. Best-effort no-op when the peeked
        # session is somehow gone (should not happen — spawn_session above just
        # constructed it).
        #
        # ★ Recovery invariant: crash-recovery re-wake (``restore_all`` /
        # _rewake_pipeline_runs) calls the lower-level ``spawn_session`` directly,
        # NEVER this method — so a re-woken session never has its config
        # projections refreshed to CURRENT disk state here; it only gets whatever
        # ``restore_state``/the work-order snapshot restores (pre-crash snapshot
        # fidelity, by construction of this call graph, not a special-cased guard).
        spawned_session = self._peek_session(name, sid)
        if spawned_session is not None:
            await spawned_session.refresh_config_projections()
            # #4193 ①: caller-declared, NOT mode-derived — unlike ``_ephemeral``
            # below, this is set unconditionally from the ``attended`` argument
            # every time (default True leaves the Session's own True default
            # unchanged; ``RouterHostAdapter.spawn_session`` is the one caller
            # that passes False).
            spawned_session._attended = attended
        if mode == "ephemeral":
            # #2103: mark the live session so it auto-vanishes once its task is done
            # (Session._maybe_schedule_ephemeral_vanish, via this registry's
            # remove_session teardown seam). Persistent spawns leave the flag False.
            if spawned_session is not None:
                spawned_session.mark_ephemeral()
                # #2585 PR2: an ephemeral spawn is structurally headless — it
                # receives exactly ONE prompt via MessageBus.request (see
                # session_api.run_agent_step) and returns; there is no
                # interactive user on the other end to answer a clarifying
                # question, regardless of which frontend's session_factory
                # spawned it. Force the override here (NOT in spawn_session
                # itself, which persistent/A2A spawns also use and which MAY
                # have a real user eventually) so the worker always lands on
                # the "proceed with assumption" SP branch instead of wasting
                # its one turn asking a question no one can answer.
                spawned_session._non_interactive = True
        if narrowing or base_dir is not None or sandbox is not None:
            # #3562: the file write itself is the primitive's ``_persist_session_narrowing``
            # — one writer for both spawn entry points, so the two cannot drift from each
            # other or from the reader. Only the CALL SITE stays here; see the note above
            # this method's ``spawn_session`` call for the measurement that keeps it here.
            # #4200 2/2: base_dir alone (no narrowing) must still reach the write — the
            # pre-#4200 condition (``if narrowing:``) would silently drop a base_dir-only
            # spawn request. #5352: same for sandbox alone.
            self._persist_session_narrowing(
                name, sid, narrowing or {}, base_dir=base_dir, sandbox=sandbox,
            )
            # #2126: ENFORCE the narrowing just written. The per-session capability
            # layer (#1827 / #2103-S1a) only resolves WITH a sid, and every
            # construction-time factory caller resolves sid=None — so the live spawned
            # session's _contextual_permission (set once at construction) ignores its
            # own config.yaml. Re-resolve WITH the sid and re-inject into the live
            # session here, BEFORE the caller starts its run-loop, so the first turn
            # gates against the narrowing. Without this the write above is inert
            # (security-theater: narrowing accepted + persisted but never enforced).
            session = self._peek_session(name, sid)
            inject = getattr(session, "apply_per_session_narrowing", None)
            if callable(inject):
                contextual, excluded = self.resolved_profile_for(name, sid=sid)
                inject(contextual, excluded)
            # #5352: the SAME #2126 enforcement, one axis over — the sandbox override
            # just written (or already present from a prior spawn/recovery) is inert
            # until re-resolved WITH the sid and re-injected into the live session.
            inject_sandbox = getattr(session, "apply_per_session_sandbox", None)
            if callable(inject_sandbox):
                inject_sandbox(self.resolved_sandbox_for(name, sid=sid))
        if self._state_log is not None:
            await self._state_log.append(
                "session_spawned", entity_kind="session", name=name, sid=sid,
                mode=mode, narrowing=narrowing,
                base_dir=str(base_dir) if base_dir is not None else None,
                sandbox=sandbox,
            )
        return sid

    async def ensure_running(self, name: str, sid: str = _DEFAULT_SID) -> "object":
        """Load + start session.run() + forwarder for `(name, sid)` without
        changing the user-attached pointer. Used for agent-to-agent
        messaging (PR11): when A sends to B, B's task must be live to
        consume the inbox put, but the user's display stays on whoever
        they were attached to.

        The forwarder is still started so that, should the user later
        attach to B, B's pre-existing outbox messages route correctly.

        #3793 stage 2: ``sid`` is now a parameter (defaulting to
        ``_DEFAULT_SID`` — every pre-existing caller that omits it keeps
        byte-identical behaviour). This is what makes this method the boot
        -only primitive AG-UI's 5 ``registry.attach(agent_name)`` call sites
        move to: AG-UI's own per-connection state (``SurfaceManager``, keyed
        by ``connection_id``) already tracks who is attached to what — it
        never read ``self._connection``/``attached_name``/``attached_session``
        (measured: zero references in ``interfaces/transport/agui/``), so
        the ONLY thing AG-UI actually needed from ``attach()`` was this boot
        side effect. Calling this instead of ``attach()`` means an AG-UI
        connection resolving/booting a session no longer flips the
        registry's own ``AttachedConnection`` (the TUI's shared focus
        pointer) — the ADR-0039 D4 N:N witness this stage exists to satisfy.
        """
        session = self.get_or_load(name)
        key = (name, sid)
        if key not in self._tasks or self._tasks[key].done():
            self._ensure_session_run(name, sid, session)
        if key not in self._forward_tasks or self._forward_tasks[key].done():
            self._forward_tasks[key] = asyncio.create_task(self._forwarder(name, sid))
        return session

    def ensure_session_running(self, name: str, sid: str) -> "object | None":
        """FP-0043 Stage 4b-2: start a session's run-loop WITHOUT a forwarder.

        For a transport that drains a Session's ``.outbox`` DIRECTLY (web: each
        browser thread drains its own ``web:<thread>`` session), the registry-level
        forwarder must NOT run — the forwarder ``await``s ``session.outbox.get()``
        and would race / steal the messages the direct drain needs. So this only
        boots ``session.run()`` (so the inbox is consumed), keyed by ``(name, sid)``,
        and leaves output to the caller's direct drain. Idempotent; no-op if the
        session is not loaded (the caller resolves/spawns it first). Distinct from
        ``ensure_running`` (default session + forwarder for the REPL/TUI sink)."""
        session = self._peek_session(name, sid)
        if session is None:
            return None
        key = (name, sid)
        if key not in self._tasks or self._tasks[key].done():
            self._ensure_session_run(name, sid, session)
        return session

    def bind_focus_listeners(
        self,
        *,
        on_audit_event: "Callable[..., None] | None" = None,
        intervention_channel: str | None = None,
    ) -> None:
        """Bind front-end listeners that follow the focused (attached) session.

        The interactive REPL/CUI binds its working-indicator audit-event callback
        and its intervention listener channel here ONCE; the registry wires them
        to the currently-attached session now and re-wires them on every
        subsequent ``attach`` / ``attach_session`` so an agent switch never
        strands them on the old session. Idempotent per front-end (one binding).
        """
        self._focus_chat_listener = on_audit_event
        self._focus_intervention_channel = intervention_channel
        self._wire_focus_listeners(self.attached_session())

    def unbind_focus_listeners(self) -> None:
        """Unwire the focus listeners from the live attached session and clear
        the binding (front-end teardown). Uses the CURRENT attached session, so a
        switch before teardown unwires the right one."""
        self._unwire_focus_listeners(self.attached_session())
        self._focus_chat_listener = None
        self._focus_intervention_channel = None

    def _wire_focus_listeners(self, session: "object | None") -> None:
        """#5041 ① (architect's own BLOCK finding, #5344's TESTS-READ(B)):
        the closure actually subscribed to ``session.audit_events`` BINDS
        the target agent's name NOW, at subscribe time — never left for
        the listener to read a live ``self.attached_name`` later, when it
        is actually CALLED. ``EventLog`` dispatches to subscribers via its
        own background consumer (#4961 C), not synchronously with
        ``emit()`` — so a listener that reads global "who's attached"
        state at call time is answering an EXECUTION-ORDER question
        ("has a switch landed before this callback happened to run?") it
        has no business depending on. Binding here instead means a
        callback that runs late still carries the name of the session it
        was ACTUALLY subscribed to, regardless of any later switch —
        structurally, not by timing luck.

        ``self.attached_name`` is safe to read HERE specifically because
        this call always happens in the SAME synchronous block as the
        connection flip that made ``session`` the target (``attach()`` /
        ``attach_session()`` / ``bind_focus_listeners``'s own initial
        wire) — the same no-``await``-in-between barrier property
        ``_announce_session_attached`` already relies on elsewhere in
        this file."""
        if session is None:
            return
        if self._focus_chat_listener is not None:
            listener = self._focus_chat_listener
            agent = self.attached_name

            def _bound_listener(event: "object", _listener=listener, _agent=agent) -> None:
                _listener(event, agent=_agent)

            self._wired_chat_listener = _bound_listener
            session.subscribe_audit_events(_bound_listener)
        if self._focus_intervention_channel is not None:
            try:
                session.register_intervention_listener(self._focus_intervention_channel)
            except AttributeError:
                pass

    def _unwire_focus_listeners(self, session: "object | None") -> None:
        if session is None:
            return
        if self._wired_chat_listener is not None:
            # Unsubscribe the EXACT closure `_wire_focus_listeners` built
            # (never a freshly-constructed one, or the identity check
            # inside `unsubscribe_audit_events` silently no-ops and the
            # old session keeps calling a listener meant for a switch
            # that already happened).
            session.unsubscribe_audit_events(self._wired_chat_listener)
            self._wired_chat_listener = None
        if self._focus_intervention_channel is not None:
            try:
                session.unregister_intervention_listener(self._focus_intervention_channel)
            except AttributeError:
                pass

    def add_attach_listener(self, agent_name: str, callback: "Callable[[str], None]") -> None:
        """#4534 PR-2b: register a per-agent-name switch-follow subscriber.

        ``callback`` is invoked with the target ``sid`` whenever
        ``attach``/``attach_session`` switches focus for ``agent_name`` —
        the AG-UI remote tap's per-connection replacement for consuming the
        retired ``__session_switch_request__`` sentinel off the outbox
        (:class:`~reyn.interfaces.transport.agui.endpoint._SessionFrameSource`).
        Fired synchronously, with no ``await`` between the connection flip
        and the call (:meth:`_announce_session_attached`'s own barrier
        property, extended to every listener, not only ``repl_outbox``) —
        ``callback`` must not block or await; its job is to hand the sid to
        a side-channel (e.g. an ``asyncio.Queue.put_nowait``) a consumer
        task picks up, mirroring ``_on_audit_event``'s own idiom."""
        self._attach_listeners.setdefault(agent_name, []).append(callback)

    def remove_attach_listener(self, agent_name: str, callback: "Callable[[str], None]") -> None:
        """Undo :meth:`add_attach_listener`. A no-op if already removed
        (connection teardown racing a registry-side cleanup is tolerated,
        not an error)."""
        listeners = self._attach_listeners.get(agent_name)
        if not listeners or callback not in listeners:
            return
        listeners.remove(callback)
        if not listeners:
            del self._attach_listeners[agent_name]

    def add_remove_listener(self, callback: "Callable[[str], None]") -> None:
        """#5146: subscribe to "an agent name was just purged" — ``callback``
        is invoked with the purged ``name``, fired synchronously from inside
        :meth:`remove` (purge only), same no-``await``-between critical-
        section idiom as :meth:`add_attach_listener`. The intended (only,
        today) consumer is AG-UI's ``SurfaceRegistry`` (#5146): a purge frees
        the name for immediate re-declaration, but a stale ``SurfaceManager``
        keyed by that name would otherwise hand the NEW identity's first
        connection the OLD identity's ``_active_driver`` token and surface
        set — the #5084 name-reuse class, for operator authority instead of
        spawn lineage. This registry does not import or call into transport
        itself (#5139's own layering ruling); the listener is the seam that
        lets transport clean up its own bookkeeping instead."""
        self._remove_listeners.append(callback)

    def remove_remove_listener(self, callback: "Callable[[str], None]") -> None:
        """Undo :meth:`add_remove_listener`. A no-op if already removed."""
        if callback in self._remove_listeners:
            self._remove_listeners.remove(callback)

    # ── #5729: per-session status (turn_active / iv_waiting) fan-out ───────

    def add_status_listener(
        self, callback: "Callable[[str, str, bool, bool, int], None]",
    ) -> None:
        """Subscribe to every live session's ``(turn_active, iv_waiting)``
        transitions in this process, for the TUI agent tab (#5729).

        ``callback`` is invoked synchronously with ``(agent_name, sid,
        turn_active, iv_waiting, seq)`` — the sibling of
        :meth:`add_attach_listener`'s fired-synchronously, no-``await``-
        between idiom, not a new mechanism. Unlike ``add_attach_listener``
        this is not keyed by agent name: a status consumer wants every
        session in this process (owner ruling B — a remote client sees
        sessions it has not attached too), so there is exactly one global
        listener list, not one per name.

        ``seq`` is a per-``(name, sid)`` monotonic counter this registry
        owns (see :attr:`_status_seq_by_key`) — a caller merging deltas
        keeps the highest ``seq`` applied per key and discards one that is
        not strictly greater, the same stale-delta-cannot-resurrect-old-
        state gate ``Session._bump_queue_seq`` already established for the
        sent-queue region. It is a SEPARATE counter, not a reuse of
        ``Session.queue_seq`` — see :attr:`_status_seq_by_key`'s own
        comment for why."""
        self._status_listeners.append(callback)

    def remove_status_listener(
        self, callback: "Callable[[str, str, bool, bool, int], None]",
    ) -> None:
        """Undo :meth:`add_status_listener`. A no-op if already removed."""
        if callback in self._status_listeners:
            self._status_listeners.remove(callback)

    def status_listener_count(self) -> int:
        """Public read of how many :meth:`add_status_listener` subscribers
        are currently registered (#5729) — the test-facing witness that a
        consumer's teardown (``on_unmount`` in the TUI) actually called
        :meth:`remove_status_listener` rather than leaking one per attach/
        detach cycle. Not used by any production code path."""
        return len(self._status_listeners)

    def _subscribe_session_status(self, name: str, sid: str, session: "object") -> None:
        """Wire one freshly-stored session into the status fan-out (#5729).

        Called exactly once per real session object, from :meth:`_store_session`
        (both its own call site and the ``spawn_session`` new-sid path, which
        routes through it — see that method's own note).

        One audit-event subscription now covers both halves of
        ``iv_waiting`` (as well as ``turn_active``):

        - **turn_active**: ``turn_started``/``turn_settled`` (NOT
          ``chat_turn_completed_inline`` — that one only fires on the single
          router branch that took no catalog dispatch, per router_loop.py's
          own guarding ``if``; ``turn_settled`` is the unconditional
          ``finally``-block event session.py documents as firing "for EVERY
          turn kind").
        - **iv_waiting OUT** (resolved): ``user_answered_intervention`` —
          verified common to all 6 ``intervention_bus.request()`` callers
          (renderer.py's own comment: "the SAME primitive... which all 6
          paths DO share" — the resolution funnel every answer path routes
          through, ``InterventionHandler.deliver_answer_to``).
        - **iv_waiting IN** (enqueued): ``intervention_announced`` — a NEW
          #5729 audit event, emitted from ``InterventionHandler.announce``
          (the ONE choke point all 6 callers share; ``user_intervention_
          requested`` is ask_user.py-only, verified via renderer.py's own
          comment).

        ⚠️ An earlier revision of this method subscribed to the session's
        OUTBOX (``session.outbox_hub``) instead, since ``announce`` puts a
        ``kind="intervention"`` message there too. That was a real,
        MEASURED regression: adding a subscriber starts ``OutboxHub``'s
        drain task eagerly for EVERY session (its own docstring: "Lazily
        (re)starts... only when the first surface attaches") — for a
        session nothing else has ever subscribed to, this began silently
        consuming ``session.outbox`` before any real UI attached, starving
        direct ``outbox.get_nowait()`` readers elsewhere (caught by
        ``test_multisession_history_isolation_2348.py`` going from green to
        red on this exact change). The new ``intervention_announced`` audit
        event is the fix: same choke point, the ALREADY-existing,
        side-effect-free ``subscribe_audit_events`` channel instead.

        Recomputes both bools LIVE off ``session`` on every fire (never
        reads from the triggering event itself) — over-firing is harmless
        (the recompute is idempotent), under-firing would leave a bool
        stuck stale, the worse failure.

        ``name``/``sid`` are captured by this call's own local scope, not a
        shared loop variable — each call to this method gets its own stack
        frame, so the closure below cannot suffer the classic late-binding
        bug where every closure in a loop ends up sharing the LAST loop
        variable's value (see the accept test that drives 2 real sessions
        and asserts the two never cross)."""
        interventions = getattr(session, "interventions", None)

        def _on_status_event(_event: "object") -> None:
            turn_active = bool(getattr(session, "turn_active", False))
            iv_waiting = (
                not interventions.is_empty() if interventions is not None else False
            )
            key = (name, sid)
            seq = self._status_seq_by_key.get(key, 0) + 1
            self._status_seq_by_key[key] = seq
            for listener in list(self._status_listeners):
                listener(name, sid, turn_active, iv_waiting, seq)

        subscribe = getattr(session, "subscribe_audit_events", None)
        if callable(subscribe):
            subscribe(_on_status_event, kinds=_STATUS_AUDIT_EVENT_KINDS)

    def all_sessions_status(self) -> "list[dict]":
        """Snapshot of ``turn_active``/``iv_waiting`` for every live session in
        this process (#5729), for the agent tab's initial render / a reader
        that missed deltas.

        Computed fresh on every call by enumerating live sessions and reading
        each one's own public accessors — the same "no stored copy, compute
        by listing" shape :meth:`session_tree` already established (architect
        ruling: a saved dict here would be a second copy of status that
        nothing bounds from drifting). Iterates :meth:`list_active_names`
        (every declared, non-archived agent) the same way :meth:`session_tree`
        does, but — unlike that method — only emits a row for a name that has
        at least one LOADED session (an unattached, never-loaded agent has no
        live Session to read ``turn_active``/``iv_waiting`` off of; it is not
        "not running", it is "nothing to report", so it is simply absent
        here rather than fabricated as False).

        ★ Scope: this process only. A session in a sibling process is
        invisible to this call (registry has no cross-process knowledge —
        #5694/#5714) — the caller (the agent tab) must render this as "every
        session in this process", never as "every session"."""
        out: "list[dict]" = []
        for name in self.list_active_names():
            sessions = self._sessions.get(name) or {}
            for sid in sorted(sessions):
                session = sessions[sid]
                interventions = getattr(session, "interventions", None)
                out.append({
                    "agent": name,
                    "sid": sid,
                    "turn_active": bool(getattr(session, "turn_active", False)),
                    "iv_waiting": (
                        not interventions.is_empty()
                        if interventions is not None else False
                    ),
                })
        return out

    def _announce_session_attached(self, name: str, sid: str) -> None:
        """#3310 N1: notify the client a switch just happened, as a stream
        BARRIER on ``repl_outbox`` — the one queue every local client drains.

        ★Altitude: this cannot ride ``new_session._audit_events`` (the per-
        session audit-event stream `session.subscribe_audit_events` follows) —
        that stream IS the thing being swapped, so a client keying its reset
        on an event emitted FROM the new session couldn't distinguish "new
        session's first frame" from "the switch itself". Emitting here, at
        the registry seam that owns ``repl_outbox``, is the one altitude
        that sees both the old and the new session.

        ★It is an ``EventFrame``, never an ``OutboxMessage`` kind — same
        reasoning the owner ratified for #3288 ③b: an EVENT frame is opt-in
        draw (a consuming surface with no branch for it drops it silently,
        never rendering a garbage row), where an unknown DISPLAY kind is
        rendered generically. Registering a new closed-vocabulary
        ``OutboxMessage`` kind for a pure state transition would be the
        category error #3288 ③b designed out.

        ★Barrier property (design-pass, issue #3310): this call is placed
        IMMEDIATELY after the ``self._connection.switch(key)`` flip, with NO
        ``await`` anywhere in between (``switch`` is a plain synchronous
        method, and ``Queue.put_nowait`` never suspends, unlike
        ``await Queue.put``). Single event loop ⇒ nothing can interleave
        inside that synchronous region, so on the ``repl_outbox`` FIFO
        "before this frame = old session's frames, after = new session's
        frames" holds BY CONSTRUCTION — a client resets its display on this
        frame and cross-contamination between sessions becomes impossible,
        not merely unlikely. Mirrors the no-await critical-section idiom in
        ``Session.cancel_queued`` (#3300 Y-server / #3306).
        """
        self.repl_outbox.put_nowait(
            EventFrame(Event(type="session_attached", data={"agent": name, "session_id": sid}))
        )
        # #4534 PR-2b: same no-await critical section, extended to every
        # per-agent-name switch-follow subscriber (see add_attach_listener).
        # Iterates a snapshot (list(...)) so a listener that unsubscribes
        # itself mid-callback (unlikely — callbacks must not block or await
        # — but not disallowed) never mutates the list being walked.
        for callback in list(self._attach_listeners.get(name, ())):
            callback(sid)

    async def attach(self, name: str, *, start_runner: bool = True) -> "object":
        """Switch the attached agent to `name`. Loads + starts session.run()
        and the outbox forwarder for the new agent if not already running.
        Old agent stays in `self._tasks` (background).

        ``start_runner`` (#4113, architect ruling 2026-08-10): False skips
        ONLY the ``self._ensure_session_run(name, sid, new_session)`` line
        below — load happens regardless (``get_or_load`` a few lines down), and
        every other side effect (forwarder, focus-listener wiring,
        connection switch, announce, pending-intervention replay) is
        UNCHANGED. Exists for ``reyn run-once``: fixing the "violating
        side" of a same-process double-pump (`registry.attach()`'s own
        background `session.run()` loop racing `send_to_agent_impl`'s
        inline `MessageBus.request` pump on the identical Session object —
        the exact shape `a2a.py`'s "self-running OR inline-driven, never
        both" invariant forbids) rather than adding a generic "refuse if a
        runner exists" gate at the pump layer, which would break every
        already-shipped MCP/A2A caller that legitimately drives a
        self-running session inline today (architect: the same
        "fix becomes an outage" shape as the MCP double-pump warning,
        this time concrete). `_run_once`'s own design never needed the
        runner — it drives the session to completion via
        `send_to_agent_impl` itself; the runner's job (nothing else)."""
        self._assert_owner_thread()
        # #3671 P3: a fresh attach attempt (or its `get_or_load` below, which
        # can itself raise) is never shadowed by a PRIOR background attempt's
        # recorded failure — clear it up front rather than only on success,
        # so a caller retrying after `record_background_attach_error` sees a
        # clean "connecting" state again immediately, not "failed" until
        # this attempt ALSO finishes.
        self._connection.record_background_attach_error(None)
        new_session = self.get_or_load(name)
        sid = _DEFAULT_SID  # FP-0043 S3: attach(name) focuses the default session
        key = (name, sid)
        old = self._connection.active
        if old is not None and old != key:
            old_session = self._peek_session(old[0], old[1])
            if old_session is not None:
                # Move any focus-following front-end listeners off the old session.
                # #3793 stage 2: no longer also flips a `Session.is_attached`
                # bool here — `Session._put_outbox_nowait` derives "nobody is
                # watching" directly from `outbox_hub.has_subscribers()`
                # instead of a second, manually-synced representation.
                self._unwire_focus_listeners(old_session)

        if old != key:
            # First attach or a genuine switch: wire the focus listeners to the
            # now-focused session (no-op if no front-end bound any).
            self._wire_focus_listeners(new_session)
        # Boot session.run() + forwarder on first attach. Keep them alive
        # across detach/re-attach cycles — shutdown drains via `running_tasks()`.
        if start_runner and (key not in self._tasks or self._tasks[key].done()):
            self._ensure_session_run(name, sid, new_session)
        if key not in self._forward_tasks or self._forward_tasks[key].done():
            self._forward_tasks[key] = asyncio.create_task(
                self._forwarder(name, sid)
            )
        self._connection.switch(key)
        self._announce_session_attached(name, sid)

        # Re-announce any pending interventions for the user. While detached,
        # `_announce_intervention` already put the original message on the
        # session outbox, but the forwarder dropped it (detached). On attach
        # we replay each pending iv so the user sees what's waiting.
        # (Post-refactor: the active intervention queue lives on the
        # InterventionRegistry service; reach via `_interventions.list_active()`.)
        for iv in new_session._interventions.list_active():
            if not iv.future.done():
                await new_session._announce_intervention(iv)
        return new_session

    async def attach_session(self, name: str, sid: str) -> "object":
        """FP-0043 Stage 4a: focus an EXISTING conversation Session ``(name, sid)``
        — the session-level analogue of ``attach``. Unlike ``attach`` (which
        get_or_loads the default session, BUILDING it if absent), this requires
        the target session to already exist (= opened via ``spawn_session``) and
        raises ``KeyError`` otherwise — no build, focus only. Mirrors ``attach``'s
        run-loop/forwarder boot + the connection-switch flip, so the focused
        session's output routes to ``repl_outbox`` and the previously-focused
        session stops forwarding (``self._connection.is_attached(key)`` — #3793
        stage 2: no longer also a ``Session.is_attached`` bool flip)."""
        target = self._peek_session(name, sid)
        if target is None:
            raise KeyError(f"no session {sid!r} for agent {name!r}")
        key = (name, sid)
        old = self._connection.active
        if old is not None and old != key:
            old_session = self._peek_session(old[0], old[1])
            if old_session is not None:
                self._unwire_focus_listeners(old_session)
        if old != key:
            self._wire_focus_listeners(target)
        if key not in self._tasks or self._tasks[key].done():
            self._ensure_session_run(name, sid, target)
        if key not in self._forward_tasks or self._forward_tasks[key].done():
            self._forward_tasks[key] = asyncio.create_task(self._forwarder(name, sid))
        self._connection.switch(key)
        self._announce_session_attached(name, sid)
        for iv in target._interventions.list_active():
            if not iv.future.done():
                await target._announce_intervention(iv)
        return target

    async def _forwarder(self, name: str, sid: str = _DEFAULT_SID) -> None:
        """Pump one session's outbox into the registry-level repl_outbox.

        Runs continuously per (name, sid) session. Only forwards when that
        session is the attached one; otherwise drops the message (transient
        kinds were already dropped at source, durable narration is in history).

        ADR-0039 P6b: subscribes to the session's outbox *hub* (unbounded local
        sink) instead of draining ``session.outbox`` directly — so this local
        REPL forwarder and any concurrent AG-UI surface each receive the full
        stream rather than stealing frames. Delivery is byte-identical to the
        pre-hub direct drain (an unbounded subscription is a transparent 1:1
        pipe and is never disconnected-slow).
        """
        key = (name, sid)
        agent = self._peek_session(name, sid)
        sub = agent.outbox_hub.subscribe()  # maxsize=0 → unbounded local sink
        try:
            while True:
                msg = await sub.get()
                if msg is None:
                    # Subscription force-closed (disconnect-slow). An unbounded
                    # local sink never reaches this, but end cleanly if it ever does.
                    return
                if msg.kind == "__end__":
                    # Session shut down — propagate to REPL only if we're the
                    # attached one (otherwise REPL would terminate spuriously
                    # on a detached session's shutdown).
                    if self._connection.is_attached(key):
                        await self.repl_outbox.put(msg)
                    return
                if self._connection.is_attached(key):
                    await self.repl_outbox.put(msg)
                # else: drop — session is detached, transient kinds were already
                # dropped at source, durable narration is in history.jsonl
        finally:
            sub.close()

    def detach(self) -> None:
        """Mark the attached session as detached without stopping its task."""
        active = self._connection.active
        if active is None:
            return
        self._connection.switch(None)

    @property
    def attached_name(self) -> str | None:
        # FP-0043 S3: the connection's active key is (name, sid); the public
        # accessor exposes the agent NAME (byte-identical to the prior str|None).
        active = self._connection.active
        return active[0] if active is not None else None

    @property
    def attached_sid(self) -> str | None:
        """FP-0043 Stage 4a: the focused session-id (or None) — the public
        surface for `/session list`'s focus marker + tests, so callers don't
        reach into the connection's own state."""
        active = self._connection.active
        return active[1] if active is not None else None

    def attached_session(self) -> "object | None":
        """#4995 slice 1 (architect correction): NOT owner-thread-asserted
        — a read, not a mutation. See :meth:`get_session`'s own docstring."""
        active = self._connection.active
        if active is None:
            return None
        return self._peek_session(active[0], active[1])

    def record_background_attach_error(self, error: str) -> None:
        """#3671 P3: called by a caller doing a BACKGROUND attach (P2's
        `chat.py._background_attach`) when it gives up, so `attach_failed()`
        below can tell a client apart "still connecting" from "gave up" —
        both look identical from `has_session()` alone (`False` either way).
        A caller that never does a background attach never calls this; a
        transport reading `attach_failed()` before ANY attach was even
        attempted correctly still sees `False` (== "connecting")."""
        self._assert_owner_thread()
        self._connection.record_background_attach_error(error)

    def attach_failed(self) -> bool:
        """#3671 P3: whether the most recent attach attempt is KNOWN to have
        given up (vs. still in flight, or having never started) — see
        `record_background_attach_error`. Cleared at the top of every
        `attach()` call, so a retry is never shadowed by a stale failure."""
        return self._connection.attach_failed()

    async def resume_deferred_agents(self) -> "list[str]":
        """#3671 P4 item C-1 (v2, owner ruling): proactively build+restore+run
        every in-flight agent `restore_all(only_names=...)` deferred — i.e.
        finish what pre-C-1 `restore_all()` used to do for ALL of them,
        unconditionally. Called by `chat.py`'s background attach task AFTER
        `attach(name)` has already completed: the target agent is already
        live and `has_session()` has already flipped True by the time this
        runs, so it never delays startup — but every crashed-mid-task agent
        still auto-resumes THIS run, matching pre-C-1 crash-recovery
        semantics (the owner ruling: "resume everyone, just don't make
        startup wait for it").

        Two live paths reach `self._pending_restore` (lead-coder review,
        #3683): this proactive sweep, AND `get_or_load`'s own on-demand hook
        — genuinely BOTH reachable, not a declared-but-dead second branch:
        the interactive client is already rendering (P2) while this sweep
        runs as its own background task, so the operator's own `/attach
        <other>` or a live delegation's `ensure_running(<other>)` can reach a
        still-pending agent before this sweep gets to it. Both are safe
        together because `self._pending_restore.pop(key, None)` is the SOLE
        gate on every `restore_state` call for a given `(name, sid)` — this
        is a `dict.pop`, synchronous and atomic under asyncio's single-
        threaded cooperative scheduling (NOT thread-safe in general — that
        guarantee is specific to this being asyncio, not a claim that would
        hold under real OS threads). Whichever path's `pop` returns
        non-`None` first is the sole owner for that entry; the other finds
        `None` and skips. Separately, `attach()`/`ensure_running()` ALSO each
        independently guard their own `self._ensure_session_run(name, sid,
        session)` call against re-creation (`if key not in self._tasks or
        self._tasks[key].done(): ...`), so even a session already restored
        by one path never gets a second run-task from the other.
        """
        self._assert_owner_thread()
        resumed: "list[str]" = []
        for name, sid in list(self._pending_restore.keys()):
            # Pop (not peek) BEFORE constructing: `get_or_load` is synchronous
            # (no `await` inside it), so nothing else can run between this
            # pop and the `get_or_load` call below within THIS coroutine —
            # no window for an on-demand `attach()`/`ensure_running()` to
            # race THIS (name, sid) between the two lines. See the docstring
            # above for why racing a DIFFERENT (name, sid) (there IS an
            # `await` below, on purpose) is also safe.
            snap = self._pending_restore.pop((name, sid), None)
            if snap is None:
                continue  # already consumed via on-demand attach/ensure_running
            session = self.get_or_load(name)
            session.restore_state(snap)
            await self.ensure_running(name)
            resumed.append(name)
            # #3671 P4 C-1 v2 (owner ruling: don't compete with the client's
            # own first render / input handling for CPU): `ensure_running`
            # itself has no internal `await` (it only SCHEDULES tasks via
            # `asyncio.create_task`, never blocks on them), so without this
            # explicit yield, sweeping N deferred agents would run essentially
            # synchronously with no chance for the render/input loop (a
            # SEPARATE task) to actually get scheduled in between.
            await asyncio.sleep(0)
        return resumed

    def _spawn_session_run(
        self, name: str, sid: str, coro: "Coroutine[Any, Any, Any]",
    ) -> asyncio.Task:
        """#5694 stage 2 (architect ruling): the ONE creation site for a
        ``(name, sid)`` background ``session.run()`` task. Originally
        (this method's own first PR) called directly from each of this
        file's 4 pre-existing ``asyncio.create_task(<session>.run())``
        call sites (``ensure_running``, ``ensure_session_running``,
        ``attach``, ``attach_session``); a later same-issue PR added
        :meth:`_ensure_session_run` between them and this method (see
        that method's own docstring) — all 4 sites now route through
        THAT method, which is this method's own sole caller.

        Root cause this closes: measured directly (``git grep -c
        'create_task(' registry.py`` before this change) — 4 creation
        sites, 0 ``add_done_callback`` registrations anywhere in this
        file, and every ``.done()`` read on these tasks was ONLY ever the
        pre-existing "should I restart this?" guard
        (``key not in self._tasks or self._tasks[key].done()``), never a
        read of *why* it finished. A ``session.run()`` task that raises is
        therefore consumed by nothing more specific than Python's own
        generic "Task exception was never retrieved" unhandled-exception
        path (``asyncio_diagnostics.py``'s global handler) — which never
        even learns *which* ``(name, sid)`` died, because none of these 4
        call sites passed ``name=`` to ``create_task`` either. #5694's own
        incident (a specific agent's Session vanishing while a sibling
        agent kept answering fine, in the SAME process) is exactly the
        shape this silence hides: the death was real, but nothing recorded
        it as an event distinguishable from "the whole process is fine."

        Named (``session.run:<name>:<sid>``) so the generic handler above
        also gets a real diagnostic if it ever fires for one of these
        specifically. ``coro`` (not the ``Session`` object) is the
        parameter — this method has no opinion on what it's running,
        only on funnelling every outcome through one done-callback; the 4
        real call sites pass ``<session>.run()``, tests exercising the
        3-way outcome dispatch below can pass any coroutine.

        NOT folded into ``runtime.tracked_tasks.TrackedTaskSet`` (#4759):
        that module's own docstring declares "every producer calls
        ``TrackedTaskSet.spawn`` (never ``asyncio.create_task``
        directly)", and on its face this looks like exactly such a
        producer. It isn't, for two independent reasons, checked directly
        against ``TrackedTaskSet``'s own contract (``tracked_tasks.py``)
        rather than assumed:

        1. **Different owner, different lifetime.** Every existing
           ``TrackedTaskSet`` instance is a ``Session``'s own
           ``_background_tasks`` — its ``aclose()`` is called from that
           SAME session's own teardown/quiesce (``await_quiescent``,
           ``aclose_background_tasks``). A ``(name, sid)`` run-task's
           lifetime is NOT scoped to any one session's teardown — it
           already outlives a `/detach`, and this registry (not the
           ``Session`` being run) is what starts, restarts and finally
           cancels it (``shutdown()``, ``remove_session()``). Session-
           scoping this task inside the very ``Session`` object it drives
           would reproduce the #5709 R5 hazard that issue's own review
           named for a structurally identical question ("a SEPARATE
           ``TrackedTaskSet``, never a Session's own ``_background_
           tasks``") — one level up: THIS task is the thing that RUNS
           ``Session.run()``, not a helper a running session spawns
           for itself.
        2. **The restart-guard needs keyed lookup; ``TrackedTaskSet``
           doesn't offer one.** ``TrackedTaskSet`` is a set (``__iter__``/
           ``__len__``/``pending()`` — no ``__getitem__``, no key
           parameter anywhere in its public surface); every one of this
           file's 4 call sites needs `` self._tasks[key].done()`` to
           decide "restart or reuse" BEFORE creating a task, which a
           set-shaped funnel cannot answer without this file keeping a
           SEPARATE ``dict`` alongside it anyway — the very duplication
           ``TrackedTaskSet`` exists to remove for its own producers. The
           existing ``self._tasks: dict[(name, sid), asyncio.Task]`` IS
           this file's own funnel (enumerable via ``running_tasks()``,
           drained in ``shutdown()``); this method makes its CREATION
           side single, the same shape ``TrackedTaskSet.spawn`` gives its
           own producers, without discarding the keyed lookup none of
           the 4 call sites can do without.
        """
        task = asyncio.create_task(coro, name=f"session.run:{name}:{sid}")
        task.add_done_callback(functools.partial(self._on_session_run_task_done, name, sid))
        return task

    def _on_session_run_task_done(self, name: str, sid: str, task: asyncio.Task) -> None:
        """The one done-callback #5694 stage 2 prescribes — reads exactly
        3 mutually-exclusive outcomes and durably records which one, with
        the ``(name, sid)`` this task was for.

        ``task.exception()`` is called UNCONDITIONALLY on a non-cancelled
        task (never merely `` task.cancelled()``-then-skip) — this is not
        optional: asyncio only suppresses its own "Task exception was
        never retrieved" console warning once something has actually
        RETRIEVED the exception via ``.exception()``/``.result()``; a
        callback that only checked ``.cancelled()`` and returned early on
        a normal-looking task would leave a raised-but-unread exception on
        the table for asyncio's own generic (unnamed, (name, sid)-blind)
        handler to complain about later — the exact silence this issue
        exists to close, reintroduced one line away from the fix.
        ``task.cancelled()`` is read FIRST because ``.exception()`` itself
        raises ``CancelledError`` on a cancelled task (checking would
        crash this callback, not record cancellation).

        Emits via ``emit_direct_event`` with an EXPLICIT ``reyn_root=
        self._project_root / ".reyn"`` — never ``emit_cli_event`` (which
        derives the root from ``Path.cwd()``): that function's own
        docstring names exactly this process shape as the case it is
        wrong for ("a long-lived server process's cwd is not reliably its
        project root"), and ``AgentRegistry`` is exactly that — one
        instance, one resolved ``self._project_root``, living for the
        whole ``reyn web``/``reyn chat`` process. ``track_audit_seq``
        is left at its default (``True``): unlike a one-shot CLI
        diagnostic, this registry can emit this kind many times over its
        own lifetime (every agent restart, every session end) — a real
        series, per ``emit_direct_event``'s own docstring.

        Best-effort, mirroring ``process_registry.py``'s own
        ``process_marker_reaped`` emit (#5358): an audit-emit failure here
        must never propagate into asyncio's own done-callback machinery
        (an exception raised from a done-callback is itself reported via
        the SAME generic unhandled-exception path this method exists to
        give a more specific answer than) — logged, swallowed.

        #5714 (architect ruling, point ③): ALSO presses
        ``process_registry.record_session_ended`` here — not a second
        lifecycle mechanism, the ruling's own explicit instruction is
        that THIS callback is the one and only push site (verified by
        this PR's own structural test: ``record_session_ended(`` has
        exactly one production call site, here). #5714 reshaped the
        marker's own identity field into a collection of ``sessions``
        entries — once a process can host N Sessions, an entry with no
        way to mark itself ended would show a genuinely-finished session
        as "hosted" forever, the same class of lie #5714 as a whole
        exists to close, one level down. Fires unconditionally (every
        status — completed/exception/cancelled all mean the task
        ENDED), same as the audit-event emit above; best-effort,
        identical failure posture."""
        if task.cancelled():
            status, exception_type, exception_message = "cancelled", "", ""
        else:
            exc = task.exception()
            if exc is not None:
                status = "exception"
                exception_type = type(exc).__name__
                exception_message = str(exc)
            else:
                status, exception_type, exception_message = "completed", "", ""
        try:
            from reyn.core.events.events import emit_direct_event

            emit_direct_event(
                "session_run_task_finished",
                surface="registry",
                reyn_root=self._project_root / ".reyn",
                name=name,
                sid=sid,
                status=status,
                exception_type=exception_type,
                exception_message=exception_message,
            )
        except Exception:
            logger.warning(
                "registry: failed to emit session_run_task_finished for "
                "(%r, %r) (diagnostic-only, does not block anything)",
                name, sid, exc_info=True,
            )
        try:
            from reyn.runtime.process_registry import record_session_ended

            record_session_ended(agent_name=name, sid=sid)
        except Exception:
            logger.warning(
                "registry: failed to record session end in the process "
                "registry for (%r, %r) (diagnostic-only, does not block "
                "anything)", name, sid, exc_info=True,
            )

    def _ensure_session_run(self, name: str, sid: str, session: "object") -> asyncio.Task:
        """#5694 stage 2 disposition (architect ruling): the ONE place that
        decides "reuse the existing `(name, sid)` run-task, or spawn a
        fresh one" — folds the `if key not in self._tasks or self._tasks
        [key].done(): self._tasks[key] = self._spawn_session_run(...)`
        pattern this file's 4 call sites (`ensure_running`,
        `ensure_session_running`, `attach`, `attach_session`) each
        duplicated, the direct continuation of #5715's own
        `_spawn_session_run` consolidation.

        Architect's own framing: the disposition for #5694 is NOT a new
        automatic restart policy — a request-driven restart (this exact
        guard) was ALREADY happening, silently. "新しい自動動作を足しません
        … 問いは『再起動するか』ではありません。もう しています。問いは
        『それが記録されているか』で、答えはされていません。" Verified
        before implementing (not merely inherited from the ruling's own
        structural argument): every production caller of the 4 methods
        this guard lives in (`ensure_running`/`ensure_session_running`/
        `attach`/`attach_session`) is itself triggered by a real request —
        an HTTP/SSE endpoint handling an inbound connection or submit, a
        CLI command's own startup, a cron/webhook ingress adapter firing
        on a real external event, an agent-to-agent message delivery
        (`wake=True` only), or a pipeline step's own settle-time delivery
        — never a bare periodic poll of registry state with no request
        behind it. This confirms (does not merely assume) the "the
        request side already IS the backstop, no separate crash-loop
        guard is needed" premise the ruling's own charter-Q1 answer rests
        on.

        This method emits ONE additional fact `_on_session_run_task_done`
        does not: not "a task finished" (already recorded there, at the
        moment it happened) but "THIS caller, right now, discovered a
        PRIOR task was already done and is replacing it" — the moment
        #5694's own incident showed gets silently consumed as a mere
        restart trigger, with no record anywhere that it happened.
        Fires ONLY when a task already existed for `key` AND it was
        done — never on a genuine first boot (`key` absent), which is
        not a rediscovery of anything.

        Emitted kind is named `session_run_task_rediscovered_dead`, not
        e.g. `session_run_task_restarted` — architect's own requirement:
        "発見の時刻は死亡時刻ではありません（上界です — process_marker_
        reaped と同じ性質）。『restarted』だけだと死亡時刻と読まれます."
        The event's own envelope timestamp is when THIS caller happened
        to notice, which can lag the task's real completion by an
        arbitrary amount (nothing polls for this — it is only ever
        discovered the next time some real request needs this
        `(name, sid)` again).

        Deliberately NOT a policy decision of any kind: no retry count,
        no backoff, no threshold, no push notification — the ruling's own
        explicit "落とすもの（全部）" list. The read side is `reyn doctor`
        and fleet visibility (reyn-broker#31 / #5709's own series), same
        as `session_run_task_finished` above.

        Self-contained idempotency (a still-running task is REUSED, never
        replaced): every one of the 4 call sites already re-derives this
        same `not done()` check as its own outer guard before calling
        here, so a caller never reaches this method with a live task in
        practice — but the method does not lean on that; it re-checks
        and returns the existing task rather than trusting the caller,
        the same "don't trust the caller's own guard, still be correct
        standalone" property #5709 R5's own idempotent `arm_process_
        loop_beat` has. Caught directly by this file's own test suite,
        driving this method past its outer guards: the first draft
        unconditionally spawned a fresh task even when `existing` was
        still alive, quietly doubling the run-loop for one `(name, sid)`
        — fixed to the current early-return shape before this PR
        landed."""
        key = (name, sid)
        existing = self._tasks.get(key)
        if existing is not None:
            if not existing.done():
                return existing
            self._emit_session_run_task_rediscovered_dead(name, sid)
        task = self._spawn_session_run(name, sid, session.run())
        self._tasks[key] = task
        return task

    def _emit_session_run_task_rediscovered_dead(self, name: str, sid: str) -> None:
        """The one emit site for `_ensure_session_run`'s own rediscovery
        fact — split out from that method for the same reason
        `_on_session_run_task_done` is its own method: an audit-emit
        failure here must never block the real restart it is only ever
        a diagnostic record of. Best-effort, mirroring that method's own
        try/except shape."""
        try:
            from reyn.core.events.events import emit_direct_event

            emit_direct_event(
                "session_run_task_rediscovered_dead",
                surface="registry",
                reyn_root=self._project_root / ".reyn",
                name=name,
                sid=sid,
            )
        except Exception:
            logger.warning(
                "registry: failed to emit session_run_task_rediscovered_dead "
                "for (%r, %r) (diagnostic-only, does not block the restart)",
                name, sid, exc_info=True,
            )

    def running_tasks(self) -> list[asyncio.Task]:
        """All non-completed tasks (session.run + forwarders) for shutdown drain."""
        out: list[asyncio.Task] = []
        for table in (self._tasks, self._forward_tasks):
            out.extend(t for t in table.values() if not t.done())
        return out

    def is_session_running(self, name: str, sid: str) -> bool:
        """Whether ``(name, sid)`` has a LIVE ``session.run()`` background task —
        the same predicate ``ensure_running``/``attach``/``ensure_session_running``
        already each re-derive inline (``key not in self._tasks or
        self._tasks[key].done()`` before creating a new task) to avoid double-
        arming, exposed here as a public read for a caller that instead needs to
        avoid double-*driving*.

        proposal 0067 P4d (#3978), architect ruling 2026-08-10: ``run_prompt
        (collect="attached")`` drives its target inline via ``MessageBus.request``
        — safe ONLY when nothing else is concurrently pumping the same Session's
        ``run_one_iteration`` (reyn's own invariant, ``a2a.py``'s routers state it
        explicitly: "a session is EITHER self-running OR inline-driven, never
        both"). A target this returns ``True`` for MUST be refused with a named
        error, not raced against — the architect-ruled fix is refusal, not a new
        lock (the production ``get_agent_lock`` acquire path does not cover a
        session's own run-loop, so adding one here would not actually serialize
        the two pumps; see issue #4113's measurement of the sibling MCP hazard).

        ⚠️ **This predicate covers only HALF the invariant, and the name invites
        the other half to be assumed.** It answers "is `(name, sid)` self-running
        its own background loop" — it does NOT answer "is anyone driving this
        session right now": a session someone ELSE is currently pumping INLINE
        (e.g. a concurrent ``run_prompt`` call, or `mcp.server._get_session`'s
        no-run-loop path) has no `self._tasks` entry at all, so this returns
        ``False`` for it — a FALSE "nobody is driving this" reading. It is
        therefore not a witness that a target is safe to drive; it is only a
        witness that ONE particular way of being unsafe (self-running) is
        absent. Issue #4113 is the registry-owned "who is driving this session
        right now" marker that would cover BOTH axes — this method is the
        interim, self-running-only half, and callers that need the full
        invariant (this one included) must not treat it as more than that."""
        key = (name, sid)
        return key in self._tasks and not self._tasks[key].done()

    async def shutdown(self) -> None:
        """Best-effort: stop all loaded sessions, then await/cancel their tasks.

        Cooperative first: each session.run loop notices the shutdown sentinel
        (agent.shutdown) at its next turn boundary; a short grace window lets a
        non-stuck session drain that way (the common idle / fast-turn /quit is
        unaffected — the sentinel is processed well within the grace). Any run
        task still alive after the grace is *stuck* — e.g. blocked mid-LLM-call on
        a slow/hung provider that never reaches the boundary to see the sentinel —
        so it is hard-cancelled. The CancelledError lands on the `acompletion`
        await (a safe cancel point: completed turns already wrote their WAL /
        history inline, and the cancelled turn simply didn't complete — no partial
        write), so shutdown always returns instead of hanging on /quit.
        """
        for name, agent in self._iter_named_sessions():
            try:
                await agent.shutdown()
            except Exception as exc:
                logger.warning("agent shutdown failed for %r: %s", name, exc)
        # Cancel forwarders so they don't block on a queue that won't refill.
        for t in self._forward_tasks.values():
            if not t.done():
                t.cancel()
        tasks = self.running_tasks()
        if tasks:
            # Cooperative grace, then hard-cancel any straggler (cancelled forwarders
            # finish immediately; a stuck session.run lands in `pending`).
            _done, pending = await asyncio.wait(tasks, timeout=_SHUTDOWN_GRACE_S)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # #4759: drain every loaded session's own background-task funnel
        # (Session.aclose_background_tasks -> TrackedTaskSet.aclose, see
        # tracked_tasks.py) — covers the ephemeral auto-vanish task
        # (SpawnTracker._vanish_task, itself the #4759 root cause: it closes
        # this session's held MCP connections via remove_session, but was
        # previously a bare detached asyncio.create_task with no strong ref
        # anywhere this drain could see, so a normal shutdown could return
        # while it was still mid-flight, orphaning the OS subprocess it was
        # about to close), plus chain-timeout watchdogs, fire-and-forget
        # WAL-append tasks, the hook-bus bridge (session_api.py's
        # _hook_bus_bridge_task), OutboxHub's drain loop,
        # hooks/external_fire.py's own SEPARATE drain loop (a different
        # bridge from the hook-bus one above — both happen to say
        # "hook"/"drain" but are two distinct producers), and
        # restored-intervention watchers — this loop itself needs no
        # per-task-type knowledge, so THIS method never grows regardless of
        # how many producers exist.
        #
        # That is not quite the same as "a future producer is automatically
        # covered", though: SpawnTracker and OutboxHub made task_tracker a
        # REQUIRED constructor param (their fallback path was deleted, so
        # skipping the funnel there is now a TypeError, not a silent gap).
        # ChainManager and hooks/external_fire.py's bridge, by contrast,
        # still accept a caller that doesn't pass/expose one (ChainManager:
        # DELIBERATELY, optional param — see its own #4759 comment for the
        # measured cost/benefit; external_fire: a getattr(self._session,
        # "_background_tasks", None) guard against an arbitrary session-like
        # object, not a constructor default) — for THOSE two, a reviewer
        # adding a NEW watchdog/drain-task producer inside them still needs
        # to remember to route it through self._task_tracker/the getattr
        # result, same as any other new code. "Spawn through the funnel" is
        # covered by construction only where the funnel is a required
        # dependency; where it's optional, it is still a discipline, not a
        # guarantee.
        #
        # Run AFTER the run-loop tasks have drained/cancelled above (same
        # reasoning #2714's own MCP-close comment below gives: no in-flight
        # call should race this close) and BEFORE the MCP-connection sweep —
        # the vanish task's own remove_session() call does that session's
        # aclose_mcp_connections() as part of ITS work, so letting it run
        # first means the later MCP sweep below sees an already-clean state
        # for a session that just vanished (aclose_mcp_connections is
        # idempotent either way, so this ordering is a clarity choice, not a
        # correctness requirement).
        #
        # Time-bounded with the SAME grace window the task-drain above uses
        # (_SHUTDOWN_GRACE_S) — a background task that doesn't respond to
        # its own funnel's cancel within that window is logged and left
        # rather than hanging /quit indefinitely (testing.md's "no test/no
        # caller waits unboundedly" principle applied to a caller, not a
        # test: an unbounded await_quiescent-style drain here would trade
        # one orphan-risk for a hang-risk, a worse defect, not a fix).
        for _name, session in self._iter_named_sessions():
            aclose_bg = getattr(session, "aclose_background_tasks", None)
            if callable(aclose_bg):
                try:
                    await asyncio.wait_for(aclose_bg(), timeout=_SHUTDOWN_GRACE_S)
                except TimeoutError:
                    logger.warning(
                        "background-task teardown timed out for %r after %.1fs — "
                        "some background task(s) may still be running",
                        _name, _SHUTDOWN_GRACE_S,
                    )
                except Exception as exc:
                    logger.warning("background-task teardown failed for %r: %s", _name, exc)
        # #2714: close held MCP connections (Option C) for EVERY loaded session on the
        # NORMAL-exit path too — the REPL's /quit + Ctrl-C/EOF (interfaces/repl/repl.py)
        # route through here, but pre-#2714 only ``remove_session`` (spawned-session
        # drop) and ``archive_agent`` (DELETE) ran the MCP teardown, so the MAIN
        # interactive session's held stdio subprocesses were orphaned on every ordinary
        # exit (accumulating in Windows Task Manager — Unix reaps them, Windows does
        # not). Run AFTER the run-loop tasks have drained/cancelled above so no in-flight
        # MCP call races the close, and while this event loop is still alive (this async
        # method is awaited before the loop tears down — an async close needs a live
        # loop; MCPClient.close's synchronous belt-and-suspenders reap covers the case
        # where a teardown fault or loop-teardown cuts the graceful close short). A
        # no-op for ephemeral sessions (never populate the connection service) and
        # sessions built without one (getattr guard) — mirrors remove_session/
        # archive_agent's teardown seam.
        for _name, session in self._iter_named_sessions():
            aclose_mcp = getattr(session, "aclose_mcp_connections", None)
            if callable(aclose_mcp):
                try:
                    await aclose_mcp()
                except Exception as exc:
                    logger.warning("MCP connection teardown failed for %r: %s", _name, exc)
        # #2783: drain every loaded session's EventStore (per-session instance) so
        # trailing audit events (fire-and-forget via submit_nowait) survive a normal
        # exit instead of being silently dropped when asyncio.run cancels outstanding
        # tasks at loop teardown. Same getattr-guarded, same-loop-as-MCP pattern above.
        for _name, session in self._iter_named_sessions():
            aclose_event_store = getattr(session, "aclose_event_store", None)
            if callable(aclose_event_store):
                try:
                    await aclose_event_store()
                except Exception as exc:
                    logger.warning("EventStore teardown failed for %r: %s", _name, exc)
        # #2783: drain the registry-wide StateLog (WAL) — the same gap #1765 left open
        # for the exact same reason (fire-and-forget submit_nowait, cancelled at loop
        # teardown). One shared instance, so this runs once per shutdown() call, not
        # per session.
        if self._state_log is not None:
            try:
                await self._state_log.aclose()
            except Exception as exc:
                logger.warning("StateLog teardown failed: %s", exc)

    def loaded_names(self) -> list[str]:
        """#4995 slice 1 (architect correction): NOT owner-thread-asserted
        — a read, not a mutation. See :meth:`get_session`'s own docstring."""
        return list(self._sessions.keys())

    def session_tree(self) -> "list[dict]":
        """Snapshot of the agent→session tree for the status-bar agent menu.

        A read-only, freshly-built copy (no handle to live registry state):
        agents in name order, each with its sessions (sids, sorted) and
        which (agent, sid) is the current attach focus.

        #5094: iterates :meth:`list_active_names` (every DECLARED,
        non-archived agent — disk-backed, the SAME source :meth:`exists`
        reads) rather than :meth:`loaded_names` (only agents with a LIVE
        in-memory ``Session`` right now) — architect's own measured
        finding: a remote client's agent tab read this method via
        ``status.py``'s ``_snapshot`` and showed NOTHING for any agent not
        yet attached in THIS process, regardless of how many agents the
        workspace actually had declared (owner live-blocked on this,
        #5041/#5094 — the #5097 wire fix alone did not close it, because
        the wire faithfully carried an empty roster). A declared-but-
        unattached agent now appears with an empty ``sessions`` list — the
        #4996-family distinction between "not yet attached" and "does not
        exist" this repo's own vocabulary already names. ``loaded_names``
        itself is UNCHANGED and still answers its own, different question
        ("who is running right now") — this is not a replacement, it is
        splitting two questions this one call site used to conflate.
        """
        out: list[dict] = []
        active = self._connection.active
        for name in self.list_active_names():
            sids = sorted((self._sessions.get(name) or {}).keys())
            out.append({
                "agent": name,
                "attached": active is not None and active[0] == name,
                "sessions": [
                    {"sid": sid, "attached": active == (name, sid)}
                    for sid in sids
                ],
            })
        return out

    def iter_other_agents(self, self_name: str) -> list[dict]:
        """List `{name, role}` for every agent except `self_name`.

        Used by RouterLoop (via Session.list_available_agents) to populate
        the reachable agent list. `role` is the first non-empty line of
        each agent's profile.role; empty when the agent has no role.
        """
        out: list[dict] = []
        for name in self.list_names():
            if name == self_name:
                continue
            try:
                profile = self.load_profile(name)
            except Exception as exc:
                logger.warning("profile load failed for agent %r — excluded from routing: %s", name, exc)
                continue
            role_lines = (profile.role or "").strip().splitlines()
            role_excerpt = role_lines[0].strip() if role_lines else ""
            out.append({"name": name, "role": role_excerpt})
        return out

    def iter_reachable_agents(self, self_name: str) -> list[dict]:
        """Same as iter_other_agents, but filtered by topology rules.

        Agents the caller cannot reach (per `permit`) are dropped so the
        router LLM never proposes a delegation that would be blocked at
        send time.
        """
        return [
            entry for entry in self.iter_other_agents(self_name)
            if self.permit(self_name, entry["name"])
        ]

    # ── topology ────────────────────────────────────────────────────────────────

    @property
    def _topologies(self) -> dict[str, Topology]:
        """#2946 Item 4: lazy-loaded topology map — the blocking glob + per-file
        YAML parse (``_reload_topologies``) only runs on first access, not at
        Registry construction. ``_topologies_raw`` is ``None`` until then.
        Every reader/writer below goes through this property (mutating the
        returned dict in place, e.g. ``self._topologies[name] = topo``), so
        the lazy-load is transparent to all existing call sites."""
        if self._topologies_raw is None:
            self._reload_topologies()
        assert self._topologies_raw is not None
        return self._topologies_raw

    def _reload_topologies(self) -> None:
        topologies: dict[str, Topology] = {}
        if self._topology_dir.is_dir():
            for path in sorted(self._topology_dir.glob("*.yaml")):
                try:
                    topo = Topology.load(path)
                except Exception as e:
                    # Hand-edited / outdated yaml — surface but don't crash.
                    import sys
                    print(
                        f"warning: skipping malformed topology {path.name}: {e}",
                        file=sys.stderr,
                    )
                    continue
                topologies[topo.name] = topo
        self._topologies_raw = topologies

    def _affiliated_agents(self) -> set[str]:
        """Names of agents that belong to at least one user-declared topology."""
        s: set[str] = set()
        for t in self._topologies.values():
            s.update(t.members)
        return s

    def _default_topology(self) -> Topology:
        """Synthesize the auto-managed `_default` network topology.

        Members = every existing agent that is NOT a member of any
        user-declared topology. Computed on demand; not persisted.
        """
        affiliated = self._affiliated_agents()
        # #1954: archived agents don't actively participate — exclude them from
        # the auto-default network (a user-topology member keeps its membership
        # for rewind-recovery, but is skipped from active comm by can_send).
        members = tuple(n for n in self.list_active_names() if n not in affiliated)
        return Topology(
            name=_DEFAULT_TOPOLOGY_NAME,
            kind="network",
            members=members,
        )

    def list_topologies(self) -> list[Topology]:
        """Return all topologies including the auto-managed `_default`.

        Order: user-declared (sorted by name) first, then `_default` last
        so user-declared entries don't get visually buried under the auto
        one.
        """
        out = [self._topologies[k] for k in sorted(self._topologies)]
        out.append(self._default_topology())
        return out

    def get_topology(self, name: str) -> Topology:
        if name == _DEFAULT_TOPOLOGY_NAME:
            return self._default_topology()
        if name not in self._topologies:
            raise FileNotFoundError(f"topology {name!r} not found")
        return self._topologies[name]

    def topology_exists(self, name: str) -> bool:
        if name == _DEFAULT_TOPOLOGY_NAME:
            return True
        return name in self._topologies

    def topologies_for_agent(self, agent: str) -> list[Topology]:
        """All topologies the agent currently belongs to (including `_default`)."""
        return [t for t in self.list_topologies() if agent in t.members]

    def resolved_profile_for(
        self, agent: str, *, is_delegate: "bool | None" = None, sid: "str | None" = None
    ) -> "tuple[object | None, frozenset[str]]":
        """#1827 S3: the agent's effective contextual narrowing — the composition
        (most-restrictive: ∪ deny, ∩ allow, ∪ excluded) of every restrict-only layer:
        topology ``capability_profile`` bindings, the #2081 ``_delegate`` floor, and
        (#2103 S1a) the per-session config.

        Returns ``(ContextualPermission | None, excluded_categories)``.

        **No layer →** ``(None, frozenset())`` = byte-identical to pre-#1827.

        **#2081 `_delegate` floor:** when this is an UNBOUND-by-topology **delegate**
        load (``is_delegate``) and ``delegation.capability_default=deny``, the
        restrictive built-in ``_delegate`` floor is composed in — a topology binding
        REPLACES it (the binding is the re-grant). ``is_delegate=None`` (the factory's
        construction-time call) falls back to the ``_constructing_as_delegate``
        transient; an explicit value wins.

        **#2103 S1a per-session config:** when ``sid`` is given AND a per-session
        ``config.yaml`` exists (``.reyn/agents/<name>/state/sessions/<sid>/config.yaml``
        — the spawner-set, workspace-backed P5 narrowing), it composes in as an
        ADDITIONAL restrict-only ∩ conjunct — folded into the single ContextualLayer
        (no 4th EffectivePermission conjunct), so it can only narrow within the agent
        envelope, never re-grant (structural: one more conjunct in ``all(...)``).
        ``sid=None`` or no file → byte-identical (inert).

        **#2103 C2 (gate-6) fail-closed cap-walk:** a DECLARED topology binding whose
        profile file is ABSENT or MALFORMED is surfaced (stderr) and composes the
        restrictive ``_delegate`` floor — it FAILS CLOSED, not skips, so a deleted /
        corrupt narrowing cannot silently widen the member (delete-to-uncap). This is
        distinct from *no binding declared* (``profile_for`` → None), which correctly
        skips (present-but-unrestricted). Existence (file present vs absent) is the
        discriminator, mirroring the lineage #2161 fix. It never crashes construction.
        """
        from reyn.security.permissions.capability_profile import (
            compose_resolved,
            delegate_floor_origin,
            load_capability_profile,
            load_delegate_profile,
            resolve_profile,
        )
        from reyn.security.permissions.effective import NarrowingOrigin

        resolved: list = []
        for topo in self.topologies_for_agent(agent):
            name = topo.profile_for(agent)
            if not name:
                # No binding DECLARED for this member in this topology → present-but-
                # unrestricted, nothing to impose (the analog of #2161's present-but-
                # parent_ctx-None skip). Distinct from a DECLARED-but-unresolvable
                # binding below, which fails CLOSED.
                continue
            path = self._capability_profile_dir / f"{name}.yaml"
            if not path.is_file():
                # #2103 C2 (gate-6, generalising #2161): a binding IS declared (the
                # member is meant to be NARROWED by {name}) but its profile file is
                # ABSENT (purged / typo / archived-then-GC'd). FAIL CLOSED — compose the
                # restrictive _delegate floor, NOT skip. Skipping is the fail-OPEN
                # escalation: the declared narrowing silently vanishes → the member
                # resolves WIDER than intended (delete-the-profile-to-uncap-the-member).
                # Existence (file present vs absent) distinguishes this from the
                # no-binding-declared skip above. Mirror of the lineage #2161 fix.
                import sys
                print(
                    f"warning: capability_profile {name!r} (bound in topology "
                    f"{topo.name!r}) not found at {path} — failing closed (floor)",
                    file=sys.stderr,
                )
                resolved.append(resolve_profile(
                    load_delegate_profile(self._project_root),
                    origin=delegate_floor_origin(
                        f"topology {topo.name!r} binds this agent to capability_profile "
                        f"{name!r}, but that profile file is missing at {path} — the "
                        "declared narrowing cannot be resolved, so the restrictive "
                        "floor is applied instead of none (a purged profile must not "
                        "widen the agent)"
                    ),
                ))
                continue
            try:
                prof = load_capability_profile(path)
            except Exception as e:  # noqa: BLE001 — hand-edited yaml, surface not crash
                # #2103 C2 (gate-6): a declared binding whose file is PRESENT but
                # MALFORMED is likewise unresolvable → FAIL CLOSED (floor), not skip — a
                # corrupt narrowing must not silently widen the member. (It also must
                # not crash session construction, hence floor-and-continue not raise.)
                import sys
                print(
                    f"warning: malformed capability_profile {path.name}: {e} "
                    "— failing closed (floor)",
                    file=sys.stderr,
                )
                resolved.append(resolve_profile(
                    load_delegate_profile(self._project_root),
                    origin=delegate_floor_origin(
                        f"topology {topo.name!r} binds this agent to capability_profile "
                        f"{name!r}, but {path.name} could not be parsed — the declared "
                        "narrowing cannot be resolved, so the restrictive floor is "
                        "applied instead of none"
                    ),
                ))
                continue
            resolved.append(resolve_profile(prof, origin=NarrowingOrigin(
                label=(
                    f"the capability_profile {name!r} bound to this agent by topology "
                    f"{topo.name!r}"
                ),
                cause=(
                    "the topology declares this agent's capability surface, and this "
                    "capability is outside it"
                ),
                lifts_when=(
                    f"the operator edits {path} or rebinds the member in topology "
                    f"{topo.name!r}. This narrowing is durable — it does not lift on "
                    "its own"
                ),
            )))

        # #2081: an UNBOUND-by-topology delegate under delegation.capability_default=
        # deny gets the restrictive _delegate floor (a topology binding REPLACES it —
        # the binding is the re-grant). The delegate-ness propagates recursively (every
        # A2A request-path load passes is_delegate=True regardless of the spawner's own
        # status), so a re-granted coordinator's sub-delegate is STILL default-denied
        # (no laundering). Appended as a conjunct so a per-session narrowing composes
        # WITH it (not instead of it).
        if not resolved:
            effective_delegate = (
                self._constructing_as_delegate if is_delegate is None else is_delegate
            )
            if effective_delegate and self._delegation_capability_default == "deny":
                resolved.append(resolve_profile(
                    load_delegate_profile(self._project_root),
                    origin=delegate_floor_origin(
                        "this agent is an unbound delegate (it was spawned by another "
                        "agent's delegation and no topology binds its capabilities) "
                        "while reyn.yaml sets `delegation.capability_default: deny`"
                    ),
                ))

        # #2103 S1a: the per-session config is an ADDITIONAL restrict-only ∩ conjunct
        # (the spawner-set, workspace-backed narrowing) — composed into the single
        # ContextualLayer, never re-granting (structural). Inert when sid is None or
        # no config.yaml exists → byte-identical.
        if sid is not None:
            ps = self._load_per_session_capability_profile(agent, sid)
            if ps is not None:
                resolved.append(resolve_profile(ps, origin=NarrowingOrigin(
                    label="the per-session capability narrowing for this session",
                    cause=(
                        "the agent that started this session narrowed it at spawn "
                        "time; the narrowing is recorded in this session's own "
                        "config.yaml under .reyn/state"
                    ),
                    lifts_when=(
                        "a new session is started without the narrowing (it is "
                        "session-scoped and cannot be widened from inside)"
                    ),
                )))

        # #2103 B (agent-spawn, Decision A): cap a SPAWNED agent at ⊆ its PARENT, LIVE +
        # by construction. The parent's OWN resolved effective is composed as one more
        # restrict-only conjunct; compose_resolved is a lattice-meet (∩ allow, ∪ deny),
        # which is order-independent, so the child can never EXCEED the parent — even if
        # the child's assigned subset is mis-specified wider, or a topology re-grants
        # (the re-grant is bounded ONLY because this LIVE parent-conjunct caps it; a
        # stale snapshot could not — this is why Decision A, not a persisted ⊆, is
        # REQUIRED). Recursive: the parent's resolved already ∩'d ITS parent up to the
        # operator-authorized top (lineage is acyclic → terminates). The parent resolves
        # with its OWN delegate-status (is_delegate=None → its construction transient)
        # at the agent envelope (no sid). A forged/absent lineage simply isn't here
        # (OS-set), so it cannot widen.
        edge = self._spawn_lineage.get(agent)
        if edge is not None:
            parent, parent_seq = edge
            # #2103 C2b (#2166): the stored edge froze the parent's identity at spawn —
            # #5084: agent_directory_identity(parent), the parent AGENT DIRECTORY's own
            # (ino, st_birthtime), re-stat'd here. If the parent name was purged + REUSED, the
            # current identity differs → the edge is STALE (it points to a GONE
            # identity, not the live same-named agent). Treat exactly like an absent
            # parent: FAIL CLOSED. (``parent_seq is None`` only when the parent's
            # profile was itself absent at spawn time — a defensive fallback, not a
            # documented case since #5084 — the absent-vs-present existence-check
            # below governs that, Q2 — no false-positive.)
            stale = (
                parent_seq is not None
                and self.agent_directory_identity(parent) != parent_seq
            )
            if stale or not (self._dir / parent).is_dir():
                # #2161 (absent parent) + #2166 (name-reused → stale identity): the capping
                # parent's identity is gone, so ⊆-parent CANNOT be verified. FAIL CLOSED:
                # compose the restrictive _delegate floor (NOT skip — skipping is the
                # fail-open escalation: purge/reuse-the-parent-to-uncap-the-child). DISTINCT
                # from a PRESENT-but-unrestricted parent (parent_ctx is None in the else
                # branch → correctly skipped, no cap to impose). One seam covers every
                # cap-drop cause (purge, name-reuse, crash, fs-delete).
                resolved.append(resolve_profile(
                    load_delegate_profile(self._project_root),
                    origin=delegate_floor_origin(
                        f"this agent was spawned by {parent!r}, whose identity is no "
                        "longer present (purged, or the name was re-created as a "
                        "different agent). The ⊆-parent cap cannot be verified, so the "
                        "restrictive floor is applied rather than an unverified surface"
                    ),
                ))
            else:
                parent_ctx, parent_excl = self.resolved_profile_for(parent)
                if parent_ctx is not None:
                    resolved.append((parent_ctx, parent_excl))

        if not resolved:
            return None, frozenset()
        return compose_resolved(resolved)

    def per_session_narrowing(self, name: str, sid: "str | None" = None) -> "dict | None":
        """#3546: the per-session narrowing MAPPING persisted for ``(name, sid)`` —
        the inverse of what ``spawn_session_recorded`` writes, so a caller that
        spawns a child session under the SAME identity can hand it back as
        ``narrowing=`` and have the child be born inside the same envelope.

        Returns the ``config.yaml`` body minus the synthetic ``name`` key
        ``spawn_session_recorded`` stamps on it (``{"name": "_session_<sid>",
        **narrowing}``), so the round-trip is exact. ``None`` when the session has
        no narrowing at all — which is also what a caller passes for "nothing to
        inherit", so the inert case stays byte-identical.

        This is the RAW mapping, deliberately not the resolved
        ``ContextualPermission``: the value is re-persisted into the child's own
        ``config.yaml``, and resolution happens on the child's side through the
        normal ``resolved_profile_for`` path. The other layers of the envelope
        (topology ``capability_profile`` bindings, the #2081 ``_delegate`` floor)
        are keyed by the AGENT NAME, not the sid, so a same-identity child
        re-derives them without anything being passed — this accessor exists for
        the one layer that is sid-keyed.

        A malformed file yields ``None`` with a warning, matching
        ``_load_per_session_capability_profile``'s fail-open-and-surface handling
        of the SAME file: a parent whose own narrowing was skipped as unreadable
        must not hand a child a narrowing the parent itself is not under.

        ``sid=None`` means the implicit "main" session, the same normalisation
        ``session_nesting_depth`` applies (#3556: ``RouterHostAdapter.
        live_session_id`` is ``str | None``, and a main session's narrowing lives at
        the agent-level state dir — reading None as "nothing to inherit" instead
        would drop that layer for exactly the spawner that has no spawned sid).
        """
        import yaml
        path = self._session_state_dir(
            name, sid if sid is not None else _DEFAULT_SID,
        ) / "config.yaml"
        if not path.is_file():
            return None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — hand/LLM-written yaml, surface not crash
            import sys
            print(
                f"warning: skipping malformed per-session config {path} while "
                f"deriving a child session's inherited narrowing: {e}",
                file=sys.stderr,
            )
            return None
        if not isinstance(raw, dict):
            return None
        narrowing = {k: v for k, v in raw.items() if k != "name"}
        return narrowing or None

    def resolved_sandbox_for(self, name: str, sid: "str | None" = None) -> "dict | None":
        """#5352: this ``(name, sid)``'s effective sandbox-policy override — the
        sibling-key counterpart of ``base_dir`` (see ``_persist_session_narrowing``'s
        own docstring for that shape), NOT the ∩-composed ``resolved_profile_for``
        machinery: an agent's ``sandbox:`` declaration is a per-agent BASELINE (the
        same "each agent's own operator-authored value" shape ``allowed_mcp`` /
        ``base_dir`` already use), not a restrict-only term composed across
        topology / delegate-floor / session layers.

        Session-layer (``sid``'s own ``config.yaml`` ``sandbox:`` key — the value a
        spawn resolved via #5352's priority table and persisted) wins over the
        agent-layer (``name``'s own ``profile.yaml`` ``sandbox:`` declaration) wins
        over ``None`` (no override at either layer — ``Agent.sandbox_config``, the
        process-wide default, governs unchanged).

        A malformed session config.yaml is surfaced (stderr) and skipped — falls
        through to the agent layer, restrict-only-safe (can only WIDEN toward that
        layer, never past the effective floor), same posture every other reader of
        this file already takes."""
        if sid is not None:
            path = self._session_state_dir(name, sid) / "config.yaml"
            if path.is_file():
                import yaml
                try:
                    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001 — hand/LLM-written yaml, surface not crash
                    import sys
                    print(
                        f"warning: skipping malformed per-session config {path} "
                        f"while resolving the sandbox override: {e}",
                        file=sys.stderr,
                    )
                    raw = None
                if isinstance(raw, dict) and isinstance(raw.get("sandbox"), dict):
                    return dict(raw["sandbox"])
        try:
            return self.load_profile(name).sandbox
        except FileNotFoundError:
            return None

    def _load_per_session_capability_profile(
        self, name: str, sid: str
    ) -> "object | None":
        """#2103 S1a: load the per-session capability narrowing for ``(name, sid)`` —
        ``<session-state-dir>/config.yaml`` (a capability_profile YAML, sibling of the
        per-session snapshot.json; workspace-backed P5). ``None`` when absent. A
        malformed file is surfaced (stderr) and skipped — a typo must not crash
        session construction, and (restrict-only) skipping it only WIDENS toward the
        agent envelope, never past it."""
        from reyn.security.permissions.capability_profile import load_capability_profile
        path = self._session_state_dir(name, sid) / "config.yaml"
        if not path.is_file():
            return None
        try:
            return load_capability_profile(path)
        except Exception as e:  # noqa: BLE001 — hand/LLM-written yaml, surface not crash
            import sys
            print(
                f"warning: skipping malformed per-session config {path}: {e}",
                file=sys.stderr,
            )
            return None

    def permit(self, from_agent: str, to_agent: str) -> bool:
        """Return True iff some shared topology permits from→to.

        PR13: there is no permissive fallback. The auto `_default` network
        topology covers agents that haven't been placed in any user
        topology, so the empty-topology bootstrap state still permits free
        communication. Once an agent is placed in a user topology it
        leaves `_default` and only the user topology's rule applies.
        """
        if from_agent == to_agent:
            return False
        # #1954: a soft-deleted (archived) agent does not actively participate,
        # even though its topology membership is preserved for rewind-recovery.
        if self.is_archived(from_agent) or self.is_archived(to_agent):
            return False
        candidates = list(self._topologies.values())
        candidates.append(self._default_topology())
        shared = [
            t for t in candidates
            if from_agent in t.members and to_agent in t.members
        ]
        if not shared:
            return False
        return any(t.can_send(from_agent, to_agent) for t in shared)

    def add_topology(self, topo: Topology) -> None:
        if topo.name == _DEFAULT_TOPOLOGY_NAME:
            raise ValueError(
                f"topology {_DEFAULT_TOPOLOGY_NAME!r} is auto-managed; cannot create"
            )
        _validate_topology_name(topo.name)
        if topo.name in self._topologies:
            raise FileExistsError(f"topology {topo.name!r} already exists")
        for m in topo.members:
            if not self.exists(m):
                raise ValueError(f"topology {topo.name!r}: agent {m!r} does not exist")
        topo.save(self._topology_dir / f"{topo.name}.yaml")
        self._topologies[topo.name] = topo

    def remove_topology(self, name: str) -> None:
        if name == _DEFAULT_TOPOLOGY_NAME:
            raise ValueError(
                f"topology {_DEFAULT_TOPOLOGY_NAME!r} is auto-managed; cannot remove"
            )
        if name not in self._topologies:
            raise FileNotFoundError(f"topology {name!r} not found")
        path = self._topology_dir / f"{name}.yaml"
        if path.is_file():
            path.unlink()
        del self._topologies[name]

    def add_member(self, topology_name: str, agent: str) -> Topology:
        if topology_name == _DEFAULT_TOPOLOGY_NAME:
            raise ValueError(
                f"topology {_DEFAULT_TOPOLOGY_NAME!r} is auto-managed; cannot mutate"
            )
        topo = self.get_topology(topology_name)
        if not self.exists(agent):
            raise ValueError(f"agent {agent!r} does not exist")
        new_topo = topo.with_member_added(agent)
        new_topo.save(self._topology_dir / f"{topology_name}.yaml")
        self._topologies[topology_name] = new_topo
        return new_topo

    def remove_member(self, topology_name: str, agent: str) -> Topology:
        if topology_name == _DEFAULT_TOPOLOGY_NAME:
            raise ValueError(
                f"topology {_DEFAULT_TOPOLOGY_NAME!r} is auto-managed; cannot mutate"
            )
        topo = self.get_topology(topology_name)
        new_topo = topo.with_member_removed(agent)
        new_topo.save(self._topology_dir / f"{topology_name}.yaml")
        self._topologies[topology_name] = new_topo
        return new_topo

    # ── #2103 Piece-2: topology-lifecycle EMIT seams (rewind durability) ──────
    # The create-side mirror of the agent-lifecycle seams (#2118). The sync
    # add_topology/add_member/remove_member/remove_topology above are the MECHANISM
    # (private internals); EVERY state-affecting topology mutation routes through a
    # logged seam below so rewind reconstructs the topology config-set as-of-cut.
    # MUST-1 invariant: a topology is fully-tracked or fully-untracked — a sync
    # mutation on a tracked topology would diverge on reconstruction.

    @staticmethod
    def _topology_payload(topo: Topology) -> dict:
        """#2103: serialise a Topology into a topology_created/updated WAL payload
        (the FULL config → as-of-cut reconstruction is latest-≤-cut-wins)."""
        return {
            "name": topo.name,
            "kind": topo.kind,
            "members": list(topo.members),
            "leader": topo.leader,
            "created_at": topo.created_at,
            "profiles": dict(topo.profiles),
        }

    async def _emit_topology(
        self, kind: str, name: str, topo: "Topology | None",
    ) -> None:
        """#2103: emit a topology-lifecycle WAL event — the ONE logged path every
        state-affecting topology mutator routes through. No-op without a WAL."""
        if self._state_log is None:
            return
        fields: dict = {"name": name}
        if topo is not None:
            fields["topology"] = self._topology_payload(topo)
        await self._state_log.append(kind, **fields)

    async def create_topology(self, topo: Topology) -> None:
        """#2103 logged CREATE seam: add_topology (sync) + emit topology_created.
        Every creation surface (LLM tool / web / CLI) routes through this."""
        self.add_topology(topo)
        await self._emit_topology("topology_created", topo.name, topo)

    async def add_topology_member(self, topology_name: str, agent: str) -> Topology:
        """#2103 logged UPDATE seam: add_member (sync) + emit topology_updated."""
        topo = self.add_member(topology_name, agent)
        await self._emit_topology("topology_updated", topo.name, topo)
        return topo

    async def remove_topology_member(self, topology_name: str, agent: str) -> Topology:
        """#2103 logged UPDATE seam: remove_member (sync) + emit topology_updated."""
        topo = self.remove_member(topology_name, agent)
        await self._emit_topology("topology_updated", topo.name, topo)
        return topo

    async def delete_topology(self, name: str) -> None:
        """#2103 logged DELETE seam: remove_topology (sync) + emit topology_removed."""
        self.remove_topology(name)
        await self._emit_topology("topology_removed", name, None)

    def _cascade_agent_removal(self, agent: str) -> "list[tuple[str, Topology | None]]":
        """Drop `agent` from every topology it's a member of.

        Team topologies losing their leader are removed entirely (a leader-less
        team is meaningless). Pipelines and networks shrink in place. Empty
        topologies are removed.

        #2103 MUST-1: returns the topology mutations so the (async) caller emits
        them through the logged path — else a tracked topology cascaded
        synchronously would diverge on reconstruction. (name, None) = removed;
        (name, new_topo) = updated.
        """
        changes: list[tuple[str, Topology | None]] = []
        for name in list(self._topologies.keys()):
            topo = self._topologies[name]
            if agent not in topo.members:
                continue
            if topo.kind == "team" and topo.leader == agent:
                self.remove_topology(name)
                changes.append((name, None))
                continue
            new_members = tuple(m for m in topo.members if m != agent)
            if not new_members:
                self.remove_topology(name)
                changes.append((name, None))
                continue
            # #2103: PRESERVE surviving members' capability_profile bindings — drop
            # ONLY the removed member's. Rebuilding without profiles wiped EVERY
            # binding, so purging one member silently changed a SURVIVOR's effective
            # capability (resolved_profile_for treats a missing binding as no-narrowing
            # = full ⊆-parent cap → a widen/escalation in the narrowing-binding case).
            # Dropping the removed member's entry also keeps Topology.__post_init__
            # happy (it rejects profiles bound to non-members) → reconstruction-safe.
            new_profiles = {
                m: p for m, p in topo.profiles.items() if m != agent
            }
            new_topo = Topology(
                name=topo.name,
                kind=topo.kind,
                members=new_members,
                leader=topo.leader,
                created_at=topo.created_at,
                profiles=new_profiles,
            )
            new_topo.save(self._topology_dir / f"{name}.yaml")
            self._topologies[name] = new_topo
            changes.append((name, new_topo))
        return changes


def _drain_queue(q: asyncio.Queue) -> None:
    """Best-effort drop of all currently-queued items. Non-blocking."""
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass


__all__ = ["AgentRegistry", "DEFAULT_AGENT_NAME"]
