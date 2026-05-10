"""Default chunker implementations for index_docs stdlib skill.

Each public function is a python preprocessor / postprocessor step that
receives the full artifact dict from the OS harness and returns a
JSON-serializable value that is placed at ``into`` in the artifact.

Override pattern (ADR-0033 §2.1): project-specific chunkers (Python AST,
custom Markdown) replace this module via skill.md ``module:`` override.

All three public functions use ``mode: trusted`` so they can access the
filesystem. ``pure`` mode does not allow ``open()``.
"""
from __future__ import annotations

import glob as _glob_mod
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 preprocessor steps
# ─────────────────────────────────────────────────────────────────────────────


def gather_samples(artifact: dict) -> dict:
    """Phase 1 preprocessor: sample files matched by path glob.

    Receives the full index_docs_input artifact. Returns sample excerpts
    and a file summary for the LLM's strategy decision.

    Returns:
        {
            "samples": [
                {
                    "path": str,
                    "excerpt": str,         # first ~1500 chars
                    "size_tokens": int,     # rough token estimate
                    "structure_hint": str,  # e.g. "Markdown with headings"
                }
            ],
            "summary": {
                "file_count": int,
                "ext_dist":   dict,  # e.g. {".md": 10, ".py": 5}
                "total_bytes": int,
                "mean_bytes":  int,
            },
            "file_count": int,  # top-level convenience copy
        }
    """
    data = artifact.get("data") or {}
    path = str(data.get("path") or "")
    sample_size: int = int(data.get("sample_size") or 5)

    files = _glob_files(path)
    if not files:
        return {
            "samples": [],
            "summary": {
                "file_count": 0,
                "ext_dist": {},
                "total_bytes": 0,
                "mean_bytes": 0,
            },
            "file_count": 0,
        }

    # ── Build extension index + total size ───────────────────────────────────
    by_ext: dict[str, list[str]] = {}
    total_bytes = 0
    for f in files:
        ext = Path(f).suffix
        by_ext.setdefault(ext, []).append(f)
        try:
            total_bytes += Path(f).stat().st_size
        except OSError:
            pass

    # ── Stratified sampling: 1–2 per extension, up to sample_size total ──────
    picks: list[str] = []
    for _ext, file_list in by_ext.items():
        n = min(2, len(file_list))
        picks.extend(file_list[:n])
        if len(picks) >= sample_size:
            picks = picks[:sample_size]
            break
    picks = picks[:sample_size]

    samples = []
    for f in picks:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = text[:1500]
        samples.append(
            {
                "path": f,
                "excerpt": excerpt,
                "size_tokens": _approx_tokens(text),
                "structure_hint": _detect_structure(text, Path(f).suffix),
            }
        )

    n = len(files)
    return {
        "samples": samples,
        "summary": {
            "file_count": n,
            "ext_dist": {ext: len(fs) for ext, fs in by_ext.items()},
            "total_bytes": total_bytes,
            "mean_bytes": total_bytes // n if n else 0,
        },
        "file_count": n,
    }


