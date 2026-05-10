"""S9 batch-18 driver — cost preflight gate retest after B17-S9-1 fix.

Verifies LLM can output `control.type: "abort"` when `cost.threshold_exceeded: true`,
now that the abort CandidateOutput is added to _build_candidates (commit a4c1b47).

For each run:
  1. Fresh workspace (CWD = tmp dir per run)
  2. Symlink/copy minimal config (reyn.yaml + reyn.local.yaml with cost_warn_threshold=5)
  3. REYN_EMBEDDING_PROVIDER=fake + REYN_LLM_TRACE_DUMP=/tmp/reyn_s9_b18/run_<i>.jsonl
  4. Invoke `reyn run index_docs '<json_input>'` with cost_warn_threshold:5 in input
  5. Parse trace dump for LLM decision
  6. Inspect workspace for chunks.jsonl / SQLite db / sources.yaml side effects
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REYN_ROOT = Path("/Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2")
TRACE_BASE = Path("/tmp/reyn_s9_b18")
WORKSPACE_BASE = Path("/tmp/reyn_s9_b18_ws")

N = 3


def _cleanup() -> None:
    if TRACE_BASE.exists():
        shutil.rmtree(TRACE_BASE)
    TRACE_BASE.mkdir(parents=True)
    if WORKSPACE_BASE.exists():
        shutil.rmtree(WORKSPACE_BASE)
    WORKSPACE_BASE.mkdir(parents=True)


def _seed_workspace(ws: Path) -> None:
    """Set up a minimal workspace pointing at REYN_ROOT's source tree.

    We need:
      - reyn.yaml + reyn.local.yaml at workspace root (for config + model resolution)
      - the path glob (= "src/reyn/**/*.py") to resolve to REYN_ROOT's actual code
        — easiest: cwd = REYN_ROOT, but then .reyn/ writes go to the real repo
        — so: copy reyn.yaml + reyn.local.yaml to ws, but symlink src/ → REYN_ROOT/src/
    """
    ws.mkdir(parents=True, exist_ok=True)
    # Copy configs
    shutil.copy(REYN_ROOT / "reyn.yaml", ws / "reyn.yaml")
    # Custom reyn.local.yaml with cost_warn_threshold = 5
    local_cfg = (REYN_ROOT / "reyn.local.yaml").read_text()
    # Append embedding.cost_warn_threshold (in case OS injects from config in future)
    if "cost_warn_threshold" not in local_cfg:
        local_cfg = local_cfg + "\n\nembedding:\n  cost_warn_threshold: 5\n"
    (ws / "reyn.local.yaml").write_text(local_cfg)
    # Symlink src so glob "src/reyn/**/*.py" resolves
    (ws / "src").symlink_to(REYN_ROOT / "src", target_is_directory=True)


def _input_json() -> str:
    """Build input artifact JSON — note we explicitly include cost_warn_threshold:5
    because OS doesn't inject embedding.cost_warn_threshold from reyn.yaml into
    artifact data (B17-S9-2 deferred). This matches batch-17 S9 driver."""
    return json.dumps({
        "type": "index_docs_input",
        "data": {
            "source": "large",
            "path": "src/reyn/**/*.py",
            "description": "All Reyn Python source",
            "mode": "append",
            "cost_warn_threshold": 5,
        },
    })


def _run_one(i: int) -> dict:
    ws = WORKSPACE_BASE / f"run_{i}"
    _seed_workspace(ws)

    trace = TRACE_BASE / f"run_{i}.jsonl"
    env = os.environ.copy()
    env["REYN_EMBEDDING_PROVIDER"] = "fake"
    env["REYN_LLM_TRACE_DUMP"] = str(trace)
    env["PYTHONPATH"] = f"{REYN_ROOT}/src:{REYN_ROOT}/scripts"
    # Pre-register fake provider via sitecustomize-style import
    # Use a wrapper: invoke python -c that registers fake then runs the CLI
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys; sys.path.insert(0, '%s/scripts');"
            " from dogfood_rag_helper import register_fake_embedding_provider;"
            " register_fake_embedding_provider();"
            " from reyn.cli import main;"
            " main()"
        ) % REYN_ROOT,
        "run",
        "index_docs",
        _input_json(),
    ]
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=ws,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.time() - started

    # Inspect workspace for side effects
    chunks_jsonl = ws / ".reyn" / "workspace" / "artifacts" / "chunks.jsonl"
    # Try alternative artifact paths
    candidates = list(ws.rglob("chunks.jsonl"))
    chunks_path = candidates[0] if candidates else None
    sqlite_db = ws / ".reyn" / "index" / "large" / "index.db"
    sqlite_candidates = list(ws.rglob("index.db"))
    sqlite_path = sqlite_candidates[0] if sqlite_candidates else None
    sources_yaml = ws / ".reyn" / "state" / "sources.yaml"
    has_large_source = False
    if sources_yaml.exists():
        txt = sources_yaml.read_text()
        has_large_source = "large" in txt and "name:" in txt

    # Parse trace dump for LLM decision
    decision_info = _extract_decision(trace)

    return {
        "i": i,
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        "elapsed": elapsed,
        "chunks_path": str(chunks_path) if chunks_path else None,
        "chunks_count": _count_lines(chunks_path) if chunks_path else 0,
        "sqlite_path": str(sqlite_path) if sqlite_path else None,
        "sources_yaml_exists": sources_yaml.exists(),
        "sources_yaml_has_large": has_large_source,
        "decision": decision_info,
        "trace_path": str(trace),
    }


def _count_lines(p: Path | None) -> int:
    if not p or not p.exists():
        return 0
    with p.open() as f:
        return sum(1 for _ in f)


def _extract_decision(trace_path: Path) -> dict:
    """Pull LLM control.type / decision / reason from trace dump (if any)."""
    if not trace_path.exists():
        return {"found": False, "reason": "no trace file"}
    info: dict = {"found": False, "responses": []}
    try:
        with trace_path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # Trace entries are request/response records; we care about responses
                if rec.get("kind") == "response" or "response" in rec:
                    body = rec.get("response") or rec.get("body") or rec
                    # Try to find control / decision in nested payload
                    text_blob = json.dumps(body)
                    if '"control"' in text_blob or '"decision"' in text_blob:
                        info["responses"].append({
                            "raw_excerpt": text_blob[:1500],
                        })
        info["found"] = bool(info["responses"])
    except Exception as e:
        info["error"] = str(e)
    return info


def _classify(run: dict) -> str:
    """verified | refuted | inconclusive | blocked."""
    rc = run["rc"]
    stderr = run["stderr_tail"]
    chunks_count = run["chunks_count"]
    sqlite_exists = bool(run["sqlite_path"])
    has_large = run["sources_yaml_has_large"]

    # Did LLM emit abort? Best signal: workflow_aborted in stderr or stdout
    aborted = (
        "WorkflowAborted" in stderr
        or "workflow_aborted" in stderr
        or "WorkflowAborted" in run.get("stdout", "")
        or "decision_abort" in stderr
        or '"abort"' in run.get("stdout", "")
    )

    # If we created chunks or SQLite or sources entry → postprocessor ran → not aborted
    side_effects = chunks_count > 0 or sqlite_exists or has_large

    if aborted and not side_effects:
        return "verified"
    if not aborted and side_effects:
        return "refuted"
    if not aborted and not side_effects:
        # Could be timeout / driver error (rc != 0 but no side effects, no abort)
        if rc != 0 and "timeout" in stderr.lower():
            return "inconclusive"
        # If finished cleanly with no effects → unclear (could have aborted silently)
        return "inconclusive"
    return "inconclusive"


def main() -> None:
    _cleanup()
    runs: list[dict] = []
    for i in range(1, N + 1):
        print(f"=== Run {i}/{N} starting ===", flush=True)
        try:
            r = _run_one(i)
        except subprocess.TimeoutExpired as e:
            r = {
                "i": i,
                "rc": -1,
                "stdout": "",
                "stderr_tail": f"TIMEOUT after {e.timeout}s",
                "elapsed": float(e.timeout or 0),
                "chunks_path": None,
                "chunks_count": 0,
                "sqlite_path": None,
                "sources_yaml_exists": False,
                "sources_yaml_has_large": False,
                "decision": {"found": False, "reason": "timeout"},
                "trace_path": str(TRACE_BASE / f"run_{i}.jsonl"),
            }
        verdict = _classify(r)
        r["verdict"] = verdict
        runs.append(r)
        print(f"  rc={r['rc']} chunks={r['chunks_count']} sqlite={bool(r['sqlite_path'])} "
              f"has_large={r['sources_yaml_has_large']} verdict={verdict} elapsed={r['elapsed']:.1f}s",
              flush=True)
        # tail of stderr / stdout for visibility
        print(f"  stdout_tail: {r['stdout'][-500:]!r}", flush=True)
        print(f"  stderr_tail: {r['stderr_tail'][-500:]!r}", flush=True)

    summary = {
        "verified": sum(1 for r in runs if r["verdict"] == "verified"),
        "refuted": sum(1 for r in runs if r["verdict"] == "refuted"),
        "inconclusive": sum(1 for r in runs if r["verdict"] == "inconclusive"),
        "blocked": sum(1 for r in runs if r["verdict"] == "blocked"),
    }
    print(f"\n=== SUMMARY === {summary}", flush=True)

    # Write structured result for downstream parse
    out = TRACE_BASE / "_summary.json"
    out.write_text(json.dumps({"runs": runs, "summary": summary}, indent=2, default=str))
    print(f"\nResult JSON: {out}", flush=True)


if __name__ == "__main__":
    main()
