#!/usr/bin/env python3
"""Dogfood batch 18 — S6 retest driver: Multi-source recall.

Pre-seeds TWO sources (`reyn_docs` + `reyn_src`) via `write_index_directly`,
registers `FakeEmbeddingProvider`, then invokes `reyn chat --cui` with the
prompt "How is recall implemented?". For each of N=3 fresh runs we:

1. Wipe `<workspace>/.reyn/agents/default/history.jsonl`.
2. Spawn `reyn chat --cui` subprocess with `REYN_EMBEDDING_PROVIDER=fake`
   and `REYN_LLM_TRACE_DUMP=/tmp/reyn_s6_b18/run_<i>.jsonl`.
3. Inspect the trace for `recall` tool_call args; record `sources` field.

Verdict criteria:
  verified     — recall invoked; sources contains BOTH reyn_docs AND reyn_src.
  refuted      — recall not invoked, OR sources has only one source.
  inconclusive — driver/subprocess error.
  blocked      — structural blocker (e.g. tool absent from catalog).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REYN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REYN_ROOT / "scripts"))

from dogfood_rag_helper import (  # noqa: E402
    make_chunks_for_seed,
    register_fake_embedding_provider,
    write_dogfood_reyn_yaml,
    write_index_directly,
)

WORKSPACE = Path("/tmp/reyn_s6_b18_ws")
TRACE_DIR = Path("/tmp/reyn_s6_b18")
BOOTSTRAP_DIR = Path("/tmp/reyn_s6_b18_bootstrap")
PROMPT = "How is recall implemented?"
N_RUNS = 3

DOC_TEXTS = [
    "The `recall` tool performs semantic search over indexed sources. "
    "It accepts `query: str`, `sources: list[str]` (optional, defaults to all), "
    "and `top_k: int` (default 5). Returns matching chunks with score and metadata.",
    "ChunkMetadata describes a single chunk: source_path, source_type, "
    "content_hash, embedding_model, chunk_index, size_tokens, parent_context, extra.",
    "SourceManifest is the registry of indexed sources, persisted at "
    "`.reyn/index/sources.yaml`. Each entry has name, description, path, backend, "
    "chunk_count, last_indexed, embedding_model.",
    "EmbeddingProvider is a Protocol with `embed(texts, model)` returning a list "
    "of vectors. Providers are registered by name (`openai`, `gemini`, `fake`).",
    "IndexBackend is the persistence layer (sqlite default). It exposes "
    "`write(source, chunks, mode)`, `query(source, vector, top_k)`, and "
    "`drop(source)` for CRUD against indexed chunks.",
    "The `index_docs` skill orchestrates: glob expansion → chunking → "
    "embedding (cost-gated via cost_warn_threshold) → backend.write → "
    "SourceManifest.upsert. Permission: `index_write`.",
]

SRC_TEXTS = [
    "def handle_recall(args, ctx): query = args['query']; sources = args.get('sources') "
    "or [s.name for s in ctx.manifest.list()]; top_k = args.get('top_k', 5); "
    "vec = await ctx.embedder.embed([query], 'standard'); "
    "chunks = await ctx.backend.query_many(sources, vec[0], top_k); return {'chunks': chunks}",
    "def handle_embed(args, ctx): texts = args['texts']; model = args.get('model', 'standard'); "
    "provider = get_provider(ctx.config.embedding.provider); "
    "result = await provider.embed(texts, model); return {'vectors': result['vectors']}",
    "def handle_index_query(args, ctx): source = args['source']; vector = args['vector']; "
    "top_k = args.get('top_k', 5); rows = await ctx.backend.query(source, vector, top_k); "
    "return {'rows': rows}",
    "def handle_index_write(args, ctx): source = args['source']; chunks = args['chunks']; "
    "mode = args.get('mode', 'append'); result = await ctx.backend.write(source, iter(chunks), mode); "
    "await ctx.manifest.upsert(SourceEntry(name=source, ...)); return result",
    "def handle_index_drop(args, ctx): source = args['source']; await ctx.backend.drop(source); "
    "await ctx.manifest.remove(source); return {'dropped': True}",
    "from reyn.index import SqliteIndexBackend; from reyn.index.source_manifest import "
    "get_source_manifest, SourceEntry; from reyn.embedding import get_provider, register_provider",
]


def reset_workspace() -> None:
    """Reset the workspace dir to a clean state."""
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)


def clean_history() -> None:
    history = WORKSPACE / ".reyn" / "agents" / "default" / "history.jsonl"
    if history.exists():
        history.unlink()


async def seed_two_sources() -> None:
    """Pre-seed reyn_docs + reyn_src with FakeEmbeddingProvider."""
    register_fake_embedding_provider()
    docs_chunks = make_chunks_for_seed(DOC_TEXTS, "docs/recall.md", "md")
    src_chunks = make_chunks_for_seed(SRC_TEXTS, "src/reyn/recall.py", "py")
    await write_index_directly(
        WORKSPACE,
        source="reyn_docs",
        description="Reyn documentation about recall, embedding, and indexing.",
        path_glob="docs/**/*.md",
        chunks_data=docs_chunks,
    )
    await write_index_directly(
        WORKSPACE,
        source="reyn_src",
        description="Reyn source code: recall handler, embedding, index handlers.",
        path_glob="src/**/*.py",
        chunks_data=src_chunks,
    )


def setup_bootstrap() -> None:
    """Write a sitecustomize.py that auto-registers FakeEmbeddingProvider in
    every Python process started with BOOTSTRAP_DIR on PYTHONPATH.
    """
    BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
    sc = BOOTSTRAP_DIR / "sitecustomize.py"
    helper_path = REYN_ROOT / "scripts"
    sc.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(helper_path)!r})\n"
        "try:\n"
        "    from dogfood_rag_helper import register_fake_embedding_provider\n"
        "    register_fake_embedding_provider()\n"
        "except Exception as e:\n"
        "    import sys as _s; print('[bootstrap] fake provider register failed:', e, file=_s.stderr)\n"
    )


def ensure_openai_key_in_env() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        out = subprocess.run(
            ["zsh", "-lic", "echo -n $OPENAI_API_KEY"],
            capture_output=True, text=True, timeout=10,
        )
        key = out.stdout.strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
    except Exception:
        pass


def setup_workspace() -> None:
    reset_workspace()
    setup_bootstrap()
    ensure_openai_key_in_env()
    write_dogfood_reyn_yaml(WORKSPACE)
    # Copy reyn.local.yaml for proxy api_base (LiteLLM at localhost:4000).
    src_local = REYN_ROOT / "reyn.local.yaml"
    if src_local.exists():
        (WORKSPACE / "reyn.local.yaml").write_text(src_local.read_text())
    # Initialize default agent
    subprocess.run(
        ["reyn", "agent", "new", "default"],
        cwd=str(WORKSPACE),
        capture_output=True,
        timeout=30,
    )
    asyncio.run(seed_two_sources())
    # Verify
    out = subprocess.run(
        ["reyn", "source", "list"],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(f"[setup] source list output:\n{out.stdout}")


def run_chat_turn(run_id: int) -> dict:
    trace_file = TRACE_DIR / f"run_{run_id}.jsonl"
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    if trace_file.exists():
        trace_file.unlink()

    existing_pp = os.environ.get("PYTHONPATH", "")
    pp = str(BOOTSTRAP_DIR) + (os.pathsep + existing_pp if existing_pp else "")
    env = {
        **os.environ,
        "REYN_EMBEDDING_PROVIDER": "fake",
        "REYN_LLM_TRACE_DUMP": str(trace_file),
        "PYTHONPATH": pp,
    }

    start = time.time()
    result = subprocess.run(
        ["reyn", "chat", "--cui"],
        input=f"{PROMPT}\n",
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(WORKSPACE),
        env=env,
    )
    elapsed = time.time() - start

    # Parse trace for tool calls + system prompt fragment
    recall_called = False
    recall_sources: list[str] | None = None
    all_tool_calls: list[dict] = []
    sp_indexed_section = ""
    catalog_has_recall = False

    if trace_file.exists():
        for line in trace_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            kind = d.get("kind", "")
            if kind == "request":
                # Inspect tools array for recall presence
                tools = d.get("tools") or []
                for t in tools:
                    fn = t.get("function") or {}
                    if fn.get("name") == "recall":
                        catalog_has_recall = True
                # Inspect system prompt for indexed sources section
                for m in d.get("messages", []):
                    if m.get("role") == "system":
                        c = str(m.get("content", ""))
                        idx = c.find("## Indexed sources")
                        if idx >= 0 and not sp_indexed_section:
                            sp_indexed_section = c[idx : idx + 600]
            elif kind == "response":
                tcs = d.get("tool_calls") or []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments", "")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                    except Exception:
                        args = {}
                    all_tool_calls.append({"name": name, "args": args})
                    if name == "recall":
                        recall_called = True
                        recall_sources = args.get("sources")

    return {
        "run_id": run_id,
        "elapsed": round(elapsed, 1),
        "returncode": result.returncode,
        "recall_called": recall_called,
        "recall_sources": recall_sources,
        "all_tool_calls": all_tool_calls,
        "sp_indexed_section": sp_indexed_section[:300],
        "catalog_has_recall": catalog_has_recall,
        "stderr": result.stderr[-400:],
        "stdout_tail": result.stdout[-300:],
    }


def assess_verdict(obs: dict) -> str:
    if obs["returncode"] != 0 and not obs["recall_called"]:
        # Subprocess failed without recall ever happening
        if "Traceback" in obs["stderr"] or "error" in obs["stderr"].lower()[:120]:
            return "inconclusive"
    if not obs["catalog_has_recall"]:
        return "blocked"
    if not obs["recall_called"]:
        return "refuted"
    sources = obs["recall_sources"]
    if not isinstance(sources, list):
        # recall called but no sources field — refuted (single-source default may be the issue)
        return "refuted"
    has_docs = "reyn_docs" in sources
    has_src = "reyn_src" in sources
    if has_docs and has_src:
        return "verified"
    return "refuted"


def main() -> None:
    print("=" * 60)
    print("S6 retest (Batch 18): Multi-source recall")
    print(f"Workspace: {WORKSPACE}")
    print(f"Trace dir: {TRACE_DIR}")
    print("=" * 60)

    setup_workspace()

    results = []
    for run_id in range(1, N_RUNS + 1):
        print(f"\n[Run {run_id}/{N_RUNS}] Cleaning history, running chat...")
        clean_history()
        obs = run_chat_turn(run_id)
        verdict = assess_verdict(obs)
        obs["verdict"] = verdict
        print(f"  Elapsed: {obs['elapsed']}s  rc={obs['returncode']}")
        print(f"  catalog_has_recall: {obs['catalog_has_recall']}")
        print(f"  recall_called: {obs['recall_called']}")
        print(f"  recall_sources: {obs['recall_sources']}")
        print(f"  all_tool_calls: {[(tc['name'], list(tc['args'].keys())) for tc in obs['all_tool_calls']]}")
        print(f"  Verdict: {verdict}")
        results.append(obs)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    verdicts = [r["verdict"] for r in results]
    for v in ("verified", "refuted", "inconclusive", "blocked"):
        c = verdicts.count(v)
        print(f"  {v}: {c}/{N_RUNS}")

    out_path = Path("/tmp/s6_b18_results.json")
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
