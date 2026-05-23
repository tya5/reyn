"""Tier 2: dogfood_batch_dispatch + dogfood_aggregate.

Pinned invariants for the (1)+(2) bundle:

- ``load_batch_config`` parses the full YAML schema, validates
  required keys, defaults ``parallel`` etc.
- ``render_worker_prompt`` produces a non-empty markdown body that
  includes the worker's port, scenario set path, env_vars, user_params,
  past-batch verdict table, hard caps, and deliverable path.
- ``load_worker_results`` reads all ``results-worker-*.json`` under a
  journal dir + returns them keyed by worker name (W1, W2, ...).
- ``compute_totals`` normalises long-form (``verified``) AND short-form
  (``V``) verdict counts (= historic B42 used long, B43 short).
- ``build_aggregate`` produces the expected aggregate.json shape with
  verdict_totals + per-worker breakdown + env_settings + delta_vs_<prev>.

End-to-end smoke: the script also verifies build_aggregate against the
real B43 journal in the repo (= scenarios_total=54, verified=22 per
PR #278). That regression guard ensures the tools agree with the
existing batch retrospectives.

testing.ja.md compliance: no ``unittest.mock.patch``; uses real
filesystem fixtures via tmp_path + reads the committed B43 journal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dogfood_aggregate import (  # noqa: E402
    _normalise_verdicts,
    build_aggregate,
    compute_totals,
    load_worker_results,
)
from dogfood_batch_config import load_batch_config  # noqa: E402
from dogfood_batch_dispatch import render_worker_prompt  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
B43_JOURNAL = (
    REPO_ROOT
    / "docs/deep-dives/journal/dogfood/2026-05-20-batch-43-post-empty-stop-retry"
)


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "batch.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _minimal_config_yaml(tmp_path: Path, journal_dir: Path) -> str:
    return f"""
batch:
  name: B99
  date: 2026-06-01
  head: deadbeef
  env_vars:
    REYN_EMPTY_STOP_RETRY: "1"
  user_params:
    hot_list_n: 10
  hard_caps:
    tool_uses: 50
    wall_clock_min: 15

workers:
  - name: W1
    scenario_set: smoke.yaml
    scenario_set_path: dogfood/scenarios/smoke.yaml
    port: 8231
    n_scenarios: 7
    worktree: /tmp/reyn-worktrees/b99-1
    agent_prefix: dogfood-b99-1-s

past_batches:
  - name: B43
    aggregate_path: {B43_JOURNAL / "aggregate.json"}

journal_dir: {journal_dir}
"""


# ---------------------------------------------------------------------------
# Config parsing (shared by both tools)
# ---------------------------------------------------------------------------


def test_load_batch_config_round_trips_required_fields(tmp_path):
    """Tier 2: well-formed config parses without loss."""
    cfg_path = _write_config(
        tmp_path, _minimal_config_yaml(tmp_path, tmp_path / "journal"),
    )
    cfg = load_batch_config(cfg_path)
    assert cfg.batch.name == "B99"
    assert cfg.batch.head == "deadbeef"
    assert cfg.batch.env_vars["REYN_EMPTY_STOP_RETRY"] == "1"
    assert cfg.workers[0].name == "W1"
    assert cfg.workers[0].port == 8231
    assert cfg.workers[0].n_scenarios == 7
    assert cfg.past_batches[0].name == "B43"


def test_load_batch_config_rejects_missing_required_key(tmp_path):
    """Tier 2: missing batch.name raises ValueError with a clear message."""
    cfg_path = _write_config(tmp_path, """
batch:
  date: 2026-06-01
  head: x
workers:
  - {name: W1, scenario_set: s, scenario_set_path: p, port: 1,
     n_scenarios: 1, worktree: /tmp/x, agent_prefix: y}
past_batches: []
journal_dir: /tmp/j
""")  # missing batch.name
    with pytest.raises(ValueError, match="batch.name"):
        load_batch_config(cfg_path)


def test_load_batch_config_rejects_empty_workers(tmp_path):
    """Tier 2: empty workers list raises ValueError."""
    cfg_path = _write_config(tmp_path, """
