"""ChatSession — long-lived chat loop driving the skill_router stdlib skill."""
from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reyn.agent import Agent
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.budget.budget import (
    BudgetTracker,
    format_budget_full,
    format_cost_line,
    format_refusal_message,
    format_warn_message,
)
from reyn.chat.services import ChainManager, InterventionRegistry, SnapshotJournal
from reyn.chat.services.chain_manager import _PendingChain
from reyn.compiler import load_dsl_skill
from reyn.compiler.parser import _split_frontmatter
from reyn.config import EventsConfig, LimitsConfig
from reyn.events.event_store import EventStore
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.permissions.permissions import PermissionResolver
from reyn.skill.skill_paths import resolve_skill_path, stdlib_root, SkillNotFoundError
from reyn.skill.skill_registry import SkillRegistry
from reyn.events.state_log import StateLog
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    UserIntervention,
    match_choice,
)
from reyn.chat.outbox import OutboxMessage


ROUTER_SKILL_NAME = "skill_router"
NARRATOR_SKILL_NAME = "skill_narrator"


class RouterCapExceeded(Exception):
    """Raised when a user turn (or top-level agent_request) drives more
    skill_router invocations than the configured cap. Caught by handlers,
    which surface a structured fallback reply to the user / requester."""

    def __init__(self, count: int, cap: int, last_reason: str = "") -> None:
        super().__init__(
            f"Router exhausted retry budget ({count}/{cap}) for this turn"
        )
        self.count = count
        self.cap = cap
        self.last_reason = last_reason


class ChatInterventionBus:
    """InterventionBus impl that routes through ChatSession's outbox/inbox.

    One instance per skill spawn — captures `run_id` and a default `skill_name`
    so the chat session can drop pending interventions when the spawn is
    cancelled. Interventions emitted by ops carry their own `skill_name` from
    `OpContext`; this bus only fills in `run_id` (which the OS layer doesn't
    have, since chat tracks runs separately from `Agent.run_id`).
    """

    def __init__(self, session: "ChatSession", run_id: str | None, skill_name: str | None) -> None:
        self._session = session
        self._run_id = run_id
        self._skill_name = skill_name

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        if iv.run_id is None:
            iv.run_id = self._run_id
        if not iv.skill_name:
            iv.skill_name = self._skill_name
        # PR-intervention-link L6: short-circuit if a previous (crashed-then-
        # restored) run's intervention was already answered post-restart.
        # The L5 watcher buffered the answer keyed by run_id; the resuming
        # skill's first ask_user picks it up here without dispatching a
        # duplicate prompt.
        if iv.run_id is not None:
            buffered = self._session._consume_buffered_intervention_answer(iv.run_id)
            if buffered is not None:
                return buffered
        return await self._session._dispatch_intervention(iv)

    # Note: _dispatch_intervention on session.py is now a thin wrapper around
    # InterventionRegistry.dispatch (wave 2 of PR-refactor-session-1). Kept
    # method-level call so the bus signature stays stable.


@dataclass
class ChatMessage:
    role: str  # "user" | "agent" | "skill_event" | "summary"
    text: str
    ts: str
    seq: int = 0  # monotonic per-session sequence id; 0 for non-conversational entries
    meta: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_short(run_id: str) -> str:
    """Last 4 chars of a chat-side run_id, used as a display tag."""
    return run_id[-4:] if run_id else ""


def _run_meta(run_id: str | None, skill_name: str | None) -> dict:
    """Standard `meta` payload for OutboxMessage produced inside a skill spawn."""
    if run_id is None:
        return {"skill_name": skill_name} if skill_name else {}
    return {
        "run_id": run_id,
        "run_id_short": _run_short(run_id),
        "skill_name": skill_name,
    }


def _new_chain_id() -> str:
    """Mint a fresh chain_id for a top-level user request. Each user submission
    starts a new chain; agent_request / agent_response payloads forward the
    chain_id they received without minting new ones."""
    return uuid.uuid4().hex


def _read_memory_index(path: Path) -> str:
    """Return MEMORY.md contents at `path` or empty string if absent."""
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _merge_memory_indexes(
    *, shared_path: Path, agent_path: Path, agent_name: str,
) -> dict:
    """Combine the shared and agent-scoped MEMORY.md files into a single
    `data.memory_index` payload (PR15).

    The router phase used to read `.reyn/memory/MEMORY.md` via a preprocessor
    `file/read` step; that step is removed because the agent-scoped path
    `.reyn/agents/<name>/memory/MEMORY.md` is dynamic and a static phase
    YAML cannot interpolate it. ChatSession synthesizes the merged view
    here and stuffs it directly into the artifact.

    The two layers are kept separate in the output markdown — `(shared)` and
    `(agent: <name>)` — so the LLM can decide which slug path to use when
    writing new memory entries.
    """
    shared = _read_memory_index(shared_path).strip()
    agent  = _read_memory_index(agent_path).strip()

    if not shared and not agent:
        return {"status": "not_found", "content": ""}

    parts: list[str] = []
    if shared:
        parts.append(f"# Memory Index (shared)\n\n{_strip_index_header(shared)}")
    else:
        parts.append("# Memory Index (shared)\n\n(empty)")
    parts.append(
        f"# Memory Index (agent: {agent_name})\n\n"
        f"{_strip_index_header(agent) if agent else '(empty)'}"
    )
    return {"status": "ok", "content": "\n\n".join(parts).strip() + "\n"}


def _strip_index_header(content: str) -> str:
    """Drop a leading `# Memory Index` heading (with optional trailing blank
    lines) from a stored MEMORY.md so we don't render two headings when
    merging. Anything else is returned verbatim."""
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("# Memory Index"):
        # Skip the heading and any immediately-following blank lines.
        i = 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        lines = lines[i:]
    return "\n".join(lines).strip()


# NOTE: `_PendingChain` lives in `reyn.chat.services.chain_manager` (PR-refactor-session-1
# wave 2). Kept import at top of file for backward-compat references.


def _iv_meta(iv: "UserIntervention") -> dict:
    """Standard `meta` payload for OutboxMessage announcing an intervention.

    Includes structured choice data so TUI renderers can build chip buttons
    without re-parsing the formatted text string.
    """
    out = {"intervention_id": iv.id, "intervention_kind": iv.kind}
    if iv.run_id:
        out["run_id"] = iv.run_id
        out["run_id_short"] = _run_short(iv.run_id)
    if iv.skill_name:
        out["skill_name"] = iv.skill_name
    if iv.choices:
        out["choices"] = [
            {"id": c.id, "label": c.label, "hotkey": c.hotkey}
            for c in iv.choices
        ]
    if iv.suggestions:
        out["suggestions"] = list(iv.suggestions)
    return out


def _render_summary_for_storage(structured: dict) -> str:
    """Render a chat_summary structured dict to a quick-display text blob.

    Stored in ChatMessage.text so REPL traces and audit dumps don't need
    to re-render the structured form. The slicer prefers the structured
    form for LLM consumption — this is for human consumption only.
    """
    parts: list[str] = []
    topic = (structured.get("topic_arc") or "").strip()
    if topic:
        parts.append(f"[topic] {topic}")
    for key in ("decisions", "pending", "session_user_facts", "artifacts_referenced"):
        items = structured.get(key) or []
        if not items:
            continue
        parts.append(f"[{key}]")
        parts.extend(f"  - {item}" for item in items)
    return "\n".join(parts)


def enumerate_available_skills(exclude: set[str]) -> list[dict]:
    """Walk reyn/project, reyn/local, stdlib/skills and collect skill catalogue entries.

    Each entry has `{name, description}` always, plus an optional `routing`
    block lifted from the skill's frontmatter. The router uses `routing.intents`,
    `routing.when_to_use`, `routing.when_not_to_use`, and `routing.examples`
    to decide whether the user's request matches the skill.
    """
    sl = stdlib_root()
    roots = [
        Path("reyn") / "project",
        Path("reyn") / "local",
        sl / "skills",
    ]
    seen: set[str] = set()
    results: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in seen or d.name in exclude:
                continue
            md = d / "skill.md"
            if not md.exists():
                continue
            try:
                fm, _ = _split_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            description = ""
            if fm.get("description"):
                description = str(fm["description"]).strip().splitlines()[0]
            entry: dict = {"name": fm.get("name") or d.name, "description": description}
            routing = fm.get("routing")
            if isinstance(routing, dict) and routing:
                entry["routing"] = routing
            results.append(entry)
            seen.add(d.name)
    return results


