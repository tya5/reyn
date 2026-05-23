#!/usr/bin/env python3
"""Dogfood driver for the ``skill_search → skill_importer`` chain.

Measures the cumulative effect of the under-trigger + skill_importer
quality cascade (PRs #551, #555, #560, #564, #567, #569, #572, #576,
#578, #583). Each run:

  1. Sets up an isolated tmp workspace with the fixture's ``reyn.yaml``
     + ``reyn.local.yaml`` (= permissions + LiteLLM proxy).
  2. ``reyn run skill_search '{"text": "<query>"}'`` — gets a candidate
     list against the default registry (= ``anthropics/skills``).
  3. Picks the FIRST candidate's ``source_url``.
  4. ``reyn run skill_importer '{"text": "Import the skill at <url>"}'``
     — actual import + lint.
  5. Reads the artifacts left on disk:
     - ``.reyn/artifacts/skill_search/search/v01_skill_candidate_list.json``
     - ``.reyn/artifacts/skill_importer/convert/v01_skill_import_result.json``
     - ``reyn/local/<name>/skill.md`` (= for description preservation check)

Per-run measurements:

  - ``candidate_name``     — what skill_search picked first
  - ``lint_passed``        — bool from skill_importer's result artifact
  - ``phase_count``        — len(result.phases)
  - ``phases``             — list of phase names
  - ``description_chars``  — length of the imported description field
                             (= proxy for verbatim preservation)
  - ``has_artifacts_dir``  — whether ``reyn/local/<name>/artifacts/``
                             was created (= PR #576 wiring check)
  - ``elapsed_s``          — wall clock for the run

Aggregates across N runs:
  - lint_pass_rate
  - mean_phase_count + distribution (= PR #583 decomposition
    discipline empirical signal)
  - mean_description_chars
  - artifacts_dir_present_rate

Usage:
    python dogfood/scripts/run_skill_importer_chain.py \\
        [--query "PDF"] [--n 5] [--timeout 300]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "dogfood" / "fixtures" / "skill_importer_chain"


def setup_workspace(target: Path) -> None:
    """Copy fixture + project reyn.local.yaml into ``target``."""
    shutil.copy2(FIXTURE_DIR / "reyn.yaml", target / "reyn.yaml")
    project_local = REPO_ROOT / "reyn.local.yaml"
    if project_local.exists():
        shutil.copy2(project_local, target / "reyn.local.yaml")


def run_reyn(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Invoke ``reyn`` with given args and capture stdout / stderr."""
    env = {**os.environ}
    return subprocess.run(
        ["reyn", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=env,
    )


def read_artifact_json(path: Path) -> dict | None:
    """Read a Reyn artifact JSON file; return None on miss."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def extract_description(skill_md: Path) -> tuple[str, int]:
    """Parse a skill.md frontmatter for the ``description`` field.

    Returns (description_text, char_count). Empty string + 0 on failure.
    """
    if not skill_md.exists():
        return "", 0
    text = skill_md.read_text()
    if not text.startswith("---"):
        return "", 0
    end = text.find("---", 3)
    if end == -1:
        return "", 0
    fm = text[3:end]
    # Match both ``description: <one-line>`` and the block-scalar
    # ``description: |\n  <multi-line>`` forms.
    block_m = re.search(
        r"^description:\s*\|\s*\n((?:[ \t]+.*\n?)+)",
        fm, re.MULTILINE,
    )
    if block_m:
        lines = block_m.group(1).splitlines()
        # Strip leading indent (= 2 spaces or 4 spaces; pick the min).
        stripped = [ln.lstrip() for ln in lines]
        desc = " ".join(s for s in stripped if s).strip()
        return desc, len(desc)
    inline_m = re.search(
        r"^description:\s*(.+?)(?:\n[a-zA-Z_-]+:|\n---)",
        fm, re.DOTALL | re.MULTILINE,
    )
    if inline_m:
        desc = " ".join(inline_m.group(1).split()).strip()
        return desc, len(desc)
    return "", 0


def run_one(idx: int, query: str, timeout: int, keep_workspace: bool) -> dict:
    """One full chain trial: workspace setup → skill_search → skill_importer."""
    workspace = Path(
        tempfile.mkdtemp(prefix=f"skill_chain_r{idx}_"),
    )
    try:
        setup_workspace(workspace)

        # ── Step A: skill_search ─────────────────────────────────────
        start_a = time.time()
        proc_a = run_reyn(
            ["run", "skill_search", json.dumps({"text": query})],
            workspace, timeout,
        )
        elapsed_a = round(time.time() - start_a, 1)

        candidate_path = (
            workspace / ".reyn" / "artifacts" / "skill_search"
            / "search" / "v01_skill_candidate_list.json"
        )
        candidate_artifact = read_artifact_json(candidate_path) or {}
        # Reyn artifacts wrap data: { type, data: { candidates: [...] } }
        candidates_list = (
            (candidate_artifact.get("data") or {}).get("candidates")
            or candidate_artifact.get("candidates")
            or []
        )
        if not candidates_list:
            return {
                "run": idx,
                "query": query,
                "stage": "search",
                "error": "no candidates returned",
                "search_stderr_tail": (proc_a.stderr or "")[-600:],
                "elapsed_s": elapsed_a,
            }
        first = candidates_list[0]
        source_url = first.get("source_url", "")
        candidate_name = first.get("name", "")

        # ── Step B: skill_importer ───────────────────────────────────
        start_b = time.time()
        proc_b = run_reyn(
            ["run", "skill_importer",
             json.dumps({"text": f"Import the skill at {source_url}"})],
            workspace, timeout,
        )
        elapsed_b = round(time.time() - start_b, 1)

        importer_path = (
            workspace / ".reyn" / "artifacts" / "skill_importer"
            / "convert" / "v01_skill_import_result.json"
        )
        importer_artifact = read_artifact_json(importer_path) or {}
        # Same data wrapper as skill_search artifact.
        importer_result = (
            importer_artifact.get("data") or importer_artifact
        )

        skill_name = importer_result.get("skill_name", "")
        lint_passed = importer_result.get("lint_passed")
        phases = importer_result.get("phases") or []

        imported_dir = workspace / "reyn" / "local" / skill_name
        skill_md = imported_dir / "skill.md"
        description, desc_chars = extract_description(skill_md)
        artifacts_dir = imported_dir / "artifacts"

        return {
            "run": idx,
            "query": query,
            "candidate_name": candidate_name,
            "skill_name": skill_name,
            "source_url": source_url,
            "lint_passed": lint_passed,
            "phase_count": len(phases),
            "phases": phases,
            "description_chars": desc_chars,
            "description_head": description[:140],
            "has_artifacts_dir": artifacts_dir.is_dir(),
            "elapsed_search_s": elapsed_a,
            "elapsed_importer_s": elapsed_b,
            "elapsed_total_s": round(elapsed_a + elapsed_b, 1),
            "importer_stderr_tail":
                (proc_b.stderr or "")[-200:] if proc_b.returncode else "",
        }
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="PDF",
                    help="Search query for skill_search (default: 'PDF')")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--keep-workspace", action="store_true")
    args = ap.parse_args()

    print(f"[query] {args.query!r}  [n] {args.n}", flush=True)
    results = []
    for i in range(1, args.n + 1):
        print(f"[run {i}/{args.n}] starting ...", flush=True)
        r = run_one(i, args.query, args.timeout, args.keep_workspace)
        results.append(r)
        if r.get("error"):
            print(f"[run {i}/{args.n}] ERROR at {r['stage']}: {r['error']}",
                  flush=True)
        else:
            print(
                f"[run {i}/{args.n}] "
                f"skill={r['skill_name']} "
                f"lint={r['lint_passed']} "
                f"phases={r['phase_count']} "
                f"desc_chars={r['description_chars']} "
                f"artifacts_dir={r['has_artifacts_dir']} "
                f"t={r['elapsed_total_s']}s",
                flush=True,
            )

    n = len(results)
    successful = [r for r in results if not r.get("error")]
    n_lint_pass = sum(1 for r in successful if r.get("lint_passed"))
    n_artifacts_dir = sum(1 for r in successful if r.get("has_artifacts_dir"))
    phase_counts = [r.get("phase_count", 0) for r in successful]
    desc_chars_list = [r.get("description_chars", 0) for r in successful]

    summary = {
        "query": args.query,
        "n": n,
        "n_completed": len(successful),
        "n_errored": n - len(successful),
        "lint_pass_rate": round(n_lint_pass / max(len(successful), 1), 2),
        "artifacts_dir_present_rate": round(
            n_artifacts_dir / max(len(successful), 1), 2,
        ),
        "phase_count_distribution": {
            str(k): phase_counts.count(k) for k in sorted(set(phase_counts))
        },
        "mean_phase_count": round(
            sum(phase_counts) / max(len(phase_counts), 1), 2,
        ),
        "mean_description_chars": round(
            sum(desc_chars_list) / max(len(desc_chars_list), 1), 1,
        ),
        "mean_total_elapsed_s": round(
            sum(r.get("elapsed_total_s", 0) for r in successful)
            / max(len(successful), 1), 1,
        ),
    }

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\n=== RUNS ===")
    for r in results:
        compact = {
            "run": r["run"],
            "skill_name": r.get("skill_name", "(error)"),
            "lint_passed": r.get("lint_passed"),
            "phase_count": r.get("phase_count"),
            "description_chars": r.get("description_chars"),
            "has_artifacts_dir": r.get("has_artifacts_dir"),
            "elapsed_s": r.get("elapsed_total_s"),
        }
        print(json.dumps(compact, ensure_ascii=False))


if __name__ == "__main__":
    main()
