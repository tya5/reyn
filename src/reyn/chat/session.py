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
    role: str  # "user" | "agent" | "skill_event"
    text: str
    ts: str
    meta: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        history_window: int = 12,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
    ) -> None:
        self.chat_id = chat_id or _new_chat_id()
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        self._limits = limits or LimitsConfig()
        self._mcp_servers = mcp_servers
        self.output_language = output_language
        self.history_window = history_window
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context

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
        """Cancel any in-flight user-initiated skill runs.

        Memory writes happen inline during each router turn, so there is no
        background extraction to drain — shutdown is now strictly a teardown
        of whatever the user explicitly launched.
        """
        for task in self.running_skills.values():
            task.cancel()
        if self.running_skills:
            await asyncio.gather(*self.running_skills.values(), return_exceptions=True)

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
        """
        history_payload = [
            {"role": m.role, "text": m.text}
            for m in self.history[-self.history_window:]
            if m.role in ("user", "agent")
        ]
        avail = enumerate_available_skills(exclude={ROUTER_SKILL_NAME})

        data: dict = {
            "user_message": user_text,
            "history": history_payload,
            "available_skills": avail,
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
