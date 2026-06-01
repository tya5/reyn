import asyncio
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Coroutine, TypeVar, Union

import httpx

logger = logging.getLogger(__name__)
from reyn.llm.json_parse import loads_lenient
from reyn.llm.model_resolver import ModelSpec

# FP-0008 #1135 sibling: inline cap for the raw output captured on a pre-parse
# JSON-decode failure (opt A — llm layer, no offload).
_JSON_DECODE_RAW_CAP = 8192


def _truncate_json_for_event(raw: str, pos: "int | None", cap: int = _JSON_DECODE_RAW_CAP) -> str:
    """Bound the raw LLM output for the json-decode-failure event (#1135 sibling).

    Returns *raw* unchanged when ≤ ``cap``. Otherwise returns a ``cap``-byte
    window centered on the JSONDecodeError position (where the malformation is),
    or the head when ``pos`` is unknown, with ``…[N bytes before/after]`` markers.
    Inline-only: the malformation in a decode failure is diagnosable from the
    window, so no offload is needed at the llm layer.
    """
    if len(raw) <= cap:
        return raw
    if pos is None:
        return raw[:cap] + f"\n…[truncated {len(raw) - cap} bytes]"
    half = cap // 2
    start = max(0, min(pos - half, len(raw) - cap))
    end = start + cap
    head = f"…[{start} bytes before]\n" if start > 0 else ""
    tail = f"\n…[{len(raw) - end} bytes after]" if end < len(raw) else ""
    return head + raw[start:end] + tail
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import ContextFrame

if TYPE_CHECKING:
    from reyn.budget.budget import BudgetTracker
    from reyn.events.events import EventLog

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Payload trace dump (opt-in via REYN_LLM_TRACE_DUMP env var)
# ---------------------------------------------------------------------------


def _get_trace_dump_path() -> str | None:
    """Read trace dump path from env var at call time (allows runtime toggling).

    Evaluated on every call so that the env var can be set or cleared while
    the process is running (e.g. toggling debug tracing without restart).
    Returns None when the env var is absent or empty — completely no-op.
    """
    return os.environ.get("REYN_LLM_TRACE_DUMP") or None


# ---------------------------------------------------------------------------
# Size limit + rotation
# ---------------------------------------------------------------------------

def _get_trace_dump_max_size() -> int:
    """Read max dump file size from env var (bytes). Default: 100 MB.

    Reads REYN_LLM_TRACE_DUMP_MAX_SIZE at call time so the limit can be
    changed without restart. Falls back to 100 MB on missing or invalid value.
    """
    val = os.environ.get("REYN_LLM_TRACE_DUMP_MAX_SIZE")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return 100 * 1024 * 1024  # 100 MB


def _maybe_rotate_dump(path: str) -> None:
    """Rotate the dump file if it exceeds the configured size limit.

    Rotation keeps exactly one generation: ``<path>`` becomes ``<path>.1``.
    Any pre-existing ``<path>.1`` is replaced (single-generation policy).
    A message is printed to stderr so rotation is never silent.
    OSError (disk full, permission denied, etc.) causes silent fall-through
    so the main dump path continues regardless.
    """
    try:
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        limit = _get_trace_dump_max_size()
        if size <= limit:
            return
        rotated = path + ".1"
        if os.path.exists(rotated):
            os.remove(rotated)
        os.rename(path, rotated)
        print(
            f"[reyn] LLM trace dump rotated: {path} -> {rotated} "
            f"(size {size:,} > limit {limit:,})",
            file=sys.stderr,
        )
    except OSError:
        pass  # rotation failure is non-fatal; dump continues


# ---------------------------------------------------------------------------
# Secrets redaction
# ---------------------------------------------------------------------------

_DEFAULT_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "openai-key"),
    (re.compile(r"xoxb-[A-Za-z0-9-]{20,}"), "slack-token"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), "bearer-token"),
    (
        re.compile(r"-----BEGIN [A-Z ]+ KEY-----[\s\S]*?-----END [A-Z ]+ KEY-----"),
        "private-key",
    ),
]


