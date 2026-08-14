import asyncio
import contextvars
import json
import logging
import os
import re
import sys
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Callable, Coroutine, NamedTuple, TypeVar, Union

logger = logging.getLogger(__name__)
from reyn.core.turn_scope import get_active_turn_chain_id
from reyn.llm.litellm_bootstrap import LitellmUnavailableError, ensure_litellm_ready
from reyn.llm.model_resolver import ModelSpec
from reyn.llm.pricing import TokenUsage, UsageSource, estimate_cost, parse_usage_source
from reyn.prompt.loop_control import G12_SIGNAL_ERROR_TEXT as _G12_SIGNAL_ERROR_TEXT
from reyn.prompt.loop_control import G12_SIGNAL_TEXT as _G12_SIGNAL_TEXT

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.runtime.budget.budget import BudgetTracker

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
            # Mask string KEYS too — a secret used as a dict key leaks just as
            # much as one in a value. The result is a redacted copy destined for
            # json.dumps, so changing keys is safe (no caller re-reads it by key).
            return {
                (_mask(k) if isinstance(k, str) else k): _walk(v)
                for k, v in obj.items()
            }
        # tuple / set are walked too (not just list) — a secret inside one would
        # otherwise pass through untouched. The redacted copy is json-serialized
        # by every caller, where tuples/sets already become arrays, so emitting a
        # list here changes nothing downstream (and a set, which json can't even
        # serialize, now survives as a redacted list).
        if isinstance(obj, (list, tuple, set)):
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


# Uniform in-stream continuation signal for all successful tool results.  The
# prior text asserted "task complete" unconditionally — that is overstate for
# many op kinds (write/edit: change applied but unverified; any op: the task may
# have further steps).  "resume" is the same token used by the chat empty-stop
# recovery path (EMPTY_STOP_RETRY_DIRECTIVE) — a pure continuation nudge with
# no state assertion or instruction, matching the "uniform resume" philosophy.
#
# #1439 Fix #2: the trailing-tool result was an ERROR. The success text above
# asserts "task complete" unconditionally, so an errored exec carried "task
# complete" → the agent narrated error-as-success (14096). The error cell drops
# "complete" and signals the failure + a continuation nudge (decision-enabling).
# Only the error cell changes; the success text change (above) is orthogonal.
#
# reyn.prompt.loop_control (SP prompt-package, Phase 3 §J) — both texts
# imported above, re-bound to the original private names so this module is
# otherwise unchanged.

# Tool-result status values (JSON `status` field) that mean the call failed.
# Sourced from the op_runtime envelopes (error / denied / not_found) + a generic
# "failed". Anything else (ok / absent / non-JSON / unparseable) is the success
# cell = byte-identical signal.
_G12_ERROR_STATUSES = frozenset({"error", "denied", "not_found", "failed"})


def _is_g12_error_status(status: object) -> bool:
    """True iff ``status`` is an explicit error value (str in _G12_ERROR_STATUSES)."""
    return isinstance(status, str) and status.lower() in _G12_ERROR_STATUSES


