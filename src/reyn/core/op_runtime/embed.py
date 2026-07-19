"""embed op handler — the raw embedding primitive: batch texts -> vectors.

FP-0057 Phase 1. `embed` is the **user-facing** primitive (the user composes
`embed` -> their own external MCP vector-DB's tools via pipeline; reyn never
hosts a user RAG store) AND the shared logic Phase 2's `index_update` /
`semantic_search` will call internally — same `EmbeddingProvider`, no
duplicated embed logic, split only by audience surface.

Reuses the existing `EmbeddingProvider` (`get_provider` -> the
`LiteLLMEmbeddingProvider`, the sole embedder — reyn depends on litellm
exclusively for embeddings, #3128). This handler batches nothing
itself: `provider.embed()` already internally splits into
`embedding.batch_size` (default 100) sized API calls; the op contract is
simply list-in -> list-out (ADR-0033 §2.1 / FP-0057 design "batch: list ->
vectors").

Bound + cancel seam (#3043). `embed` was the ONE provider call in the OS that
was neither bounded nor cancellable: MCP has the gateway's 120s bound +
`race_cancellable`; the chat LLM path has `chat.timeout.llm_call_seconds` + a
timeout-exhaustion channel; `litellm.aembedding` had neither, so its only
ceiling was litellm's own `request_timeout` default (6000s/attempt, ~5h across
`max_retries`) and a Ctrl-C could not interrupt it. The two halves now live at
the two altitudes each one belongs at:

  - the BOUND is inside the provider (`LiteLLMEmbeddingProvider`, resolved by
    `resolve_embed_timeout` from `embedding.timeout`) — so it covers every
    caller of the provider by construction, not per-call-site. It is a LATENCY
    invariant, not a COST one: it caps how long we wait, not how many requests
    the provider receives (the OpenAI SDK client retries beneath it — 9 per
    `embed` at `max_retries: 3`, measured; #3047);
  - the CANCEL is HERE, at the op, racing `provider.embed()` against
    `ctx.cancel_event` via `race_cancellable` — the same primitive and the same
    `Cancelled` → `status="cancelled"` + `<op>_cancelled` audit-event surface
    `mcp` / `sandboxed_exec` already use. This is the right altitude because
    EVERY embedding egress in the OS funnels through this op (`semantic_search`,
    `index_update`, and `ActionEmbeddingIndex` all dispatch `EmbedIROp` rather
    than calling `provider.embed()` provider-direct — see the redaction-egress
    seam below, which is load-bearing for the same reason), so one seam here
    makes every embedding egress cancellable. `race_cancellable` cancels the
    HOST task, so the cancel propagates through the provider's internal
    `asyncio.gather` into the in-flight HTTP read — it does not wait out the
    bound above.

Redaction-egress seam (co-vet #3, security): embedding via an API-backed
provider is a DATA EGRESS point — text content leaves the process to an
external embedding API. Every text is passed through the PRE-embed scan
(`redact_secrets`, the existing FP-0050 secret-redaction primitive also used
at the compaction-input egress boundary) BEFORE `provider.embed()` is called
— the seam sits at the op's PRE-call point, ahead of the egress, and is not
bypassable (no caller-supplied "skip scan" flag; every text in the batch is
scanned unconditionally). A redaction hit is recorded as an audit-event
(`embed_secret_redacted`) so the seam firing is observable (P6). Phase 1
scaffolds the seam with the existing secret-redaction pass; the full firm
ephemeral-attachment content policy is Phase 3 per the FP-0057 design doc
(`docs/deep-dives/proposals/0057-rag-retrieval-redesign.md`).
"""
from __future__ import annotations

import os

from reyn.core.cancellable import Cancelled, race_cancellable
from reyn.data.embedding import get_provider
from reyn.schemas.models import EmbedIROp
from reyn.security.secret_redaction import redact_secrets

from . import register
from .context import OpContext


def _resolve_provider(event_sink=None):
    """Resolve the embedding provider (env override + reyn.yaml embedding config).

    Mirrors `op_runtime.semantic_search._resolve_provider` — both call sites
    resolve the SAME shared `EmbeddingProvider` (`LiteLLMEmbeddingProvider` via
    `get_provider`); semantic_search's provider-direct query embed and this
    op's batch embed are independent call sites into one shared provider, not
    duplicated embedding logic. Kept as a small local helper (not a
    cross-module import of semantic_search's private function) so each
    op-runtime module stays self-contained and independently testable via
    monkeypatching its own module-level `get_provider` name (established
    op_runtime test convention, see `tests/test_op_semantic_search.py`).

    FP-0057 #2856 Part A: ``event_sink`` (from ``ctx.embedding_event_sink``) is
    forwarded to ``get_provider`` so a session-scoped TUI model-download status
    sink still fires even though this call resolves a FRESH provider per op call
    (the caller — e.g. ``ActionEmbeddingIndex`` via the tool-use `embed` op path
    — no longer holds its own long-lived provider instance).
    """
    name = os.environ.get("REYN_EMBEDDING_PROVIDER", "litellm")
    if name == "litellm":
        try:
            from reyn.config import load_config
            cfg = load_config().embedding
        except Exception:
            cfg = None
        return get_provider(name, config=cfg or {}, event_sink=event_sink)
    return get_provider(name, config={}, event_sink=event_sink)