def _get_extra_redact_patterns() -> list[tuple[re.Pattern, str]]:
    """Read extra redaction patterns from REYN_LLM_TRACE_REDACT_PATTERNS.

    Value is a comma-separated list of regex strings. Invalid patterns are
    silently skipped so a typo never blocks the dump path.
    """
    val = os.environ.get("REYN_LLM_TRACE_REDACT_PATTERNS")
    if not val:
        return []
    out: list[tuple[re.Pattern, str]] = []
    for i, raw in enumerate(val.split(",")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append((re.compile(raw), f"custom-{i}"))
        except re.error:
            continue
    return out


def _redact_secrets(payload: dict) -> dict:
    """Mask known sensitive patterns inside a payload dict (recursive).

    Default ON — disabled only when REYN_LLM_TRACE_REDACT=off.
    Walks all strings inside dicts and lists; non-string values are untouched.
    False positives (long strings matching a pattern) are possible; see docs.
    """
    if os.environ.get("REYN_LLM_TRACE_REDACT") == "off":
        return payload

    patterns = _DEFAULT_REDACT_PATTERNS + _get_extra_redact_patterns()

    def _mask(s: str) -> str:
        for pat, name in patterns:
            s = pat.sub(f"[REDACTED:{name}]", s)
        return s

    def _walk(obj: object) -> object:
        if isinstance(obj, str):
            return _mask(obj)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        return obj

    return _walk(payload)  # type: ignore[return-value]


def _dump_llm_request(payload: dict) -> str | None:
    """If REYN_LLM_TRACE_DUMP is set, append a request record to that file.

    Returns request_id (str) so the response can be paired, or None when
    tracing is disabled (env var not set). Completely no-op when disabled.

    Production hardening applied before write:
    - Rotates the file when it exceeds REYN_LLM_TRACE_DUMP_MAX_SIZE (default 100 MB).
    - Redacts known sensitive patterns via _redact_secrets (default ON).
    """
    path = _get_trace_dump_path()
    if not path:
        return None
    _maybe_rotate_dump(path)
    request_id = str(uuid.uuid4())
    record: dict = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **payload,
    }
    record = _redact_secrets(record)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # never crash the main path
        logger.warning("llm trace dump write failed: %s", exc)
        return None
    return request_id


def _extract_provider_response_fields(response) -> dict:
    """Extract provider-side response fields the OS doesn't otherwise surface.

    The narrow ``content / tool_calls / finish_reason / usage`` payload
    written to the trace is enough for happy-path debugging, but it
    discards the fields needed to diagnose **empty-stop** cases:

      * ``vertex_ai_safety_results`` — was the response filtered for safety?
      * ``provider_specific_fields.refusal`` — did the model refuse?
      * ``completion_tokens_details`` — reasoning vs. text token split,
        relevant for thinking-mode models.
      * ``system_fingerprint`` — provider build identity, useful when an
        attractor only fires on a specific provider revision.

    Without these, an operator looking at a trace dump can't tell the
    difference between "model literally output nothing" and "provider
    blocked the response with a safety filter". Origin: dogfood v7
    diagnosis of Q4 empty-stop required `litellm.acompletion(...).model_dump()`
    to confirm `safety_results=[]` and `refusal=null` — the existing
    trace alone was insufficient.

    Returns a dict of useful provider fields (= empty when the response
    object doesn't expose them, which is fine — providers vary).
    Best-effort: never raises, drops fields it can't read.
    """
    out: dict = {}
    try:
        choice = response.choices[0]
    except Exception:
        return out

    # Provider-specific message-level fields (Vertex AI / Anthropic / etc.).
    msg = getattr(choice, "message", None)
    if msg is not None:
        psf = getattr(msg, "provider_specific_fields", None)
        if isinstance(psf, dict) and psf:
            out["provider_specific_fields"] = psf

    # Vertex AI / Gemini specific top-level fields.
    for attr in (
        "vertex_ai_safety_results",
        "vertex_ai_grounding_metadata",
        "vertex_ai_citation_metadata",
        "vertex_ai_url_context_metadata",
    ):
        val = getattr(response, attr, None)
        # Skip empty lists / None — they're noise.
        if val:
            out[attr] = val

    # OpenAI-specific fields.
    sf = getattr(response, "system_fingerprint", None)
    if sf:
        out["system_fingerprint"] = sf
    st = getattr(response, "service_tier", None)
    if st:
        out["service_tier"] = st

    # Reasoning / completion token details (= present on thinking-mode
    # models like o1, claude-3.7-sonnet thinking).
    usage_obj = getattr(response, "usage", None)
    if usage_obj is not None:
        ctd = getattr(usage_obj, "completion_tokens_details", None)
        if ctd is not None:
            try:
                out["completion_tokens_details"] = (
                    ctd.model_dump() if hasattr(ctd, "model_dump") else dict(ctd)
                )
            except Exception:
                pass

    return out


