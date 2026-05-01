"""ChatSession — long-lived chat loop driving the skill_router stdlib skill."""
from __future__ import annotations
import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reyn.agent import Agent
from reyn.compiler import load_dsl_skill
from reyn.compiler.parser import _split_frontmatter
from reyn.config import LimitsConfig
from reyn.events import EventLog
from reyn.model_resolver import ModelResolver
from reyn.permissions import PermissionResolver
from reyn.reporters.persister import EventPersister
from reyn.skill_paths import resolve_skill_path, stdlib_root
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    UserIntervention,
    match_choice,
)
from reyn.chat.outbox import OutboxMessage


ROUTER_SKILL_NAME = "skill_router"
NARRATOR_SKILL_NAME = "skill_narrator"


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
        return await self._session._dispatch_intervention(iv)


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


@dataclass
class _PendingChain:
    """Multi-hop relay state held in a delegating agent.

    Created when an agent receives an `agent_request` and decides to
    further delegate (router emits `messages_to_agents`). The reply to
    the upstream `origin_agent` is held back until every entry in
    `waiting_on` has returned an `agent_response` for this chain_id.
    On the final response, the agent re-runs its router so the LLM can
    compose a synthesized answer with all delegate replies in history,
    then sends that answer to `origin_agent` at `origin_depth`.
    """
    chain_id: str
    origin_agent: str
    origin_depth: int
    original_request: str
    waiting_on: set[str]