async def handle(op: EmbedIROp, ctx: OpContext) -> dict:
    """Execute an embed op: batch texts -> vectors (FP-0057 Phase 1).

    Steps:
      1. PRE-embed redaction-egress scan (co-vet #3) — every text is passed
         through `redact_secrets()` before it reaches the provider, i.e.
         before the egress boundary to an external embedding API.
      2. `provider.embed(scanned_texts, model)` — batches internally; list in,
         list out, vector order preserved. The provider is resolved fresh per
         call via `_resolve_provider(event_sink=ctx.embedding_event_sink)`
         (FP-0057 #2856 Part A) — `ctx.embedding_event_sink` forwards the
         caller's TUI model-download status sink through WITHOUT the caller
         holding its own provider instance, so a tool-use caller (e.g.
         `ActionEmbeddingIndex`) routes through this op (inheriting the
         redaction seam above) while keeping its download-status rows.
      3. FP-0063 PC: price this call (`estimate_embedding_cost`, its OWN
         model's rate — X6 mixed-model correctness) for the returned metadata,
         and record it into the INDEPENDENT embedding-cost aggregate via
         `ctx.budget_gateway.record_embedding` — the single recording entry
         point, which fans out to session scope (itself) and agent/project
         scope (the process-shared tracker it holds, keyed by the session's
         agent NAME — the key the per-scope readers use; `ctx.agent_id` is the
         FP-0016 host identity and would be the wrong key). None of this
         touches the chat `CostBreakdown` (owner: "embedding は独立追跡の想定").

    Returns: `{"kind": "embed", "vectors": list[list[float]], "model": str,
    "total_tokens": int, "cost_usd": float | None, "priced": bool}`.
    On a Ctrl-C mid-embed (#3043) the shape is instead `{"kind": "embed",
    "status": "cancelled", "model": str}` + an `embed_cancelled` audit-event —
    the `mcp` / `sandboxed_exec` cancelled-surface, not an error.

    `cost_usd` is `None` (with `priced=False`) when litellm cannot price
    `model` — an unpriced/unknown model must degrade VISIBLY, never silently
    read as $0.00 (#1829 sentinel, extended to embedding mode). Errors
    propagate to the shared `execute_op` try/except (status="error" +
    `control_ir_failed` event) — this handler does not swallow provider
    failures.
    """
    if not op.texts:
        return {
            "kind": "embed", "vectors": [], "model": op.embedding_model,
            "total_tokens": 0, "cost_usd": 0.0, "priced": True,
        }

    # ── PRE-embed egress seam (co-vet #3) — unconditional, unbypassable ────
    scanned_texts = [redact_secrets(t) for t in op.texts]
    redacted_count = sum(1 for orig, scanned in zip(op.texts, scanned_texts) if orig != scanned)
    if redacted_count:
        ctx.events.emit(
            "embed_secret_redacted",
            count=redacted_count,
            model=op.embedding_model,
        )

    provider = _resolve_provider(event_sink=ctx.embedding_event_sink)
    try:
        result = await race_cancellable(
            provider.embed(scanned_texts, op.embedding_model),
            cancel_event=ctx.cancel_event,
        )
    except Cancelled:
        # #3043: distinct from a provider failure (P6 audit) — mirrors mcp_cancelled
        # / sandboxed_exec_cancelled. No cost is recorded: the call never returned a
        # token count, so pricing it would be an invention. NOTE (#3047): requests
        # already DELIVERED before the cancel are therefore not reflected here —
        # that under-count is structural to recording post-await (it predates this
        # path and is not specific to cancel), and closing it needs a design, not a
        # guess at this seam.
        ctx.events.emit("embed_cancelled", model=op.embedding_model)
        return {"kind": "embed", "status": "cancelled", "model": op.embedding_model}

    model_used = result.get("model", op.embedding_model)
    total_tokens = result.get("total_tokens", 0)

    # ── #3047 (c): delivered-attempt observation (P6 audit-event, NO cost) ─
    # The cost tracker below records only the ONE returned response's tokens,
    # so a call that retried N times before succeeding reports 1-of-N delivered
    # requests silently. This audit-event makes the retry overhead VISIBLE
    # without pricing it (owner GO: observation-only, candidate (c)). It is
    # purely a P6 signal — it does NOT touch `record_embedding` / the cost
    # aggregate, so it cannot double-count (that is the whole point of (c)).
    # Read defensively: `attempts` is `NotRequired` on EmbedBatchResult, so a
    # loopless provider (test stubs) omits it and we simply do not emit — an
    # absent key means "provider has no retry concept", never a fabricated 1.
    # Emitted on the SUCCESS path only; the all-attempts-failed batch path
    # raises and surfaces via `control_ir_failed`, a separate loud surface.
    attempts = result.get("attempts")
    if attempts is not None:
        ctx.events.emit(
            "embed_attempts",
            model=model_used,
            attempts=attempts,
            successful_batches=result.get("successful_batches", 0),
        )

    # ── FP-0063 PC: independent embedding-cost tracking (X2b/X4/X2c) ───────
    from reyn.llm.pricing import estimate_embedding_cost
    cost_usd, _pricing_snapshot = estimate_embedding_cost(model_used, total_tokens)

    gateway = getattr(ctx, "budget_gateway", None)
    if gateway is not None:
        gateway.record_embedding(model=model_used, tokens=total_tokens)

    return {
        "kind": "embed",
        "vectors": result.get("vectors", []),
        "model": model_used,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "priced": cost_usd is not None,
    }


from reyn.core.offload.canonical import embed_to_canonical  # noqa: E402

register("embed", handle, canonical=embed_to_canonical)
