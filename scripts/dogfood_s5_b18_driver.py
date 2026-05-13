#!/usr/bin/env python3
"""S5 retest driver — Batch 18 RAG fix retest (N=3).

Runs in the worktree. For each run:
1. Wipe .reyn/index, .reyn/state/sources.yaml, history.jsonl
2. Seed reyn_docs source via write_index_directly() with FakeEmbeddingProvider
3. Run `reyn chat --cui` subprocess with prompt
4. Parse trace + events to detect recall tool invoke + sources field

Verdict criteria (per run):
- verified: recall invoked at least once with sources containing 'reyn_docs',
  AND tool result chunks reflected in LLM reply text
- refuted: recall not invoked OR sources missing reyn_docs OR <ctrl42>
  hallucination (B17-S5-1)
- inconclusive: subprocess error / driver bug / can't determine
- blocked: structural blocker
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = WORKTREE / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(WORKTREE / "src"))

from dogfood_rag_helper import (
    make_chunks_for_seed,
    register_fake_embedding_provider,
    write_index_directly,
)

SEED_TEXTS = [
    "## recall tool\n\nThe recall tool searches indexed sources by query. It accepts query, sources list, and top_k.",
    "## ChunkMetadata\n\nChunkMetadata is the OS-level data carrier. Fields: source_path, content_hash, embedding_model, chunk_index, size_tokens, parent_context, extra.",
    "## index_docs skill\n\nindex_docs runs Phase 1 LLM strategy decision then Skill.postprocessor with python+embed+index_write chain.",
    "## SourceManifest\n\nThe SourceManifest is a per-process singleton tracking indexed sources. Auto-updated on index_docs completion.",
    "## drop_source\n\nThe drop_source tool removes an indexed source. Requires permissions.index_drop and emits an audit event.",
    "## Permission gates\n\npermissions.embed and permissions.index_drop default to ask. Stdlib skill auto-trusts via skill.md declarations.",
    "## IndexBackend protocol\n\nIndexBackend is a Protocol with write/query/drop/stat methods. SqliteIndexBackend is the phase 1 default.",
    "## EmbeddingProvider\n\nEmbeddingProvider abstracts the embedding API. LiteLLMEmbeddingProvider passes through; phase 2 will add local fallback.",
    "## Cost preflight\n\nUX gap fix B: Phase 1 preprocessor estimates indexing cost from samples + total chunk count, gates user when threshold exceeded.",
    "## Override pattern\n\nProject-specific chunkers replace stdlib via skill.md `module:` field. Python AST chunking, custom Markdown formats become 1-file replacements.",
]

PROMPT = "What does the recall tool do? Search the docs."
N_RUNS = 3
AGENT = "default"
TRACE_DIR = Path("/tmp/reyn_s5_b18")


def wipe_state(workspace: Path) -> None:
    """Wipe index + history but keep agent dir + reyn.yaml."""
    paths = [
        workspace / ".reyn" / "index",
        workspace / ".reyn" / "state" / "sources.yaml",
        workspace / ".reyn" / "agents" / AGENT / "history.jsonl",
        workspace / ".reyn" / "events",
    ]
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    # Best-effort singleton reset (only matters if seeding from this same Python process)
    try:
        import reyn.index.source_manifest as sm_mod
        if hasattr(sm_mod, "_MANIFEST_CACHE"):
            sm_mod._MANIFEST_CACHE.clear()
        if hasattr(sm_mod, "_singleton"):
            sm_mod._singleton = None
        if hasattr(sm_mod, "_manifests"):
            sm_mod._manifests.clear()
    except (ImportError, AttributeError):
        pass


async def seed_source(workspace: Path) -> int:
    register_fake_embedding_provider()
    chunks = make_chunks_for_seed(SEED_TEXTS, source_path="docs/concepts/rag.md")
    await write_index_directly(
        workspace=workspace,
        source="reyn_docs",
        description="Reyn concept documentation",
        path_glob="docs/concepts/*.md",
        chunks_data=chunks,
    )
    return len(chunks)


def run_chat_subprocess(workspace: Path, run_id: int, env_extras: dict) -> dict:
    """Run reyn chat --cui via subprocess. Send prompt on stdin then EOF."""
    trace_file = TRACE_DIR / f"run_{run_id}.jsonl"
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    if trace_file.exists():
        trace_file.unlink()

    sitecustomize_dir = SCRIPTS_DIR / "_sitecustomize_fake_embed"
    existing_pp = os.environ.get("PYTHONPATH", "")
    new_pp = f"{sitecustomize_dir}{os.pathsep}{existing_pp}" if existing_pp else str(sitecustomize_dir)
    env = {
        **os.environ,
        "REYN_LLM_TRACE_DUMP": str(trace_file),
        "REYN_EMBEDDING_PROVIDER": "fake",
        "PYTHONPATH": new_pp,
        **env_extras,
    }

    t0 = time.time()
    result = subprocess.run(
        ["reyn", "chat", "--cui"],
        input=f"{PROMPT}\n",
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(workspace),
        env=env,
    )
    elapsed = time.time() - t0

    return {
        "elapsed": round(elapsed, 2),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "trace_file": str(trace_file),
    }


def parse_trace(trace_path: Path) -> dict:
    """Extract LLM reply text + tool calls + system prompt section."""
    out = {
        "reply_text": "",
        "tool_calls": [],
        "indexed_section": "",
        "n_requests": 0,
        "n_responses": 0,
    }
    if not trace_path.exists():
        return out

    lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    last_response_text = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = d.get("kind", "")
        if kind == "request":
            out["n_requests"] += 1
            for m in d.get("messages", []):
                if m.get("role") == "system":
                    content = str(m.get("content", ""))
                    idx = content.find("## Indexed sources")
                    if idx >= 0 and not out["indexed_section"]:
                        out["indexed_section"] = content[idx : idx + 800]
        elif kind == "response":
            out["n_responses"] += 1
            content = d.get("content") or ""
            if content:
                last_response_text = content
            tcs = d.get("tool_calls") or []
            for tc in tcs:
                # Normalize: tool name + args
                if isinstance(tc, dict):
                    name = tc.get("name") or tc.get("function", {}).get("name", "")
                    args = tc.get("args") or tc.get("arguments") or tc.get("function", {}).get("arguments", "")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    out["tool_calls"].append({"name": name, "args": args})

    out["reply_text"] = last_response_text
    return out


def harvest_events(workspace: Path, since_ts: float) -> list[dict]:
    """Read all events for the agent since since_ts."""
    events: list[dict] = []
    d = workspace / ".reyn" / "events" / "agents" / AGENT / "chat"
    if not d.exists():
        return events
    for month_dir in sorted(d.iterdir()):
        if not month_dir.is_dir():
            continue
        for f in sorted(month_dir.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = ev.get("ts", "")
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts_str)
                    ev_ts = dt.timestamp()
                except (ValueError, TypeError):
                    ev_ts = 0.0
                if ev_ts >= since_ts:
                    events.append(ev)
    return events


def classify(parsed: dict, events: list[dict], proc: dict) -> tuple[str, dict]:
    """Determine verdict from trace + events."""
    reply = parsed["reply_text"] or ""
    tool_calls = parsed["tool_calls"]

    # Look for recall in trace tool_calls
    recall_calls_trace = [tc for tc in tool_calls if tc["name"] == "recall"]

    # Also check events
    tool_called_events = [e for e in events if e.get("type") == "tool_called"]
    recall_events = [
        e for e in tool_called_events
        if (e.get("data") or {}).get("tool") == "recall"
    ]

    all_tool_names = sorted({
        *(tc["name"] for tc in tool_calls),
        *((e.get("data") or {}).get("tool") for e in tool_called_events if (e.get("data") or {}).get("tool")),
    })

    # ctrl42 hallucination check
    ctrl42 = "<ctrl" in reply or "default_api." in reply

    # Subprocess error
    if proc["returncode"] != 0 and not reply and not events:
        return "blocked", {"reason": "subprocess_error", "tools": all_tool_names}

    if ctrl42:
        return "refuted", {"reason": "ctrl42_hallucination", "tools": all_tool_names}

    if not recall_calls_trace and not recall_events:
        return "refuted", {"reason": "no_recall_invoke", "tools": all_tool_names}

    # Check sources field
    sources_ok = False
    recall_args_seen = []
    for tc in recall_calls_trace:
        args = tc.get("args") or {}
        if isinstance(args, dict):
            recall_args_seen.append(args)
            srcs = args.get("sources") or []
            if "reyn_docs" in srcs:
                sources_ok = True
    for e in recall_events:
        args = (e.get("data") or {}).get("args") or {}
        if isinstance(args, dict):
            recall_args_seen.append(args)
            srcs = args.get("sources") or []
            if "reyn_docs" in srcs:
                sources_ok = True

    if not sources_ok:
        return "refuted", {
            "reason": "sources_field_missing_reyn_docs",
            "tools": all_tool_names,
            "recall_args": recall_args_seen,
        }

    # Verify reply reflects chunk content
    chunk_kw = ["recall tool", "indexed sources", "query", "sources", "top_k",
                "search", "indexed", "ChunkMetadata"]
    reply_l = reply.lower()
    has_chunk_content = any(kw.lower() in reply_l for kw in chunk_kw)

    if has_chunk_content:
        return "verified", {
            "reason": "recall_with_reyn_docs_and_chunk_content",
            "tools": all_tool_names,
            "recall_args": recall_args_seen,
        }
    return "inconclusive", {
        "reason": "recall_invoked_but_reply_no_chunk_content",
        "tools": all_tool_names,
        "recall_args": recall_args_seen,
    }


async def run_one(run_idx: int, workspace: Path) -> dict:
    print(f"\n{'='*60}", flush=True)
    print(f"[run {run_idx}/{N_RUNS}] starting", flush=True)

    print(f"[run {run_idx}] wiping state...", flush=True)
    wipe_state(workspace)

    print(f"[run {run_idx}] seeding reyn_docs...", flush=True)
    n_chunks = await seed_source(workspace)
    print(f"[run {run_idx}] seeded {n_chunks} chunks", flush=True)

    # Verify seed via CLI
    r = subprocess.run(
        ["reyn", "source", "list"],
        capture_output=True, text=True, cwd=str(workspace),
    )
    seed_ok = "reyn_docs" in r.stdout
    print(f"[run {run_idx}] source list seed_ok={seed_ok}: {r.stdout.strip()[:200]}", flush=True)

    since_ts = time.time()

    print(f"[run {run_idx}] running reyn chat --cui ...", flush=True)
    proc = run_chat_subprocess(workspace, run_idx, env_extras={})
    print(f"[run {run_idx}] elapsed={proc['elapsed']}s rc={proc['returncode']}", flush=True)
    if proc["returncode"] != 0:
        print(f"[run {run_idx}] stderr (last 400): {proc['stderr'][-400:]}", flush=True)

    time.sleep(1.5)

    parsed = parse_trace(Path(proc["trace_file"]))
    events = harvest_events(workspace, since_ts)
    verdict, why = classify(parsed, events, proc)

    print(f"[run {run_idx}] reply_len={len(parsed['reply_text'])} tools={why.get('tools')}", flush=True)
    if parsed["reply_text"]:
        print(f"[run {run_idx}] reply (first 300): {parsed['reply_text'][:300]!r}", flush=True)
    print(f"[run {run_idx}] VERDICT: {verdict.upper()} ({why.get('reason')})", flush=True)

    return {
        "run": run_idx,
        "seed_ok": seed_ok,
        "n_chunks": n_chunks,
        "elapsed": proc["elapsed"],
        "returncode": proc["returncode"],
        "reply_text": parsed["reply_text"],
        "tools_called": why.get("tools", []),
        "recall_args": why.get("recall_args", []),
        "verdict": verdict,
        "reason": why.get("reason"),
        "stderr_tail": proc["stderr"][-400:] if proc["returncode"] != 0 else "",
    }


async def main() -> int:
    workspace = WORKTREE
    print(f"Workspace: {workspace}", flush=True)

    runs = []
    for i in range(1, N_RUNS + 1):
        r = await run_one(i, workspace)
        runs.append(r)
        if i < N_RUNS:
            time.sleep(2)

    verdicts = [r["verdict"] for r in runs]
    counts = Counter(verdicts)

    print("\n" + "=" * 60, flush=True)
    print("S5 RETEST AGGREGATE (Batch 18, N=3)", flush=True)
    print("=" * 60, flush=True)
    invoked = sum(1 for r in runs if any(t == "recall" for t in r["tools_called"]))
    print(f"recall invoked: {invoked}/{N_RUNS}", flush=True)
    for v in ("verified", "refuted", "inconclusive", "blocked"):
        print(f"  {v}: {counts.get(v, 0)}/{N_RUNS}", flush=True)

    out = {
        "scenario": "S5_retest",
        "batch": 18,
        "n": N_RUNS,
        "prompt": PROMPT,
        "runs": runs,
        "counts": dict(counts),
        "recall_invoke_rate": f"{invoked}/{N_RUNS}",
    }
    out_path = workspace / "artifacts" / "s5_b18_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
