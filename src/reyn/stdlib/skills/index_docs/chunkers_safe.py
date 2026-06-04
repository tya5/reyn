"""Safe-mode postprocessor steps for index_docs.

Hosts the two postprocessor python steps that run mode: safe —
``extract_and_split`` (glob enumeration) and ``write_chunks_with_lock``
(advisory lock + chunked JSONL write).

FP-0042 Phase 2.1 split the safe-mode preprocessor steps into
``chunkers_preproc_safe.py`` (= ``gather_samples`` + ``cost_preflight``);
Phase 2.2 migrated ``write_chunks_with_lock`` from the pre-FP-0042
``chunkers.py`` (mode: unsafe) into this module. Phase 2.8
(2026-05-23) retired the last unsafe holdout, ``apply_strategy``,
and deleted ``chunkers.py`` entirely. File reads / writes / mkdir /
delete go through :mod:`reyn.safe.file`; PID identity + liveness go
through :mod:`reyn.safe.process`. Path manipulation uses plain string
operations because ``pathlib`` is not on the safe-mode import
allowlist.

R-PURE-MODE-REDEFINE audit (2026-05-15) signed off on ``glob.glob`` as
a restricted ambient source for path-list-only enumeration; that
carveout still governs the ``extract_and_split`` glob call.
"""
from __future__ import annotations

import glob as _glob_mod
import hashlib
import json
import re
import time as _time

from reyn.safe import embed_index as _embed_index
from reyn.safe import file as _safe_file
from reyn.safe import process as _safe_process

# ─── extract_and_split ──────────────────────────────────────────────────────


def extract_and_split(artifact: dict) -> list:
    """Postprocessor python step (mode: safe): glob enum — enumerates source files.

    Receives the LLM's finish artifact (= chunk_strategy). Enumerates files
    matching the path glob and returns an ordered list of source file paths.
    Does NOT read file content — content read is deferred to
    ``write_chunks_with_lock``.

    Glob ownership rationale (R-PURE-MODE audit, 2026-05-15): ``glob.glob``
    exposes filesystem path state (= list of paths matching the pattern) but
    never reads file content. The audit endorsed this as a restricted
    ambient source — see ``docs/deep-dives/audits/2026-05-15-pure-mode-
    stdlib-audit.md`` for the full reasoning.

    No file-type filter: keeping the safe-mode allowlist narrow means
    ``glob.glob`` does not distinguish files from directories, but for
    typed-extension patterns (``**/*.md``, ``**/*.py``, …) directory
    matches are exotic. If one does sneak through, ``write_chunks_with_lock``
    surfaces it via the file read raising ``IsADirectoryError`` /
    ``PermissionError`` (skipped silently in the read loop) — preferable
    to a silent drop here.

    Returns a list of source-file path dicts placed at ``data.chunk_list``:
        [{"source_path": str}, ...]
    """
    data = artifact.get("data") or {}
    path = str(data.get("path") or "")
    if not path:
        return []

    matches = _glob_mod.glob(path, recursive=True)
    return [{"source_path": fp} for fp in sorted(matches)]


# ─── write_chunks_with_lock ─────────────────────────────────────────────────


