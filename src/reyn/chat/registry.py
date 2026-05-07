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

logger = logging.getLogger(__name__)

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog

from .profile import PROFILE_FILENAME, AgentProfile
from .topology import TOPOLOGY_DIRNAME, Topology, _validate_topology_name

DEFAULT_AGENT_NAME = "default"

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
    ) -> None:
        """
        session_factory: returns a configured ChatSession given an AgentProfile.
            The factory captures CLI-derived defaults (model, resolver, permissions,
            limits, mcp config, …) — registry doesn't need to know them.
        state_log: PR21 WAL for crash recovery. When None, persistence is
            disabled (tests / non-chat invocation). Owned by the caller; the
            registry just hands it to each constructed session and uses it
            during `restore_all()`.
        """
        self._dir = project_root / ".reyn" / "agents"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._topology_dir = project_root / ".reyn" / TOPOLOGY_DIRNAME
        self._factory = session_factory
        self._state_log = state_log
        self._project_root = project_root
        self._agents: dict[str, "object"] = {}            # name -> ChatSession
        self._tasks: dict[str, asyncio.Task] = {}         # name -> session.run() task
        self._forward_tasks: dict[str, asyncio.Task] = {} # name -> outbox forwarder
        self._attached: str | None = None
        # WAL truncation throttle (skill resume design). monotonic ts of last
        # successful truncation attempt; ``None`` means no throttle is active.
        self._last_truncation_ts: float | None = None
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

    # ── persistence ──────────────────────────────────────────────────────────

    def list_names(self) -> list[str]:
        """All agent names found on disk (sorted)."""
        out = []
        for entry in self._dir.iterdir():
            if entry.is_dir() and (entry / PROFILE_FILENAME).is_file():
                out.append(entry.name)
        return sorted(out)

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
        if name == self._attached:
            raise ValueError(f"cannot remove attached agent {name!r}")
        target = self._dir / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        # Cancel any cached tasks / drop session before deleting on-disk state.
        for task_dict in (self._tasks, self._forward_tasks):
            task = task_dict.pop(name, None)
            if task and not task.done():
                task.cancel()
        self._agents.pop(name, None)
        # Recursive rm — agents/<name>/ is reyn-managed, no surprises expected.
        import shutil
        shutil.rmtree(target)
        # PR12: cascade — drop the agent from any topology it belongs to so
        # we don't leave dangling references. Topologies that would become
        # invalid (team losing its leader, kind=team with no members) are
        # removed entirely.
        self._cascade_agent_removal(name)

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
        """
        if self._state_log is None:
            return {}

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
            session = self._agents.get(name)
            if session is None:
                continue
            try:
                await self._recover_plans_for_agent(name, session)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "plan recovery failed for agent=%s: %r", name, exc,
                )

        return snapshots

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
             ``session._spawn_resumed_plan`` → PlanRuntime task starts.

        Errors at any step degrade gracefully: a bad config falls back
        to defaults; an unloadable artifact yields forced discard with
        outbox notice (= ADR-0023 §3.5 corruption fallback).
        """
        from reyn.plan import (
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

        decisions = coordinator.discover_and_decide(
            plan_registry=plan_registry,
            wal_events=wal_events,
            decomposition_loader=_decomposition_loader,
            child_skill_lookup=_child_skill_lookup,
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
                await session._spawn_resumed_plan(decision=decision)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_spawn_resumed_plan failed for %s: %r",
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
            floor = self._compute_truncate_floor()
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
        return stats

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
        for name, session in self._agents.items():
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

    def _compute_truncate_floor(self) -> int:
        """Return the lowest seq that MUST remain in the WAL.

        ``floor = min(全 持続 agent applied_seq, 全 active skill
        last_phase_applied_seq) + 1``

        Truly dormant agents (no snapshot file on disk) are *excluded*
        from the floor calculation. The invariant that justifies this
        skip:

          A dormant agent has no live ``ChatSession``. WAL events are
          only appended through a session's ``SnapshotJournal``, which
          targets the session's own agent. Therefore no WAL event can
          target an agent whose session has never been instantiated
          this run, and dropping events older than the dormant agent's
          (zero) applied_seq cannot orphan messages.

          When the dormant agent later receives its first event, the
          ``SnapshotJournal`` assigns ``applied_seq`` to the freshly
          allocated WAL seq — effectively a "starts here" stamp — so
          the agent never gets stuck below the truncation floor.

        Returns 0 when:
          - no persistent agents found on disk (no constraint anywhere
            in the system → don't truncate)
          - any snapshot file is malformed (conservative — keep WAL
            bloated rather than risk dropping needed entries)
        """
        seqs: list[int] = []

        # Per-agent applied_seq from each agent's main snapshot. Two skip
        # paths for dormant agents (semantically equivalent — an agent that
        # has never absorbed a WAL event):
        #   1. snapshot file missing — fresh agent, never persisted
        #   2. snapshot file present but ``applied_seq == 0`` — written by
        #      ``restore_all`` for an agent with no events to absorb
        #
        # AgentSnapshot.load is defensive (returns empty on parse error),
        # which would silently mask corruption as the dormant case. We
        # parse explicitly here to distinguish: corruption returns
        # floor=0 (fail closed, keep WAL intact); legitimate ``applied_seq
        # == 0`` means dormant and can be skipped safely.
        for name in self.list_names():
            snap_path = self._dir / name / "state" / "snapshot.json"
            if not snap_path.is_file():
                continue
            try:
                data = json.loads(snap_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "WAL truncation: cannot read agent snapshot %s: %s",
                    snap_path, e,
                )
                return 0
            if not isinstance(data, dict):
                logger.warning(
                    "WAL truncation: malformed agent snapshot %s "
                    "(not an object)", snap_path,
                )
                return 0
            try:
                applied_seq = int(data.get("applied_seq", 0))
            except (TypeError, ValueError):
                return 0
            if applied_seq == 0:
                # Dormant — no WAL events have targeted this agent
                # (SnapshotJournal would have bumped applied_seq above 0
                # on first append). Skip from floor calc.
                continue
            seqs.append(applied_seq)

        # Per-active-skill last_phase_applied_seq. We do NOT load the full
        # SkillSnapshot dataclass here — only need one int field. Direct
        # JSON read sidesteps any future schema bumps from breaking
        # truncation just to extract a number.
        for name in self.list_names():
            skills_dir = self._dir / name / "state" / "skills"
            if not skills_dir.is_dir():
                continue
            for snap_file in skills_dir.glob("*.snapshot.json"):
                try:
                    data = json.loads(snap_file.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001 — see returns 0 above
                    logger.warning(
                        "WAL truncation: cannot read skill snapshot %s: %s",
                        snap_file, e,
                    )
                    return 0
                if isinstance(data, dict):
                    val = data.get("last_phase_applied_seq", 0)
                    try:
                        seqs.append(int(val))
                    except (TypeError, ValueError):
                        return 0

        # ADR-0023 §3.1: per-active-plan last_step_applied_seq. Direct
        # JSON read mirrors the skill block above. Plans live at
        # state/plans/<plan_id>.snapshot.json (sibling to per-plan dirs).
        # If a long-running plan exists, its watermark pins the floor so
        # plan_step_* events the resume analyzer needs aren't truncated.
        for name in self.list_names():
            plans_dir = self._dir / name / "state" / "plans"
            if not plans_dir.is_dir():
                continue
            for snap_file in plans_dir.glob("*.snapshot.json"):
                try:
                    data = json.loads(snap_file.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "WAL truncation: cannot read plan snapshot %s: %s",
                        snap_file, e,
                    )
                    return 0
                if isinstance(data, dict):
                    val = data.get("last_step_applied_seq", 0)
                    try:
                        seqs.append(int(val))
                    except (TypeError, ValueError):
                        return 0

        if not seqs:
            return 0
        # Drop entries strictly below the lowest absorbed seq. The +1 makes
        # the boundary exclusive: the seq itself remains as a watermark
        # (StateLog.truncate_below additionally guards the highest seq).
        return min(seqs) + 1

    # ── lifecycle ────────────────────────────────────────────────────────────

    def get_or_load(self, name: str) -> "object":
        """Return the ChatSession for `name`, instantiating from profile if new."""
        if name in self._agents:
            return self._agents[name]
        if not self.exists(name):
            raise FileNotFoundError(
                f"agent {name!r} not found; run `reyn agent new {name}` to create it"
            )
        profile = self.load_profile(name)
        session = self._factory(profile)
        self._agents[name] = session
        return session

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
        if name not in self._tasks or self._tasks[name].done():
            self._tasks[name] = asyncio.create_task(session.run())
        if name not in self._forward_tasks or self._forward_tasks[name].done():
            self._forward_tasks[name] = asyncio.create_task(self._forwarder(name))
        return session

    async def attach(self, name: str) -> "object":
        """Switch the attached agent to `name`. Loads + starts session.run()
        and the outbox forwarder for the new agent if not already running.
        Old agent stays in `self._tasks` (background)."""
        new_session = self.get_or_load(name)
        old_name = self._attached
        if old_name and old_name != name:
            old_session = self._agents.get(old_name)
            if old_session is not None:
                # Mark detached BEFORE switching so transient outbox emissions
                # from the old session start dropping at the source
                # (`ChatSession._put_outbox` filters status/trace).
                old_session.is_attached = False

        new_session.is_attached = True
        # Boot session.run() + forwarder on first attach. Keep them alive
        # across detach/re-attach cycles — shutdown drains via `running_tasks()`.
        if name not in self._tasks or self._tasks[name].done():
            self._tasks[name] = asyncio.create_task(new_session.run())
        if name not in self._forward_tasks or self._forward_tasks[name].done():
            self._forward_tasks[name] = asyncio.create_task(
                self._forwarder(name)
            )
        self._attached = name

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

    async def _forwarder(self, name: str) -> None:
        """Pump one agent's outbox into the registry-level repl_outbox.

        Runs continuously per agent. Only forwards when this agent is the
        attached one; otherwise drops the message (transient kinds were
        already dropped at source, durable narration is in history). Special
        kind `__attach_request__` is consumed here as a control signal.
        """
        agent = self._agents[name]
        while True:
            msg = await agent.outbox.get()
            if msg.kind == "__end__":
                # Session shut down — propagate to REPL only if we're the
                # attached one (otherwise REPL would terminate spuriously
                # on a detached agent's shutdown).
                if name == self._attached:
                    await self.repl_outbox.put(msg)
                return
            if msg.kind == "__attach_request__":
                # User typed `:attach <other>` while this agent was attached.
                if msg.text and self.exists(msg.text):
                    await self.attach(msg.text)
                continue
            if name == self._attached:
                await self.repl_outbox.put(msg)
            # else: drop — agent is detached, transient kinds were already
            # dropped at source, durable narration is in history.jsonl

    def detach(self) -> None:
        """Mark the attached agent as detached without stopping its task."""
        if self._attached is None:
            return
        session = self._agents.get(self._attached)
        if session is not None:
            session.is_attached = False
        self._attached = None

    @property
    def attached_name(self) -> str | None:
        return self._attached

    def attached_session(self) -> "object | None":
        if self._attached is None:
            return None
        return self._agents.get(self._attached)

    def running_tasks(self) -> list[asyncio.Task]:
        """All non-completed tasks (session.run + forwarders) for shutdown drain."""
        out: list[asyncio.Task] = []
        for table in (self._tasks, self._forward_tasks):
            out.extend(t for t in table.values() if not t.done())
        return out

    async def shutdown(self) -> None:
        """Best-effort: stop all loaded sessions, then await tasks."""
        for name, agent in list(self._agents.items()):
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
        return list(self._agents.keys())

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
