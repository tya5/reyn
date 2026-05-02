import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Coroutine, TypeVar
import litellm
from reyn.schemas.models import ContextFrame
from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    from reyn.budget.budget import BudgetTracker

T = TypeVar("T")


async def shutdown_logging() -> None:
    """Drain LiteLLM's async logging worker before the event loop closes.

    Background:
      LiteLLM enqueues an `async_success_handler` coroutine into a
      process-wide `LoggingWorker` queue after every `acompletion()`.
      In short-lived `asyncio.run` scripts (our case) the loop closes
      before the worker pulls those items, the coroutines are
      garbage-collected unawaited, and Python emits
      `RuntimeWarning: coroutine 'Logging.async_success_handler' was never awaited`.

      LiteLLM tracks this as a known issue and added the `clear_queue()`
      API as the recommended drain point:
        - Issue: https://github.com/BerriAI/litellm/issues/13970
        - Fix:   https://github.com/BerriAI/litellm/pull/14050

      The fix's worker-side `except CancelledError: await clear_queue()`
      doesn't fully cover us because the cancellation handler may not
      complete before the loop dies. Calling `clear_queue()` explicitly
      from `run_async` — while the loop is still alive — closes the gap.

      If LiteLLM ever guarantees clean drain in `asyncio.run` shutdown
      without caller intervention, this function and `run_async` become
      thin wrappers and can be removed.
    """
    try:
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
        await GLOBAL_LOGGING_WORKER.clear_queue()
    except Exception:
        # Best-effort: never raise from shutdown.
        pass


def run_async(coro: Coroutine[object, object, T]) -> T:
    """`asyncio.run` plus LiteLLM logging-worker drain. See `shutdown_logging`."""
    async def _wrapped() -> T:
        try:
            return await coro
        finally:
            await shutdown_logging()

    return asyncio.run(_wrapped())


@dataclass
class LLMCallResult:
    data: dict
    usage: TokenUsage | None


@dataclass
class LLMToolCallResult:
    """Result for tool_use loop. Returns the raw assistant message so the
    caller can branch on tool_calls vs text content."""
    content: str | None              # text content, may be None or ""
    tool_calls: list                 # provider-normalized list (litellm shape:
                                     # [{id, type:"function", function:{name, arguments}}, ...]),
                                     # empty list if none
    finish_reason: str | None
    usage: TokenUsage
    # raw message for debugging:
    raw_message: object | None = None

_SYSTEM_BASE = """\
You are an AI agent executing a phase in a structured workflow.
Respond with ONLY valid JSON — no markdown fences, no explanation, no comments.

You have TWO output formats depending on whether you need to perform operations first.

━━━ FORMAT A: act turn (perform operations, then be re-called with results) ━━━
Use this when you need to read a file, ask the user, or invoke a tool BEFORE deciding.
{
  "type": "act",
  "ops": [<op>, ...]
}
The OS will execute the ops and call you again with results in control_ir_results.
Leave ops non-empty — an act turn with empty ops is useless.

━━━ FORMAT B: decide turn (routing decision + artifact) ━━━
Use this when you have all the information needed to complete the phase.
{
  "type": "decide",
  "control": {
    "type": "transition|finish|abort",
    "decision": "continue|finish|abort",
    "next_phase": "<phase_name> or null",
    "confidence": 0.0-1.0,
    "reason": {"summary": "one-sentence explanation"}
  },
  "artifact": {"type": "<schema_name>", "data": {...}},
  "ops": []
}
ops in a decide turn: only write ops are useful here (reads would require another act turn).
Leave ops empty ([]) if no writes are needed.

━━━ DECIDE TURN RULES ━━━
control.type:
- "transition": move to next_phase (must be non-null, must be in candidate_outputs).
- "finish": end the workflow. next_phase MUST be null. Only when "end" is in candidate_outputs.
- "abort": unrecoverable error. next_phase MUST be null.
- "rollback": send the immediately preceding phase back for revision. next_phase MUST be null.
  Use when the current phase rejects the preceding phase's output and needs it revised.
  The OS determines the rollback target automatically — do NOT specify next_phase.
  artifact may be empty ({}) for rollback; put the rejection reason in control.reason.summary.

control.decision:
- "continue": normal transition to any next phase (also used for rollback).
- "finish": workflow complete. type MUST be "finish", next_phase MUST be null.
- "abort": cannot continue. type MUST be "abort".

Consistency requirements (violations cause rejection):
- type="finish"   → decision="finish", next_phase=null
- type="transition" → next_phase is non-null
- type="abort"    → decision="abort", next_phase=null
- type="rollback" → decision="continue", next_phase=null

control.reason MUST be {"summary": "..."} — NOT a plain string.
control.confidence MUST be a float in [0.0, 1.0].

Artifact rules:
- artifact MUST always have: {"type": "<schema_name>", "data": {...}}
  - "type" is the schema_name of the chosen candidate_output.
  - "data" contains ONLY fields defined in the candidate's artifact_schema.
- Do NOT put "type" inside the "data" object.
- All user-facing text in artifact.data MUST be in the language specified by output_language.

━━━ ops rules (both turns) ━━━
- Available op kinds and schemas are listed in available_control_ops in the context.
- Use only listed kinds; unknown kinds are skipped.
- op_catalog (when present) is a reference list of every Control IR op kind the OS supports — meta-skills (those that author or modify other skills' phase frontmatter) consult it when choosing `allowed_ops` values for the phases they generate. Normal phases ignore it.

━━━ control_ir_results ━━━
- When non-empty, this is a re-call after your previous act turn.
- Each entry is the result of one op you previously requested. Common shapes:
    file read:  {"kind": "file", "op": "read", "path": "...", "content": "...", "status": "ok"}
    ask_user:   {"kind": "ask_user", "question": "...", "answer": "...", "status": "ok"}
    lint:       {"kind": "lint", "skill_path": "reyn/local/my_skill", "passed": true, "error_count": 0, "warning_count": 1, "issues": [...], "status": "ok"}
    eval:       {"kind": "eval", "spec_path": "...", "passed": true, "overall_score": 0.95, "passed_criteria": 19, "total_criteria": 20, "weakest_phase": "...", "status": "ok"}
- Use these results together with input_artifact to complete the phase goal.
- Once you have what you need, output a decide turn to make your routing decision.

━━━ artifact_ref ━━━
- When input_artifact has "type": "artifact_ref", the artifact is too large to inline.
- Fields: {"type": "artifact_ref", "artifact_type": "...", "ref_path": "...", "size_bytes": N}
- To read its content, emit an act turn with op=read on ref_path before deciding:
    {"type": "act", "ops": [{"kind": "file", "op": "read", "path": "<ref_path>"}]}
"""


