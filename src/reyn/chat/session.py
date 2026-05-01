"""ChatSession — long-lived chat loop driving the skill_router stdlib skill."""
from __future__ import annotations
import asyncio
import json
import uuid
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


ROUTER_SKILL_NAME = "skill_router"


@dataclass
class ChatMessage:
    role: str  # "user" | "agent" | "skill_event" | "summary"
    text: str
    ts: str
    seq: int = 0  # monotonic per-session sequence id; 0 for non-conversational entries
    meta: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _new_chat_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{uuid.uuid4().hex[:6]}"


def enumerate_available_skills(exclude: set[str]) -> list[dict]:
    """Walk reyn/project, reyn/local, stdlib/skills and collect {name, description}."""
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
            results.append({"name": fm.get("name") or d.name, "description": description})
            seen.add(d.name)
    return results


class ChatSession:
    def __init__(
        self,
        chat_id: str | None = None,
        model: str = "standard",
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        limits: LimitsConfig | None = None,
        mcp_servers: dict | None = None,
        output_language: str = "ja",
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        compaction_config: "CompactionConfig | None" = None,
    ) -> None:
        self.chat_id = chat_id or _new_chat_id()
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        self._limits = limits or LimitsConfig()
        self._mcp_servers = mcp_servers
        self.output_language = output_language
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context

        from reyn.config import CompactionConfig
        self._compaction = compaction_config or CompactionConfig()
        self._next_seq = 1
        self._compacting = False
        self._compaction_task: asyncio.Task | None = None

        self.workspace_dir = Path(".reyn") / "chats" / self.chat_id
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.workspace_dir / "history.jsonl"
        self.events_path = self.workspace_dir / "events.jsonl"
        self.runs_root = self.workspace_dir / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

        self.history: list[ChatMessage] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()

        from reyn.pricing import TokenUsage
        self._total_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0

        self._chat_events = EventLog(subscribers=[EventPersister(self.events_path)])
        self.running_skills: dict[str, asyncio.Task] = {}

        # ask_user routing state. Only one question is "active" at a time;
        # extra questions queue. Each entry: (run_id, skill_name, question, suggestions, future)
        self._active_question: tuple[str, str, asyncio.Future] | None = None
        self._pending_questions: list[tuple[str, str, str, list[str], asyncio.Future]] = []

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
        await self.inbox.put(("user", text))

    async def shutdown(self) -> None:
        await self.inbox.put(("shutdown", ""))

    # ── main loop ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._chat_events.emit("chat_started", chat_id=self.chat_id, model=self.model)

        try:
            while True:
                kind, text = await self.inbox.get()
                if kind == "shutdown":
                    break
                if kind == "user":
                    await self._handle_user_message(text)
        finally:
            await self._drain_on_shutdown()
            self._chat_events.emit("chat_stopped", chat_id=self.chat_id)
            await self.outbox.put(("__end__", ""))

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

        if self._compaction_task is not None and not self._compaction_task.done():
            try:
                await self._compaction_task
            except Exception:
                pass

    async def _handle_user_message(self, text: str) -> None:
        # If a spawned skill is waiting on ask_user, route this input to that skill
        # instead of the router.
        if await self._maybe_answer_active_question(text):
            return

        self._append_history(ChatMessage(role="user", text=text, ts=_now_iso()))
        self._chat_events.emit("user_message_received", text=text)
        await self.outbox.put(("status", "考え中..."))

        try:
            decision = await self._invoke_router(text)
        except Exception as exc:
            await self.outbox.put(("error", f"router failed: {exc}"))
            return

        reply_text = (decision.get("reply_text") or "").strip()
        skills_to_run = decision.get("skills_to_run") or []

        if reply_text:
            await self.outbox.put(("agent", reply_text))
            self._append_history(ChatMessage(role="agent", text=reply_text, ts=_now_iso()))

        for spec in skills_to_run:
            await self._spawn_skill(spec)

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
        user_input_fn=None,
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
            user_input_fn=user_input_fn,
            subscribers=subscribers,
            prompt_cache_enabled=self._prompt_cache_enabled,
            project_context=self._project_context,
        )

    def _load_stdlib_skill(self, skill_name: str):
        """Load a stdlib skill by its directory name. Propagates parse errors."""
        sl = stdlib_root()
        skill_md = sl / "skills" / skill_name / "skill.md"
        return load_dsl_skill(str(skill_md), dsl_root=str(sl))

    def pre_guard_stdlib_skills(self, skill_names: list[str]) -> None:
        """Call startup_guard for each named stdlib skill before the REPL starts.

        startup_guard() uses blocking input(). If called inside run_repl's
        asyncio event loop it blocks prompt_async() and deadlocks. Calling this
        synchronously here, before run_async(), avoids the race.
        """
        if self._perm is None:
            return
        for name in skill_names:
            try:
                skill = self._load_stdlib_skill(name)
                self._perm.startup_guard(skill, name)
            except SystemExit:
                raise
            except Exception:
                pass  # surface the error inside the REPL when the skill runs

    async def _run_stdlib_skill(
        self,
        skill_name: str,
        input_artifact: dict,
        *,
        state_subdir: str,
        user_input_fn=None,
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
        agent = self._build_agent(
            user_input_fn=user_input_fn,
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

    async def _invoke_router(
        self,
        user_text: str,
        skill_completion: dict | None = None,
        state_subdir: str = "router",
    ) -> dict:
        """Run the skill_router skill.

        When `skill_completion` is provided, the router switches to narration
        mode: it produces a natural-language reply describing the result.

        History is NOT inlined into the artifact — the route phase has a
        Python preprocessor step that reads `.reyn/chats/<chat_id>/history.jsonl`
        and slices the recent N turns. This eliminates the snapshot-per-turn
        duplication that previously bloated workspace artifacts.
        """
        avail = enumerate_available_skills(exclude={ROUTER_SKILL_NAME, "chat_compactor"})

        data: dict = {
            "user_message": user_text,
            "chat_id": self.chat_id,
            # Precomputed for the route phase preprocessor: the file/read op
            # uses this via args_from. ChatSession owns this path because the
            # workspace dir was created relative to the cwd at session start.
            "history_path": str(self.history_path),
            "available_skills": avail,
            # Pass the head/tail config through so the slicer can honor it
            # without needing access to ReynConfig.
            "compaction": {
                "head_size": self._compaction.head_size,
                "tail_size": self._compaction.tail_size,
            },
        }
        if skill_completion is not None:
            data["skill_completion"] = skill_completion

        input_artifact = {"type": "chat_routing_request", "data": data}

        result = await self._run_stdlib_skill(
            ROUTER_SKILL_NAME, input_artifact, state_subdir=state_subdir,
            forward_events=True,
        )
        return result.data

    # ── ask_user routing ────────────────────────────────────────────────────────

    async def _maybe_answer_active_question(self, text: str) -> bool:
        """If a skill is awaiting ask_user, deliver `text` to it and return True.

        Stale (cancelled/done) futures are discarded transparently and the next
        pending question is promoted in their place.
        """
        # Clean up any stale active question first
        while self._active_question is not None and self._active_question[2].done():
            self._active_question = None
            await self._promote_next_question()

        if self._active_question is None:
            return False

        run_id, skill_name, future = self._active_question
        future.set_result(text)
        self._append_history(ChatMessage(
            role="user", text=text, ts=_now_iso(),
            meta={"answered_skill": skill_name, "answered_run_id": run_id},
        ))
        self._chat_events.emit(
            "user_answered_skill", run_id=run_id, skill=skill_name, answer=text,
        )
        self._active_question = None
        await self._promote_next_question()
        return True

    async def _promote_next_question(self) -> None:
        """Pop the next pending question (if any) and make it active."""
        while self._pending_questions:
            run_id, skill_name, question, suggestions, future = self._pending_questions.pop(0)
            if future.done():
                continue
            self._active_question = (run_id, skill_name, future)
            await self._announce_question(skill_name, question, suggestions)
            return

    async def _announce_question(self, skill_name: str, question: str, suggestions: list[str]) -> None:
        msg = f"[{skill_name}] 質問: {question}"
        if suggestions:
            msg += f"\n  候補: {' / '.join(suggestions)}"
        await self.outbox.put(("ask", msg))

    def _make_skill_user_input_fn(self, run_id: str, skill_name: str):
        """Build a user_input_fn that surfaces the question through chat outbox
        and waits for the user's next message to fulfill it."""
        async def fn(question: str, suggestions: list[str]) -> str:
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            if self._active_question is None:
                self._active_question = (run_id, skill_name, future)
                await self._announce_question(skill_name, question, suggestions)
            else:
                self._pending_questions.append((run_id, skill_name, question, suggestions, future))
                await self.outbox.put((
                    "status",
                    f"[{skill_name}] 質問待ち ({len(self._pending_questions)}件キュー中)",
                ))
            try:
                return await future
            except asyncio.CancelledError:
                # Skill was cancelled while awaiting; surface empty answer
                return ""
        return fn

    def _drop_question_for_run(self, run_id: str) -> None:
        """Clear any question state belonging to a finished/cancelled run."""
        if self._active_question and self._active_question[0] == run_id:
            _, _, future = self._active_question
            if not future.done():
                future.cancel()
            self._active_question = None
        self._pending_questions = [
            q for q in self._pending_questions if q[0] != run_id
        ]

    # ── skill spawn ─────────────────────────────────────────────────────────────

    async def _spawn_skill(self, spec: dict) -> None:
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            await self.outbox.put(("error", f"invalid skill spec: {spec}"))
            return

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._chat_events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)
        await self.outbox.put(("status", f"[{skill_name}] 起動..."))

        task = asyncio.create_task(self._run_one_skill(run_id, skill_name, input_artifact))
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self._drop_question_for_run(rid)

        task.add_done_callback(_cleanup)

    async def _run_one_skill(self, run_id: str, skill_name: str, input_artifact: dict) -> None:
        try:
            skill_dir, dsl_root = resolve_skill_path(skill_name)
        except SystemExit:
            await self.outbox.put(("error", f"skill not found: {skill_name}"))
            return
        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), dsl_root=str(dsl_root))
        except Exception as exc:
            await self.outbox.put(("error", f"failed to load {skill_name}: {exc}"))
            return

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            user_input_fn=self._make_skill_user_input_fn(run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox)],
        )
        try:
            result = await agent.run(skill, input_artifact, output_language=self.output_language)
        except asyncio.CancelledError:
            await self.outbox.put(("status", f"[{skill_name}] cancelled"))
            raise
        except Exception as exc:
            self._chat_events.emit("skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc))
            await self.outbox.put(("error", f"[{skill_name}] failed: {exc}"))
            return

        self._accumulate(result)
        self._chat_events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
        )

        # Hand the result back to the router so the agent can phrase a
        # natural-language report instead of dumping JSON to the user.
        narrated: str | None = None
        try:
            decision = await self._invoke_router(
                user_text="",
                skill_completion={
                    "skill": skill_name,
                    "status": result.status,
                    "result": result.data,
                },
                state_subdir=f"narrator/{run_id}",
            )
            narrated = (decision.get("reply_text") or "").strip() or None
        except Exception as exc:
            self._chat_events.emit(
                "skill_narration_failed", run_id=run_id, skill=skill_name, error=str(exc),
            )

        if narrated:
            self._append_history(ChatMessage(
                role="agent", text=narrated, ts=_now_iso(),
                meta={"narrated_skill": skill_name, "run_id": run_id, "status": result.status},
            ))
            await self.outbox.put(("agent", narrated))
        else:
            # Fallback: raw dump so the user at least sees something.
            summary = json.dumps(result.data, ensure_ascii=False, indent=2)
            fallback = f"[{skill_name}] 完了 (status={result.status})\n{summary}"
            self._append_history(ChatMessage(
                role="skill_event", text=fallback, ts=_now_iso(),
                meta={"skill": skill_name, "run_id": run_id, "status": result.status},
            ))
            await self.outbox.put(("skill_done", fallback))
