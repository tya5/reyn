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
    # Outstanding (unresolved) interventions keyed by intervention_id.
    outstanding_interventions: dict[str, dict] = field(default_factory=dict)
    # R-D12: durable buffered intervention answers keyed by run_id.
    # Populated when the user answers an intervention post-restart but
    # before the resuming run consumes it. Survives a *second* crash
    # so the answer is replayed when the run finally resumes (the
    # in-memory ``_buffered_intervention_answers`` dict in Session
    # is the runtime cache; this field is its on-disk durable form).
    # Each value is ``{"text": str, "choice_id": str | None}``.
    buffered_intervention_answers: dict[str, dict] = field(default_factory=dict)
    # #1800 slice 4b: staged wake=false ride-along (C) messages waiting for
    # the next wake=true trigger turn to consume them.  Persisted (decision B)
    # so a crash while waiting for the trigger doesn't silently drop context
    # that was already inbox_consumed.  The in-memory ``_next_turn_context``
    # list in Session is the runtime cache; this field is its on-disk form.
    # Each entry is ``{"kind": str, "payload": dict}`` (no msg_id — already
    # consumed from the inbox before staging here).
    next_turn_context: list[dict] = field(default_factory=list)
    # #2884: the hook-driven-turns loop-valve counter (Session._hook_driven_turns),
    # snapshot-backed for crash-durability. A pure-WAL-derived count is NOT
    # truncation-safe — the consumed `inbox_put{kind:"hook"}` events it would
    # need are pruned by `truncate_below` (only REWIND_KIND is force-kept,
    # retention.py) — exactly the #2259 config-loss class. The snapshot floor
    # sits at/above the truncation floor, so this field survives truncation
    # by construction. Kept current between snapshots by `apply_events`
    # replaying `hook_driven_turns_set` WAL entries (see `_apply_one`).
    hook_driven_turns: int = 0

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
                "Run `reyn chat --reset` to wipe in-flight run state "
                "(audit logs in .reyn/events/ are preserved)."
            )
        def _coerce_int(v: object) -> int:
            # A version-matched but hand-edited / corrupted snapshot may carry a
            # null / non-numeric applied_seq; .get(k, 0) only defaults a *missing*
            # key. Mirrors the #1906 TokenUsage fix.
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        return cls(
            agent_name=agent_name,
            # FP-0043 S5: prefer the saved session_id; fall back to the caller's
            # (which defaults "main") for legacy snapshots written pre-S5.
            session_id=str(data.get("session_id", session_id)),
            applied_seq=_coerce_int(data.get("applied_seq", 0)),
            inbox=list(data.get("inbox", []) or []),
            pending_chains=dict(data.get("pending_chains", {}) or {}),
            outstanding_interventions=dict(
                data.get("outstanding_interventions", {}) or {}
            ),
            buffered_intervention_answers=dict(
                data.get("buffered_intervention_answers", {}) or {}
            ),
            next_turn_context=list(
                data.get("next_turn_context", []) or []
            ),
            hook_driven_turns=_coerce_int(data.get("hook_driven_turns", 0)),
        )

    def to_payload(self) -> dict:
        """The serialisable payload dict (references to the live mutable state). ``serialize``
        json.dumps this immediately (so it is a consistent capture). #2259 PR-2b's ``save_nowait``
        deep-copies it for a CONSISTENT sync capture and stamps ``applied_seq`` from the
        worker-assigned WAL seq in the durable job (the seq is not known on the task loop)."""
        return {
            "version": SNAPSHOT_VERSION,
            "session_id": self.session_id,  # FP-0043 S5 (additive; legacy load → "main")
            "applied_seq": self.applied_seq,
            "inbox": self.inbox,
            "pending_chains": self.pending_chains,
            "outstanding_interventions": self.outstanding_interventions,
            "buffered_intervention_answers": self.buffered_intervention_answers,
            "next_turn_context": self.next_turn_context,
            "hook_driven_turns": self.hook_driven_turns,
        }

    def serialize(self) -> str:
        """Serialise to a JSON string — SYNCHRONOUS, so it captures a consistent view of the
        mutable state (inbox / chains / …) at the call instant. #1765 1a-ii splits this from
        the durable write so an off-loop save snapshots the state here (sync) and only the
        write+fsync runs off the event loop, with no risk of the state being mutated mid-write.
        """
        return json.dumps(self.to_payload(), ensure_ascii=False, indent=2)

    @staticmethod
    def serialize_payload(payload: dict) -> str:
        """Serialise a pre-captured payload dict (#2259 PR-2b: ``save_nowait`` stamps the
        worker-assigned ``applied_seq`` into a deep-copied payload, then serialises here)."""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def write_durable(path: Path, data: str) -> None:
        """Atomically + durably write pre-serialised snapshot ``data`` (tmp → fsync → rename).
        Pure I/O (no mutable-state access), so it is safe to run OFF the event loop (#1765)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def save(self, path: Path) -> None:
        """Synchronous atomic save (serialise + durable write). Unchanged contract."""
        self.write_durable(path, self.serialize())

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
        # ── #1800 slice 4b: next-turn-context staging ───────────────────
        elif kind == "next_turn_context_staged":
            entry = event.get("entry")
            if entry and isinstance(entry, dict):
                self.next_turn_context.append(entry)
        elif kind == "next_turn_context_cleared":
            self.next_turn_context.clear()
        # ── #2884: loop-valve counter (between-snapshot replay maintenance) ──
        elif kind == "hook_driven_turns_set":
            try:
                self.hook_driven_turns = int(event.get("count", 0))
            except (TypeError, ValueError):
                self.hook_driven_turns = 0
        # step_started/completed/failed mutate per-task snapshot only — no agent-level state change here.
        # Unknown kinds: no-op (forward compatibility for future kinds)