def write_chunks_with_lock(artifact: dict) -> dict:
    """Postprocessor python step (mode: safe): source-level advisory lock +
    provider-direct embed+index (#1303 Stage I).

    Receives the artifact after ``extract_and_split`` has placed the ordered
    file list at ``data.chunk_list``. Acquires the source-level lock under
    ``.reyn/index/<source>/.lock`` (= default-zone write), reads each source
    file's content (= default-zone read, granted by ``preprocessor_executor``
    via CWD), splits into chunks per strategy, and **streams** the chunks
    straight into :func:`reyn.safe.embed_index.embed_and_index` — which embeds
    them provider-direct and writes the vectors to
    ``.reyn/index/<source>/index.db`` (default-zone write) — then releases the
    lock. There is no intermediate ``<cwd>/artifacts/*.jsonl`` file: the old
    ``embed`` + ``index_write`` run-ops are folded into this one step.

    The lock is recovered as stale when the holder PID is no longer alive
    (= ``reyn.safe.process.pid_alive`` returns False), matching the
    legacy unsafe-mode behaviour. The PID written is the safe-mode
    subprocess's own — when the subprocess exits without releasing
    (= SIGKILL, OOM, parent crash), the lock is reapable on the next run.

    Returns:
        {
            "chunk_count":          int,   # = embedded + skipped_embed
            "source_lock_acquired": bool,
            "embedded":             int,   # chunks newly embedded
            "skipped_embed":        int,   # chunks skipped pre-embed (resume)
            "written":              int,   # chunks written to the index
            "skipped_write":        int,   # dup content_hash at write time
        }
    """
    data = artifact.get("data") or {}
    strategy = {
        "boundary": data.get("boundary", "blank_line"),
        "max_chunk_size_tokens": int(data.get("max_chunk_size_tokens") or 600),
        "min_chunk_size_tokens": int(data.get("min_chunk_size_tokens") or 50),
        "overlap_ratio": float(data.get("overlap_ratio") or 0.0),
        "preserve_parent_context": bool(data.get("preserve_parent_context", True)),
    }
    source = str(data.get("source") or "unknown")
    mode = str(data.get("mode") or "append")
    description = data.get("description")
    path = data.get("path")
    chunk_list = data.get("chunk_list") or []
    file_paths: list[str] = []
    seen: set[str] = set()
    for entry in chunk_list:
        fp = str(entry.get("source_path", ""))
        if fp and fp not in seen:
            file_paths.append(fp)
            seen.add(fp)

    # Lock path: ".reyn/index/<source>/.lock" via plain string join (pathlib
    # is not on the safe-mode import allowlist). The .reyn/ tree is the
    # default write zone, so no extra skill.md declaration is needed for
    # the lock itself.
    lock_dir = f".reyn/index/{source}"
    lock_path = f"{lock_dir}/.lock"

    _safe_file.mkdir(lock_dir, parents=True, exist_ok=True)
    lock_acquired = False

    if _safe_file.exists(lock_path):
        try:
            lock_data = json.loads(_safe_file.read(lock_path))
            holder_pid = int(lock_data.get("pid", 0))
            if holder_pid and _safe_process.pid_alive(holder_pid):
                raise RuntimeError(
                    f"Source '{source}' is currently being indexed by PID"
                    f" {holder_pid}. Wait for completion or kill the holder."
                )
        except (json.JSONDecodeError, ValueError):
            pass  # Corrupted lock — take over.

    _safe_file.write(
        lock_path,
        json.dumps({"pid": _safe_process.getpid(), "ts": _time.time()}),
    )
    lock_acquired = True

    try:
        stats = _embed_index.embed_and_index(
            _iter_chunks_from_paths(file_paths, strategy),
            source,
            "standard",
            mode=mode,
            description=description if isinstance(description, str) else None,
            path=path if isinstance(path, str) else None,
        )
    finally:
        _safe_file.delete(lock_path, missing_ok=True)

    return {
        "chunk_count": stats["embedded"] + stats["skipped_embed"],
        "source_lock_acquired": lock_acquired,
        "embedded": stats["embedded"],
        "skipped_embed": stats["skipped_embed"],
        "written": stats["written"],
        "skipped_write": stats["skipped_write"],
    }


# ─── chunk generator + split helpers ────────────────────────────────────────
#
# Originally duplicated from the (now-deleted) ``chunkers.py``
# ``apply_strategy`` codepath; kept here as the canonical home. Pure regex
# + string ops, no external dependencies.


