"""reyn #5620 — standalone litellm PROXY patch (D only).

Owner ruling (2026-09-02, verbatim): "litellm patch は proxy/lib 分けて
我々のリポジトリにコミットしておいて... proxy はランタイムだけで良い"
(2026-08-30) — the owner's `junk/litellm` proxy runs its OWN litellm
install (python3.13, litellm 1.95.0 + the `proxy` extra), separate from
reyn's own in-process litellm (1.96.2, `src/reyn/llm/
_litellm_compat_patches.py` — retired, #5620, both its own patches
verified unreachable). THIS file therefore imports ONLY the standard
library and litellm itself — never ``reyn`` — so it can be dropped into
that proxy venv's site-packages without installing reyn there at all.

## Origin (patches A/B/C dropped, D kept)

This file descends from a hand-placed ``litellm_patch.py`` (#5568,
2026-08-30/09-02, real-machine incident: reyn-self history.jsonl growth
+ a live 500 on a real gpt-5.6-luna call). That version carried 4
patches (A/B/C/D); A/B/C are dropped here — #5620's own contract: only
D is the proxy's OWN defect (a request-processing/routing bug in the
PROXY layer itself, `common_request_processing.py`/`llm_http_handler.py`
— A/B/C targeted the SAME classes reyn's own #5603/#5614 already cover
on reyn's side, or classes reyn's real call path never reaches; keeping
them here would duplicate reyn's own coverage on a code path reyn
already handles).

## The defect D fixes (traced directly against litellm 1.95.0,
file:line — re-verify against whatever version is actually installed;
:func:`_version_is_pinned` reports the version this trace was done
against, not a floor/ceiling this file enforces)

A PROXY client that asks for ``stream: false`` on a `/v1/responses` call
routed to the ``chatgpt`` provider still gets a raw SSE pass-through
(and, if the upstream connection ends mid-stream, a trailing
``data: {"error": {...}}`` frame instead of a real HTTP 4xx/5xx) — never
the JSON (or a real error status) a ``stream: false`` caller expects.
The chain (each step re-traceable against the installed litellm's own
source, not re-explained end-to-end here — see the removed hand-placed
version's own file history for the full step-by-step, kept out of this
file to stay short):

1. ``ChatGPTResponsesAPIConfig.transform_responses_api_request`` forces
   ``stream=True`` on every outbound request to the real ChatGPT
   backend, regardless of the caller's own flag.
2. ``OpenAIResponsesAPIConfig.should_fake_stream`` (inherited,
   unoverridden) reads the CALLER's original flag — stays ``False`` for
   a ``stream:false`` caller, so litellm's own "fake a synchronous
   response out of a real stream" path never engages.
3. ``BaseLLMHTTPHandler.async_response_api_handler`` therefore returns a
   raw streaming iterator even though step 2 never faked it — this is
   the ONE seam this patch targets (see "Why here, not one layer up" and
   "Why here, not one layer down" below).
4. The proxy's own request-processing (a large inline method) checks
   "is this a streaming request" as `caller flag OR isinstance(response,
   AsyncIterator)` — the isinstance half is unconditionally True for
   step 3's return value, so the proxy streams SSE to the client
   regardless of what the client actually asked for.

## Why patch `async_response_api_handler`, not a layer up or down

- **Not layer up** (the proxy's own request-processing method): that
  method is one ~2000-line inline function, not a small seam, and its
  own streaming-detection helper is SYNCHRONOUS — it cannot itself drain
  an async iterator or hand back a replacement value to the caller's own
  local variable.
- **Not layer down** (steps 1/2 individually): forcing `stream=False`
  upstream (undoing step 1) would change what litellm sends the REAL
  ChatGPT backend, a materially bigger behavior change than "fix what
  the PROXY hands back to ITS OWN client" — and litellm's own upstream
  behavior is not reyn's (or the owner's fork's) responsibility to
  second-guess (#5568's own "third-party responsibility" ruling).
- `async_response_api_handler` is the ASYNC method that actually
  PRODUCES the streaming iterator (step 3) — patching it at the SOURCE
  means steps 4+ never see an iterator at all for this shape; they fall
  through their own EXISTING, unmodified JSON-return path. It is a
  single classmethod-level patch (`BaseLLMHTTPHandler`, a module-level
  singleton instance reused everywhere in litellm) — picked up
  regardless of import order.

## No-op when absent (same #5603 discipline this repo already uses)

`_reyn_5620d_patched` is a class-attribute guard: applying twice is a
no-op, not a double-wrap. The wrapper itself only intervenes when ALL
THREE conditions hold — the CALLER asked for `stream=False`, the
resolved provider is `"chatgpt"`, and litellm's own return value is
genuinely the raw streaming iterator type this defect produces; any
other shape (stream:true, a different provider, or litellm already
handing back a parsed response) falls through `return result` UNCHANGED.
If litellm restructures the private symbols this depends on
(`BaseLLMHTTPHandler`, `ResponsesAPIStreamingIterator`, the exact
signature `async_response_api_handler` takes), :func:`apply` reports
`False` and writes that into the status file rather than raising — a
broken proxy patch must never be a broken proxy.

## Status + reach (owner: "起動した瞬間に見える必要がある")

Writes ``~/.reyn/litellm-proxy-patch-status.json`` (path/schema mirrored
in reyn's own ``src/reyn/llm/litellm_proxy_patch_status.py`` — see that
module's own docstring for why this is a hand-copied literal, not an
import, and how the gate test keeps the two in sync) once at import
time, and updates the SAME file's own ``reached.D`` counter every time
the patched wrapper actually intercepts a call (not merely "the patch
function ran without raising" — a real interception).

## Off switch

Delete this file and its own ``.pth`` line from the proxy venv's
site-packages (``python install.py --uninstall``, see this directory's
own ``install.py``). No litellm file is ever edited on disk.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: Mirrors `reyn.llm.litellm_proxy_patch_status.LITELLM_PROXY_PATCH_STATUS_
#: PATH_STR` — a HAND-COPIED literal, not an import (this file must never
#: import reyn — see module docstring). Kept in sync by a Tier 2 gate test
#: on the reyn side (`tests/llm/test_5620_litellm_proxy_patch_d.py`), which
#: reads this exact literal out of this file's own source and asserts it
#: equals the reyn-side constant.
_STATUS_PATH_STR = "~/.reyn/litellm-proxy-patch-status.json"

_reached_d = 0
_legacy_present = False

#: The pre-#5620 hand-placed patch's OWN guard attribute (#5568(D),
#: `litellm_patch.py` + `zz_litellm_patch.pth`, a DIFFERENT file this
#: one supersedes). #5620/PR-review finding (lead-coder, real drive on
#: the owner's own live proxy venv, 2026-09-02): that legacy install can
#: still be ACTIVE (its own `.pth` never removed) in the SAME venv this
#: file gets installed into — `install.py` does clean it up, but only on
#: a run of `install.py` itself; a venv that already has BOTH patches'
#: own `.pth` files present would otherwise apply this file's own
#: `apply_d()` on TOP OF the legacy wrapper (both target the exact same
#: class method), producing a double-wrapped chain neither patch was
#: ever verified against. Detected and refused below — never silently
#: layered.
_LEGACY_PATCHED_ATTR = "_reyn_5568d_patched"


def apply_d() -> bool:
    """Apply the ONE proxy-side patch this file carries. Returns whether
    it applied (``True`` also on an already-applied no-op re-call, per
    the module docstring's own no-op discipline). Returns ``False``
    (never applies) when the legacy pre-#5620 patch is already active on
    the same class — see ``_LEGACY_PATCHED_ATTR``'s own docstring."""
    global _legacy_present
    try:
        import litellm  # noqa: F401 — imported for the status write's own version read
        from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
        from litellm.responses.sse_output_recovery import (
            record_output_item_chunk,
            record_output_text_chunk,
        )
        from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator
        from litellm.types.llms.openai import ResponsesAPIResponse, ResponsesAPIStreamEvents
    except Exception as exc:
        print(
            f"[reyn litellm_proxy_patch] apply_d: import failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False

    if getattr(BaseLLMHTTPHandler, _LEGACY_PATCHED_ATTR, False):
        _legacy_present = True
        print(
            "[reyn litellm_proxy_patch] apply_d: refusing to apply — the "
            "legacy pre-#5620 patch (litellm_patch.py / "
            "zz_litellm_patch.pth) is already active on this class; run "
            "install.py --uninstall on the legacy install first (its own "
            "site-packages files still carry the OLD filenames, install.py "
            "removes them too). Never double-wrapping the same method.",
            file=sys.stderr,
        )
        return False

    if getattr(BaseLLMHTTPHandler, "_reyn_5620d_patched", False):
        return True

    original = BaseLLMHTTPHandler.async_response_api_handler

    def _as_dict(chunk):
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

    async def _collect_or_raise(iterator, *, model):
        last_error_frame = None
        items: dict = {}
        text_only: dict = {}
        async for chunk in iterator:
            err = getattr(chunk, "error", None)
            if isinstance(err, dict) and err:
                last_error_frame = err

            t = getattr(chunk, "type", None)
            if t in (
                ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            ):
                d = _as_dict(chunk)
                if d:
                    if t == ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE:
                        record_output_item_chunk(parsed_chunk=d, output_items=items)
                    else:
                        record_output_text_chunk(
                            parsed_chunk=d, output_items=items, text_only_items=text_only,
                        )

        completed = getattr(iterator, "completed_response", None)
        response_obj = getattr(completed, "response", None) if completed else None
        if response_obj is not None:
            if isinstance(response_obj, dict):
                try:
                    response_obj = ResponsesAPIResponse(**response_obj)
                except Exception:  # noqa: BLE001 — same coercion reyn's own bridge patch used
                    response_obj = ResponsesAPIResponse.model_construct(**response_obj)
            out = getattr(response_obj, "output", None)
            if isinstance(out, list) and len(out) == 0:
                merged = {**text_only}
                merged.update(items)
                if merged:
                    response_obj.output = [v for _, v in sorted(merged.items())]
            return response_obj

        if last_error_frame is not None:
            message = last_error_frame.get("message") or "Response API in-stream error"
            code = last_error_frame.get("code")
            try:
                status_code = int(code)
            except (TypeError, ValueError):
                raise litellm.APIError(
                    status_code=502,
                    message=(
                        "reyn litellm_proxy_patch(D): stream ended without "
                        "response.completed and the trailing error frame "
                        f"carried no usable numeric code (code={code!r}); "
                        f"raw frame={last_error_frame!r}"
                    ),
                    llm_provider="chatgpt",
                    model=model or "",
                )
            # The upstream frame's own status/message, verbatim — never a
            # mapped/invented code (reyn's own overflow classifier keys off
            # message text; a paraphrase could silently misclassify).
            raise litellm.APIError(
                status_code=status_code, message=message,
                llm_provider="chatgpt", model=model or "",
            )

        raise litellm.APIError(
            status_code=502,  # the one invented status: asked, got nothing conclusive back
            message="reyn litellm_proxy_patch(D): stream ended without response.completed",
            llm_provider="chatgpt", model=model or "",
        )

    async def async_response_api_handler(self, *args, **kwargs):
        global _reached_d
        # #5620 (corrected, PR-review — lead-coder's own real drive
        # against the owner's live proxy venv, 2026-09-02): litellm's
        # REAL `async_response_api_handler` signature (verified against a
        # CLEAN `pip install litellm[proxy]==1.95.0`, no legacy patch
        # active) carries real named parameters, including
        # `response_api_optional_request_params`/`custom_llm_provider`/
        # `model` — an earlier revision of this comment claimed the real
        # signature was a bare `(self, *args, **kwargs)`; that claim was
        # itself an artifact of measuring the signature AFTER the legacy
        # pre-#5620 patch (`litellm_patch.py`) had already wrapped this
        # SAME method with its own `(self, *args, **kwargs)` closure on
        # the owner's own live proxy venv (that venv's own `.pth` was
        # still active) — `_LEGACY_PATCHED_ATTR`'s own guard above exists
        # precisely so this file never applies on top of that and
        # produces the same illusion again. Reading straight off THIS
        # wrapper's own `**kwargs` here (it receives the identical call
        # either way) is still correct and simpler than `inspect.
        # signature(original).bind_partial(...)` would be — not because
        # the real signature lacks names, but because this wrapper's own
        # `**kwargs` already carries every keyword argument by name
        # regardless of what `original`'s own signature declares.
        params = kwargs.get("response_api_optional_request_params") or {}
        custom_llm_provider = kwargs.get("custom_llm_provider")
        model = kwargs.get("model")
        client_wants_stream = bool(params.get("stream"))

        result = await original(self, *args, **kwargs)

        if (
            not client_wants_stream
            and custom_llm_provider == "chatgpt"
            and isinstance(result, ResponsesAPIStreamingIterator)
        ):
            _reached_d += 1
            _write_status()
            return await _collect_or_raise(result, model=model)
        return result

    BaseLLMHTTPHandler.async_response_api_handler = async_response_api_handler
    BaseLLMHTTPHandler._reyn_5620d_patched = True
    return True


def _write_status() -> None:
    """One JSON object (overwritten each call, never appended — this is a
    CURRENT-state file, not a log; ``reyn doctor`` reads only the latest
    write). Owner instruction (2026-08-30): visible the moment the proxy
    starts, so a broken/missing patch reads as an absent or stale file
    rather than silence."""
    try:
        import litellm
        from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
        patched_d = bool(getattr(BaseLLMHTTPHandler, "_reyn_5620d_patched", False))
    except Exception:  # noqa: BLE001 — status write degrades, never raises into the caller
        patched_d = False
    try:
        import litellm
        version = getattr(litellm, "__version__", None)
        if version is None:
            import importlib.metadata
            version = importlib.metadata.version("litellm")
    except Exception:  # noqa: BLE001
        version = None

    payload = {
        "pid": os.getpid(),
        "litellm_version": version,
        "patched": {"D": patched_d},
        "reached": {"D": _reached_d},
        # #5620/PR-review: True when the legacy pre-#5620 patch was found
        # already active (apply_d() refused to double-wrap) — see
        # _LEGACY_PATCHED_ATTR's own docstring. `patched.D` stays False
        # in that case, distinguishing "this file's own patch never
        # applied because the OLD one is still here" from any other
        # apply failure.
        "legacy_present": _legacy_present,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        path = Path(_STATUS_PATH_STR).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — a status-write failure must never break the proxy
        pass


def apply() -> bool:
    """The one entry point the ``.pth`` line's own bare ``import
    litellm_proxy_patch`` triggers (this function runs at import time,
    below — see the bottom of this file). Returns whether D applied."""
    ok = apply_d()
    _write_status()
    return ok


apply()