def _system_prompt(
    skill_name: str = "",
    skill_description: str = "",
    phase_role: str | None = None,
    project_context: str = "",
    agent_role: str = "",
) -> str:
    """Compose the system prompt: format contract + skill goal + role + project context.

    Stable, persona-bearing fields live here (system) so the LLM treats them
    as authoritative role definitions; volatile per-phase data stays in the
    user-turn ContextFrame JSON.

    `agent_role` (multi-agent: PR10) is the persona text from
    `.reyn/agents/<name>/profile.yaml`. It applies to every phase the agent
    runs and sits between the project context and the per-phase role so the
    agent's overall persona doesn't shadow phase-specific responsibilities.
    """
    sections: list[str] = [_SYSTEM_BASE]
    if skill_name or skill_description:
        sections.append(
            f"━━━ SKILL ━━━\n{skill_name}\n{skill_description}".strip()
        )
    if phase_role:
        sections.append(
            f"━━━ ROLE ━━━\nYou are acting as: {phase_role}"
        )
    if project_context:
        sections.append(
            f"━━━ PROJECT CONTEXT ━━━\n{project_context.strip()}"
        )
    if agent_role:
        sections.append(
            f"━━━ AGENT ROLE ━━━\n{agent_role.strip()}"
        )
    return "\n\n".join(sections)


