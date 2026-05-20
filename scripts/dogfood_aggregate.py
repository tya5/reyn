"""Aggregate dogfood batch results — produce aggregate.json + a
past-batch comparison table for the retrospective.md.

Consumes the per-worker JSON files written under
``<journal_dir>/workers/results-worker-{N}.json`` plus the past-batch
aggregate.json files declared in the YAML batch config (see
``dogfood_batch_config.py``). Emits:

  - ``<journal_dir>/aggregate.json``: verdict_totals + per-worker
    breakdown + delta_vs_<prev_batch> + env_settings, in the same
    shape the existing B42 / B43 aggregates use.
  - stdout markdown table: past-batch comparison (= the row a
    retrospective.md typically opens with).

Usage::

    python scripts/dogfood_aggregate.py --config batch.yaml [--write]

  Without ``--write``, the aggregate.json content is printed to stdout
  as JSON (= dry-run). With ``--write``, it's persisted to the
  journal_dir alongside the worker files.

Tier 2 testing seam: ``load_worker_results``, ``compute_totals``,
``build_aggregate``, and ``render_comparison_table`` are independent
pure functions consumed by ``test_dogfood_aggregate.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from dogfood_batch_config import (  # noqa: E402
    BatchConfig,
    PastBatch,
    load_batch_config,
)


def _normalise_verdicts(raw: dict[str, Any] | None) -> dict[str, int]:
    """Worker JSONs use either ``verdicts`` (= V/I/R/B keys) or
    ``counts`` (= same shape). Some B42 files spell out
    ``verified``/``inconclusive``. Normalise to V/I/R/B integers."""
    if raw is None:
        return {"V": 0, "I": 0, "R": 0, "B": 0}
    long_to_short = {
        "verified": "V", "inconclusive": "I",
        "refuted": "R", "blocked": "B",
    }
    out = {"V": 0, "I": 0, "R": 0, "B": 0}
    for k, v in raw.items():
        key = long_to_short.get(k, k.upper() if len(k) == 1 else k)
        if key in out:
            out[key] = int(v)
    return out


def load_worker_results(journal_dir: Path) -> dict[str, dict[str, Any]]:
    """Read every ``results-worker-*.json`` under ``<journal>/workers/``
    and return a dict mapping worker name (= e.g. ``"W1"``) to the
    parsed JSON. Missing files surface a clean error so the user
    knows which worker is incomplete."""
    workers_dir = journal_dir / "workers"
    if not workers_dir.is_dir():
        raise FileNotFoundError(f"workers dir not found: {workers_dir}")
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(workers_dir.glob("results-worker-*.json")):
        stem = p.stem  # results-worker-1
        try:
            n = int(stem.rsplit("-", 1)[1])
        except ValueError:
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}: not valid JSON ({exc})") from exc
        out[f"W{n}"] = data
    if not out:
        raise FileNotFoundError(
            f"no results-worker-*.json files in {workers_dir}"
        )
    return out


def compute_totals(
    worker_results: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Sum the V/I/R/B verdict counts across all workers. Tolerates
    the long-form (``verified`` etc.) and short-form (``V`` etc.) keys
    that historic B42 / B43 results have used."""
    totals = {"V": 0, "I": 0, "R": 0, "B": 0}
    for w in worker_results.values():
        raw = w.get("verdicts") or w.get("counts")
        counts = _normalise_verdicts(raw)
        for k in totals:
            totals[k] += counts[k]
    return totals


def _past_totals(past: PastBatch) -> dict[str, int]:
    path = Path(past.aggregate_path)
    if not path.is_file():
        return {"V": 0, "I": 0, "R": 0, "B": 0}
    data = json.loads(path.read_text())
    return _normalise_verdicts(data.get("verdict_totals"))


