"""Safe-mode preprocessor steps for index_docs (FP-0042 Phase 2.1).

This module is the safe-mode home for ``gather_samples`` and
``cost_preflight`` — the two Phase-1 preprocessor python steps that
sample input files and estimate embedding cost so the LLM can choose
a chunk strategy (or abort on cost). Before FP-0042 these lived in
``chunkers.py`` with ``mode: unsafe`` because they called
``reyn.api.unsafe.file``. After FP-0042 they read via
``reyn.safe.file``, which goes through Reyn's permission resolver
per call.

Module split rationale (matches the existing ``chunkers_safe.py``
pattern from R-PURE-MODE-REDEFINE Class A): the safe-mode AST
validator walks every module-level import. ``chunkers.py`` still
contains ``import os`` / ``import reyn.api.unsafe.file`` for the
remaining unsafe-mode steps (``write_chunks_with_lock``,
``apply_strategy``); inheriting those imports would force every
safe-mode step in the same module to fail validation. Splitting the
safe steps into their own file with safe-only imports keeps both
sides clean.

The remaining unsafe step ``write_chunks_with_lock`` will migrate to
safe-mode in a follow-up PR (FP-0042 Phase 2.2) once the
``reyn.safe.file`` surface grows ``delete`` / ``mkdir_parents`` and
the lock mechanism is reworked off ``os.getpid``.

Output-shape contract: the two functions here return the same shape
as the legacy ``chunkers.py`` implementations so the LLM-facing
artifact at ``data.samples_result`` / ``data.cost`` is bit-compatible
across the migration. Field names, key sets, rounding, and the
``_detect_structure`` heuristic all mirror the unsafe-mode versions.
"""
from __future__ import annotations

import glob as _glob_mod
import re as _re
from math import ceil

from reyn.safe import file as _safe_file

# ─── Helpers ────────────────────────────────────────────────────────────────


# POSIX file-type mask + regular-file bit (= stat.S_IFMT / S_IFREG).
# We hard-code the values rather than ``import stat`` because the safe-mode
# allowlist does not include the ``stat`` module. The numbers are stable
# POSIX constants and are also the values CPython's stat.py exposes.
_S_IFMT = 0o170000
_S_IFREG = 0o100000


def _is_regular_file(path: str) -> bool:
    """Return True iff ``path`` exists and is a regular file.

    Replaces the unsafe-mode ``os.path.isfile`` filter — ``os`` is not
    in the safe-mode import allowlist. Uses ``reyn.safe.file.stat``
    whose ``mode`` field is the underlying ``os.stat`` mode int. Any
    error (permission denied, missing file, broken symlink) returns
    False, matching ``os.path.isfile``'s suppress-all-errors behaviour.
    """
    try:
        info = _safe_file.stat(path)
    except (OSError, PermissionError):
        return False
    return (int(info.get("mode", 0)) & _S_IFMT) == _S_IFREG


def _path_suffix(path: str) -> str:
    """Return the lowercase extension including the leading dot.

    Pathlib is not in the safe-mode allowlist, so use plain string
    manipulation. Mirrors ``pathlib.PurePath.suffix`` for the cases
    that matter to ``_detect_structure``:

    - ``foo.md`` → ``.md``
    - ``.hidden`` → ``""`` (hidden file with no real extension)
    - ``.hidden.md`` → ``.md``
    - ``foo`` → ``""``
    """
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    idx = name.rfind(".")
    return name[idx:] if idx > 0 else ""


def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)


def _detect_structure(text: str, ext: str) -> str:
    """Heuristic structure hint for LLM strategy context.

    Mirrors ``chunkers.py``'s unsafe-mode helper exactly so the LLM
    sees identical ``structure_hint`` strings across the migration.
    Duplicated rather than imported because importing back into
    ``chunkers.py`` would re-inherit its unsafe imports under the
    safe-mode AST walk.
    """
    if ext in {".md", ".markdown", ".mdx"}:
        if _re.search(r"^#+\s", text, _re.MULTILINE):
            return "Markdown with headings"
        return "Markdown without headings"
    if ext == ".py":
        if _re.search(r"^(class |def )", text, _re.MULTILINE):
            return "Python with class/function definitions"
        return "Python script"
    if ext in {".js", ".ts", ".tsx", ".jsx"}:
        return "JavaScript/TypeScript"
    if ext in {".json", ".yaml", ".yml", ".toml"}:
        return "Structured data file"
    return "Plain text"


def _glob_files(path: str) -> list[str]:
    """Expand a glob pattern; return sorted regular-file matches.

    Uses stdlib ``glob.glob`` (allowed in safe mode as a restricted
    ambient source per the 2026-05-15 R-PURE-MODE stdlib audit) then
    filters to regular files via ``_is_regular_file`` — this preserves
    the unsafe-mode behaviour where ``os.path.isfile`` filtered
    directories out before sampling.
    """
    if not path:
        return []
    matches = _glob_mod.glob(path, recursive=True)
    return sorted(m for m in matches if _is_regular_file(m))


# ─── Preprocessor steps ─────────────────────────────────────────────────────


def gather_samples(artifact: dict) -> dict:
    """Phase 1 preprocessor: sample files matched by path glob.

    Receives the full ``index_docs_input`` artifact. Returns sample
    excerpts and a file summary for the LLM's strategy decision.

    FP-0042: file content reads + stat calls go through
    ``reyn.safe.file``, which checks the path is under the skill's
    declared read paths (or the workspace default zone). Reads
    outside the declared set raise ``PermissionError`` which the
    loops below treat the same as ``OSError`` (= silent skip,
    matching the legacy ``os.path.isfile`` / ``open`` failure path).
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

    by_ext: dict[str, list[str]] = {}
    total_bytes = 0
    for f in files:
        ext = _path_suffix(f)
        by_ext.setdefault(ext, []).append(f)
        try:
            total_bytes += int(_safe_file.stat(f).get("size", 0))
        except (OSError, PermissionError):
            pass

    # Stratified sampling: 1–2 per extension, up to sample_size total.
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
            text = _safe_file.read(f)
        except (OSError, PermissionError):
            continue
        excerpt = text[:1500]
        samples.append(
            {
                "path": f,
                "excerpt": excerpt,
                "size_tokens": _approx_tokens(text),
                "structure_hint": _detect_structure(text, _path_suffix(f)),
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

    Receives the artifact after ``gather_samples`` has injected its
    result at ``data.samples_result``. Returns cost-estimate fields so
    the LLM can decide to abort if cost is too high.

    FP-0042 note: no file content I/O here — the file_count comes from
    re-globbing the same path the samples were drawn from. ``glob.glob``
    is metadata-only (admitted by the safe-mode allowlist as a
    restricted ambient source); no ``reyn.safe.file`` calls happen, so
    the migration is purely about dropping the
    ``import reyn.api.unsafe.file`` dependency.
    """
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

    avg_size = sum(s.get("size_tokens", 0) for s in samples) / len(samples)
    # Each file produces ceil(size / 600) chunks at the default max_chunk_size.
    chunks_per_file = max(1, ceil(avg_size / 600))
    estimated_chunks = max(1, n_files) * chunks_per_file

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