def _dump_llm_response(request_id: str | None, payload: dict) -> None:
    """If REYN_LLM_TRACE_DUMP is set and request_id is non-None, append response record.

    Production hardening applied before write:
    - Rotates the file when it exceeds REYN_LLM_TRACE_DUMP_MAX_SIZE (default 100 MB).
    - Redacts known sensitive patterns via _redact_secrets (default ON).
    """
    path = _get_trace_dump_path()
    if not path or not request_id:
        return
    _maybe_rotate_dump(path)
    record: dict = {
        "kind": "response",
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **payload,
    }
    record = _redact_secrets(record)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("llm trace dump write failed: %s", exc)


_G12_SIGNAL_TEXT = "(answered) — task complete; reply to user or chain another tool"


def _g12_signal_enabled() -> bool:
    """Return True unless `REYN_G12_SIGNAL` env var explicitly disables it.

    Recognised disable values (case-insensitive): "off", "0", "false", "no".
    Any other value (or unset) leaves the workaround active.
    """
    val = os.environ.get("REYN_G12_SIGNAL", "").strip().lower()
    return val not in {"off", "0", "false", "no"}


def _apply_g12_signal(messages: list[dict]) -> list[dict]:
    """Embed the G12 "(answered)" signal inside the trailing role=tool message.

    Replaces the prior shape (= append `{"role": "user", "content": "(answered)"}`)
    which violated the OpenAI / Anthropic role contract. See the docstring
    in `call_llm_tools` (around the call site) for the full motivation +
    measurement data.

    Behaviour:
      - **`REYN_G12_SIGNAL=off`** env var disables the workaround entirely
        (= returns messages unchanged). Operator opt-out for diagnostic
        or A/B comparison purposes.
      - No-op when `messages` is empty or messages[-1] is not role=tool.
      - JSON object tool content (= `{...}`-shaped string): inject a
        top-level `_g12_signal` field after the opening brace, with
        trailing-comma elision for empty-object shapes (= `"{}"`,
        `"{ }"`) so the output is always parse-valid.
      - Plain-text or non-JSON tool content: prefix with the signal text
        + a blank line for visual separation.
      - Non-string content (= list of content parts or None): leave
        untouched (= no safe place to embed the signal without a deeper
        API contract decision).

    The returned list is either the same `messages` reference (no-op case)
    or a new list with only the trailing message replaced.
    """
    if not _g12_signal_enabled():
        return messages
    if not messages:
        return messages
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return messages
    content = last.get("content")
    if not isinstance(content, str):
        return messages
    new_last = dict(last)
    if content.startswith("{"):
        inner = content[1:]
        # Empty-object shapes ("{}", "{ }", "{\n}") must not get a
        # separator comma, otherwise the output would have a trailing
        # `, }` which fails JSON parse.
        if inner.lstrip().startswith("}"):
            new_last["content"] = f'{{"_g12_signal": "{_G12_SIGNAL_TEXT}"{inner}'
        else:
            new_last["content"] = (
                f'{{"_g12_signal": "{_G12_SIGNAL_TEXT}", {inner}'
            )
    else:
        new_last["content"] = f"{_G12_SIGNAL_TEXT}\n\n{content}"
    return messages[:-1] + [new_last]


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

