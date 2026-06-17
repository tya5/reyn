"""reyn.eval.result_loader — load eval result files written by ``reyn eval run``.

Result files live at:
    ``.reyn/eval-results/<skill_name>/<YYYYMMDDTHHMMSSZ>.jsonl``

Each line is a JSON record with at minimum:
    case_id           (str)
    pass              (bool)
    score             (float)
    skill_version_hash (str | None)
    tags              (list[str])

A *run result* wraps a file's records into a dict suitable for ``compute_diff``:
    run_id            — derived from the filename stem (timestamp)
    skill_version_hash — from the first record that carries a non-None value,
                         or "unknown" when all records have None
    timestamp          — ISO-8601 approximation from the filename stem
    cases             — list of {case_id, score, ...} dicts (one per line)
"""
from __future__ import annotations

import json
from pathlib import Path

# ── public API ────────────────────────────────────────────────────────────────


def load_runs_for_skill(skill_name: str, results_dir_template: str) -> list[dict]:
    """Return all eval run results for *skill_name*, newest first.

    Parameters
    ----------
    skill_name:
        The skill whose result directory to scan.
    results_dir_template:
        Template string with ``{skill}`` placeholder, e.g.
        ``".reyn/eval-results/{skill}"``.

    Returns
    -------
    list[dict] sorted by filename descending (newest first).  Each dict has
    the shape described in the module docstring.
    """
    results_dir = Path(results_dir_template.format(skill=skill_name))
    if not results_dir.exists():
        return []

    files = sorted(results_dir.glob("*.jsonl"), key=lambda p: p.name, reverse=True)
    runs = []
    for f in files:
        run = _load_run_file(f)
        if run is not None:
            runs.append(run)
    return runs


def load_run_by_id(
    skill_name: str,
    run_id: str,
    results_dir_template: str,
) -> dict | None:
    """Load a single run by run_id (filename stem) or skill_version_hash prefix.

    Returns None if no matching run is found.
    """
    all_runs = load_runs_for_skill(skill_name, results_dir_template)
    for run in all_runs:
        if run["run_id"] == run_id:
            return run
        # Allow prefix matching on skill_version_hash
        svh = run.get("skill_version_hash") or ""
        if svh != "unknown" and svh.startswith(run_id):
            return run
    return None


# ── internal ──────────────────────────────────────────────────────────────────


def _load_run_file(path: Path) -> dict | None:
    """Parse a single JSONL result file into a run dict.

    Returns None if the file is empty or unreadable.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    cases: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not cases:
        return None

    # Extract skill_version_hash from the first record that has a non-None value.
    skill_version_hash: str = "unknown"
    for case in cases:
        svh = case.get("skill_version_hash")
        if svh:
            skill_version_hash = str(svh)
            break

    return {
        "run_id": path.stem,
        "skill_version_hash": skill_version_hash,
        "timestamp": _stem_to_iso(path.stem),
        "cases": cases,
    }


def _stem_to_iso(stem: str) -> str:
    """Convert ``20260514T213000Z`` → ``2026-05-14T21:30:00Z`` (best-effort)."""
    try:
        bare = stem.rstrip("Z")[:15]
        # Parse YYYYMMDDTHHMMSS
        year = bare[0:4]
        month = bare[4:6]
        day = bare[6:8]
        hour = bare[9:11]
        minute = bare[11:13]
        second = bare[13:15]
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}Z"
    except (IndexError, ValueError):
        return stem