def cost_preflight(artifact: dict) -> dict:
    """Phase 1 preprocessor: estimate embedding cost (UX gap fix B).

    Receives the artifact after ``gather_samples`` has injected its result
    at ``data.samples_result``. Returns cost estimate fields so the LLM
    can decide to abort if cost is too high.

    Returns:
        {
            "chunk_count":        int,
            "estimated_tokens":   int,
            "estimated_cost_usd": float,
            "model":              str,
            "threshold_exceeded": bool,
        }
    """
    from math import ceil

    data = artifact.get("data") or {}
    path = str(data.get("path") or "")
    samples_result = data.get("samples_result") or {}
    samples = samples_result.get("samples") or []
    threshold = int(data.get("cost_warn_threshold") or 10_000)

    if not samples:
        return {
            "chunk_count": 0,
            "estimated_tokens": 0,
            "estimated_cost_usd": 0.0,
            "model": "standard",
            "threshold_exceeded": False,
        }

    files = _glob_files(path)
    n_files = len(files)

    # Rough estimate: avg tokens per sample → chunks per file
    avg_size = sum(s.get("size_tokens", 0) for s in samples) / len(samples)
    # Assume each file produces ceil(size / 600) chunks (default max_chunk_size)
    chunks_per_file = max(1, ceil(avg_size / 600))
    estimated_chunks = max(1, n_files) * chunks_per_file

    # Cost estimation: ~0.02 USD / 1M tokens for text-embedding-3-small
    # Use sample token counts to extrapolate
    sample_texts = [s.get("excerpt", "") for s in samples]
    sample_tokens = sum(_approx_tokens(t) for t in sample_texts)
    avg_tokens_per_sample = sample_tokens / len(samples) if samples else 0
    total_tokens = int(avg_tokens_per_sample * estimated_chunks)

    rate = 0.02  # USD / 1M tokens (text-embedding-3-small; phase 1 hardcoded)
    cost = total_tokens / 1_000_000 * rate

    return {
        "chunk_count": estimated_chunks,
        "estimated_tokens": total_tokens,
        "estimated_cost_usd": round(cost, 4),
        "model": "standard",
        "threshold_exceeded": estimated_chunks > threshold,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Postprocessor python step
# ─────────────────────────────────────────────────────────────────────────────

_CHUNKS_JSONL_PATH = "artifacts/chunks.jsonl"


def apply_strategy(artifact: dict) -> dict:
    """Postprocessor python step: chunk files per LLM strategy.

    Receives the LLM's finish artifact (= chunk_strategy). The artifact
    echoes skill-input fields (source, path, description, mode).

    Acquires the source-level advisory lock before processing (UX gap fix D).
    Writes chunks to ``artifacts/chunks.jsonl`` in the workspace.

    Returns a summary dict placed at ``data.chunk_stats``:
        {
            "chunk_count":          int,
            "source_lock_acquired": bool,
            "chunks_path":          str,
        }
    """
    # ── Trusted-mode note ─────────────────────────────────────────────────────
    # This step runs with mode=trusted so it can access the filesystem.
    # It writes chunks.jsonl relative to the working directory (which the OS
    # sets to the workspace base_dir before invoking the python step).

    data = artifact.get("data") or {}
    strategy = {
        "boundary": data.get("boundary", "blank_line"),
        "max_chunk_size_tokens": int(data.get("max_chunk_size_tokens") or 600),
        "min_chunk_size_tokens": int(data.get("min_chunk_size_tokens") or 50),
        "overlap_ratio": float(data.get("overlap_ratio") or 0.0),
        "preserve_parent_context": bool(data.get("preserve_parent_context", True)),
    }
    path = str(data.get("path") or "")
    source = str(data.get("source") or "unknown")

    # ── Concurrent lock (UX gap fix D) ───────────────────────────────────────
    import time as _time

    lock_path = Path(".reyn") / "index" / source / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_acquired = False

    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            holder_pid = int(lock_data.get("pid", 0))
            if holder_pid and _pid_alive(holder_pid):
                raise RuntimeError(
                    f"Source '{source}' is currently being indexed by PID"
                    f" {holder_pid}. Wait for completion or kill the holder."
                )
        except (json.JSONDecodeError, ValueError):
            pass  # Corrupted lock — take over

    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": _time.time()}),
        encoding="utf-8",
    )
    lock_acquired = True

    try:
        chunk_count = _write_chunks_jsonl(path, strategy)
    finally:
        # Release lock on completion or error
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    return {
        "chunk_count": chunk_count,
        "source_lock_acquired": lock_acquired,
        "chunks_path": _CHUNKS_JSONL_PATH,
    }


# ─────────────────────────────────────────────────────────────────────────────
# JSONL writer (called by apply_strategy)
# ─────────────────────────────────────────────────────────────────────────────


