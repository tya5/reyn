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

# #4108: every routing/meta key a "chain_update" WAL event carries that is
# NOT itself chain state — excluded when apply_events mirrors an event's
# fields back onto pending_chains (field-name-independent write-back, see
# that branch's own comment). This is a closed, explicit list rather than a
# structurally-derived one (lead-coder's review, #4108): the alternative —
# an ALLOW-list of known chain-state field names (waiting_on/arm_at/...) —
# has the identical maintenance burden this fix exists to remove (a new
# _PendingChain field would need THAT list updated too), and _PendingChain
# lives in runtime/services/chain_manager.py, which this module (core/events/)
# does not import (the reverse direction is the established one — chain_manager
# only imports AgentSnapshot under TYPE_CHECKING). Origin of each key, so a
# future addition to either source is recognizable as "another one of these":
#   kind, seq, session_id — added by every WAL entry unconditionally
#     (StateLog.append_nowait / SnapshotJournal._wal_append_nowait's own
#     chokepoint, never caller-supplied).
#   target, agent          — the (agent_name, session_id) routing pair
#     record_chain_register/record_chain_update pass explicitly, alongside
#     (not as part of) the chain-state ``fields`` dict.
#   chain_id               — the dict key pending_chains is keyed by; carried
#     IN the event for WAL readability, but redundant with the key itself.
_CHAIN_EVENT_META_KEYS = frozenset({"kind", "seq", "target", "agent", "session_id", "chain_id"})


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
    # (#2884 added `hook_driven_turns` here — the hook-driven-turns
    # loop-valve counter, snapshot-backed for crash-durability. #5561
    # (owner ruling) retired the valve itself; this field, its WAL kind
    # `hook_driven_turns_set`, and `_apply_one`'s own handling branch for
    # it were removed with it — an old WAL/snapshot still carrying either
    # is tolerated by the existing "unknown kinds: no-op" fallback below,
    # the same reader-tolerance #3436 already established for
    # `task_subscribed`/`task_rebound`'s own retirement, state_log.py.)

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
        its own entries. This is the per-session replay-determinism guarantee.

        #2946 item 2: delegates to ``event_route_key`` (the single source of truth
        for the routing rule) so a bucketing caller (``registry.restore_all``) can
        pre-sort a WAL tail by (agent, session) WITHOUT re-deriving — and risking
        drift from — this matching rule."""
        return self.event_route_key(event) == (self.agent_name, self.session_id)

    @staticmethod
    def event_route_key(event: dict) -> "tuple[str, str] | None":
        """The (agent_name, session_id) bucket a WAL ``event`` routes to, or
        ``None`` if it carries neither ``target`` nor ``agent`` (never matches
        any snapshot — same as ``_matches_agent`` returning False for every
        snapshot).

        #2946 item 2: ``restore_all`` calls this ONCE per WAL entry to bucket
        the shared tail by (agent, session) up front, so each snapshot applies
        only its own bucket instead of re-walking the whole tail and calling
        ``_matches_agent`` on every OTHER agent's entries too (the O(agents ×
        tail) re-scan the issue describes). Mirrors ``_matches_agent``'s rule
        exactly: falls back from ``target`` to ``agent`` (the two are written
        mutually-exclusively — ``SnapshotJournal``'s single WAL-append
        chokepoint passes exactly one per call — so this is equivalent to the
        boolean-or `agent_matches` it replaces, not a precedence choice over a
        case that occurs), and ``session_id`` defaults to "main" when absent
        (the same legacy/default-session fallback ``_matches_agent`` documents
        above)."""
        agent_name = event.get("target")
        if agent_name is None:
            agent_name = event.get("agent")
        if agent_name is None:
            return None
        return (agent_name, event.get("session_id", "main"))

    def _apply_one(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "inbox_put":
            self.inbox.append({
                "id": event["msg_id"],
                "kind": event["msg_kind"],
                "payload": event.get("payload", {}),
            })
        elif kind in ("inbox_consume", "inbox_cancel"):
            # #3300 P3 Y-server: `inbox_cancel` is symmetric with
            # `inbox_consume` for replay purposes (both remove the entry) —
            # the DISTINCTION between dispatched vs cancelled matters to the
            # cancel-by-id semantics at record time (session.py), not to
            # snapshot reconstruction, which only needs "this id is gone".
            msg_id = event.get("msg_id")
            self.inbox = [m for m in self.inbox if m.get("id") != msg_id]
        elif kind == "chain_register":
            chain_entry = {
                "chain_id": event["chain_id"],
                "origin_depth": int(event["origin_depth"]),
                "original_request": event["original_request"],
                "waiting_on": list(event.get("waiting_on", [])),
                # #3978 P4: the task kind (prompt/pipeline/exec) — persisted
                # key is "task_kind" (chain_manager.py's own record_chain_register
                # collision note: "kind" is SnapshotJournal._wal_append_nowait's
                # own WAL-event-type positional). Without this branch, a crash
                # recovered via pure WAL REPLAY (not a loaded snapshot file)
                # would silently drop it — ChainManager.restore() reads it
                # back out under the same key.
                "task_kind": event.get("task_kind"),
            }
            # proposal 0067 P4e (#3978): "requester" is a nested
            # {"agent_name", "session_id"} value (ChainManager.register()'s
            # own docstring) — mirrored through as-is when present. A
            # pre-P4e WAL event (recorded under the old flat "origin_agent"/
            # "origin_sid" keys) has no "requester" key — NORMALIZED into the
            # same nested shape here, at replay time, rather than carrying
            # the legacy flat keys forward into ``pending_chains``: every
            # entry this branch produces uses ONE shape regardless of which
            # WAL-event generation wrote it, so nothing downstream (including
            # ChainManager.restore(), which reads "requester" unconditionally
            # after this normalization) needs its own legacy-fallback branch.
            if "requester" in event:
                chain_entry["requester"] = event["requester"]
            else:
                chain_entry["requester"] = {
                    "agent_name": event.get("origin_agent", ""),
                    "session_id": event.get("origin_sid") or "main",
                }
            self.pending_chains[event["chain_id"]] = chain_entry
        elif kind == "chain_update":
            # #4108: field-name-INDEPENDENT write-back — mirrors every
            # non-routing key the event actually carries, rather than
            # hardcoding "waiting_on" (which had two bugs: any OTHER field
            # a caller passed — e.g. proposal 0067 P8's ``arm_at`` — was
            # silently dropped from reconstruction, AND an update call that
            # did NOT pass waiting_on at all still overwrote it to `[]`,
            # destroying real state). Only touches keys the event actually
            # names; a key absent from THIS event is left as whatever a
            # PRIOR event already set. ``_CHAIN_EVENT_META_KEYS`` is every
            # routing/meta field ``SnapshotJournal``'s WAL-append chokepoint
            # adds that is NOT itself chain state.
            chain = self.pending_chains.get(event["chain_id"])
            if chain is not None:
                for key, value in event.items():
                    if key in _CHAIN_EVENT_META_KEYS:
                        continue
                    chain[key] = list(value) if key == "waiting_on" else value
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
        # (#2884 added a `hook_driven_turns_set` branch here — the loop-valve
        # counter's between-snapshot replay maintenance. #5561 retired it;
        # an old WAL still carrying the kind falls through to the "unknown
        # kinds: no-op" fallback below, same as #3436's own retirement.)
        # step_started/completed/failed mutate per-task snapshot only — no agent-level state change here.
        # Unknown kinds: no-op (forward compatibility for future kinds)