def _iter_chunks_from_paths(file_paths: list[str], strategy: dict):
    """Yield chunk dicts (``{text, metadata}``) from an ordered file list.

    Reads source files via :mod:`reyn.safe.file` and streams the chunks
    straight into :func:`reyn.safe.embed_index.embed_and_index` — no
    intermediate JSONL file (#1303 Stage I). A generator so a bulk index
    holds only one embed batch in memory at a time. Pure regex + string ops,
    no external dependencies. Unreadable files are silently skipped (matches
    the legacy OSError-swallowing read loop).
    """
    boundary = strategy["boundary"]
    max_size = strategy["max_chunk_size_tokens"]
    min_size = strategy.get("min_chunk_size_tokens", 50)
    overlap = strategy.get("overlap_ratio", 0.0)
    preserve = strategy.get("preserve_parent_context", True)

    chunk_idx = 0
    for file_path in file_paths:
        try:
            text = _safe_file.read(file_path)
        except (OSError, PermissionError):
            continue
        for chunk_text, parent_ctx in _split(
            text, boundary, max_size, min_size, overlap
        ):
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            metadata = {
                "source_path": file_path,
                "source_type": suffix_no_dot(file_path) or "unknown",
                "content_hash": content_hash,
                "embedding_model": "",   # filled in by embed_and_index
                "chunk_index": chunk_idx,
                "size_tokens": approx_tokens(chunk_text),
                "parent_context": parent_ctx if preserve else None,
                "extra": {},
            }
            yield {"text": chunk_text, "metadata": metadata}
            chunk_idx += 1


def suffix_no_dot(path: str) -> str:
    """Return the file extension without the leading dot (e.g. ``foo.md`` → ``md``).

    Replaces ``pathlib.PurePath(path).suffix.lstrip(".")`` which the
    legacy unsafe code used. Pathlib is not in the safe-mode allowlist.
    """
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    idx = name.rfind(".")
    if idx <= 0:
        return ""
    return name[idx + 1:]


def approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)


def _split(
    text: str,
    boundary: str,
    max_size: int,
    min_size: int,
    overlap: float,
):
    """Yield (chunk_text, parent_context) tuples per strategy."""
    if boundary == "heading":
        yield from _split_heading(text, max_size, min_size, overlap)
    elif boundary == "blank_line":
        yield from _split_blank_line(text, max_size, min_size, overlap)
    elif boundary == "sentence":
        yield from _split_sentence(text, max_size, min_size, overlap)
    else:
        yield from _split_blank_line(text, max_size, min_size, overlap)


def _split_heading(text, max_size, min_size, overlap):
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
        if approx_tokens(section_text) <= max_size:
            if approx_tokens(section_text) >= min_size:
                yield section_text, heading_label
        else:
            for sub, _ in _split_blank_line(section_text, max_size, min_size, overlap):
                yield sub, heading_label


def _split_blank_line(text, max_size, min_size, overlap):
    """Split at blank lines; pack paragraphs into chunks <= max_size."""
    paragraphs = re.split(r"\n\s*\n", text)
    current: list[str] = []
    current_size = 0
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_size = approx_tokens(para)
        if current_size + para_size > max_size and current:
            chunk = "\n\n".join(current)
            if approx_tokens(chunk) >= min_size:
                yield chunk, None
            current = [para]
            current_size = para_size
        else:
            current.append(para)
            current_size += para_size
    if current:
        chunk = "\n\n".join(current)
        if approx_tokens(chunk) >= min_size:
            yield chunk, None


def _split_sentence(text, max_size, min_size, overlap):
    """Split at sentence boundaries; pack into chunks <= max_size."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    current: list[str] = []
    current_size = 0
    for sent in sentences:
        s_size = approx_tokens(sent)
        if current_size + s_size > max_size and current:
            chunk = " ".join(current)
            if approx_tokens(chunk) >= min_size:
                yield chunk, None
            current = [sent]
            current_size = s_size
        else:
            current.append(sent)
            current_size += s_size
    if current:
        chunk = " ".join(current)
        if approx_tokens(chunk) >= min_size:
            yield chunk, None
