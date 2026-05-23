"""Deprecated unsafe-mode chunker step for index_docs stdlib skill.

Post-FP-0042 (2026-05-23) this module hosts only the deprecated
``apply_strategy`` step, kept for project-override compatibility. All
active stdlib code paths run mode: safe via the companion modules:

  - ``chunkers_preproc_safe.py`` — Phase-1 preprocessor (gather_samples,
    cost_preflight). Migrated in Phase 2.1.
  - ``chunkers_safe.py`` — postprocessor safe-mode steps
    (extract_and_split, write_chunks_with_lock). The latter migrated in
    Phase 2.2.

This module's ``import os`` / ``from pathlib import Path`` would be
inherited by the safe-mode AST walk if the safe-mode steps lived here,
which is why they live in companion modules. ``apply_strategy``
remains mode: unsafe so its imports are admitted.

Override pattern (ADR-0033 §2.1): project-specific chunkers replace this
module via skill.md ``module:`` override. Existing overrides that
patch ``apply_strategy`` continue to work; new overrides should target
the two-step chain (``extract_and_split`` → ``write_chunks_with_lock``)
in ``chunkers_safe.py``.
"""
from __future__ import annotations

import glob as _glob_mod
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterator

# ─────────────────────────────────────────────────────────────────────────────
# Deprecated postprocessor python step (kept for project-override compat)
# ─────────────────────────────────────────────────────────────────────────────

_CHUNKS_JSONL_PATH = "artifacts/chunks.jsonl"


def apply_strategy(artifact: dict) -> dict:
    """Postprocessor python step: chunk files per LLM strategy.

    Receives the LLM's finish artifact (= chunk_strategy). The artifact
    echoes skill-input fields (source, path, description, mode).

    Acquires the source-level advisory lock before processing (UX gap fix D).
    Writes chunks to ``artifacts/chunks.jsonl`` in the workspace.

    .. deprecated::
        R-PURE-MODE-REDEFINE Class A replaced this monolithic step with the
        two-step chain ``extract_and_split`` (safe) → ``write_chunks_with_lock``
        (unsafe, minimal). This function is kept for override compatibility:
        project skills that override ``apply_strategy`` via ``extends:
        stdlib/index_docs`` continue to work unchanged. New skills should
        prefer the two-step chain in skill.md.

    Returns a summary dict placed at ``data.chunk_stats``:
        {
            "chunk_count":          int,
            "source_lock_acquired": bool,
            "chunks_path":          str,
        }
    """
    # ── Unsafe-mode note ──────────────────────────────────────────────────────
    # This step runs with mode=unsafe so it can access the filesystem.
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
# JSONL writers (called by deprecated apply_strategy)
# ─────────────────────────────────────────────────────────────────────────────


def _write_chunks_jsonl_from_paths(file_paths: list[str], strategy: dict) -> int:
    """Chunk an ordered list of file paths and write to artifacts/chunks.jsonl.

    Called only by ``apply_strategy`` (the deprecated monolithic step).
    The active two-step chain has its own writer in ``chunkers_safe.py``.
    """
    boundary = strategy["boundary"]
    max_size = strategy["max_chunk_size_tokens"]
    min_size = strategy.get("min_chunk_size_tokens", 50)
    overlap = strategy.get("overlap_ratio", 0.0)
    preserve = strategy.get("preserve_parent_context", True)

    output_path = Path(_CHUNKS_JSONL_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_idx = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for file_path in file_paths:
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


def _write_chunks_jsonl(path: str, strategy: dict) -> int:
    """Chunk files matched by glob and write to artifacts/chunks.jsonl.

    Called by ``apply_strategy`` (deprecated monolithic step, mode: unsafe).
    Returns chunk count.
    """
    file_paths = _glob_files(path)
    return _write_chunks_jsonl_from_paths(file_paths, strategy)


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
    """Expand a glob pattern; return sorted list of file paths.

    Used by ``apply_strategy`` (= deprecated monolithic postprocessor
    step, mode: unsafe).
    """
    if not path:
        return []
    matches = _glob_mod.glob(path, recursive=True)
    return sorted(m for m in matches if os.path.isfile(m))


def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive without killing it."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
