"""Run log (JSONL) and RPD state file management."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── RPD state ────────────────────────────────────────────────────────────────

RPD_HARD_CAP = 8_000        # flash requests/day ceiling
RPD_ESTIMATED_PER_RUN = 5  # conservative estimate before actual count is known


def load_rpd_state(out_dir: Path) -> dict:
    path = out_dir / "rpd_state.json"
    if not path.exists():
        return {"total_flash_requests": 0, "updated_at": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"total_flash_requests": 0, "updated_at": ""}


def save_rpd_state(out_dir: Path, state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    (out_dir / "rpd_state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ── Runs log ─────────────────────────────────────────────────────────────────


def load_completed_runs(out_dir: Path) -> set[str]:
    """Return set of run_ids already in runs.jsonl."""
    path = out_dir / "runs.jsonl"
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            run_id = rec.get("run_id")
            if run_id:
                completed.add(run_id)
        except json.JSONDecodeError:
            pass
    return completed


def append_run(out_dir: Path, record: dict) -> None:
    path = out_dir / "runs.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ── Summary ──────────────────────────────────────────────────────────────────


def compute_summary(records: list[dict], phase: str) -> dict:
    total_flash = sum(r.get("flash_requests", 0) or 0 for r in records)
    conditions = ["weak-baseline", "weak-experimental", "strong-experimental"]
    per_condition = {c: _agg(_by(records, "condition", c)) for c in conditions}
    scenarios = sorted({r.get("scenario", "") for r in records if r.get("scenario")})
    per_scenario = {s: _agg(_by(records, "scenario", s)) for s in scenarios}
    return {
        "phase": phase,
        "total_runs": len(records),
        "total_flash_requests": total_flash,
        "per_condition": per_condition,
        "per_scenario": per_scenario,
    }


def _by(records: list[dict], key: str, value: str) -> list[dict]:
    return [r for r in records if r.get(key) == value]


def _mean(lst: list) -> float | None:
    return round(sum(lst) / len(lst), 2) if lst else None


def _agg(recs: list[dict]) -> dict:
    if not recs:
        return {"n": 0}
    n = len(recs)
    ok = [r for r in recs if r.get("status") == "ok"]

    def _scores(field: str) -> list[float]:
        return [
            r["judge_score"][field]
            for r in ok
            if r.get("judge_score") and r["judge_score"].get(field) is not None
        ]

    return {
        "n": n,
        "mean_field_extraction": _mean(_scores("field_extraction")),
        "mean_accuracy": _mean(_scores("accuracy")),
        "mean_utility": _mean(_scores("utility")),
        "empty_stop_count": sum(1 for r in recs if r.get("status") == "empty_stop"),
        "cap_exceeded_count": sum(1 for r in recs if r.get("status") == "cap_exceeded"),
        "mean_calls_per_run": _mean([r.get("calls", 0) or 0 for r in recs]),
    }
