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
  background (skills can keep progressing); only the REPL's display
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
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote
from uuid import uuid4

logger = logging.getLogger(__name__)

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.anchor_store import AnchorStore
from reyn.core.events.retention import RetentionPolicy, compute_retention_floor
from reyn.core.events.snapshot_generations import (
    REWIND_KIND,
    Branch,
    RewindBeyondRetentionError,
    RewindIntoAbandonedError,
    SnapshotGenerationStore,
    active_rewind_target,
    branch_ids_for,
    is_active_seq,
    lineage_predecessor,
    list_branches,
    reconstruct,
)
from reyn.core.events.snapshot_generations import checkout as _append_reset_record
from reyn.core.events.state_log import StateLog
from reyn.task.subscription import SubscriptionRegistry

from .profile import PROFILE_FILENAME, AgentProfile
from .topology import TOPOLOGY_DIRNAME, Topology, _validate_topology_name

DEFAULT_AGENT_NAME = "default"

# shutdown() grace window: how long to let session.run loops drain cooperatively
# (notice the shutdown sentinel at a turn boundary) before hard-cancelling any
# that are still stuck — e.g. blocked mid-LLM-call on a slow/hung provider, which
# never reaches the boundary to see the sentinel. Keeps /quit from hanging.
_SHUTDOWN_GRACE_S = 3.0
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


