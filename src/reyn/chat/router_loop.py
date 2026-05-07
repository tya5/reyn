"""RouterLoop — drives the chat router via native LLM tool_use (PR35).

Loop: build tools + prompt → call_llm_tools → if tool_calls, execute in
parallel, append results to messages, repeat → if text reply, emit to host
outbox and stop. Bounded by max_iterations.
"""
from __future__ import annotations

import asyncio
import functools
import json
from typing import TYPE_CHECKING, Any, Protocol

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import (
    _DESCRIBE_SKILL_STRIP_FIELDS,
    MAX_DESC_LEN_FOR_LISTING,
    build_tools,
    get_dispatch_kind,
)
from reyn.chat.session import _TOOL_FAILED_FALLBACK_MSG
from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.llm.llm import call_llm_tools
from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Empty-response detection (Option F — ADR-0021)
# ---------------------------------------------------------------------------

# Localized user-facing message when the model returns an empty response
# (finish_reason=stop, no content, no tool calls). Deterministic i18n so
# output_language is always honoured.  P7-clean: no skill / tool names.
# "en" is the global-safe default.
_EMPTY_RESPONSE_MSG: dict[str, str] = {
    "ja": (
        "モデルが空の応答を返しました。"
        " 別の表現で再入力するか、設定を確認してください。"
    ),
    "en": (
        "The model returned an empty response."
        " Please try rephrasing your request or check your configuration."
    ),
}


