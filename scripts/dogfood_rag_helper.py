"""dogfood_rag_helper.py — shared setup for batch 17 RAG dogfood scenarios.

Each scenario driver imports this to register a deterministic FakeEmbeddingProvider
(since OPENAI_API_KEY is not set in the dogfood env). Returns 1536-dim vectors
derived from text hash; cosine similarity is meaningless but the pipeline works.

Usage in scenario driver:
    import sys
    sys.path.insert(0, "<reyn_root>/scripts")
    from dogfood_rag_helper import register_fake_embedding_provider, seed_test_files
    register_fake_embedding_provider()
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import struct
from pathlib import Path
from typing import Any


def _deterministic_vector(text: str, dim: int = 1536) -> list[float]:
    """Generate a deterministic 1536-dim vector from text hash.

    Uses SHA-256 expansion to produce *finite* float values in [-1, 1] derived
    from the byte hash (interpreted as uint16 -> [-1, 1] range). Avoids the
    NaN-from-random-float32-bit-pattern trap of older versions. Vectors are
    normalized to unit length so cosine similarity is well-defined.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()  # 32 bytes
    needed = dim * 2  # 2 bytes per float (uint16 -> normalized)
    chunks = []
    for i in range((needed + 31) // 32):
        chunks.append(hashlib.sha256(h + i.to_bytes(4, "big")).digest())
    blob = b"".join(chunks)[:needed]
    # Each pair of bytes -> uint16 -> map to [-1, 1]
    floats = []
    for i in range(0, needed, 2):
        u16 = (blob[i] << 8) | blob[i + 1]
        # Map [0, 65535] to [-1, 1]
        f = (u16 / 32767.5) - 1.0
        floats.append(f)
    # Normalize
    norm = sum(f * f for f in floats) ** 0.5 or 1.0
    return [f / norm for f in floats]


class FakeEmbeddingProvider:
    """Deterministic fake provider for dogfood. Compatible with EmbeddingProvider."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._dim = 1536

    async def embed(self, texts: list[str], model: str) -> dict:
        if not texts:
            return {"vectors": [], "model": model, "total_tokens": 0}
        vectors = [_deterministic_vector(t, self._dim) for t in texts]
        total_tokens = sum(max(1, len(t) // 4) for t in texts)
        return {
            "vectors": vectors,
            "model": f"fake/{model}",
            "total_tokens": total_tokens,
        }

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(max(1, len(t) // 4) for t in texts)

    def get_dimension(self, model: str) -> int:
        return self._dim


def register_fake_embedding_provider() -> None:
    """Register FakeEmbeddingProvider as 'fake' in the embedding registry.

    After this, code that calls `get_provider("fake", config={...})` returns
    a FakeEmbeddingProvider instance.
    """
    from reyn.data.embedding import register_provider
    register_provider("fake", FakeEmbeddingProvider)


def seed_test_files(workspace: Path, files: dict[str, str]) -> None:
    """Seed `files` dict into `workspace` paths.

    Example:
        seed_test_files(workspace, {
            ".reyn/memory/feedback_x.md": "# X\n\ncontent",
            ".reyn/memory/feedback_y.md": "# Y\n\ncontent",
        })
    """
    for relpath, content in files.items():
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def write_dogfood_reyn_yaml(workspace: Path, embedding_provider: str = "fake") -> None:
    """Write a minimal reyn.yaml for dogfood that selects the fake provider.

    The provider registration happens at runtime via `register_fake_embedding_provider`,
    but the config's `embedding.provider` key (if used) directs the op handler to it.
    Also forces small `cost_warn_threshold` for S9.
    """
    yaml_content = """# Dogfood batch 17 — fake embedding provider
model: standard
models:
  light: openai/gemini-2.5-flash-lite
  standard: openai/gemini-2.5-flash-lite
  strong: openai/gemini-2.5-flash-lite

permissions:
  python.pure: allow
  python.trusted: allow
  embed: allow

embedding:
  default_class: standard
  classes:
    light: openai/text-embedding-3-small
    standard: openai/text-embedding-3-small
    strong: openai/text-embedding-3-small
  batch_size: 10
  max_concurrent_batches: 1
  max_retries: 1
  retry_backoff: exponential
  tokenizer: cl100k_base
  cost_warn_threshold: 10000
"""
    (workspace / "reyn.yaml").write_text(yaml_content, encoding="utf-8")


async def write_index_directly(
    workspace: Path,
    source: str,
    description: str,
    path_glob: str,
    chunks_data: list[dict],
) -> None:
    """Bypass index_docs skill and write directly to backend + manifest.

    For S5/S6/S8 which need a pre-seeded source state without re-running the
    full skill (= LLM cost + non-determinism). Returns chunk count written.
    """
    from datetime import datetime, timezone

    from reyn.data.index import SqliteIndexBackend
    from reyn.data.index.source_manifest import SourceEntry, get_source_manifest

    backend = SqliteIndexBackend(workspace)
    write_result = await backend.write(source, iter(chunks_data), mode="replace")

    manifest = get_source_manifest(workspace)
    await manifest.upsert(SourceEntry(
        name=source,
        description=description,
        path=path_glob,
        backend="sqlite",
        last_indexed=datetime.now(timezone.utc).isoformat(),
        chunk_count=write_result["written"],
        embedding_model="fake/standard",
    ))


def make_chunks_for_seed(texts: list[str], source_path: str, source_type: str = "md") -> list[dict]:
    """Build a list of {text, vector, metadata} dicts ready for write_index_directly."""
    chunks = []
    for i, text in enumerate(texts):
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunks.append({
            "text": text,
            "vector": _deterministic_vector(text, 1536),
            "metadata": {
                "source_path": source_path,
                "source_type": source_type,
                "content_hash": h,
                "embedding_model": "fake/standard",
                "chunk_index": i,
                "size_tokens": max(1, len(text) // 4),
                "parent_context": None,
                "extra": {},
            },
        })
    return chunks


# --------------------------------------------------------------------- #
# Sentinel for verification
# --------------------------------------------------------------------- #

REYN_ROOT = Path(__file__).parent.parent


if __name__ == "__main__":
    # Self-test
    register_fake_embedding_provider()
    from reyn.data.embedding import get_provider
    p = get_provider("fake")
    result = asyncio.run(p.embed(["hello", "world"], "standard"))
    assert len(result["vectors"]) == 2
    assert len(result["vectors"][0]) == 1536
    print(f"OK: FakeEmbeddingProvider registered + emitted {len(result['vectors'])} vectors of dim {len(result['vectors'][0])}")
