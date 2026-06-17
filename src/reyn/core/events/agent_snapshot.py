"""AgentSnapshot — per-agent state snapshot for crash recovery (PR21).

Stores the agent's recovery-critical runtime state plus the WAL `seq`
already absorbed (`applied_seq`). On restart, the registry replays WAL
entries past every snapshot's `applied_seq`, then hands each agent its
final snapshot to populate in-memory queues / dicts.

Atomic write: dump to `<path>.tmp`, fsync, rename. mid-write crash leaves
the previous file intact.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SNAPSHOT_VERSION = 1


class SchemaVersionError(Exception):
    """Raised when a snapshot file's schema version does not match the
    current code's expected version.

    Message includes a hint to run ``reyn chat --reset`` so operators have
    a clear next-action. PR-resume-ux β U4: pre-1.0 we refuse to load
    incompatible snapshots rather than silently corrupt state. Post-1.0
    will add automated migration (R-D15).
    """


@dataclass
class AgentSnapshot:
    """Recovery-critical state for one agent.

    `applied_seq` is the highest WAL seq whose effects are already baked
    into `inbox` / `pending_chains`. WAL replay applies events with
    `seq > applied_seq`.
    """

    agent_name: str
    # FP-0043 Stage 5: the conversation session this snapshot belongs to. Default
    # "main" = the implicit single session (byte-identical pre-S5); spawned
    # sessions get their sid. WAL replay routes each entry by (agent_name,
    # session_id) so per-session snapshots stay isolated.
    session_id: str = "main"
    applied_seq: int = 0
    # inbox messages: each is {"id": str, "kind": str, "payload": dict}
    inbox: list[dict] = field(default_factory=list)
    # pending chains keyed by chain_id: each value is the _PendingChain
    # field set serialized as a dict ({chain_id, origin_agent, origin_depth,
    # original_request, waiting_on: list}).
    pending_chains: dict[str, dict] = field(default_factory=dict)
    # NEW (skill resume design — PR-state-foundation):
    # run_ids of skills currently executing under this agent.
    active_skill_run_ids: list[str] = field(default_factory=list)
    # Outstanding (unresolved) interventions keyed by intervention_id.
    outstanding_interventions: dict[str, dict] = field(default_factory=dict)
    # R-D12: durable buffered intervention answers keyed by skill_run_id.
    # Populated when the user answers an intervention post-restart but
    # before the resuming skill consumes it. Survives a *second* crash
    # so the answer is replayed when the skill finally resumes (the
    # in-memory ``_buffered_intervention_answers`` dict in Session
    # is the runtime cache; this field is its on-disk durable form).
    # Each value is ``{"text": str, "choice_id": str | None}``.
    buffered_intervention_answers: dict[str, dict] = field(default_factory=dict)
    # ADR-0022 — plan-mode crash resilience Phase 1.
    # plan_ids of in-flight plan-mode executions for this agent. Populated
    # by `plan_started` WAL events; pruned by `plan_completed` /
    # `plan_aborted`. AgentRegistry.restore_all uses non-empty
    # active_plan_ids post-replay as the "interrupted plan" signal and
    # discards orphan child skills + emits a user-facing outbox message.
    # Additive field (no SNAPSHOT_VERSION bump) — follows the R-D13
    # `parent_run_id` precedent on `load`: defaults to [] when absent.
    active_plan_ids: list[str] = field(default_factory=list)

    # ── persistence ─────────────────────────────────────────────────────

    @classmethod
    def empty(cls, agent_name: str, session_id: str = "main") -> "AgentSnapshot":
        return cls(agent_name=agent_name, session_id=session_id)

    @classmethod
    def load(cls, agent_name: str, path: Path, session_id: str = "main") -> "AgentSnapshot":
        # FP-0043 Stage 5: session_id defaults to "main" so a legacy caller (and a
        # legacy agent_name-keyed snapshot at the pre-S5 path) loads as the agent's
        # "main" session — the migration fallback, no recovery-state loss.
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt / missing file → defensive empty (existing behavior;
            # there is no version info to compare anyway).
            return cls.empty(agent_name, session_id)
        if not isinstance(data, dict):
            return cls.empty(agent_name, session_id)
        # PR-resume-ux β U4: schema_version refuse. A missing version field
        # or a mismatch is treated as incompatible — operator must
        # explicitly --reset to wipe.
        version = data.get("version")
        if version != SNAPSHOT_VERSION:
            raise SchemaVersionError(
                f"AgentSnapshot at {path} has version {version!r}, "
                f"expected {SNAPSHOT_VERSION}. "
                "Run `reyn chat --reset` to wipe in-flight skill state "
                "(audit logs in .reyn/events/ are preserved)."
            )
        return cls(
            agent_name=agent_name,
            # FP-0043 S5: prefer the saved session_id; fall back to the caller's
            # (which defaults "main") for legacy snapshots written pre-S5.
            session_id=str(data.get("session_id", session_id)),
            applied_seq=int(data.get("applied_seq", 0)),
            inbox=list(data.get("inbox", []) or []),
            pending_chains=dict(data.get("pending_chains", {}) or {}),
            active_skill_run_ids=list(
                data.get("active_skill_run_ids", []) or []
            ),
            outstanding_interventions=dict(
                data.get("outstanding_interventions", {}) or {}
            ),
            buffered_intervention_answers=dict(
                data.get("buffered_intervention_answers", {}) or {}
            ),
            # ADR-0022: additive — defaults to [] when reading older snapshots
            # written before the field existed. No SNAPSHOT_VERSION bump.
            active_plan_ids=list(data.get("active_plan_ids", []) or []),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": SNAPSHOT_VERSION,
            "session_id": self.session_id,  # FP-0043 S5 (additive; legacy load → "main")
            "applied_seq": self.applied_seq,
            "inbox": self.inbox,
            "pending_chains": self.pending_chains,
            "active_skill_run_ids": self.active_skill_run_ids,
            "outstanding_interventions": self.outstanding_interventions,
            "buffered_intervention_answers": self.buffered_intervention_answers,
            "active_plan_ids": self.active_plan_ids,
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    # ── replay (apply WAL entries to this snapshot) ─────────────────────

    def apply_events(self, events: Iterable[dict]) -> None:
        """Apply each WAL event whose target matches this agent.

        Events with `seq <= self.applied_seq` are skipped (already baked
        in). `target` / `agent` field disambiguates which agent the event
        affects.
        """
        for event in events:
            seq = event.get("seq")
            if not isinstance(seq, int) or seq <= self.applied_seq:
                continue
            if not self._matches_agent(event):
                continue
            self._apply_one(event)
            self.applied_seq = seq

    def _matches_agent(self, event: dict) -> bool:
        """Return True if `event` affects THIS (agent, session).

        FP-0043 Stage 5: routes by agent (target/agent) AND session. A WAL entry's
        ``session_id`` defaults to "main" when absent (legacy entries written
        pre-S5, and the default single session) — so legacy entries deterministically
        replay into the agent's "main" session, and a spawned session only absorbs
        its own entries. This is the per-session replay-determinism guarantee."""
        agent_matches = (
            event.get("target") == self.agent_name
            or event.get("agent") == self.agent_name
        )
        return agent_matches and event.get("session_id", "main") == self.session_id

    def _apply_one(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "inbox_put":
            self.inbox.append({
                "id": event["msg_id"],
                "kind": event["msg_kind"],
                "payload": event.get("payload", {}),
            })
        elif kind == "inbox_consume":
            msg_id = event.get("msg_id")
            self.inbox = [m for m in self.inbox if m.get("id") != msg_id]
        elif kind == "chain_register":
            self.pending_chains[event["chain_id"]] = {
                "chain_id": event["chain_id"],
                "origin_agent": event["origin_agent"],
                "origin_depth": int(event["origin_depth"]),
                "original_request": event["original_request"],
                "waiting_on": list(event.get("waiting_on", [])),
            }
        elif kind == "chain_update":
            chain = self.pending_chains.get(event["chain_id"])
            if chain is not None:
                chain["waiting_on"] = list(event.get("waiting_on", []))
        elif kind in ("chain_resolve", "chain_timeout_fired"):
            self.pending_chains.pop(event.get("chain_id"), None)
        # ── skill resume kinds (PR-state-foundation) ────────────────────
        elif kind == "skill_started":
            run_id = event.get("run_id")
            if run_id and run_id not in self.active_skill_run_ids:
                self.active_skill_run_ids.append(run_id)
        elif kind in ("skill_completed", "skill_discarded"):
            # PR-resume-ux β: skill_discarded prunes active_skill_run_ids
            # the same way skill_completed does — both are terminal states
            # from the agent-snapshot perspective.
            run_id = event.get("run_id")
            if run_id and run_id in self.active_skill_run_ids:
                self.active_skill_run_ids.remove(run_id)
        elif kind == "intervention_dispatched":
            iv_id = event.get("intervention_id")
            if iv_id:
                self.outstanding_interventions[iv_id] = event.get("iv_dict", {})
        elif kind == "intervention_resolved":
            iv_id = event.get("intervention_id")
            if iv_id:
                self.outstanding_interventions.pop(iv_id, None)
        # ── R-D12: durable buffered answer ──────────────────────────────
        elif kind == "intervention_answer_buffered":
            run_id = event.get("run_id")
            if run_id:
                self.buffered_intervention_answers[run_id] = {
                    "text": event.get("text", ""),
                    "choice_id": event.get("choice_id"),
                }
        elif kind == "intervention_answer_consumed":
            run_id = event.get("run_id")
            if run_id:
                self.buffered_intervention_answers.pop(run_id, None)
        # ── ADR-0022: plan-mode lifecycle (Phase 1) ─────────────────────
        elif kind == "plan_started":
            plan_id = event.get("plan_id")
            if plan_id and plan_id not in self.active_plan_ids:
                self.active_plan_ids.append(plan_id)
        elif kind in ("plan_completed", "plan_aborted"):
            plan_id = event.get("plan_id")
            if plan_id and plan_id in self.active_plan_ids:
                self.active_plan_ids.remove(plan_id)
        # skill_phase_advanced, step_started/completed/failed, skill_resumed
        # mutate per-skill snapshot only — no agent-level state change here.
        # Unknown kinds: no-op (forward compatibility for future kinds)