def _extract_json(text: str) -> str:
    """
    Strip markdown code fences wrapping the entire response.
    Only matches fences that surround the whole text, not embedded ones
    (e.g. code blocks inside article body).
    Falls back to the original text if extraction yields an empty string.
    """
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)```\s*$", stripped, re.DOTALL)
    if match:
        inner = match.group(1).strip()
        if inner:
            return inner
    return stripped


def _repair_json(text: str) -> str:
    """Remove trailing commas — the most common LLM JSON mistake."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _extract_usage(response) -> TokenUsage | None:
    """Extract token usage from a litellm response object."""
    try:
        u = response.usage
        if u is None:
            return None
        return TokenUsage(
            prompt_tokens=int(u.prompt_tokens or 0),
            completion_tokens=int(u.completion_tokens or 0),
        )
    except Exception:
        return None


def proxy_kwargs() -> dict:
    """Return extra kwargs for litellm.completion() when a proxy is configured.

    api_base is read from LITELLM_API_BASE (set by CLI from reyn.yaml).
    API keys are read automatically by litellm from provider env vars
    (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) — never passed explicitly here.
    """
    api_base = os.environ.get("LITELLM_API_BASE")
    if not api_base:
        return {}
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    return {"api_base": api_base, "custom_llm_provider": "openai", "api_key": api_key}


def _build_system_message(system_text: str, prompt_cache_enabled: bool) -> dict:
    """Build the system message, optionally with an Anthropic cache_control marker.

    cache_control={"type": "ephemeral"} tells Anthropic models (and AWS Bedrock
    Claude) to cache the system-prompt prefix for ~5 minutes, eliminating
    re-encoding cost on subsequent calls. Providers that don't recognize the
    marker (Gemini, OpenAI proxy, etc.) ignore the extra field — the multi-block
    content array itself is part of the OpenAI chat-completions spec since the
    multimodal extension and is accepted as plain text by all major providers.
    """
    if not prompt_cache_enabled:
        return {"role": "system", "content": system_text}
    return {
        "role": "system",
        "content": [
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
        ],
    }


async def call_llm(
    model: str,
    frame: ContextFrame,
    prior_attempts: list[dict[str, str]] | None = None,
    rollback_context: dict | None = None,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    prompt_cache_enabled: bool = True,
    skill_name: str = "",
    skill_description: str = "",
    phase_role: str | None = None,
    project_context: str = "",
    agent_role: str = "",
    budget: "BudgetTracker | None" = None,
    budget_agent: str | None = None,
) -> LLMCallResult:
    """
    Call the LLM and return a parsed JSON dict.

    prior_attempts: list of {"raw": str, "error": str} from previous phase retries.
      Each entry is appended as an assistant/user turn so the LLM sees what was wrong.
    rollback_context: {"rejected_artifact": dict, "reason": str, "rollback_from": str}
      Injected as the first prior-attempt entry when this phase is being re-run after rollback.
    timeout: per-call HTTP timeout (seconds), passed to litellm.acompletion.
    max_retries: transient-error retries (LiteLLM exponential backoff), via num_retries.
    prompt_cache_enabled: when True, attach Anthropic cache_control marker to
      the system prompt. Ignored by non-Anthropic providers.
    skill_name / skill_description / phase_role / project_context / agent_role:
      assembled into the system prompt by `_system_prompt()`. Pass empty strings
      to fall back to the format-contract-only base.
    budget: optional BudgetTracker. When provided, check_pre_llm is called
      before the LLM call (raises BudgetExceeded if refused) and record_llm
      is called after a successful call. budget=None skips all tracking.
    budget_agent: agent name passed to budget.check_pre_llm / record_llm.
      Typically the agent running the skill (e.g. "default"). None = no agent context.
    """
    # Budget pre-check — runs before any LLM call
    if budget is not None:
        from reyn.budget.budget import BudgetExceeded, format_refusal_message
        check = budget.check_pre_llm(model=model, agent=budget_agent)
        if not check.allowed:
            raise BudgetExceeded(
                check.hard_dimension or "budget",
                format_refusal_message(check, agent=budget_agent),
            )

    system = _system_prompt(
        skill_name=skill_name,
        skill_description=skill_description,
        phase_role=phase_role,
        project_context=project_context,
        agent_role=agent_role,
    )
    user_content = json.dumps(frame.model_dump(mode="json"), indent=2, ensure_ascii=False)
    messages: list[dict] = [
        _build_system_message(system, prompt_cache_enabled),
        {"role": "user", "content": user_content},
    ]

    # Build combined injection list: rollback context first, then same-phase retries
    all_injections: list[dict[str, str]] = []
    if rollback_context:
        all_injections.append({
            "raw": json.dumps(rollback_context["rejected_artifact"], ensure_ascii=False),
            "error": (
                f"Your previous output was rolled back by "
                f"[{rollback_context['rollback_from']}]: {rollback_context['reason']}\n"
                "Please revise your output to address the feedback."
            ),
        })
    if prior_attempts:
        all_injections.extend(prior_attempts)

    # Inject semantic-rejection feedback from outer phase retry loop
    if all_injections:
        for pa in all_injections:
            messages.append({"role": "assistant", "content": pa["raw"]})
            messages.append({
                "role": "user",
                "content": (
                    f"Your output was rejected: {pa['error']}\n"
                    "Fix the issue and output a valid JSON response."
                ),
            })

    last_exc: Exception | None = None
    last_raw: str = ""
    attempt0_raw: str = ""

    for attempt in range(2):  # attempt 0 = first call, attempt 1 = JSON-repair retry
        if attempt == 1:
            # Only retry if we actually got a non-empty (but unparseable) response
            if not attempt0_raw:
                break
            messages = messages + [
                {"role": "assistant", "content": attempt0_raw},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON. "
                        "Output ONLY a single valid JSON object — no explanation, no markdown."
                    ),
                },
            ]

        # response_format may not be supported by all models; pass it only when available
        extra = proxy_kwargs()
        # When routing via a local proxy, strip the provider prefix from the model
        # name (e.g. "openai/gemini-2.5-flash-lite" → "gemini-2.5-flash-lite") so
        # the proxy receives the bare model name it registered under.
        effective_model = model.split("/", 1)[1] if extra and "/" in model else model
        common_kwargs = {"timeout": timeout, "num_retries": max_retries}
        try:
            response = await litellm.acompletion(
                model=effective_model,
                messages=messages,
                response_format={"type": "json_object"},
                **common_kwargs,
                **extra,
            )
        except Exception:
            response = await litellm.acompletion(
                model=effective_model, messages=messages, **common_kwargs, **extra,
            )

        usage = _extract_usage(response)
        last_raw = response.choices[0].message.content or ""
        if attempt == 0:
            attempt0_raw = last_raw

        if not last_raw:
            last_exc = ValueError("LLM returned empty response")
            continue

        text = _extract_json(last_raw)

        parsed: dict | None = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass

        if parsed is None:
            try:
                parsed = json.loads(_repair_json(text))
            except json.JSONDecodeError as exc:
                last_exc = exc
                continue  # retry

        result = LLMCallResult(data=parsed, usage=usage)
        # Budget post-record — after successful parse
        if budget is not None and result.usage is not None:
            budget.record_llm(
                model=model,
                agent=budget_agent,
                usage=result.usage,
            )
        return result

    raise ValueError(
        f"LLM returned invalid JSON after repair and retry.\n"
        f"Error: {last_exc}\n"
        f"Raw response (first 800 chars):\n{last_raw[:800]}"
    ) from last_exc


async def call_llm_tools(
    *,
    model: str,
    messages: list[dict],            # OpenAI-format messages (role/content/tool_calls/tool_call_id)
    tools: list[dict],               # OpenAI-format tools array
    tool_choice: str = "auto",       # "auto" | "required" | "none" (note: "none" not Gemini-safe)
    timeout: float | None = None,
    max_retries: int = 1,
    skill_name: str = "router",      # for budget/event tagging
    skill_description: str = "",
    prompt_cache_enabled: bool = True,
    budget: "BudgetTracker | None" = None,
    budget_agent: str | None = None,
) -> LLMToolCallResult:
    """Tool-use variant of call_llm. Returns raw assistant message.

    Forces gemini-safe settings:
      - stream=False (Gemini #21041 streaming+tools bug)
      - thinking disabled (Gemini #17949 multi-turn parallel + thinking bug)

    budget: optional BudgetTracker. When provided, check_pre_llm is called
      before the LLM call (raises BudgetExceeded if refused) and record_llm
      is called after a successful call. budget=None skips all tracking.
    budget_agent: agent name passed to budget.check_pre_llm / record_llm.
    """
    # Budget pre-check — runs before the LLM call
    if budget is not None:
        from reyn.budget.budget import BudgetExceeded, format_refusal_message
        check = budget.check_pre_llm(model=model, agent=budget_agent)
        if not check.allowed:
            raise BudgetExceeded(
                check.hard_dimension or "budget",
                format_refusal_message(check, agent=budget_agent),
            )

    extra = proxy_kwargs()
    # Strip provider prefix when routing via local proxy (same logic as call_llm)
    effective_model = model.split("/", 1)[1] if extra and "/" in model else model

    call_kwargs: dict = {
        "model": effective_model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        # Gemini-safe forced settings:
        "stream": False,             # Gemini #21041: streaming + tools bug
        # No response_format: incompatible with tools= on most providers
        # No thinking kwargs: disabled by default on all providers
        **extra,
    }
    if timeout is not None:
        call_kwargs["timeout"] = timeout
    call_kwargs["num_retries"] = max_retries

    response = await litellm.acompletion(**call_kwargs)

    msg = response.choices[0].message
    usage = _extract_usage(response) or TokenUsage()

    # Budget post-record — after successful LLM call
    if budget is not None:
        budget.record_llm(
            model=model,
            agent=budget_agent,
            usage=usage,
        )

    # Normalize tool_calls to plain dicts so callers don't depend on litellm internals
    tool_calls = [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,  # already JSON string
            },
        }
        for tc in (msg.tool_calls or [])
    ]

    finish_reason = None
    try:
        finish_reason = response.choices[0].finish_reason
    except Exception:
        pass

    return LLMToolCallResult(
        content=msg.content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        raw_message=msg,
    )
