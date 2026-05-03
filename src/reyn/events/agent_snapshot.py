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

    # ── persistence ─────────────────────────────────────────────────────

    @classmethod
    def empty(cls, agent_name: str) -> "AgentSnapshot":
        return cls(agent_name=agent_name)

    @classmethod
    def load(cls, agent_name: str, path: Path) -> "AgentSnapshot":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt / missing file → defensive empty (existing behavior;
            # there is no version info to compare anyway).
            return cls.empty(agent_name)
        if not isinstance(data, dict):
            return cls.empty(agent_name)
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
            applied_seq=int(data.get("applied_seq", 0)),
            inbox=list(data.get("inbox", []) or []),
            pending_chains=dict(data.get("pending_chains", {}) or {}),
            active_skill_run_ids=list(
                data.get("active_skill_run_ids", []) or []
            ),
            outstanding_interventions=dict(
                data.get("outstanding_interventions", {}) or {}
            ),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": SNAPSHOT_VERSION,
            "applied_seq": self.applied_seq,
            "inbox": self.inbox,
            "pending_chains": self.pending_chains,
            "active_skill_run_ids": self.active_skill_run_ids,
            "outstanding_interventions": self.outstanding_interventions,
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
        """Return True if `event` affects this agent."""
        return (
            event.get("target") == self.agent_name
            or event.get("agent") == self.agent_name
        )

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
        # skill_phase_advanced, step_started/completed/failed, skill_resumed
        # mutate per-skill snapshot only — no agent-level state change here.
        # Unknown kinds: no-op (forward compatibility for future kinds)