def _iv_meta(iv: "UserIntervention") -> dict:
    """Standard `meta` payload for OutboxMessage announcing an intervention."""
    out = {"intervention_id": iv.id, "intervention_kind": iv.kind}
    if iv.run_id:
        out["run_id"] = iv.run_id
        out["run_id_short"] = _run_short(iv.run_id)
    if iv.skill_name:
        out["skill_name"] = iv.skill_name
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
    ) -> None:
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
        # Optional back-reference for slash commands like :agents / :attach
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

        from reyn.config import CompactionConfig
        self._compaction = compaction_config or CompactionConfig()
        self._next_seq = 1
        self._compacting = False
        self._compaction_task: asyncio.Task | None = None

        self.workspace_dir = Path(".reyn") / "agents" / self.agent_name
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.workspace_dir / "history.jsonl"
        self.events_path = self.workspace_dir / "events.jsonl"
        self.runs_root = self.workspace_dir / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

        self.history: list[ChatMessage] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        # Detached by default — AgentRegistry.attach() flips this on. Outbox
        # `status`/`trace` emissions are dropped while detached so background
        # agents don't accumulate display noise.
        self.is_attached: bool = False

        # PR14: per-chain state for multi-hop relay. When this agent receives
        # an agent_request and decides to delegate to other agents, the upstream
        # reply is held back until every delegate replies. Keyed by chain_id;
        # absent entry means the chain is either not multi-hop or originated
        # locally (user-initiated chains live without a pending entry — their
        # interim+final UX is handled in _handle_user_message).
        self._pending_chains: dict[str, _PendingChain] = {}
        # PR18: timeout watchdog tasks per pending chain, paired 1:1 with
        # `_pending_chains`. Started at registration, cancelled on resolution
        # or shutdown. Empty when timeouts are disabled (chain_timeout_seconds
        # <= 0).
        self._pending_chain_timers: dict[str, asyncio.Task] = {}

        from reyn.pricing import TokenUsage
        self._total_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0

        self._chat_events = EventLog(subscribers=[EventPersister(self.events_path)])
        self.running_skills: dict[str, asyncio.Task] = {}
        # Per-run wall-clock start (monotonic) for `:list` elapsed-seconds display.
        self.running_skills_started_at: dict[str, float] = {}

        # User-intervention routing state. The deque preserves FIFO emission order;
        # the dict gives O(1) lookup by intervention id. Untyped user lines answer
        # the head of the deque (oldest still-pending intervention).
        self._active_interventions: dict[str, UserIntervention] = {}
        self._intervention_order: deque[str] = deque()

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
        await self.inbox.put(("user", {"text": text, "chain_id": _new_chain_id()}))

    async def submit_agent_request(
        self, *, from_agent: str, request: str, depth: int, chain_id: str,
    ) -> None:
        await self.inbox.put(("agent_request", {
            "from_agent": from_agent, "request": request, "depth": depth,
            "chain_id": chain_id,
        }))

    async def submit_agent_response(
        self, *, from_agent: str, response: str, depth: int, chain_id: str,
    ) -> None:
        await self.inbox.put(("agent_response", {
            "from_agent": from_agent, "response": response, "depth": depth,
            "chain_id": chain_id,
        }))

    async def shutdown(self) -> None:
        await self.inbox.put(("shutdown", {}))

    # ── main loop ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._chat_events.emit("chat_started", agent_name=self.agent_name, model=self.model)

        try:
            while True:
                kind, payload = await self.inbox.get()
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
        for task in list(self._pending_chain_timers.values()):
            if not task.done():
                task.cancel()
        if self._pending_chain_timers:
            await asyncio.gather(
                *self._pending_chain_timers.values(), return_exceptions=True
            )
        self._pending_chain_timers.clear()

        if self._compaction_task is not None and not self._compaction_task.done():
            try:
                await self._compaction_task
            except Exception:
                pass

    async def _handle_user_message(self, text: str, *, chain_id: str) -> None:
        # Slash commands (`:list`, `:cancel <id>`, `:answer <id> <text>`) take
        # precedence over both the active-intervention router and a fresh
        # router turn.
        if text.startswith(":"):
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
            kind="status", text="考え中...", meta={"chain_id": chain_id},
        ))

        try:
            decision = await self._invoke_router(text)
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed: {exc}",
                meta={"chain_id": chain_id},
            ))
            return

        # User-initiated chains keep the PR11 interim+final UX: reply_text
        # is delivered to the user immediately even when delegations are
        # also present. The deferred-reply mechanic is reserved for the
        # agent_request branch (see _handle_agent_request).
        await self._dispatch_routing_decision_for_user(
            decision, chain_id=chain_id, depth=0,
        )

        # Fire-and-forget compaction check after the user has the reply.
        # Reuses self._compacting as a single-flight lock; no await here so
        # the user's next prompt isn't blocked. _drain_on_shutdown awaits any
        # in-flight compaction task so a quick /quit after a heavy turn does
        # not lose the summary.
        if self._compaction_task is None or self._compaction_task.done():
            self._compaction_task = asyncio.create_task(self._maybe_compact())

    # ── skill invocation helpers ────────────────────────────────────────────────

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

    async def _invoke_router(self, user_text: str, state_subdir: str = "router") -> dict:
        """Run the skill_router skill on a user utterance.

        Narration of finished skill runs is handled by `_invoke_narrator` —
        the router is now routing-only (PR9).

        History is NOT inlined into the artifact — the classify phase has a
        Python preprocessor step that reads `.reyn/agents/<name>/history.jsonl`
        and slices the recent N turns. This eliminates the snapshot-per-turn
        duplication that previously bloated workspace artifacts.
        """
        avail = enumerate_available_skills(exclude={
            ROUTER_SKILL_NAME, "chat_compactor", NARRATOR_SKILL_NAME,
        })
        # PR15: filter to the agent's allowlist (if any). stdlib system skills
        # are already excluded above and are not subject to this filter — they
        # always run. The router LLM only sees what passes both gates.
        if self._allowed_skills is not None:
            allow = set(self._allowed_skills)
            avail = [s for s in avail if s.get("name") in allow]
        # PR11: list peer agents (excluding self) so the router can decide
        # between local skill invocation and delegation to another agent.
        # PR12: filter by topology rules so the LLM only sees reachable peers.
        if self._registry is not None:
            available_agents = self._registry.iter_reachable_agents(self.agent_name)
        else:
            available_agents = []

        # PR15: pre-merge shared + agent memory indexes here. Static phase
        # YAML can't interpolate `.reyn/agents/<self.agent_name>/...`, so
        # we synthesize the merged `memory_index` and embed it directly.
        # Shape matches what the file/read op used to return ({status, content}).
        memory_index = _merge_memory_indexes(
            shared_path=Path(".reyn") / "memory" / "MEMORY.md",
            agent_path=self.workspace_dir / "memory" / "MEMORY.md",
            agent_name=self.agent_name,
        )

        data: dict = {
            "user_message": user_text,
            "chat_id": self.agent_name,
            # Precomputed for the classify phase preprocessor: the file/read op
            # uses this via args_from. ChatSession owns this path because the
            # workspace dir was created relative to the cwd at session start.
            "history_path": str(self.history_path),
            "available_skills": avail,
            "available_agents": available_agents,
            "memory_index": memory_index,
            # Pass the head/tail config through so the slicer can honor it
            # without needing access to ReynConfig.
            "compaction": {
                "head_size": self._compaction.head_size,
                "tail_size": self._compaction.tail_size,
            },
        }
        input_artifact = {"type": "chat_routing_request", "data": data}

        result = await self._run_stdlib_skill(
            ROUTER_SKILL_NAME, input_artifact, state_subdir=state_subdir,
            forward_events=True,
        )
        return result.data

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
        except Exception:
            return None
        if not run_result.ok:
            return None
        text = (run_result.data or {}).get("reply_text")
        return (text or "").strip() or None

    # ── intervention routing ─────────────────────────────────────────────────────

    async def _maybe_answer_oldest_intervention(self, text: str) -> bool:
        """If any intervention is pending, deliver `text` to the oldest and
        return True. Stale (already-resolved) entries are evicted transparently."""
        # Evict stale heads.
        while self._intervention_order:
            head_id = self._intervention_order[0]
            iv = self._active_interventions.get(head_id)
            if iv is None or iv.future.done():
                self._intervention_order.popleft()
                self._active_interventions.pop(head_id, None)
                continue
            break

        if not self._intervention_order:
            return False

        head_id = self._intervention_order[0]
        iv = self._active_interventions[head_id]
        return await self._deliver_answer_to(iv, text)

    async def _deliver_answer_to(self, iv: UserIntervention, text: str) -> bool:
        """Resolve `iv` with `text` and append a user-history entry.

        Returns True when the intervention was consumed (answer set OR
        unrecognized-choice hint emitted, both of which should suppress
        a fresh router turn). Shared between oldest-intervention routing
        and the targeted `:answer <id>` slash command.
        """
        if iv.future.done():
            return False
        if iv.choices:
            choice = match_choice(text, iv.choices)
            if choice is None:
                # Unrecognized choice — surface a status hint and don't resolve.
                # The user can re-type the correct hotkey.
                hint = " / ".join(c.label for c in iv.choices)
                await self._put_outbox(OutboxMessage(
                    kind="status",
                    text=f"unknown choice; expected one of: {hint}",
                    meta=_iv_meta(iv),
                ))
                return True  # consumed: don't fall through to a fresh router turn
            answer = InterventionAnswer(text=text, choice_id=choice.id)
        else:
            answer = InterventionAnswer(text=text)

        iv.future.set_result(answer)
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
            choice_id=answer.choice_id,
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
            lines.append(f"質問: {iv.prompt}")
        else:
            lines.append(iv.prompt)
        if iv.detail:
            lines.append(f"  {iv.detail}")
        if iv.suggestions:
            lines.append(f"  候補: {' / '.join(iv.suggestions)}")
        if iv.choices:
            labels = " / ".join(c.label for c in iv.choices)
            lines.append(f"  {labels}")
        await self._put_outbox(OutboxMessage(
            kind="intervention",
            text="\n".join(lines),
            meta=_iv_meta(iv),
        ))

    async def _dispatch_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Register an intervention in the queue, announce it (or signal queued
        status), then await the user's response. Always cleans up on exit so a
        cancelled skill doesn't leave dangling entries.
        """
        self._active_interventions[iv.id] = iv
        self._intervention_order.append(iv.id)
        try:
            if len(self._intervention_order) == 1:
                await self._announce_intervention(iv)
            else:
                queued = len(self._intervention_order) - 1
                await self._put_outbox(OutboxMessage(
                    kind="status",
                    text=f"質問待ち ({queued}件キュー中)",
                    meta=_iv_meta(iv),
                ))
            try:
                return await iv.future
            except asyncio.CancelledError:
                return InterventionAnswer(text="")
        finally:
            self._active_interventions.pop(iv.id, None)
            try:
                self._intervention_order.remove(iv.id)
            except ValueError:
                pass
            # If the head was cleared, announce the next pending intervention.
            await self._maybe_announce_next()

    async def _maybe_announce_next(self) -> None:
        """Announce the new head intervention (if any) when the previous one
        was resolved or cancelled. Skips already-announced heads."""
        if not self._intervention_order:
            return
        head_id = self._intervention_order[0]
        iv = self._active_interventions.get(head_id)
        if iv is None or iv.future.done():
            return
        # We can't tell from state alone whether this head was already announced;
        # _dispatch_intervention announces eagerly when len==1 on first push, so
        # if we're here because the previous head finished, this head still
        # needs an announcement.
        await self._announce_intervention(iv)

    def _drop_interventions_for_run(self, run_id: str | None) -> None:
        """Cancel any pending interventions tagged with `run_id`."""
        if not run_id:
            return
        victims = [
            iv_id for iv_id, iv in self._active_interventions.items()
            if iv.run_id == run_id
        ]
        for iv_id in victims:
            iv = self._active_interventions.pop(iv_id, None)
            try:
                self._intervention_order.remove(iv_id)
            except ValueError:
                pass
            if iv is not None and not iv.future.done():
                iv.future.cancel()

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

        try:
            decision = await self._invoke_router(request)
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

        reply_text = (decision.get("reply_text") or "").strip()
        skills_to_run = decision.get("skills_to_run") or []
        messages_to_agents = [
            m for m in (decision.get("messages_to_agents") or [])
            if (m.get("to") or "").strip() and (m.get("request") or "").strip()
        ]

        # Skills run locally without affecting hop depth.
        for spec in skills_to_run:
            await self._spawn_skill(spec)

        if messages_to_agents:
            # PR14 deferred path: register pending chain, dispatch delegations,
            # WITHOUT replying to the requester this turn. The reply will be
            # synthesized after every delegate responds (see _handle_agent_response).
            # If reply_text was also produced it's discarded — the deferred
            # final answer takes the place of any interim narration.
            self._pending_chains[chain_id] = _PendingChain(
                chain_id=chain_id,
                origin_agent=from_agent,
                origin_depth=depth,
                original_request=request,
                waiting_on={(m["to"] or "").strip() for m in messages_to_agents},
            )
            # PR18: arm the timeout watchdog for this chain.
            self._arm_chain_timeout(chain_id)
            for msg in messages_to_agents:
                await self._send_to_agent(
                    to=(msg["to"] or "").strip(),
                    request=(msg["request"] or "").strip(),
                    depth=depth + 1,
                    chain_id=chain_id,
                )
            return

        # PR11-compatible single-hop reply path. Always send (even on empty
        # reply_text) so the requester's chain unwinds.
        if reply_text:
            self._append_history(ChatMessage(
                role="agent", text=reply_text, ts=_now_iso(),
                meta={
                    "source": "agent_response_outgoing",
                    "to_agent": from_agent, "depth": depth,
                    "chain_id": chain_id,
                },
            ))
        await self._send_agent_response(
            to=from_agent, response=reply_text, depth=depth, chain_id=chain_id,
        )

    async def _handle_agent_response(self, payload: dict) -> None:
        """Process an incoming agent_response.

        Two branches:
        - chain_id ∈ self._pending_chains → multi-hop relay. Drop sender
          from waiting_on; when waiting_on becomes empty, re-invoke router
          and forward the synthesized reply (or fresh delegations) on the
          same chain. Reply goes to the chain's `origin_agent`, NOT
          `from_agent`.
        - chain_id ∉ self._pending_chains → user-initiated chain (PR11
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

        pending = self._pending_chains.get(chain_id)
        if pending is not None:
            await self._resolve_pending_chain(
                pending, from_agent=from_agent,
            )
            return

        # User-initiated chain: PR11 path, reply goes to user.
        try:
            decision = await self._invoke_router(response)
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_response): {exc}",
                meta={"chain_id": chain_id},
            ))
            return
        await self._dispatch_routing_decision_for_user(
            decision, chain_id=chain_id, depth=depth,
        )

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
        pending.waiting_on.discard(from_agent)
        if pending.waiting_on:
            return  # still waiting on other delegates

        chain_id = pending.chain_id
        try:
            decision = await self._invoke_router(pending.original_request)
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
            self._pending_chains.pop(chain_id, None)
            self._cancel_chain_timeout(chain_id)
            return

        new_delegations = [
            m for m in (decision.get("messages_to_agents") or [])
            if (m.get("to") or "").strip() and (m.get("request") or "").strip()
        ]
        for spec in decision.get("skills_to_run") or []:
            await self._spawn_skill(spec)

        if new_delegations:
            # Continue the chain with a fresh wave of delegations.
            pending.waiting_on = {(m["to"] or "").strip() for m in new_delegations}
            for msg in new_delegations:
                await self._send_to_agent(
                    to=(msg["to"] or "").strip(),
                    request=(msg["request"] or "").strip(),
                    depth=pending.origin_depth + 1,
                    chain_id=chain_id,
                )
            return

        final_reply = (decision.get("reply_text") or "").strip()
        if final_reply:
            self._append_history(ChatMessage(
                role="agent", text=final_reply, ts=_now_iso(),
                meta={
                    "source": "agent_response_outgoing",
                    "to_agent": pending.origin_agent,
                    "depth": pending.origin_depth,
                    "chain_id": chain_id,
                },
            ))
        await self._send_agent_response(
            to=pending.origin_agent, response=final_reply,
            depth=pending.origin_depth, chain_id=chain_id,
        )
        self._pending_chains.pop(chain_id, None)
        self._cancel_chain_timeout(chain_id)

    # ── chain timeout (PR18) ───────────────────────────────────────────────────

    def _arm_chain_timeout(self, chain_id: str) -> None:
        """Start a watchdog task for `chain_id`. No-op when timeouts are
        disabled (chain_timeout_seconds <= 0). Idempotent — replaces any
        existing timer for the same chain_id."""
        if self._chain_timeout_seconds <= 0:
            return
        existing = self._pending_chain_timers.pop(chain_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        self._pending_chain_timers[chain_id] = asyncio.create_task(
            self._chain_timeout_watch(chain_id)
        )

    def _cancel_chain_timeout(self, chain_id: str) -> None:
        timer = self._pending_chain_timers.pop(chain_id, None)
        if timer is not None and not timer.done():
            timer.cancel()

    async def _chain_timeout_watch(self, chain_id: str) -> None:
        """Watchdog: after `chain_timeout_seconds`, if `chain_id` is still
        pending, synthesize an error response upstream and clear it.

        Cancellation (when the chain resolves normally) raises CancelledError
        out of the sleep — we just exit. shutdown() also cancels these tasks
        and gathers them with `return_exceptions=True`, so a late firing
        during teardown is harmless.
        """
        try:
            await asyncio.sleep(self._chain_timeout_seconds)
        except asyncio.CancelledError:
            return
        pending = self._pending_chains.pop(chain_id, None)
        self._pending_chain_timers.pop(chain_id, None)
        if pending is None:
            return  # resolved between sleep wake and pop — nothing to do
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
            await self._spawn_skill(spec)
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
        """Same shape as `_resolve_run_id` but over `_active_interventions`."""
        prefix = prefix.strip()
        if not prefix:
            return None, []
        candidates = [
            iid for iid in self._active_interventions
            if iid.startswith(prefix) or iid.endswith(prefix)
        ]
        return (candidates[0] if len(candidates) == 1 else None), candidates

    async def _maybe_handle_slash(self, text: str) -> bool:
        """Dispatch `:command args...` lines. Returns True when consumed.

        Unknown slash commands also return True (with a hint on outbox) to
        keep the router from running on user typos like ":halp".
        """
        body = text[1:].lstrip()
        if not body:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=(
                    "known commands: :list, :cancel <id>, :answer <id> <text>, "
                    ":agents, :attach <name>"
                ),
            ))
            return True
        parts = body.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        handler = {
            "list": self._slash_list,
            "cancel": self._slash_cancel,
            "answer": self._slash_answer,
            "agents": self._slash_agents,
            "attach": self._slash_attach,
        }.get(cmd)
        if handler is None:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=(
                    f"unknown command :{cmd}; try :list / :cancel / :answer / "
                    ":agents / :attach"
                ),
            ))
            return True
        await handler(args)
        return True

    async def _slash_list(self, args: str) -> None:
        """`:list` — running skills + pending interventions."""
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
        if self._active_interventions:
            lines.append("pending interventions:")
            for iid in self._intervention_order:
                iv = self._active_interventions[iid]
                short = (iv.run_id[-4:] if iv.run_id else "----")
                lines.append(
                    f"  {iid[:8]}  {iv.kind:<20}  {iv.skill_name or '?'}#{short}"
                )
        await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))

    async def _slash_cancel(self, args: str) -> None:
        """`:cancel <id-prefix>` — cancel a running skill task."""
        prefix = args.strip()
        if not prefix:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: :cancel <id-prefix>",
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
        """`:answer <id-prefix> <text>` — deliver answer to a non-head intervention."""
        parts = args.split(maxsplit=1)
        if not parts:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: :answer <id-prefix> <text>",
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
        iv = self._active_interventions[iid]
        await self._deliver_answer_to(iv, text)

    async def _slash_agents(self, args: str) -> None:
        """`:agents` — list known agents (registry-backed)."""
        if self._registry is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text="agent registry not wired; :agents only works in `reyn chat`",
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
        """`:attach <name>` — switch attached agent.

        The actual switch happens in repl._input_loop, which owns the display
        wiring. Here we only validate the name and put a sentinel attach
        request on this session's outbox; the REPL listens for the kind.
        """
        name = args.strip()
        if not name:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: :attach <name>",
            ))
            return
        if self._registry is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text="agent registry not wired; :attach only works in `reyn chat`",
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

    # ── skill spawn ─────────────────────────────────────────────────────────────

    async def _spawn_skill(self, spec: dict) -> None:
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

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._chat_events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)
        # Track elapsed time for `:list` and provenance for outbox messages
        self.running_skills_started_at[run_id] = time.monotonic()
        await self._put_outbox(OutboxMessage(
            kind="status", text="起動...",
            meta=_run_meta(run_id, skill_name),
        ))

        task = asyncio.create_task(self._run_one_skill(run_id, skill_name, input_artifact))
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

    async def _run_one_skill(self, run_id: str, skill_name: str, input_artifact: dict) -> None:
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, dsl_root = resolve_skill_path(skill_name)
        except SystemExit:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"skill not found: {skill_name}", meta=meta,
            ))
            return
        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), dsl_root=str(dsl_root))
        except Exception as exc:
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
            result = await agent.run(skill, input_artifact, output_language=self.output_language)
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
            fallback = f"完了 (status={result.status})\n{summary}"
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