def build_aggregate(
    config: BatchConfig,
    worker_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Construct the aggregate.json dict.

    Includes verdict_totals + per-worker breakdown + env_settings +
    delta_vs_<each past batch>. Matches the shape established by B42
    / B43 aggregate.json so downstream consumers (= retrospective.md
    rendering, batch_dispatch citing past verdicts) keep working.
    """
    totals = compute_totals(worker_results)
    n_total = sum(totals.values())
    workers_section: dict[str, Any] = {}
    for w_spec in config.workers:
        wr = worker_results.get(w_spec.name)
        if wr is None:
            workers_section[w_spec.name] = {
                "scenarios": w_spec.n_scenarios,
                "missing": True,
            }
            continue
        counts = _normalise_verdicts(wr.get("verdicts") or wr.get("counts"))
        workers_section[w_spec.name] = {
            "scenarios": w_spec.n_scenarios,
            "v": counts["V"], "i": counts["I"],
            "r": counts["R"], "b": counts["B"],
            "scenario_set": w_spec.scenario_set,
        }
    delta: dict[str, dict[str, Any]] = {}
    for pb in config.past_batches:
        pt = _past_totals(pb)
        delta[f"delta_vs_{pb.name.lower()}"] = {
            f"{pb.name.lower()}_v": pt["V"],
            f"{config.batch.name.lower()}_v": totals["V"],
            "delta_v": totals["V"] - pt["V"],
        }
    return {
        "batch": config.batch.name,
        "date": config.batch.date,
        "head_at_dispatch": config.batch.head,
        "scenarios_total": n_total,
        "verdict_totals": {
            "verified": totals["V"],
            "inconclusive": totals["I"],
            "refuted": totals["R"],
            "blocked": totals["B"],
        },
        "verified_rate": round(totals["V"] / n_total, 3) if n_total else 0.0,
        "env_settings": {
            **{k: v for k, v in config.batch.env_vars.items()},
            **{f"user_param_{k}": v for k, v in config.batch.user_params.items()},
        },
        **delta,
        "workers": workers_section,
    }


def render_comparison_table(
    config: BatchConfig,
    aggregate: dict[str, Any],
) -> str:
    """Markdown comparison table the retrospective.md typically opens
    with — one row per worker, columns for each past batch's V count
    plus the new batch's V count + ΔvsPrev.
    """
    # Header
    past_names = [pb.name for pb in config.past_batches]
    headers = ["Worker", "Scenario set"]
    headers.extend(f"{n} V" for n in past_names)
    headers.append(f"{config.batch.name} V")
    if past_names:
        headers.append(f"Δvs{past_names[0]}")

    # Width sizing
    widths = [len(h) for h in headers]

    rows: list[list[str]] = []
    for w_spec in config.workers:
        ws = aggregate["workers"].get(w_spec.name, {})
        v_now = ws.get("v", 0)
        n_total = ws.get("scenarios", w_spec.n_scenarios)
        row = [w_spec.name, w_spec.scenario_set]
        # Past-batch V counts for this worker
        prev_v: int | None = None
        for pb in config.past_batches:
            pt = _past_verdicts_for_worker(pb, w_spec.name)
            if pt is None:
                row.append("?")
                continue
            row.append(f"{pt['V']}/{pt['total']}")
            if prev_v is None:
                prev_v = pt["V"]
        row.append(f"{v_now}/{n_total}")
        if past_names:
            if prev_v is not None:
                delta_v = v_now - prev_v
                row.append(("+" if delta_v >= 0 else "") + str(delta_v))
            else:
                row.append("?")
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
        rows.append(row)

    sep_line = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    header_line = "| " + " | ".join(
        h.ljust(widths[i]) for i, h in enumerate(headers)
    ) + " |"
    row_lines = [
        "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *row_lines])


def _past_verdicts_for_worker(pb: PastBatch, worker_name: str) -> dict[str, int] | None:
    """Extract V + scenarios total for the named worker from a past
    batch's aggregate.json. Returns None if the past file doesn't have
    the worker (= e.g. scenario set changed between batches)."""
    path = Path(pb.aggregate_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None
    workers = data.get("workers") or {}
    for key, val in workers.items():
        if key.startswith(worker_name + "_") or key == worker_name:
            return {
                "V": int(val.get("v") or val.get("V") or 0),
                "total": int(val.get("scenarios") or 0),
            }
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--config", required=True, type=Path,
        help="YAML batch config (see dogfood_batch_config.py).",
    )
    p.add_argument(
        "--write", action="store_true",
        help="Persist aggregate.json to journal_dir. Without it, "
        "the aggregate JSON is printed to stdout.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    config = load_batch_config(args.config)
    journal = Path(config.journal_dir)
    worker_results = load_worker_results(journal)
    aggregate = build_aggregate(config, worker_results)
    if args.write:
        out_path = journal / "aggregate.json"
        out_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False) + "\n")
        print(f"[aggregate] wrote {out_path}", file=sys.stderr)
    else:
        print(json.dumps(aggregate, indent=2, ensure_ascii=False))
    print("\n--- Comparison table ---\n", file=sys.stderr)
    print(render_comparison_table(config, aggregate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
