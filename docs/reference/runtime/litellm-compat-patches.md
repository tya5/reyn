# litellm compat patches

Reyn works around real litellm defects in two places, split by which
litellm install the defect lives in: reyn's own in-process litellm (this
page's own "lib" section), and the owner's `junk/litellm` proxy (a
separate installation, a separate version — this page's own "proxy"
section).

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

## Proxy side (#5620)

`scripts/litellm_proxy_patch/` — a standalone patch (no `reyn` import)
for the owner's own `junk/litellm` proxy, a SEPARATE litellm
installation (its own venv, python3.13, litellm 1.95.0 + the `proxy`
extra) from reyn's own in-process litellm. Owner ruling (2026-08-30):
"proxy はランタイムだけで良い" — the proxy venv never gets a reyn
install.

**The defect (D)**: a proxy client requesting `stream: false` on a
`/v1/responses` call routed to the `chatgpt` provider still gets raw SSE
pass-through (and, on a mid-stream upstream error, a trailing
`data: {"error": {...}}` frame rather than a real HTTP 4xx/5xx) instead
of the plain JSON a `stream: false` caller expects. Full traced chain:
`scripts/litellm_proxy_patch/litellm_proxy_patch.py`'s own module
docstring. Reached only when {caller stream=false, provider=chatgpt,
litellm hands back a raw streaming iterator anyway} — every other shape
falls through unchanged.

**Install / verify / uninstall**: `scripts/litellm_proxy_patch/README.md`.
Status file: `~/.reyn/litellm-proxy-patch-status.json` (path/schema:
`src/reyn/llm/litellm_proxy_patch_status.py`) — written by the proxy
process itself; `reyn doctor` reads the same file (a separate process
this reyn process never imports or connects to).

**Regression detection**: `tests/scaffold/test_5620_litellm_proxy_
defects.py` reproduces the 3 upstream defects D's own chain depends on,
pinned to litellm 1.95.0 (`importlib.metadata.version("litellm")`) — a
different installed version skips with an explicit qualifier (not a bare
skip), and CI carries a dedicated, path-conditional leg that installs
`litellm[proxy]==1.95.0` in its own venv specifically to run this file
un-skipped. RED there (the defect no longer reproduces) is GOOD NEWS —
see that test file's own module docstring for the removal instructions
when it fires. `tests/llm/test_5620_litellm_proxy_patch_d.py` exercises
D's own patched behavior (real/broken SSE fixtures) and the path/schema
parity gate between this page's 2 copies of the status-file literal.

**When to remove**: `scripts/litellm_proxy_patch/README.md`'s own
"When to remove this entirely" section.
