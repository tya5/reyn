"""AgentRegistry — owner of all ChatSession instances in a `reyn chat` process.

PR10 introduces multiple agents (= multiple ChatSession instances) sharing
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
- `attached` returns the currently-attached ChatSession (or None)
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
from uuid import uuid4

logger = logging.getLogger(__name__)

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.anchor_store import AnchorStore
from reyn.core.events.retention import RetentionPolicy, compute_retention_floor
from reyn.core.events.snapshot_generations import (
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
from reyn.core.events.workspace_op_content_log import WorkspaceOpContentLog
from reyn.core.events.workspace_version_store import WorkspaceVersionStore

from .profile import PROFILE_FILENAME, AgentProfile
from .topology import TOPOLOGY_DIRNAME, Topology, _validate_topology_name

DEFAULT_AGENT_NAME = "default"
# FP-0043 Stage 3: the implicit per-agent session id. Single-session paths
# resolve to this id, keeping N=1 behaviour byte-identical. Spawned sessions get
# generated ids (Stage 4 routes inbound messages to non-default sessions).
_DEFAULT_SID = "main"

# ADR-0038 1f: WAL-entry-kind → rewind-point boundary label. All inputs are
# OS-level ``WAL_EVENT_KINDS`` (P7-safe — no skill/domain strings). The three
# output labels are the D6 Phase-1 granularity (turn / plan-step / phase).
_REWIND_PLAN_STEP_KINDS = frozenset({
    "step_completed", "step_failed",
    "plan_step_completed", "plan_step_failed",
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
    """In-process map of agent_name -> ChatSession with persistence wired in.

    Owns the **REPL-facing outbox**: a single queue that consumers (e.g.
    `repl._output_loop`) read regardless of which agent is attached. A
    per-agent forwarder task pumps the agent's own `outbox` into this queue
    only while that agent is the attached one — detached agents drop
    transient outbox items, durable kinds (agent / skill_done) still
    persist to history via the agent's `_append_history` (handled at the
    ChatSession layer, not here).
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
        workspace_capture: bool = True,
        act_turn_capture: bool = False,
    ) -> None:
        """
        session_factory: returns a configured ChatSession given an AgentProfile.
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
        self._dir = project_root / ".reyn" / "agents"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._topology_dir = project_root / ".reyn" / TOPOLOGY_DIRNAME
        self._factory = session_factory
        self._state_log = state_log
        self._project_root = project_root
        # FP-0043 Stage 3: the Registry holds N conversation Sessions per Agent.
        # Identity (the Agent value object, S2) is shared per name; the
        # conversation instances (= today's ChatSession, inbox+run-loop+history)
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
        # WAL truncation throttle (skill resume design). monotonic ts of last
        # successful truncation attempt; ``None`` means no throttle is active.
        self._last_truncation_ts: float | None = None
        # ADR-0038 Stage 1c-2: set for the duration of a global rewind. While
        # set, ``maybe_truncate_for_size`` no-ops so a compaction can't advance
        # the WAL keep-floor over the reset-record / reconstruct reads mid-cut.
        self._rewind_in_progress: bool = False
        # ADR-0038 Stage 1e (D5): retention window. None → live (current).
        self._retention_policy = retention_policy or RetentionPolicy()
        # ADR-0038 Stage 1d: the workspace half of a generation. One shadow-git
        # for the (single-SSoT) workspace, git-dir under .reyn (out of the
        # tracked tree). Host-mode worktree = project_root; container mode is a
        # tracked follow-up (#1544). Lazily built so non-chat / no-WAL callers
        # never touch git.
        self._workspace_store: WorkspaceVersionStore | None = None
        # ADR-0038 #1544: FS+exec backend. None / name!="container" → host mode
        # (host subprocess git runner). When container, git runs in-container via
        # backend.run with the container path context (the work-tree is
        # container-side; host git can't reach it).
        self._environment_backend = environment_backend
        # ADR-0038 #1557 (gap-#1): host-side OS-state dir (--state-dir). When set,
        # the shadow git-dir lives under it (a first-class member of the persisted
        # OS-state set, alongside events/artifacts — one persistence switch) rather
        # than at project_root/.reyn. None → default project_root/.reyn location.
        self._workspace_state_dir = workspace_state_dir
        # #1582: time-travel cost opt-out. False → "runtime-only rewind": the
        # workspace store is never built/attached, so cut_generation skips the
        # per-boundary shadow-git capture (the largest constant cost) while the
        # runtime substrate (AgentSnapshot generations + WAL) is untouched. The
        # existing None-guards (attach / capture / restore / prune) make this
        # coherent by construction. Run-level (construction-time), like
        # retention_policy — not a mid-session toggle.
        self._workspace_capture = workspace_capture
        # #1560: opt-in per-step (act-turn) workspace capture (default off). When
        # on, a generic post-append observer on the WAL captures a write-tree
        # snapshot at each ``step_completed`` into the op-content-log, so act-turn
        # rewind can restore mid-skill-run workspace state (restore = PR-2). The
        # callback is registered ONLY when on, so default users pay zero per-append
        # cost (the WAL's observer list stays empty). Gated additionally by the
        # Tier-1 workspace store (off / None → no-op).
        self._act_turn_capture = act_turn_capture
        self._op_content_log: "WorkspaceOpContentLog | None" = None
        if act_turn_capture and state_log is not None:
            state_log.register_post_append(self._on_wal_append_capture)
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
        """All agent names found on disk (sorted)."""
        out = []
        for entry in self._dir.iterdir():
            if entry.is_dir() and (entry / PROFILE_FILENAME).is_file():
                out.append(entry.name)
        return sorted(out)

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

    def remove(self, name: str) -> None:
        if name == DEFAULT_AGENT_NAME:
            raise ValueError("cannot remove the default agent")
        if self._attached is not None and self._attached[0] == name:
            raise ValueError(f"cannot remove attached agent {name!r}")
        target = self._dir / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        # Cancel any cached tasks / drop sessions before deleting on-disk state.
        # FP-0043 Stage 3: removing an agent drops ALL its sessions (every sid).
        sids = list(self._sessions.get(name, {}).keys())
        for task_dict in (self._tasks, self._forward_tasks):
            for sid in sids:
                task = task_dict.pop((name, sid), None)
                if task and not task.done():
                    task.cancel()
        self._sessions.pop(name, None)
        self._identities.pop(name, None)
        # Recursive rm — agents/<name>/ is reyn-managed, no surprises expected.
        import shutil
        shutil.rmtree(target)
        # PR12: cascade — drop the agent from any topology it belongs to so
        # we don't leave dangling references. Topologies that would become
        # invalid (team losing its leader, kind=team with no members) are
        # removed entirely.
        self._cascade_agent_removal(name)

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

        # 1. Load snapshots
        snapshots: dict[str, AgentSnapshot] = {}
        for name in self.list_names():
            snap_path = self._dir / name / "state" / "snapshot.json"
            if snap_path.is_file():
                snapshots[name] = AgentSnapshot.load(name, snap_path)
            else:
                snapshots[name] = AgentSnapshot.empty(name)

        if not snapshots:
            return {}

        # 2-3. WAL replay from min(applied_seq) + 1
        min_seq = min(s.applied_seq for s in snapshots.values())
        wal_entries = list(self._state_log.iter_from(min_seq + 1))
        for snap in snapshots.values():
            snap.apply_events(wal_entries)

        # 4. Save the post-replay snapshots
        for name, snap in snapshots.items():
            snap_path = self._dir / name / "state" / "snapshot.json"
            snap.save(snap_path)

        # 5. Hand each non-empty snapshot to its session.
        # PR-intervention-link L4: outstanding_interventions also triggers
        # restore — without it, an agent whose only stranded state is an
        # in-flight ask_user would be skipped here and the user could not
        # clear the queued intervention after restart.
        # ADR-0022: active_plan_ids also triggers restore — needed so the
        # cleanup hook can fire and notify the user that their plan was
        # interrupted.
        for name, snap in snapshots.items():
            if (not snap.inbox
                    and not snap.pending_chains
                    and not snap.outstanding_interventions
                    and not snap.active_plan_ids):
                continue
            session = self.get_or_load(name)
            session.restore_state(snap)
            await self.ensure_running(name)

        # 6. ADR-0023 Phase 2: orphan plan recovery via PlanResumeCoordinator.
        # Plans whose decomposition artifact survived (= post-Step-6 plans)
        # are analyzed for memo replay; pre-Phase-2 plans without an
        # artifact get the same Phase 1 outcome (= forced discard +
        # outbox notice + plan_aborted) via the coordinator's missing-
        # artifact fallback.
        for name, snap in snapshots.items():
            if not snap.active_plan_ids:
                continue
            session = self._peek_session(name)
            if session is None:
                continue
            try:
                await self._recover_plans_for_agent(name, session)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "plan recovery failed for agent=%s: %r", name, exc,
                )

        return snapshots

    # ── Global rewind (ADR-0038 Stage 1c-2, D2 consistent-cut) ──────────────

    def _store_for(self, name: str) -> SnapshotGenerationStore:
        """Return the snapshot-generation store for ``name``.

        Reuses the live session's store when the agent is loaded (so an
        in-flight session and the rewind path share one view of the
        generations dir); otherwise constructs one over the on-disk path.
        """
        session = self._peek_session(name)
        store = getattr(session, "_generation_store", None)
        if isinstance(store, SnapshotGenerationStore):
            return store
        return SnapshotGenerationStore(
            name, self._dir / name / "state" / "generations",
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
        abandoned). Because ``reconstruct`` / ``_materialize_rewind`` /
        ``_restore_workspace_active`` all recompute ``is_active`` from the full
        chain, both substrates follow the *target's* lineage automatically.

        Architecture-enforced global cut (D2): one global single-seq WAL + one
        workspace SSoT ⇒ one reset-record moves *every* agent atomically:

          1. retention guard — reject a target truncated out of the WAL (1e).
          2. all-cancel  — ``cancel_inflight`` on every loaded session.
          3. all-quiesce — ``await_quiescent`` on every loaded session (1c-1):
             stop-world THEN settle, so no straggler appends past the record.
          4. append ONE global reset-record (fsync'd before any reconstruct —
             the crash-mid-rewind idempotence keystone, 1b).
          5. reconstruct every KNOWN agent as-of the target lineage (honoring the
             recomputed is_active) + persist a **self-contained** snapshot at
             ``applied_seq = R`` (``restore_all`` replays only > R); loaded
             sessions reset (``reset_for_rewind``) + re-adopt; the workspace is
             restored to the nearest active generation ``<= seq``.

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
            # 2. all-cancel (stop-world).
            for session in sessions:
                await session.cancel_inflight()
            # 3. all-quiesce (settle every WAL-append task).
            for session in sessions:
                await session.await_quiescent()
            # 4. single global reset-record; supersedes = prior active head (audit).
            prior_head = self._state_log.current_seq
            reset_seq = await _append_reset_record(
                self._state_log, target_seq=seq, supersedes=prior_head,
            )
            # 5. materialise both substrates along the target lineage.
            agents = await self._materialize_rewind(
                reconstruct_seq=reset_seq, workspace_at_or_below=seq,
            )
            return {
                "target_n": seq,
                "reset_seq": reset_seq,
                "agents": agents,
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
          - ``step_completed`` / ``step_failed`` /
            ``plan_step_completed`` / ``plan_step_failed`` → ``plan-step``
          - anything else (``inbox_consume``, …)           → ``turn``

        Generations are per-agent but keyed by the single global WAL seq, so
        the union across known agents is the global rewind-point set. Abandoned
        (rewound-past) boundaries are filtered out via ``is_active_seq``.

        Empty when there is no WAL or no generations.
        """
        if self._state_log is None:
            return []

        # Union of generation boundary seqs across every known agent. Default =
        # active branch only (1f); include_abandoned = all branches (Phase-2 tree).
        seqs: set[int] = set()
        for name in self.list_names():
            for s in self._store_for(name).seqs():
                if include_abandoned or is_active_seq(self._state_log, s):
                    seqs.add(s)
        if not seqs:
            return []

        # One pass over the WAL to map boundary seq → (ts, kind). The audit
        # EventStore is NOT consulted — keeping WAL and audit decoupled.
        wal_at: dict[int, dict] = {}
        for entry in self._state_log.iter_from(1):
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
        immediately-prior checkpoint that is a **turn** (plan-step / phase cuts are
        skipped — ``record_plan_step_completed`` cuts intra-turn checkpoints, but an
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
    def workspace_store(self) -> WorkspaceVersionStore | None:
        """The shadow-git workspace store (ADR-0038 1d), lazily built.

        ``None`` when there is no WAL (non-chat / tests that opt out), OR when
        ``workspace_capture`` is disabled (#1582 runtime-only rewind — the opt-out
        for the per-boundary shadow-git capture cost). Host-mode worktree =
        ``project_root``; git-dir under ``.reyn`` (or ``--state-dir``, #1557).
        Container mode is a tracked follow-up (#1544).
        """
        if not self._workspace_capture:
            return None
        if self._state_log is None:
            return None
        if self._workspace_store is None:
            self._workspace_store = self._build_workspace_store()
        return self._workspace_store

    def _build_workspace_store(self) -> WorkspaceVersionStore:
        """Build the workspace store for the active environment (#1544).

        Host (default): a host subprocess git runner over the host git-dir.
        Container (``environment_backend.name == "container"``): git runs
        in-container via ``backend.run`` with the CONTAINER path context, while
        the small FS surface (init dir + ``info/exclude``) stays on the host
        git-dir — which in mount-mode is the bind-mount source, so the write is
        visible in-container. (Attach/baked mode has no bind-mount → host FS and
        container git diverge → degrades; attach-mode persistence is a tracked
        follow-up, #1544 checklist.)

        #1557 gap-#1: the host git-dir is routed under ``workspace_state_dir``
        (``--state-dir``) when provided, so the workspace-version history persists
        alongside the rest of the host-side OS state (one switch); otherwise it
        defaults to ``project_root/.reyn``. (Container persistence *modes* — bind-
        mount injection / attach-sync — remain the deferred #1557 follow-up.)
        """
        host_git_root = self._workspace_state_dir or (self._project_root / ".reyn")
        host_git_dir = host_git_root / "workspace-shadow.git"
        backend = self._environment_backend
        if backend is not None and getattr(backend, "name", "") == "container":
            from reyn.core.events.workspace_version_store import _ContainerGitRunner

            repo_dir = str(getattr(backend, "repo_dir", "/workspace"))
            runner = _ContainerGitRunner(
                backend,
                git_dir=f"{repo_dir}/.reyn/workspace-shadow.git",
                work_tree=repo_dir,
            )
            return WorkspaceVersionStore(
                self._project_root, host_git_dir, git_runner=runner,
            )
        return WorkspaceVersionStore(self._project_root, host_git_dir)

    @property
    def op_content_log(self) -> "WorkspaceOpContentLog | None":
        """The op-granular (act-turn) content log (#1560), lazily built.

        ``None`` when act-turn capture is off or there is no WAL. Lives beside the
        shadow git-dir (under ``--state-dir`` when set, #1557) — the same root as
        the boundary generations, so the persistence switch covers both.
        """
        if not self._act_turn_capture or self._state_log is None:
            return None
        if self._op_content_log is None:
            root = self._workspace_state_dir or (self._project_root / ".reyn")
            self._op_content_log = WorkspaceOpContentLog(root / "op-content-log.jsonl")
        return self._op_content_log

    async def _on_wal_append_capture(self, kind: str, seq: int, fields: dict) -> None:
        """Generic WAL post-append observer: per-step act-turn workspace capture.

        Fires only for ``step_completed`` (the act-turn step boundary, whose seq is
        ``CommittedStep.seq``). Gated by the Tier-1 workspace store: when workspace
        capture is off (``workspace_store is None``), this is a no-op too (one
        switch). Captures a bare ``write-tree`` snapshot and records ``(seq,
        tree_sha)``. Best-effort — runs on the swallow-safe observer path, so any
        failure here never affects the WAL append (the store/log already swallow).
        """
        if kind != "step_completed":
            return
        ws = self.workspace_store
        log = self.op_content_log
        if ws is None or log is None:
            return
        tree_sha = await ws.capture_tree()
        if tree_sha is not None:
            # #1560 PR-3: pin the tree as a gc-root BEFORE logging it (the bare
            # write-tree is otherwise unreachable → auto-gc'd). Ref-before-append
            # gives the strict invariant "logged ⇒ gc-protected": any entry the
            # restore reads is guaranteed to have a surviving tree. Dropped at prune.
            await ws.ref_op_tree(seq, tree_sha)
            log.append(seq, tree_sha)

    async def restore_workspace_to_act_turn(self, target_seq: int) -> str | None:
        """Restore the workspace to the act-turn (per-op) state as-of ``target_seq``.

        The workspace half of an act-turn rewind (#1560 PR-2), the coherent
        counterpart to ``SkillResumeCoordinator.plan_for_act_turn_rewind`` (which
        truncates the runtime memo at ``target_seq``). Because the op-content-log is
        keyed by ``op_seq == CommittedStep.seq``, the SAME ``target_seq`` restores
        both substrates: runtime memo[≤K] (coordinator) ⊗ workspace tree[≤K] (here).

        Lineage resolution is the caller's job (the op-content-log is branch-agnostic
        — capture is always current-branch): pick the latest op-tree with
        ``op_seq <= target_seq`` that is **is_active** (skipping abandoned-interval
        op-trees, mirroring ``_restore_workspace_active`` for generations), then
        ``read-tree`` it. Falls back to the nearest **boundary** generation
        (``_restore_workspace_active``) when no active op-tree ``<= target_seq``
        exists (act-turn capture was off for that span / pre-feature). No-op when
        act-turn capture or the workspace store is disabled (Tier-1 #1582 / no WAL).
        Returns the restored tree sha (op-tree path) or ``None``.
        """
        if self._state_log is None:
            return None
        log = self.op_content_log
        ws = self.workspace_store
        if log is None or ws is None:
            return None
        # is_active-honoring: the branch-agnostic op-content-log may hold op-trees
        # from abandoned intervals; skip them (same lineage rule as the boundary
        # restore) and take the latest active op-tree at-or-below the target.
        active = [
            e for e in log.entries()
            if int(e["op_seq"]) <= target_seq
            and is_active_seq(self._state_log, int(e["op_seq"]))
        ]
        if active:
            tree_sha = max(active, key=lambda e: int(e["op_seq"]))["tree_sha"]
            return await ws.restore_tree(tree_sha)
        # boundary fallback: no act-turn op-tree on the active lineage <= target →
        # the nearest active generation (today's behaviour) — strictly additive.
        await self._restore_workspace_active(at_or_below=target_seq)
        return None

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

    async def _materialize_rewind(
        self, *, reconstruct_seq: int, workspace_at_or_below: int,
    ) -> list[str]:
        """Bring BOTH substrates to the active branch as-of ``reconstruct_seq``.

        Idempotent — shared by ``rewind_to`` (right after the reset-record) and
        crash ``recover_rewind_if_needed`` (at restart). Per agent: ``reconstruct``
        as-of the active branch + persist a self-contained snapshot pinned to
        ``reconstruct_seq`` (so ``restore_all`` replays only beyond it); loaded
        sessions are reset + re-adopt it. Then the workspace substrate is
        restored to the nearest **active** generation ``<= workspace_at_or_below``.

        ``reconstruct_seq`` is the WAL head at call time (= R in rewind_to, =
        current head in recovery); ``workspace_at_or_below`` is ``target_n`` in
        rewind_to (= gen-N) or head in recovery (= latest active gen, keeping
        post-rewind workspace work). Returns the agents materialised.
        """
        agents: list[str] = []
        for name in self.list_names():
            store = self._store_for(name)
            snap = reconstruct(
                name, store, self._state_log, target_seq=reconstruct_seq,
            )
            # Self-contained: the reset-record carries no agent target, so
            # reconstruct leaves applied_seq at the last active entry. Pin it to
            # the head so restore_all's replay floor skips the abandoned segment.
            snap.applied_seq = reconstruct_seq
            snap.save(self._dir / name / "state" / "snapshot.json")
            session = self._peek_session(name)
            if session is not None:
                await session.reset_for_rewind()
                session.restore_state(snap)
            agents.append(name)
        await self._restore_workspace_active(at_or_below=workspace_at_or_below)
        return agents

    async def _restore_workspace_active(self, *, at_or_below: int) -> None:
        """Restore the workspace to the nearest ACTIVE generation <= ``at_or_below``.

        Honors is_active (mirrors ``reconstruct`` for runtime): gen-tags in an
        abandoned segment ``(N, R)`` are skipped, so a crash-after-rewind-before-
        any-post-rewind-capture never restores the undone-future workspace. Git
        absence degrades at exec time inside the store (#1544 — no git_available()
        pre-gate, which would test the host PATH meaninglessly in container mode).
        """
        ws = self.workspace_store
        if ws is None:
            return
        active = [
            s for s in await ws.seqs()
            if s <= at_or_below and is_active_seq(self._state_log, s)
        ]
        if active:
            await ws.restore_to_seq(max(active))

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
        head = self._state_log.current_seq
        agents = await self._materialize_rewind(
            reconstruct_seq=head, workspace_at_or_below=head,
        )
        return {"recovered_target_n": target, "head": head, "agents": agents}

    # ── Plan resume recovery (ADR-0023 Phase 2 step 7d) ─────────────────────

    async def _recover_plans_for_agent(
        self, agent_name: str, session: "Any",
    ) -> None:
        """Rehydrate per-agent PlanRegistry, run coordinator, spawn launchable.

        Called once per agent during ``restore_all`` cleanup, after
        snapshots have been replayed onto AgentSnapshot. Steps:

          1. Build a per-agent PlanRegistry and load any surviving plan
             snapshot files (= populated by Step 6's PlanRegistry).
          2. Build a PlanResumeCoordinator with policy from reyn.yaml.
          3. Call coordinator.discover_and_decide → analyze each active
             plan against WAL events.
          4. Call coordinator.apply_decisions → cancel children flagged,
             discard non-resumable plans (= surfaces outbox notice).
          5. For each launchable decision, call
             ``session._plan_runner.spawn_resumed_plan`` → PlanRuntime
             task starts.

        Errors at any step degrade gracefully: a bad config falls back
        to defaults; an unloadable artifact yields forced discard with
        outbox notice (= ADR-0023 §3.5 corruption fallback).
        """
        from reyn.core.plan import (
            PlanRegistry,
            PlanResumeCoordinator,
            build_plan_resume_config,
            read_decomposition,
        )

        agent_state_dir = (
            Path(".reyn") / "agents" / agent_name / "state"
        )
        plan_registry = PlanRegistry(
            agent_name=agent_name, agent_state_dir=agent_state_dir,
        )
        plan_registry.load_active()
        if not plan_registry.list_active():
            # No surviving plan snapshots — every active_plan_ids entry
            # is a Phase-1-era plan with no artifact. Fall back to the
            # legacy discard path (= outbox notice + plan_aborted).
            await self._legacy_discard_orphan_plans(agent_name, session)
            return

        # Resolve config (= reyn.yaml plan_resume:). Tolerant on errors.
        config = None
        try:
            from reyn.config import load_config
            cfg = load_config(Path.cwd())
            config = build_plan_resume_config(cfg.plan_resume_raw)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("plan_resume config load failed: %r", exc)
            config = build_plan_resume_config(None)

        coordinator = PlanResumeCoordinator(config=config)

        # Materialize WAL events once for the coordinator.
        wal_events: list[dict] = []
        if self._state_log is not None:
            wal_events = list(self._state_log.iter_from(0))

        def _decomposition_loader(plan_id: str):
            return read_decomposition(agent_state_dir, plan_id)

        def _child_skill_lookup(child_run_id: str) -> str | None:
            # Best-effort: query the agent's SkillRegistry if available.
            sk_reg = getattr(session, "_skill_registry", None)
            if sk_reg is None:
                return "unknown"
            snap = sk_reg.get(child_run_id)
            if snap is None:
                return "unknown"
            return "in_flight"

        # ADR-0024: thread agent_state_dir so analyzer's get_step_result
        # path can resolve spilled-to-file step results. Coordinator
        # forwards to PlanResumeAnalyzer.analyze.
        decisions = coordinator.discover_and_decide(
            plan_registry=plan_registry,
            wal_events=wal_events,
            decomposition_loader=_decomposition_loader,
            child_skill_lookup=_child_skill_lookup,
            agent_state_dir=agent_state_dir,
        )

        async def _on_outbox_notice(plan_id: str, message: str) -> None:
            from reyn.chat.outbox import OutboxMessage
            try:
                await session._put_outbox(OutboxMessage(
                    kind="error", text=message,
                    meta={"plan_id": plan_id, "reason": "resume_discard"},
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("plan resume outbox notice failed: %r", exc)
            # Apply Phase 1 sibling: emit plan_aborted on agent snapshot
            # so active_plan_ids is cleared.
            try:
                await session._journal.record_plan_aborted(
                    plan_id=plan_id, reason="resume_discard",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("plan_aborted on discard failed: %r", exc)

        skill_registry = getattr(session, "_skill_registry", None)
        launchable = await coordinator.apply_decisions(
            decisions,
            plan_registry=plan_registry,
            skill_registry=skill_registry,
            on_outbox_notice=_on_outbox_notice,
        )
        for decision in launchable:
            try:
                await session._plan_runner.spawn_resumed_plan(
                    decision=decision,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "spawn_resumed_plan failed for %s: %r",
                    decision.plan.plan_id, exc,
                )

    async def _legacy_discard_orphan_plans(
        self, agent_name: str, session: "Any",
    ) -> None:
        """Phase 1 fallback: no decomposition artifact (pre-Phase-2 plans)
        → emit plan_aborted + outbox notice."""
        snap = session._journal.snapshot
        for plan_id in list(snap.active_plan_ids):
            try:
                await session._journal.record_plan_aborted(
                    plan_id=plan_id, reason="restart_cleanup",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "plan_aborted (legacy) failed for %s/%s: %r",
                    agent_name, plan_id, exc,
                )
            try:
                from reyn.chat.outbox import OutboxMessage
                await session._put_outbox(OutboxMessage(
                    kind="error",
                    text=(
                        "A plan-mode reply was interrupted by a previous "
                        "session crash. The partial work could not be "
                        "preserved — please re-issue your request."
                    ),
                    meta={"plan_id": plan_id, "reason": "restart_cleanup"},
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "outbox notice (legacy) failed for %s/%s: %r",
                    agent_name, plan_id, exc,
                )

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
    # R-D4: size safety net default. ChatSession's chat-turn-boundary
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
            stats = await self._state_log.truncate_below(floor)
        except Exception as e:  # noqa: BLE001 — defensive; never fail caller
            logger.warning("WAL truncation: rewrite failed (floor=%d): %s", floor, e)
            return None
        # Stamp success so throttle gates the next attempt. (We don't gate
        # on dropped==0 — even a no-op rewrite resets the throttle window.)
        self._last_truncation_ts = now
        # ADR-0038 Stage 1e (D5): GC generations + workspace blobs on the SAME
        # boundary (Q3 piggyback). prune_below(floor) drops only what is below the
        # (retention-clamped) WAL floor — generations >= floor stay reconstructable,
        # so this never drops rewind history within the retention window.
        await self._prune_generations_below(floor)
        return stats

    async def _prune_generations_below(self, floor: int) -> None:
        """Drop snapshot + workspace generations below ``floor`` (Stage 1e GC).

        ``floor`` is the truncation floor (already retention-clamped), so a
        generation at-or-above it stays reconstructable. Defensive — never raises
        into the truncation hot path. Workspace GC degrades at exec time inside
        the store (#1544 — no host-PATH git_available pre-gate).
        """
        try:
            for name in self.list_names():
                self._store_for(name).prune_below(floor)   # SnapshotGenerationStore (sync)
            ws = self.workspace_store
            if ws is not None:
                await ws.prune_below(floor)
            # #1560 PR-3: act-turn op-content-log GC on the same boundary — drop
            # entries below the floor + unref the out-window op-trees (so auto-gc
            # reclaims them, the same bounded lifecycle generations get).
            op_log = self.op_content_log
            if op_log is not None:
                op_log.prune_below(floor)                   # WorkspaceOpContentLog (sync)
                if ws is not None:
                    await ws.unref_op_trees_below(floor)
            # #1547: anchors GC'd on the same boundary as generations/blobs.
            anchors = self.anchor_store
            if anchors is not None:
                anchors.prune_below(floor)                  # AnchorStore (sync)
        except Exception as e:  # noqa: BLE001 — defensive; never fail caller
            logger.warning("Stage 1e generation GC failed (floor=%d): %s", floor, e)

    async def maybe_truncate_for_size(
        self, *, threshold_bytes: int | None = None,
    ) -> dict | None:
        """Size-driven WAL truncation safety net (R-D4).

        Called from places that don't naturally fire phase-completion
        events but still want to bound WAL growth — primarily the
        ChatSession chat-turn boundary (each user message handled).

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

        Dormant agents (no live ChatSession registered in
        ``self._agents``) are excluded from the floor calculation — the
        same skip the pre-N7 disk-read path applied for
        ``applied_seq == 0`` snapshots. The invariant that justifies
        this:

          A dormant agent has no live ``ChatSession``. WAL events are
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

    def get_or_load(self, name: str) -> "object":
        """Return the ChatSession for `name`, instantiating from profile if new."""
        existing = self._peek_session(name)
        if existing is not None:
            return existing
        if not self.exists(name):
            raise FileNotFoundError(
                f"agent {name!r} not found; run `reyn agent new {name}` to create it"
            )
        profile = self.load_profile(name)
        session = self._construct_session(profile)
        self._store_session(name, session)
        return session

    def _construct_session(self, profile: AgentProfile) -> "object":
        """Build a configured Session from a profile (factory + shared-store
        attach), WITHOUT inserting it into the session map. Shared by get_or_load
        (default session) and spawn_session (additional sessions) — FP-0043 S3."""
        session = self._factory(profile)
        # ADR-0038 Stage 1d: hand the session the single shared workspace
        # shadow-git store so cut_generation captures the workspace at each
        # boundary against the same git-dir the registry's rewind/recovery uses.
        ws = self.workspace_store
        attach = getattr(session, "attach_workspace_store", None)
        if ws is not None and callable(attach):
            attach(ws)
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
        self._sessions.setdefault(name, {})[new_sid] = session
        return new_sid

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
                # (`ChatSession._put_outbox` filters status/trace).
                old_session.is_attached = False

        new_session.is_attached = True
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
                    # Issue #191: forward the message to the REPL so the
                    # TUI's _on_attach_request handler fires and the
                    # header / sticky status reflect the new agent. The
                    # registry consumed the message as a control signal,
                    # but TUI needs the same signal for render update.
                    await self.repl_outbox.put(msg)
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
        # agent NAME (byte-identical to the prior str|None) — the focused sid is
        # an internal detail until multi-session UI lands.
        return self._attached[0] if self._attached is not None else None

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
        """Best-effort: stop all loaded sessions, then await tasks."""
        for name, agent in self._iter_named_sessions():
            try:
                await agent.shutdown()
            except Exception as exc:
                logger.warning("agent shutdown failed for %r: %s", name, exc)
        # Cancel forwarders so they don't block on a queue that won't refill
        for t in self._forward_tasks.values():
            if not t.done():
                t.cancel()
        if self.running_tasks():
            await asyncio.gather(*self.running_tasks(), return_exceptions=True)

    def loaded_names(self) -> list[str]:
        return list(self._sessions.keys())

    def iter_other_agents(self, self_name: str) -> list[dict]:
        """List `{name, role}` for every agent except `self_name`.

        Used by RouterLoop (via ChatSession.list_available_agents) to populate
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
        members = tuple(n for n in self.list_names() if n not in affiliated)
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

    def _cascade_agent_removal(self, agent: str) -> None:
        """Drop `agent` from every topology it's a member of.

        Team topologies losing their leader are removed entirely (a leader-less
        team is meaningless). Pipelines and networks shrink in place. Empty
        topologies are removed.
        """
        for name in list(self._topologies.keys()):
            topo = self._topologies[name]
            if agent not in topo.members:
                continue
            if topo.kind == "team" and topo.leader == agent:
                self.remove_topology(name)
                continue
            new_members = tuple(m for m in topo.members if m != agent)
            if not new_members:
                self.remove_topology(name)
                continue
            new_topo = Topology(
                name=topo.name,
                kind=topo.kind,
                members=new_members,
                leader=topo.leader,
                created_at=topo.created_at,
            )
            new_topo.save(self._topology_dir / f"{name}.yaml")
            self._topologies[name] = new_topo


def _drain_queue(q: asyncio.Queue) -> None:
    """Best-effort drop of all currently-queued items. Non-blocking."""
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass


__all__ = ["AgentRegistry", "DEFAULT_AGENT_NAME"]