def _trailing_tool_is_error(content: str) -> bool:
    """#1439 Fix #2: True iff a JSON tool result carries an explicit error status
    — at the **dispatch level (top-level) OR the op level (nested under data)**.

    The production envelope nests the op status: ``dispatch_tool`` wraps every
    successful dispatch as ``{"status": "ok", "data": <op_result>}`` (dispatcher.py
    :84-97, confirmed router_loop.py:1913), so an op-execution error (file/grep
    failure, exec returncode≠0 — incl. the 14096 case) lives at ``data.status``
    while the top-level stays ``ok``. Only a *dispatch*-level failure (tool-not-
    found / perm-deny / dispatch exception) sets the top-level ``status`` to error.
    We check both: top-level first, then one level into ``data``.

    Conservative by design: only valid JSON with an explicit error status (at
    either level) counts. A non-JSON body, an unparseable ``{``-prefixed string
    (the existing string-surgery path handles those), a missing status, or
    ``ok`` at both levels → False → the success cell (byte-identical signal). This
    keeps the error path narrow so the replay-gate risk is bounded to genuinely-
    errored trailing tools.
    """
    if not content.startswith("{"):
        return False
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    # Dispatch-level error (top-level status) — rare.
    if _is_g12_error_status(parsed.get("status")):
        return True
    # Op-execution error nested under the dispatch wrapper's ``data`` — the
    # common case (and 14096). Recurse exactly one level.
    data = parsed.get("data")
    if isinstance(data, dict) and _is_g12_error_status(data.get("status")):
        return True
    return False


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
      - Canonical frontmatter+text tool content (= `---\\n<yaml>---\\n<text>`,
        the #2425 案B dominant shape): inject the signal as a LABELED
        `_g12_signal` frontmatter field right after the opening delimiter —
        mirroring the JSON `_g12_signal` field. This is the #2689 fix: the
        prior code hit the plain-text branch below and glued a bare
        `resume\\n\\n` prefix onto the actual tool result, which the model
        read as corrupted tool output (a foreign, unlabeled token on the
        front of the op result). A labeled frontmatter field is structured
        metadata the model can attribute correctly, same as the JSON path.
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
    # Two-cell signal: error → error cell; all other results → success cell.
    # The embed STRUCTURE is unchanged — only the injected text differs — so
    # all structural branches below are preserved.
    signal = _G12_SIGNAL_ERROR_TEXT if _trailing_tool_is_error(content) else _G12_SIGNAL_TEXT
    new_last = dict(last)
    if content.startswith("{"):
        inner = content[1:]
        # Empty-object shapes ("{}", "{ }", "{\n}") must not get a
        # separator comma, otherwise the output would have a trailing
        # `, }` which fails JSON parse.
        if inner.lstrip().startswith("}"):
            new_last["content"] = f'{{"_g12_signal": "{signal}"{inner}'
        else:
            new_last["content"] = (
                f'{{"_g12_signal": "{signal}", {inner}'
            )
    elif content.startswith("---\n") and "\n---\n" in content:
        # #2689: #2425 案B canonical tool-result format `---\n<yaml>---\n<text>`.
        # Embed the signal as a LABELED frontmatter field (mirroring the JSON
        # `_g12_signal` field) instead of the bare `resume\n\n` prefix the plain-
        # text branch below would produce — that prefix read to the model as a
        # foreign token glued to the front of the op result (corrupted tool
        # output). Inject after the opening `---\n` delimiter (content[4:] drops
        # it); the block stays valid YAML frontmatter with `_g12_signal` first.
        new_last["content"] = f"---\n_g12_signal: {signal}\n{content[4:]}"
    else:
        new_last["content"] = f"{signal}\n\n{content}"
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

    #3671: guarded at the call site in ``run_async`` by ``is_litellm_ready()``
    — the queue this drains can only have entries if ``acompletion()`` ran
    at least once this call (this docstring's own "enqueues ... after every
    acompletion()"), so a session that never touched the LLM has NOTHING to
    drain, and an unconditional touch below would otherwise force litellm's
    cold import (#3671's whole cost) just to confirm an empty queue.

    #4395/#4421 (architect finding): the call-site guard used to be
    ``"litellm" in sys.modules`` — Python places a module into
    ``sys.modules`` at the START of import, before its top-level code
    finishes, so that check only ever proved the import STARTED, not that
    it FINISHED; #4417's background warming thread turned that into a live
    race (a shutdown racing a still-in-flight warm-up could grab a
    genuinely incomplete module). Both the call-site guard and this
    function's own body were fixed the same way as #4423: gate on
    ``is_litellm_ready()`` (the real "genuinely finished" signal) and read
    the confirmed module's attributes — never a fresh ``import`` of any
    kind, including a submodule. ``litellm.litellm_core_utils.logging_
    worker`` is not one of the submodules litellm's own ``__init__``
    eagerly populates, so ``ensure_litellm_ready()`` itself now imports it
    once, inside the chokepoint (#4421 seam alignment) — this function
    never needs an ``import`` statement of its own at all.
    """
    from reyn.llm.litellm_bootstrap import is_litellm_ready
    if not is_litellm_ready():
        return
    try:
        import sys
        litellm = sys.modules["litellm"]
        await litellm.litellm_core_utils.logging_worker.GLOBAL_LOGGING_WORKER.clear_queue()
    except Exception:
        # Best-effort: never raise from shutdown.
        pass


async def _close_litellm_async_clients(pre_existing_keys: frozenset) -> None:
    """Close LiteLLM's cached aiohttp-backed async HTTP clients before the
    event loop closes (issue #2787), scoped to clients created during THIS
    `run_async` call (#3434).

    #3434 root cause: litellm's own `close_litellm_async_clients()` iterates
    *every* entry in the process-wide `litellm.in_memory_llm_clients_cache`
    unconditionally — not just entries this call created. The cache is never
    evicted on close (see `LLMClientCache`'s own docstring: eviction
    intentionally never closes, because an in-flight request may still hold
    the client), so a prior test's still-open, still-cached client for e.g.
    `vertex_ai` gets closed-but-left-cached the moment *any* `run_async` call
    runs anywhere later in the same worker process — even an LLM-free one.
    `get_cache` then hands that closed client to the next real call with a
    matching provider/params key, which fails with "Cannot send a request,
    as the client has been closed." This is exactly why the failing test
    varies run to run under `-n auto`: it depends on xdist worker
    assignment and intra-worker test order, not on any one test's own
    defect.

    Background (why closing is needed at all — issue #2787):
      LiteLLM's default async transport (`litellm/llms/custom_httpx/
      aiohttp_transport.py`) is a real `aiohttp.ClientSession` cached in
      the process-wide `litellm.in_memory_llm_clients_cache`.
      `ClientSession.__del__` / `BaseConnector.__del__` fire a
      "message"-only (no ``exception`` key) `loop.call_exception_handler`
      context plus a `ResourceWarning` when a session/connector is
      garbage-collected still open -- this is exactly the "Unhandled
      exception in event loop: / Exception None" noise reported in #2787.
      Must run *after* `shutdown_logging`: `clear_queue()` awaits queued
      LiteLLM `async_success_handler` coroutines, which may still need the
      cached async client to complete -- closing the client first could
      break that drain.

    Fix: diff the cache's key set against `pre_existing_keys` (#3671:
    `litellm_bootstrap.client_cache_baseline()`, captured lazily by the
    first real litellm use *within this call*, not necessarily by
    `run_async` itself — see that function's own docstring for why) and
    temporarily hide the pre-existing entries from litellm's own cache dict
    while invoking its official close routine — so only clients newly
    cached during this call get closed, and clients other in-flight
    callers still own are left alone. Restoring afterwards is unconditional
    (`finally`) so a raise from litellm's close routine can never
    permanently evict them.

    #3671: NOT called at all (not "called with an empty pre_existing_keys")
    when the call never touched litellm — `run_async` checks
    `client_cache_baseline() is not None` before calling this, so a
    session that never used the LLM never pays this function's own `import
    litellm` either. Calling it with `pre_existing_keys=frozenset()` for
    that case (the previous behaviour) would look idempotent but is NOT:
    `cache_dict` may be non-empty from OTHER calls in the same worker
    process, and an empty baseline would make the diff below treat every
    one of THEIR entries as "new to this call" and close them — the #3434
    bug, reintroduced by the very frozenset() default that looked like a
    safe idempotent no-op.

    Considered and accepted, not overlooked: the hidden window spans an
    `await` (litellm's close routine), so in principle another task on the
    SAME event loop could `get_cache`/`set_cache` a preserved key while it's
    hidden, miss, create a replacement, and then have this function's final
    `cache_dict.update(preserved)` clobber that replacement with the
    preserved (older) entry. Accepted for this call site specifically: this
    window only exists on `run_async`'s own shutdown path, after `await
    coro` has already returned/raised — by that point nothing on this loop
    is meant to still be issuing LLM calls that would touch the cache. The
    alternative (not hiding at all) reintroduces the #3434 defect this
    function exists to fix, which is the worse failure mode.
    """
    # #4395/#4421: bare `import litellm` → `is_litellm_ready()` gate + read
    # the confirmed module. The docstring above already argues this is
    # call-order-safe (the call site never invokes this function unless
    # `client_cache_baseline()` is non-None, which only happens after a
    # genuine litellm touch this call) — but "safe by call order" is
    # exactly the shape #4415/#4417 showed can go stale silently when a
    # NEW call path is added later. Gating structurally costs nothing here
    # (this function already tolerates litellm being unavailable — return
    # early) and removes the dependency on that argument staying true.
    from reyn.llm.litellm_bootstrap import is_litellm_ready
    if not is_litellm_ready():
        return
    import sys
    litellm = sys.modules["litellm"]

    cache = getattr(litellm, "in_memory_llm_clients_cache", None)
    cache_dict = getattr(cache, "cache_dict", None)
    if cache_dict is None:
        return

    preserved = {k: v for k, v in cache_dict.items() if k in pre_existing_keys}
    for key in preserved:
        del cache_dict[key]
    try:
        try:
            await litellm.llms.custom_httpx.async_client_cleanup.close_litellm_async_clients()
        except Exception:
            # Best-effort: never raise from shutdown.
            pass
    finally:
        cache_dict.update(preserved)


def run_async(coro: Coroutine[object, object, T]) -> T:
    """`asyncio.run` plus LiteLLM logging-worker drain. See `shutdown_logging`.

    This is the shared loop-owning choke point for `reyn chat` (the
    interactive REPL / `--once` one-shot drive) and the mcp.py CLI commands
    that build their own event loop -- installing the durable asyncio
    unhandled-exception capture here (rather than at each call site)
    covers all of them from one place. See
    ``reyn.core.events.asyncio_diagnostics`` for why.

    #3671: does NOT import litellm itself, and never did directly — but
    used to force it as a side effect via `litellm_bootstrap`'s
    predecessor of `client_cache_baseline` being captured EAGERLY here,
    before `coro` (the whole session) had run at all. A session that
    never calls the LLM (the common case for "just show the TUI") was
    paying litellm's own multi-second cold import before the UI could
    mount, for a baseline `_close_litellm_async_clients` would only ever
    need if the LLM WAS called. `reset_client_cache_baseline` below only
    arms lazy capture — the ACTUAL import now happens on whichever call
    reaches it first, inside the session's own real LLM use
    (`recorded_acompletion`/the embedding provider), if it happens at
    all. `"litellm" not in sys.modules` after a session that never used
    it is the gate this exists to keep true —
    `test_run_async_never_imports_litellm_without_an_llm_call` pins it.
    """
    from reyn.llm.litellm_bootstrap import client_cache_baseline, reset_client_cache_baseline

    # #3434 (scope) / #3671 (cost): arm a FRESH per-call baseline without
    # importing litellm — see `reset_client_cache_baseline`'s own docstring.
    reset_client_cache_baseline()

    async def _wrapped() -> T:
        from reyn.core.events.asyncio_diagnostics import (
            install_asyncio_exception_handler,
        )
        install_asyncio_exception_handler(asyncio.get_running_loop())
        try:
            return await coro
        finally:
            # #3671: two INDEPENDENT gates, deliberately not one.
            #
            # `shutdown_logging` gates on `is_litellm_ready()` internally
            # (own function, own docstring) — the general "was litellm
            # FULLY imported this call" signal, true for `recorded_
            # acompletion`/the embedding provider AND for any other lazy
            # litellm call site in this codebase (there are several —
            # cost/pricing/budget lookups, `router_loop.py`, the replay
            # harness) that does not happen to route through `ensure_
            # litellm_ready`. Draining litellm's logging-worker queue is
            # safe and idempotent regardless of WHICH path imported
            # litellm, so the broader signal is correct here. #4395/#4421
            # (architect finding): the call SITE used to re-check
            # `"litellm" in sys.modules` before calling — Python places a
            # module into `sys.modules` at the START of import, before
            # its top-level code finishes, so that check only ever proved
            # the import STARTED. Removed here — `shutdown_logging()` now
            # owns its own correct (`is_litellm_ready()`) gate internally,
            # a single source of truth rather than two checks that could
            # drift apart.
            #
            # `_close_litellm_async_clients` gates on `client_cache_baseline()
            # is not None` — a NARROWER, TRUSTED-baseline-only signal.
            # Closing clients needs to know what pre-existed THIS call
            # (#3434); a call that imported litellm through some OTHER path
            # never captured that baseline, and guessing `frozenset()` would
            # misread "unknown" as "nothing pre-existed" and close every
            # OTHER call's still-open client — the #3434 bug, reintroduced.
            # Skipping is the safe direction: at worst a client that path
            # created is not closed until GC (#2787's pre-existing risk
            # class, not a new one), never another call's client closed
            # early.
            await shutdown_logging()
            pre_existing_keys = client_cache_baseline()
            if pre_existing_keys is not None:
                await _close_litellm_async_clients(pre_existing_keys)

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
    # #1652/②: the model's reasoning as a normalized BUNDLE
    # ({reasoning_content?, thinking_blocks?, provider_specific_fields?}) — the
    # litellm cross-provider standard, captured so the chat layer can persist it
    # and re-attach it natively to the assistant history message next turn (not
    # just as SP text). None when the model emitted no reasoning (thinking off /
    # weak model / first turn). Captured at the boundary; display + cross-turn
    # replay are gated in the chat layer, capture is always-on. (Legacy persisted
    # entries may be a plain ``str`` = the old text-only shape; readers absorb it
    # as ``{"reasoning_content": str}``.)
    reasoning: dict | None = None

# ---------------------------------------------------------------------------
# Infrastructure retry — exponential backoff on transient LLM API errors
# ---------------------------------------------------------------------------

# Retryable: infrastructure / transient errors where the same call may succeed.
# Non-retryable: semantic / auth / quota errors (4xx) where retry won't help.
# Resolved lazily so importing this module does not trigger `import litellm`.
# #3671 P1: single check-then-set on ONE global holding the complete tuple —
# already the correct shape (see ``_get_retryable_litellm_exceptions``'s own
# docstring below), no lock needed: the checked value and the assigned
# value are the SAME variable, assigned in one atomic STORE, so a reader
# only ever observes ``None`` or the fully-built tuple, never a partial one.
_RETRYABLE_LITELLM_EXCEPTIONS: tuple | None = None

# Resolved lazily so importing this module does not trigger `import httpx` (its
# CLI pretty-printing subtree — rich.progress / rich.syntax / pygments — costs
# ~90ms and reyn never invokes it; cold-start sweep companion to #2930's
# litellm chokepoint, see litellm_bootstrap.py's module docstring).
#
# #3671 P1: this used to be TWO separate globals (`_HTTPX_READ_TIMEOUT_EXC` /
# `_HTTPX_CONNECT_ERROR_EXC`), checked via ONE of them directly — the same
# "invariant split across two variables" bug shape as the version of
# ``ensure_litellm_ready`` lead-coder found, not a mere missing lock: a
# thread scheduled between the two assignment statements could hand a
# concurrent caller a tuple with one member still ``None`` (a caller doing
# ``isinstance(exc, connect_err_type)`` with ``connect_err_type is None``
# raises ``TypeError``, not a clean non-match). Adding a lock around the
# two-variable version would only have hidden this, not fixed it, per owner
# directive (prefer a non-lock fix that removes the actual defect over
# excluding around it). The real fix: ONE global holding the complete pair,
# assigned in a single atomic STORE — same shape as
# `_RETRYABLE_LITELLM_EXCEPTIONS` above.
_HTTPX_EXC_TYPES: "tuple[type[BaseException], type[BaseException]] | None" = None


def _get_httpx_exc_types() -> "tuple[type[BaseException], type[BaseException]]":
    """Return ``(httpx.ConnectError, httpx.ReadTimeout)``, loading httpx lazily.

    Cached in ``_HTTPX_EXC_TYPES`` after first call — see that global's own
    comment for why it is ONE tuple, not two separate globals (#3671 P1).
    """
    global _HTTPX_EXC_TYPES
    if _HTTPX_EXC_TYPES is None:
        import httpx  # noqa: PLC0415
        _HTTPX_EXC_TYPES = (httpx.ConnectError, httpx.ReadTimeout)
    return _HTTPX_EXC_TYPES

def _env_num(name: str, default: "int | float", lo: "int | float", hi: "int | float",
             cast):
    """Operator tuning knob from the environment, clamped to ``[lo, hi]``; falls back
    to ``default`` on unset/invalid. A flaky-provider robustness lever: bump retries /
    backoff without a code change (parallels REYN_LLM_TRACE_DUMP + the #1626
    empty-choices observability). Read once at import — set it in the subprocess env."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(lo, min(hi, cast(raw)))
    except (TypeError, ValueError):
        return default


# Defaults preserve today's behaviour (3 attempts / 2s base); the env overrides let an
# operator absorb a transient empty-generation / 5xx storm without editing code.
_LLM_RETRY_MAX_ATTEMPTS: int = _env_num("REYN_LLM_RETRY_MAX_ATTEMPTS", 3, 1, 10, int)
_LLM_RETRY_BASE_S: float = _env_num("REYN_LLM_RETRY_BASE_S", 2.0, 0.1, 30.0, float)
_LLM_RETRY_MAX_BACKOFF_S: float = 16.0

# #1829 S3b: SINGLE-SOURCE router config resolution. The chokepoint funcs
# (_use_llm_router / the Router builder) must NOT each read env independently
# (double-source). They all resolve through ``_resolved_router_config()``:
# reyn.yaml ``llm.router.*`` (set on this ContextVar by the runtime/session at
# construction — same pattern as ``set_llm_request_event_log``) is authoritative;
# when absent (tests / CLI / pre-#1829 configs) it falls back to the legacy env
# vars + defaults (the ``ssl_verify`` → env → default idiom). One resolution site.
_router_config_var: "contextvars.ContextVar[object | None]" = contextvars.ContextVar(
    "reyn_llm_router_config", default=None,
)


def set_router_config(cfg: object) -> "contextvars.Token":
    """#1829 S3b: set the ambient ``RouterConfig`` (reyn.yaml ``llm.router.*``)
    the LLM chokepoint resolves against. The runtime/session sets this at
    construction (mirrors ``set_llm_request_event_log``). Returns the token so a
    caller MAY reset for a nested scope. ``None`` → env+default fallback."""
    return _router_config_var.set(cfg)


def _env_router_config():
    """Back-compat ``RouterConfig`` from the legacy env vars + defaults, used when
    no reyn.yaml router config is in context (tests / CLI / pre-#1829). This is the
    ``env → default`` tail of the single-source idiom."""
    from reyn.config.infra import RouterConfig
    return RouterConfig(
        use=os.environ.get("REYN_LLM_USE_ROUTER", "").strip().lower()
        in ("1", "true", "yes"),
        num_retries=_env_num("REYN_LLM_ROUTER_NUM_RETRIES", 3, 0, 10, int),
    )


def _resolved_router_config():
    """#1829 S3b single-source: the effective ``RouterConfig``. reyn.yaml (via the
    ContextVar) is authoritative; absent → env+default. The ONLY place router
    config is resolved — ``_use_llm_router`` and the Router builder both read this,
    so there is never a double source (PR-review axis #3)."""
    cfg = _router_config_var.get()
    return cfg if cfg is not None else _env_router_config()


def _use_llm_router() -> bool:
    """#1829: True when the LLM call routes through a litellm.Router. Default OFF
    → byte-equivalent to the direct ``litellm.acompletion`` call. Resolved
    single-source from reyn.yaml ``llm.router.use`` (authoritative) or the legacy
    ``REYN_LLM_USE_ROUTER`` env var (fallback)."""
    return bool(_resolved_router_config().use)


# #1835: ambient retry config (jitter + respect_retry_after). The runtime/session
# sets this at construction via ``set_retry_config`` (mirrors set_router_config).
# Default=None → env-fallback defaults (both features ON). The ContextVar scope
# ensures per-task isolation under pytest-asyncio per-test event loops.
_retry_config_var: "contextvars.ContextVar[object | None]" = contextvars.ContextVar(
    "reyn_llm_retry_config", default=None,
)


def set_retry_config(cfg: object) -> "contextvars.Token":
    """#1835: publish the ambient ``RetryConfig`` (reyn.yaml ``llm.retry.*``).

    The runtime/session sets this at construction; mirrors ``set_router_config``.
    Returns the token so a caller MAY reset for a nested scope. ``None`` → defaults.
    """
    return _retry_config_var.set(cfg)


def _resolved_retry_config():
    """#1835: single-source effective ``RetryConfig``.

    reyn.yaml ``llm.retry.*`` (via the ContextVar) is authoritative; absent →
    default (both jitter and respect_retry_after ON). The ONLY place retry timing
    config is resolved — ``_backoff_s`` and ``_llm_call_with_retry`` read this.
    """
    cfg = _retry_config_var.get()
    if cfg is not None:
        return cfg
    from reyn.config.infra import RetryConfig
    return RetryConfig()


# #1868: ambient budget-limit policy context for the per-LLM-call cost gate. The
# runtime/session sets (bus, on_limit, run_id, non_interactive) here at the same
# place it binds the limit framework (mirrors set_llm_request_event_log /
# set_router_config). UNSET → the budget gate FAILS CLOSED (deny / unattended) — no
# policy context means no silent allow (owner + safety-critical requirement).
class LLMCallLimitContext(NamedTuple):
    """The ambient per-LLM-call policy context (#1868 budget gate + #2210 timeout gate).

    Carries the safety/limit policy the per-LLM-call gates route a limit-exceed through:
    ``bus`` / ``on_limit`` / ``run_id`` / ``non_interactive`` (the budget-exceed +
    timeout-exhaustion → ``handle_limit_exceeded`` channel) PLUS the per-call HTTP bounds
    ``llm_call_timeout`` / ``llm_max_retries`` (#2210: the router path passes no explicit
    ``timeout`` to ``call_llm_tools`` and reads these from here — the kernel path passes
    its own explicit values and is unaffected). ``None`` timeout/retries = not published
    (the explicit-param caller, or a context set without the #2210 fields)."""

    bus: object
    on_limit: object
    run_id: object
    non_interactive: bool
    llm_call_timeout: "float | None" = None
    llm_max_retries: "int | None" = None


_llm_call_limit_context_var: "contextvars.ContextVar[LLMCallLimitContext | None]" = (
    contextvars.ContextVar("reyn_llm_call_limit_context", default=None)
)


def set_llm_call_limit_context(
    bus: object, on_limit: object, run_id: object, non_interactive: bool = False,
    llm_call_timeout: "float | None" = None, llm_max_retries: "int | None" = None,
) -> "contextvars.Token":
    """#1868/#2210: publish the per-LLM-call policy context (bus / on_limit / run_id /
    non_interactive + the per-call timeout / retries). The cost gate (budget exceed) and
    the timeout gate (persistent provider hang) both route through it; the router path
    also reads ``llm_call_timeout`` / ``llm_max_retries`` for its ``call_llm_tools`` call.
    Set by the runtime at construction; propagates into the run's tasks. Returns the token
    so a caller MAY reset for a nested scope."""
    return _llm_call_limit_context_var.set(LLMCallLimitContext(
        bus, on_limit, run_id, non_interactive, llm_call_timeout, llm_max_retries))


async def _budget_exceed_allows_continue(check: object, budget_agent: object) -> bool:
    """#1868: route a ``check_pre_llm`` refusal through the limit framework's 3-mode
    policy (``handle_limit_exceeded``). Returns True if the over-budget call may
    proceed (interactive-approved or bounded auto-extend), False to deny (the caller
    then raises ``BudgetExceeded`` — today's behavior). The ambient policy context
    is set by ``set_llm_call_limit_context``; **UNSET → fail-closed deny** (no policy
    = no silent allow). BudgetLedger accounting is unchanged — only the exceed→
    response path is unified; an allowed call is still recorded by construction."""
    ctx = _llm_call_limit_context_var.get()
    if ctx is None:
        return False  # fail-closed: no policy context → deny (= unattended)
    from reyn.runtime.budget.budget import format_refusal_message
    from reyn.runtime.limits.limit_handler import handle_limit_exceeded
    dimension = getattr(check, "hard_dimension", None) or "budget"
    decision = await handle_limit_exceeded(
        bus=ctx.bus,
        on_limit=ctx.on_limit,
        kind=f"cost.{dimension}",  # dimension-in-kind (#1868 Q4): own auto_extend counter + audit
        run_id=str(ctx.run_id or ""),
        prompt=format_refusal_message(check, agent=budget_agent),
        detail=(f"agent={budget_agent}" if budget_agent else ""),
        extension_amount=1.0,  # allow ONE over-cap call past the gate (bounded by auto_extend_times)
        non_interactive=bool(ctx.non_interactive),
    )
    return bool(decision.allow_continue)


def _is_llm_timeout_exc(exc: BaseException) -> bool:
    """#2210: True for a per-call HTTP TIMEOUT (a hung/slow provider) — ``litellm`` Timeout or
    an ``httpx`` read timeout, the timeout subset of ``_is_retryable_exc``. Used to route ONLY
    a persistent timeout through the on_limit policy (other infra errors surface as-is).
    ``httpx`` is lazy-imported (avoids a module-level ``import httpx`` — see
    ``_get_httpx_exc_types``).

    #4395 (owner-observed, live: ``AttributeError: module 'litellm' has no
    attribute 'exceptions'``): this used to do its own bare ``import
    litellm`` — a chokepoint-bypass with a NEW failure mode PR-2's
    background warming thread exposed. Python places a module into
    ``sys.modules`` at the START of import, before its top-level code
    finishes — a bare ``import litellm`` on the main thread while the
    warming thread is mid-import can observe the SAME (still-executing,
    genuinely incomplete) module object, missing attributes litellm's own
    ``__init__`` hasn't assigned yet. Before PR-2, every import was
    synchronous on whichever thread triggered it, so this race was latent,
    never live. Fixed the same way as ``pricing.py``'s
    ``_usage_object_for`` (#4413) and ``_get_retryable_litellm_exceptions``
    (#4417 hardening): gate on ``is_litellm_ready()`` — which is only
    True once the chokepoint's OWN attempt (real ownership via
    ``_ready_registry``'s Future, not a bare ``import`` racing it) has
    genuinely finished — and read the module from ``sys.modules`` only
    then. If litellm isn't ready yet, ``exc`` cannot be a genuine
    ``litellm.exceptions.Timeout`` instance (you cannot instantiate a
    class from a module that was never successfully imported), so
    returning False here is correct, not just a safe placeholder.
    """
    from reyn.llm.litellm_bootstrap import is_litellm_ready
    _, read_timeout_exc = _get_httpx_exc_types()
    if isinstance(exc, read_timeout_exc):
        return True
    if not is_litellm_ready():
        return False
    import sys
    litellm = sys.modules["litellm"]
    return isinstance(exc, litellm.exceptions.Timeout)


def _resolve_llm_call_bounds(
    timeout: "float | None", max_retries: int
) -> "tuple[float | None, int]":
    """#2210: resolve the per-call HTTP bounds for ``call_llm_tools``. An EXPLICIT
    ``timeout`` (the kernel path threads it via the LLMCallRecorder) WINS — returned as-is,
    so the kernel behaviour is unchanged (regression-impossible by construction). Only a
    ``None`` timeout (the router path, which passes no timeout) falls back to the ambient
    per-LLM-call policy context (same ``safety.timeout.*`` source). No context → stays
    ``None`` (litellm default — the pre-#2210 behaviour, no worse)."""
    if timeout is None:
        ctx = _llm_call_limit_context_var.get()
        if ctx is not None:
            timeout = ctx.llm_call_timeout
            if ctx.llm_max_retries is not None:
                max_retries = ctx.llm_max_retries
    return timeout, max_retries


async def _llm_timeout_allows_continue(model: object, detail: str) -> bool:
    """#2210: route a persistent LLM-call TIMEOUT (the per-call HTTP timeout + Router/Reyn
    retries all exhausted = a hung/slow provider) through the SAME limit framework the
    budget gate uses (``handle_limit_exceeded`` + ``safety.on_limit``), instead of a bare
    error. Returns True if the caller may RETRY once more (a fresh timeout window —
    interactive-approved, or bounded ``auto_extend`` within ``auto_extend_times`` so a hung
    provider cannot retry forever), False to give up and surface the timeout (clean
    turn-end). UNSET context → fail-closed (no retry; surface the timeout)."""
    ctx = _llm_call_limit_context_var.get()
    if ctx is None:
        return False  # fail-closed: no policy context → surface the timeout
    from reyn.runtime.limits.limit_handler import handle_limit_exceeded
    decision = await handle_limit_exceeded(
        bus=ctx.bus,
        on_limit=ctx.on_limit,
        kind="timeout.llm_call",  # own auto_extend counter + audit namespace
        run_id=str(ctx.run_id or ""),
        prompt=(f"The model provider ({model}) is not responding — the request timed out "
                "after the configured retries. Retry, or stop?"),
        detail=detail,
        extension_amount=1.0,  # one more timeout window per approval (bounded by auto_extend_times)
        non_interactive=bool(ctx.non_interactive),
    )
    return bool(decision.allow_continue)


class EmptyLLMResponseError(Exception):
    """The LLM returned a 200 response with an empty ``choices`` list.

    Not an API-level error — litellm neither raises nor retries it — yet the
    downstream ``response.choices[0]`` access would IndexError and silently
    crash the router loop mid-task (#187 B1: gemini-2.5-flash-lite via the
    LiteLLM proxy intermittently returns this 200+empty shape, killing the
    turn before the agent edits). Raised by ``_llm_call_with_retry`` so the
    same backoff machinery retries it (the condition is transient), and on
    exhaustion the caller sees this named error instead of a cryptic
    IndexError.
    """


def _empty_response_diag(response: object) -> str:
    """Compact provider-response shape for an empty-choices error (flake
    observability). The block reason — Gemini ``finish_reason``
    SAFETY/MAX_TOKENS/RECITATION, ``prompt_feedback``, ``usage`` — lives in
    vendor-specific fields we can't predict, so dump the whole (truncated) response
    so a recurrence is diagnosable instead of a bare "empty choices". Best-effort:
    diagnostics must NEVER mask or replace the empty-choices error itself."""
    try:
        return json.dumps(response.model_dump(), default=str)[:500]
    except Exception:  # noqa: BLE001 — never let a diag failure shadow the real error
        return repr(response)[:300]


def _get_retryable_litellm_exceptions() -> tuple:
    """Return the tuple of retryable litellm exceptions.

    Cached in _RETRYABLE_LITELLM_EXCEPTIONS after first call.

    #3671 P1: unsynchronized check-then-set, deliberately left without a
    lock (owner directive: no lock without a real correctness need) — the
    checked global and the assigned global are the SAME variable, in one
    atomic STORE, so a concurrent reader only ever observes ``None`` or the
    complete tuple.

    #4395 PR-2 hardening (NOT a required landing condition — architect
    measured the actual call graph and confirmed this function's only
    reachable path, `_llm_call_with_retry`'s exception handler ← a real
    completion attempt that already imported litellm via `recorded_
    acompletion`'s own readiness check, is never actually racing the
    background warming thread): this function no longer imports litellm
    on its own AT ALL — it only reads `sys.modules["litellm"]` once
    `is_litellm_ready()` confirms it is already there. This removes a
    CAPABILITY (independently importing litellm) rather than adding a new
    moving part — the call-order argument above is correct TODAY but is an
    implicit invariant a future refactor could silently break; this makes
    it a structural guarantee instead. Same shape as #4413's fix to
    `pricing.py`'s `_usage_object_for` — "structurally incapable of
    bypassing the chokepoint," not just reliant on today's caller
    happening to gate it first. If litellm isn't ready yet, returns an
    empty tuple — logically correct, not just a safe placeholder: `exc`
    cannot be a genuine instance of one of litellm's own exception classes
    if litellm itself was never successfully imported, so `isinstance(exc,
    ())` (always False) can't misclassify anything.
    """
    global _RETRYABLE_LITELLM_EXCEPTIONS
    if _RETRYABLE_LITELLM_EXCEPTIONS is None:
        from reyn.llm.litellm_bootstrap import is_litellm_ready
        if not is_litellm_ready():
            return ()
        import sys
        litellm = sys.modules["litellm"]
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
    if isinstance(exc, EmptyLLMResponseError):
        # #187 B1: 200 + choices=[] is a transient provider condition — retry.
        return True
    if isinstance(exc, _get_retryable_litellm_exceptions()):
        return True
    if isinstance(exc, _get_httpx_exc_types()):
        return True
    return False


def _backoff_s(attempt: int) -> float:
    """Exponential backoff with optional equal jitter (#1835), capped at max.

    When ``llm.retry.jitter`` is true (default), applies AWS-style equal jitter:
      ``sleep = base/2 + uniform(0, base/2)`` (range: [base/2, base]).
    When false, returns pure exponential (legacy: 2s, 4s, 8s, 16s).

    ``attempt`` is 0-indexed (attempt 0 = first retry, after initial call fails).
    Min 0.0 — never negative on a negative index.
    """
    # #2259 PR-2a: the timing curve is the shared `backoff_s` formula (one formula across the
    # LLM-call + durable-write retry paths); the LLM path injects its base/cap + config jitter.
    from reyn.core.retry import backoff_s  # noqa: PLC0415
    return backoff_s(
        attempt, base_s=_LLM_RETRY_BASE_S, max_s=_LLM_RETRY_MAX_BACKOFF_S,
        jitter=_resolved_retry_config().jitter,
    )


def _extract_retry_after(exc: BaseException) -> float | None:
    """#1835: extract the ``Retry-After`` wait (seconds) from a retryable exception.

    Checks (in order, defensive at each step — an unparseable value is ignored):
      1. ``exc.response.headers["retry-after"]`` — the canonical HTTP location.
         litellm wraps provider responses as ``httpx.Response``; the header is
         present on 429 / 503 replies that carry it.
      2. ``exc.headers`` — a secondary attr some litellm exception shapes expose
         (not present in current litellm, but defensive probe costs nothing).

    ``Retry-After`` value formats (RFC 7231):
      - Delta-seconds (e.g. ``"30"`` → 30.0 s).
      - HTTP-date (e.g. ``"Sat, 21 Jun 2026 12:00:00 GMT"`` → computed delta).
        Negative deltas (date in the past) are clamped to 0.

    Returns the wait in seconds, capped at ``_LLM_RETRY_MAX_BACKOFF_S``, or
    ``None`` when no parseable ``Retry-After`` is found (caller falls back to
    jittered backoff).  Never raises — unparseable values are silently ignored.
    """
    # Gather candidate header dicts (response.headers takes priority).
    header_dicts: list = []
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            header_dicts.append(headers)
    exc_headers = getattr(exc, "headers", None)
    if exc_headers is not None:
        header_dicts.append(exc_headers)

    for headers in header_dicts:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            continue
        if not raw:
            continue
        try:
            # Try delta-seconds first (most common: "30", "120").
            delta = float(raw)
            return min(max(0.0, delta), _LLM_RETRY_MAX_BACKOFF_S)
        except (ValueError, TypeError):
            pass
        try:
            # Try HTTP-date ("Sat, 21 Jun 2026 12:00:00 GMT").
            dt = parsedate_to_datetime(raw)
            delta = (dt - datetime.now(UTC)).total_seconds()
            return min(max(0.0, delta), _LLM_RETRY_MAX_BACKOFF_S)
        except Exception:
            pass
    return None


async def _llm_call_with_retry(
    coro_fn,
    model: str,
    event_log: "EventLog | None",
    *,
    sleep_fn=None,
) -> object:
    """Execute ``coro_fn()`` with infrastructure-error retry + backoff.

    ``coro_fn`` must be a zero-arg async callable that returns the litellm
    response object.  It is called once per attempt.

    ``sleep_fn`` is an injectable async sleep callable (default: ``asyncio.sleep``).
    Tests pass a recording shim here to capture actual sleep durations without
    mocking — the default is preserved in production so behaviour is unchanged.

    Backoff timing (#1835):
      - ``llm.retry.jitter=true`` (default): equal jitter (AWS pattern):
        ``sleep = base/2 + uniform(0, base/2)`` where ``base = min(base_s * 2**attempt, max_s)``.
      - ``llm.retry.respect_retry_after=true`` (default): when a retryable exception
        carries a ``Retry-After`` header, honour it (capped at max_backoff) INSTEAD
        of the jittered backoff.

    Emits ``llm_call_retry`` on each retry and ``llm_call_retry_exhausted``
    when all attempts are exhausted.  When ``event_log`` is None, observability
    events are silently skipped (= callers without an EventLog context).

    Raises the last exception when all retries are exhausted.
    """
    if sleep_fn is None:
        sleep_fn = asyncio.sleep
    last_exc: BaseException | None = None
    for attempt in range(_LLM_RETRY_MAX_ATTEMPTS):
        try:
            response = await coro_fn()
            # #187 B1 root fix: an empty `choices` list is a transient provider
            # condition — not an API error, so litellm neither raises nor retries
            # it, yet the downstream `response.choices[0]` access IndexErrors and
            # silently kills the router loop mid-task. Raise a named retryable
            # error so the SAME backoff machinery retries it (covers both
            # call_llm and call_llm_tools, the two choices[0] callsites), and on
            # exhaustion the caller sees a clear error instead of an IndexError.
            if not getattr(response, "choices", None):
                # Flake observability: capture the provider response shape (finish_reason
                # / prompt_feedback / safety-block / usage) so a recurrence is
                # diagnosable. logging.warning fires per attempt (a flake that self
                # -recovers on retry still leaves its WHY in the log); the error message
                # carries it for the exhaustion path.
                _diag = _empty_response_diag(response)
                logging.getLogger(__name__).warning(
                    "LLM returned 200 with empty choices (model=%s) — provider response: %s",
                    model, _diag,
                )
                raise EmptyLLMResponseError(
                    f"LLM returned a 200 response with empty choices (model={model!r}); "
                    f"provider response: {_diag}"
                )
            return response
        except BaseException as exc:
            if not _is_retryable_exc(exc):
                raise
            # #1829 S3a (#1835 fold): on the router path the litellm.Router has
            # ALREADY retried infra exceptions (5xx / timeout / connect) with
            # native Retry-After respect, so re-retrying them here would double
            # (Router N × Reyn N). Only EmptyLLMResponseError (200 + empty choices,
            # #187 B1) stays Reyn-owned — the Router does not retry a non-exception
            # 200. Router OFF → unchanged (full exponential-backoff retry of all
            # _is_retryable_exc kinds; byte-identical to pre-#1829).
            if _use_llm_router() and not isinstance(exc, EmptyLLMResponseError):
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
            # #1835: Retry-After takes priority over jittered backoff when
            # respect_retry_after is enabled (default true) and the exception
            # carries a parseable Retry-After header. Falls back to _backoff_s
            # (which applies equal jitter when jitter=true, default).
            retry_cfg = _resolved_retry_config()
            retry_after = (
                _extract_retry_after(exc) if retry_cfg.respect_retry_after else None
            )
            backoff = retry_after if retry_after is not None else _backoff_s(attempt)
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
            await sleep_fn(backoff)
    # Should be unreachable — loop always raises or returns.
    assert last_exc is not None
    raise last_exc  # pragma: no cover


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


def _extract_cache_tokens(u) -> tuple[int, int]:
    """Extract (cached_tokens, cache_creation_tokens) from a litellm usage obj.

    cached_tokens (cache READ / hit) is cross-provider normalized: litellm
    surfaces it as both ``usage.cache_read_input_tokens`` (top-level, Anthropic
    style) and ``usage.prompt_tokens_details.cached_tokens`` (OpenAI style) —
    equal when both present. Prefer the top-level field, fall back to the
    nested one. cache_creation (``cache_creation_input_tokens``, Anthropic
    cache-write) has no OpenAI / Gemini equivalent → 0 there.
    Best-effort: any missing / non-numeric field reads as 0.
    """
    def _as_int(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    cached = _as_int(getattr(u, "cache_read_input_tokens", None))
    if cached == 0:
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            getter = details.get if isinstance(details, dict) else (
                lambda k, _d=details: getattr(_d, k, None)
            )
            cached = _as_int(getter("cached_tokens"))
    creation = _as_int(getattr(u, "cache_creation_input_tokens", None))
    return cached, creation


def _dictify(v):
    """Best-effort convert a litellm sub-object to a JSON-serialisable form
    (so a reasoning bundle survives history persistence). ``model_dump`` for
    pydantic, recurse lists, else pass through."""
    if hasattr(v, "model_dump"):
        try:
            return v.model_dump()
        except Exception:
            return None
    if isinstance(v, list):
        return [_dictify(x) for x in v]
    return v


def _extract_reasoning_bundle(msg) -> dict | None:
    """#1652/②: capture the model's reasoning as a normalized, persistable bundle.

    litellm standardizes provider reasoning onto ``reasoning_content`` (text) +
    ``thinking_blocks`` (structured) cross-provider — that is what Reyn's proxy
    returns. We capture those (provider-agnostic: NO per-provider logic) plus a
    generic ``provider_specific_fields`` catch-all when present, so the bundle
    can be re-attached natively to the assistant history message next turn and
    litellm re-applies it per provider.

    Returns ``None`` when the model emitted no reasoning (all fields empty) — the
    omit-when-empty discipline so an empty turn stays byte-identical. Each field
    is dict-ified so the bundle JSON-persists in history.
    """
    bundle: dict = {}
    text = getattr(msg, "reasoning_content", None) or None
    if text:
        bundle["reasoning_content"] = text
    thinking = getattr(msg, "thinking_blocks", None)
    if thinking:
        bundle["thinking_blocks"] = _dictify(thinking)
    psf = getattr(msg, "provider_specific_fields", None)
    if isinstance(psf, dict) and psf:
        _psf = _dictify(psf)
        if _psf:
            bundle["provider_specific_fields"] = _psf
    return bundle or None


#: #3351: attribute name under which ``recorded_acompletion`` stamps the
#: PROVENANCE of a response's token counts onto the response object itself,
#: so ``_extract_usage`` derives it in ONE place for every reader. Stamped on
#: the response rather than threaded as a parameter because
#: ``call_llm_tools`` re-extracts usage from the response object it got back
#: from the chokepoint — a parameter would have to be plumbed through two
#: call layers and would silently default to "provider" wherever someone
#: forgot. Underscore-prefixed so it stays out of litellm's ``model_dump``
#: (verified against a real ``ModelResponse``) and never reaches a payload
#: trace dump or the wire.
_USAGE_SOURCE_ATTR = "_reyn_usage_source"


def _stamp_usage_source(response: object, source: UsageSource) -> object:
    """Record where *response*'s token counts came from, returning *response*.

    A response object that refuses the attribute (a mapping, a slotted stand-in)
    leaves provenance UNSTATED — which ``_extract_usage`` reads as
    ``UNKNOWN``, never as ``PROVIDER``. The failure mode of the observability
    machinery is therefore "we don't know", not a false claim of exactness.
    """
    try:
        setattr(response, _USAGE_SOURCE_ATTR, source)
    except Exception:  # noqa: BLE001 — provenance stamping must never break a call
        logger.debug("could not stamp usage provenance on %s", type(response).__name__)
    return response


def _provider_reported_usage(chunks: list) -> bool:
    """#3351: did the PROVIDER's own token counts ride this chunk stream?

    ``litellm.stream_chunk_builder`` fills each missing field from
    ``litellm.token_counter`` (``prompt_tokens or token_counter(...)``, per
    field, in ``litellm_core_utils/streaming_chunk_builder_utils.py``), so the
    reconstructed ``response.usage`` is a LOCAL ESTIMATE for exactly the fields
    no chunk reported. This answers "were BOTH prompt and completion reported"
    — a partially-estimated total is an estimate for accounting purposes, and
    the conservative direction is the one that never labels a token_counter
    figure as provider-supplied.
    """
    prompt = 0
    completion = 0
    for chunk in chunks:
        u = getattr(chunk, "usage", None)
        if u is None:
            continue
        try:
            prompt += int(getattr(u, "prompt_tokens", 0) or 0)
            completion += int(getattr(u, "completion_tokens", 0) or 0)
        except (TypeError, ValueError):  # a malformed usage field reports nothing
            continue
    return bool(prompt) and bool(completion)


def _extract_usage(response) -> TokenUsage | None:
    """Extract token usage from a litellm response object.

    #3351: the returned ``TokenUsage`` carries its own PROVENANCE
    (``TokenUsage.source``), read from the stamp ``recorded_acompletion`` put
    on *response* — so every consumer of the numbers (``record_llm`` → the
    ledger / ``/cost`` / the budget caps, the cost audit-events, the per-turn
    buckets) holds the origin alongside the figure instead of receiving a bare
    int whose two possible origins differ by up to +86%.
    """
    try:
        u = response.usage
        if u is None:
            return None
        cached, creation = _extract_cache_tokens(u)
        return TokenUsage(
            prompt_tokens=int(u.prompt_tokens or 0),
            completion_tokens=int(u.completion_tokens or 0),
            cached_tokens=cached,
            cache_creation_tokens=creation,
            source=_read_usage_source(response),
        )
    except Exception:
        return None


def _read_usage_source(response: object) -> UsageSource:
    """The provenance stamped on *response*, or ``UNKNOWN`` when there is none.

    An unstamped response is a response obtained outside
    ``recorded_acompletion`` — nothing in ``src/`` does that today (the #1190
    AST guard keeps the completion call-sites inside that one funnel), so this
    reads UNKNOWN only for a hand-built object. Never PROVIDER by default.
    """
    return parse_usage_source(getattr(response, _USAGE_SOURCE_ATTR, None))


def proxy_kwargs() -> dict:
    """Return extra kwargs for litellm.completion() when a proxy is configured.

    api_base is read from LITELLM_API_BASE (set by CLI from reyn.yaml).
    API keys are read automatically by litellm from provider env vars
    (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) — never passed explicitly here.

    #4347: an earlier version DID read+pass ``OPENAI_API_KEY`` explicitly
    (7efd505f6) without updating this docstring, so doc and code disagreed.
    Removed on the owner's own framing: litellm abstracts 100+ providers, so
    reyn naming ONE provider's env var here means every OTHER provider's user
    (ANTHROPIC_API_KEY-only, etc.) got nothing from this line — the same
    "reyn can't enumerate providers litellm already abstracts" argument
    #3905 applied to ``_PROVIDER_ENV_VARS`` and closed there, not here.
    Measured on a live proxy (#4347's issue comments, armB/armC): omitting
    ``api_key`` entirely lets litellm resolve OPENAI_API_KEY itself when set
    (proxy call succeeds, reyn passed nothing), and when unset litellm raises
    its own named-and-actionable error telling the operator what to set —
    which the removed ``"dummy"`` fallback was silently swallowing.
    """
    api_base = os.environ.get("LITELLM_API_BASE")
    if not api_base:
        return {}
    return {"api_base": api_base, "custom_llm_provider": "openai"}


# #1829 S2: loop-aware single-deployment Router cache. A ``litellm.Router`` binds
# to the event loop it first awaits on, so a process-global cache would trip
# "bound to a different event loop" under pytest-asyncio's per-test loops (the
# reason S1 built per-call). Keying by the RUNNING loop gives each loop its own
# Router — the same loop-aware-registry pattern as the #1762 agent-lock fix.
# WeakKeyDictionary → a finished loop's Routers are GC'd with the loop.
_ROUTERS_BY_LOOP: "weakref.WeakKeyDictionary[object, dict[object, object]]" = (
    weakref.WeakKeyDictionary()
)


def _router_cache_fingerprint(rcfg) -> tuple:
    """#1829 S3b (F1): a hashable signature of the Router-build-affecting config
    (``num_retries`` / ``cooldown_time`` / ``allowed_fails`` / ``fallbacks``). Part
    of the per-loop cache key so a changed ``llm.router.*`` rebuilds the Router
    instead of silently reusing a stale one — the cache is correct WITHOUT relying
    on a "config is loop-uniform" assumption (today config is process-global, so
    this is constant; the key just makes that robust against a future per-session
    override). ``use`` is excluded — it gates entry to this path, not the build.

    #1829 S4: includes the credential ENV-VAR NAMES — **never the key VALUES** (the
    value is read from os.environ at build, never stored/fingerprinted; secret-safe
    by construction).

    #1870: includes ``retry_policy`` mapping (None → empty) so a changed
    retry_policy invalidates the cached Router.

    #4354: no longer includes credential env-var names — ``llm.router.credentials``
    is gone (see ``_deployments_for_model``)."""
    rp = rcfg.retry_policy or {}
    return (
        rcfg.num_retries,
        rcfg.cooldown_time,
        rcfg.allowed_fails,
        tuple(sorted((k, tuple(v)) for k, v in rcfg.fallbacks.items())),
        tuple(sorted(rp.items())),
    )


def _deployments_for_model(model: str, rcfg) -> list:
    """#4354: the litellm deployment for *model* — always a single plain deployment
    (no ``api_key``; litellm/the Router resolve credentials itself). #1829 S4's
    per-model credential-rotation branch (``llm.router.credentials``, one deployment
    per ``os.environ[api_key_env]``) was removed — the same rotation is expressible
    in litellm proxy's own ``config.yaml`` (multiple deployments under one
    ``model_name``), so reyn reading key values just to translate them into Router
    deployments duplicated litellm's own mechanism while being the last place reyn
    held a secret VALUE (rather than a name/reference) in memory."""
    return [{"model_name": model, "litellm_params": {"model": model}}]


def _bare_model_name(name: str) -> str:
    """Strip a ``provider/`` prefix, if any — the same transform the proxy-
    routing branch (``llm.py``'s main funnel, ``effective_model = model.
    split("/", 1)[1] if extra.get("api_base") and "/" in model else model``)
    applies to the PRIMARY model before it reaches litellm. #3833: model
    identity (and fallback config) is a property of the MODEL, not of the
    transport used to reach it (:func:`_single_deployment_router`'s own
    docstring already states this principle for `num_retries`/credentials —
    this is the same principle applied to fallback lookup)."""
    return name.split("/", 1)[1] if "/" in name else name


def _single_deployment_router(model: str, *, original_model: "str | None" = None):
    """#1829 S1→S4: return a per-running-loop-cached ``litellm.Router`` for
    *model*. Single deployment by default; when reyn.yaml ``llm.router.fallbacks``
    declares a chain for *model*, a **multi-deployment** Router (primary + each
    fallback target as its own deployment) wired with ``fallbacks`` +
    ``cooldown_time`` + ``allowed_fails``; and when ``llm.router.credentials``
    declares keys for a model, that model expands to one deployment per usable key
    (same model_name) so the Router rotates / fails over across keys (S4) — the
    #1835 fold (Router owns
    infra-exception retry w/ native Retry-After + cooldown + cross-model fallback;
    replay-compat probe-verified: a realized fallback still routes through the
    monkeypatched ``litellm.acompletion``). ``num_retries`` comes from the
    single-source resolved config (``_resolved_router_config`` — reyn.yaml
    authoritative, env fallback), NOT a module constant (no double source).

    Per-call routing params (api_base / provider / api_key / response_format / …)
    are passed through on the ``router.acompletion`` call (not baked into the
    deployment), so the underlying ``litellm.acompletion`` — which Router invokes
    internally — receives the SAME (model, messages, kwargs) as a direct call →
    LLMReplay/cost-recording-compatible. Cached per running loop so the cached
    Router is never reused across event loops (the #1762 binding class). The cache
    key is ``(model, config-fingerprint)`` (#1829 S3b F1) — a changed
    ``llm.router.*`` rebuilds rather than silently reusing a stale Router, so the
    cache is correct without assuming config is loop-uniform.

    #3833: *model* here is already the LITELLM-FACING name (the main funnel's
    ``effective_model`` — provider-prefix-stripped when routing through a
    proxy ``api_base``, unchanged for a direct-provider route). *original_model*
    is the pre-strip class/config name, passed through ONLY so
    ``llm.router.fallbacks`` config declared under that (possibly prefixed)
    spelling is still found: the fallback map's declared intent is a property
    of the MODEL, not of whether this particular call happens to route
    through a proxy — a config keyed ``"openai/gpt-4o-mini"`` must resolve
    the same fallback chain whether or not ``LITELLM_API_BASE`` is set.
    Fallback TARGETS are then normalised to match *model*'s own
    (stripped-or-not) convention, so the deployments this Router actually
    builds — and the ``fallbacks`` kwarg litellm's Router reads at runtime —
    name models the SAME way the primary does; a mismatch there would make
    the config resolve but the Router silently never use it (the config
    lookup and the runtime kwarg must agree — see the docstring's own
    caution against fixing only one).
    """
    # #4395/#4421: bare `import litellm as _ll` → read the confirmed
    # module. Reached only from an already-gated completion path
    # (`recorded_acompletion`'s own readiness check runs first), but "safe
    # by call order" is exactly the shape #4415/#4417 showed can go stale
    # silently — no bare import outside the chokepoint, structurally.
    # There is no fallback for a Router construction (the return value is
    # used immediately for a real completion), so a not-ready state raises
    # explicitly rather than proceeding with a broken reference.
    from reyn.llm.litellm_bootstrap import LitellmUnavailableError, is_litellm_ready
    if not is_litellm_ready():
        raise LitellmUnavailableError(
            "import litellm failed — see the reyn.llm.litellm_bootstrap "
            "warn-once log line for the underlying cause",
        )
    import sys
    _ll = sys.modules["litellm"]
    rcfg = _resolved_router_config()
    loop = asyncio.get_running_loop()
    per_loop = _ROUTERS_BY_LOOP.get(loop)
    if per_loop is None:
        per_loop = {}
        _ROUTERS_BY_LOOP[loop] = per_loop
    # #3833: include original_model — two different pre-strip class names
    # (e.g. "openai/gpt-4o" vs "azure/gpt-4o") can strip to the SAME bare
    # `model`, but their reyn.yaml fallback declarations may differ; caching
    # on the bare name alone could serve one class's Router to the other.
    cache_key = (model, original_model, _router_cache_fingerprint(rcfg))
    router = per_loop.get(cache_key)
    if router is None:
        # #3833: try the pre-strip (class/config) spelling FIRST — that is
        # how an operator's reyn.yaml/reyn.local.yaml naturally writes a
        # fallback chain — falling back to the already-bare `model` for a
        # config that already declares bare names.
        _raw_targets = None
        if original_model is not None:
            _raw_targets = rcfg.fallbacks.get(original_model)
        if not _raw_targets:
            _raw_targets = rcfg.fallbacks.get(model)
        _stripped_primary = original_model is not None and original_model != model
        fb_targets = list(dict.fromkeys(
            (_bare_model_name(t) if _stripped_primary else t)
            for t in (_raw_targets or [])
            if t and (_bare_model_name(t) if _stripped_primary else t) != model
        ))
        # model_list: the primary + each DISTINCT fallback target, each EXPANDED to
        # one deployment per usable credential (S4 rotation) or a single plain
        # deployment (S3b). Same model_name across a model's credential deployments
        # → the Router rotates / fails over across keys.
        model_list: list = []
        for m in [model, *fb_targets]:
            model_list.extend(_deployments_for_model(m, rcfg))
        kwargs: dict = {"model_list": model_list, "num_retries": rcfg.num_retries}
        if fb_targets:
            kwargs["fallbacks"] = [{model: fb_targets}]
        if rcfg.cooldown_time is not None:
            kwargs["cooldown_time"] = rcfg.cooldown_time
        if rcfg.allowed_fails is not None:
            kwargs["allowed_fails"] = rcfg.allowed_fails
        if rcfg.retry_policy:
            kwargs["retry_policy"] = _ll.RetryPolicy(**rcfg.retry_policy)
        router = _ll.Router(**kwargs)
        per_loop[cache_key] = router
    return router


def routing_for_spec(spec: "ModelSpec | None") -> dict | None:
    """#309: per-class litellm routing (api_base / custom_llm_provider) for a
    model class, or ``None`` to inherit the global ``proxy_kwargs()`` endpoint
    (backward-compat — existing single-endpoint configs are byte-identical).

    Enables simultaneous multi-provider use (e.g. router=light on a Gemini proxy
    + capable on Anthropic-direct):
      - ``api_base`` set → route to that endpoint; ``custom_llm_provider`` =
        ``spec.provider`` or ``"openai"`` (OpenAI-compatible proxy). No
        ``api_key`` here (#4347, mirrors ``proxy_kwargs()``'s own fix) —
        litellm resolves it itself from OPENAI_API_KEY (litellm standard —
        never a literal secret in config).
      - ``provider`` set, no ``api_base`` → DIRECT to that provider (no api_base
        override); litellm resolves the key from its standard env var
        (ANTHROPIC_API_KEY / GEMINI_API_KEY / …). This opts the class OUT of the
        global proxy.
      - neither → ``None`` → caller falls back to ``proxy_kwargs()``.
    """
    if spec is None:
        return None
    api_base = getattr(spec, "api_base", None)
    provider = getattr(spec, "provider", None)
    if api_base:
        return {
            "api_base": api_base,
            "custom_llm_provider": provider or "openai",
        }
    if provider:
        return {"custom_llm_provider": provider}
    return None


# #1190 cost-observability: the valid purpose (cost-attribution) buckets. Every
# recorded_acompletion call must tag one so /cost can break spend down by where
# the LLM call originated. ``dogfood`` covers test/trace sites (recorder=None).
LLM_PURPOSES: tuple[str, ...] = (
    "main", "compaction", "judge", "dogfood",
)

#: #1676 request-param redaction (kept, #3830-follow-up did not touch this —
#: see ``_redact_llm_request_params``). Was imported from the now-removed
#: ``reyn.llm.secret_scrub`` module; inlined here as its sole remaining
#: consumer.
_LLM_REQUEST_SECRET_HINTS: tuple[str, ...] = (
    "api_key", "api-key", "authorization", "secret", "token",
)


def _redact_llm_request_params(base_kwargs: dict, response_format: dict | None) -> dict:
    """#1669: build the non-message, non-tools LLM call params for the
    ``llm_request`` event, with secret-like values redacted.

    ``messages`` is never present (separate positional arg); ``tools`` is dropped
    (surfaced as ``tools_count``); ``response_format`` is added explicitly because
    it is applied inside ``_once`` rather than carried in ``base_kwargs``, so the
    event still reflects the actual outgoing param.
    """
    out: dict = {}
    for k, v in base_kwargs.items():
        if k in ("tools", "messages"):  # tools → count; messages never surfaced
            continue
        kl = k.lower()
        if any(hint in kl for hint in _LLM_REQUEST_SECRET_HINTS):
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    if response_format is not None and "response_format" not in out:
        out["response_format"] = response_format
    return out


def _emit_llm_request_error(
    model: str, purpose: str, exc: BaseException, base_kwargs: dict,
) -> None:
    """#1676: emit a P6 ``llm_request_error`` with the FULL provider error detail
    (status_code + whole message/body, NOT truncated — the owner's 405 root-cause
    signal) so an LLM-call failure is visible in the event tab. Same ambient
    EventLog (ContextVar) + ``model``/``purpose`` context as ``llm_request``
    (#1669). Wrapped so the audit emit can never mask the real exception (the
    caller re-raises regardless)."""
    try:
        from reyn.core.events.events import get_llm_request_event_log
        log = get_llm_request_event_log()
        if log is None:
            return
        detail: dict = {
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "status_code": getattr(exc, "status_code", None),
        }
        # litellm exceptions carry the provider body on ``.body`` (often the parsed
        # error dict) and/or ``.response`` (an httpx.Response). Capture BOTH, whole
        # (#3830 origin, scrub dropped #3830-follow-up: reyn never passes api_key
        # to litellm since #4348, so there is nothing of reyn's to scrub here —
        # litellm's own error text is already provider-scrubbed, #4343).
        body = getattr(exc, "body", None)
        if body is not None:
            detail["provider_body"] = body if isinstance(body, (dict, list)) else str(body)
        resp = getattr(exc, "response", None)
        if resp is not None:
            text = getattr(resp, "text", None)
            detail["provider_response"] = text if isinstance(text, str) else str(resp)
        log.emit(
            "llm_request_error",
            model=model,
            purpose=purpose,
            params=_redact_llm_request_params(base_kwargs, None),
            **detail,
        )
    except Exception:  # noqa: BLE001 — audit emit must never mask the real error
        pass


class ResponsesEndpointRequiredError(Exception):
    """#1678, delegated to litellm at #3288-follow-up (issue #3288 comment
    thread, 2026-07-26/27): litellm >= 1.89.3 auto-bridges eligible
    ``reasoning_effort + tools`` calls to the OpenAI/Azure ``/v1/responses``
    endpoint internally (``litellm.main.responses_api_bridge_check`` — reyn no
    longer rewrites the model string itself, see ``recorded_acompletion``).
    reyn can no longer tell whether a given call WAS bridged, so this error is
    raised on a 405 for a call shaped ``reasoning_effort + tools`` resolved to
    the OpenAI or Azure provider (see ``_may_need_responses_endpoint``) — the
    guidance ("this MAY need /v1/responses, but your endpoint doesn't serve
    it") is equally true whether litellm applied its own bridge or the
    operator's proxy simply doesn't support the endpoint. **Provider-scoped**
    (co-vet finding on PR #3331): litellm's bridge only ever fires for
    ``custom_llm_provider in ("openai", "azure")`` — a Gemini 405 is
    categorically unrelated to ``/v1/responses``, so an unscoped trigger would
    turn a decision-enabling error into a decision-MISLEADING one for every
    other provider. Kept deliberately (owner decision, #3288 comment thread):
    it is the safety net that makes delegating to litellm's narrower,
    upstream-maintained routing decision reversible — if litellm's bridge
    coverage misses a model that genuinely needs it, this surfaces as an
    ACTIONABLE 405 instead of a raw one."""


def _may_need_responses_endpoint(model: str) -> bool:
    """#3331 co-vet finding: does ``model`` resolve to a provider litellm's
    OWN ``/v1/responses`` auto-bridge (``litellm.main.responses_api_bridge_check``,
    delegated to at #3288-follow-up) ever applies to? Used ONLY to scope the
    ``ResponsesEndpointRequiredError`` 405 guidance — NOT to rewrite the model
    (reyn no longer does that; litellm decides bridging internally).

    litellm's own bridge check gates on ``custom_llm_provider in ("openai",
    "azure")`` (read directly from its source, ``litellm/main.py``) — so this
    mirrors that pair exactly, not reyn's own judgment about who "should"
    need it. Without this scope, the 405 guidance would fire for e.g. a
    Gemini call shaped ``tools + reasoning_effort`` that 405s for a reason
    entirely unrelated to ``/v1/responses`` (litellm never bridges Gemini),
    which is categorically false guidance, not merely imprecise.

    Reuses the SAME derivation this repo's #3325 fix (and the now-deleted
    ``_requires_responses_bridge``) used: ``litellm.get_llm_provider``,
    queried WITHOUT an explicit ``custom_llm_provider`` (``None``) — the same
    transport-independence discipline ``_streaming_capability`` documents.
    reyn's proxy routing (``proxy_kwargs()``) forces
    ``custom_llm_provider="openai"`` on the wire for ALL models when an
    operator proxy is configured, so deriving "is this OpenAI/Azure?" from
    that forced value would make every model look eligible — the exact
    over-wide failure #3325 fixed, reproduced one layer over. Capability (and
    provider identity) is a property of the MODEL, not of the transport used
    to reach it.

    Any lookup failure (unmapped model, litellm internal error, litellm not
    yet ready — #4395/#4421: no bare ``import litellm`` outside the
    chokepoint, so "not ready" is now one more lookup failure this
    function already tolerates) is caught and treated as "does not need
    it" — conservative in the direction that matters for a diagnostic
    (never claim a model needs an endpoint reyn can't confirm it resolves
    to)."""
    try:
        from reyn.llm.litellm_bootstrap import is_litellm_ready
        if not is_litellm_ready():
            return False
        import sys
        litellm = sys.modules["litellm"]

        _, provider, _, _ = litellm.get_llm_provider(model=model, custom_llm_provider=None)
        return provider in (
            litellm.LlmProviders.OPENAI.value,
            litellm.LlmProviders.AZURE.value,
        )
    except Exception:  # noqa: BLE001 — unknown provider → conservative "no"
        return False


def _emit_chat_cost_events(
    model: str, usage: "TokenUsage | None", chain_id: str | None = None,
    *,
    call_id: "str | None" = None,
    finish_reason: "str | None" = None,
) -> None:
    """#1683: emit the cost-tab's usage events for the chat path via the #1669
    ambient EventLog. The TUI cost tab reads ``llm_called`` (model) then accumulates
    tokens/cost on ``llm_response_received``, so emit BOTH (in that order). Minimal
    fields — the cost tab derives the label from the events file path, so no
    run_id is needed. None EventLog (no active session) → skip. Wrapped so an
    observability emit never breaks the LLM call.

    #3339: ``chain_id`` (the ambient turn key, see ``reyn.core.turn_scope``) is
    stamped on both events when a turn is in scope, so the audit trail can be
    re-grouped per turn — the same key ``turn_started`` / ``turn_completed``
    already carry. Omitted entirely when there is no turn: an unattributable
    call must read as "no turn", never as a turn it did not belong to.

    #4691 Phase 1 ①: ``call_id``/``finish_reason`` — the litellm
    ``ModelResponse``'s own ``id`` and ``choices[0].finish_reason`` — are
    stamped on ``llm_response_received`` ONLY (not ``llm_called``, which
    fires before the response exists; both events happen to be emitted
    from the same post-response call site today, but that is an
    implementation detail, not a reason to backdate response fields onto
    the pre-response event). This is the CALL-granularity key the outbox
    meta (router_loop.py) and the flowview tree (#4691 Phase B) both need
    to know which litellm call a given row belongs to — measured
    (architect, #4691): litellm's ``ModelResponse`` always carries ``id``
    for the ROUTER's own synchronous call path; the STREAMING path's own
    id-availability is unmeasured, tracked as this feature's own accept
    condition (a streamed turn's rows must still connect parent/child).
    Both ``None`` only when genuinely absent off the response — never a
    fabricated placeholder."""
    if usage is None:
        return
    try:
        from reyn.core.events.events import get_llm_request_event_log
        log = get_llm_request_event_log()
        if log is None:
            return
        # Strip the proxy provider-prefix for the pricing lookup (mirrors the
        # kernel's LLMCallRecorder), then emit model + tokens + cost_usd.
        _pricing_model = (
            model.split("/", 1)[1] if "/" in model and proxy_kwargs() else model
        )
        cost_usd, _snapshot = estimate_cost(_pricing_model, usage)
        turn_key = {"chain_id": chain_id} if chain_id is not None else {}
        log.emit("llm_called", model=model, **turn_key)
        log.emit(
            "llm_response_received",
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=cost_usd,
            # #3351: PROVENANCE of the figures on this same event — "provider"
            # (the provider reported them) / "estimated" (litellm.token_counter
            # filled them locally) / "unknown". Declared MANDATORY in
            # ``EVENT_AUDIT_REQUIREMENTS`` so the audit trail cannot carry the
            # numbers without saying where they came from, and so estimated
            # turns are findable AFTER THE FACT via ``reyn events`` (the numbers
            # are already grouped by ``chain_id``).
            usage_source=usage.source.value,
            # #4691 Phase 1 ①: see this function's own docstring.
            call_id=call_id,
            finish_reason=finish_reason,
            **turn_key,
        )
    except Exception:  # noqa: BLE001 — observability emit must never break the call
        pass


# ---------------------------------------------------------------------------
# #3288 ③a: capability-gated streaming (core LLM streaming loop)
# ---------------------------------------------------------------------------


def _streaming_capability(model: str, has_tools: bool) -> "bool | None":
    """What the CATALOG says this model can do — not whether a call streams.

    A litellm inline
    capability query, mirroring the existing ``litellm.supports_response_schema``
    precedent (``router_loop.py``'s structured-output precheck). NEVER a
    hardcoded provider/model-name check (owner design principle) — this is
    the ONLY place ③a decides whether a given call streams.

    Two capability axes, composed conservatively:

    - ``litellm.utils.supports_native_streaming``: can this model stream at
      all? False only for the handful of reasoning-only endpoints (e.g.
      ``o1-pro`` / ``gpt-5-pro``) that reject ``stream=True`` outright;
      ``None``/unset in litellm's model map is treated by litellm itself as
      an optimistic True default.
    - IF ``tools`` are attached to this call: additionally require
      ``litellm.supports_function_calling`` — the historical Gemini
      streaming+tools bug (litellm#21041, fixed upstream since) only bites
      the streaming+tools COMBINATION, so a tools-bearing call needs both
      axes to hold, not just plain-text streaming.

    Queried WITHOUT an explicit ``custom_llm_provider`` (``None`` → litellm
    infers the provider from the bare model name). This matters: reyn's proxy
    routing (``proxy_kwargs()``) forces ``custom_llm_provider="openai"`` on
    the wire for ALL models when an operator proxy is configured — passing
    that routing artifact into the capability query would misresolve e.g.
    ``gemini-2.5-flash`` as an unmapped "openai" model and always report
    "no capability" for the very providers this feature targets. Capability
    is a property of the MODEL, not of the transport used to reach it.

    **A model absent from litellm's catalog is UNKNOWN, not incapable.**
    ``supports_native_streaming`` collapses those two into one ``False``: for a
    model it finds, an unset ``supports_native_streaming`` field defaults to
    ``True``, but a model it cannot find at all falls into its ``except`` and
    returns ``False``. Reading that ``False`` as "cannot stream" makes catalog
    membership decide a capability, and the two have no relation — one is a
    property of the model, the other of how current the shipped table is.

    Reyn makes that especially load-bearing: ``reyn/__init__.py`` sets
    ``LITELLM_LOCAL_MODEL_COST_MAP`` by default to silence litellm's startup
    network fetch, so the catalog is the copy BUNDLED with the installed
    litellm. A model newer than that snapshot is therefore permanently absent,
    not intermittently — every model too new for the pinned table would have
    streamed only if the operator opted back into the remote fetch.

    Asking for a whole response is not the safe direction either. A provider
    that only ever streams (measured on a Codex-backed endpoint: SSE arrives
    even when ``stream`` is not requested) must then have its stream folded
    into one response by litellm's bridge — a translation nobody asked for,
    and where the reply was observed to be lost entirely. "Conservative" has to
    mean "do not assert what we did not measure", and an absent catalog row
    measures nothing.

    Returns ``None`` for "the catalog does not say" — a THIRD answer, not a
    ``False``. Capability is what the model can do; whether a given call
    streams is a policy question that reads this and decides. They were one
    function, so an absent catalog row silently became a policy outcome;
    :func:`_streaming_enabled` is where the decision lives now, and it is the
    only thing call sites ask.
    """
    try:
        # #4395/#4421: no bare `import litellm` outside the chokepoint —
        # "not ready" maps to the SAME `None` ("the catalog does not say")
        # this function already returns for an absent-from-catalog model;
        # both are "cannot confirm", not "confirmed no".
        from reyn.llm.litellm_bootstrap import is_litellm_ready
        if not is_litellm_ready():
            return None
        import sys
        litellm = sys.modules["litellm"]
        supports_native_streaming = litellm.utils.supports_native_streaming
        try:
            litellm.get_model_info(model=model, custom_llm_provider=None)
        except Exception:  # noqa: BLE001 — absent from the catalog, nothing more
            return None
        if not supports_native_streaming(model=model, custom_llm_provider=None):
            return False
        if has_tools and not litellm.supports_function_calling(
            model=model, custom_llm_provider=None,
        ):
            return False
        return True
    except Exception:  # noqa: BLE001 — could not ask; still not a "cannot"
        return None


def _streaming_enabled(
    model: str, has_tools: bool, override: "bool | None" = None,
) -> bool:
    """Whether THIS call streams — the policy decision (#3288 ③a).

    Reads :func:`_streaming_capability` and resolves its three answers:

    - ``False`` — the catalog states the model cannot stream natively (the
      reasoning-only endpoints that reject ``stream=True`` outright). Honoured:
      it is a real statement about the model.
    - ``True`` — stream.
    - ``None`` — the catalog does not say. Stream.

    ``None`` resolving to "stream" is the part that carries a reason. Absence
    from the catalog is a fact about the shipped table, not about the model,
    and reyn pins that table: ``reyn/__init__.py`` sets
    ``LITELLM_LOCAL_MODEL_COST_MAP`` by default to silence litellm's startup
    network fetch, so a model newer than the installed snapshot is absent on
    every run, permanently.

    Nor is "collect the whole response" the cautious direction. A provider that
    only ever streams (measured on a Codex-backed endpoint: SSE arrives with no
    ``stream`` in the request) then needs its stream folded into one response by
    litellm's bridge — a translation nobody asked for, and where the reply was
    observed to be dropped entirely. Declining to stream asserts just as much as
    streaming does; it only looks safer.

    ``override`` is the operator's answer, from a model class's ``stream:``
    field, and it WINS over the catalog in both directions. An operator can
    know something the shipped table does not — that is the ordinary case for
    a model too new for it, and refusing their answer would leave them arguing
    with a snapshot they cannot edit. A wrong override costs one provider
    error; deferring to a stale table cost a silently dropped reply.
    """
    if override is not None:
        return override
    capability = _streaming_capability(model, has_tools)
    return capability is not False


async def recorded_acompletion(
    *,
    model: str,
    messages: list,
    purpose: str,
    model_class: "str | None",
    model_class_ceiling: "str | None" = None,
    recorder: object | None = None,
    agent: str | None = None,
    response_format: dict | None = None,
    fallback_without_response_format: bool = False,
    extra_kwargs: dict | None = None,
    emit_cost_events: bool = False,  # #1683: chat path opts in (kernel emits via LLMCallRecorder)
    routing: dict | None = None,  # #309: per-class api_base/provider; None → global proxy_kwargs()
    on_content_delta: "Callable[[str], None] | None" = None,  # #3288 ③b: opt-in per-chunk content-delta callback (see _stream_and_reconstruct)
    stream_override: "bool | None" = None,  # a model class's ``stream:`` — operator policy, NOT a litellm kwarg
    prompt_cache_key: str | None = None,  # #4700: routing hint — see the call-kwargs block below for the full reasoning
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

    ``model_class`` / ``model_class_ceiling`` (#4206 T1 — ②bounding axis,
    ``model``): REQUIRED (no default) so a caller must explicitly say
    whether THIS call is subject to class-based ceiling enforcement —
    ``model_class=None`` opts a call OUT (e.g. compaction, which follows
    ``Session.model`` directly and never goes through ``class_for_purpose``,
    #3785; or the dogfood/eval auxiliary surfaces, which use a fixed literal
    model string, not a class). A caller that forgets to pass it gets a
    ``TypeError`` at the call site, not a silently-unenforced bound — the
    same "no default that means the same thing as forgetting" discipline
    #4271 applied to ``timeout``. The check runs HERE, inside the #1190
    chokepoint, deliberately — not at each call site — so a NEW call site
    added later cannot forget to enforce it (#3903①'s
    reject-not-clamp shape, moved to the one place enforcement cannot be
    bypassed by construction, not by convention). ``model_class_ceiling``
    defaults to ``None`` (unbounded) — the compat default every other
    bounding axis in reyn uses; a caller only supplies a real ceiling when
    one is actually configured. When ``model_class`` is over ceiling, this
    function raises :class:`~reyn.llm.model_resolver.
    ModelClassExceedsCeilingError` BEFORE ``litellm.acompletion`` is ever
    called — no partial call, no charge, no record.

    ``on_content_delta`` (#3288 ③b): an OPT-IN, per-call callback invoked
    SYNCHRONOUSLY with each non-empty content-delta string as ``_stream_and_reconstruct``
    (③a) drains the chunk stream — never invoked on the whole-collect (non-streaming)
    path, so it is capability-gated by construction (see ``_streaming_enabled``: no
    capability ⇒ no streaming ⇒ this callback never fires). ``None`` (every caller
    except ``RouterLoop``'s primary reply call) is byte-identical to today. A raising
    callback is caught and logged — a broken display-event emit must never break the
    LLM call it is merely narrating. The reconstructed WHOLE response (below) is
    unaffected either way — this is purely an additional notification channel, not a
    change to what this function returns or records.

    ``prompt_cache_key`` (#4700): OPTIONAL — sent to litellm only when set
    (``None``, the default, is byte-identical to before #4700). See the
    inline comment at the ``base_kwargs`` assembly below for the full
    session-unit-granularity measurement and reasoning; whitelisted via
    ``allowed_openai_params`` the same way ``reasoning_effort`` already is,
    so it never raises regardless of proxy vs. direct-provider routing.
    """
    # #4206 T1: enforce BEFORE anything else — before ensure_litellm_ready(),
    # before litellm is even imported, so a rejected call touches litellm in
    # NO way (falsify target: litellm.acompletion is provably never invoked).
    if model_class is not None and model_class_ceiling is not None:
        from reyn.llm.model_resolver import (
            ModelClassExceedsCeilingError,
            model_class_exceeds_ceiling,
        )

        if model_class_exceeds_ceiling(model_class, model_class_ceiling):
            raise ModelClassExceedsCeilingError(model_class, model_class_ceiling, purpose)

    # perf: litellm's own import is ~1.5s and is kept off the chat startup
    # path (input box renders before any LLM use). ``ensure_litellm_ready``
    # is the single chokepoint that applies the #2929 console-log routing +
    # ``suppress_debug_info`` the first time ANY call site touches litellm.
    # #4395 PR-1: use the module it RETURNS rather than a separate bare
    # ``import litellm`` — that used to independently re-attempt (and
    # re-fail) litellm's own slow, unbounded import on every completion
    # call while it kept failing (a failed import isn't cached by Python;
    # only this chokepoint's own attempt was). No fallback exists here —
    # a real completion genuinely needs litellm — so failure surfaces as
    # an explicit, legible error instead of whatever incidental exception
    # a redundant re-attempt would have produced. Retriable on the NEXT
    # call (`ensure_litellm_ready()`'s own docstring: a failure is not
    # permanently cached — an environmental cause can clear, and this
    # path has no fallback to fall permanently back to).
    #
    # #4395: called via ``asyncio.to_thread`` rather than awaited/called
    # directly in-loop — this function is ``async def`` and a synchronous
    # ``ensure_litellm_ready()`` call here blocks the WHOLE event loop
    # (animation + input, not just this coroutine) for as long as the
    # underlying import takes, which is exactly the owner-reported freeze
    # (lead-coder correction, #4395: PR-1 must land this call site in an
    # already-awaitable form so PR-2's dedicated-thread/cooldown mechanism
    # slots in behind the same await point — writing it as an in-loop
    # blocking call here would force PR-2 to rewrite every caller instead
    # of just the chokepoint).
    #
    # ``ignore_cooldown=True`` (#4395 PR-2 follow-up, lead-coder review):
    # the axis② cooldown protects callers WITH a safe fallback from
    # repeatedly re-attempting a broken import — this call site has NO
    # fallback and was already a "must wait" site before axis② existed.
    # Without this flag, a single earlier failure would hard-fail EVERY
    # completion for the next 60s without even attempting one, silently
    # dropping any transient failure that would have cleared and
    # succeeded on retry. See ``ensure_litellm_ready()``'s own docstring
    # for the full reasoning.
    litellm = await asyncio.to_thread(ensure_litellm_ready, ignore_cooldown=True)
    if litellm is None:
        raise LitellmUnavailableError(
            "import litellm failed — see the reyn.llm.litellm_bootstrap "
            "warn-once log line for the underlying cause",
        )

    # #1652/②: canonical litellm mechanism for reasoning continuity across tool
    # turns — when a thinking-enabled request carries an assistant turn whose
    # thinking_blocks are absent, litellm drops the `thinking` param for that
    # turn instead of erroring. Global, idempotent; the litellm-native handling
    # (NOT a Reyn workaround) that lets native reasoning re-attach round-trip.
    litellm.modify_params = True

    # #1190 stage (iii): typo guard — a purpose outside the known set would
    # silently land spend in an unattributed bucket in /cost.
    if purpose not in LLM_PURPOSES:
        raise ValueError(
            f"recorded_acompletion: unknown purpose {purpose!r}; "
            f"must be one of {LLM_PURPOSES}"
        )

    # #2287 follow-up: repair the assistant.tool_calls ↔ role=tool pairing on the FINAL assembled
    # wire list, at the single provider chokepoint — so no split pair reaches the provider as a 400
    # from ANY source (compaction/decompose elide, rewind mid-cycle, interrupt, over-budget group).
    # Full-list (not per-segment): an intact pair split only across a segment boundary (call in head,
    # its real result in tail, a bridge/summary between) is left untouched — never duplicate-synth'd.
    from reyn.llm.wire_format import repair_tool_call_pairing  # noqa: PLC0415 — local, no cycle
    messages = repair_tool_call_pairing(messages)

    # #309: per-class routing (api_base/provider) wins; None → global proxy_kwargs().
    extra = routing if routing is not None else proxy_kwargs()

    # #3905: the #2708 P3.2b credential pre-check (_PROVIDER_ENV_VARS + a typed
    # MissingCredentialsError) was removed. It could only ever cover a fixed
    # enumeration of providers — owner ruling: an unnecessary hardcode reyn had
    # no business maintaining, since litellm ALREADY raises a typed exception
    # naming the provider AND the exact env var to set (verified: neither
    # provider name nor variable name needed re-deriving; measured directly
    # that litellm's own message for openai/anthropic missing-key cases already
    # names the specific env var). litellm's own exception now propagates
    # unmodified — no network call was ever saved by the old pre-check either
    # (litellm validates before touching the network on the providers
    # measured), so nothing is lost by letting litellm raise it.
    # Strip the provider prefix ONLY when routing to an api_base endpoint
    # (OpenAI-compatible proxy expects a bare model + custom_llm_provider). A
    # direct-provider route (provider set, no api_base) keeps the prefix so
    # litellm resolves the provider from it.
    effective_model = (
        model.split("/", 1)[1] if extra.get("api_base") and "/" in model else model
    )
    base_kwargs = dict(extra_kwargs or {})
    base_kwargs.update(extra)  # Reyn routing/proxy kwargs win over caller-supplied ones

    # #1650: when an operator sets ``reasoning_effort`` on a model, the proxy
    # path forces ``custom_llm_provider=openai`` (proxy_kwargs), under which
    # litellm validates params against OpenAI and REJECTS ``reasoning_effort``
    # as unsupported for a gemini model name BEFORE forwarding to the proxy
    # (UnsupportedParamsError). Whitelisting it via ``allowed_openai_params``
    # makes litellm forward it to the proxy, which maps it to the provider's
    # native thinking budget. Verified live (#1650 proxy smoke: reasoning_tokens
    # 0 → ~420). Harmless on the direct path where reasoning_effort is already
    # a native gemini param. Single chokepoint = covers call_llm + call_llm_tools.
    if "reasoning_effort" in base_kwargs:
        _allowed = list(base_kwargs.get("allowed_openai_params") or [])
        if "reasoning_effort" not in _allowed:
            _allowed.append("reasoning_effort")
        base_kwargs["allowed_openai_params"] = _allowed

    # #4700: send OpenAI's ``prompt_cache_key`` — the public spec (developers.
    # openai.com/api/docs/guides/prompt-caching) states requests are routed to
    # a machine keyed FIRST on this value, prefix-hash only as a secondary
    # signal; reyn sent nothing, so every call fell back to prefix-hash-only
    # routing. Granularity = SESSION (not agent), decided against real
    # measurement, not a default:
    #   reyn-self, 2026-08-14, cached_tokens breakdown across 84 hits —
    #     A (shared head, e.g. system prompt + tool schemas): 9,728 tokens,
    #       hit 43 times across 10 DIFFERENT files (session-crossing reuse)
    #     B (session-specific tail): 163,328-280,064 tokens, each hit only
    #       WITHIN its own single file (session-local reuse)
    #   B is worth ~20x more than A by volume in this sample — a session-unit
    #   key protects B (the 20x side) at the cost of A's session-crossing
    #   reuse (~4% of the total, 9,728 / ~240,000) — the ratio is THIS
    #   day/agent's value; a short-conversation agent (small B, A dominates
    #   proportionally) could invert it.
    #   Selection reason: OpenAI's own spec text — "use more keys for
    #   higher-volume workloads" — points AT finer-grained keys as volume
    #   grows, and per-session is the natural finer unit above per-agent.
    #   Unmeasured, carried as assumptions: (1) the provider's cache capacity
    #   is ORGANIZATION-wide, not per-key (read from the spec's prose, not
    #   measured); (2) parallel sessions were never run for real — this is
    #   extrapolated from a single sequential session log; (3) what the
    #   shared 9,728-token head actually IS (system prompt + tool schemas is
    #   the working guess, matched against the session's own measured
    #   minimum prompt size of 14,540 — not confirmed against a payload dump).
    # Whitelisted via ``allowed_openai_params`` for the SAME reason as
    # ``reasoning_effort`` above (proxy forces custom_llm_provider="openai";
    # a direct, non-proxy provider route, #309, would otherwise raise
    # UnsupportedParamsError — verified live against both shapes with the
    # installed litellm: bare-model+custom_llm_provider="openai" already
    # passes unwhitelisted, gemini/anthropic direct-provider raises without
    # the whitelist and passes with it).
    if prompt_cache_key:
        base_kwargs["prompt_cache_key"] = prompt_cache_key
        _allowed = list(base_kwargs.get("allowed_openai_params") or [])
        if "prompt_cache_key" not in _allowed:
            _allowed.append("prompt_cache_key")
        base_kwargs["allowed_openai_params"] = _allowed

    # #1678 → delegated to litellm at #3288-follow-up (issue #3288 comment
    # thread, owner-approved 2026-07-26/27): ``reasoning_effort`` + ``tools``
    # together are only valid on /v1/responses for SOME reasoning models
    # (owner-confirmed gpt-5.4 405 repro). reyn used to rewrite the model
    # string to a ``responses/`` bridge marker itself (#1678) so litellm would
    # route to /v1/responses while still returning a chat-completions shape.
    # That manual bridge is now REDUNDANT: litellm >= 1.89.3 ships its own
    # ``responses_api_bridge_check`` (upstream PR BerriAI/litellm#23577,
    # merged 2026-03-13 — before #1678 was even filed), entered from inside
    # ``litellm.acompletion()`` itself, requiring NO ``responses/`` prefix
    # from the caller. Investigation (issue #3288 comment thread) found
    # reyn's own provider-allowlist bridge was actually the WRONG direction
    # of "safe": it fired for every openai/azure reasoning model (incl. ones
    # nobody verified need it), which is exactly how the #3288 default-config
    # streaming regression happened for Gemini before the #3325 provider gate
    # — a bridge that is too WIDE silently breaks streaming for models that
    # didn't need it. litellm's own narrower, upstream-maintained routing
    # (read from its source, ``litellm/main.py::responses_api_bridge_check``:
    # ``custom_llm_provider in ("openai", "azure") AND is_gpt_5_model(model)
    # AND reasoning_effort is not None AND (reasoning_summary OR
    # (is_gpt_5_4_plus_model(model) AND tools))``, plus a separate
    # ``mode == "responses"`` trigger for Responses-only models like
    # ``o1-pro`` — NOT merely "gpt-5.4-family", which would drop the
    # ``gpt-5``/``gpt-5.1`` + explicit ``reasoning_summary`` path) is
    # strictly better: reyn passes the bare resolved model straight to
    # ``litellm.acompletion`` and litellm decides internally.
    #
    # This no longer gates a rewrite (reyn does not know whether litellm
    # applied its own bridge) — it ONLY gates the decision-enabling
    # ``ResponsesEndpointRequiredError`` below, by call SHAPE (tools +
    # reasoning_effort) AND provider (co-vet finding on PR #3331: litellm's
    # own bridge only ever fires for openai/azure — an unscoped Gemini 405
    # would get the SAME "/v1/responses" guidance despite litellm never
    # bridging Gemini, which is categorically false, not merely imprecise).
    # See ``_may_need_responses_endpoint``'s docstring.
    _needs_responses_endpoint = bool(
        base_kwargs.get("tools")
        and base_kwargs.get("reasoning_effort")
        and _may_need_responses_endpoint(effective_model)
    )

    # #1669: emit a P6 ``llm_request`` event (TUI-observable) carrying the
    # non-message call params, ONCE here — before the ``_once`` response_format
    # retry loop, so a fallback retry does not double-emit. Ambient EventLog via
    # ContextVar (set by the session / kernel runtime); None → skip, mirroring the
    # ``recorder=None`` graceful path. ``messages`` is excluded by construction
    # (a separate positional arg, never in base_kwargs); ``tools`` → count;
    # secret-like fields redacted. Never let an audit emit break the LLM call.
    try:
        from reyn.core.events.events import get_llm_request_event_log
        _llm_event_log = get_llm_request_event_log()
        if _llm_event_log is not None:
            _llm_event_log.emit(
                "llm_request",
                model=effective_model,
                purpose=purpose,
                tools_count=len(base_kwargs.get("tools") or []),
                params=_redact_llm_request_params(base_kwargs, response_format),
            )
    except Exception:  # noqa: BLE001
        pass

    async def _stream_and_reconstruct(model: str, msgs: list, call_kwargs: dict) -> object:
        """#3288 ③a streaming loop: call litellm with ``stream=True``, drain
        the async chunk stream, and reconstruct the SAME raw-response shape
        ``litellm.acompletion(..., stream=False)`` would have returned — so
        this chokepoint's callers (``msg = response.choices[0].message``,
        ``_extract_usage(response)`` below) see NO behavioral difference
        (③a is an internal optimization, not a caller-visible change; ③b/c/d
        are later phases that change what callers see). Nested INSIDE
        ``recorded_acompletion`` (not module-level) so its
        ``litellm.acompletion`` call stays within the #1190 AST-guarded
        chokepoint span — no second completion call-site.

        Reconstruction uses litellm's OWN ``stream_chunk_builder`` (content
        deltas concatenated; tool_call argument deltas accumulated PER
        INDEX, the documented shape multi-argument tool calls arrive in;
        usage summed across chunks) rather than a hand-rolled accumulator —
        litellm's tested, canonical stream-to-response reconstruction,
        reused instead of duplicated.

        ★usage/cost single-emission (#3288 ③a architect gate): this
        function returns ONE reconstructed response with ONE ``.usage``;
        the enclosing ``recorded_acompletion`` calls
        ``recorder.record_llm(...)`` exactly once against it below,
        IDENTICAL to the whole-collect path — usage is summed HERE, across
        chunks, never emitted per-chunk. ``budget.py`` (the cost band,
        ``record_llm`` at budget.py:1135) is untouched by ③a.

        ★``stream_options={"include_usage": True}`` is set UNCONDITIONALLY
        (#3348): it is what makes the provider's OWN token counts reach this
        function at all. Without it litellm's ``CustomStreamWrapper`` never
        yields the usage-bearing final chunk, ``stream_chunk_builder`` has no
        provider figure to sum, and it falls back to
        ``litellm.token_counter`` — a LOCAL ESTIMATE that then flows into
        ``recorder.record_llm`` / ``/cost`` / budget caps as if it were the
        provider's number (measured on a live Gemini call: 13 recorded vs 7
        actual, +86%). Estimated spend is not spend.

        This is deliberately NOT gated on a supported-params query, which is
        what #3348 removed. The flag is consumed CLIENT-SIDE by the stream
        wrapper (``CustomStreamWrapper.check_send_stream_usage``), so it works
        for providers whose wire protocol has no such field. Two SEPARATE
        properties of litellm's param layer make passing it unconditionally
        safe — neither implies the other, and both are pinned by Tier 1 tests
        in ``tests/llm/test_streaming_usage_provider_supplied_3348.py``:
        (1) it does not RAISE for a provider that rejects the param —
        ``litellm/utils.py`` skips ``stream_options`` when pruning unsupported
        params (``if k == "user" or k == "stream_options" or k == "stream":
        continue``), so no ``UnsupportedParamsError`` even under
        ``drop_params=False``; and (2) it does not LEAK to the wire — the
        param-mapping layer forwards it only to the OpenAI-compatible
        providers that genuinely take it (measured across 8 providers ×
        ``drop_params`` ∈ {False, True}: reaches openai + groq only; dropped
        for anthropic / gemini / cohere / mistral / bedrock / ollama).
        Property (1) holds by way of a single ``continue`` line in litellm, so
        it is structural in THIS litellm version, not for all time — hence the
        witness. The supported-params list
        describes what a provider ACCEPTS ON THE WIRE — Gemini and Anthropic
        do not list it — which is the wrong question for a client-side flag,
        and gating on it made reyn's token accounting silently provider-
        dependent (exact on Anthropic, estimated on Gemini). One path, no
        provider branching.

        #3288 ③b: ``on_content_delta`` (the enclosing ``recorded_acompletion``'s
        parameter, closed over here) is invoked ONCE PER CHUNK that carries a
        non-empty ``delta.content``, with that chunk's raw text — BEFORE the
        chunk is appended to ``chunks`` below, so it fires against REAL,
        individually-drained chunks (never a synthesized/batched replay of the
        reconstructed whole). A callback failure is caught and logged, never
        allowed to abort the stream — the callback narrates the call, it does
        not gate it. tool_call-only chunks (no ``content``) are silently
        skipped — ③b carries TEXT deltas only; tool-call argument streaming is
        out of scope (unchanged: tool_calls are read from the reconstructed
        whole response, same as ③a).
        """
        stream_kwargs = dict(call_kwargs)
        stream_kwargs.pop("stream", None)
        stream_kwargs.pop("stream_options", None)
        stream_kwargs["stream"] = True
        # #3348: unconditional — see the docstring. Gating this on the
        # provider's supported-params list is what made reyn record a LOCAL
        # ESTIMATE instead of the provider's own token counts on every
        # streamed Gemini call.
        stream_kwargs["stream_options"] = {"include_usage": True}

        chunk_stream = await litellm.acompletion(model=model, messages=msgs, **stream_kwargs)
        if not hasattr(chunk_stream, "__aiter__"):
            # Defensive: the callee did not honor stream=True (returned an
            # already-complete response synchronously instead of an async
            # chunk iterator) — accept it as the final response rather than
            # crashing. Real litellm ALWAYS returns an async-iterable
            # (CustomStreamWrapper) here, so this never fires against
            # production litellm — but it DOES fire routinely in this
            # repo's own tests: many pre-③a test doubles stub
            # ``litellm.acompletion`` with a plain async function that
            # returns a flat response regardless of the ``stream`` kwarg
            # (they never had to branch on it before ③a, since streaming
            # was previously hardcoded off). Such a test silently exercises
            # this fallback (whole-collect), NOT the streaming
            # reconstruction path below, even though the call requested
            # ``stream=True`` — a test that wants to prove IT specifically
            # exercised the streaming reconstruction must witness real
            # chunk consumption (e.g. a counter incremented while iterating
            # the async generator), not just that ``stream=True`` was
            # passed in the call kwargs.
            return _stamp_usage_source(chunk_stream, UsageSource.PROVIDER)
        chunks = []
        _delta_fired = False
        async for chunk in chunk_stream:
            chunks.append(chunk)
            if on_content_delta is not None:
                _delta_text = None
                try:
                    _delta_text = chunk.choices[0].delta.content
                except Exception:  # noqa: BLE001 — malformed/empty chunk shape
                    _delta_text = None
                if _delta_text:
                    _delta_fired = True
                    try:
                        on_content_delta(_delta_text)
                    except Exception:  # noqa: BLE001 — a display-event emit must never break the LLM call
                        logger.exception(
                            "recorded_acompletion: on_content_delta callback raised; "
                            "continuing the stream"
                        )
        # #3288 ③b co-vet fix: a silent functional-dead-mode guard. If the
        # provider streamed at least one chunk AND a callback was supplied,
        # but NOT ONE chunk ever exposed a non-empty delta.content (e.g. a
        # provider whose chunk shape this parsing doesn't match), every turn
        # would silently produce ZERO delta notifications forever — L9 still
        # surfaces the final text via the reconstructed whole response below,
        # so nothing user-visible breaks and nobody would ever notice.
        # ONE debug log per STREAM (not per chunk, which would be noise) is
        # cheap and makes that silent mode observable.
        if on_content_delta is not None and chunks and not _delta_fired:
            logger.debug(
                "recorded_acompletion: streamed %d chunk(s) but on_content_delta "
                "never fired — the provider's chunk shape may not expose "
                "delta.content the way this parsing expects",
                len(chunks),
            )
        # #3351: decide provenance from the RAW chunks, BEFORE reconstruction
        # collapses "the provider reported nothing" into a plausible-looking
        # int. This is the one place in the process where the two origins are
        # still distinguishable.
        _source = (
            UsageSource.PROVIDER if _provider_reported_usage(chunks) else UsageSource.ESTIMATED
        )
        if _source is UsageSource.ESTIMATED:
            logger.debug(
                "usage provenance: no provider usage on %d streamed chunk(s) for model=%s "
                "— litellm.token_counter will fill the counts (recorded as ESTIMATED)",
                len(chunks), model,
            )
        reconstructed = litellm.stream_chunk_builder(chunks, messages=msgs)
        if reconstructed is None:
            # No chunks at all (degenerate empty stream) — degrade to the
            # whole-collect call rather than surface None to a caller that
            # expects a message-bearing response object. That call returns the
            # provider's own usage payload, so provenance is PROVIDER, not the
            # ESTIMATED verdict the (empty) chunk list produced above.
            return _stamp_usage_source(
                await litellm.acompletion(model=model, messages=msgs, **call_kwargs),
                UsageSource.PROVIDER,
            )
        return _stamp_usage_source(reconstructed, _source)

    async def _once(rf: dict | None) -> object:
        call_kwargs = dict(base_kwargs)
        if rf is not None:
            call_kwargs["response_format"] = rf
        if _use_llm_router():
            # #1829: route through a litellm.Router (gated OFF by default → this
            # branch is inert in production). Router.acompletion invokes
            # litellm.acompletion internally (replay-compat verified, incl. a
            # realized fallback), so the LLMReplay monkeypatch + this
            # cost-recording chokepoint both still apply.
            # S3b single-source num_retries: the Router's retry count comes from
            # the resolved config (baked at construction), so STRIP the per-call
            # num_retries (the callsite's max_retries) — else it would override the
            # config-set value (probe: per-call wins). Config is the one source.
            router_kwargs = {k: v for k, v in call_kwargs.items() if k != "num_retries"}
            # #3351: a non-streamed completion returns the PROVIDER's own usage
            # payload — litellm's only token_counter fills live in the streaming
            # reconstruction (``streaming_chunk_builder_utils.calculate_usage``)
            # and in the legacy ``text_completion`` helper, neither of which is
            # on this path (read from litellm's source, not assumed).
            return _stamp_usage_source(
                await _single_deployment_router(
                    effective_model, original_model=model,
                ).acompletion(
                    model=effective_model, messages=messages, **router_kwargs
                ),
                UsageSource.PROVIDER,
            )
        # #3288 ③a: capability-gated streaming loop, INSIDE the single #1190
        # funnel (the two ``litellm.acompletion`` call sites remain exactly
        # this one and the Router branch above — no second completion
        # call-site was added). The decision is ``_streaming_enabled`` — a
        # policy over a litellm capability query, never a hardcoded provider
        # check. A capability the catalog does not state is NOT read as
        # "cannot": see ``_streaming_enabled`` for why whole-collect is not
        # the cautious direction it looks like.
        _capable = _streaming_enabled(
            effective_model,
            has_tools=bool(call_kwargs.get("tools")),
            override=stream_override,
        )
        # Permanent, one-line-per-call debug signal for the gate DECISION itself
        # (not just "streamed but the callback never fired" — the #3288 follow-up
        # gap: the gate silently deciding NOT to stream had no observable trace at
        # all, which is why the default-config dark-streaming bug needed a live
        # diagnostic run + code trace to find).
        logger.debug(
            "streaming gate: model=%s has_tools=%s capable=%s",
            effective_model, bool(call_kwargs.get("tools")), _capable,
        )
        if _capable:
            # Provenance is stamped INSIDE (only the streaming path can produce
            # a token_counter estimate — see ``_provider_reported_usage``).
            return await _stream_and_reconstruct(effective_model, messages, call_kwargs)
        return _stamp_usage_source(
            await litellm.acompletion(model=effective_model, messages=messages, **call_kwargs),
            UsageSource.PROVIDER,
        )

    # response_format fallback (predates #1212): on a provider that rejects
    # response_format, retry once without it. Used by the json-mode path
    # (call_llm passes fallback_without_response_format=True). The #1212 op-loop
    # uses tools-only op-turns + a separate json transition (ADR-0035 D2
    # separate-decide) and never combines tools+response_format, so the
    # per-(model, call-shape) combine-degrade cache (D5) was superseded and
    # pruned (#1226, user GO).
    # #1676: capture an LLM-call failure as a P6 ``llm_request_error`` (full
    # provider detail incl status_code + whole body) at this single chokepoint,
    # then RE-RAISE (never swallow). Wraps the response_format-fallback retry so a
    # final failure (no fallback, or the fallback also failed) emits exactly once.
    try:
        try:
            response = await _once(response_format)
        except Exception:
            if response_format is not None and fallback_without_response_format:
                response = await _once(None)
            else:
                raise
    except Exception as exc:
        # #3830 origin, scrub dropped #3830-follow-up: reyn never passes
        # api_key to litellm since #4348, so there was nothing of reyn's
        # left for this boundary to scrub — litellm's own error text is
        # already provider-scrubbed (#4343).
        _emit_llm_request_error(effective_model, purpose, exc, base_kwargs)
        # #1678, delegated to litellm at #3288-follow-up: this call shape
        # (reasoning_effort + tools, resolved to the openai/azure provider —
        # see the comment above ``_needs_responses_endpoint`` and
        # ``_may_need_responses_endpoint``'s docstring) MAY need
        # /v1/responses — litellm decides internally now, so reyn can no
        # longer tell whether IT applied a bridge. On a 405 for this shape,
        # turn the raw dead-end into a decision-enabling error naming BOTH
        # remedies (the raw 405 detail is already captured in the #1676
        # llm_request_error event above) — the guidance is equally true
        # whether litellm's own bridge fired or the proxy just doesn't serve
        # /v1/responses. A 405 on a call NOT shaped this way, or resolved to
        # a provider litellm never bridges (e.g. Gemini), is unaffected.
        if _needs_responses_endpoint and getattr(exc, "status_code", None) == 405:
            raise ResponsesEndpointRequiredError(
                f"This call combines reasoning_effort + tools on model {model!r}, "
                "which MAY require the OpenAI/Azure /v1/responses endpoint — but "
                "the configured endpoint/proxy does not serve /v1/responses "
                "(HTTP 405). Options: (1) set reasoning_effort to none / unset it "
                "for this agent, OR (2) enable the /v1/responses endpoint on your "
                "proxy."
            ) from exc
        raise

    usage = _extract_usage(response)
    # #1829 S3b (cost-records-actual-model): when routing through the Router, a
    # FALLBACK may have served the call with a different model than requested —
    # attribute cost to the model that ACTUALLY ran (``response.model``), not the
    # requested one. Gated on router-ON + a genuine difference, so the OFF path
    # (and router-ON without a realized fallback) records ``effective_model``
    # exactly as before (byte-identical).
    _cost_model = effective_model
    if _use_llm_router():
        _actual = getattr(response, "model", None)
        if isinstance(_actual, str) and _actual and _actual != effective_model:
            _cost_model = _actual
    # #3339: the turn this call belongs to, read from the ambient turn scope
    # (set once per turn at the session's router-loop seam). None outside any
    # turn — passed through as None so the cost path files the call under no
    # turn rather than the most recent one.
    _chain_id = get_active_turn_chain_id()
    if recorder is not None and usage is not None:
        recorder.record_llm(
            model=_cost_model, agent=agent, usage=usage, purpose=purpose,
            chain_id=_chain_id,
        )
    # #1683: the interactive chat path records cost to the in-memory recorder
    # (→ header) but emits NO usage event, so the TUI cost tab (which reads
    # `llm_called` + accumulates on `llm_response_received` from the events log)
    # stays empty. Opt-in callers (the chat router) emit BOTH events here via the
    # #1669 ambient EventLog. The kernel/phase path leaves this False — it emits
    # these events via LLMCallRecorder, so emitting here too would double-count.
    # (Interim: a future cleanup could centralize the kernel's emission into this
    # chokepoint and drop the flag — out of scope here.)
    if emit_cost_events:
        # #4691 Phase 1 ①: measured directly off the response, never
        # invented — a provider that omits either field means None, not a
        # minted placeholder (see _emit_chat_cost_events's own docstring).
        _call_id = getattr(response, "id", None)
        _finish_reason = None
        _choices = getattr(response, "choices", None)
        if _choices:
            _finish_reason = getattr(_choices[0], "finish_reason", None)
        _emit_chat_cost_events(
            _cost_model, usage, _chain_id,
            call_id=_call_id, finish_reason=_finish_reason,
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


async def call_llm_tools(
    *,
    model: "Union[str, ModelSpec]",
    messages: list[dict],            # OpenAI-format messages (role/content/tool_calls/tool_call_id)
    tools: list[dict],               # OpenAI-format tools array
    tool_choice: str = "auto",       # "auto" | "required" | "none" (note: "none" not Gemini-safe)
    timeout: float | None = None,
    max_retries: int = 1,
    prompt_cache_enabled: bool = True,
    budget: "BudgetTracker | None" = None,
    budget_agent: str | None = None,
    purpose: str = "main",  # #1190 cost-attribution bucket (chat router = main)
    trace_caller: str | None = None,
    event_log: "EventLog | None" = None,
    emit_cost_events: bool = False,  # #1683: forwarded to recorded_acompletion (chat opts in)
    response_format: "dict | None" = None,  # 0062: RouterLoop's separate no-tools structured-answer turn ONLY — every op-loop tool-decision call leaves this None (ADR-0035 D2 separate-decide preserved: never combined with a non-empty `tools`)
    on_content_delta: "Callable[[str], None] | None" = None,  # #3288 ③b: forwarded to recorded_acompletion — see its docstring
    model_class: "str | None" = None,  # #4206 T1: forwarded to recorded_acompletion — see its docstring (None = not subject to ceiling enforcement)
    model_class_ceiling: "str | None" = None,  # #4206 T1: the caller's effective ceiling, if any (None = unbounded)
    prompt_cache_key: str | None = None,  # #4700: forwarded to recorded_acompletion — see its docstring for the full reasoning
) -> LLMToolCallResult:
    """Tool-use variant of call_llm. Returns raw assistant message.

    Forces gemini-safe settings:
      - thinking disabled (Gemini #17949 multi-turn parallel + thinking bug)

    Streaming (#3288 ③a) is decided per-call by ``recorded_acompletion`` via
    a capability-informed policy (see ``_streaming_enabled``) — NOT forced off
    here. The historical Gemini streaming+tools bug (litellm#21041) is
    absorbed by that capability check, not by this function.

    budget: optional BudgetTracker. When provided, check_pre_llm is called
      before the LLM call (raises BudgetExceeded if refused) and record_llm
      is called after a successful call. budget=None skips all tracking.
    budget_agent: agent name passed to budget.check_pre_llm / record_llm.

    ``response_format`` (0062): passed straight to ``recorded_acompletion``
    with NO ``fallback_without_response_format`` (unlike the dogfood-judge /
    compaction json-mode call sites) — a schema-bearing structured-output
    call must never silently degrade to free-form text; a provider rejection
    surfaces as-is for the caller (``RouterLoop._run_structured_answer_turn``)
    to classify.

    ``on_content_delta`` (#3288 ③b): forwarded verbatim to ``recorded_acompletion``
    — see its docstring. ``None`` (every call site except ``RouterLoop``'s
    primary reply call) is byte-identical to before ③b.

    ``model_class`` / ``model_class_ceiling`` (#4206 T1): forwarded verbatim to
    ``recorded_acompletion`` — see its docstring for the ②bounding enforcement
    contract. Defaulted to ``None`` here (unlike ``recorded_acompletion``'s own
    required param) because this wrapper has many pre-existing non-RouterLoop
    callers (tests, other op-loop shapes) that are not subject to the ceiling
    axis at all; ``RouterLoop`` — the one caller whose calls ARE class-based —
    passes its resolved ``router_model`` class explicitly at every call site.

    ``prompt_cache_key`` (#4700): forwarded verbatim to ``recorded_acompletion``
    — see its docstring / inline comment for the full session-unit-granularity
    reasoning. ``None`` (every call site that does not pass one) is
    byte-identical to before #4700 — nothing is sent, matching today's
    behavior exactly.
    """
    # Normalize model to ModelSpec — accept both str (backward compat) and ModelSpec.
    spec: ModelSpec = model if isinstance(model, ModelSpec) else ModelSpec(model=model, kwargs={})

    # Budget pre-check — runs before the LLM call
    if budget is not None:
        from reyn.runtime.budget.budget import BudgetExceeded, format_refusal_message
        check = budget.check_pre_llm(model=spec.model, agent=budget_agent)
        if not check.allowed:
            # #1868: route the exceed through the 3-mode limit policy (deny /
            # auto-allow / ask-user). Denied — or NO policy context (fail-closed)
            # — raises (today's behavior); approved / auto-extended → proceed past
            # the cap (the call is still recorded; BudgetLedger accounting unchanged).
            if not await _budget_exceed_allows_continue(check, budget_agent):
                raise BudgetExceeded(
                    check.hard_dimension or "budget",
                    format_refusal_message(check, agent=budget_agent),
                )

    # #309: per-class routing (api_base/provider) wins; None → global proxy.
    _routing = routing_for_spec(spec)
    extra = _routing if _routing is not None else proxy_kwargs()
    # Strip provider prefix only when routing to an api_base proxy (same logic as
    # call_llm); a direct-provider route keeps the prefix.
    effective_model = (
        spec.model.split("/", 1)[1]
        if extra.get("api_base") and "/" in spec.model else spec.model
    )
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
        # Gemini rejects tool_choice ("Function calling config is set without
        # function_declarations") when tools=[] — omit tool_choice for tool-less
        # calls. This fixes force-close wrap-up and any other tools=[] path.
        # "none" is already documented as not Gemini-safe; "auto" with tools=[]
        # is equally rejected. Plain text completion = no tool_choice needed.
        **({} if not tools else {"tool_choice": tool_choice}),
        # spec.kwargs passthrough (operator-declared, e.g. temperature)
        **spec_kwargs,
        # No thinking kwargs: disabled by default on all providers
        **extra,
    }
    # #3288 ③a: no ``stream`` key set here — the decision (capability-gated,
    # never a hardcoded "Gemini doesn't stream") is made INSIDE
    # ``recorded_acompletion`` (the #1190 single funnel), which reconstructs
    # the SAME raw-response shape when it streams, so this function's
    # ``msg = response.choices[0].message`` extraction below is unaffected
    # either way. Was: unconditional ``"stream": False`` (Gemini litellm#21041
    # streaming+tools bug) — replaced by a per-call litellm capability query.
    # #2210: an EXPLICIT timeout (the kernel path threads `safety.timeout.llm_call_seconds`
    # via the LLMCallRecorder) WINS and is used as-is — zero kernel regression. Only the
    # router path, which passes no `timeout`, falls back to the ambient per-LLM-call policy
    # context (same `safety.timeout.*` source). Without that wiring a hung provider hung the
    # whole turn.
    timeout, max_retries = _resolve_llm_call_bounds(timeout, max_retries)
    if timeout is not None:
        call_kwargs["timeout"] = timeout
    call_kwargs["num_retries"] = max_retries

    # Payload trace dump (request)
    _trace_rid = _dump_llm_request({
        "model": effective_model,
        "caller_hint": trace_caller or "unknown",
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice if tools else None,
        "sampling_params": {
            "timeout": timeout,
            "max_retries": max_retries,
        },
    })

    async def _tools_call() -> object:
        # #1190: route through the single cost-observability chokepoint.
        # recorder=None — call_llm_tools keeps its own record below; the
        # chokepoint re-derives proxy kwargs (idempotent) so only the
        # pre-built tools/tool_choice kwargs flow as extras. The op-loop is
        # tools-only (ADR-0035 D2 separate-decide) — no response_format here.
        _kw = dict(call_kwargs)
        _model = _kw.pop("model")
        _messages = _kw.pop("messages")
        return await recorded_acompletion(
            model=_model, messages=_messages, purpose=purpose,
            recorder=None, extra_kwargs=_kw,
            emit_cost_events=emit_cost_events,  # #1683: chat opts in
            routing=_routing,  # #309 per-class api_base/provider (else global wins)
            response_format=response_format,  # 0062: None for every op-loop tool call
            on_content_delta=on_content_delta,  # #3288 ③b: forwarded straight through
            stream_override=spec.stream,  # operator policy from the model class
            model_class=model_class,  # #4206 T1: forwarded straight through
            model_class_ceiling=model_class_ceiling,  # #4206 T1: forwarded straight through
            prompt_cache_key=prompt_cache_key,  # #4700: forwarded straight through
        )

    # #2210 HIGH layer: when the per-call HTTP timeout + the Router/Reyn retries are ALL
    # exhausted (a persistently hung/slow provider), `_llm_call_with_retry` re-raises the
    # litellm `Timeout`. Route it through the safety on_limit policy (the SAME framework the
    # budget gate + phase/chain timeouts use) instead of a bare error: on approval (bounded
    # `auto_extend` within `auto_extend_times`, or interactive yes) retry once more with a
    # fresh timeout window; otherwise surface the timeout (the turn ends cleanly, not hangs).
    while True:
        try:
            response = await _llm_call_with_retry(_tools_call, effective_model, event_log)
            break
        except Exception as _exc:
            # Only a persistent TIMEOUT routes through on_limit; any other error surfaces
            # as-is (unchanged). ``_llm_timeout_allows_continue`` False (on_limit deny / bound
            # exhausted / no context) → re-raise = clean turn-end.
            if not _is_llm_timeout_exc(_exc):
                raise
            if not await _llm_timeout_allows_continue(effective_model, str(_exc)):
                raise

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
            # #3339: same ambient turn key as the chokepoint's own record above
            # — this path records its own call, so it must key it too or a
            # tool-bearing turn's spend would silently miss its bucket.
            chain_id=get_active_turn_chain_id(),
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
        # #1652/②: capture the provider reasoning as a normalized BUNDLE
        # (reasoning_content + thinking_blocks, the litellm cross-provider
        # standard) so it can be re-attached natively to the assistant history
        # message next turn — not just the text. None when the model emitted no
        # reasoning (omit-when-empty). See _extract_reasoning_bundle.
        reasoning=_extract_reasoning_bundle(msg),
    )