def _write_chunks_jsonl(path: str, strategy: dict) -> int:
    """Chunk files and write to artifacts/chunks.jsonl. Returns chunk count."""
    boundary = strategy["boundary"]
    max_size = strategy["max_chunk_size_tokens"]
    min_size = strategy.get("min_chunk_size_tokens", 50)
    overlap = strategy.get("overlap_ratio", 0.0)
    preserve = strategy.get("preserve_parent_context", True)

    output_path = Path(_CHUNKS_JSONL_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_idx = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for file_path in _glob_files(path):
            try:
                text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for chunk_text, parent_ctx in _split(
                text, boundary, max_size, min_size, overlap
            ):
                content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                metadata = {
                    "source_path": file_path,
                    "source_type": Path(file_path).suffix.lstrip(".") or "unknown",
                    "content_hash": content_hash,
                    "embedding_model": "",   # filled in by embed op
                    "chunk_index": chunk_idx,
                    "size_tokens": _approx_tokens(chunk_text),
                    "parent_context": parent_ctx if preserve else None,
                    "extra": {},
                }
                record = {"text": chunk_text, "metadata": metadata}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                chunk_idx += 1

    return chunk_idx


# ─────────────────────────────────────────────────────────────────────────────
# Split implementations
# ─────────────────────────────────────────────────────────────────────────────


def _split(
    text: str,
    boundary: str,
    max_size: int,
    min_size: int,
    overlap: float,
) -> Iterator[tuple[str, str | None]]:
    """Yield (chunk_text, parent_context) tuples per strategy."""
    if boundary == "heading":
        yield from _split_heading(text, max_size, min_size, overlap)
    elif boundary == "blank_line":
        yield from _split_blank_line(text, max_size, min_size, overlap)
    elif boundary == "sentence":
        yield from _split_sentence(text, max_size, min_size, overlap)
    else:
        # Unknown boundary — fall back to blank_line
        yield from _split_blank_line(text, max_size, min_size, overlap)


def _split_heading(
    text: str, max_size: int, min_size: int, overlap: float
) -> Iterator[tuple[str, str | None]]:
    """Split at Markdown headings (#, ##); pack each section until max_size."""
    headings = list(re.finditer(r"^(#+)\s+(.+)$", text, re.MULTILINE))
    if not headings:
        yield from _split_blank_line(text, max_size, min_size, overlap)
        return
    for i, h in enumerate(headings):
        section_start = h.start()
        section_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section_text = text[section_start:section_end]
        heading_label = h.group(2).strip()
        if _approx_tokens(section_text) <= max_size:
            if _approx_tokens(section_text) >= min_size:
                yield section_text, heading_label
        else:
            # Large section — sub-split by blank lines
            for sub, _ in _split_blank_line(section_text, max_size, min_size, overlap):
                yield sub, heading_label


def _split_blank_line(
    text: str, max_size: int, min_size: int, overlap: float
) -> Iterator[tuple[str, str | None]]:
    """Split at blank lines; pack paragraphs into chunks <= max_size."""
    paragraphs = re.split(r"\n\s*\n", text)
    current: list[str] = []
    current_size = 0
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_size = _approx_tokens(para)
        if current_size + para_size > max_size and current:
            chunk = "\n\n".join(current)
            if _approx_tokens(chunk) >= min_size:
                yield chunk, None
            current = [para]
            current_size = para_size
        else:
            current.append(para)
            current_size += para_size
    if current:
        chunk = "\n\n".join(current)
        if _approx_tokens(chunk) >= min_size:
            yield chunk, None


def _split_sentence(
    text: str, max_size: int, min_size: int, overlap: float
) -> Iterator[tuple[str, str | None]]:
    """Split at sentence boundaries; pack into chunks <= max_size."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    current: list[str] = []
    current_size = 0
    for sent in sentences:
        s_size = _approx_tokens(sent)
        if current_size + s_size > max_size and current:
            chunk = " ".join(current)
            if _approx_tokens(chunk) >= min_size:
                yield chunk, None
            current = [sent]
            current_size = s_size
        else:
            current.append(sent)
            current_size += s_size
    if current:
        chunk = " ".join(current)
        if _approx_tokens(chunk) >= min_size:
            yield chunk, None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _glob_files(path: str) -> list[str]:
    """Expand a glob pattern; return sorted list of file paths."""
    if not path:
        return []
    matches = _glob_mod.glob(path, recursive=True)
    return sorted(m for m in matches if os.path.isfile(m))


def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)


def _detect_structure(text: str, ext: str) -> str:
    """Heuristic structure hint for LLM strategy context."""
    if ext in {".md", ".markdown", ".mdx"}:
        if re.search(r"^#+\s", text, re.MULTILINE):
            return "Markdown with headings"
        return "Markdown without headings"
    if ext == ".py":
        if re.search(r"^(class |def )", text, re.MULTILINE):
            return "Python with class/function definitions"
        return "Python script"
    if ext in {".js", ".ts", ".tsx", ".jsx"}:
        return "JavaScript/TypeScript"
    if ext in {".json", ".yaml", ".yml", ".toml"}:
        return "Structured data file"
    return "Plain text"


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive without killing it."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
