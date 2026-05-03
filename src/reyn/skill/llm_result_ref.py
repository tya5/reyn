"""Workspace-backed reference for large LLM results (R-D10).

Background: when an LLM returns a large response (5–30 KB normal, 50+ KB
for control-IR-heavy phases), the entire payload was being inlined into
the WAL via ``step_completed.result``. Active agents accumulated MB-class
inline payloads in the WAL between phase-truncation events. This hurt
disk usage and replay time on resume.

Solution: above a size threshold (default 32 KB serialized), write the
result to a per-run file under
``<agent_state_dir>/skills/<run_id>_llm_results/<args_hash>.json``
and store a tiny ``{"_ref": "<filename>"}`` placeholder in the WAL. On
memo lookup the placeholder is transparently resolved back to the
original result.

Lifecycle: the per-run llm_results directory is bound to the skill run.
``SkillRegistry.complete()`` removes it alongside the per-skill
snapshot, so cleanup is automatic and matches the snapshot lifecycle.

Filename = args_hash because:
  - args_hash is already computed for the WAL entry
  - same args_hash = same result, so dedup is correct
  - 16 hex chars (64-bit) collision risk negligible per skill run

Threshold rationale: 32 KB is small enough that any "interesting"
response (hand-written control_ir, multi-paragraph artifact) gets
ref'd, while typical short LLM acknowledgements (≤ 1 KB) stay inline
to avoid file-system roundtrips.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# Default size threshold above which results are written to a ref file
# instead of inlined in the WAL. Override per call when needed (tests).
DEFAULT_LLM_RESULT_REF_THRESHOLD: int = 32_768


def llm_results_dir(agent_state_dir: Path, run_id: str) -> Path:
    """Return the per-run llm_results directory path.

    The directory is created lazily by ``write_if_large``; this helper
    only computes the path and does not require the directory to exist.
    """
    return Path(agent_state_dir) / "skills" / f"{run_id}_llm_results"


def write_if_large(
    *,
    agent_state_dir: Path,
    run_id: str,
    args_hash: str,
    result: dict,
    threshold: int = DEFAULT_LLM_RESULT_REF_THRESHOLD,
) -> dict:
    """Return ``result`` unchanged if small, or a ``{"_ref": "<file>"}`` placeholder if large.

    On large input:
      - Writes ``json.dumps(result)`` to
        ``<llm_results_dir>/<args_hash>.json``
      - Returns ``{"_ref": "<args_hash>.json"}``
      - Side-effects: creates the parent directory if missing

    Defensive: on any IO error, logs a warning and returns the original
    result inline (= falls back to old behavior). The WAL entry stays
    correct; only the size-optimization is forfeit.
    """
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        # Result not JSON-serializable; let the WAL append handle it.
        logger.warning("LLM result serialize failed (run=%s): %s", run_id, e)
        return result
    size_bytes = len(serialized.encode("utf-8"))
    if size_bytes <= threshold:
        return result
    d = llm_results_dir(agent_state_dir, run_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{args_hash}.json"
        p.write_text(serialized, encoding="utf-8")
    except OSError as e:
        logger.warning(
            "LLM result ref write failed (run=%s args_hash=%s, %d bytes): %s; "
            "falling back to inline",
            run_id, args_hash, size_bytes, e,
        )
        return result
    return {"_ref": f"{args_hash}.json"}


def resolve(
    *,
    agent_state_dir: Path,
    run_id: str,
    value: Any,
) -> Any:
    """If ``value`` is a ``{"_ref": ...}`` placeholder, load from disk; else return as-is.

    Returns ``None`` when the ref points to a missing file (caller
    should fall through to a fresh LLM call as if memo missed). Handles
    JSON parse errors the same way.
    """
    if not (isinstance(value, dict) and list(value.keys()) == ["_ref"]):
        return value
    rel = value["_ref"]
    if not isinstance(rel, str):
        return None
    p = llm_results_dir(agent_state_dir, run_id) / rel
    if not p.is_file():
        logger.warning(
            "LLM result ref missing (run=%s rel=%s); resume will fall through",
            run_id, rel,
        )
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(
            "LLM result ref load failed (run=%s rel=%s): %s; resume will fall through",
            run_id, rel, e,
        )
        return None


def cleanup_for_run(agent_state_dir: Path, run_id: str) -> None:
    """Remove the per-run llm_results directory if it exists.

    Called by ``SkillRegistry.complete()`` so the lifecycle of ref
    files matches the per-skill snapshot. Defensive: ignores errors so
    a partial state on disk never blocks skill completion.
    """
    d = llm_results_dir(agent_state_dir, run_id)
    if d.is_dir():
        try:
            shutil.rmtree(d)
        except OSError as e:
            logger.warning(
                "LLM result dir cleanup failed (run=%s dir=%s): %s",
                run_id, d, e,
            )