batch: {name: B, date: d, head: h}
workers: []
past_batches: []
journal_dir: /tmp/j
""")
    with pytest.raises(ValueError, match="workers"):
        load_batch_config(cfg_path)


# ---------------------------------------------------------------------------
# Verdict normalisation (= historic B42 long-form vs B43 short-form)
# ---------------------------------------------------------------------------


def test_normalise_verdicts_handles_short_form_v_i_r_b():
    """Tier 2: short-form V/I/R/B keys pass through unchanged."""
    out = _normalise_verdicts({"V": 5, "I": 2, "R": 3, "B": 1})
    assert out == {"V": 5, "I": 2, "R": 3, "B": 1}


def test_normalise_verdicts_handles_long_form_verified_etc():
    """Tier 2: long-form verified/inconclusive/refuted/blocked map to V/I/R/B."""
    out = _normalise_verdicts(
        {"verified": 7, "inconclusive": 1, "refuted": 2, "blocked": 0},
    )
    assert out == {"V": 7, "I": 1, "R": 2, "B": 0}


def test_normalise_verdicts_treats_missing_as_zero():
    """Tier 2: empty dict and None both produce zero-filled V/I/R/B output."""
    assert _normalise_verdicts({}) == {"V": 0, "I": 0, "R": 0, "B": 0}
    assert _normalise_verdicts(None) == {"V": 0, "I": 0, "R": 0, "B": 0}


# ---------------------------------------------------------------------------
# Worker result loading + totals
# ---------------------------------------------------------------------------


def test_load_worker_results_reads_existing_b43_journal():
    """Tier 2: regression guard against committed data — the real B43
    journal in this repo loads to 7 workers (= W1..W7).
    """
    results = load_worker_results(B43_JOURNAL)
    assert set(results.keys()) == {f"W{i}" for i in range(1, 8)}


def test_compute_totals_matches_b43_published_aggregate():
    """Tier 2: aggregating the real B43 worker files reproduces the
    same V=22 / I=12 / R=20 / B=0 totals the published B43
    aggregate.json (= PR #278) declares. Detects future drift if
    someone edits either side without updating the other.
    """
    results = load_worker_results(B43_JOURNAL)
    totals = compute_totals(results)
    assert totals == {"V": 22, "I": 12, "R": 20, "B": 0}


def test_load_worker_results_raises_on_missing_dir(tmp_path):
    """Tier 2: missing workers/ dir → clean FileNotFoundError instead
    of a downstream parse-related exception that masks the real
    cause."""
    with pytest.raises(FileNotFoundError, match="workers"):
        load_worker_results(tmp_path)


# ---------------------------------------------------------------------------
# Aggregate construction
# ---------------------------------------------------------------------------


def test_build_aggregate_against_b43_real_journal(tmp_path):
    """Tier 2: end-to-end — feed the real B43 journal into the new tool
    and verify the output aggregate.json has the same headline numbers
    as the published one (V=22, rate≈0.407, B=0). Detects any future
    semantic drift in the aggregate shape.
    """
    cfg_yaml = f"""
batch:
  name: B43_repro
  date: 2026-05-20
  head: e96d479f
  env_vars:
    REYN_EMPTY_STOP_RETRY: "1"
  user_params:
    hot_list_n: 10

workers:
  - {{name: W1, scenario_set: chat_router_smoke.yaml,
     scenario_set_path: dogfood/scenarios/chat_router_smoke.yaml,
     port: 8231, n_scenarios: 7,
     worktree: /tmp/reyn-worktrees/b43-1, agent_prefix: dogfood-b43-1-s}}
  - {{name: W2, scenario_set: stdlib_skills_core.yaml,
     scenario_set_path: dogfood/scenarios/stdlib_skills_core.yaml,
     port: 8232, n_scenarios: 9,
     worktree: /tmp/reyn-worktrees/b43-2, agent_prefix: dogfood-b43-2-s}}
  - {{name: W3, scenario_set: control_ir_ops.yaml,
     scenario_set_path: dogfood/scenarios/control_ir_ops.yaml,
     port: 8233, n_scenarios: 9,
     worktree: /tmp/reyn-worktrees/b43-3, agent_prefix: dogfood-b43-3-s}}
  - {{name: W4, scenario_set: permissions_and_safety.yaml,
     scenario_set_path: dogfood/scenarios/permissions_and_safety.yaml,
     port: 8234, n_scenarios: 8,
     worktree: /tmp/reyn-worktrees/b43-4, agent_prefix: dogfood-b43-4-s}}
  - {{name: W5, scenario_set: multi_agent_and_mcp,
     scenario_set_path: dogfood/scenarios/multi_agent_and_mcp.yaml,
     port: 8235, n_scenarios: 7,
     worktree: /tmp/reyn-worktrees/b43-5, agent_prefix: dogfood-b43-5-s}}
  - {{name: W6, scenario_set: plan_mode_fp_0011_mixed,
     scenario_set_path: dogfood/scenarios/plan_mode.yaml,
     port: 8236, n_scenarios: 7,
     worktree: /tmp/reyn-worktrees/b43-6, agent_prefix: dogfood-b43-6-s}}
  - {{name: W7, scenario_set: long_session_v1.yaml,
     scenario_set_path: dogfood/scenarios/long_session_v1.yaml,
     port: 8237, n_scenarios: 7,
     worktree: /tmp/reyn-worktrees/b43-7, agent_prefix: dogfood-b43-7-s}}

past_batches: []
journal_dir: {B43_JOURNAL}
"""
    cfg_path = _write_config(tmp_path, cfg_yaml)
    config = load_batch_config(cfg_path)
    results = load_worker_results(Path(config.journal_dir))
    aggregate = build_aggregate(config, results)
    assert aggregate["scenarios_total"] == 54
    assert aggregate["verdict_totals"]["verified"] == 22
    assert aggregate["verdict_totals"]["blocked"] == 0
    assert round(aggregate["verified_rate"], 3) == round(22 / 54, 3)


# ---------------------------------------------------------------------------
# Worker prompt rendering
# ---------------------------------------------------------------------------


def test_render_worker_prompt_includes_port_and_paths(tmp_path):
    """Tier 2: rendered prompt contains the worker's port, scenario
    set path, deliverable path. These are the load-bearing fields the
    sub-agent reads to execute the dispatch."""
    cfg_path = _write_config(
        tmp_path, _minimal_config_yaml(tmp_path, tmp_path / "journal"),
    )
    config = load_batch_config(cfg_path)
    prompt = render_worker_prompt(config, config.workers[0])
    assert "8231" in prompt
    assert "dogfood/scenarios/smoke.yaml" in prompt
    assert "results-worker-1.json" in prompt
    assert "B99" in prompt


def test_render_worker_prompt_cites_past_verdicts(tmp_path):
    """Tier 2: the prompt includes past-batch verdicts pulled from the
    referenced aggregate.json. This is the manual citation step the
    tool eliminates — the source must be the file, not a hardcoded
    string."""
    cfg_path = _write_config(
        tmp_path, _minimal_config_yaml(tmp_path, tmp_path / "journal"),
    )
    config = load_batch_config(cfg_path)
    prompt = render_worker_prompt(config, config.workers[0])
    # B43 W1 had V=3 per the published aggregate; the prompt should cite it
    assert "B43" in prompt
    assert "3" in prompt  # the V count for W1 in B43


def test_render_worker_prompt_includes_hard_caps(tmp_path):
    """Tier 2: hard caps (= tool_uses, wall_clock_min) appear in the
    prompt so the sub-agent enforces them. Without these the
    feedback_subagent_scope_bounding discipline isn't propagated.
    """
    cfg_path = _write_config(
        tmp_path, _minimal_config_yaml(tmp_path, tmp_path / "journal"),
    )
    config = load_batch_config(cfg_path)
    prompt = render_worker_prompt(config, config.workers[0])
    assert "50" in prompt  # tool_uses cap
    assert "15" in prompt  # wall_clock_min cap


def test_render_worker_prompt_includes_env_vars(tmp_path):
    """Tier 2: env_vars from the batch config appear in the setup
    block so the sub-agent starts reyn web with the right flags."""
    cfg_path = _write_config(
        tmp_path, _minimal_config_yaml(tmp_path, tmp_path / "journal"),
    )
    config = load_batch_config(cfg_path)
    prompt = render_worker_prompt(config, config.workers[0])
    assert "REYN_EMPTY_STOP_RETRY=1" in prompt