# ---------------------------------------------------------------------------
# Infrastructure retry — exponential backoff on transient LLM API errors
# ---------------------------------------------------------------------------

# Retryable: infrastructure / transient errors where the same call may succeed.
# Non-retryable: semantic / auth / quota errors (4xx) where retry won't help.
# Resolved lazily so importing this module does not trigger `import litellm`.
_RETRYABLE_LITELLM_EXCEPTIONS: tuple | None = None

_LLM_RETRY_MAX_ATTEMPTS: int = 3   # total attempts = 1 initial + 2 retries
_LLM_RETRY_BASE_S: float = 2.0     # first backoff: 2s → 4s → 8s (capped at 16s)
_LLM_RETRY_MAX_BACKOFF_S: float = 16.0


def _get_retryable_litellm_exceptions() -> tuple:
    """Return the tuple of retryable litellm exceptions, loading litellm lazily.

    Cached in _RETRYABLE_LITELLM_EXCEPTIONS after first call.
    """
    global _RETRYABLE_LITELLM_EXCEPTIONS
    if _RETRYABLE_LITELLM_EXCEPTIONS is None:
        import litellm
        _RETRYABLE_LITELLM_EXCEPTIONS = (
            litellm.exceptions.Timeout,           # request timed out
            litellm.exceptions.APIConnectionError, # network-level connection failure
            litellm.exceptions.ServiceUnavailableError,  # 503
            litellm.exceptions.BadGatewayError,    # 502
            litellm.exceptions.InternalServerError, # 500
        )
    return _RETRYABLE_LITELLM_EXCEPTIONS


def _is_retryable_exc(exc: BaseException) -> bool:
    """Return True for infrastructure errors that justify a retry attempt.

    Catches litellm's typed exceptions for 5xx / timeout / connection failures.
    Also catches httpx transport-level errors that LiteLLM may not wrap when
    the request fails before reaching the provider's HTTP response logic.
    """
    if isinstance(exc, _get_retryable_litellm_exceptions()):
        return True
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout)):
        return True
    return False


def _backoff_s(attempt: int) -> float:
    """Exponential backoff: 2s, 4s, 8s, … capped at _LLM_RETRY_MAX_BACKOFF_S.

    ``attempt`` is 0-indexed (= attempt 0 is the first retry, after the initial
    call fails).  Min 0.0 — never negative if someone passes a negative index.
    """
    return min(_LLM_RETRY_BASE_S * (2 ** attempt), _LLM_RETRY_MAX_BACKOFF_S)


async def _llm_call_with_retry(
    coro_fn,
    model: str,
    event_log: "EventLog | None",
) -> object:
    """Execute ``coro_fn()`` with infrastructure-error retry + backoff.

    ``coro_fn`` must be a zero-arg async callable that returns the litellm
    response object.  It is called once per attempt.

    Emits ``llm_call_retry`` on each retry and ``llm_call_retry_exhausted``
    when all attempts are exhausted.  When ``event_log`` is None, observability
    events are silently skipped (= callers without an EventLog context).

    Raises the last exception when all retries are exhausted.
    """
    last_exc: BaseException | None = None
    for attempt in range(_LLM_RETRY_MAX_ATTEMPTS):
        try:
            return await coro_fn()
        except BaseException as exc:
            if not _is_retryable_exc(exc):
                raise
            last_exc = exc
            retries_remaining = _LLM_RETRY_MAX_ATTEMPTS - attempt - 1
            if retries_remaining == 0:
                if event_log is not None:
                    try:
                        event_log.emit(
                            "llm_call_retry_exhausted",
                            model=model,
                            attempt_n=attempt + 1,
                            error_kind=type(exc).__name__,
                        )
                    except Exception:
                        pass
                raise
            backoff = _backoff_s(attempt)
            if event_log is not None:
                try:
                    event_log.emit(
                        "llm_call_retry",
                        model=model,
                        attempt_n=attempt + 1,
                        error_kind=type(exc).__name__,
                        backoff_s=backoff,
                    )
                except Exception:
                    pass
            await asyncio.sleep(backoff)
    # Should be unreachable — loop always raises or returns.
    assert last_exc is not None
    raise last_exc  # pragma: no cover


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