def _count_inflight_disposition(tasks: "list") -> "tuple[int, int]":
    """#2115: classify settled in-flight skill tasks → (cancelled, finished). A task
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
    """Map a WAL entry kind to a rewind-point boundary label (turn / plan-step / phase)."""
    if wal_kind == "skill_phase_advanced":
        return "phase"
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


class AgentRegistry:
    """In-process map of agent_name -> Session with persistence wired in.

    Owns the **REPL-facing outbox**: a single queue that consumers (e.g.
    `repl._output_loop`) read regardless of which agent is attached. A
    per-agent forwarder task pumps the agent's own `outbox` into this queue
    only while that agent is the attached one — detached agents drop
    transient outbox items, durable kinds (agent / skill_done) still
    persist to history via the agent's `_append_history` (handled at the
    Session layer, not here).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        session_factory: Callable[[AgentProfile], "object"],
        state_log: StateLog | None = None,
        retention_policy: RetentionPolicy | None = None,
        environment_backend: "object | None" = None,
        workspace_state_dir: "Path | None" = None,
        delegation_capability_default: str = "inherit",
        max_spawn_depth: int = 0,
        max_spawn_children: int = 0,
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
        # #2103 C3: operator spawn-tree bounds (safety.spawn.*), enforced at the LLM
        # spawn seams (host adapter). 0 = unlimited. Util/test callers default to
        # unlimited (byte-identical); the 5 frontend factory sites supply them via the
        # factory_config bundle.
        self._max_spawn_depth = max_spawn_depth
        self._max_spawn_children = max_spawn_children
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
        # composes with #2161's absent-parent existence-check). The identity is minted
        # in create_agent (the spawn seam) as an IN-MEMORY monotonic counter (#2259 PR-2b
        # owner (b) model: agent identity = in-memory id synced at spawn; the WAL seq is now
        # worker-assigned async + unavailable synchronously, so the counter IS the identity —
        # the worker links id↔seq in the durable agent_created record). (Q1: every create_agent
        # parent is identity-tracked → name-reuse always detected for real spawn lineages; a
        # bare-``create()`` non-spawn parent has no identity → None → #2161 existence
        # fallback, no false-positive, Q2).
        self._spawn_lineage: "dict[str, tuple[str, int | None]]" = {}
        # #2103 C2b: name → its CURRENT stable identity (the create_agent-minted token).
        # Rebuilt as-of-cut on rewind (_materialize_rewind). A name-reused agent has a
        # NEW token here, so a stored edge carrying the OLD token reads as stale.
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
        self._attached: "tuple[str, str] | None" = None              # (name, sid) focus
        # Focus-following front-end listeners (REPL/CUI): a chat-event callback
        # (working indicator) and an intervention listener channel (ask_user) that
        # must follow the attached session across agent switches. None until a
        # front-end binds them; wired to the attached session on bind and re-wired
        # on every attach so a `/attach <other>` doesn't strand them on the old
        # session. Generic session-level listeners (not skill-specific).
        self._focus_chat_listener: "Callable[..., None] | None" = None
        self._focus_intervention_channel: str | None = None
        # WAL truncation throttle (skill resume design). monotonic ts of last
        # successful truncation attempt; ``None`` means no throttle is active.
        self._last_truncation_ts: float | None = None
        # ADR-0038 Stage 1c-2: set for the duration of a global rewind. While
        # set, ``maybe_truncate_for_size`` no-ops so a compaction can't advance
        # the WAL keep-floor over the reset-record / reconstruct reads mid-cut.
        self._rewind_in_progress: bool = False
        # ADR-0038 Stage 1e (D5): retention window. None → live (current).
        self._retention_policy = retention_policy or RetentionPolicy()
        # #2187 S1: the Task backend is GLOBAL — ONE per process, registry-owned, built
        # lazily here. Reverts the #2180 per-AGENT / #2186 per-SESSION splits: a task is
        # first-class (task ⊥ agent/session, #2187 §2), so its store is a single global db
        # (``.reyn/state/tasks.db`` — same path the A2A/web server already uses), NOT
        # agent-keyed. ONE instance ⟹ ONE connection in this process: the #2180
        # single-connection-registry-owned-lazy pattern survives, just keyed globally
        # instead of per-agent (so the per-instance ``asyncio.Lock`` serialises every write
        # + the ``restore_to_seq`` file-swap is the only connection touching the file — the
        # #2125/#2180 cross-connection concerns stay dissolved by construction). The A2A/web
        # server (a separate process) opens its own connection to the same global db;
        # sqlite multi-process file-locking handles that. Process-lifetime (parity with
        # ``_state_log``, never closed mid-run); the per-agent
        # close-on-PURGE the #2180 split needed is gone — a global db outlives any one
        # agent's teardown. (S2 moves task STATE into the global WAL control-plane; this
        # stage is the data-plane relocation only — the #2128 rewind/generation MECHANISM
        # is unchanged, just single-backend.)
        self._task_backend: "object | None" = None
        # #2187 backend-master: the live Task SUBSCRIPTION registry (the Reyn-internal
        # task↔session binding — assignee + requester). WAL-derived (the SAME live-state
        # pattern the reverted (A) used for STATUS, now applied to the CORRECT target:
        # the binding is what Reyn owns + rewinds; task-STATE stays in the backend, the
        # external master). The #1560 post-append observer keeps it live; restore_all
        # rebuilds it by replay (recovery / rewind). Registered whenever a WAL exists.
        self._task_subscriptions = SubscriptionRegistry()
        if state_log is not None:
            state_log.register_post_append(self._on_wal_append_subscription)
        # #1547: per-checkpoint anchor text (rewind-timeline preview). One global
        # store keyed by WAL seq; lazily built. None when no WAL.
        self._anchor_store: AnchorStore | None = None
        # Single queue the REPL drains; registry routes each attached agent's
        # outbox into here.
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        # Ensure default exists so `reyn chat` (no name) works out of the box.
        if not (self._dir / DEFAULT_AGENT_NAME / PROFILE_FILENAME).is_file():
            AgentProfile.new(DEFAULT_AGENT_NAME, role="").save(
                self._dir / DEFAULT_AGENT_NAME
            )
        # PR12: topology declarations under `.reyn/topologies/<name>.yaml`.
        # Bad files become warnings rather than startup errors so a hand-edited
        # yaml doesn't lock the user out of `reyn chat`.
        self._topologies: dict[str, Topology] = {}
        self._reload_topologies()

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
        single-session lookup)."""
        return self._peek_session(name, sid)

    def session_ids(self, name: str) -> list[str]:
        """FP-0043 Stage 4a: the loaded session-ids for an agent (for `/session
        list`). Empty until the agent's default session loads; "main" + any
        spawned ids thereafter."""
        return list(self._sessions.get(name, {}).keys())

    def agent_cost_usd(self, name: str) -> float:
        """Total cost in USD across ALL sessions of agent ``name``.

        Single source of truth for per-agent cost aggregation — used by both
        the inline status bar and the run_repl exit summary so they never drift
        when sessions are spawned via /session new.
        """
        total = 0.0
        for sid in self.session_ids(name):
            sess = self.get_session(name, sid)
            if sess is not None:
                total += sess.total_cost_usd
        return total

    def agent_total_usage(self, name: str) -> "object":
        """Aggregate TokenUsage across ALL sessions of agent ``name``."""
        from reyn.llm.pricing import TokenUsage
        total: "TokenUsage" = TokenUsage()
        for sid in self.session_ids(name):
            sess = self.get_session(name, sid)
            if sess is not None:
                total += sess.total_usage
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
            self.spawn_session(agent_name, sid=sid)
        return self.get_session(agent_name, sid)

    def load_profile(self, name: str) -> AgentProfile:
        return AgentProfile.load(self._dir / name)

    def exists(self, name: str) -> bool:
        return (self._dir / name / PROFILE_FILENAME).is_file()

    def create(self, name: str, *, role: str = "") -> AgentProfile:
        _validate_agent_name(name)
        if self.exists(name):
            raise FileExistsError(f"agent {name!r} already exists")
        profile = AgentProfile.new(name, role=role)
        profile.save(self._dir / name)
        return profile

    def _record_spawn_lineage(self, child: str, parent: str) -> None:
        """#2103 B: OS-set the spawn lineage ``child → parent``, set-once + immutable.
        The lineage is the no-escalation linchpin (resolved_profile_for caps the child
        at ⊆ parent via it), so it must NOT be forgeable or mutable post-spawn: a
        re-set to a DIFFERENT parent is refused, and a self-link is rejected. Acyclic
        by construction — the parent pre-exists and the child is freshly created, so a
        child can never be an ancestor of its parent. Idempotent on the same parent
        (rewind-reconstruction may replay the same edge).

        #2103 C2b: the edge stores ``(parent_name, parent_identity)`` — the parent's
        identity FROZEN at spawn time (``_agent_create_seq.get(parent)``; None when the
        parent was not minted via create_agent, e.g. a bare-``create()`` operator agent).
        Immutability/cycle compare by NAME (the identity is metadata for staleness)."""
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
        self._spawn_lineage[child] = (parent, self._agent_create_seq.get(parent))

    def is_spawn_descendant(self, agent: str, ancestor: str) -> bool:
        """#2103 C1: True iff ``agent`` is ``ancestor`` itself OR a transitive spawn-
        descendant of it (walk ``agent``'s lineage chain upward; acyclic → terminates).

        The subtree-membership predicate the ``topology_create`` spawn-seam uses to
        forge-guard which agents an LLM may wire into a topology: members must be ⊆ the
        creator's spawn subtree. That restriction is what makes C's profile bindings
        safe BY CONSTRUCTION — every LLM-bindable member is a lineage descendant of the
        creator, so ``resolved_profile_for``'s live parent-conjunct (B-core) backstops
        the binding (it can only narrow within the member's ⊆-creator envelope, never
        re-grant past it). An agent with no lineage edge (operator-top) is in no one's
        subtree but its own → an LLM cannot wire a non-descendant peer.

        #2103 C2b (#2166): a STALE edge — its parent name was purged + REUSED, so the
        frozen parent identity no longer matches the current ``_agent_create_seq[name]``
        — is a dangling link to a GONE identity, NOT a real ancestry. The walk stops at
        it (returns False), so a name-reused agent is rejected as a forged ancestor (the
        C1 forge-guard bypass tui found). A None identity (untracked parent) carries no
        staleness signal → the link is honoured (the #2161 existence-check governs that
        case at resolve time)."""
        if agent == ancestor:
            return True
        cursor: str = agent
        seen: "set[str]" = set()
        while True:
            edge = self._spawn_lineage.get(cursor)
            if edge is None:
                return False
            pname, pseq = edge
            if pseq is not None and self._agent_create_seq.get(pname) != pseq:
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
            if pseq is not None and self._agent_create_seq.get(pname) != pseq:
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
        pid = self._agent_create_seq.get(parent)
        n = 0
        for _child, (pname, pseq) in self._spawn_lineage.items():
            if pname == parent and (pseq is None or pseq == pid):
                n += 1
        return n

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

    async def create_agent(
        self, name: str, *, role: str = "", parent: "str | None" = None
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
        profile = self.create(name, role=role)
        if parent is not None:
            self._record_spawn_lineage(name, parent)
        # #2103 C2b: the parent's identity FROZEN at this spawn (the same value the edge
        # stored) — carried on agent_created so a rewind reconstructs the edge with the
        # parent-identity-AT-SPAWN (not the latest), so a rewind across a purge+name-reuse
        # does not resurrect this child under the reused parent.
        parent_seq = self._agent_create_seq.get(parent) if parent is not None else None
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
                    "allowed_skills": profile.allowed_skills,
                    "allowed_mcp": profile.allowed_mcp,
                },
            )
            self._record_agent_identity_generation(name)
        return profile

    def remove(self, name: str, *, purge: bool = False) -> "list[tuple[str, Topology | None]]":
        """Delete an agent. Default (#1954 Option A) = ARCHIVE (soft-delete): the
        runtime PITR generations are kept in place so rewind-to-before-delete
        works within the retention window, plus a tombstone recording the archival
        WAL seq (the slice-2 WAL-window GC hinge). ``purge=True`` is the guarded
        escape hatch — a real hard-delete (rmtree) that destroys the rewind
        history (time-travel-to-before-purge is intentionally unsupported)."""
        if name == DEFAULT_AGENT_NAME:
            raise ValueError("cannot remove the default agent")
        if self._attached is not None and self._attached[0] == name:
            raise ValueError(f"cannot remove attached agent {name!r}")
        target = self._dir / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        # Cancel any cached tasks / drop in-memory sessions (both paths).
        # FP-0043 Stage 3: removing an agent drops ALL its sessions (every sid).
        sids = list(self._sessions.get(name, {}).keys())
        for task_dict in (self._tasks, self._forward_tasks):
            for sid in sids:
                task = task_dict.pop((name, sid), None)
                if task and not task.done():
                    task.cancel()
        self._sessions.pop(name, None)
        self._identities.pop(name, None)
        if purge:
            # Explicit hard-delete — agents/<name>/ is reyn-managed. Destroys the
            # runtime PITR generations (rewind-to-before-purge is intentionally
            # unsupported); the real escape hatch for a genuine delete.
            import shutil
            # #2187 S1: the Task backend is GLOBAL (``.reyn/state/tasks.db``), NOT under
            # this agent dir, so the agent-dir rmtree no longer touches it — no per-agent
            # close needed (the #2180 close-before-rmtree is gone with the per-agent split).
            shutil.rmtree(target)
            # PR12: a hard-deleted agent would leave dangling topology references,
            # so drop it from every topology (a team losing its leader / an
            # emptied topology is removed entirely). #2103 MUST-1: return the
            # cascade's topology changes so the async caller emits them logged.
            return self._cascade_agent_removal(name)
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
        return []  # archive does not cascade — topology membership preserved (#1954)

    async def archive_agent(self, name: str, *, purge: bool = False) -> None:
        """#2103 S2b: the action-layer DELETE seam — archive (or purge) the agent
        (sync ``remove``) + emit the lifecycle event (``agent_archived`` |
        ``agent_purged``) so rewind reconstructs the as-of-cut archived-state and
        honors the permanent purge (fork A). The ONE delete seam the action-layer
        callers (CLI / web + the spawn op) route through. Emit no-ops without a WAL."""
        cascade_changes = self.remove(name, purge=purge)
        if self._state_log is not None:
            await self._state_log.append(
                "agent_purged" if purge else "agent_archived",
                entity_kind="agent", name=name,
            )
            # #2103 MUST-1: emit the purge cascade's topology changes through the
            # logged seam so rewind reconstructs the topology config-set consistently.
            for tname, topo in cascade_changes:
                await self._emit_topology(
                    "topology_removed" if topo is None else "topology_updated",
                    tname, topo,
                )

    def recent_user_message(self, name: str) -> str:
        """Return the most recent user-role text from the agent's history, or "".

        Reads history.jsonl synchronously (read-only). Returns "" on any
        failure or when no user message exists. Used by the right-panel
        Agents tab to surface idle-state context.
        """
        history_path = self._dir / name / "history.jsonl"
        if not history_path.is_file():
            return ""
        last_text: str = ""
        try:
            with history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict) and entry.get("role") == "user":
                        last_text = str(entry.get("text", ""))
        except OSError:
            return ""
        return last_text

    def message_count(self, name: str) -> int:
        """Return the total number of conversation messages in history, or 0."""
        history_path = self._dir / name / "history.jsonl"
        if not history_path.is_file():
            return 0
        count = 0
        try:
            with history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict) and entry.get("role") in ("user", "agent"):
                        count += 1
        except OSError:
            return 0
        return count

    def last_activity_at(self, name: str) -> datetime | None:
        """Last mtime across history.jsonl and any chat events file.

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

    async def restore_all(self) -> dict[str, AgentSnapshot]:
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
        """
        if self._state_log is None:
            return {}

        # 0. crash-mid-rewind recovery (no-op without an active reset-record).
        await self.recover_rewind_if_needed()

        # #2187 backend-master: rebuild the live Task SUBSCRIPTION registry (the
        # Reyn-internal task↔session binding) by replaying the WAL. ``is_active_seq``
        # skips abandoned rewind-branch segments (the SAME active-branch predicate the
        # workspace/runtime restore honours), so a restart after a rewind reconstructs
        # the active branch's bindings — a prior rewind's undone (re)binding is not
        # resurrected. The backend's task-STATE is the current external truth (re-read,
        # not rewound) — only Reyn's own subscription is replayed.
        self._task_subscriptions.replay(
            self._state_log.iter_from(0),
            is_active=lambda s: is_active_seq(self._state_log, s),
        )
        # #2187 Stage 4: the recovery RE-READ seam — the binding is restored above; now
        # re-read the CURRENT backend task-state (the external master, not rewound).
        await self._reconcile_subscriptions_after_recovery()

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

        # 2-3. WAL replay from min(applied_seq) + 1. Each snapshot's
        # _matches_agent now filters by (agent, session_id), so a shared WAL tail
        # routes every entry to exactly its (name, sid) snapshot.
        min_seq = min(s.applied_seq for s in all_snaps.values())
        wal_entries = list(self._state_log.iter_from(min_seq + 1))
        for snap in all_snaps.values():
            snap.apply_events(wal_entries)

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
        # #2187 5d: a session with a recovery-ACTIONABLE subscription MUST be instantiated
        # even with an empty snapshot — else a delegate that consumed its inbox then
        # crashed (RUNNING task, empty inbox) would never be re-woken (finding 3). Bounded:
        # only §3.6-actionable tasks (an awaited>0 idle parent is NOT instantiated).
        recovery_work = await self._compute_recovery_work()
        for (name, sid), snap in all_snaps.items():
            if (not snap.inbox
                    and not snap.pending_chains
                    and not snap.outstanding_interventions
                    and sid not in recovery_work):
                continue
            if sid == _DEFAULT_SID:
                session = self.get_or_load(name)
                session.restore_state(snap)
                await self.ensure_running(name)
            else:
                if not self._has_session(name, sid):
                    self.spawn_session(name, sid=sid)
                session = self._peek_session(name, sid)
                if session is not None:
                    session.restore_state(snap)

        # #2187 §3.6 (5d): the RE-DELIVERY half — now that the actionable sessions are
        # live, re-publish their missed events through each session's OWN production waker.
        await self._redeliver_recovery_wakes(recovery_work)
        return snapshots

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

    async def checkout(self, seq: int) -> dict:
        """Global consistent-cut checkout to ANY WAL ``seq`` (ADR-0038 D8 Phase-2).

        The unified time-travel primitive: jump the whole world's active cut to
        ``seq`` — whether ``seq`` is on the live branch (= undo, the ``rewind_to``
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

        Architecture-enforced global cut (D2): one global single-seq WAL ⇒ one
        reset-record moves *every* agent atomically:

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

        ``_rewind_in_progress`` gates compaction for the whole window.
        """
        if self._state_log is None:
            raise RuntimeError("checkout requires a state log")
        # 1e (D5): bounded by retention — reject targets truncated out of the WAL.
        # Guard on the PHYSICAL oldest kept seq (not the policy floor): under a
        # live policy nothing is truncated between turns, so recent history stays
        # reachable; only genuinely-truncated history is rejected.
        oldest = next(iter(self._state_log.iter_from(1)), None)
        oldest_seq = oldest.get("seq") if oldest else None
        if oldest_seq is not None and seq < oldest_seq:
            raise RewindBeyondRetentionError(
                f"checkpoint seq {seq} is outside the retained WAL (oldest "
                f"kept = {oldest_seq}) — it has been truncated. Configure a deeper "
                "retention window to reach this far back."
            )

        self._rewind_in_progress = True
        try:
            sessions = self._iter_sessions()
            # #2115: snapshot the in-flight skill tasks BEFORE the cancel, so the
            # summary reports their TRUE disposition (cancelled vs
            # finished-before-the-cancel-landed) instead of a hardcoded "cancelled"
            # literal — a skill that already returned wins the cancel race.
            inflight_tasks = [t for s in sessions for t in s.running_skills.values()]
            # 2. all-cancel (stop-world).
            for session in sessions:
                await session.cancel_inflight()
            # 3. all-quiesce (re-drain to a fixpoint — no append lands past the reset).
            for session in sessions:
                await session.await_quiescent()
            # 4. single global reset-record; supersedes = prior active head (audit).
            prior_head = self._state_log.last_durable_seq
            reset_seq = await _append_reset_record(
                self._state_log, target_seq=seq, supersedes=prior_head,
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
        """
        if self._state_log is None:
            raise RuntimeError("rewind_to requires a state log")
        if not is_active_seq(self._state_log, target_n):
            raise RewindIntoAbandonedError(
                f"rewind target seq {target_n} is on an abandoned branch — "
                "Phase-1 undo only rewinds to a seq on the active timeline "
                "(use checkout for a Phase-2 branch-switch)."
            )
        return await self.checkout(target_n)

    def list_rewind_points(self, *, include_abandoned: bool = False) -> list[dict]:
        """Enumerate rewind targets for the time-travel UI (1f / Phase-2 fork).

        Returns one row per snapshot-generation boundary, ascending by seq::

            [{"seq": int, "ts": str, "kind": str, "anchor": str, "branch_id": int}, ...]

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

          - ``skill_phase_advanced``                      → ``phase``
          - ``step_completed`` / ``step_failed``          → ``plan-step``
          - anything else (``inbox_consume``, …)           → ``turn``

        Generations are per-agent but keyed by the single global WAL seq, so
        the union across known agents is the global rewind-point set. Abandoned
        (rewound-past) boundaries are filtered out via ``is_active_seq``.

        Empty when there is no WAL or no generations.
        """
        if self._state_log is None:
            return []

        # #2236: compute the WAL retention floor using the SAME source as
        # checkout() (lines 1044–1048) so the list and the checkout guard
        # agree by construction.  Points below this floor would always be
        # rejected by checkout — advertising them is misleading.
        oldest = next(iter(self._state_log.iter_from(1)), None)
        oldest_seq: int | None = oldest.get("seq") if oldest else None

        # Union of generation boundary seqs across every known agent. Default =
        # active branch only (1f); include_abandoned = all branches (Phase-2 tree).
        seqs: set[int] = set()
        for name in self.list_names():
            for s in self._store_for(name).seqs():
                if oldest_seq is not None and s < oldest_seq:
                    continue  # #2236: truncated out of WAL — not reachable
                if include_abandoned or is_active_seq(self._state_log, s):
                    seqs.add(s)
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
        branch_of = branch_ids_for(self._state_log, sorted(seqs))
        rows: list[dict] = []
        for s in sorted(seqs):
            entry = wal_at.get(s, {})
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
            })
        return rows

    def list_branches(self) -> "list[Branch]":
        """The derived branch tree for the fork UX (#1533 Phase-2 2a / D8).

        ``[Branch(branch_id, fork_point_seq, head_seq, parent_branch_id,
        is_active)]`` derived from the reset-record chain (no stored registry).
        Tree topology (nesting/active); per-branch checkpoint *membership* comes
        from ``list_rewind_points(include_abandoned=True)`` rows' ``branch_id``.
        Empty when there is no WAL.
        """
        if self._state_log is None:
            return []
        return list_branches(self._state_log)

    def predecessor_turn_checkpoint(self, seq: int) -> int | None:
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
        """
        if self._state_log is None:
            return None
        # Checkpoint seqs = generation boundaries across every known agent (all
        # branches — the lineage walk may cross to a parent/ancestor branch).
        cps: set[int] = set()
        for name in self.list_names():
            cps.update(self._store_for(name).seqs())
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
        return lineage_predecessor(self._state_log, turn_cps, seq)

    @property
    def task_subscriptions(self) -> SubscriptionRegistry:
        """#2187 backend-master: the live Task SUBSCRIPTION registry (the Reyn-internal
        task↔session binding). The op-layer gates requests against this (single-writer /
        role / abort-cascade); the backend is the external master of task-STATE."""
        return self._task_subscriptions

    async def _on_wal_append_subscription(self, kind: str, seq: int, fields: dict) -> None:
        """#2187 backend-master: the #1560 post-append observer that keeps the live
        SubscriptionRegistry current. Applies each durable subscription WAL append
        (non-subscription kinds are ignored by ``apply``)."""
        self._task_subscriptions.apply(kind, seq, fields)

    async def _reconcile_subscriptions_after_recovery(self) -> "list[str]":
        """#2187 recovery RE-READ + PRUNE (the backend-master recovery model: re-subscribe,
        then re-read the current external task-state). The subscription (the Reyn-internal
        binding) is already restored by the WAL replay in ``restore_all``; the backend is
        the external MASTER of task-STATE and is NOT rewound — so on recovery the binding
        may name a task whose backend state moved (or vanished) while Reyn was down.

        Stage 5d — the PRUNE half (runs here, BEFORE session instantiation, since it needs
        no live session): re-read each binding; a STALE one (the backend no longer holds
        the task — the master dropped it) is PRUNED from the live registry (live-only,
        self-healing — see ``SubscriptionRegistry.prune``). The RE-DELIVERY half (waking
        the re-subscribed session for its actionable tasks, §3.6) runs AFTER session
        instantiation, via each live session's own waker — ``_redeliver_recovery_wakes``.
        Returns the pruned (stale) task ids (for the log / tests)."""
        task_ids = self._task_subscriptions.task_ids()
        if not task_ids:
            return []
        backend = self.task_backend  # the durable external master (built lazily)
        stale = [tid for tid in task_ids if await backend.get(tid) is None]
        for tid in stale:
            self._task_subscriptions.prune(tid)
        if stale:
            logger.info(
                "#2187 recovery reconcile: pruned %d/%d stale subscription(s) (the "
                "external master no longer holds the task).", len(stale), len(task_ids))
        return stale

    async def _recovery_action(self, backend, task) -> "str | None":
        """#2187 §3.6 (5d): the 7-state recovery wake predicate → the ``publish_task_event``
        event to re-deliver to the task's owner on recovery, or ``None`` (no wake).

        - ``UNASSIGNED`` (no subscriber) / ``BLOCKED`` (DAG-driven, woken by the readiness
          promote, not the subscription) / terminal (``DONE``/``FAILED``/``ABORTED``) → None.
        - ``READY`` → ``ready`` (the assignee resumes execution).
        - ``RUNNING`` → ``recovery_resume`` iff ``N_awaited == 0`` (the awaited children
          settled while down → continue/complete) OR a child ``FAILED`` (recover); else
          None (still blocked on running awaited children → idle, no busy-loop)."""
        from reyn.runtime.services.task_wake import (  # noqa: PLC0415
            TASK_EVENT_READY,
            TASK_EVENT_RECOVERY_RESUME,
        )
        from reyn.task import TaskState  # noqa: PLC0415

        if task.status is TaskState.READY:
            return TASK_EVENT_READY
        if task.status is TaskState.RUNNING:
            counts = await backend.open_child_counts(task.task_id)
            if counts.awaited == 0:
                return TASK_EVENT_RECOVERY_RESUME
            children = await backend.children_of(task.task_id)
            if any(c.status is TaskState.FAILED for c in children):
                return TASK_EVENT_RECOVERY_RESUME
        return None

    async def _compute_recovery_work(self) -> "dict[str, list]":
        """#2187 5d: group the recovery-ACTIONABLE subscriptions by assignee sid
        ``{sid → [(task, event), ...]}`` from the current backend state. Drives BOTH the
        step-5 instantiate widening (a session with an actionable task MUST be
        instantiated to be re-woken — finding 3: else a delegate that consumed its inbox
        then crashed never resumes = "org builds but doesn't run" recurs after crash) AND
        the re-delivery. Stale bindings are already pruned; an UNASSIGNED binding has no
        owner."""
        subs = self._task_subscriptions
        backend = self.task_backend
        out: "dict[str, list]" = {}
        for tid in subs.task_ids():
            assignee = subs.assignee_of(tid)
            if assignee is None:
                continue
            task = await backend.get(tid)
            if task is None:
                continue
            action = await self._recovery_action(backend, task)
            if action is not None:
                out.setdefault(assignee, []).append((task, action))
        return out

    async def _redeliver_recovery_wakes(self, recovery_work: dict) -> None:
        """#2187 §3.6 (5d) re-delivery — AFTER session instantiation, via each LIVE
        session's OWN ``task_waker`` (the production per-agent waker → delivery-equivalent
        BY CONSTRUCTION; the session holds its agent_name, so no separate resolution, no
        divergent path). Session-driven: for each live session, re-publish its actionable
        tasks' events through the SAME ``publish_task_event`` seam production uses."""
        if not recovery_work:
            return
        for _name, session in self._iter_named_sessions():
            work = recovery_work.get(getattr(session, "_session_id", None))
            if not work:
                continue
            waker = getattr(session, "_task_waker", None)
            if waker is None:
                continue
            for task, event in work:
                await waker.publish_task_event(event, task)

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
        # #2187 S1: the Task backend is GLOBAL (not under this agent dir), so the rmtree
        # no longer touches it — the #2180 per-agent close-before-rmtree is gone.
        try:
            shutil.rmtree(self._dir / name)
        except FileNotFoundError:
            pass  # already gone — fine
        except OSError as e:  # noqa: BLE001 — best-effort; never raise into rewind
            logger.warning("#2103: drop of agent %r failed (left on disk): %s", name, e)

    def _agent_lifecycle(
        self,
    ) -> "tuple[dict[str, tuple[int, dict, str | None, int | None]], dict[str, int], set[str]]":
        """#2103 S2: one WAL scan → the agent-lifecycle state (created, archived,
        purged):
        - created: name → (create_seq, profile-payload, parent, parent_seq) from
          ``agent_created`` (the payload re-materialises the profile on a
          forward-checkout-past-drop; ``parent`` (#2103 B) rebuilds the spawn lineage
          as-of-cut so a re-materialised child regains its ⊆-parent cap — else
          escalation-on-rewind; ``parent_seq`` (#2103 C2b) is the parent's identity
          AT-SPAWN, so the rebuilt edge reads STALE if the parent name was later
          purged+reused → no resurrection of the child under the reused parent).
        - archived: name → latest ``agent_archived`` seq (the as-of-cut hide hinge).
        - purged: names with an ``agent_purged`` event (fork A: permanent — never
          re-materialised at any cut).
        Empty without a WAL. Inert until S2b emits the events."""
        created: dict[str, tuple[int, dict, "str | None", "int | None"]] = {}
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
                created[name] = (
                    seq,
                    payload if isinstance(payload, dict) else {},
                    _parent if isinstance(_parent, str) else None,
                    _parent_seq if isinstance(_parent_seq, int) else None,
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
            allowed_skills=profile_payload.get("allowed_skills"),
            allowed_mcp=profile_payload.get("allowed_mcp"),
        )
        prof.save(self._dir / name)

    def _reconcile_archived_as_of_cut(
        self, archived: "dict[str, int]", cut: int,
    ) -> None:
        """#2103 S2: rewrite each present agent's ``.archived`` tombstone to the
        as-of-cut archived-state — archived iff its latest ``agent_archived`` seq ≤
        cut. So rewind-before-archive → active (marker cleared); rewind-after →
        archived (marker present). Inert when no ``agent_archived`` events exist
        (the #1954 file-only tombstone is left untouched)."""
        for name, aseq in archived.items():
            target = self._dir / name
            if not target.is_dir():
                continue
            marker = target / ARCHIVED_MARKER
            if aseq <= cut:
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
        lifecycle WAL (WAL-sourced only — never the rotated audit log). Per WAL-tracked
        topology name, the LATEST event with seq ≤ cut decides: created/updated → exists
        with that FULL config; removed (or no event ≤ cut, i.e. created-after-cut) →
        gone. Reconcile both the on-disk YAML and the in-memory ``_topologies`` map.
        MUST-2: ONLY WAL-tracked names are touched — untracked/pre-WAL topologies are
        never created, mutated, or deleted here. Inert without lifecycle events."""
        for name, evs in self._topology_lifecycle().items():
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
    ) -> "dict[str, tuple[int, str | None, int | None]]":
        """#2259 PR-1b: per-agent identity + frozen lineage as-of-cut from the truncation-
        surviving generations — the latest generation ≤ cut per agent. Returns
        ``{name: (create_seq, spawn_parent, spawn_parent_seq)}``. The rewind rebuild prefers
        this (survives truncation) over the `agent_created` WAL scan."""
        store = self._agent_identity_generation_store()
        out: dict[str, tuple[int, str | None, int | None]] = {}
        for name in store.names():
            latest = store.latest_at_or_below(name, cut)
            if latest is None:
                continue  # first generation after the cut → didn't exist as-of-cut
            _seq, data = latest
            out[name] = (
                int(data.get("create_seq", _seq)),
                data.get("spawn_parent"),
                data.get("spawn_parent_seq"),
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
        LATEST generation with seq ≤ cut (each generation is complete — no forward-replay). A
        registry whose first generation is AFTER the cut didn't exist as-of-cut → removed.
        Only generation-tracked registries are touched (operator-owned / pre-feature yaml with
        no generation is left alone). This survives WAL truncation — the bug the former
        event-replay reconstruct had (config_changed below the floor was lost)."""
        import yaml  # noqa: PLC0415 — local, matching the file convention

        store = self._config_generation_store()
        for rel_path in store.paths():
            latest = store.latest_at_or_below(rel_path, cut)
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
        (in-flight task writes settle before teardown); #2180: the Task backend is NOT
        closed on a session drop — it is agent-shared (one connection per agent), closed
        only on agent teardown, so the surviving sessions keep using it."""
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

        #2187 S1 (supersedes the #2125/#2180 per-session/per-agent close): the Task backend
        is GLOBAL — ONE process-wide ``sqlite3`` connection (registry-owned via the
        ``task_backend`` property), not under any agent/session dir. So this seam must NOT
        close it (it is shared process-wide + outlives this session). It only QUIESCES
        (``cancel_inflight`` + ``await_quiescent``, mirroring the global-rewind stop-world —
        idempotent when rewind_to already quiesced; REQUIRED for the ephemeral caller, which
        has no rewind orchestration) so any in-flight ``BEGIN IMMEDIATE`` settles before
        teardown. The Task backend is the EXTERNAL MASTER of task-state (#2187) and is NOT
        rewound by time-travel.

        Full teardown (rmtree) is correct: the global WAL is the durable source — the
        ``session_spawned`` create-record + the session's session_id-routed entries
        survive (the per-session dir is the snapshot/generations CACHE), so a
        forward-checkout re-materialises from the WAL, not the dir. The MAIN session
        (``_DEFAULT_SID``) is the agent's primary and is NOT removable here (its
        lifecycle is ``registry.remove``). A no-op for an unknown ``(name, sid)``."""
        if sid == _DEFAULT_SID:
            raise ValueError("cannot remove the main session via remove_session")
        removed = False
        # #2125: quiesce the session BEFORE teardown so any in-flight task write
        # completes ahead of teardown.
        # #2187 S1: do NOT close the Task backend here — it is GLOBAL (one process-wide
        # connection, registry-owned via the ``task_backend`` property), shared across all
        # sessions + process-lifetime, so a per-session/per-agent teardown never closes it.
        session = self._peek_session(name, sid)
        if session is not None:
            cancel_inflight = getattr(session, "cancel_inflight", None)
            if callable(cancel_inflight):
                await cancel_inflight()
            quiesce = getattr(session, "await_quiescent", None)
            if callable(quiesce):
                await quiesce()
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

    @property
    def task_backend(self) -> "object":
        """#2187 S1: the GLOBAL Task backend — ONE per process, registry-owned, built
        lazily. The SINGLE construction seam for every session (chat / stdio-MCP / spawned)
        — they all route ``task_backend=`` through here, so there is ONE connection in this
        process (the #2180 single-connection pattern, now keyed globally not per-agent: the
        per-instance ``asyncio.Lock`` serialises every write). Path is the global
        ``project_root/.reyn/state/tasks.db`` (the same db the A2A/web server uses — a
        first-class global task store, task ⊥ agent/session). Lazy import avoids an import
        cycle (task.factory → task.sqlite_backend)."""
        if self._task_backend is None:
            from reyn.task.factory import create_task_backend  # noqa: PLC0415
            path = self._project_root / ".reyn" / "state" / "tasks.db"
            self._task_backend = create_task_backend(
                "sqlite",
                path=str(path),
                # #2187 backend-master (2c-i): inject the WAL-derived
                # SubscriptionRegistry so the backend hydrates the binding
                # (assignee/requester/requester_kind) through it.
                subscription_reader=self._task_subscriptions,
            )
        return self._task_backend

    # #2187 S1: the #2180 per-agent ``_close_task_backend`` is removed — the global Task
    # backend is PROCESS-LIFETIME (parity with ``_state_log``, which is
    # likewise never closed mid-run); a global store is not closed on any one agent's
    # purge/drop (it outlives them), and an open sqlite connection does not block the
    # agent-dir rmtree (POSIX unlink-while-open). Closed on process exit.

    def _purge_session_dir(self, name: str, sid: str) -> bool:
        """#2125: the destructive half of session teardown — rmtree the per-session
        state dir. Split out from ``remove_session`` so the rewind path can DEFER it
        until after the substrate restores succeed (atomicity). Best-effort; LOGs an
        ``OSError`` rather than swallowing. Returns True iff a dir was removed."""
        state_dir = self._session_state_dir(name, sid)  # sid != main → sessions/<enc>/
        if not state_dir.is_dir():
            return False
        import shutil
        try:
            shutil.rmtree(state_dir)
            return True
        except OSError as e:  # noqa: BLE001 — best-effort; LOG (don't silently swallow)
            logger.warning(
                "#2103/#2125: teardown of session %r/%r left state on disk: %s",
                name, sid, e,
            )
            return False

    async def _materialize_rewind(
        self, *, reconstruct_seq: int, workspace_at_or_below: int,
    ) -> list[str]:
        """Bring the runtime substrate to the active branch as-of ``reconstruct_seq``.

        Idempotent — shared by ``rewind_to`` (right after the reset-record) and
        crash ``recover_rewind_if_needed`` (at restart). Per agent: ``reconstruct``
        as-of the active branch + persist a self-contained snapshot pinned to
        ``reconstruct_seq`` (so ``restore_all`` replays only beyond it); loaded
        sessions are reset + re-adopt it.

        ``reconstruct_seq`` is the WAL head at call time (= R in rewind_to, =
        current head in recovery); ``workspace_at_or_below`` is the as-of-cut DROP
        boundary = ``target_n`` in rewind_to or head in recovery. Returns the agents
        materialised.
        """
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
        # #2103: the existence cut is the rewind TARGET (``workspace_at_or_below`` =
        # target_n), NOT ``reconstruct_seq`` (= R, the reset-record head). An entity
        # whose create-seq > target didn't exist as-of-target → drop it. In crash
        # recovery ``workspace_at_or_below`` = head, so create-seq > head is never
        # true → no spurious drops (recovery reconstructs the present, not a rewind).
        drop_cut = workspace_at_or_below
        # #2103 S2 re-materialise: an agent created ≤ cut, NOT purged, currently
        # ABSENT (dropped at a prior cut) → re-create from its agent_created record
        # so a forward-checkout-past-drop brings it back (the inverse of the drop).
        for _rname, (_rcseq, _rpayload, _rparent, _rpseq) in ag_created.items():
            if _rname in ag_purged:
                continue  # fork A: purged = permanent, never re-materialised
            if _rcseq <= drop_cut and not (self._dir / _rname).is_dir():
                self._rematerialise_agent(_rname, _rpayload)
        for name in self.list_names():
            # An agent created after the cut — OR purged (fork A: permanent) — is
            # torn down (subsumes its nested sessions) instead of reconstructed.
            # Reversible (create case): a forward-checkout past the create
            # re-materialises it from the agent_created WAL record (the pass above).
            agent_seq = created_at.get(("agent", name, ""))
            if name in ag_purged or (agent_seq is not None and agent_seq > drop_cut):
                self._drop_agent(name)
                continue
            for sid in self._discover_session_ids(name):
                # A session spawned after the cut → drop just that session.
                # #2154: OR a session that VANISHED at-or-before the cut (it was gone
                # as-of-cut) — the destroy-side mirror of the spawn-cut. A genuine
                # vanish normally already rmtree'd the dir (so discovery won't surface
                # it); this guard keeps reconstruction correct if a dir SURVIVES its
                # vanish (a crash mid-rmtree, or a future session re-materialise seam).
                sess_seq = created_at.get(("session", name, sid))
                van_seq = sess_vanished.get((name, sid))
                spawned_after_cut = sess_seq is not None and sess_seq > drop_cut
                vanished_by_cut = van_seq is not None and van_seq <= drop_cut
                if spawned_after_cut or vanished_by_cut:
                    # #2125/#2180: detach now (quiesce in-flight writes; the global Task
                    # backend is NOT closed on a session drop — one process-wide
                    # connection); defer the destructive rmtree until the restores succeed.
                    await self._drop_session(name, sid, purge_dir=False)
                    deferred_session_purges.append((name, sid))
                    continue
                store = self._store_for(name, sid)
                snap = reconstruct(
                    name, store, self._state_log,
                    target_seq=reconstruct_seq, session_id=sid,
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
        self._reconcile_archived_as_of_cut(ag_archived, drop_cut)
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
        """
        if self._state_log is None:
            return None
        target = active_rewind_target(self._state_log)
        if target is None:
            return None
        head = self._state_log.last_durable_seq
        agents = await self._materialize_rewind(
            reconstruct_seq=head, workspace_at_or_below=head,
        )
        return {"recovered_target_n": target, "head": head, "agents": agents}

    # ── WAL truncation (skill resume design) ────────────────────────────────
    #
    # Trigger policy: semantic boundary — call this after appending a
    # `skill_phase_advanced` or `skill_completed` event to the WAL. Throttled
    # to avoid thrashing on bursty phase completions. Size-based safety net
    # (long-idle skills) is intentionally deferred until we have real WAL
    # size telemetry from dogfood.
    #
    # Floor calculation: `min(全 agent applied_seq, 全 active skill
    # last_phase_applied_seq) + 1` — everything strictly below this seq is
    # universally absorbed and droppable. Replaying from `floor - 1` would
    # be a no-op for every snapshot, so dropping below it is safe.
    #
    # Owner rationale: AgentRegistry is the only layer that has both
    # (a) the StateLog handle, and (b) visibility into all agents' snapshots
    # + every active skill snapshot under each agent's `state/skills/`
    # directory. Pushing this into entry points (`reyn chat`, `reyn web`)
    # would duplicate the orchestration; pushing it into StateLog itself
    # would force the WAL to know about agent / skill layout.

    _TRUNCATION_THROTTLE_SECS: float = 5.0
    # R-D4: size safety net default. Session's chat-turn-boundary
    # call uses this threshold. Long-idle skills with no semantic
    # boundary events (= no phase_advanced / skill_completed) would
    # otherwise let the WAL grow unboundedly between turns.
    _SIZE_SAFETY_NET_BYTES: int = 1_000_000

    async def truncate_wal_if_eligible(
        self, *, bypass_throttle: bool = False,
    ) -> dict | None:
        """Compute floor across all agents + active skill snapshots, then
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
        bubble up to disturb the caller's hot path (phase advance / skill
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
                # #2187 S1: the Task backend is GLOBAL (not under this agent dir) — the
                # #2180 per-agent close-before-rmtree is gone.
                shutil.rmtree(self._dir / name, ignore_errors=True)
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

        When a user runs ``/skill discard <run_id>`` on agent B, and that
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

    # R-D16: skills awaiting an intervention longer than this many seconds
    # are excluded from the WAL truncation floor calc. Without this, a
    # single skill stuck on ``ask_user`` (e.g. user away from terminal)
    # pins the floor at its ``last_phase_applied_seq`` indefinitely and
    # the WAL grows unbounded. Long-await skills accept memo loss for the
    # awaited window — at resume they fall through to re-execute the op
    # whose memo was truncated, which is the same behaviour as a memo
    # cache miss.
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

        ``floor = min(全 active session applied_seq, 全 active skill
        last_phase_applied_seq, 全 active plan last_step_applied_seq) + 1``

        PR-N7 (FP-0008): reads watermarks exclusively from in-memory
        state — session journal snapshots + per-session skill / plan
        registries — by walking ``self._agents.values()`` and calling
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

        R-D16: skills awaiting an intervention for longer than
        ``_LONG_AWAIT_THRESHOLD_SEC`` are excluded so the WAL can keep
        advancing — delegated to
        :meth:`SkillRegistry.iter_applied_phase_seqs` which performs
        the elapsed check in-memory.

        Returns 0 when no watermark is available (no live session, no
        active skill, no active plan).
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
        non-delegation load (byte-identical to pre-#2081)."""
        existing = self._peek_session(name)
        if existing is not None:
            return existing
        if not self.exists(name):
            raise FileNotFoundError(
                f"agent {name!r} not found; run `reyn agent new {name}` to create it"
            )
        profile = self.load_profile(name)
        session = self._construct_session(profile, is_delegate=is_delegate)
        self._store_session(name, session)
        return session

    def _construct_session(
        self, profile: AgentProfile, *, is_delegate: bool = False
    ) -> "object":
        """Build a configured Session from a profile (factory + shared-store
        attach), WITHOUT inserting it into the session map. Shared by get_or_load
        (default session) and spawn_session (additional sessions) — FP-0043 S3.

        #2081: ``is_delegate`` is published on the transient
        ``_constructing_as_delegate`` for the duration of the (synchronous) factory
        call, so the factory's ``resolved_profile_for(profile.name)`` sees it
        without a factory-signature change. Save/restore (not set-False) so it is
        correct under nesting too — non-re-entrant today, but free future-proofing."""
        _prev_delegate = self._constructing_as_delegate
        self._constructing_as_delegate = is_delegate
        try:
            session = self._factory(profile)
        finally:
            self._constructing_as_delegate = _prev_delegate
        # #1547: hand the session the shared anchor store so cut_generation
        # records the rewind-timeline preview text at each boundary.
        anchors = self.anchor_store
        attach_anchor = getattr(session, "attach_anchor_store", None)
        if anchors is not None and callable(attach_anchor):
            attach_anchor(anchors)
        return session

    def spawn_session(self, name: str, sid: "str | None" = None) -> str:
        """FP-0043 Stage 3: open a NEW conversation Session under an existing
        Agent, SHARING the agent's identity object. Returns the new session-id.

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
        session = self._construct_session(self.load_profile(name))
        if shared is not None:
            # Share the SAME identity object (not the fresh one the factory built),
            # so a future identity change propagates to all of the agent's sessions.
            session._agent = shared
        # FP-0043 Stage 5: stamp the new session's id so EVERY WAL append it makes
        # carries new_sid (per-session snapshot routing). Done here — before the
        # session's run-loop / forwarder go live (that is attach_session, S4a,
        # strictly later) — so there is NO "main"-tagged append window for the
        # spawned session. The journal is built eagerly in __init__ (set_session_id
        # propagates to the in-memory snapshot too); the skill_registry is lazy and
        # reads _session_id at construction, so setting the attribute covers a later
        # build, and we also fix up an already-built one defensively.
        session._session_id = new_sid
        session._journal.set_session_id(new_sid)
        existing_skill_registry = getattr(session, "_skill_registry", None)
        if existing_skill_registry is not None:
            existing_skill_registry.set_session_id(new_sid)
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
        # scope); chat events are the P6 audit log. Isolating both aligns them with the
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
        # action_usage.json stays agent-wide by design (name-only, NOT re-keyed): it is
        # the agent's tool-habit ranking (writer = compactor, reader = RouterLoop's
        # per-turn hot-list cold-start hint), an agent-knowledge tier — and per-turn
        # freshness for the CURRENT conversation is already supplied separately by the
        # live overlay (this session's uncompacted calls layered on each turn). So it is
        # correctly shared across the agent's sessions, not per-conversation state.
        self._sessions.setdefault(name, {})[new_sid] = session
        return new_sid

    async def spawn_session_recorded(
        self, name: str, *, mode: str = "persistent",
        narrowing: "dict | None" = None,
    ) -> str:
        """#2103 S1bc: the action-layer SESSION-SPAWN seam — spawn a fresh-context
        session under ``name`` (sync ``spawn_session``) + persist the spawner's
        per-session capability narrowing (workspace-backed P5 config.yaml, the #2103
        S1a layer) + emit ``session_spawned`` so rewind tracks/drops/re-materialises
        it. Mirrors ``create_agent`` (the agent CREATE seam): the mechanism stays sync;
        the event marks the LLM action. ``session_spawned`` is config-complete
        (mode + narrowing) for symmetric re-materialise. Returns the new sid.

        Does NOT submit a task — that is the caller (the spawn op), separable from the
        record. Emit no-ops without a WAL."""
        sid = self.spawn_session(name)
        if mode == "ephemeral":
            # #2103: mark the live session so it auto-vanishes once its task is done
            # (Session._maybe_schedule_ephemeral_vanish, via this registry's
            # remove_session teardown seam). Persistent spawns leave the flag False.
            ephemeral_session = self._peek_session(name, sid)
            if ephemeral_session is not None:
                ephemeral_session._ephemeral = True
        if narrowing:
            import yaml
            cfg_path = self._session_state_dir(name, sid) / "config.yaml"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                yaml.safe_dump({"name": f"_session_{sid}", **narrowing}),
                encoding="utf-8",
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
        if self._state_log is not None:
            await self._state_log.append(
                "session_spawned", entity_kind="session", name=name, sid=sid,
                mode=mode, narrowing=narrowing,
            )
        return sid

    async def ensure_running(self, name: str) -> "object":
        """Load + start session.run() + forwarder for `name` without
        changing the user-attached pointer. Used for agent-to-agent
        messaging (PR11): when A sends to B, B's task must be live to
        consume the inbox put, but the user's display stays on whoever
        they were attached to.

        The forwarder is still started so that, should the user later
        attach to B, B's pre-existing outbox messages route correctly.
        """
        session = self.get_or_load(name)
        sid = _DEFAULT_SID  # FP-0043 S3: name-keyed API drives the default session
        key = (name, sid)
        if key not in self._tasks or self._tasks[key].done():
            self._tasks[key] = asyncio.create_task(session.run())
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
            self._tasks[key] = asyncio.create_task(session.run())
        return session

    def bind_focus_listeners(
        self,
        *,
        on_chat_event: "Callable[..., None] | None" = None,
        intervention_channel: str | None = None,
    ) -> None:
        """Bind front-end listeners that follow the focused (attached) session.

        The interactive REPL/CUI binds its working-indicator chat-event callback
        and its intervention listener channel here ONCE; the registry wires them
        to the currently-attached session now and re-wires them on every
        subsequent ``attach`` / ``attach_session`` so an agent switch never
        strands them on the old session. Idempotent per front-end (one binding).
        """
        self._focus_chat_listener = on_chat_event
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
        if session is None:
            return
        if self._focus_chat_listener is not None:
            session.subscribe_chat_events(self._focus_chat_listener)
        if self._focus_intervention_channel is not None:
            try:
                session.register_intervention_listener(self._focus_intervention_channel)
            except AttributeError:
                pass

    def _unwire_focus_listeners(self, session: "object | None") -> None:
        if session is None:
            return
        if self._focus_chat_listener is not None:
            session.unsubscribe_chat_events(self._focus_chat_listener)
        if self._focus_intervention_channel is not None:
            try:
                session.unregister_intervention_listener(self._focus_intervention_channel)
            except AttributeError:
                pass

    async def attach(self, name: str) -> "object":
        """Switch the attached agent to `name`. Loads + starts session.run()
        and the outbox forwarder for the new agent if not already running.
        Old agent stays in `self._tasks` (background)."""
        new_session = self.get_or_load(name)
        sid = _DEFAULT_SID  # FP-0043 S3: attach(name) focuses the default session
        key = (name, sid)
        old = self._attached
        if old is not None and old != key:
            old_session = self._peek_session(old[0], old[1])
            if old_session is not None:
                # Mark detached BEFORE switching so transient outbox emissions
                # from the old session start dropping at the source
                # (`Session._put_outbox` filters status/trace).
                old_session.is_attached = False
                # Move any focus-following front-end listeners off the old session.
                self._unwire_focus_listeners(old_session)

        new_session.is_attached = True
        if old != key:
            # First attach or a genuine switch: wire the focus listeners to the
            # now-focused session (no-op if no front-end bound any).
            self._wire_focus_listeners(new_session)
        # Boot session.run() + forwarder on first attach. Keep them alive
        # across detach/re-attach cycles — shutdown drains via `running_tasks()`.
        if key not in self._tasks or self._tasks[key].done():
            self._tasks[key] = asyncio.create_task(new_session.run())
        if key not in self._forward_tasks or self._forward_tasks[key].done():
            self._forward_tasks[key] = asyncio.create_task(
                self._forwarder(name, sid)
            )
        self._attached = key

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
        run-loop/forwarder boot + the ``is_attached`` focus flip, so the focused
        session's output routes to ``repl_outbox`` and the previously-focused
        session stops forwarding (``is_attached=False``)."""
        target = self._peek_session(name, sid)
        if target is None:
            raise KeyError(f"no session {sid!r} for agent {name!r}")
        key = (name, sid)
        old = self._attached
        if old is not None and old != key:
            old_session = self._peek_session(old[0], old[1])
            if old_session is not None:
                old_session.is_attached = False
                self._unwire_focus_listeners(old_session)
        target.is_attached = True
        if old != key:
            self._wire_focus_listeners(target)
        if key not in self._tasks or self._tasks[key].done():
            self._tasks[key] = asyncio.create_task(target.run())
        if key not in self._forward_tasks or self._forward_tasks[key].done():
            self._forward_tasks[key] = asyncio.create_task(self._forwarder(name, sid))
        self._attached = key
        for iv in target._interventions.list_active():
            if not iv.future.done():
                await target._announce_intervention(iv)
        return target

    async def _forwarder(self, name: str, sid: str = _DEFAULT_SID) -> None:
        """Pump one session's outbox into the registry-level repl_outbox.

        Runs continuously per (name, sid) session. Only forwards when that
        session is the attached one; otherwise drops the message (transient
        kinds were already dropped at source, durable narration is in history).
        Special kind `__attach_request__` is consumed here as a control signal.
        """
        key = (name, sid)
        agent = self._peek_session(name, sid)
        while True:
            msg = await agent.outbox.get()
            if msg.kind == "__end__":
                # Session shut down — propagate to REPL only if we're the
                # attached one (otherwise REPL would terminate spuriously
                # on a detached session's shutdown).
                if key == self._attached:
                    await self.repl_outbox.put(msg)
                return
            if msg.kind == "__attach_request__":
                # User typed `/attach <other>` while this agent was attached.
                if msg.text and self.exists(msg.text):
                    await self.attach(msg.text)
                    # (Issue #191 re-post removed: that forwarded msg to the
                    # Textual TUI's _on_attach_request for header refresh.
                    # TUI deleted — re-post is dead code; _output_loop never
                    # handled this kind, so only effect was a bare-text leak.)
                continue
            if msg.kind == "__session_switch_request__":
                # FP-0043 Stage 4a: `/session switch <sid>` — focus another session
                # of the CURRENT agent (msg.text = target sid). Routed through the
                # forwarder (mirroring __attach_request__) so the focus flip +
                # display re-wire are sequenced on the registry side, not raced by a
                # direct call from the slash handler. Graceful on a bad sid: drop
                # (the slash handler validated existence + replied before posting).
                if msg.text:
                    try:
                        await self.attach_session(name, msg.text)
                    except KeyError:
                        pass  # session vanished between validate + switch — no-op
                    # (re-post removed: was for Textual TUI header refresh — dead
                    # code after TUI deletion; _output_loop never consumed it.)
                continue
            if key == self._attached:
                await self.repl_outbox.put(msg)
            # else: drop — session is detached, transient kinds were already
            # dropped at source, durable narration is in history.jsonl

    def detach(self) -> None:
        """Mark the attached session as detached without stopping its task."""
        if self._attached is None:
            return
        session = self._peek_session(self._attached[0], self._attached[1])
        if session is not None:
            session.is_attached = False
        self._attached = None

    @property
    def attached_name(self) -> str | None:
        # FP-0043 S3: _attached is (name, sid); the public accessor exposes the
        # agent NAME (byte-identical to the prior str|None).
        return self._attached[0] if self._attached is not None else None

    @property
    def attached_sid(self) -> str | None:
        """FP-0043 Stage 4a: the focused session-id (or None) — the public
        surface for `/session list`'s focus marker + tests, so callers don't
        reach into `_attached`."""
        return self._attached[1] if self._attached is not None else None

    def attached_session(self) -> "object | None":
        if self._attached is None:
            return None
        return self._peek_session(self._attached[0], self._attached[1])

    def running_tasks(self) -> list[asyncio.Task]:
        """All non-completed tasks (session.run + forwarders) for shutdown drain."""
        out: list[asyncio.Task] = []
        for table in (self._tasks, self._forward_tasks):
            out.extend(t for t in table.values() if not t.done())
        return out

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
        if not tasks:
            return
        # Cooperative grace, then hard-cancel any straggler (cancelled forwarders
        # finish immediately; a stuck session.run lands in `pending`).
        _done, pending = await asyncio.wait(tasks, timeout=_SHUTDOWN_GRACE_S)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def loaded_names(self) -> list[str]:
        return list(self._sessions.keys())

    def session_tree(self) -> "list[dict]":
        """Snapshot of the agent→session tree for the status-bar agent menu.

        A read-only, freshly-built copy (no handle to live registry state): agents in
        load order, each with its sessions (sids, sorted) and which (agent, sid) is the
        current attach focus.
        """
        out: list[dict] = []
        for name in self.loaded_names():
            sids = sorted((self._sessions.get(name) or {}).keys())
            out.append({
                "agent": name,
                "attached": self._attached is not None and self._attached[0] == name,
                "sessions": [
                    {"sid": sid, "attached": self._attached == (name, sid)}
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

    def _reload_topologies(self) -> None:
        self._topologies = {}
        if not self._topology_dir.is_dir():
            return
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
            self._topologies[topo.name] = topo

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
            load_capability_profile,
            load_delegate_profile,
            resolve_profile,
        )

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
                resolved.append(resolve_profile(load_delegate_profile(self._project_root)))
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
                resolved.append(resolve_profile(load_delegate_profile(self._project_root)))
                continue
            resolved.append(resolve_profile(prof))

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
                resolved.append(resolve_profile(load_delegate_profile(self._project_root)))

        # #2103 S1a: the per-session config is an ADDITIONAL restrict-only ∩ conjunct
        # (the spawner-set, workspace-backed narrowing) — composed into the single
        # ContextualLayer, never re-granting (structural). Inert when sid is None or
        # no config.yaml exists → byte-identical.
        if sid is not None:
            ps = self._load_per_session_capability_profile(agent, sid)
            if ps is not None:
                resolved.append(resolve_profile(ps))

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
            # #2103 C2b (#2166): the stored edge froze the parent's identity at spawn. If
            # the parent name was purged + REUSED, the current identity differs → the edge
            # is STALE (it points to a GONE identity, not the live same-named agent). Treat
            # exactly like an absent parent: FAIL CLOSED. (A None frozen identity = a
            # parent never minted via create_agent → no staleness signal → the absent-vs-
            # present existence-check below governs, Q2 — no false-positive.)
            stale = (
                parent_seq is not None
                and self._agent_create_seq.get(parent) != parent_seq
            )
            if stale or not (self._dir / parent).is_dir():
                # #2161 (absent parent) + #2166 (name-reused → stale identity): the capping
                # parent's identity is gone, so ⊆-parent CANNOT be verified. FAIL CLOSED:
                # compose the restrictive _delegate floor (NOT skip — skipping is the
                # fail-open escalation: purge/reuse-the-parent-to-uncap-the-child). DISTINCT
                # from a PRESENT-but-unrestricted parent (parent_ctx is None in the else
                # branch → correctly skipped, no cap to impose). One seam covers every
                # cap-drop cause (purge, name-reuse, crash, fs-delete).
                resolved.append(resolve_profile(load_delegate_profile(self._project_root)))
            else:
                parent_ctx, parent_excl = self.resolved_profile_for(parent)
                if parent_ctx is not None:
                    resolved.append((parent_ctx, parent_excl))

        if not resolved:
            return None, frozenset()
        return compose_resolved(resolved)

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
