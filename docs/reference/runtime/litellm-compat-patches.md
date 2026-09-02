# litellm compat patches

Reyn works around real litellm defects in two places, split by which
litellm install the defect lives in: reyn's own in-process litellm (this
page's own "lib" section), and the owner's `junk/litellm` proxy (a
separate installation, a separate version — see #5620's own proxy-side
PR for that section, added alongside this one).

## Lib side — retired (#5620)

`src/reyn/llm/_litellm_compat_patches.py` (#5603) used to carry 2
monkeypatches applied once inside `ensure_litellm_ready()`'s own startup
chokepoint:

- **A** (`apply_stream_chunk_recovery`) — targeted
  `ResponsesToCompletionBridgeHandler._collect_response_from_stream_async`,
  litellm's Responses→chat-completions bridge.
- **B** (`apply_overflow_diagnosis`) — targeted `ChatGPTResponsesAPIConfig`,
  a Responses-API config class reyn's own production code never uses
  (reyn uses `OpenAIResponsesAPIConfig`, #5568).

Both are removed (#5620), with 0 reachability on reyn's real production
call path, verified directly rather than assumed:

- **B**: reyn never constructs a `ChatGPTResponsesAPIConfig` — it is a
  different Responses-API config class than the one reyn's own resolved
  models route through (#5568's own record).
- **A**: a real `httpx.MockTransport`-driven call through
  `litellm.acompletion()` (5 caller-shape combinations — streaming and
  non-streaming, a plain reasoning model and a Responses-only model),
  against the installed litellm 1.96.2, never once reached the private
  method A patched — the caller-level `stream` flag and the bridge's
  internal HTTP-level `stream` flag are always identical under this
  litellm version, so the "caller asked for `stream=False` but the
  bridge received a streaming object anyway" state A existed to recover
  from cannot occur. See PR history for the full trace.

reyn's own classification-side response to the underlying incident this
patch module was built around lives in #5614 (`classify_llm_failure`'s
own `code == 200` branch, `src/reyn/services/compaction/engine.py`) —
that fix is unaffected by this retirement; it corrects reyn's own
failure classification, not litellm's bridge internals.

No lib-side `reyn doctor` section or audit-event remains — there is
nothing left to measure.
