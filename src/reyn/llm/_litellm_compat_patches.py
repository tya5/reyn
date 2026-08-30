"""#5603 — reyn's own local patches to litellm's Responses-API bridge,
made reproducible AS A DEPENDENCY (repo module + startup import, not a
`site-packages`-direct `.pth` file — see this module's own docstring
sections below for the incident this replaces).

## Why this exists at all (owner's own 2 questions, #5603)

> "依存ライブラリ側の場合は、依存としてパッチ当てる方法ないとダメだよ？"
> "reyn の import は litellm ver up で壊れることないの？フェールセーフ
>  対応まで考えてるの？"

The PREVIOUS form (#5568) lived as `litellm_patch.py` + `zz_litellm_patch.pth`
dropped straight into `site-packages/` by hand. Two real defects, both
disclosed by the owner's own questions:

1. **Not reproducible as a dependency** — a fresh `.venv`, a CI runner, or
   any OTHER machine never gets it. `pip install -U litellm` does not
   remove it (so it LOOKS durable), but that is not the same claim as "it
   is declared anywhere reyn's own dependency graph can reproduce."
2. **Fails silently on a version bump** — a broken `.pth` line degrades to
   `Error processing line 1 of …/zz_litellm_patch.pth: ModuleNotFoundError
   … Remainder of file ignored`. The PROCESS STILL STARTS. The patch is
   simply not applied. Nothing in reyn's own event log or console says so.
   The original bug this patch exists to fix comes back with zero signal.

## The fix (architect's own design, #5603)

- **Lives in this repo, imported from reyn's own startup chokepoint**
  (:func:`reyn.llm.litellm_bootstrap.ensure_litellm_ready`) — the ONE
  place `import litellm` itself is allowed, so a patch-application
  failure surfaces at the SAME seam an `import litellm` failure already
  does, not silently past it.
- **No version-ceiling constant.** Owner's own standing instruction: never
  embed an unjustified number without a rationale or a config knob. A
  declared "verified up to litellm X.Y.Z" ceiling would BE such a number
  (who re-verifies it on the next litellm release?). Architect's own
  ruling: the CI-side scaffold test (`tests/scaffold/test_5603_*.py`)
  answers "does the defect still exist" directly, against whatever
  litellm version is actually installed — no proxy measure (a version
  number, a symbol's mere presence) stands in for the real question. Red
  there means "upstream may have fixed it — remove the workaround and
  this test," never "reyn broke."
- **Each patch function is a no-op when the defect it targets is not
  present**, BY CONSTRUCTION, not by a runtime self-check — see each
  function's own docstring for exactly which line makes this true. A
  stale patch left applied after litellm genuinely fixes the underlying
  defect (the window between the scaffold test going red and someone
  actually removing this module) therefore changes nothing observable.
- **Correctness-critical vs. diagnostic-only get different failure
  modes** (architect's own ruling — the two patches are NOT the same
  risk class):
  - :func:`apply_stream_chunk_recovery` (A) fixes a defect that DISCARDS
    a real, successful response (`ValueError("Unknown items in responses
    API response: []")` → `APIConnectionError` for a conversation the
    model actually answered). Silently running WITHOUT this patch means
    reyn returns a wrong/failed result for otherwise-correct upstream
    behavior — "正しさに必要". If this patch cannot be applied (the
    private symbols it depends on moved), :func:`apply_all` re-raises,
    which the SAME `except Exception: result = None` branch
    `ensure_litellm_ready` already has treats identically to any other
    `import litellm` failure — every no-fallback caller (a real
    completion/embedding call) correctly sees "litellm unusable" rather
    than silently running with a known-broken bridge.
  - :func:`apply_overflow_diagnosis` (B) only IMPROVES which exception
    TYPE a genuinely-failed call surfaces as (misdiagnosis, not data
    loss — the call was going to fail either way; #5568's own record:
    "診断を直すだけで、呼びが通るようになるわけではない"). If this one
    cannot be applied, the call still fails with a worse diagnosis, not a
    wrong answer — logged as a WARNING and an audit-event, not fatal.

## Observability (architect's fail-safe #2 — lens 7)

Both application attempts emit ONE `litellm_compat_patch_applied` audit-
event each (success or failure, never silent) via the caller-supplied
`EventLog`, and `reyn doctor` prints the same measured state (never a
restated declaration — this module's own flags ARE the measurement).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog


def apply_stream_chunk_recovery(events: "EventLog | None" = None) -> bool:
    """#5568(A) — litellm's ``ResponsesToCompletionBridgeHandler.
    _collect_response_from_stream_async`` (the ``stream:false`` branch of
    the Responses→chat-completions bridge) discards every streamed chunk
    unconditionally (``async for _ in stream_iter: pass``) and returns
    ONLY whatever ``completed_response.response`` already carries. When
    the provider's own terminal ``response.completed`` event reports an
    EMPTY ``output`` (litellm's own known gap — the terminal event does
    not always carry the full content the stream already delivered piece
    by piece), the real, already-delivered content is discarded and the
    caller sees ``ValueError("Unknown items in responses API response:
    [])`` → ``APIConnectionError`` for a conversation the model actually
    answered.

    Reproduced directly (no provider, no network — #5603's own falsify):
    a synthetic async iterator yielding a real ``OUTPUT_ITEM_DONE`` chunk,
    paired with a ``completed_response.response`` reporting ``output=[]``
    — the UNPATCHED method returns ``output=[]`` regardless (see
    ``tests/scaffold/test_5603_litellm_stream_recovery_defects.py`` for
    the same repro CI runs against the live installed version).

    Fix: record ``OUTPUT_ITEM_DONE``/``OUTPUT_TEXT_DONE`` chunks as they
    stream past (litellm's OWN ``record_output_item_chunk``/
    ``record_output_text_chunk`` — no new parsing written here), and only
    when the completed response's own ``output`` is empty, substitute the
    recovered items. **No-op when the defect is absent**: the line
    ``if isinstance(out, list) and len(out) == 0`` is the ENTIRE
    substitution condition — a completed response that already carries a
    non-empty ``output`` (the ordinary, non-defective case) is returned
    completely unchanged; this patched method never inspects, let alone
    overwrites, a non-empty result.

    Correctness-critical (see this module's own docstring): raises if the
    private symbols this depends on have moved — the caller
    (:func:`apply_all`) does not catch this, by design.

    Returns ``True`` on this call (idempotent — a second call after the
    first is a no-op via the ``_reyn_5603_patched`` guard, still returns
    ``True``)."""
    from litellm.completion_extras.litellm_responses_transformation import handler as H
    from litellm.responses.sse_output_recovery import (
        record_output_item_chunk,
        record_output_text_chunk,
    )
    from litellm.types.llms.openai import ResponsesAPIStreamEvents

    cls = H.ResponsesToCompletionBridgeHandler
    if getattr(cls, "_reyn_5603_patched", False):
        return True

    def _as_dict(chunk: Any) -> "dict | None":
        if isinstance(chunk, dict):
            return chunk
        for attr in ("model_dump", "dict"):
            fn = getattr(chunk, attr, None)
            if callable(fn):
                try:
                    return fn()
                except Exception:  # noqa: BLE001 — a malformed chunk is skipped, not fatal
                    pass
        return None

    async def _collect_response_from_stream_async(self: Any, stream_iter: Any) -> Any:
        items: dict = {}
        text_only: dict = {}
        async for chunk in stream_iter:
            t = getattr(chunk, "type", None)
            if t not in (
                ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            ):
                continue
            d = _as_dict(chunk)
            if not d:
                continue
            if t == ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE:
                record_output_item_chunk(parsed_chunk=d, output_items=items)
            else:
                record_output_text_chunk(
                    parsed_chunk=d, output_items=items, text_only_items=text_only,
                )

        completed = getattr(stream_iter, "completed_response", None)
        response_obj = getattr(completed, "response", None) if completed else None
        if response_obj is None:
            raise ValueError("Stream ended without a completed response")

        hidden_params = getattr(stream_iter, "_hidden_params", None)
        response = self._coerce_response_object(response_obj, hidden_params)

        out = getattr(response, "output", None)
        if isinstance(out, list) and len(out) == 0:
            merged = {**text_only}
            merged.update(items)
            if merged:
                response.output = [v for _, v in sorted(merged.items())]

        return response

    cls._collect_response_from_stream_async = _collect_response_from_stream_async
    cls._reyn_5603_patched = True
    return True


def apply_overflow_diagnosis(events: "EventLog | None" = None) -> bool:
    """#5568(B) — a provider (observed: a real gpt-5.6-luna deployment)
    can end a Responses-API SSE stream after only ``response.created``/
    ``response.in_progress`` on a genuine context-length overflow, with
    NO terminal event and no body — the OpenAI public streaming-events
    spec's own documented shape for "started, then cut off", also
    reported independently by other client implementations against the
    SAME upstream behavior (maximhq/bifrost#4413). litellm's own
    ``_extract_completed_response_from_sse`` (a pure function — no
    network, no ``logging_obj``) returns ``(None, None)`` for this input:
    no completed response AND no error message, because nothing in the
    truncated stream told it WHY. The caller then raises
    ``OpenAIError(message=error_message or raw_response.text, ...)`` —
    since ``error_message`` is ``None``, the WHOLE raw SSE body (real
    incident: 163,835 bytes) becomes the exception's message, and reyn's
    own ``is_context_overflow_error``/``classify_llm_failure`` (neither
    a real ``ContextWindowExceededError`` nor a recognisable keyword
    anywhere in that raw SSE blob) both fail to recognise it as an
    overflow.

    Reproduced directly (no provider — #5603's own falsify): a synthetic
    SSE string containing only ``response.created``/``response.
    in_progress`` lines fed to the unpatched, pure
    ``_extract_completed_response_from_sse`` returns ``(None, None)`` —
    see ``tests/scaffold/test_5603_litellm_stream_recovery_defects.py``.

    Fix: on the SAME "no completed response" failure, re-scan the raw SSE
    for ANY terminal event or ANY real output content; if genuinely
    neither is present (the truncated-overflow shape, not some other
    provider error this patch has no business reinterpreting), re-raise
    as ``litellm.ContextWindowExceededError`` instead — reyn's own
    ``classify_llm_failure``/``is_context_overflow_error`` then correctly
    classify it as OVERFLOW. This corrects the DIAGNOSIS only — it does
    not make the call succeed (#5568's own record: "診断を直すだけで、
    呼びが通るようになるわけではない").

    **No-op when the defect is absent**: this patch wraps the ORIGINAL
    method in a try/except and re-raises the SAME exception UNCHANGED
    whenever a genuine terminal event or real output content is present
    in the raw SSE (``if has_terminal or has_body: raise``) — an ordinary
    successful call, or a call that fails for any OTHER reason, is never
    touched; the wrapped call only ever converts the ONE specific silent-
    truncation shape.

    Diagnostic-only (see this module's own docstring): a failure to apply
    this patch is logged, never fatal — the caller (:func:`apply_all`)
    catches any exception here.

    Returns ``True`` on this call (idempotent — a second call after the
    first is a no-op via the ``_reyn_5603b_patched`` guard, still returns
    ``True``)."""
    from litellm.llms.chatgpt.responses import transformation as T
    from litellm.responses.sse_output_recovery import parse_sse_json_chunk
    from litellm.types.llms.openai import ResponsesAPIStreamEvents

    cls = getattr(T, "ChatGPTResponsesAPIConfig", None)
    if cls is None:
        for name in dir(T):
            o = getattr(T, name)
            if isinstance(o, type) and hasattr(o, "_extract_completed_response_from_sse"):
                cls = o
                break
    if cls is None:
        raise AttributeError(
            "reyn #5603(B): could not locate the Responses-API config class "
            "in litellm.llms.chatgpt.responses.transformation — upstream "
            "may have restructured this module"
        )
    if getattr(cls, "_reyn_5603b_patched", False):
        return True

    orig = cls.transform_response_api_response

    def transform_response_api_response(
        self: Any, model: Any, raw_response: Any, logging_obj: Any,
    ) -> Any:
        try:
            return orig(self, model=model, raw_response=raw_response, logging_obj=logging_obj)
        except Exception as exc:
            body = getattr(raw_response, "text", "") or ""
            if not body.lstrip().startswith(("data:", "event:")):
                raise
            seen = set()
            for line in body.splitlines():
                d = parse_sse_json_chunk(line)
                if d:
                    seen.add(d.get("type"))
            terminal = {
                ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
                getattr(ResponsesAPIStreamEvents, "RESPONSE_INCOMPLETE", None),
                getattr(ResponsesAPIStreamEvents, "RESPONSE_FAILED", None),
            }
            has_terminal = bool(seen & terminal)
            has_body = any(
                t and ("output_text" in str(t) or "output_item" in str(t)) for t in seen
            )
            if has_terminal or has_body:
                raise
            import litellm
            raise litellm.ContextWindowExceededError(
                message=(
                    "reyn #5603(B): the upstream stream ended after "
                    f"{sorted(str(t) for t in seen if t)} with no terminal "
                    "event and no output — the shape the Responses API "
                    "produces on a context-length overflow whose error "
                    "event carries an empty message"
                ),
                model=model,
                llm_provider="openai",
            ) from exc

    cls.transform_response_api_response = transform_response_api_response
    cls._reyn_5603b_patched = True
    return True


def apply_all(events: "EventLog | None" = None) -> None:
    """#5603 — the ONE call site :func:`reyn.llm.litellm_bootstrap.
    ensure_litellm_ready` makes. Applies both patches with their own,
    DIFFERENT failure semantics (see each function's own docstring for
    why they differ):

    - :func:`apply_stream_chunk_recovery` (A, correctness-critical) —
      exceptions propagate UNCAUGHT. ``ensure_litellm_ready``'s own
      surrounding ``try/except Exception: result = None`` then treats a
      failure here identically to any other ``import litellm`` failure.
    - :func:`apply_overflow_diagnosis` (B, diagnostic-only) — caught
      here, logged as a WARNING, and reported via the audit-event below;
      never blocks litellm from becoming usable.

    Both successes/failures emit ``litellm_compat_patch_applied`` (one
    event per patch, `events` may be ``None`` — same posture as this
    module's own sibling hooks in ``litellm_bootstrap.py``, which already
    treat a missing ``EventLog`` as "skip the audit-event, still do the
    work")."""
    import logging

    log = logging.getLogger(__name__)

    ok_a = False
    try:
        apply_stream_chunk_recovery(events)
        ok_a = True
    finally:
        if events is not None:
            events.emit(
                "litellm_compat_patch_applied",
                patch="stream_chunk_recovery", issue="#5603", applied=ok_a,
            )
    # #5603: A is correctness-critical — a failure above already
    # propagated past the `finally` (which only records the event, never
    # swallows) before reaching here, so this line only runs on success.

    try:
        apply_overflow_diagnosis(events)
        ok_b = True
    except Exception as exc:  # noqa: BLE001 — diagnostic-only, never fatal
        ok_b = False
        log.warning(
            "reyn #5603(B): could not patch litellm's Responses-API "
            "context-overflow diagnosis (upstream may have restructured "
            "the private symbols this depends on) — calls that hit the "
            "underlying defect will surface a generic error instead of a "
            "recognised context-overflow: %r", exc,
        )
    if events is not None:
        events.emit(
            "litellm_compat_patch_applied",
            patch="overflow_diagnosis", issue="#5603", applied=ok_b,
        )