# #1190 cost-observability: the valid purpose (cost-attribution) buckets. Every
# recorded_acompletion call must tag one so /cost can break spend down by where
# the LLM call originated. ``dogfood`` covers test/trace sites (recorder=None).
LLM_PURPOSES: tuple[str, ...] = (
    "main", "phase", "compaction", "judge", "skill_node_adapt", "dogfood",
)


async def recorded_acompletion(
    *,
    model: str,
    messages: list,
    purpose: str,
    recorder: object | None = None,
    agent: str | None = None,
    response_format: dict | None = None,
    fallback_without_response_format: bool = False,
    extra_kwargs: dict | None = None,
) -> object:
    """Single cost-observability chokepoint for ALL ``litellm.acompletion`` calls (#1190).

    Absorbs proxy routing + provider-prefix strip, performs the call (with an
    optional ``response_format`` retry-without fallback), extracts usage, and
    records it via ``recorder.record_llm(purpose=...)`` **by construction** when
    a recorder is given. Returns the RAW litellm response — callers keep their
    own response-shape handling (``.content`` / json parse / tool extraction)
    above. ``purpose`` is the required cost-attribution bucket (see
    ``LLM_PURPOSES``).

    Stage (iii)'s AST guard (tya5/reyn#1190) enforces that ``litellm.acompletion``
    is called ONLY inside this function, so no LLM call can bypass recording.
    Replay-safe: the call still bottoms out at ``litellm.acompletion``, which
    ``LLMReplay`` monkeypatches.
    """
    import litellm

    # #1190 stage (iii): typo guard — a purpose outside the known set would
    # silently land spend in an unattributed bucket in /cost.
    if purpose not in LLM_PURPOSES:
        raise ValueError(
            f"recorded_acompletion: unknown purpose {purpose!r}; "
            f"must be one of {LLM_PURPOSES}"
        )

    extra = proxy_kwargs()
    effective_model = model.split("/", 1)[1] if extra and "/" in model else model
    base_kwargs = dict(extra_kwargs or {})
    base_kwargs.update(extra)  # Reyn proxy kwargs win over caller-supplied ones

    async def _once(rf: dict | None) -> object:
        call_kwargs = dict(base_kwargs)
        if rf is not None:
            call_kwargs["response_format"] = rf
        return await litellm.acompletion(model=effective_model, messages=messages, **call_kwargs)

    # #1212 D5: per-(model, call-shape) capability cache. If this model+shape is
    # already known to reject response_format, skip the doomed attempt and go
    # straight to the no-response_format call (only on the fallback-enabled path,
    # so non-fallback callers keep their exact raise-on-error semantics). Pure
    # optimization — the fallback below still handles a first/uncached 400.
    from reyn.llm.capability_cache import (
        record_response_format_support,
        response_format_supported,
    )

    has_tools = bool(base_kwargs.get("tools"))
    rf = response_format
    if (
        response_format is not None
        and fallback_without_response_format
        and response_format_supported(effective_model, has_tools=has_tools) is False
    ):
        rf = None

    try:
        response = await _once(rf)
        if rf is not None and fallback_without_response_format:
            record_response_format_support(effective_model, has_tools=has_tools, supported=True)
    except Exception:
        if rf is not None and fallback_without_response_format:
            record_response_format_support(effective_model, has_tools=has_tools, supported=False)
            response = await _once(None)
        else:
            raise

    if recorder is not None:
        usage = _extract_usage(response)
        if usage is not None:
            recorder.record_llm(
                model=effective_model, agent=agent, usage=usage, purpose=purpose,
            )
    return response


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
    model: "Union[str, ModelSpec]",
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
    purpose: str = "main",  # #1190 cost-attribution bucket (see LLM_PURPOSES)
    trace_caller: str | None = None,
    event_log: "EventLog | None" = None,
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
    # Normalize model to ModelSpec — accept both str (backward compat) and ModelSpec.
    spec: ModelSpec = model if isinstance(model, ModelSpec) else ModelSpec(model=model, kwargs={})

    # Budget pre-check — runs before any LLM call
    if budget is not None:
        from reyn.budget.budget import BudgetExceeded, format_refusal_message
        check = budget.check_pre_llm(model=spec.model, agent=budget_agent)
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
    _trace_rid: str | None = None  # request_id for paired response dump

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
        effective_model = spec.model.split("/", 1)[1] if extra and "/" in spec.model else spec.model
        common_kwargs = {"timeout": timeout, "num_retries": max_retries}
        # Merge operator-declared kwargs (spec.kwargs) with Reyn defaults.
        # Reyn-set options (common_kwargs, extra) take precedence over spec.kwargs
        # so proxy routing and retry settings are never overridden by operator config.
        spec_kwargs = dict(spec.kwargs)

        # Payload trace dump — dump once per attempt (only attempt 0 creates a
        # new request_id; attempt 1 re-uses the same id so the pair is linked).
        if attempt == 0:
            _trace_rid = _dump_llm_request({
                "model": effective_model,
                "caller_hint": trace_caller or "unknown",
                "messages": messages,
                "tools": None,
                "tool_choice": None,
                "sampling_params": {"timeout": timeout, "max_retries": max_retries},
                "spec_kwargs": spec_kwargs,
            })

        async def _do_call() -> object:
            # #1190: route through the single cost-observability chokepoint
            # (proxy + prefix-strip + json_object fallback live there now).
            # recorder=None here — call_llm keeps its own retry-aware
            # record-once below (the chokepoint records the bypass sites that
            # have no existing record). ``extra`` is re-derived inside the
            # chokepoint, so only spec/common kwargs are passed through.
            return await recorded_acompletion(
                model=effective_model,
                messages=messages,
                purpose=purpose,
                recorder=None,
                response_format={"type": "json_object"},
                fallback_without_response_format=True,
                extra_kwargs={**spec_kwargs, **common_kwargs},
            )

        response = await _llm_call_with_retry(_do_call, effective_model, event_log)

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
            parsed = loads_lenient(
                text,
                on_raw_decode=lambda discarded_len, head: logger.warning(
                    "llm_json_raw_decode_recovered: discarded %d bytes of trailing "
                    "garbage after valid JSON object. head=%r",
                    discarded_len,
                    head,
                ),
            )
        except json.JSONDecodeError as exc:
            last_exc = exc
            continue  # retry

        # Dump response before returning
        finish_reason: str | None = None
        try:
            finish_reason = response.choices[0].finish_reason
        except Exception:
            pass
        _dump_llm_response(_trace_rid, {
            "content": last_raw,
            "tool_calls": [],
            "finish_reason": finish_reason,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
            **_extract_provider_response_fields(response),
        })

        result = LLMCallResult(data=parsed, usage=usage)
        # Budget post-record — after successful parse.
        # Use effective_model (proxy prefix stripped) so estimate_cost inside
        # record_llm resolves against the bare litellm model_cost key (F4 Bug 1).
        if budget is not None and result.usage is not None:
            budget.record_llm(
                model=effective_model,
                agent=budget_agent,
                usage=result.usage,
                purpose=purpose,
            )
        return result

    # FP-0008 #1135 sibling (opt A, canonical contract in #1135): capture the
    # raw LLM output into the always-on P6 events log on a pre-parse JSON-decode
    # failure — otherwise it survives only in the opt-in REYN_LLM_TRACE_DUMP, so
    # the failure (e.g. a malformed `\escape`) is undiagnosable from events. The
    # read-side is dogfood_trace --mode llm-emitted-ops. Inline-cap only: the llm
    # layer has no state_dir to offload to, and a decode malformation is
    # diagnosable from a window around the error position.
    if event_log is not None:
        _pos = getattr(last_exc, "pos", None)
        event_log.emit(
            "llm_output_json_decode_failed",
            failure_kind="json_decode",
            error=str(last_exc),
            raw_output=_truncate_json_for_event(last_raw, _pos),
        )
    raise ValueError(
        f"LLM returned invalid JSON after repair and retry.\n"
        f"Error: {last_exc}\n"
        f"Raw response (first 800 chars):\n{last_raw[:800]}"
    ) from last_exc