def _strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block (``---\\n...\\n---\\n``) from
    a memory file's text and return the body alone.

    Used by :meth:`RouterLoop._read_memory_body` to give the LLM the
    actual remembered text instead of metadata fields it doesn't need
    (= ``name`` / ``description`` / ``type``). When the input doesn't
    start with a frontmatter delimiter the original text is returned
    unchanged — handles legacy memory files written before the frontmatter
    convention existed.
    """
    text = content or ""
    if not text.lstrip().startswith("---"):
        return text
    # Find first non-blank line; require it to be exactly "---".
    lines = text.split("\n")
    # Skip leading blanks.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return text
    # Find the closing "---" after the opening one.
    close = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close == -1:
        # No closing delimiter — leave content alone rather than truncating.
        return text
    body_lines = lines[close + 1:]
    # Trim a single leading blank line that conventionally follows the
    # closing delimiter; keep subsequent whitespace as authored.
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return "\n".join(body_lines).rstrip("\n") + ("\n" if body_lines else "")


def _is_empty_router_response(response: Any) -> bool:
    """OS-side detection: model emitted no text and no tool calls.

    Provider-level glitch (observed with weak models such as
    gemini-2.5-flash-lite at ~50% rate — ADR-0021 / B7-G12).  This is
    NOT recovered by Reyn — surfaced to the user as an explicit failure
    for user-side handling (no retry, no context change, no model switch).

    Trigger: finish_reason=="stop", content empty, tool_calls empty.
    """
    if response is None:
        return True
    finish = getattr(response, "finish_reason", None)
    content = getattr(response, "content", None) or ""
    tool_calls = getattr(response, "tool_calls", None) or []
    return finish == "stop" and not content.strip() and not tool_calls


# ---------------------------------------------------------------------------
# Host protocol
# ---------------------------------------------------------------------------

class RouterLoopHost(Protocol):
    """Abstract surface RouterLoop needs. ChatSession will implement this
    in wave 3; for now, define the protocol and use a fake in tests."""

    # Static catalogue access
    chat_id: str
    agent_name: str
    agent_role: str
    # BCP-47 code (e.g. "ja", "en") when the user explicitly configured
    # output_language; None when unset, in which case build_system_prompt
    # skips the language directive entirely so the LLM picks based on
    # the user's input language naturally.
    output_language: str | None

    @property
    def events(self) -> Any:
        """EventLog (has .emit(type: str, **data)) for tool dispatch events."""
        ...

    def list_available_skills(self) -> list[dict]:
        """Each entry: {name, description, routing?, category?}"""
        ...

    def list_available_agents(self) -> list[dict]:
        """Each entry: {name, role, cluster?}"""
        ...

    def get_memory_index(self) -> dict:
        """Returns {status: 'ok'|'not_found', content: str}"""
        ...

    def get_file_permissions(self) -> dict | None:
        """{read: [paths], write: [paths]} or None"""
        ...

    def get_mcp_servers(self) -> list[dict]:
        """[{name, description, ...}, ...]"""
        ...

    def get_web_fetch_allowed(self) -> bool:
        """True if `web.fetch: allow` is in the operator's permissions."""
        ...

    def get_project_context(self) -> str:
        """Project context text (= REYN.md / `project_context_path` content),
        or empty string when the operator has not configured one. Threaded
        into the router's system prompt so the chat reply path knows about
        the user's project — without this, only the skill execution path
        sees REYN.md and casual chat queries get answered without
        project-specific context."""
        ...

    async def web_search(self, *, query: str, max_results: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/search op (DuckDuckGo)."""
        ...

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/fetch op."""
        ...

    # Memory file paths (for list_memory / read_memory_body)
    def memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer ('shared'|'agent') + slug to file path"""
        ...

    def memory_dir(self, layer: str) -> str:
        """Directory for the layer's memory files"""
        ...

    # Action callbacks (async)
    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict: ...

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None: ...

    async def put_outbox(self, *, kind: str, text: str,
                         meta: dict) -> None: ...

    # File ops (via op_runtime/file under permission scope)
    async def file_read(self, path: str) -> str: ...

    async def file_write(self, path: str, content: str) -> dict: ...

    async def file_delete(self, path: str) -> dict: ...

    async def file_list_directory(self, path: str) -> list[dict]: ...

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict: ...

    # MCP ops
    async def mcp_list_servers(self) -> list[dict]: ...

    async def mcp_list_tools(self, server: str) -> list[dict]: ...

    async def mcp_call_tool(self, server: str, tool: str,
                             args: dict) -> dict: ...

    # Resolve router model (config "router" → real model id)
    def resolve_model(self, name: str) -> str: ...


# ---------------------------------------------------------------------------
# RouterLoop
# ---------------------------------------------------------------------------

class RouterLoop:
    """Drives the chat router via native LLM tool_use.

    Loops: build tools+prompt → call_llm_tools → if tool_calls, execute
    in parallel, append results to messages, repeat → if text, emit to
    outbox and stop. Bounded by max_iterations.
    """

    def __init__(
        self,
        host: RouterLoopHost,
        chain_id: str,
        max_iterations: int = 5,
        router_model: str = "light",  # config tier (light = intent classification)
        budget: Any = None,  # BudgetTracker | None — process-shared cost tracker
    ):
        self.host = host
        self.chain_id = chain_id
        self.max_iterations = max_iterations
        self.router_model = router_model
        self.budget = budget
        self._catalog: dict[str, dict] = {}  # populated per run()
        self._tool_names: frozenset[str] = frozenset()  # kept for backward compat
        self._total_usage: TokenUsage = TokenUsage()

    @property
    def total_usage(self) -> TokenUsage:
        """Accumulated token usage across all LLM calls made in this loop."""
        return self._total_usage

    async def run(self, user_text: str, history: list[dict]) -> TokenUsage:
        """Process one user utterance end-to-end. Emits to host.put_outbox.

        Returns the total TokenUsage accumulated across all LLM calls so the
        caller can credit it to the session-level usage counter (F4 Bug 2).
        """
        self._total_usage = TokenUsage()
        host = self.host
        tools = build_tools(
            host.list_available_skills(),
            host.list_available_agents(),
            file_permissions=host.get_file_permissions(),
            mcp_servers=host.get_mcp_servers(),
            web_fetch_allowed=host.get_web_fetch_allowed(),
        )
        self._catalog = {t["function"]["name"]: t for t in tools}
        self._tool_names = frozenset(self._catalog.keys())  # backward compat
        system_prompt = build_system_prompt(
            agent_name=host.agent_name,
            agent_role=host.agent_role,
            available_skills=host.list_available_skills(),
            available_agents=host.list_available_agents(),
            memory_index=host.get_memory_index(),
            file_permissions=host.get_file_permissions(),
            mcp_servers=host.get_mcp_servers(),
            web_fetch_allowed=host.get_web_fetch_allowed(),
            output_language=host.output_language,
            project_context=host.get_project_context(),
        )
        # ChatSession._handle_user_message appends the user turn to history
        # BEFORE invoking _run_router_loop, so by the time we get here the
        # caller's `history` argument already ends with this turn's user
        # message. Appending it again as a trailing user message creates a
        # consecutive-duplicate-user pair that confuses the LLM (= G12-style
        # empty-stop attractor was reproduced via mcp_probe at ~80% rate
        # against gemini-2.5-flash-lite). Use history as-is; only fall back
        # to an explicit append if for some reason the latest history entry
        # is NOT this turn's user text (= defensive — keeps tests that pass
        # an empty / mismatched history alive).
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        if not history or history[-1].get("role") != "user" or history[-1].get("content") != user_text:
            messages.append({"role": "user", "content": user_text})

        for _iteration in range(self.max_iterations):
            result = await call_llm_tools(
                model=host.resolve_model(self.router_model),
                messages=messages,
                tools=tools,
                tool_choice="auto",
                skill_name="router",
                budget=self.budget,
                budget_agent=host.agent_name,
                trace_caller="router",
            )
            if result.usage:
                self._total_usage += result.usage
            if result.tool_calls:
                # F5 fix (dogfood batch 1): dedupe duplicate async
                # tool_calls within the same round. Weak models
                # occasionally emit `delegate_to_agent` twice in one
                # tool_calls list, which would inbox_put the same
                # request twice and double-charge the peer.
                # G3 fix (dogfood batch 5 B5-M1): extend dedupe to
                # invoke_skill — same skill + same args in one round
                # spawns redundant runs (333k tokens / 51 LLM calls
                # observed). invoke_skill is sync but NOT idempotent
                # from a cost perspective; deduping is safe because
                # same args → same deterministic result.
                tool_calls = self._dedupe_tool_calls_round(result.tool_calls)
                # parallel execute all tool calls (deduped)
                tool_results = await asyncio.gather(*[
                    self._execute_tool(tc) for tc in tool_calls
                ])
                # Detect async-deferred dispatches via dispatch_kind
                # registry (router_tools._DISPATCH_KIND). Async tools'
                # results arrive via a separate channel (e.g.
                # delegate_to_agent → PR14 pending_chain re-invokes router
                # in a future turn). The current loop can't wait for the
                # result; if we continue, the LLM would see only "dispatched"
                # status and re-dispatch (per dogfood verify_lead repro).
                # Exit after the dispatch; the future invocation resumes.
                async_count = sum(
                    1
                    for tc in tool_calls
                    if get_dispatch_kind(tc["function"]["name"]) == "async"
                )
                if async_count:
                    plural = "s" if async_count > 1 else ""
                    await self.host.put_outbox(
                        kind="status",
                        text=(
                            f"dispatched {async_count} async request{plural}; "
                            f"awaiting peer reply"
                        ),
                        meta={"chain_id": self.chain_id},
                    )
                    return self._total_usage
                # G10 / B2-M2 fix: intercept invoke_skill tool_failed results and
                # emit a deterministic i18n message instead of letting the LLM
                # generate an English fallback reply. Checked before accumulating
                # messages so the LLM is never called for this error path.
                for tc, r in zip(tool_calls, tool_results):
                    if (
                        tc["function"]["name"] == "invoke_skill"
                        and isinstance(r, dict)
                        and r.get("status") == "error"
                    ):
                        try:
                            args = json.loads(tc["function"].get("arguments", "{}"))
                        except (json.JSONDecodeError, KeyError):
                            args = {}
                        tool_name = args.get("name", "invoke_skill")
                        err_info = r.get("error", {})
                        error_msg = (
                            err_info.get("message", str(r))
                            if isinstance(err_info, dict)
                            else str(err_info)
                        )
                        lang = getattr(host, "output_language", None)
                        tmpl = _TOOL_FAILED_FALLBACK_MSG.get(
                            lang,
                            _TOOL_FAILED_FALLBACK_MSG["en"],
                        )
                        fallback = tmpl.format(
                            tool_name=tool_name, error=error_msg
                        )
                        await host.put_outbox(
                            kind="agent",
                            text=fallback,
                            meta={"chain_id": self.chain_id},
                        )
                        return self._total_usage

                # No delegation — accumulate messages for next iteration.
                # Use deduped tool_calls so the assistant message and tool
                # result messages stay in sync (matching tool_call_ids).
                messages.append({
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": tool_calls,
                })
                for tc, r in zip(tool_calls, tool_results):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(r, default=str),
                    })
                continue

            # Option F (ADR-0021): detect empty-stop before treating as text reply.
            # Empty-stop = finish_reason="stop", content empty, no tool calls.
            # This is a provider-level glitch (observed at ~50% rate with weak
            # models — B7-G12 measurement).  Reyn does NOT retry, change context,
            # or switch models.  Responsibility: observe + surface to user.
            if _is_empty_router_response(result):
                # P6: emit audit event — state change must be observable.
                self.host.events.emit(
                    "router_empty_response_detected",
                    finish_reason=result.finish_reason,
                    completion_tokens=getattr(result.usage, "completion_tokens", 0)
                    if result.usage else 0,
                    prompt_tokens=getattr(result.usage, "prompt_tokens", 0)
                    if result.usage else 0,
                    caller_hint="router",
                    model=host.resolve_model(self.router_model),
                )
                lang = getattr(host, "output_language", None)
                failure_text = _EMPTY_RESPONSE_MSG.get(
                    lang, _EMPTY_RESPONSE_MSG["en"]
                )
                await host.put_outbox(
                    kind="agent",
                    text=failure_text,
                    meta={
                        "chain_id": self.chain_id,
                        "source": "router_empty_response",
                    },
                )
                return self._total_usage  # no retry

            # Text reply — emit and stop
            await self.host.put_outbox(
                kind="agent",
                text=result.content or "",
                meta={"chain_id": self.chain_id},
            )
            return self._total_usage

        # max_iterations exhausted
        await self.host.put_outbox(
            kind="error",
            text=f"Router loop exceeded max iterations ({self.max_iterations}).",
            meta={"chain_id": self.chain_id},
        )
        return self._total_usage

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    def _dedupe_tool_calls_round(self, tool_calls: list[dict]) -> list[dict]:
        """Dedupe duplicate tool_calls within the same round (F5 + G3).

        Covers two categories of duplicates that weak models emit:

        1. Async tools (e.g. `delegate_to_agent`) — F5 fix (batch 1).
           Duplicates would inbox_put the same request twice, doubling
           peer cost and confusing the chain.

        2. `invoke_skill` — G3 fix (batch 5 B5-M1).
           Three identical invoke_skill calls in one round caused 333k
           tokens / 51 LLM calls. Same skill + same args → same result;
           only the first call is needed.

        Keyed on (tool_name, arguments_json). The original tool_call_id
        is preserved for the kept copy so the assistant/tool message
        alignment downstream stays intact.

        Emits a `tool_call_deduped` audit event per suppressed call.
        """
        # Tools that are dedupe candidates: async tools (by dispatch kind)
        # and invoke_skill (sync but non-idempotent from a cost standpoint).
        # Other sync tools (describe_skill, list_skills, read_file, …) are
        # deliberately excluded — dupes there are wasteful but
        # correctness-preserving and the tool_call_id count must stay
        # consistent with what the LLM emitted.
        _DEDUPE_SYNC_TOOLS: frozenset[str] = frozenset({"invoke_skill"})

        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for tc in tool_calls:
            name = tc["function"]["name"]
            is_async = get_dispatch_kind(name) == "async"
            is_dedupe_sync = name in _DEDUPE_SYNC_TOOLS
            if is_async or is_dedupe_sync:
                key = (name, tc["function"].get("arguments", ""))
                if key in seen:
                    reason = (
                        "duplicate_async_in_round"
                        if is_async
                        else "duplicate_invoke_skill_in_round"
                    )
                    self.host.events.emit(
                        "tool_call_deduped",
                        name=name,
                        chain_id=self.chain_id,
                        reason=reason,
                    )
                    continue
                seen.add(key)
            deduped.append(tc)
        return deduped

    # Keep backward-compat alias (tests and callers that reference the old name
    # will still work; the alias delegates to the unified implementation).
    _dedupe_async_tool_calls = _dedupe_tool_calls_round

    async def _execute_tool(self, tc: dict) -> dict:
        """Dispatch one tool call via dispatch_tool (cross-cutting concerns).

        Returns the tool_result content (will be JSON-serialized into the
        next round's messages).
        """
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

        dctx = DispatchContext(
            caller_kind="router",
            caller_id=self.host.agent_name,
            chain_id=self.chain_id,
            tool_catalog=self._catalog,
            events=self.host.events,
        )

        return await dispatch_tool(
            name=name,
            args=args,
            ctx=dctx,
            invoker=functools.partial(self._invoke_router_tool, name),
        )

    async def _invoke_router_tool(self, name: str, args: dict) -> Any:
        """Execute a validated tool call by name.

        Called by dispatch_tool after name/args validation. The if/elif
        tree from the old _execute_tool lives here — assumes name is valid.
        """
        # A. Discovery
        if name == "list_skills":
            return self._list_skills(args.get("path", ""))
        if name == "describe_skill":
            return self._describe_skill(args.get("name", ""))
        if name == "list_agents":
            return self._list_agents(args.get("path", ""))
        if name == "describe_agent":
            return self._describe_agent(args.get("name", ""))
        if name == "list_memory":
            return self._list_memory(args.get("path", ""))
        if name == "read_memory_body":
            return await self._read_memory_body(
                args.get("layer", ""), args.get("slug", "")
            )

        # B. Action
        if name == "invoke_skill":
            skill_name = args["name"]
            # Layer B: defense-in-depth — verify skill name at runtime.
            # Only enforced when the host exposes a non-empty skills list
            # (same precondition as Layer A enum in build_tools).
            available_skills = {s["name"] for s in self.host.list_available_skills()}
            if available_skills and skill_name not in available_skills:
                raise ValueError(
                    f"skill {skill_name!r} not found; "
                    f"available: {sorted(available_skills)}"
                )
            return await self.host.run_skill_awaitable(
                skill=skill_name,
                input=args["input"],
                chain_id=self.chain_id,
            )
        if name == "delegate_to_agent":
            await self.host.send_to_agent(
                to=args["to"],
                request=args["request"],
                depth=0,
                chain_id=self.chain_id,
            )
            # 案 f-1: clear note that the reply arrives asynchronously and
            # the LLM should wait for it. RouterLoop also exits after
            # detecting any async dispatch (defense in depth via
            # router_tools.get_dispatch_kind); if the loop ever did
            # continue, this message tells the LLM what state we're in.
            return {
                "status": "dispatched",
                "to": args["to"],
                "note": (
                    "Peer's reply will arrive in a future router invocation; "
                    "please wait for it."
                ),
            }
        if name in ("remember_shared", "remember_agent"):
            layer = "shared" if name == "remember_shared" else "agent"
            return await self._remember(layer=layer, **args)
        if name == "forget_memory":
            return await self._forget(args["layer"], args["slug"])

        # C. File
        if name == "list_directory":
            return await self.host.file_list_directory(args["path"])
        if name == "read_file":
            return await self.host.file_read(args["path"])
        if name == "write_file":
            return await self.host.file_write(args["path"], args["content"])
        if name == "delete_file":
            return await self.host.file_delete(args["path"])

        # D. MCP
        if name == "list_mcp_servers":
            return await self.host.mcp_list_servers()
        if name == "list_mcp_tools":
            return await self.host.mcp_list_tools(args["server"])
        if name == "call_mcp_tool":
            return await self.host.mcp_call_tool(
                args["server"], args["tool"], args["args"]
            )

        # E. Web (OS-native ops, not skills) — delegate to host so the
        # underlying op_runtime/web.py handlers run with the same OpContext
        # plumbing as Control IR phase invocations.
        if name == "web_search":
            return await self.host.web_search(
                query=args["query"],
                max_results=args.get("max_results", 5),
            )
        if name == "web_fetch":
            return await self.host.web_fetch(
                url=args["url"],
                max_length=args.get("max_length", 50_000),
            )

        # Should not be reached if catalog is correct — dispatch_tool already
        # validated name is in catalog. Return error for safety.
        return {"error": f"unhandled tool: {name}"}

    # -----------------------------------------------------------------------
    # Discovery helpers (pure, no async host calls)
    # -----------------------------------------------------------------------

    @staticmethod
    def _skill_item(s: dict) -> dict:
        """Build a list_skills item from a catalogue entry.

        Always includes ``name`` and ``description``. Passes through
        ``input_artifact`` and ``input_fields`` when present so the LLM
        sees the correct input field names before calling ``invoke_skill``
        (RETRO-H2 fix — plan D: pre-call structural context provision).

        Description is truncated to MAX_DESC_LEN_FOR_LISTING chars + "..."
        to mitigate the G12 empty-stop attractor (B7 finding: skill
        description verbosity triggers the attractor — a62a9dad / a947255e).
        describe_skill returns the full description (details on demand).
        """
        raw_desc = s.get("description", "")
        if len(raw_desc) > MAX_DESC_LEN_FOR_LISTING:
            desc = raw_desc[:MAX_DESC_LEN_FOR_LISTING] + "..."
        else:
            desc = raw_desc
        item: dict = {
            "name": s["name"],
            "description": desc,
        }
        if "input_artifact" in s:
            item["input_artifact"] = s["input_artifact"]
        if "input_fields" in s:
            item["input_fields"] = s["input_fields"]
        return item

    def _list_skills(self, path: str) -> list[dict]:
        """Browse skill catalogue hierarchically.

        path == "" → group by category, return [{category, count, sample_names}, ...]
        path == "<category>" → return [{name, description, input_artifact?, input_fields?}, ...]

        The ``sample_names`` preview (up to 5 names per category) was added to
        defuse a G12 empty-stop attractor: with only ``count`` in the
        response, the LLM had nothing concrete to narrate when answering
        "list available skills" and exited with ``finish=stop`` /
        ``content=""``. Surfacing actual skill names gives it material to
        speak with — confirmed via dogfood trace re-run.
        """
        skills = self.host.list_available_skills()

        if not path:
            # Group by category, with a small sample of names per category to
            # defuse the empty-stop attractor on path="".
            categories: dict[str, list[dict]] = {}
            for skill in skills:
                cat = skill.get("category") or "general"
                categories.setdefault(cat, []).append(skill)
            return [
                {
                    "category": cat,
                    "count": len(items),
                    # Up to 5 names per category — concrete enough to narrate,
                    # bounded enough to stay under the verbosity attractor
                    # threshold for projects with hundreds of skills.
                    "sample_names": [s.get("name", "") for s in items[:5]],
                }
                for cat, items in sorted(categories.items())
            ]

        # Return skills in the given category
        by_category = [
            self._skill_item(s)
            for s in skills
            if (s.get("category") or "general") == path
        ]
        if by_category:
            return by_category

        # path didn't match any category — try as a skill name (fallback)
        by_name = [
            self._skill_item(s)
            for s in skills
            if s.get("name") == path
        ]
        return by_name

    def _describe_skill(self, name: str) -> dict:
        """Return router-optimised entry for one skill, or error dict.

        Returns the catalogue entry with internal-only fields stripped
        (``routing``, ``category``) to keep the tool_response concise.
        The ``routing`` block (when_to_use / when_not_to_use / examples)
        averages 800–1400 chars and triggers the G12 P-b verbosity attractor
        when included in the describe_skill tool_response (Pattern D —
        B11-R2 diagnosis: 20% → 0% empty-stop after stripping).

        ``name``, ``description``, ``input_artifact``, and ``input_fields``
        are preserved — they are all the router needs to build a valid
        invoke_skill call.  (P7-clean: filtering uses OS-level field names
        in ``_DESCRIBE_SKILL_STRIP_FIELDS``, not any skill-specific strings.)
        """
        for skill in self.host.list_available_skills():
            if skill.get("name") == name:
                return {k: v for k, v in skill.items() if k not in _DESCRIBE_SKILL_STRIP_FIELDS}
        return {"error": f"skill not found: {name}"}

    def _list_agents(self, path: str) -> list[dict]:
        """Browse agent catalogue hierarchically.

        path == "" → group by cluster, return [{cluster, count}, ...]
        path == "<cluster>" → return [{name, role}, ...] for that cluster
        """
        agents = self.host.list_available_agents()

        if not path:
            clusters: dict[str, list[dict]] = {}
            for agent in agents:
                cluster = agent.get("cluster") or "default"
                clusters.setdefault(cluster, []).append(agent)
            return [
                {"cluster": cluster, "count": len(items)}
                for cluster, items in sorted(clusters.items())
            ]

        return [
            {"name": a["name"], "role": a.get("role", "")}
            for a in agents
            if (a.get("cluster") or "default") == path
        ]

    def _describe_agent(self, name: str) -> dict:
        """Return full entry for one agent, or error dict."""
        for agent in self.host.list_available_agents():
            if agent.get("name") == name:
                return agent
        return {"error": f"agent not found: {name}"}

    def _list_memory(self, path: str) -> list[dict]:
        """Browse memory hierarchically.

        path == "" → [{path: "shared", count: N}, {path: "agent", count: M}]
        path == "shared" or "agent" → sub-type counts
        path == "shared/<type>" or "agent/<type>" → items in that layer+type
        """
        memory_index = self.host.get_memory_index()
        content = memory_index.get("content", "") if memory_index.get("status") == "ok" else ""

        if not path:
            shared_count = self._count_memory_layer(content, "shared")
            agent_count = self._count_memory_layer(content, "agent")
            return [
                {"path": "shared", "count": shared_count},
                {"path": "agent", "count": agent_count},
            ]

        parts = path.split("/", 1)
        layer = parts[0]  # "shared" or "agent"

        if len(parts) == 1:
            # Return sub-categories (types) for this layer
            type_counts = self._count_memory_types(content, layer)
            return [
                {"path": f"{layer}/{mtype}", "count": count}
                for mtype, count in sorted(type_counts.items())
                if count > 0
            ]

        # path == "shared/user" etc. → return items matching layer + type
        mtype = parts[1]
        return self._list_memory_items(content, layer, mtype)

    def _count_memory_layer(self, content: str, layer: str) -> int:
        """Count total entries in the given memory layer from index content."""
        import re
        total = 0
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        slug_re = re.compile(r"\(([^)]+)\.md\)")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if in_layer:
                for _ in slug_re.finditer(line):
                    total += 1
        return total

    def _count_memory_types(self, content: str, layer: str) -> dict[str, int]:
        """Return {type: count} for a given layer."""
        import re
        counts: dict[str, int] = {}
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        slug_re = re.compile(r"\(([^)]+)\.md\)")
        type_re = re.compile(r"^(user|feedback|project|reference)_")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if in_layer:
                for slug_m in slug_re.finditer(line):
                    slug = slug_m.group(1)
                    tm = type_re.match(slug)
                    if tm:
                        mtype = tm.group(1)
                        counts[mtype] = counts.get(mtype, 0) + 1
        return counts

    def _list_memory_items(
        self, content: str, layer: str, mtype: str
    ) -> list[dict]:
        """Return [{slug, name, description}, ...] for layer+type."""
        import re
        items: list[dict] = []
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        # Match "- [Name](slug.md) — description" or table rows
        entry_re = re.compile(
            r"\[([^\]]+)\]\(([^)]+)\.md\)(?:\s*[—–-]+\s*(.+))?"
        )
        type_re = re.compile(r"^(user|feedback|project|reference)_")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if not in_layer:
                continue
            for em in entry_re.finditer(line):
                name = em.group(1)
                slug = em.group(2)
                desc = (em.group(3) or "").strip()
                tm = type_re.match(slug)
                if tm and tm.group(1) == mtype:
                    items.append({"slug": slug, "name": name, "description": desc})
        return items

    async def _read_memory_body(self, layer: str, slug: str) -> dict:
        """Read the full body of a memory entry.

        Memory files are stored as Markdown with a YAML frontmatter (= the
        ``name`` / ``description`` / ``type`` metadata fields written by
        ``_remember``). Returning the full file content with the frontmatter
        intact triggered a G12 empty-stop attractor: when the LLM asked
        ``read_memory_body`` and got back e.g.::

            ---
            name: User Name
            description: User Name
            type: user
            ---

            Yasuda

        it sometimes parsed the frontmatter as the content and exited with
        ``finish=stop`` / ``content=""`` instead of narrating "Yasuda".
        Confirmed via dogfood trace on Q10 ``who am I?`` — the recall
        returned the body with frontmatter and produced an empty reply.

        Stripping the frontmatter before returning gives the LLM clean
        text to narrate. The metadata fields are not LLM-actionable here
        (they were emitted at write time and are surfaced separately via
        ``list_memory``), so dropping them costs nothing.
        """
        path = self.host.memory_path(layer, slug)
        try:
            content = await self.host.file_read(path)
            return {
                "content": _strip_frontmatter(content),
                "layer": layer,
                "slug": slug,
            }
        except Exception as exc:
            return {"error": str(exc), "layer": layer, "slug": slug}

    async def _remember(
        self,
        *,
        layer: str,
        slug: str,
        name: str,
        description: str,
        type: str,
        body: str,
    ) -> dict:
        """Write a memory entry and regenerate the index."""
        # Defensive: strip trailing .md if LLM emitted it in slug despite
        # the tool description saying "Filename stem".
        if slug.endswith(".md"):
            slug = slug[:-3]
        frontmatter = (
            f"---\nname: {name}\ndescription: {description}\ntype: {type}\n---\n\n{body}\n"
        )
        # memory_path appends .md itself — pass bare slug.
        file_path = self.host.memory_path(layer, slug)
        await self.host.file_write(file_path, frontmatter)

        mem_dir = self.host.memory_dir(layer)
        index_path = mem_dir + "/MEMORY.md"
        await self.host.file_regenerate_index(
            mem_dir,
            index_path,
            "- [{name}]({slug}.md) — {description}",
            "# Memory Index\n\n",
        )
        return {"saved": slug, "layer": layer}

    async def _forget(self, layer: str, slug: str) -> dict:
        """Delete a memory entry and regenerate the index."""
        # Defensive: strip trailing .md if LLM emitted it.
        if slug.endswith(".md"):
            slug = slug[:-3]
        # memory_path appends .md itself.
        file_path = self.host.memory_path(layer, slug)
        await self.host.file_delete(file_path)

        mem_dir = self.host.memory_dir(layer)
        index_path = mem_dir + "/MEMORY.md"
        await self.host.file_regenerate_index(
            mem_dir,
            index_path,
            "- [{name}]({slug}.md) — {description}",
            "# Memory Index\n\n",
        )
        return {"deleted": slug, "layer": layer}
