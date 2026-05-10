"""embed op handler — embed texts via EmbeddingProvider (ADR-0033 Phase 1).

Two input forms:
  Form A (inline): op.texts is set → embed directly, return vectors inline.
  Form B (artifact): op.input_artifact is set → stream JSONL, batch embed,
    write output_artifact. Idempotent: skips chunks whose content_hash
    already appears in the output_artifact.

Progress events are emitted per batch (UX gap fix C, ADR-0033 §2.1).

Config access: reads ctx.workspace.base_dir to locate artifacts; embedding
config is read from os.environ / global default (phase 1 minimal: uses
LiteLLMEmbeddingProvider with empty config dict). A full config wiring
path (ctx → reyn.yaml embedding section) is wired in Wave 2G (CLI plumbing);
for phase 1 test-coverage the FakeEmbeddingProvider fixture is used.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from reyn.embedding import get_provider
from reyn.schemas.models import EmbedIROp

from . import register
from .context import OpContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_jsonl_lines(path: Path) -> int:
    """Count non-empty lines in a JSONL file. Returns 0 if file absent."""
    if not path.exists():
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


async def _flush_batch(
    batch: list[dict],
    provider: object,
    model: str,
    f_out: object,
    text_field: str,
) -> None:
    """Embed a batch and append results to f_out."""
    texts = [c[text_field] for c in batch]
    result = await provider.embed(texts, model=model)
    for chunk, vector in zip(batch, result["vectors"]):
        # Update embedding_model in metadata
        meta = chunk.get("metadata", {})
        meta["embedding_model"] = result["model"]
        out = {**chunk, "metadata": meta, "vector": vector}
        f_out.write(json.dumps(out, ensure_ascii=False) + "\n")


async def _embed_artifact_form(
    op: EmbedIROp,
    ctx: OpContext,
    provider: object,
) -> dict:
    """Stream JSONL input → batch embed → write JSONL output (Form B).

    Idempotent: scans output_artifact for existing content_hash, skips them.
    Emits embed_progress events per batch (UX gap fix C).
    """
    workspace = ctx.workspace.base_dir
    input_path = workspace / op.input_artifact   # type: ignore[operator]
    output_path = workspace / op.output_artifact  # type: ignore[operator]

    # 1. Load existing content_hashes from output (idempotent re-run)
    seen_hashes: set[str] = set()
    if output_path.exists():
        with open(output_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    ch = rec.get("metadata", {}).get("content_hash")
                    if ch:
                        seen_hashes.add(ch)
                except (json.JSONDecodeError, KeyError):
                    continue

    # 2. Stream input + batch process
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not input_path.exists():
        raise FileNotFoundError(
            f"embed op: input_artifact not found: {op.input_artifact!r}"
        )

    # Determine batch_size from provider config if available; default 100
    batch_size: int = 100
    try:
        batch_size = int(provider._batch_size)  # type: ignore[attr-defined]
    except AttributeError:
        pass

    batch: list[dict] = []
    embedded_count = 0
    skipped_count = 0
    total_chunks = _count_jsonl_lines(input_path)
    text_field = op.text_field

    with open(input_path, encoding="utf-8") as f_in, open(output_path, "a", encoding="utf-8") as f_out:
        for line in f_in:
            if not line.strip():
                continue
            chunk = json.loads(line)
            chash = chunk.get("metadata", {}).get("content_hash", "")
            if chash and chash in seen_hashes:
                skipped_count += 1
                continue
            batch.append(chunk)
            if len(batch) >= batch_size:
                await _flush_batch(batch, provider, op.model, f_out, text_field)
                embedded_count += len(batch)
                batch = []
                # Emit progress event (UX gap fix C)
                done = embedded_count + skipped_count
                pct = int(done / total_chunks * 100) if total_chunks else 100
                ctx.events.emit(
                    "embed_progress",
                    embedded=embedded_count,
                    skipped=skipped_count,
                    total=total_chunks,
                    pct=pct,
                )
        if batch:
            await _flush_batch(batch, provider, op.model, f_out, text_field)
            embedded_count += len(batch)

    return {"embedded_count": embedded_count, "skipped_count": skipped_count}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle(
    op: EmbedIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute an embed op (ADR-0033 §2.1).

    Returns:
      Form A: {vectors: list[list[float]], total_tokens: int, model: str}
      Form B: {embedded_count: int, skipped_count: int}
    """
    # Input validation
    if op.texts is not None and op.input_artifact is not None:
        raise ValueError("EmbedIROp: only one of texts / input_artifact may be set")
    if op.texts is None and op.input_artifact is None:
        raise ValueError("EmbedIROp: one of texts / input_artifact must be set")

    # Instantiate provider — phase 1 always litellm; config injected via env.
    # OpContext does not yet expose embedding config directly (Wave 2G wires it);
    # for now use empty config (provider reads LITELLM_API_BASE from env).
    # REYN_EMBEDDING_PROVIDER env var allows dogfood/test overrides (e.g. "fake").
    import os as _os
    _provider_name = _os.environ.get("REYN_EMBEDDING_PROVIDER", "litellm")
    provider = get_provider(_provider_name, config={})

    if op.texts is not None:
        # Form A: inline embed
        if len(op.texts) == 0:
            return {"vectors": [], "total_tokens": 0, "model": op.model}
        result = await provider.embed(op.texts, model=op.model)
        return {
            "vectors": result["vectors"],
            "total_tokens": result["total_tokens"],
            "model": result["model"],
        }

    # Form B: artifact reference
    return await _embed_artifact_form(op, ctx, provider)


register("embed", handle)