async def call_llm_tools(
    *,
    model: "Union[str, ModelSpec]",
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
    purpose: str = "main",  # #1190 cost-attribution bucket (chat router = main)
    trace_caller: str | None = None,
    event_log: "EventLog | None" = None,
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
    # Normalize model to ModelSpec — accept both str (backward compat) and ModelSpec.
    spec: ModelSpec = model if isinstance(model, ModelSpec) else ModelSpec(model=model, kwargs={})

    # Budget pre-check — runs before the LLM call
    if budget is not None:
        from reyn.budget.budget import BudgetExceeded, format_refusal_message
        check = budget.check_pre_llm(model=spec.model, agent=budget_agent)
        if not check.allowed:
            raise BudgetExceeded(
                check.hard_dimension or "budget",
                format_refusal_message(check, agent=budget_agent),
            )

    extra = proxy_kwargs()
    # Strip provider prefix when routing via local proxy (same logic as call_llm)
    effective_model = spec.model.split("/", 1)[1] if extra and "/" in spec.model else spec.model
    # Operator-declared kwargs from ModelSpec; Gemini-safe forced settings override these.
    spec_kwargs = dict(spec.kwargs)

    # ── G12 post-tool empty-stop attractor workaround (V1-INNER) ────────────
    #
    # WORKAROUND (not a real fix): when the last message is role=tool,
    # gemini-2.5-flash-lite (and likely other weak LLMs in the OpenAI
    # tool_use compat path) hits an empty-stop attractor at high rate
    # (30-100% in 2026-05-07 N=10 measurement, deterministic-leaning).
    # The model emits 0 completion tokens with finish_reason=stop, so the
    # user sees nothing after a successful tool call.
    #
    # ── V1-INNER (2026-05-18, issue #156 fix) ───────────────────────────────
    # Earlier shape: inject ``{"role": "user", "content": "(answered)"}``
    # as a trailing message. That violated the OpenAI / Anthropic role
    # contract — `role=user` content is, by spec, "what the human typed".
    # The OS was masquerading an orchestration signal as user input.
    #
    # Weak `gemini-2.5-flash-lite` correctly followed the contract: it
    # treated the literal `(answered)` as a user paste and produced
    # canned-reply replies ("It looks like you've pasted '(answered)'
    # again, which might be a leftover from a previous interaction or a
    # mistake.") at 100% rate in polluted-history post-tool turns
    # (issue #156, 10/10 reproduction on the tui-coder baseline trace).
    # The reply persisted to `history.jsonl`, polluting future turns and
    # producing a snowball where short user prompts (`?`, `f`) kept
    # reproducing the canned-reply via Mechanism B (history hallucination).
    #
    # Fix: embed the neutral signal INSIDE the role=tool message content
    # (= contract-correct location for signals about tool results) instead
    # of appending a fake user message. The signal lives as a top-level
    # `_g12_signal` field in the JSON-shaped tool result (= 100% of
    # current tool dispatch paths produce JSON-shaped tool content), or
    # as a `(answered) ` prefix on non-JSON content (defensive fallback).
    #
    # Empirical (2026-05-18, issue #156 measurement N=10 against the
    # tui-coder reproducing baseline = post-tool turn + 5-msg polluted
    # history + `summarize readme.md` prompt):
    #
    #   V7 (old shape, role=user "(answered)"):  canned 10/10, text 0/10
    #   V0 (no injection at all):                canned 0/10,  text 9/10, tool_call 1/10
    #   V1-INNER (this implementation):          canned 0/10,  text 10/10
    #   V2A (role=assistant empty content):      canned 0/10,  text 9/10, tool_call 1/10
    #
    # V1-INNER is selected because: (a) it preserves the documented signal
    # mechanism (= a downstream context whose empty_stop rate has not been
    # re-measured may still benefit from "(answered)"), (b) it's
    # contract-correct (signals about tool results live in role=tool), and
    # (c) it yields the highest reply stability (= 10/10 text vs 9/10 for
    # V0 / V2A; the LLM reliably summarises rather than choosing to chain
    # another tool).
    #
    # Caveats (carried from original workaround):
    #   - Workaround only — true fix is provider-side or different model.
    #   - 2026-05-07 V0 baseline measurement "30-60% empty_stop" appears
    #     validity-degraded in the post-FP-0034 SP/tools shape (0/10
    #     empty_stop measured 2026-05-18). The workaround's protective
    #     effect in current contexts is unverified; V1-INNER preserves
    #     the signal so contexts that still benefit are unaffected.
    #   - This modification is NOT persisted to history; it's applied at
    #     the LLM call boundary so chat history stays clean for downstream
    #     logic (= same property as the prior shape).
    #   - **Operator opt-out**: set `REYN_G12_SIGNAL=off` (case-insensitive;
    #     `0` / `false` / `no` also accepted) to disable the workaround
    #     entirely for diagnostic or A/B-comparison runs.
    messages = _apply_g12_signal(messages)

    call_kwargs: dict = {
        "model": effective_model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        # spec.kwargs passthrough (operator-declared, e.g. temperature)
        **spec_kwargs,
        # Gemini-safe forced settings override spec_kwargs:
        "stream": False,             # Gemini #21041: streaming + tools bug
        # No response_format: incompatible with tools= on most providers
        # No thinking kwargs: disabled by default on all providers
        **extra,
    }
    if timeout is not None:
        call_kwargs["timeout"] = timeout
    call_kwargs["num_retries"] = max_retries

    # Payload trace dump (request)
    _trace_rid = _dump_llm_request({
        "model": effective_model,
        "caller_hint": trace_caller or "unknown",
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "sampling_params": {
            "timeout": timeout,
            "max_retries": max_retries,
        },
    })

    async def _tools_call() -> object:
        # #1190: route through the single cost-observability chokepoint.
        # recorder=None — call_llm_tools keeps its own record below; the
        # chokepoint re-derives proxy kwargs (idempotent) so only the
        # pre-built tools/tool_choice/response_format kwargs flow as extras.
        _kw = dict(call_kwargs)
        _model = _kw.pop("model")
        _messages = _kw.pop("messages")
        return await recorded_acompletion(
            model=_model, messages=_messages, purpose=purpose,
            recorder=None, extra_kwargs=_kw,
        )

    response = await _llm_call_with_retry(_tools_call, effective_model, event_log)

    msg = response.choices[0].message
    usage = _extract_usage(response) or TokenUsage()

    # Budget post-record — after successful LLM call.
    # Use effective_model (proxy prefix stripped) so estimate_cost inside
    # record_llm resolves against the bare litellm model_cost key (F4 Bug 1).
    if budget is not None:
        budget.record_llm(
            model=effective_model,
            agent=budget_agent,
            usage=usage,
            purpose=purpose,
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
    except Exception as exc:
        logger.warning("finish_reason unavailable — budget tracking may be affected: %s", exc)

    # Payload trace dump (response). Includes provider-specific fields
    # (= safety_results, refusal, system_fingerprint, …) so empty-stop
    # diagnosis doesn't have to re-call the LLM via llm_replay.py to see
    # whether the response was content-empty vs. safety-blocked.
    _dump_llm_response(_trace_rid, {
        "content": msg.content,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        },
        **_extract_provider_response_fields(response),
    })

    return LLMToolCallResult(
        content=msg.content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        raw_message=msg,
    )