class ChatSession:
    def __init__(
        self,
        agent_name: str,
        model: str = "standard",
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        limits: LimitsConfig | None = None,
        mcp_servers: dict | None = None,
        output_language: str = "ja",
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        agent_role: str = "",
        compaction_config: "CompactionConfig | None" = None,
        registry: "AgentRegistry | None" = None,
        max_hop_depth: int = 3,
        chain_timeout_seconds: float = 60.0,
        allowed_skills: list[str] | None = None,
        allowed_mcp: list[str] | None = None,
        events_config: EventsConfig | None = None,
        state_log: StateLog | None = None,
        budget_tracker: BudgetTracker | None = None,
        snapshot_path: "Path | None" = None,
    ) -> None:
        """
        snapshot_path: optional override for the per-agent snapshot file
            location. Default: ``.reyn/agents/<agent_name>/state/snapshot.json``
            relative to the current working directory. Tests use this to
            redirect snapshot I/O to a tmp_path without touching private
            attributes.
        """
        self.agent_name = agent_name
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        self._limits = limits or LimitsConfig()
        self._mcp_servers = mcp_servers
        self.output_language = output_language
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context
        self._agent_role = agent_role
        # Optional back-reference for slash commands like /agents / /attach
        # and for agent-to-agent message routing (PR11). The factory in
        # cli/commands/chat.py wires this; tests can leave it None.
        self._registry = registry
        # PR11: max delegation hop depth (LangGraph-style). 0 = user input,
        # each `_send_to_agent` increments. Refuse send when depth > limit.
        self._max_hop_depth = max_hop_depth
        # PR18: per-chain wall-clock budget. Non-positive disables. When the
        # budget elapses, the runtime synthesizes an error response upstream
        # so a chain stuck on a non-responsive delegate doesn't hang forever.
        self._chain_timeout_seconds = chain_timeout_seconds
        # PR15: optional skill allowlist sourced from profile.allowed_skills.
        # None = unrestricted (default, BC). Empty list = router runs but no
        # skill spawn. stdlib router/compactor/narrator are NOT subject to
        # this — they're always available regardless.
        self._allowed_skills: list[str] | None = (
            list(allowed_skills) if allowed_skills is not None else None
        )
        # PR37: optional MCP server allowlist from agent profile. None = no
        # per-agent restriction (inherits project config). list[str] = only
        # these servers pass the per-agent check in require_mcp.
        self._allowed_mcp: list[str] | None = (
            list(allowed_mcp) if allowed_mcp is not None else None
        )

        # PR20: per-chat rotation policy. Defaults match EventsConfig.
        self._events_config = events_config or EventsConfig()

        # PR21: WAL + per-agent snapshot for crash recovery. state_log is
        # process-shared (owned by AgentRegistry); when None, persistence
        # is disabled (tests / non-chat invocation).
        # PR-refactor-session-1 wave 2: persistence now flows through
        # SnapshotJournal (extracted service). The session keeps the
        # snapshot_path here only because other init code references it
        # for diagnostic logging — the journal owns the actual I/O.
        self._snapshot_path = snapshot_path or (
            Path(".reyn") / "agents" / self.agent_name / "state" / "snapshot.json"
        )
        self._journal = SnapshotJournal(
            agent_name=self.agent_name,
            snapshot_path=self._snapshot_path,
            state_log=state_log,
        )
        # Track state_log directly for skill resume (PR-skill-resume): the
        # journal owns it for inbox / chain mutations, but skills launched
        # from this session also need it so dispatch_tool can emit step
        # events into the same WAL.
        self._state_log = state_log
        # PR-intervention-link L6: in-memory buffer of answers from
        # restored-then-resolved interventions, keyed by run_id. The first
        # bus.request from the resuming skill at that run_id consumes the
        # entry and returns it without re-dispatching. Persistence across
        # the (user_answered → process_crashed → skill_not_yet_resumed)
        # window is R-D12 follow-up.
        self._buffered_intervention_answers: dict[str, "InterventionAnswer"] = {}
        # Per-agent SkillRegistry — lazily constructed on first skill run.
        # Tracks active skill_run_ids and emits skill lifecycle events.
        # Truncation auto-trigger flows through registry.truncate_wal_if_eligible
        # when an AgentRegistry back-reference is wired (production path);
        # tests with registry=None see no truncation triggers (acceptable).
        self._skill_registry: SkillRegistry | None = None

        # PR22: budget / rate-limit tracker (process-shared). When None,
        # checks are noops and counters are not maintained.
        self._budget_tracker = budget_tracker

        # Per-turn cap on consecutive skill_router invocations. Prevents the
        # S4 dogfood runaway (16 router calls / 245k prompt tokens for one
        # user paste). The counter is reset at the top of each new turn
        # (`_handle_user_message` or `_handle_agent_request`); subsequent
        # in-chain re-invocations (agent_response continuation,
        # chain_resolve) accumulate against the same budget.
        if budget_tracker is not None:
            self._router_cap: int = int(
                getattr(
                    budget_tracker.config, "router_invocations_per_turn", 3
                )
            )
        else:
            self._router_cap = 3
        self._router_invocations_this_turn: int = 0
        self._router_last_reason: str = ""

        from reyn.config import CompactionConfig
        self._compaction = compaction_config or CompactionConfig()
        self._next_seq = 1
        self._compacting = False
        self._compaction_task: asyncio.Task | None = None

        # `agents/<name>/` is state-only as of PR20: profile / history /
        # memory / .input_history. Audit log lives under `events/`.
        self.workspace_dir = Path(".reyn") / "agents" / self.agent_name
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.workspace_dir / "history.jsonl"
        # PR20: chat events live at `events/agents/<name>/chat/<YYYY-MM>/...`.
        # The folder is created lazily by EventStore on first write.
        self.events_dir = (
            Path(".reyn") / "events" / "agents" / self.agent_name / "chat"
        )

        self.history: list[ChatMessage] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        # Detached by default — AgentRegistry.attach() flips this on. Outbox
        # `status`/`trace` emissions are dropped while detached so background
        # agents don't accumulate display noise.
        self.is_attached: bool = False

        from reyn.llm.pricing import TokenUsage
        self._total_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0

        self._event_store = EventStore(
            self.events_dir,
            max_bytes=self._events_config.max_bytes,
            max_age_seconds=self._events_config.max_age_seconds,
        )
        self._chat_events = EventLog(subscribers=[self._event_store])
        self.running_skills: dict[str, asyncio.Task] = {}
        # Per-run wall-clock start (monotonic) for `:list` elapsed-seconds display.
        self.running_skills_started_at: dict[str, float] = {}

        # PR-refactor-session-1 wave 2: pending-chain lifecycle and intervention
        # queue ownership extracted into services. The session orchestrates the
        # callbacks (_announce_intervention, _on_chain_timeout_fire) but holds
        # no state for them.
        self._chains = ChainManager(
            journal=self._journal,
            events=self._chat_events,
            chain_timeout_seconds=self._chain_timeout_seconds,
            max_hop_depth=self._max_hop_depth,
        )
        self._interventions = InterventionRegistry(
            on_announce=self._announce_intervention,
        )

        # F2: Delegation tracking for RouterLoop runs. Set to a list before
        # calling RouterLoop.run(); send_to_agent appends dispatched targets.
        # None when not inside a RouterLoop run (send_to_agent from old paths
        # does not accumulate). Cleared after each loop run.
        self._router_loop_delegations: list[dict] | None = None

        # F2: Agent-reply capture for agent-to-agent RouterLoop paths.
        # Set to [] before running RouterLoop in agent_request / chain_resolve
        # context; put_outbox appends "agent" kind text here so callers can
        # forward the reply upstream. None = not capturing (user-turn context).
        self._router_loop_agent_replies: list[str] | None = None

    # ── cost accumulation ───────────────────────────────────────────────────────

    def _accumulate(self, result) -> None:
        if result.token_usage is not None:
            self._total_usage += result.token_usage
        if result.cost_usd is not None:
            self._total_cost_usd += result.cost_usd

    @property
    def total_usage(self):
        return self._total_usage

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost_usd

    # ── persistence ─────────────────────────────────────────────────────────────

    def _append_history(self, msg: ChatMessage) -> None:
        # Assign monotonic seq for conversational entries (user/agent). Other
        # roles (skill_event, summary) keep seq=0 — they aren't part of the
        # turn ordering used by the slicer.
        if msg.role in ("user", "agent") and msg.seq == 0:
            msg.seq = self._next_seq
            self._next_seq += 1
        self.history.append(msg)
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

    def load_history(self) -> None:
        if not self.history_path.exists():
            return
        with self.history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.history.append(ChatMessage(**json.loads(line)))
                except Exception:
                    continue
        # Initialize the seq counter past any seqs already in the file. Old
        # entries without seq fall back to 0; the synthetic seq for them is
        # assigned by the slicer at read time, so we only care about the
        # max of explicitly-stored seqs here for the next-write counter.
        max_seen = max((m.seq for m in self.history if m.seq), default=0)
        self._next_seq = max_seen + 1

    # ── inbox API ───────────────────────────────────────────────────────────────

    async def submit_user_text(self, text: str) -> None:
        # PR14: every top-level user submission starts a fresh chain_id that
        # propagates through any agent_request / agent_response generated in
        # response. Logged in history meta + events.jsonl for cross-agent trace.
        await self._put_inbox(
            "user", {"text": text, "chain_id": _new_chain_id()},
        )

    async def submit_agent_request(
        self, *, from_agent: str, request: str, depth: int, chain_id: str,
    ) -> None:
        await self._put_inbox("agent_request", {
            "from_agent": from_agent, "request": request, "depth": depth,
            "chain_id": chain_id,
        })

    async def submit_agent_response(
        self, *, from_agent: str, response: str, depth: int, chain_id: str,
    ) -> None:
        await self._put_inbox("agent_response", {
            "from_agent": from_agent, "response": response, "depth": depth,
            "chain_id": chain_id,
        })

    async def shutdown(self) -> None:
        # `shutdown` is a control signal, not recovery state — skip WAL/snapshot.
        await self.inbox.put(("shutdown", {}))

    # ── PR21: state persistence helpers (WAL + snapshot) ─────────────────────
    # PR-refactor-session-1 wave 2: WAL/snapshot ownership moved to
    # SnapshotJournal; pending_chains lifecycle moved to ChainManager.
    # The methods below are thin delegators kept for the session-internal
    # call sites (inbox enqueue + dequeue, restoration orchestration).

    async def _put_inbox(self, kind: str, payload: dict) -> str:
        """Append `inbox_put` to WAL via journal, then queue on the async
        inbox. Returns the assigned message id (also stamped into payload
        as `_msg_id` so the consumer can look it up)."""
        msg_id = await self._journal.append_inbox(kind=kind, payload=payload)
        full_payload = {**payload, "_msg_id": msg_id}
        await self.inbox.put((kind, full_payload))
        return msg_id

    async def _consume_inbox(self) -> tuple[str, dict]:
        """Wait for next inbox message; on receive, record `inbox_consume`
        via journal (skipped for shutdown signals which are out-of-band)."""
        kind, payload = await self.inbox.get()
        msg_id = payload.get("_msg_id") if isinstance(payload, dict) else None
        if kind != "shutdown":
            await self._journal.consume_inbox(msg_id=msg_id)
        return kind, payload

    def restore_state(self, snapshot: AgentSnapshot) -> None:
        """Adopt a recovered snapshot: install in journal, repopulate the
        async inbox, restore pending chains via ChainManager (which re-arms
        timeout watchdogs), and re-enqueue outstanding interventions
        (PR-intervention-link L5) so the user can clear them after restart.

        Callable from async context only — restoration schedules asyncio
        tasks."""
        self._journal.install(snapshot)
        for msg in snapshot.inbox:
            self.inbox.put_nowait((msg["kind"], msg["payload"]))
        self._chains.restore(on_fire=self._on_chain_timeout_fire)
        # Re-enqueue interventions in FIFO insertion order (dict preserves
        # insertion order in py3.7+). Each restored iv gets a fresh future
        # and a watcher task so dispatch's finally clause fires
        # ``intervention_resolved`` to prune the snapshot when the user
        # answers.
        if snapshot.outstanding_interventions:
            restored = [
                UserIntervention.from_dict(iv_dict)
                for iv_dict in snapshot.outstanding_interventions.values()
            ]

            async def _on_restored_resolved(iv: UserIntervention) -> None:
                # Restored interventions DON'T re-emit ``intervention_dispatched``
                # (that event is already in the WAL from the original run).
                # We do TWO things here:
                #   1. Buffer the user's answer keyed by run_id so the
                #      resuming skill's first ask_user picks it up (L6).
                #   2. Emit ``intervention_resolved`` to prune the snapshot's
                #      outstanding_interventions entry.
                if iv.future.done() and iv.run_id:
                    try:
                        answer = iv.future.result()
                    except (asyncio.CancelledError, Exception):
                        answer = None
                    if answer is not None:
                        self._buffered_intervention_answers[iv.run_id] = answer
                await self._journal.record_intervention_resolved(
                    intervention_id=iv.id,
                )

            self._restore_intervention_tasks = self._interventions.restore(
                restored, watcher=_on_restored_resolved,
            )
        self._chat_events.emit(
            "session_restored",
            applied_seq=snapshot.applied_seq,
            inbox_size=len(snapshot.inbox),
            pending_chains=len(snapshot.pending_chains),
            outstanding_interventions=len(snapshot.outstanding_interventions),
        )

    # ── main loop ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._chat_events.emit("chat_started", agent_name=self.agent_name, model=self.model)

        try:
            while True:
                kind, payload = await self._consume_inbox()
                if kind == "shutdown":
                    break
                if kind == "user":
                    await self._handle_user_message(
                        payload.get("text", ""),
                        chain_id=payload.get("chain_id") or _new_chain_id(),
                    )
                elif kind == "agent_request":
                    await self._handle_agent_request(payload)
                elif kind == "agent_response":
                    await self._handle_agent_response(payload)
        finally:
            await self._drain_on_shutdown()
            self._chat_events.emit("chat_stopped", agent_name=self.agent_name)
            await self._put_outbox(OutboxMessage(kind="__end__", text=""))

    async def _drain_on_shutdown(self) -> None:
        """Cancel any in-flight user-initiated skill runs and await compaction.

        Memory writes happen inline during each router turn, so there is no
        background extraction to drain — shutdown is now strictly a teardown
        of whatever the user explicitly launched, plus a final await on the
        compaction task (if any) so the summary entry gets persisted before
        the process exits.
        """
        for task in self.running_skills.values():
            task.cancel()
        if self.running_skills:
            await asyncio.gather(*self.running_skills.values(), return_exceptions=True)

        # PR18: cancel any pending chain-timeout watchdogs so they don't keep
        # the loop alive past shutdown. Late-firing timers swallow their work
        # (the pending entry is gone) but cancellation is cleaner.
        # PR-refactor-session-1 wave 2: cancellation delegated to ChainManager.
        await self._chains.shutdown()

        if self._compaction_task is not None and not self._compaction_task.done():
            try:
                await self._compaction_task
            except Exception as exc:
                logger.warning("compaction task failed during shutdown: %s", exc)
                self._chat_events.emit("compaction_failed", error=str(exc), phase="shutdown")

    async def _handle_user_message(self, text: str, *, chain_id: str) -> None:
        # Slash commands (`/list`, `/cancel <id>`, `/answer <id> <text>`) take
        # precedence over both the active-intervention router and a fresh
        # router turn.
        if text.startswith("/"):
            if await self._maybe_handle_slash(text):
                return
        # If a spawned skill is waiting on a user intervention (ask_user or
        # permission prompt), route this input to that intervention instead of
        # starting a fresh router turn.
        if await self._maybe_answer_oldest_intervention(text):
            return

        self._append_history(ChatMessage(
            role="user", text=text, ts=_now_iso(),
            meta={"chain_id": chain_id},
        ))
        self._chat_events.emit("user_message_received", text=text, chain_id=chain_id)
        await self._put_outbox(OutboxMessage(
            kind="status", text="thinking…", meta={"chain_id": chain_id},
        ))

        # Reset the per-turn router cap counter at the top of each fresh
        # user turn. Subsequent in-chain re-invocations (agent_response on
        # this chain, _resolve_pending_chain) accumulate against the same
        # budget without resetting.
        self._reset_router_turn_counter()

        try:
            await self._run_router_loop(text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed: {exc}",
                meta={"chain_id": chain_id},
            ))
            return

        # Fire-and-forget compaction check after the user has the reply.
        # Reuses self._compacting as a single-flight lock; no await here so
        # the user's next prompt isn't blocked. _drain_on_shutdown awaits any
        # in-flight compaction task so a quick /quit after a heavy turn does
        # not lose the summary.
        if self._compaction_task is None or self._compaction_task.done():
            self._compaction_task = asyncio.create_task(self._maybe_compact())

    # ── skill invocation helpers ────────────────────────────────────────────────

    async def _auto_resume_active_skills(
        self,
        *,
        coordinator: "SkillResumeCoordinator | None" = None,
        config: "SkillResumeConfig | None" = None,
        launcher: "Callable[[Any], Awaitable[None]] | None" = None,
    ) -> list:
        """Discover active skill runs, apply resume policy, launch tasks.

        Headline UX of PR-resume-auto: after restore_state rehydrates
        the agent's snapshot + WAL, this auto-launches resume tasks for
        every still-active skill_run with no interactive prompt.

        Algorithm:
          1. Discover active runs via SkillResumeCoordinator
             (per-skill SkillSnapshot files + WAL)
          2. For each, build a ResumePlan and apply the operator's
             ``reyn.yaml`` policy (default = retry)
          3. ``discard`` decisions: call SkillRegistry.complete +
             drop pending interventions (no task launched)
          4. All other decisions: invoke ``launcher(decision)`` so
             the caller (production = ``self._spawn_resumed_skill``)
             can wire the actual asyncio task

        ``launcher`` is dependency-injected so tests can inspect
        decisions without launching real skill runtimes. Production
        callers pass None to use the default launcher.

        Returns the list of decisions that were launched (= decisions
        minus discards).
        """
        from reyn.config import SkillResumeConfig as _Config
        from reyn.skill.skill_resume_coordinator import (
            SkillResumeCoordinator as _Coord,
        )
        coord = coordinator or _Coord()
        cfg = config or _Config()
        registry = self._get_skill_registry()
        if registry is None or self._state_log is None:
            return []
        decisions = coord.discover_and_decide(
            skill_registry=registry,
            state_log=self._state_log,
            policy=cfg,
        )
        if not decisions:
            return []
        remaining = await coord.apply_decisions(
            decisions, skill_registry=registry,
            drop_interventions_for_run=self._drop_interventions_for_run,
        )
        actual_launcher = launcher or self._spawn_resumed_skill
        for decision in remaining:
            await actual_launcher(decision)
        return remaining

    async def _spawn_resumed_skill(self, decision: "Any") -> None:
        """Default launcher used by ``_auto_resume_active_skills``.

        Loads the skill by name from the resume plan, builds an Agent,
        and spawns ``Agent.run`` as a tracked asyncio task with the
        resume_plan threaded in. Exists as a separate method so the
        auto-resume hook can be tested with a stub launcher (see
        ``tests/test_session_auto_resume.py``).
        """
        plan = decision.plan
        skill_name = plan.skill_name
        run_id = plan.run_id
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, dsl_root = resolve_skill_path(skill_name)
            skill = load_dsl_skill(
                str(skill_dir / "skill.md"), dsl_root=str(dsl_root),
            )
        except (SkillNotFoundError, Exception) as exc:
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"resume failed to load: {exc}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"resume failed: {exc}", meta=meta,
            ))
            return

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox, run_id=run_id)],
        )

        async def _runner():
            try:
                await agent.run(
                    skill, plan.skill_input,
                    output_language=self.output_language,
                    skill_registry=self._get_skill_registry(),
                    state_log=self._state_log,
                    resume_plan=plan,
                    run_id=run_id,
                )
            except asyncio.CancelledError:
                await self._put_outbox(OutboxMessage(
                    kind="status", text="cancelled", meta=meta,
                ))
                raise
            except Exception as exc:  # noqa: BLE001 — surface to outbox
                self._chat_events.emit(
                    "skill_run_failed", run_id=run_id, skill=skill_name,
                    error=str(exc),
                )
                await self._put_outbox(OutboxMessage(
                    kind="error", text=f"resume failed: {exc}", meta=meta,
                ))

        self.running_skills_started_at[run_id] = time.monotonic()
        await self._put_outbox(OutboxMessage(
            kind="status", text="resuming…", meta=meta,
        ))
        task = asyncio.create_task(_runner())
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

    def _get_skill_registry(self) -> "SkillRegistry | None":
        """Return the per-agent SkillRegistry, lazily constructed on first call.

        Returns None when no state_log is wired (test / standalone mode) —
        with no WAL to write to, the registry would be a no-op anyway.

        The truncate-eligible hook closes over the back-reference to the
        owning AgentRegistry; if `registry` is None (test fixtures that
        don't construct a full process tree), the hook is None and
        ``advance_phase`` / ``complete`` skip the truncation trigger. This
        keeps truncation a production concern, not a test concern.
        """
        if self._state_log is None:
            return None
        if self._skill_registry is None:
            agent_state_dir = (
                Path(".reyn") / "agents" / self.agent_name / "state"
            )
            hook = None
            if self._registry is not None:
                # Bind self._registry into a hook that fires after every
                # ``skill_phase_advanced`` / ``skill_completed``. Throttle
                # + floor calc happen inside truncate_wal_if_eligible.
                async def _truncate_hook() -> None:
                    if self._registry is not None:
                        await self._registry.truncate_wal_if_eligible()
                hook = _truncate_hook
            self._skill_registry = SkillRegistry(
                agent_name=self.agent_name,
                agent_state_dir=agent_state_dir,
                state_log=self._state_log,
                truncate_eligible_hook=hook,
            )
        return self._skill_registry

    def _build_agent(
        self,
        *,
        intervention_bus: InterventionBus | None = None,
        mcp_servers: dict | None = None,
        subscribers: list | None = None,
    ) -> Agent:
        """Construct an Agent with this session's shared defaults applied."""
        return Agent(
            model=self.model,
            resolver=self._resolver,
            permission_resolver=self._perm,
            limits=self._limits,
            mcp_servers=mcp_servers,
            intervention_bus=intervention_bus,
            subscribers=subscribers,
            prompt_cache_enabled=self._prompt_cache_enabled,
            project_context=self._project_context,
            agent_role=self._agent_role,
            caller=f"agents/{self.agent_name}",
            budget_tracker=self._budget_tracker,
        )

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        """Drop transient kinds while detached; durable kinds are queued.

        While `is_attached=False` (PR10 multi-agent: agent running in the
        background), `status`/`trace` carry no value to a detached display
        and would just accumulate in the queue. `agent`/`skill_done`/
        `intervention`/`error`/`__end__` are kept so they reach the user
        when re-attached or remain in history (history append happens
        independently in callers).
        """
        if not self.is_attached and msg.kind in {"status", "trace"}:
            return
        await self.outbox.put(msg)

    def _load_stdlib_skill(self, skill_name: str):
        """Load a stdlib skill by its directory name. Propagates parse errors."""
        sl = stdlib_root()
        skill_md = sl / "skills" / skill_name / "skill.md"
        return load_dsl_skill(str(skill_md), dsl_root=str(sl))

    async def _run_stdlib_skill(
        self,
        skill_name: str,
        input_artifact: dict,
        *,
        state_subdir: str,
        mcp_servers: dict | None = None,
        forward_events: bool = False,
    ):
        """Load a stdlib skill, build an Agent under workspace/<state_subdir>, run it.

        When `forward_events` is True, phase_started/phase_completed events
        from this run are surfaced as `trace` messages on the chat outbox so
        the user sees progress between LLM hops. Off by default to keep
        memory/admin runs silent unless the caller opts in.

        Returns the RunResult. Callers handle exceptions.
        """
        skill = self._load_stdlib_skill(skill_name)
        subscribers = None
        if forward_events:
            from reyn.chat.forwarder import ChatEventForwarder
            subscribers = [ChatEventForwarder(skill_name, self.outbox)]
        # Inline stdlib runs (router/compactor) aren't tracked in running_skills,
        # so run_id is None — _drop_interventions_for_run won't fire on them
        # (they complete on their own, no cancellation path).
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id=None, skill_name=skill_name),
            mcp_servers=mcp_servers,
            subscribers=subscribers,
        )
        result = await agent.run(skill, input_artifact, output_language=self.output_language)
        self._accumulate(result)
        return result

    # ── compaction (Head/Body/Tail) ─────────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Cheap chars/4 token estimate. Same heuristic used by other Reyn paths."""
        return max(1, len(text or "") // 4)

    def _latest_summary(self) -> ChatMessage | None:
        for m in reversed(self.history):
            if m.role == "summary":
                return m
        return None

    async def _maybe_compact(self) -> None:
        """Fold the uncovered middle into a structured summary when token-heavy.

        Trigger: estimated tokens of user/agent turns whose seq is BOTH
          - > head_size (those are HEAD, never compacted)
          - > latest_summary.covers_through_seq (already covered)
          - <= max_seq - tail_size (TAIL is preserved as raw)
        exceeds compaction.trigger_total_tokens and contains at least
        `min_compact_batch` turns.
        """
        if self._compacting:
            self._chat_events.emit("compaction_check", outcome="already_running")
            return
        cfg = self._compaction
        turns = [m for m in self.history if m.role in ("user", "agent")]
        if len(turns) <= cfg.head_size + cfg.tail_size:
            self._chat_events.emit(
                "compaction_check", outcome="too_few_turns",
                turns=len(turns), head=cfg.head_size, tail=cfg.tail_size,
            )
            return

        latest = self._latest_summary()
        prev_cover = (latest.meta or {}).get("covers_through_seq", 0) if latest else 0
        cover_floor = max(prev_cover, cfg.head_size)

        max_seq = max((t.seq for t in turns), default=0)
        tail_threshold = max_seq - cfg.tail_size
        candidates = [t for t in turns if cover_floor < t.seq <= tail_threshold]
        if len(candidates) < cfg.min_compact_batch:
            self._chat_events.emit(
                "compaction_check", outcome="below_min_batch",
                candidate_count=len(candidates), min_batch=cfg.min_compact_batch,
            )
            return

        total_tokens = sum(self._estimate_tokens(t.text) for t in candidates)
        if total_tokens < cfg.trigger_total_tokens:
            self._chat_events.emit(
                "compaction_check", outcome="below_threshold",
                total_tokens=total_tokens, threshold=cfg.trigger_total_tokens,
                candidate_count=len(candidates),
            )
            return
        self._chat_events.emit(
            "compaction_check", outcome="triggering",
            total_tokens=total_tokens, candidate_count=len(candidates),
        )

        self._compacting = True
        try:
            await self._run_compaction(candidates, latest)
        except Exception as exc:
            self._chat_events.emit("compaction_failed", error=str(exc))
        finally:
            self._compacting = False

    async def _run_compaction(
        self,
        candidates: list[ChatMessage],
        previous_summary: ChatMessage | None,
    ) -> None:
        """Invoke chat_compactor and persist the resulting summary entry."""
        cfg = self._compaction
        prev_structured: dict | None = None
        if previous_summary is not None:
            meta = previous_summary.meta or {}
            structured = meta.get("structured")
            if isinstance(structured, dict):
                prev_structured = structured
                # carry forward the prior covers_through_seq for continuity
                if "covers_through_seq" not in prev_structured:
                    prev_structured = {
                        **prev_structured,
                        "covers_through_seq": meta.get("covers_through_seq", 0),
                    }

        input_artifact = {
            "type": "history_chunk_to_compact",
            "data": {
                "previous_summary": prev_structured,
                "new_turns": [
                    {"role": t.role, "text": t.text, "seq": t.seq} for t in candidates
                ],
                "section_token_caps": {
                    "topic_arc": cfg.section_token_caps.topic_arc,
                    "decisions": cfg.section_token_caps.decisions,
                    "pending": cfg.section_token_caps.pending,
                    "session_user_facts": cfg.section_token_caps.session_user_facts,
                    "artifacts_referenced": cfg.section_token_caps.artifacts_referenced,
                },
            },
        }

        self._chat_events.emit(
            "compaction_started",
            new_turn_count=len(candidates),
            covers_through_seq=candidates[-1].seq,
            had_previous=previous_summary is not None,
        )
        result = await self._run_stdlib_skill(
            "chat_compactor", input_artifact, state_subdir="compaction",
        )
        if not result.ok:
            self._chat_events.emit(
                "compaction_aborted", reason=f"compactor result status={result.status}",
            )
            return

        structured = dict(result.data or {})
        covers = int(structured.get("covers_through_seq") or candidates[-1].seq)
        # Render once for the persisted text field; the slicer can re-render
        # from `structured` if the stored text drifts from formatting changes.
        rendered = _render_summary_for_storage(structured)

        summary_msg = ChatMessage(
            role="summary",
            text=rendered,
            ts=_now_iso(),
            meta={"structured": structured, "covers_through_seq": covers},
        )
        self._append_history(summary_msg)
        self._chat_events.emit(
            "compaction_completed",
            covers_through_seq=covers,
            section_lengths={k: len(v) if isinstance(v, list) else len(str(v))
                             for k, v in structured.items() if k != "covers_through_seq"},
        )

    # ── router ──────────────────────────────────────────────────────────────────

    async def _emit_router_cap_exhausted_user(
        self, exc: "RouterCapExceeded", *, chain_id: str,
    ) -> None:
        """User-facing fallback when the per-turn router cap is reached.
        Emits a structured error + a polite agent reply on the outbox so
        the chat loop recovers cleanly. The underlying event was already
        emitted by `_check_and_increment_router_cap`."""
        await self._put_outbox(OutboxMessage(
            kind="error",
            text=(
                f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                f"for this turn. Last reason: "
                f"{exc.last_reason or '(none)'}. Falling back to direct reply."
            ),
            meta={"chain_id": chain_id},
        ))
        fallback = (
            "I couldn't find a way to handle that within this turn's "
            "routing budget. Please try rephrasing or breaking the request "
            "into smaller pieces."
        )
        await self._put_outbox(OutboxMessage(
            kind="agent", text=fallback, meta={"chain_id": chain_id},
        ))
        self._append_history(ChatMessage(
            role="agent", text=fallback, ts=_now_iso(),
            meta={
                "chain_id": chain_id,
                "source": "router_cap_exhausted",
            },
        ))

    def _reset_router_turn_counter(self) -> None:
        """Reset the per-turn router invocation counter. Called at the top
        of each fresh turn (`_handle_user_message`, `_handle_agent_request`).
        Re-entrant in-chain paths (`_handle_agent_response` continuation,
        `_resolve_pending_chain`) intentionally do NOT reset — their
        invocations count against the same budget."""
        self._router_invocations_this_turn = 0
        self._router_last_reason = ""

    def _check_and_increment_router_cap(self, user_text: str) -> None:
        """Increment the per-turn router invocation counter and enforce the
        cap. Raises RouterCapExceeded when the counter would exceed the
        configured cap. cap=0 disables the check.
        """
        if self._router_cap <= 0:
            return
        # If we're already at the cap, the next attempt is rejected.
        if self._router_invocations_this_turn >= self._router_cap:
            count = self._router_invocations_this_turn
            self._chat_events.emit(
                "router_retry_exhausted",
                user_message=user_text[:200],
                count=count,
                cap=self._router_cap,
                last_reason=self._router_last_reason,
            )
            raise RouterCapExceeded(
                count=count,
                cap=self._router_cap,
                last_reason=self._router_last_reason,
            )
        self._router_invocations_this_turn += 1

    async def _invoke_narrator(
        self, skill_name: str, status: str, result: dict, state_subdir: str,
    ) -> str | None:
        """Run skill_narrator on a finished skill spawn; return reply_text.

        Returns None on narration failure (e.g. lint error, LLM exception);
        the caller's fallback raw-dump path takes over.
        """
        input_artifact = {
            "type": "narration_request",
            "data": {"skill": skill_name, "status": status, "result": result},
        }
        try:
            run_result = await self._run_stdlib_skill(
                NARRATOR_SKILL_NAME, input_artifact, state_subdir=state_subdir,
                forward_events=False,  # narrator is one phase, no need to surface
            )
        except Exception as exc:
            logger.warning("narrator skill failed for %r (%s): %s", skill_name, status, exc)
            self._chat_events.emit("narrator_failed", skill=skill_name, status=status, error=str(exc))
            return None
        if not run_result.ok:
            return None
        text = (run_result.data or {}).get("reply_text")
        return (text or "").strip() or None

    # ── intervention routing ─────────────────────────────────────────────────────

    async def _maybe_answer_oldest_intervention(self, text: str) -> bool:
        """If any intervention is pending, deliver `text` to the oldest and
        return True. Stale heads are evicted by the registry on `head()`."""
        head = self._interventions.head()
        if head is None:
            return False
        return await self._deliver_answer_to(head, text)

    async def _deliver_answer_to(self, iv: UserIntervention, text: str) -> bool:
        """Resolve `iv` with `text`, append a user-history entry, emit the
        `user_answered_intervention` event.

        Wraps `InterventionRegistry.deliver_answer` with the session-level
        side effects (history + audit event + unknown-choice hint). Returns
        True when the user input was consumed (answer set OR unrecognized
        choice hint emitted, both of which suppress a fresh router turn).
        """
        if iv.future.done():
            return False
        resolved = await self._interventions.deliver_answer(iv, text)
        if not resolved and iv.choices:
            # No-match path: surface hint, but consume the input so the
            # router doesn't run on a stray hotkey-attempt.
            hint = " / ".join(c.label for c in iv.choices)
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"unknown choice; expected one of: {hint}",
                meta=_iv_meta(iv),
            ))
            return True
        if not resolved:
            return False
        # Successfully resolved: append history + emit audit event.
        choice = match_choice(text, iv.choices) if iv.choices else None
        self._append_history(ChatMessage(
            role="user", text=text, ts=_now_iso(),
            meta={
                "answered_skill": iv.skill_name or "",
                "answered_run_id": iv.run_id or "",
                "intervention_id": iv.id,
                "intervention_kind": iv.kind,
            },
        ))
        self._chat_events.emit(
            "user_answered_intervention",
            intervention_id=iv.id,
            kind=iv.kind,
            run_id=iv.run_id,
            skill=iv.skill_name,
            choice_id=choice.id if choice else None,
            answer_text=text if not iv.choices else "",
        )
        return True

    async def _announce_intervention(self, iv: UserIntervention) -> None:
        """Format and publish an intervention to the outbox for the renderer.

        Skill / run_id provenance lives in `meta` — the renderer prepends a
        `[skill#abcd]` tag, so we don't repeat it in `text`.
        """
        lines: list[str] = []
        if iv.kind == "ask_user":
            lines.append(f"Question: {iv.prompt}")
        else:
            lines.append(iv.prompt)
        if iv.detail:
            lines.append(f"  {iv.detail}")
        if iv.suggestions:
            lines.append(f"  options: {' / '.join(iv.suggestions)}")
        if iv.choices:
            labels = " / ".join(c.label for c in iv.choices)
            lines.append(f"  {labels}")
        await self._put_outbox(OutboxMessage(
            kind="intervention",
            text="\n".join(lines),
            meta=_iv_meta(iv),
        ))

    async def _dispatch_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Register an intervention via the registry. Emits a "queued" status
        when the registry already has pending entries — the registry itself
        only auto-announces the head intervention.

        Wraps `InterventionRegistry.dispatch` with the session-level
        "awaiting answer (N queued)" UX hint and the WAL persistence step
        (PR-intervention-link L3) so a crash mid-await leaves the dispatch
        on disk for resume to re-enqueue.
        """
        # Persist BEFORE awaiting so a crash mid-await leaves the WAL
        # with the dispatch event. UserIntervention.to_dict excludes the
        # volatile future field.
        await self._journal.record_intervention_dispatched(
            intervention_id=iv.id, iv_dict=iv.to_dict(),
        )
        # Pre-emit the queued-status hint when this iv won't be the head.
        if not self._interventions.is_empty():
            queued = self._interventions.queued_count()
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"awaiting answer ({queued} queued)",
                meta=_iv_meta(iv),
            ))
        try:
            return await self._interventions.dispatch(iv)
        finally:
            # Resolve event covers all exit paths (answered, cancelled,
            # task abort). Idempotent in the journal so duplicate cleanup
            # via _drop_interventions_for_run is safe.
            await self._journal.record_intervention_resolved(
                intervention_id=iv.id,
            )

    def _drop_interventions_for_run(self, run_id: str | None) -> None:
        """Cancel any pending interventions tagged with `run_id`.

        The registry's drop cancels the futures; ``_dispatch_intervention``'s
        finally clause then fires ``intervention_resolved`` to the WAL for
        each cancelled coroutine, so the snapshot's
        ``outstanding_interventions`` is pruned correctly.
        """
        self._interventions.drop_for_run(run_id)
        # Also clear any buffered answer for this run — the run is gone,
        # nothing should consume the answer (L6).
        if run_id is not None:
            self._buffered_intervention_answers.pop(run_id, None)

    def _consume_buffered_intervention_answer(
        self, run_id: str,
    ) -> "InterventionAnswer | None":
        """Pop and return the buffered answer for ``run_id`` if any.

        PR-intervention-link L6 — used by ChatInterventionBus.request to
        short-circuit dispatch when a previous (crashed-then-restored)
        run's intervention was already answered post-restart.
        """
        return self._buffered_intervention_answers.pop(run_id, None)

    # ── agent-to-agent messaging (PR11 / PR14) ──────────────────────────────────

    async def _send_to_agent(
        self, *, to: str, request: str, depth: int, chain_id: str,
    ) -> None:
        """Route a delegation request from this agent to `to`.

        depth is the hop count from the original user request (user → A = 1,
        A → B = 2, ...). Refused when depth > max_hop_depth (LangGraph-style
        guard, default 3). chain_id (PR14) identifies the logical request
        thread for cross-agent trace; it propagates verbatim to the target's
        inbox payload and is recorded in history meta + events.
        """
        if depth > self._max_hop_depth:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"agent message depth {depth} exceeds limit "
                    f"{self._max_hop_depth}; chain refused"
                ),
                meta={"chain_id": chain_id},
            ))
            self._chat_events.emit(
                "agent_message_refused",
                reason="max_hop_depth",
                to_agent=to, depth=depth, chain_id=chain_id,
            )
            return
        if to == self.agent_name:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"agent {to!r}: cannot self-message",
                meta={"chain_id": chain_id},
            ))
            return
        if self._registry is None or not self._registry.exists(to):
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"agent {to!r} not found",
                meta={"chain_id": chain_id},
            ))
            return
        # PR12: topology gate. Defense in depth alongside the
        # `iter_reachable_agents` filter that hides unreachable agents from
        # the router LLM in the first place.
        if not self._registry.permit(self.agent_name, to):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"agent {to!r}: blocked by topology rules",
                meta={"chain_id": chain_id},
            ))
            return

        # Boot the target session if not yet loaded so its session.run() is
        # ready to consume the inbox put. attach() handles task creation
        # idempotently.
        target = self._registry.get_or_load(to)
        await self._registry.ensure_running(to)

        # Sender-side audit: A's history records the delegation outgoing.
        self._append_history(ChatMessage(
            role="agent", text=request, ts=_now_iso(),
            meta={
                "source": "agent_request_outgoing",
                "to_agent": to, "depth": depth, "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "agent_message_sent",
            kind="agent_request",
            from_agent=self.agent_name, to_agent=to,
            depth=depth, chain_id=chain_id,
        )
        await target.submit_agent_request(
            from_agent=self.agent_name, request=request,
            depth=depth, chain_id=chain_id,
        )

    async def _send_agent_response(
        self, *, to: str, response: str, depth: int, chain_id: str,
    ) -> None:
        """Route a reply from this agent back to the requester `to`.

        depth is propagated from the original request (B replying to A's
        depth-1 request stays at depth 1; A's next hop will increment).
        Empty response is still sent so chains never silently stall.
        chain_id (PR14) carries the same value the original request did so
        the requester can correlate the reply with its pending chain.
        """
        if depth > self._max_hop_depth:
            return  # silently drop — sender already gave up the chain
        if self._registry is None or not self._registry.exists(to):
            return
        target = self._registry.get_or_load(to)
        await self._registry.ensure_running(to)
        self._chat_events.emit(
            "agent_message_sent",
            kind="agent_response",
            from_agent=self.agent_name, to_agent=to,
            depth=depth, chain_id=chain_id,
        )
        await target.submit_agent_response(
            from_agent=self.agent_name, response=response,
            depth=depth, chain_id=chain_id,
        )

    async def _handle_agent_request(self, payload: dict) -> None:
        """Process an incoming agent_request.

        PR14 deferred-reply path: if the router emits `messages_to_agents`
        (= this agent wants to consult others before answering), the reply
        to the requester is held back. A `_PendingChain` entry is created
        keyed by chain_id; when every delegated agent has responded, the
        router runs again with all replies in history and the synthesized
        reply_text is finally sent upstream.

        If no delegations are emitted, behavior matches PR11: send the
        router's reply_text (or empty) right back to the requester.
        """
        from_agent = payload.get("from_agent", "")
        request = payload.get("request", "")
        depth = int(payload.get("depth", 1))
        chain_id = payload.get("chain_id") or _new_chain_id()

        # Receiver-side audit
        self._append_history(ChatMessage(
            role="user", text=request, ts=_now_iso(),
            meta={
                "source": "agent_request",
                "from_agent": from_agent, "depth": depth,
                "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "agent_request_received",
            from_agent=from_agent, depth=depth, chain_id=chain_id,
        )

        # Reset the per-turn router cap counter — an inbound agent_request
        # is a fresh top-level entry into this agent's loop.
        self._reset_router_turn_counter()

        # Arm delegation and reply capture before running RouterLoop.
        self._router_loop_delegations = []
        self._router_loop_agent_replies = []
        try:
            await self._run_router_loop(request, chain_id)
        except RouterCapExceeded as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                    f"for incoming agent_request from {from_agent!r}. "
                    f"Last reason: {exc.last_reason or '(none)'}."
                ),
                meta={"chain_id": chain_id, "from_agent": from_agent},
            ))
            await self._send_agent_response(
                to=from_agent, response="", depth=depth, chain_id=chain_id,
            )
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_request): {exc}",
                meta={"chain_id": chain_id},
            ))
            # Even on failure, send empty response so the requester chain
            # doesn't stall waiting forever.
            await self._send_agent_response(
                to=from_agent, response="", depth=depth, chain_id=chain_id,
            )
            return
        finally:
            dispatched = list(self._router_loop_delegations or [])
            agent_replies = list(self._router_loop_agent_replies or [])
            self._router_loop_delegations = None
            self._router_loop_agent_replies = None

        if dispatched:
            # PR14 deferred path: RouterLoop called send_to_agent for one or
            # more peers. Register a pending chain so the reply is held until
            # all delegates respond. ChainManager persists via the journal +
            # arms the timeout watchdog.
            waiting_on = {d["to"] for d in dispatched}
            await self._chains.register(
                chain_id=chain_id,
                from_user=False,
                depth=depth,
                original_text=request,
                sender=from_agent,
                waiting_on=waiting_on,
                origin_agent=from_agent,
                origin_depth=depth,
            )
            self._chains.arm_timeout(
                chain_id, on_fire=self._on_chain_timeout_fire,
            )
            return

        # PR11-compatible single-hop reply path. RouterLoop emitted reply_text
        # via put_outbox → captured in agent_replies. Forward upstream.
        reply_text = agent_replies[0] if agent_replies else ""
        # Note: history was already appended by put_outbox; add routing meta.
        await self._send_agent_response(
            to=from_agent, response=reply_text, depth=depth, chain_id=chain_id,
        )

    async def _handle_agent_response(self, payload: dict) -> None:
        """Process an incoming agent_response.

        Two branches:
        - chain_id ∈ self._chains → multi-hop relay. Drop sender
          from waiting_on; when waiting_on becomes empty, re-invoke router
          and forward the synthesized reply (or fresh delegations) on the
          same chain. Reply goes to the chain's `origin_agent`, NOT
          `from_agent`.
        - chain_id ∉ self._chains → user-initiated chain (PR11
          compatibility). The router's reply_text is treated as a
          user-facing message (outbox + history); further delegations
          continue with depth+1 on the same chain_id.
        """
        from_agent = payload.get("from_agent", "")
        response = payload.get("response", "")
        depth = int(payload.get("depth", 1))
        chain_id = payload.get("chain_id") or _new_chain_id()

        self._append_history(ChatMessage(
            role="user", text=response, ts=_now_iso(),
            meta={
                "source": "agent_response",
                "from_agent": from_agent, "depth": depth,
                "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "agent_response_received",
            from_agent=from_agent, depth=depth, chain_id=chain_id,
        )

        pending = self._chains.get(chain_id)
        if pending is not None:
            await self._resolve_pending_chain(
                pending, from_agent=from_agent,
            )
            return

        # User-initiated chain: PR11 path, reply goes to user.
        try:
            await self._run_router_loop(response, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_response): {exc}",
                meta={"chain_id": chain_id},
            ))
            return

    async def _resolve_pending_chain(
        self, pending: "_PendingChain", *, from_agent: str,
    ) -> None:
        """Drive a multi-hop pending chain forward by one delegate response.

        Drops `from_agent` from `pending.waiting_on`. If others remain,
        no-op (the chain is still gathering replies). Otherwise, re-runs
        the router on the original request — by now every delegate's
        response is appended to history, so the LLM has all the material
        to compose a synthesized answer (or decide on more delegations,
        which keeps the chain pending).
        """
        chain_id = pending.chain_id
        pending.waiting_on.discard(from_agent)
        if pending.waiting_on:
            # Partial resolution — record the new waiting_on for recovery.
            await self._chains.update(chain_id, waiting_on=pending.waiting_on)
            return  # still waiting on other delegates

        # Arm delegation and reply capture for the re-run.
        self._router_loop_delegations = []
        self._router_loop_agent_replies = []
        try:
            await self._run_router_loop(pending.original_request, chain_id)
        except RouterCapExceeded as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                    f"resolving chain {chain_id}. "
                    f"Last reason: {exc.last_reason or '(none)'}."
                ),
                meta={"chain_id": chain_id},
            ))
            await self._send_agent_response(
                to=pending.origin_agent, response="",
                depth=pending.origin_depth, chain_id=chain_id,
            )
            await self._chains.resolve(chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"router failed (chain resolve): {exc}",
                meta={"chain_id": chain_id},
            ))
            # Send empty upstream so the parent chain doesn't hang.
            await self._send_agent_response(
                to=pending.origin_agent, response="",
                depth=pending.origin_depth, chain_id=chain_id,
            )
            await self._chains.resolve(chain_id)
            return
        finally:
            new_dispatched = list(self._router_loop_delegations or [])
            agent_replies = list(self._router_loop_agent_replies or [])
            self._router_loop_delegations = None
            self._router_loop_agent_replies = None

        if new_dispatched:
            # Continue the chain with a fresh wave of delegations.
            pending.waiting_on = {d["to"] for d in new_dispatched}
            await self._chains.update(chain_id, waiting_on=pending.waiting_on)
            # PR18: re-arm watchdog for the continued chain.
            self._chains.arm_timeout(
                chain_id, on_fire=self._on_chain_timeout_fire,
            )
            return

        final_reply = agent_replies[0] if agent_replies else ""
        # History already appended by put_outbox.
        await self._send_agent_response(
            to=pending.origin_agent, response=final_reply,
            depth=pending.origin_depth, chain_id=chain_id,
        )
        await self._chains.resolve(chain_id)

    # ── chain timeout (PR18) ───────────────────────────────────────────────────
    # PR-refactor-session-1 wave 2: timer arm/cancel + sleep-and-fire loop are
    # now owned by ChainManager. The session keeps the on-fire callback below
    # so the upstream-error UX (synthesised response + chain_timeout event)
    # stays out of the service layer.

    async def _on_chain_timeout_fire(self, chain_id: str) -> None:
        """ChainManager invokes this when a chain's timeout watchdog fires.

        Pops the pending chain via `_chains.fire_timeout` (which also
        records the WAL `chain_timeout_fired` event), emits the
        `chain_timeout` audit event, and synthesises an error response
        upstream so the parent chain doesn't hang.
        """
        pending = await self._chains.fire_timeout(chain_id)
        if pending is None:
            return  # resolved between sleep wake and fire — nothing to do
        waiting = sorted(pending.waiting_on)
        error_text = (
            f"chain timeout: {len(waiting)} delegate(s) "
            f"({', '.join(waiting) or 'unknown'}) did not respond within "
            f"{self._chain_timeout_seconds:g}s"
        )
        self._chat_events.emit(
            "chain_timeout",
            chain_id=chain_id,
            waiting_on=waiting,
            timeout_seconds=self._chain_timeout_seconds,
            origin_agent=pending.origin_agent,
        )
        try:
            await self._send_agent_response(
                to=pending.origin_agent,
                response=error_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
            )
        except Exception as exc:
            # Don't let send failures wedge the loop — we already removed
            # the pending entry, so the worst case is a chain that lost its
            # error message but already won't hang.
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain timeout: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

    async def _dispatch_routing_decision_for_user(
        self, decision: dict, *, chain_id: str, depth: int,
    ) -> None:
        """Common user-facing tail of `_handle_user_message` / agent_response
        when chain_id has no pending entry. Pushes reply_text to the user
        outbox + history, spawns any skills, and forwards delegations on
        the same chain (depth+1)."""
        reply_text = (decision.get("reply_text") or "").strip()
        skills_to_run = decision.get("skills_to_run") or []
        messages_to_agents = decision.get("messages_to_agents") or []

        if reply_text:
            await self._put_outbox(OutboxMessage(
                kind="agent", text=reply_text, meta={"chain_id": chain_id},
            ))
            self._append_history(ChatMessage(
                role="agent", text=reply_text, ts=_now_iso(),
                meta={"chain_id": chain_id},
            ))
        for spec in skills_to_run:
            await self._spawn_skill(spec, chain_id=chain_id)
        for msg in messages_to_agents:
            to = (msg.get("to") or "").strip()
            request = (msg.get("request") or "").strip()
            if to and request:
                await self._send_to_agent(
                    to=to, request=request,
                    depth=depth + 1, chain_id=chain_id,
                )

    # ── slash command dispatch ──────────────────────────────────────────────────

    def _resolve_run_id(self, prefix: str) -> tuple[str | None, list[str]]:
        """Find a unique run_id matching `prefix` (anywhere within the id).

        Matches against the full id OR the trailing 4-char short tag, since
        users see `[skill#abcd]` and naturally type the short tag.

        Returns (run_id, candidates). `run_id` is non-None only when exactly
        one candidate matches; otherwise inspect `candidates`.
        """
        prefix = prefix.strip()
        if not prefix:
            return None, []
        candidates = [
            rid for rid in self.running_skills
            if rid.startswith(prefix) or rid.endswith(prefix)
        ]
        return (candidates[0] if len(candidates) == 1 else None), candidates

    def _resolve_intervention_id(self, prefix: str) -> tuple[str | None, list[str]]:
        """Same shape as `_resolve_run_id` but over the intervention registry."""
        return self._interventions.resolve_id_prefix(prefix)

    async def _maybe_handle_slash(self, text: str) -> bool:
        """Dispatch `/command args...` lines. Returns True when consumed.

        Delegates to the SlashRegistry in `reyn.chat.slash` so new commands
        can be added without touching this method.

        Unknown slash commands also return True (with a hint on outbox) to
        keep the router from running on user typos like "/halp".
        """
        from reyn.chat.slash import REGISTRY

        body = text[1:].lstrip()
        if not body:
            known = ", ".join(f"/{n}" for n in REGISTRY.names())
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"known commands: {known}",
            ))
            return True
        parts = body.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        slash_cmd = REGISTRY.get(cmd)
        if slash_cmd is None:
            known = ", ".join(f"/{n}" for n in REGISTRY.names())
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"unknown command /{cmd}; try: {known}",
            ))
            return True
        await slash_cmd.handler(self, args)
        return True

    async def _slash_list(self, args: str) -> None:
        """`/list` — running skills + pending interventions."""
        now = time.monotonic()
        lines: list[str] = []
        if self.running_skills:
            lines.append("running skills:")
            for rid, _task in self.running_skills.items():
                started = self.running_skills_started_at.get(rid)
                elapsed = f"{int(now - started)}s" if started is not None else "?s"
                # Recover skill_name from the run_id format
                # "TIMESTAMP_<skill>_<short>" — split between first and last underscore.
                short = _run_short(rid)
                # skill_name is everything between first '_' after timestamp and the trailing _short
                trimmed = rid[: -len(short) - 1] if short else rid  # drop "_abcd"
                # trimmed = "TIMESTAMP_skill_name"; drop the leading TIMESTAMP_
                _, _, skill_part = trimmed.partition("_")
                lines.append(f"  {short}  {skill_part:<24} {elapsed:>5}  (run_id={rid})")
        else:
            lines.append("running skills: (none)")
        active_ivs = self._interventions.list_active()
        if active_ivs:
            lines.append("pending interventions:")
            for iv in active_ivs:
                short = (iv.run_id[-4:] if iv.run_id else "----")
                lines.append(
                    f"  {iv.id[:8]}  {iv.kind:<20}  {iv.skill_name or '?'}#{short}"
                )
        await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))

    async def _slash_cancel(self, args: str) -> None:
        """`/cancel <id-prefix>` — cancel a running skill task."""
        prefix = args.strip()
        if not prefix:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: /cancel <id-prefix>",
            ))
            return
        rid, candidates = self._resolve_run_id(prefix)
        if rid is None:
            if not candidates:
                await self._put_outbox(OutboxMessage(
                    kind="error", text=f"no running skill matches {prefix!r}",
                ))
            else:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"ambiguous prefix {prefix!r}; matches: {', '.join(_run_short(c) for c in candidates)}",
                ))
            return
        task = self.running_skills.get(rid)
        if task is None or task.done():
            await self._put_outbox(OutboxMessage(
                kind="status", text=f"skill {_run_short(rid)} already finished",
            ))
            return
        task.cancel()
        await self._put_outbox(OutboxMessage(
            kind="status", text="cancel requested",
            meta=_run_meta(rid, None),
        ))

    async def _slash_answer(self, args: str) -> None:
        """`/answer <id-prefix> <text>` — deliver answer to a non-head intervention."""
        parts = args.split(maxsplit=1)
        if not parts:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: /answer <id-prefix> <text>",
            ))
            return
        prefix = parts[0]
        text = parts[1] if len(parts) > 1 else ""
        iid, candidates = self._resolve_intervention_id(prefix)
        if iid is None:
            if not candidates:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"no pending intervention matches {prefix!r}",
                ))
            else:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"ambiguous prefix {prefix!r}; matches: {', '.join(c[:8] for c in candidates)}",
                ))
            return
        iv = self._interventions.get(iid)
        if iv is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"intervention {prefix!r} disappeared mid-resolution",
            ))
            return
        await self._deliver_answer_to(iv, text)

    async def _slash_agents(self, args: str) -> None:
        """`/agents` — list known agents (registry-backed)."""
        if self._registry is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text="agent registry not wired; /agents only works in `reyn chat`",
            ))
            return
        names = self._registry.list_names()
        if not names:
            await self._put_outbox(OutboxMessage(
                kind="status", text="no agents (this should not happen — default auto-creates)",
            ))
            return
        attached = self._registry.attached_name
        loaded = set(self._registry.loaded_names())
        lines = ["agents:"]
        for n in names:
            try:
                profile = self._registry.load_profile(n)
                role_excerpt = (profile.role or "").strip().splitlines()
                role = role_excerpt[0] if role_excerpt else ""
            except Exception:
                role = "(profile load failed)"
            last = self._registry.last_activity_at(n)
            last_str = last.strftime("%Y-%m-%dT%H:%M") if last else "—"
            mark = "*" if n == attached else (" " if n not in loaded else "·")
            lines.append(f"  {mark} {n:<24} {last_str:<17} {role[:60]}")
        lines.append("(* = attached, · = loaded, blank = not yet loaded)")
        await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))

    async def _slash_attach(self, args: str) -> None:
        """`/attach <name>` — switch attached agent.

        The actual switch happens in repl._input_loop, which owns the display
        wiring. Here we only validate the name and put a sentinel attach
        request on this session's outbox; the REPL listens for the kind.
        """
        name = args.strip()
        if not name:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: /attach <name>",
            ))
            return
        if self._registry is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text="agent registry not wired; /attach only works in `reyn chat`",
            ))
            return
        if not self._registry.exists(name):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"agent {name!r} not found; create with `reyn agent new {name}`",
            ))
            return
        if name == self._registry.attached_name:
            await self._put_outbox(OutboxMessage(
                kind="status", text=f"already attached to {name!r}",
            ))
            return
        # The REPL drains its own outbox loop. Send the attach request as a
        # specially-kinded message so the input loop can recognize it.
        await self._put_outbox(OutboxMessage(
            kind="__attach_request__", text=name,
        ))

    async def _slash_cost(self, args: str) -> None:
        """`/cost` — quick token + USD line for the attached agent."""
        if self._budget_tracker is None:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text="budget tracker is disabled (no `cost:` config or non-chat mode)",
            ))
            return
        snap = self._budget_tracker.snapshot()
        line = format_cost_line(snap, self.agent_name)
        await self._put_outbox(OutboxMessage(kind="status", text=line))

    async def _slash_budget(self, args: str) -> None:
        """`/budget` (full breakdown) / `/budget reset` (clear counters)."""
        if self._budget_tracker is None:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text="budget tracker is disabled (no `cost:` config or non-chat mode)",
            ))
            return
        sub = args.strip()
        if sub == "reset":
            before = self._budget_tracker.reset_all()
            self._chat_events.emit("budget_reset", before=before)
            lines = ["Budget counters reset."]
            if before.get("agent_tokens"):
                for a, t in before["agent_tokens"].items():
                    cost = before.get("agent_cost_usd", {}).get(a, 0.0)
                    lines.append(f"  per-agent ({a}) tokens:    {t:>10,} → 0")
                    lines.append(f"  per-agent ({a}) cost_usd:  ${cost:.4f} → $0.00")
            if before.get("chain_skill_calls"):
                lines.append("  per-chain skill calls:        cleared")
            if before.get("rate_window_sizes"):
                lines.append("  rate-limit window:            cleared")
            lines.append(
                "Note: daily / monthly counters are NOT reset — "
                "they auto-reset at period boundary."
            )
            lines.append("Use `/budget` to verify.")
            await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))
            return
        snap = self._budget_tracker.snapshot()
        text = format_budget_full(snap, attached=self.agent_name)
        await self._put_outbox(OutboxMessage(kind="status", text=text))

    # ── skill spawn ─────────────────────────────────────────────────────────────

    async def _spawn_skill(self, spec: dict, *, chain_id: str | None = None) -> None:
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"invalid skill spec: {spec}",
            ))
            return
        # PR15: defense-in-depth allowlist check. The router-side filter
        # already hides blocked skills from the LLM, so reaching this branch
        # implies hallucination or a stale routing_decision. Refuse + audit.
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"skill {skill_name!r} is not in allowed_skills for agent "
                    f"{self.agent_name!r}; refused"
                ),
            ))
            self._chat_events.emit(
                "skill_spawn_refused",
                reason="allowlist", skill=skill_name, agent=self.agent_name,
            )
            return

        # PR22: per-chain per-skill cap check. Refuse spawn if hard limit
        # reached. Warn dimensions are emitted via events + outbox status.
        if self._budget_tracker is not None and chain_id is not None:
            check = self._budget_tracker.check_pre_spawn(
                chain_id=chain_id, skill=skill_name,
            )
            if not check.allowed:
                self._chat_events.emit(
                    "budget_exceeded",
                    dimension=check.hard_dimension,
                    detail=check.detail,
                    skill=skill_name,
                    chain_id=chain_id,
                )
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=format_refusal_message(check),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
                return
            for dim in check.warn_dimensions:
                self._chat_events.emit(
                    "budget_warn",
                    dimension=dim, chain_id=chain_id, skill=skill_name,
                    **check.context,
                )
                await self._put_outbox(OutboxMessage(
                    kind="status",
                    text=format_warn_message(dim, check.context),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
            self._budget_tracker.record_spawn(chain_id=chain_id, skill=skill_name)

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._chat_events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)
        # Track elapsed time for `:list` and provenance for outbox messages
        self.running_skills_started_at[run_id] = time.monotonic()
        await self._put_outbox(OutboxMessage(
            kind="status", text="starting…",
            meta=_run_meta(run_id, skill_name),
        ))

        task = asyncio.create_task(
            self._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
        )
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

    async def _run_one_skill(
        self,
        run_id: str,
        skill_name: str,
        input_artifact: dict,
        *,
        chain_id: str | None = None,
    ) -> None:
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, dsl_root = resolve_skill_path(skill_name)
        except SkillNotFoundError:
            # P6 audit completeness: skill_run_spawned was emitted earlier; emit
            # skill_run_failed for the error path so events log captures every
            # state transition (dogfood S13b).
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"skill not found: {skill_name}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"skill not found: {skill_name}", meta=meta,
            ))
            return
        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), dsl_root=str(dsl_root))
        except Exception as exc:
            # P6 audit completeness: pair with skill_run_spawned above.
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"failed to load: {exc}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"failed to load {skill_name}: {exc}", meta=meta,
            ))
            return

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox, run_id=run_id)],
        )
        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self.output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
            )
        except asyncio.CancelledError:
            await self._put_outbox(OutboxMessage(
                kind="status", text="cancelled", meta=meta,
            ))
            raise
        except Exception as exc:
            self._chat_events.emit("skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc))
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"failed: {exc}", meta=meta,
            ))
            return

        # PR22: surface budget_exceeded result as a user-facing error.
        if result.status == "budget_exceeded":
            self._chat_events.emit(
                "skill_run_failed",
                run_id=run_id, skill=skill_name,
                error="budget_exceeded",
            )
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=result.error or "budget exceeded",
                meta=meta,
            ))
            return

        self._accumulate(result)
        self._chat_events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
        )

        # Hand the result to skill_narrator to phrase a natural-language report
        # instead of dumping JSON to the user. Both narrate-success and the
        # raw-dump fallback land in history as `role="agent"` with
        # `meta.source="narrator"` — keeping the LLM-visible role surface to
        # `user / agent / summary` (custom roles change LLM attention).
        narrated = await self._invoke_narrator(
            skill_name=skill_name,
            status=result.status,
            result=result.data,
            state_subdir=f"narrator/{run_id}",
        )
        if narrated is None:
            self._chat_events.emit(
                "skill_narration_failed", run_id=run_id, skill=skill_name,
            )

        if narrated:
            self._append_history(ChatMessage(
                role="agent", text=narrated, ts=_now_iso(),
                meta={
                    "source": "narrator",
                    "skill": skill_name,
                    "run_id": run_id,
                    "status": result.status,
                },
            ))
            await self._put_outbox(OutboxMessage(
                kind="agent", text=narrated, meta=meta,
            ))
        else:
            # Fallback: raw dump so the user at least sees something.
            summary = json.dumps(result.data, ensure_ascii=False, indent=2)
            fallback = f"done (status={result.status})\n{summary}"
            self._append_history(ChatMessage(
                role="agent", text=fallback, ts=_now_iso(),
                meta={
                    "source": "narrator",
                    "skill": skill_name,
                    "run_id": run_id,
                    "status": result.status,
                    "narration_failed": True,
                },
            ))
            await self._put_outbox(OutboxMessage(
                kind="skill_done", text=fallback, meta=meta,
            ))

    # ── RouterLoop helper methods (Wave 3 F1) ───────────────────────────────────

    def _memory_dir(self, layer: str) -> str:
        """Directory for the memory layer.

        layer="shared" → .reyn/memory
        layer="agent"  → .reyn/agents/<agent_name>/memory
        """
        if layer == "shared":
            return str(Path(".reyn") / "memory")
        return str(self.workspace_dir / "memory")

    def _memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer + slug to absolute file path.

        layer="shared" → .reyn/memory/<slug>.md
        layer="agent"  → .reyn/agents/<agent_name>/memory/<slug>.md
        """
        return str(Path(self._memory_dir(layer)) / f"{slug}.md")

    def _get_file_permissions_for_router(self) -> dict | None:
        """Return file permissions in the form {read: [paths], write: [paths]}
        for the router's tool catalog. None if no file permissions configured.

        Reads from self._perm (PermissionResolver) config to expose what
        paths are permitted. Returns None when no PermissionResolver is
        wired or when no file.read/file.write is configured.
        """
        if self._perm is None:
            return None
        config = self._perm._config or {}
        read_val = config.get("file.read") or (config.get("file") or {}).get("read")
        write_val = config.get("file.write") or (config.get("file") or {}).get("write")

        # "allow" string → treat as project-wide wildcard
        read_paths: list[str] = []
        write_paths: list[str] = []

        if read_val == "allow":
            read_paths = ["*"]
        elif isinstance(read_val, list):
            for entry in read_val:
                if isinstance(entry, str):
                    read_paths.append(entry)
                elif isinstance(entry, dict) and entry.get("path"):
                    read_paths.append(str(entry["path"]))

        if write_val == "allow":
            write_paths = ["*"]
        elif isinstance(write_val, list):
            for entry in write_val:
                if isinstance(entry, str):
                    write_paths.append(entry)
                elif isinstance(entry, dict) and entry.get("path"):
                    write_paths.append(str(entry["path"]))

        if not read_paths and not write_paths:
            return None
        return {"read": read_paths, "write": write_paths}

    def _mcp_servers_flat(self) -> dict:
        """Unwrap config.mcp's `{servers: {...}}` shape to flat `{name: cfg}`.

        ChatSession receives the wrapped form from CLI bootstrap (config.mcp).
        The Agent / control_ir_executor unwraps via `.get("servers", {})`;
        chat-router-side helpers historically did not (PR35 oversight) and
        treated "servers" as if it were a server name. Centralized unwrap.
        """
        raw = self._mcp_servers or {}
        if isinstance(raw, dict) and "servers" in raw:
            inner = raw.get("servers") or {}
            return inner if isinstance(inner, dict) else {}
        return raw if isinstance(raw, dict) else {}

    def _get_mcp_servers_for_router(self) -> list[dict]:
        """Return [{name, description}, ...] for configured MCP servers
        accessible to this agent. [] if none."""
        servers = self._mcp_servers_flat()
        if not servers:
            return []
        result: list[dict] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            result.append({
                "name": name,
                "description": cfg.get("description", ""),
            })
        return result

    def _make_router_op_context(self) -> "OpContext":
        """Build a minimal OpContext for router-initiated file/MCP ops.

        Uses the session's events log and permission resolver. The skill_name
        "chat_router" is used for permission key lookups — it matches what the
        PermissionResolver uses to gate paths. All .reyn/ paths are in the
        default write zone so memory ops pass without additional approval.

        PermissionDecl is populated from the agent's effective permissions
        (file_read / file_write from config, mcp from configured servers) so
        that op_runtime layer permission checks actually gate access rather than
        silently allowing everything through an empty decl.
        """
        from reyn.op_runtime.context import OpContext
        from reyn.workspace.workspace import Workspace
        from reyn.permissions.permissions import PermissionDecl

        file_perms = self._get_file_permissions_for_router() or {}
        mcp_servers = self._get_mcp_servers_for_router() or []

        # Convert flat path strings to the {path, scope} dict format used by PermissionDecl
        file_read = [{"path": p, "scope": "recursive"} for p in file_perms.get("read", [])]
        file_write = [{"path": p, "scope": "recursive"} for p in file_perms.get("write", [])]
        mcp_names = [s["name"] for s in mcp_servers]

        decl = PermissionDecl(
            file_read=file_read,
            file_write=file_write,
            mcp=mcp_names,
            allowed_mcp=self._allowed_mcp,
        )

        workspace = Workspace(
            events=self._chat_events,
            permission_resolver=self._perm,
            skill_name="chat_router",
        )
        return OpContext(
            workspace=workspace,
            events=self._chat_events,
            permission_decl=decl,
            permission_resolver=self._perm,
            skill_name="chat_router",
            mcp_servers=self._mcp_servers_flat(),
        )

    async def _file_op(self, op_dict: dict) -> dict:
        """Dispatch a file op via op_runtime. Returns result dict."""
        from reyn.op_runtime import execute_op
        from reyn.schemas.models import FileIROp

        op = FileIROp(**op_dict)
        ctx = self._make_router_op_context()
        return await execute_op(op, ctx, caller="control_ir")

    async def _file_read(self, path: str) -> dict:
        """Read a file through op_runtime.

        Returns: {"path": path, "content": <text>} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "read", "path": path})
        if result.get("status") == "ok":
            return {"path": path, "content": result.get("content", "")}
        if result.get("status") == "not_found":
            return {"error": f"file not found: {path}"}
        return {"error": result.get("error", "read failed")}

    async def _file_write(self, path: str, content: str) -> dict:
        """Write a file through op_runtime.

        Returns: {"path": path, "written": True} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "write", "path": path, "content": content})
        if result.get("status") == "ok":
            return {"path": path, "written": True}
        return {"error": result.get("error", "write failed")}

    async def _file_delete(self, path: str) -> dict:
        """Delete a file through op_runtime.

        Returns: {"path": path, "deleted": bool} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "delete", "path": path})
        if result.get("status") == "ok":
            return {"path": path, "deleted": result.get("deleted", True)}
        return {"error": result.get("error", "delete failed")}

    async def _file_list_directory(self, path: str) -> dict:
        """List directory contents through op_runtime (glob).

        Returns: {"path": path, "entries": [...]} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "glob", "path": f"{path.rstrip('/')}/*"})
        if result.get("status") == "ok":
            return {"path": path, "entries": result.get("matches", [])}
        return {"error": result.get("error", "list_directory failed")}

    async def _file_regenerate_index(
        self, *, path: str, output_path: str, entry_template: str, header: str,
    ) -> dict:
        """Regenerate an index file through op_runtime.

        Returns: {"path": path, "output_path": output_path, "entries": n} or {"error": ...}.
        """
        result = await self._file_op({
            "kind": "file", "op": "regenerate_index",
            "path": path,
            "output_path": output_path,
            "entry_template": entry_template,
            "header": header,
        })
        if result.get("status") == "ok":
            return {
                "path": path,
                "output_path": output_path,
                "entries": result.get("entries", 0),
            }
        return {"error": result.get("error", "regenerate_index failed")}

    async def _run_remember(
        self,
        *,
        layer: str,
        slug: str,
        name: str,
        description: str,
        type: str,
        body: str,
    ) -> dict:
        """Persist a memory entry. Constructs frontmatter, writes body file,
        regenerates the layer's MEMORY.md.

        layer: "shared" or "agent"
        Returns: {"saved": slug, "layer": layer, "path": <relative>}
        """
        mem_dir = self._memory_dir(layer)
        body_path = self._memory_path(layer, slug)
        # Relative path for the caller to reference
        rel_path = body_path

        # Build frontmatter + body
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {type}\n"
            f"---\n"
        )
        full_content = frontmatter + body

        write_result = await self._file_write(body_path, full_content)
        if "error" in write_result:
            return {"error": write_result["error"]}

        index_path = str(Path(mem_dir) / "MEMORY.md")
        regen_result = await self._file_regenerate_index(
            path=mem_dir,
            output_path=index_path,
            entry_template="- [{name}]({slug}.md) — {description}",
            header="# Memory Index\n\n",
        )
        if "error" in regen_result:
            return {"error": regen_result["error"]}

        self._chat_events.emit(
            "memory_saved", layer=layer, slug=slug, path=body_path,
        )
        return {"saved": slug, "layer": layer, "path": rel_path}

    async def _read_memory_body(self, *, layer: str, slug: str) -> dict:
        """Read a memory body file's contents.

        Returns: {"layer": layer, "slug": slug, "content": <text>}
        or {"error": <reason>} if not found.
        """
        body_path = self._memory_path(layer, slug)
        result = await self._file_read(body_path)
        if "error" in result:
            return {"error": result["error"]}
        return {"layer": layer, "slug": slug, "content": result["content"]}

    async def _run_forget(self, *, layer: str, slug: str) -> dict:
        """Delete a memory entry and regenerate index.

        Returns: {"deleted": slug, "layer": layer}
        or {"error": <reason>} if not found.
        """
        body_path = self._memory_path(layer, slug)
        del_result = await self._file_delete(body_path)
        if "error" in del_result:
            return {"error": del_result["error"]}
        if not del_result.get("deleted"):
            return {"error": f"memory entry not found: {slug}"}

        mem_dir = self._memory_dir(layer)
        index_path = str(Path(mem_dir) / "MEMORY.md")
        regen_result = await self._file_regenerate_index(
            path=mem_dir,
            output_path=index_path,
            entry_template="- [{name}]({slug}.md) — {description}",
            header="# Memory Index\n\n",
        )
        if "error" in regen_result:
            return {"error": regen_result["error"]}

        self._chat_events.emit("memory_deleted", layer=layer, slug=slug, path=body_path)
        return {"deleted": slug, "layer": layer}

    async def _run_skill_awaitable(self, spec: dict, *, chain_id: str) -> dict:
        """Awaitable variant of _spawn_skill. Runs a single skill, awaits its
        completion, narrates result to outbox via _invoke_narrator, returns
        the final_output dict.

        spec format: {"skill": <name>, "input": <artifact dict>}
        Returns: {"status": "finished"|"error", "data": <final_output>}
        """
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            return {"status": "error", "data": {"error": f"invalid skill spec: {spec}"}}

        # PR15: allowlist check — same defense as _spawn_skill
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            self._chat_events.emit(
                "skill_spawn_refused",
                reason="allowlist", skill=skill_name, agent=self.agent_name,
            )
            return {
                "status": "error",
                "data": {
                    "error": (
                        f"skill {skill_name!r} is not in allowed_skills for agent "
                        f"{self.agent_name!r}; refused"
                    )
                },
            }

        # PR22: budget cap check
        if self._budget_tracker is not None:
            check = self._budget_tracker.check_pre_spawn(
                chain_id=chain_id, skill=skill_name,
            )
            if not check.allowed:
                self._chat_events.emit(
                    "budget_exceeded",
                    dimension=check.hard_dimension,
                    detail=check.detail,
                    skill=skill_name,
                    chain_id=chain_id,
                )
                return {
                    "status": "error",
                    "data": {"error": check.detail or "budget exceeded"},
                }
            self._budget_tracker.record_spawn(chain_id=chain_id, skill=skill_name)

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._chat_events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)

        try:
            skill_dir, dsl_root = resolve_skill_path(skill_name)
        except SkillNotFoundError:
            # P6 audit completeness: skill_run_spawned was emitted above; we must
            # emit a corresponding skill_run_failed for the error path so the
            # event log records every state transition (dogfood S13b).
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"skill not found: {skill_name}",
            )
            return {"status": "error", "data": {"error": f"skill not found: {skill_name}"}}

        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), dsl_root=str(dsl_root))
        except Exception as exc:
            # P6 audit completeness: pair with skill_run_spawned above.
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"failed to load: {exc}",
            )
            return {"status": "error", "data": {"error": f"failed to load {skill_name}: {exc}"}}

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox, run_id=run_id)],
        )

        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self.output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
            )
        except asyncio.CancelledError:
            return {"status": "error", "data": {"error": "cancelled"}}
        except Exception as exc:
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc),
            )
            return {"status": "error", "data": {"error": str(exc)}}

        if result.status == "budget_exceeded":
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name, error="budget_exceeded",
            )
            return {"status": "error", "data": {"error": result.error or "budget exceeded"}}

        self._accumulate(result)
        self._chat_events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
        )

        # Narrate so the user sees the work (same as _run_one_skill)
        meta = _run_meta(run_id, skill_name)
        narrated = await self._invoke_narrator(
            skill_name=skill_name,
            status=result.status,
            result=result.data,
            state_subdir=f"narrator/{run_id}",
        )
        if narrated:
            self._append_history(ChatMessage(
                role="agent", text=narrated, ts=_now_iso(),
                meta={
                    "source": "narrator",
                    "skill": skill_name,
                    "run_id": run_id,
                    "status": result.status,
                },
            ))
            await self._put_outbox(OutboxMessage(kind="agent", text=narrated, meta=meta))
        else:
            summary = json.dumps(result.data, ensure_ascii=False, indent=2)
            fallback = f"done (status={result.status})\n{summary}"
            self._append_history(ChatMessage(
                role="agent", text=fallback, ts=_now_iso(),
                meta={
                    "source": "narrator",
                    "skill": skill_name,
                    "run_id": run_id,
                    "status": result.status,
                    "narration_failed": True,
                },
            ))
            await self._put_outbox(OutboxMessage(kind="skill_done", text=fallback, meta=meta))

        return {"status": result.status or "finished", "data": result.data or {}}

    async def _mcp_list_servers(self) -> list[dict]:
        """Returns the configured MCP server list with descriptions."""
        return self._get_mcp_servers_for_router()

    async def _mcp_list_tools(self, server: str) -> list[dict]:
        """Query the MCP server for its tools list."""
        from reyn.mcp_client import MCPClient, MCPError, expand_env

        servers = self._mcp_servers_flat()
        if not servers:
            return [{"error": f"no MCP servers configured"}]
        server_cfg = servers.get(server)
        if not server_cfg:
            return [{"error": f"MCP server {server!r} not configured"}]

        expanded = expand_env(server_cfg)
        if not isinstance(expanded, dict):
            return [{"error": f"MCP server {server!r} config must be a dict"}]
        if "type" not in expanded and expanded.get("url"):
            expanded = {**expanded, "type": "http"}

        try:
            client = MCPClient(expanded)
            tools = await client.list_tools()
            await client.close()
            return tools
        except MCPError as exc:
            return [{"error": str(exc)}]
        except Exception as exc:
            return [{"error": str(exc)}]

    async def _mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        """Invoke an MCP tool and return its result."""
        from reyn.op_runtime import execute_op
        from reyn.schemas.models import MCPIROp
        from reyn.permissions.permissions import PermissionDecl

        op = MCPIROp(kind="mcp", server=server, tool=tool, args=args)
        ctx = self._make_router_op_context()
        # MCP handler requires intervention_bus; wire the session's bus
        ctx.intervention_bus = ChatInterventionBus(self, run_id=None, skill_name="chat_router")
        # Narrow mcp scope to just this server while preserving file perms from the
        # populated decl. PermissionDecl.mcp must include the server for require_mcp to pass.
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        return await execute_op(op, ctx, caller="control_ir")

    # ── RouterLoopHost protocol (Wave 3 F2) ─────────────────────────────────────

    # --- Properties required by RouterLoopHost ---

    @property
    def chat_id(self) -> str:
        """chat_id exposed to RouterLoopHost — same as agent_name."""
        return self.agent_name

    @property
    def agent_role(self) -> str:
        """agent_role exposed to RouterLoopHost."""
        return self._agent_role

    @property
    def events(self):
        """EventLog exposed to RouterLoopHost for dispatch_tool events."""
        return self._chat_events

    # --- Catalogue accessors ---

    def list_available_skills(self) -> list[dict]:
        """Return enumerated skills with skill_router excluded (deleted in wave H).

        Also excludes chat_compactor and skill_narrator — these are internal
        infrastructure, not user-facing skills.
        """
        avail = enumerate_available_skills(exclude={
            ROUTER_SKILL_NAME, "chat_compactor", NARRATOR_SKILL_NAME,
        })
        # PR15: allowlist filter
        if self._allowed_skills is not None:
            allow = set(self._allowed_skills)
            avail = [s for s in avail if s.get("name") in allow]
        return avail

    def list_available_agents(self) -> list[dict]:
        """Return topology-reachable peers (PR11/PR12)."""
        if self._registry is not None:
            return list(self._registry.iter_reachable_agents(self.agent_name))
        return []

    def get_memory_index(self) -> dict:
        """Return merged shared + agent memory index."""
        return _merge_memory_indexes(
            shared_path=Path(".reyn") / "memory" / "MEMORY.md",
            agent_path=self.workspace_dir / "memory" / "MEMORY.md",
            agent_name=self.agent_name,
        )

    def get_file_permissions(self) -> dict | None:
        return self._get_file_permissions_for_router()

    def get_mcp_servers(self) -> list[dict]:
        return self._get_mcp_servers_for_router()

    def memory_path(self, layer: str, slug: str) -> str:
        return self._memory_path(layer, slug)

    def memory_dir(self, layer: str) -> str:
        return self._memory_dir(layer)

    # --- Action callbacks ---

    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict:
        return await self._run_skill_awaitable(
            {"skill": skill, "input": input}, chain_id=chain_id,
        )

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None:
        """RouterLoopHost callback: dispatch to peer and record delegation
        for pending-chain registration (F2 wave)."""
        await self._send_to_agent(
            to=to, request=request, depth=depth, chain_id=chain_id,
        )
        # Track delegations so callers can register _PendingChain after the loop.
        if self._router_loop_delegations is not None:
            self._router_loop_delegations.append({"to": to, "request": request})

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        await self._put_outbox(OutboxMessage(kind=kind, text=text, meta=meta))
        # Persist agent (conversational) replies to history so the context
        # window stays coherent across turns.
        if kind == "agent" and text:
            self._append_history(ChatMessage(
                role="agent", text=text, ts=_now_iso(), meta=meta,
            ))
            # Capture for agent-to-agent paths (agent_request / chain_resolve)
            # that need to forward the reply upstream via _send_agent_response.
            if self._router_loop_agent_replies is not None:
                self._router_loop_agent_replies.append(text)

    async def file_read(self, path: str) -> str:
        """RouterLoopHost file_read — returns content string or JSON error."""
        res = await self._file_read(path)
        if "content" in res:
            return res["content"]
        return json.dumps(res)

    async def file_write(self, path: str, content: str) -> dict:
        return await self._file_write(path, content)

    async def file_delete(self, path: str) -> dict:
        return await self._file_delete(path)

    async def file_list_directory(self, path: str) -> list[dict]:
        result = await self._file_list_directory(path)
        if isinstance(result, dict):
            return result.get("entries", [result])
        return result

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict:
        return await self._file_regenerate_index(
            path=path,
            output_path=output_path,
            entry_template=entry_template,
            header=header,
        )

    async def mcp_list_servers(self) -> list[dict]:
        return await self._mcp_list_servers()

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return await self._mcp_list_tools(server)

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return await self._mcp_call_tool(server, tool, args)

    def resolve_model(self, name: str) -> str:
        """Resolve config model name (e.g. 'router') to actual model id."""
        return self._resolver.resolve(name)

    # --- RouterLoop orchestration ---

    def _build_history_for_router(self) -> list[dict]:
        """Slice self.history into OpenAI-style messages for RouterLoop.

        Mirrors the head/tail compaction config so the LLM sees the same
        context window the old skill_router preprocessor produced.
        Returns [{role: 'user'|'assistant', content: str}, ...] ordered
        chronologically. The system prompt is prepended by RouterLoop itself.

        Only user/agent conversational turns are included. The compaction
        head_size + tail_size governs which turns to keep.
        """
        cfg = self._compaction
        turns = [m for m in self.history if m.role in ("user", "agent")]

        # Apply the same head/body/tail windowing as the compaction slicer:
        # always keep first head_size turns (HEAD) and last tail_size turns (TAIL).
        head = turns[:cfg.head_size]
        tail = turns[-cfg.tail_size:] if cfg.tail_size else []

        # Use summary as a bridge when body is compacted.
        summary = self._latest_summary()
        if summary and len(turns) > cfg.head_size + cfg.tail_size:
            bridge = [ChatMessage(
                role="agent",
                text=f"[summary of earlier conversation]\n{summary.text}",
                ts=summary.ts,
            )]
        else:
            bridge = []

        selected = head + bridge + tail if bridge else head + tail

        messages: list[dict] = []
        for m in selected:
            role = "user" if m.role == "user" else "assistant"
            messages.append({"role": role, "content": m.text})
        return messages

    async def _run_router_loop(
        self,
        user_text: str,
        chain_id: str,
    ) -> None:
        """Run RouterLoop for one user utterance. Enforces the per-turn cap,
        builds history, and calls RouterLoop.run(). Does NOT modify history
        or outbox directly — RouterLoop calls host callbacks.

        Raises RouterCapExceeded when the per-turn cap is reached.
        """
        self._check_and_increment_router_cap(user_text)
        from reyn.chat.router_loop import RouterLoop
        loop = RouterLoop(
            host=self, chain_id=chain_id, max_iterations=5,
            budget=self._budget_tracker,
        )
        history = self._build_history_for_router()
        await loop.run(user_text=user_text, history=history)
